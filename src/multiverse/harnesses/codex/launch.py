"""Pure remote-TUI launch protocol transforms; core owns transport and binding."""
import json
from copy import deepcopy
from functools import partial
from pathlib import Path
import tomllib

from ..types import NativeResult, RpcRequest, RpcNotification, RpcSpec, normalize_session_id
from .protocol import result, object_field


def tui_arguments(argv):
    """Avoid unattended update dialogs unless launch arguments explicitly opt in."""
    argv = list(argv)
    end = argv.index('--') if '--' in argv else len(argv)
    index = 1
    while index < end:
        arg = argv[index]
        value = ''
        if arg in ('-c', '--config'):
            index += 1
            if index < end:
                value = argv[index]
        elif arg.startswith('--config='):
            value = arg[len('--config='):]
        elif arg.startswith('-c') and not arg.startswith('--'):
            value = arg[2:].removeprefix('=')
        if value.partition('=')[0].strip() == 'check_for_update_on_startup':
            return argv
        index += 1
    argv[end:end] = ['-c', 'check_for_update_on_startup=false']
    return argv


def plain_arguments(argv, recipe, values):
    """Prepare a retry removing only mv2's root, with no native-policy guess."""
    argv = list(argv)
    own_root = next((index for index in range(len(recipe) - 1)
                     if recipe[index:index + 2] == ['--add-dir', '{agent_dir}']), None)
    if own_root is None or argv[own_root:own_root + 2] != ['--add-dir', values['agent_dir']]:
        return argv, False
    del argv[own_root:own_root + 2]
    return argv, True


def startup_retryable(snapshot):
    """The native TUI rejected add-dir before startup; a live warning is not a retry."""
    import re
    if type(snapshot.get('exit_code')) is not int or snapshot['exit_code'] == 0:
        return False
    text = snapshot.get('text')
    if not isinstance(text, str) or len(text) > 32768 or '\x1b' in text or '\0' in text:
        return False
    # tmux may wrap in the middle of native words and paths. Match the whole
    # diagnostic after removing screen whitespace, never a quoted substring.
    compact = ''.join(text.split())
    return re.fullmatch(
        r'Erroraddingdirectories:Ignoring--add-dir\(.+\)because'
        r'theeffectivepermissionsdonotallowadditionalwritableroots\.'
        r'Switchtoworkspace-writeordanger-full-accesstoallowthem\.', compact) is not None


def remote_arguments(argv, context):
    """Move roots, bootstrap and explicit resume grants from CLI flags to RPC."""
    end = argv.index('--') if '--' in argv else len(argv)
    output, roots, permissions = [], [], {}
    permission_flags = {'--sandbox': 'sandbox', '-s': 'sandbox',
                        '--ask-for-approval': 'approvalPolicy', '-a': 'approvalPolicy',
                        '--dangerously-bypass-approvals-and-sandbox': 'bypass'}
    bootstrap = json.loads(context.values['bootstrap_json'])
    index = 0
    while index < end:
        arg = argv[index]
        flag, equal, value = arg.partition('=')
        if arg.startswith('-c') and not arg.startswith('--') and arg != '-c':
            flag, equal, value = '-c', '=', arg[2:].removeprefix('=')
        if context.mode == 'resume' and arg[:2] in ('-s', '-a') and len(arg) > 2:
            flag, equal, value = arg[:2], '=', arg[2:].removeprefix('=')
        if context.mode == 'resume' and flag in permission_flags:
            key = permission_flags[flag]
            if key in permissions:
                raise ValueError(f'{flag} cannot be used multiple times')
            if key == 'bypass':
                if equal:
                    raise ValueError(f'{flag} does not take a value')
                permissions[key] = True
            else:
                if not equal:
                    index += 1
                    if index >= end:
                        raise ValueError(f'Missing value for {flag}')
                    value = argv[index]
                allowed = ('read-only', 'workspace-write', 'danger-full-access') if key == 'sandbox' else ('on-request', 'never')
                if value not in allowed:
                    raise ValueError(f'Invalid {flag}: {value!r}; expected one of {", ".join(allowed)}')
                permissions[key] = value
        elif flag in ('--add-dir', '-c', '--config'):
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
    # Match native CLI precedence when bypass is combined with explicit policies.
    if permissions.pop('bypass', False):
        permissions.update(sandbox='danger-full-access', approvalPolicy='never')
    return [*output, *argv[end:]], bootstrap, list(dict.fromkeys(roots)), permissions


def launch_settings(workspace):
    initialized = result((yield RpcRequest('initialize', {
        'clientInfo': {'name': 'multiverse', 'version': '1'},
        'capabilities': {'experimentalApi': True},
    }, on_success=(RpcNotification('initialized', {}),))))
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
    from .plugins import server_version
    return NativeResult('ok', value={'exclude': exclude, 'writable_roots': roots,
                                    'workspace_policy': config.get('sandbox_workspace_write') or {},
                                    'daemon_version': server_version(initialized.get('userAgent'))})


def name_session(native_id, name):
    initialized = result((yield RpcRequest('initialize', {
        'clientInfo': {'name': 'multiverse', 'version': '1'},
    }, on_success=(RpcNotification('initialized', {}),))))
    if isinstance(initialized, NativeResult):
        return initialized
    named = result((yield RpcRequest('thread/name/set', {'threadId': native_id, 'name': name})))
    return named if isinstance(named, NativeResult) else NativeResult('ok')


class LaunchProtocol:
    def __init__(self, settings):
        self.settings = settings
        self.native_id = settings.get('native_id')
        self.pending = None
        self.pending_method = None
        self.bound = False
        self.launch_refusal = None
        self.prompt_request = None
        self.prompt_submitted = False
        self.prompt_error = None

    def for_connection(self, connection_id, owns_primary):
        from .side import LaunchConnection
        return LaunchConnection(self, connection_id, owns_primary)

    def outgoing(self, value):
        method = value.get('method')
        if method in ('turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start'):
            if self.launch_refusal:
                raise ValueError(self.launch_refusal['message'])
            if not self.bound:
                raise ValueError('Native conversation binding is not confirmed; input was not submitted')
        if (method == 'turn/start' and 'id' in value and self.bound and not self.prompt_submitted
                and self.prompt_request is None and (value.get('params') or {}).get('threadId') == self.native_id):
            self.prompt_request = value['id']
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
        else:
            params.update(self.settings.get('resume_permissions') or {})
        from .plugins import config_tree, merge_config
        defaults = self.settings.get('plugin_config') or {}
        explicit = self.settings.get('plugin_user_config') or {}
        if defaults or explicit:
            native = config_tree((object_field(params, 'config') if params.get('config') is not None else {}).items())
            params['config'] = deepcopy(merge_config(merge_config(defaults, native), explicit))
            if method == 'thread/start':
                # Resolve only references declared by each accepted MCP server.
                # Native/user env values win, and changing env_vars in the request
                # drops defaults that the caller no longer wants forwarded.
                servers = params['config'].get('mcp_servers')
                for name, values in (self.settings.get('plugin_mcp_environment') or {}).items():
                    server = servers.get(name) if isinstance(servers, dict) else None
                    if not isinstance(server, dict) or not isinstance(server.get('env_vars'), list):
                        continue
                    forwarded = {key: value for key, value in values.items() if key in server['env_vars']}
                    if forwarded:
                        server = deepcopy(server)
                        server['env'] = {**forwarded, **object_field(server, 'env')} if server.get('env') is not None else forwarded
                        server['env_vars'] = [key for key in server['env_vars'] if key not in forwarded]
                        servers[name] = server
        if 'agent_environment' in self.settings:
            from .identity import session_environment
            native = config_tree((params.get('config') or {}).items())
            params['config'] = session_environment(deepcopy(native), self.settings['agent_environment'])
        existing = params.get('developerInstructions')
        params['developerInstructions'] = '\n\n'.join(part for part in (self.settings['bootstrap'], existing) if part)
        self.pending = value['id']
        self.pending_method = method
        return {**value, 'params': params}

    def incoming(self, value):
        # Native R may succeed later, but only a new mv2 launch can revalidate
        # a refused attachment. Never turn that retry into binding/readiness.
        if self.launch_refusal is not None:
            return None
        if 'method' not in value and self.prompt_request is not None and value.get('id') == self.prompt_request:
            self.prompt_request = None
            observed = result(value)
            if isinstance(observed, NativeResult):
                self.prompt_error = observed.reason
            else:
                self.prompt_submitted = True
        if 'method' in value or self.pending is None or value.get('id') != self.pending:
            return None
        method = self.pending_method
        self.pending = self.pending_method = None
        observed = result(value)
        if isinstance(observed, NativeResult):
            if not self.bound:
                error = object_field(value, 'error')
                self.launch_refusal = {'source': 'native-rpc', 'method': method,
                    'code': error['code'], 'message': error['message'],
                    'native_id': self.native_id}
            return None
        thread = object_field(observed, 'thread')
        native_id = normalize_session_id(thread.get('id'))
        if self.native_id and native_id != self.native_id:
            raise ValueError('Native reply returned a different thread ID')
        if not isinstance(thread.get('cwd'), str) or Path(thread['cwd']).resolve() != Path(self.settings['workspace']).resolve():
            raise ValueError('Native thread workspace differs from the saved workspace')
        permissions = self.settings.get('resume_permissions') or {}
        for key, expected in permissions.items():
            actual = observed.get(key)
            if key == 'sandbox':
                actual = actual.get('type') if isinstance(actual, dict) else None
                expected = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite',
                            'danger-full-access': 'dangerFullAccess'}[expected]
            if actual != expected:
                raise ValueError(f'Native resume did not apply requested {key}: expected {expected}, got {actual}. '
                                 'A shared Codex conversation keeps its current grants. Use those grants or '
                                 'deliberately change them through Codex before a new mv2 resume. '
                                 'This launch cannot accept input until its requested grant is verified; '
                                 'Multiverse will not reconfigure or restart the shared daemon.')
        self.native_id = native_id
        self.bound = True
        return native_id

    def after_bind(self, upstream_path):
        """Name only the validated thread, after core accepts its launch binding."""
        # Saved launch requests from before naming was supported have no name.
        if self.bound and self.settings.get('name'):
            return RpcSpec(upstream_path, partial(name_session, self.native_id, self.settings['name']), max_requests=2)
        return None

    def usage(self, value):
        from .usage import notification
        # Even a saved resume ID must be checked against the native reply first.
        return notification(value, self.native_id) if self.bound else None
