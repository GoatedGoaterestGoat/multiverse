"""Pure remote-TUI launch protocol transforms; core owns transport and binding."""
import json
from pathlib import Path
import tomllib

from ..types import NativeResult, RpcRequest, normalize_session_id
from .protocol import result, object_field


def remote_arguments(argv, context):
    """Move writable roots and bootstrap from rejected/dropped CLI flags to RPC."""
    end = argv.index('--') if '--' in argv else len(argv)
    output, roots = [], []
    bootstrap = json.loads(context.values['bootstrap_json'])
    index = 0
    while index < end:
        arg = argv[index]
        flag, equal, value = arg.partition('=')
        if arg.startswith('-c') and not arg.startswith('--') and arg != '-c':
            flag, equal, value = '-c', '=', arg[2:].removeprefix('=')
        if flag in ('--add-dir', '-c', '--config'):
            if not equal:
                index += 1
                if index >= end:
                    raise ValueError(f'Missing value for {flag}')
                value = argv[index]
            if flag == '--add-dir':
                if context.mode == 'spawn':
                    roots.append(str((Path(context.values['workspace']) / value).resolve()))
            else:
                name, _, literal = value.partition('=')
                if name == 'developer_instructions':
                    try:
                        bootstrap = tomllib.loads('value=' + literal)['value']
                    except tomllib.TOMLDecodeError:
                        bootstrap = literal
                    if not isinstance(bootstrap, str):
                        raise ValueError('developer_instructions must be text')
                elif name == 'sandbox_workspace_write.writable_roots':
                    configured = tomllib.loads('value=' + literal)['value']
                    if not isinstance(configured, list) or any(not isinstance(root, str) for root in configured):
                        raise ValueError('Native writable roots must be an array of paths')
                    if context.mode == 'resume':
                        raise ValueError('Native resume retains saved permissions; put writable roots in spawn_args')
                    roots.extend(str((Path(context.values['workspace']) / root).resolve()) for root in configured)
                elif name == 'sandbox_workspace_write':
                    raise ValueError('Codex bridge requires individual sandbox_workspace_write keys')
                else:
                    output.extend(['-c', value])
        else:
            output.append(arg)
        index += 1
    return [*output, *argv[end:]], bootstrap, list(dict.fromkeys(roots))


def launch_settings(workspace):
    initialized = result((yield RpcRequest('initialize', {
        'clientInfo': {'name': 'multiverse', 'version': '1'},
        'capabilities': {'experimentalApi': True},
    })))
    if isinstance(initialized, NativeResult):
        return initialized
    observed = result((yield RpcRequest('config/read', {'cwd': workspace, 'includeLayers': False})))
    if isinstance(observed, NativeResult):
        return observed
    config = object_field(observed, 'config')
    exclude = (config.get('shell_environment_policy') or {}).get('exclude') or []
    roots = (config.get('sandbox_workspace_write') or {}).get('writable_roots') or []
    if any(not isinstance(items, list) or any(not isinstance(item, str) for item in items) for items in (exclude, roots)):
        raise ValueError('Native exclusions and writable roots must be arrays of strings')
    return NativeResult('ok', value={'exclude': exclude, 'writable_roots': roots,
                                    'workspace_policy': config.get('sandbox_workspace_write') or {}})


class LaunchProtocol:
    def __init__(self, settings):
        self.settings = settings
        self.native_id = settings.get('native_id')
        self.pending = None
        self.bound = False

    def outgoing(self, value):
        method = value.get('method')
        if method == 'thread/fork':
            raise ValueError('Fork into a separate Multiverse launch to preserve session identity')
        if method not in ('thread/start', 'thread/resume') or 'id' not in value:
            return value
        params = dict(object_field(value, 'params'))
        if method == 'thread/start':
            if self.native_id or self.pending is not None:
                raise ValueError('This launch already owns a native thread')
            config = dict(object_field(params, 'config')) if params.get('config') is not None else {}
            workspace = dict(self.settings.get('workspace_policy') or {})
            workspace.update(config.get('sandbox_workspace_write') or {})
            root_lists = [self.settings['roots'], workspace.get('writable_roots') or [],
                          config.pop('sandbox_workspace_write.writable_roots', []) or []]
            if any(not isinstance(roots, list) or any(not isinstance(root, str) for root in roots) for roots in root_lists):
                raise ValueError('Native writable roots must be arrays of paths')
            roots = [root for roots in root_lists for root in roots]
            workspace['writable_roots'] = list(dict.fromkeys(roots))
            config['sandbox_workspace_write'] = workspace
            params['config'] = config
        elif not self.native_id or params.get('threadId') != self.native_id:
            raise ValueError('Native resume does not match this launch')
        existing = params.get('developerInstructions')
        params['developerInstructions'] = '\n\n'.join(part for part in (self.settings['bootstrap'], existing) if part)
        self.pending = value['id']
        return {**value, 'params': params}

    def incoming(self, value):
        if 'method' in value or self.pending is None or value.get('id') != self.pending:
            return None
        self.pending = None
        observed = result(value)
        if isinstance(observed, NativeResult):
            return None
        thread = object_field(observed, 'thread')
        native_id = normalize_session_id(thread.get('id'))
        if self.native_id and native_id != self.native_id:
            raise ValueError('Native reply returned a different thread ID')
        if not isinstance(thread.get('cwd'), str) or Path(thread['cwd']).resolve() != Path(self.settings['workspace']).resolve():
            raise ValueError('Native thread workspace differs from the saved workspace')
        self.native_id = native_id
        self.bound = True
        return native_id

    def usage(self, value):
        from .usage import notification
        # Even a saved resume ID must be checked against the native reply first.
        return notification(value, self.native_id) if self.bound else None
