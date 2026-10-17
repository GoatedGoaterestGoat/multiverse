"""Declarative Codex launch additions; never install native plugins or change config files.

The remote TUI drops MCP overrides, so approved spawn config and the user's
overlapping overrides also travel in the adapter's thread/start transform.
Records contain names and hashes, never config values or instruction content.
"""
from dataclasses import dataclass, field, replace
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
from string import Formatter
import subprocess
import tomllib
from urllib.parse import parse_qsl, urlsplit

from ...plugins import harness, manifest, trust


CONFIG_VERSION = (0, 159, 3)  # checked versioned config schema
MCP_VERSION = (0, 162, 1)     # isolated native spawn verified on this release
_versions = {}
_name = re.compile(r'[a-z0-9]+(?:[-_][a-z0-9]+)*\Z')
_secret = re.compile(r'token|secret|password|credential|authorization|cookie|api.?key', re.I)
_enum = {
    'model_reasoning_summary': {'auto', 'concise', 'detailed', 'none'},
    'model_verbosity': {'low', 'medium', 'high'},
    'tui.alternate_screen': {'auto', 'always', 'never'},
    'tui.notification_method': {'auto', 'osc9', 'bel'},
}
CONFIG_KEYS = frozenset((*_enum, 'model_reasoning_effort', 'tui.animations',
                         'tui.notifications', 'tui.status_line'))
# codex-rs/tui/src/bottom_pane/status_line_setup.rs, rust-v0.159.3
STATUS_ITEMS = frozenset('model model-name model-with-reasoning reasoning current-dir project-name '
    'project project-root hostname git-branch pull-request-number branch-changes run-state status '
    'permissions approval-mode approval context-remaining context-used context-usage five-hour-limit '
    'weekly-limit codex-version context-window-size used-tokens total-input-tokens total-output-tokens '
    'thread-credits estimated-thread-cost thread-id session-id fast-mode raw-output thread-name '
    'thread-title workspace-headline task-progress'.split())


def _plugin_fingerprint(directory):
    """Provenance of the selected snapshot, before cache markers/runtime writes.

    A marker in an ordinary plugin is just another source file. Only the
    canonical content-addressed cache may supply its original trusted hash.
    """
    value = 'sha256:' + directory.name
    if re.fullmatch(r'[0-9a-f]{64}', directory.name) and directory == trust.cache_directory(value).resolve():
        try:
            if (directory / trust.COMPLETE).read_text().strip() == value:
                return value
        except (OSError, UnicodeError):
            pass
    return trust.fingerprint(directory)


def version_text(version):
    return '.'.join(map(str, version)) if version is not None else None


def server_version(user_agent):
    """Only a release version in initialize's native NAME/VERSION prefix is evidence."""
    match = re.match(r'^[^\s/]+/(\d+)\.(\d+)\.(\d+)(?:\s|$)', user_agent) if isinstance(user_agent, str) else None
    return tuple(map(int, match.groups())) if match else None


def codex_version(executable, workspace):
    """Read the selected binary's release version, without loading config or a daemon."""
    candidate = str(Path(workspace, executable)) if '/' in executable and not Path(executable).is_absolute() else executable
    path = shutil.which(candidate)
    if path is None:
        return None
    try:
        resolved = Path(path).resolve()
        stat = resolved.stat()
        key = (str(resolved), stat.st_mtime_ns, stat.st_size)
        if key not in _versions:
            output = subprocess.run([str(resolved), '--version'], capture_output=True, text=True,
                                    cwd=workspace, timeout=5, check=False)
            match = re.fullmatch(r'(?:codex-cli\s+)?(\d+)\.(\d+)\.(\d+)\s*', output.stdout)
            _versions[key] = tuple(map(int, match.groups())) if output.returncode == 0 and match else None
        return _versions[key]
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def overrides(argv):
    """Ordered native CLI config paths, stopping before literal prompt/session arguments."""
    index = 0
    while index < len(argv) and argv[index] != '--':
        arg, literal = argv[index], None
        if arg in ('-c', '--config', '--enable', '--disable'):
            index += 1
            if index < len(argv) and argv[index] != '--':
                literal = argv[index]
                if arg in ('--enable', '--disable'):
                    literal = 'features.' + literal + '=' + ('true' if arg == '--enable' else 'false')
        elif arg.startswith('--config='):
            literal = arg[len('--config='):]
        elif arg.startswith('-c') and not arg.startswith('--'):
            literal = arg[2:].removeprefix('=')
        elif arg.startswith(('--enable=', '--disable=')):
            flag, _, name = arg.partition('=')
            literal = 'features.' + name + '=' + ('true' if flag == '--enable' else 'false')
        if literal is not None and '=' in literal:
            key, _, value = literal.partition('=')
            try:
                value = tomllib.loads('value=' + value)['value']
            except (tomllib.TOMLDecodeError, KeyError):
                pass  # Codex's own raw string fallback.
            yield key.strip(), value
        index += 1


def overlaps(left, right):
    return left == right or left.startswith(right + '.') or right.startswith(left + '.')


def toml_value(value):
    """Encode typed values as TOML; JSON objects are not TOML inline tables."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace('\x7f', r'\u007f')
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) in (int, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, list):
        return '[' + ','.join(toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + toml_value(item) for key, item in value.items()) + '}'
    raise ValueError('not a TOML config value')


def config_tree(pairs):
    """Apply native literal-dot paths in order to a private object."""
    result = {}
    for path, value in pairs:
        target = result
        parts = path.split('.')
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = deepcopy(value)
    return result


def merge_config(base, extra):
    """Native request fields override defaults; objects compose without shared mutation."""
    result = dict(base)
    for key, value in extra.items():
        result[key] = (merge_config(result[key], value)
                       if isinstance(result.get(key), dict) and isinstance(value, dict) else value)
    return result


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item and '\0' not in item for item in value)


def _text(value, label):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError(label + ' must be nonempty text without NUL characters')
    return value


def _config(key, value):
    if key not in CONFIG_KEYS:
        raise ValueError(f'config {key!r} is not allowed for a Codex plugin')
    if key in _enum:
        valid = isinstance(value, str) and value in _enum[key]
    elif key == 'model_reasoning_effort':
        valid = isinstance(value, str) and bool(value.strip()) and '\0' not in value
    elif key == 'tui.animations':
        valid = type(value) is bool
    elif key == 'tui.notifications':
        valid = type(value) is bool or _strings(value)
    else:
        valid = isinstance(value, list) and all(isinstance(v, str) and v in STATUS_ITEMS for v in value)
    if not valid:
        raise ValueError(f'config {key!r} has an unsupported type or value')


def _expand(value, directory):
    if not isinstance(value, str) or '\0' in value:
        raise ValueError('MCP argument must be text without NUL characters')
    for _, name, fmt, conversion in Formatter().parse(value):
        if name is not None and (name != 'plugin_dir' or fmt or conversion):
            raise ValueError('MCP arguments only support the {plugin_dir} placeholder')
    return value.format_map({'plugin_dir': str(directory)})


def _inside(directory, relative, label):
    _text(relative, label)
    if Path(relative).is_absolute():
        raise ValueError(label + ' must be a relative path inside the plugin')
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError(label + ' must stay inside the plugin')
    return path


def _server(value, directory, permissions):
    if not isinstance(value, dict):
        raise ValueError('MCP server must be an object')
    stdio = 'command' in value
    allowed = {'command', 'args', 'cwd', 'env', 'env_vars'} if stdio else {'url', 'bearer_token_env_var', 'env_http_headers', 'auth'}
    allowed |= {'enabled', 'required', 'enabled_tools', 'disabled_tools', 'startup_timeout_sec', 'tool_timeout_sec'}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError('unsupported MCP fields: ' + ', '.join(sorted(unknown)))
    server = dict(value)
    if stdio:
        if 'process.spawn' not in permissions:
            raise ValueError('local MCP server needs process.spawn permission')
        command = _expand(_text(server['command'], 'MCP command'), directory)
        if '/' in command and not Path(command).is_absolute():
            command = str(_inside(directory, command, 'MCP command'))
        server['command'] = command
        args = server.get('args', [])
        if not isinstance(args, list):
            raise ValueError('MCP args must be an array of strings')
        server['args'] = [_expand(arg, directory) for arg in args]
        if 'cwd' in server:
            cwd = _expand(server['cwd'], directory)
            server['cwd'] = str(Path(cwd).resolve()) if Path(cwd).is_absolute() else str(_inside(directory, cwd, 'MCP cwd'))
        env = server.get('env', {})
        if not isinstance(env, dict):
            raise ValueError('MCP env must be an object')
        for key, item in env.items():
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValueError('MCP env contains an invalid variable name')
            if key.startswith('MV2_') or key in ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_THREAD_ID', 'PATH'):
                raise ValueError('MCP env cannot replace native storage or launch identity')
            if _secret.search(key):
                raise ValueError('MCP credentials must reference environment variables, not inline values')
            if not isinstance(item, str) or '\0' in item:
                raise ValueError('MCP env values must be text without NUL characters')
        if 'env_vars' in server and (not _strings(server['env_vars']) or
                any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v) for v in server['env_vars'])):
            raise ValueError('MCP env_vars must name environment variables')
        if any(key in ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_THREAD_ID')
               for key in server.get('env_vars', [])):
            raise ValueError('MCP env_vars cannot substitute native storage or session markers')
        # env_vars are references, including opt-in launch identity. Values stay
        # out of argv; the bridge carries only declared references over RPC.
        server['env'] = dict(env)
    else:
        url = urlsplit(_text(server.get('url'), 'MCP url'))
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                or url.password is not None or url.fragment or any(_secret.search(k) for k, _ in parse_qsl(url.query))):
            raise ValueError('MCP url must be HTTP(S) without embedded credentials')
        if 'auth' in server and server['auth'] != 'oauth':
            raise ValueError('MCP authentication must stay user-owned (oauth)')
        if 'bearer_token_env_var' in server:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', _text(server['bearer_token_env_var'], 'MCP bearer_token_env_var')):
                raise ValueError('MCP bearer_token_env_var must name an environment variable')
        headers = server.get('env_http_headers', {})
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9!#$%&\'*+^_`|~-]+', k) or
                not isinstance(v, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v) for k, v in headers.items()):
            raise ValueError('MCP env_http_headers must map header names to environment variables')
    for key in ('enabled', 'required'):
        if key in server and type(server[key]) is not bool:
            raise ValueError(f'MCP {key} must be a boolean')
    if server.get('required', False):
        raise ValueError('plugin MCP startup must be optional (required=false)')
    server.setdefault('required', False)
    for key in ('enabled_tools', 'disabled_tools'):
        if key in server and not _strings(server[key]):
            raise ValueError(f'MCP {key} must be an array of tool names')
    for key in ('startup_timeout_sec', 'tool_timeout_sec'):
        if key in server and (type(server[key]) not in (int, float) or not math.isfinite(server[key]) or server[key] <= 0):
            raise ValueError(f'MCP {key} must be a positive finite number')
    return server


@dataclass
class Prepared:
    plan: object
    bootstrap: str
    defaults: dict = field(default_factory=dict)  # literal-dot CLI paths
    session_config: dict = field(default_factory=dict)
    user_config: dict = field(default_factory=dict)
    mcp_environment: dict = field(default_factory=dict)

    def apply(self, command):
        argv = list(command.argv)
        # Replace only the core bootstrap; explicit user instructions were
        # already used to suppress additions in prepare().
        core = 'developer_instructions='
        for index in range(len(argv)):
            if argv[index] == '--':
                break
            if argv[index].startswith(core):
                argv[index] = core + toml_value(self.bootstrap)
                break
        additions = [piece for key, value in sorted(self.defaults.items()) for piece in ('-c', key + '=' + toml_value(value))]
        argv[1:1] = additions
        return replace(command, argv=tuple(argv), environment=self.plan.environment, plugins=self.plan.record())


def prepare(context, command, *, bridge=False, daemon=None, final=False, settings=None, harness_name='codex'):
    """Validate and compose one mode's plan; failures lose contributions, never the launch."""
    if not isinstance(daemon, (tuple, list)) or len(daemon) != 3 or any(type(v) is not int or v < 0 for v in daemon):
        daemon = None
    else:
        daemon = tuple(daemon)
    directory = Path(context.values.get('agent_dir', ''))
    root = directory.parents[2] if len(directory.parents) > 2 else None
    bootstrap = json.loads(context.values['bootstrap_json'])
    if root is None or not (root / 'universe.json').is_file():
        return Prepared(harness.LaunchPlan(environment=dict(command.environment)), bootstrap)
    cli, version_read = None, False
    def selected_version():
        nonlocal cli, version_read
        if not version_read:
            cli = codex_version(command.argv[0], context.values['workspace'])
            version_read = True
        return cli
    # Environment contributions belong to the launched process. They are not
    # implicit MCP declarations and may contain credentials.
    base_env = dict(command.environment)
    if bridge and not final:
        return Prepared(harness.LaunchPlan(environment=base_env), bootstrap)
    info = harness.LaunchInfo(root, directory.name, context.values.get('name', directory.name),
                             context.mode, 'codex', harness_name, daemon if bridge else selected_version, bridge)
    plan = harness.launch_plan(info, base_environment=base_env)
    if plan.plugins:
        selected_version()
    for record in plan.plugins.values():
        record['codex_launch'] = {'cli_version': version_text(cli), 'daemon_version': version_text(daemon),
                                  'mode': 'bridge' if bridge else 'plain'}
    prepared = Prepared(plan, bootstrap)
    explicit = [arg.format_map(context.values) for arg in
                [*(settings or {}).get('args', []), *(settings or {}).get(context.mode + '_args', [])]]
    user = list(overrides(explicit))
    instructions_replaced = any(overlaps(key, 'developer_instructions') for key, _ in user)
    configs, servers, instructions, namespaces = [], [], [], set()
    for number, (name, plugin_dir, entry) in enumerate(plan.codex):
        record = plan.plugins[name]
        detail = {'entry': number, 'cli_version': version_text(cli), 'daemon_version': version_text(daemon),
                  'mode': 'bridge' if bridge else 'plain', 'state': 'prepared',
                  'config_keys': [], 'mcp_servers': [], 'suppressed': [], 'skipped': []}
        record.setdefault('codex', []).append(detail)
        plugin_dir = Path(plugin_dir).resolve()
        try:
            if cli is None:
                raise ValueError('the Codex CLI version could not be read')
            if cli < CONFIG_VERSION:
                raise ValueError('Codex plugin config needs CLI ' + version_text(CONFIG_VERSION) + ' or newer')
            fields = entry.fields
            unknown = set(fields) - {'config', 'instructions', 'mcp_servers'}
            if unknown:
                raise ValueError('unsupported Codex entry fields: ' + ', '.join(sorted(unknown)))
            config = fields.get('config', {})
            if not isinstance(config, dict):
                raise ValueError('Codex config must be an object of dotted keys')
            for key, value in config.items():
                _config(key, value)
            definitions = fields.get('mcp_servers', {})
            if not isinstance(definitions, dict):
                raise ValueError('mcp_servers must be an object')
            loaded_manifest = manifest.load(plugin_dir)
            detail['entry'] = loaded_manifest.harnesses['codex'].index(entry)
            permissions = loaded_manifest.permissions
            checked = []
            for local, definition in definitions.items():
                if not isinstance(local, str) or not _name.fullmatch(local):
                    raise ValueError('MCP server names must contain lowercase letters, digits, hyphens or underscores')
                native_name = 'mv2_' + name.replace('-', '_') + '_' + local.replace('-', '_')
                server = _server(definition, plugin_dir, permissions)
                if bridge:
                    from .identity import VOLATILE
                    if VOLATILE.intersection(server.get('env_vars', [])):
                        raise ValueError('Shared Codex MCP servers cannot reference launch identity or attachment sockets')
                checked.append((native_name, server))
            text = None
            if 'instructions' in fields:
                path = _inside(plugin_dir, fields['instructions'], 'instructions')
                data = path.read_bytes()
                if len(data) > 256 * 1024:
                    raise ValueError('instructions exceeds 256 KiB')
                text = data.decode('utf-8')
                if '\0' in text:
                    raise ValueError('instructions must not contain NUL characters')
                detail['instructions'] = {'path': fields['instructions'], 'sha256': hashlib.sha256(data).hexdigest()}
            record['fingerprint'] = _plugin_fingerprint(plugin_dir)
        except (OSError, ValueError, TypeError) as error:
            detail.update(state='skipped', reason=str(error))
            continue
        server_reason = None
        if bridge and final:
            server_reason = entry.fits(daemon) if daemon is not None else 'the Codex daemon version could not be read'
            if server_reason is None and daemon < CONFIG_VERSION:
                server_reason = 'Codex daemon config needs ' + version_text(CONFIG_VERSION) + ' or newer'
        for key, value in config.items():
            if not key.startswith('tui.'):
                namespaces.add(key)
            tui_reason = entry.fits(cli) if key.startswith('tui.') else None
            if tui_reason:
                detail['skipped'].append({'field': key, 'reason': tui_reason})
            elif server_reason and not key.startswith('tui.'):
                detail['skipped'].append({'field': key, 'reason': server_reason})
            elif any(overlaps(key, path) for path, _ in user) or (key == 'tui.alternate_screen' and '--no-alt-screen' in explicit):
                detail['suppressed'].append(key)
            else:
                configs.append((key, value, detail))
        for native_name, definition in checked:
            namespaces.add('mcp_servers.' + native_name)
            reason = ('plugin MCP activation is supported on spawn only' if context.mode != 'spawn' else
                      server_reason or ('plugin MCP needs Codex ' + version_text(MCP_VERSION) + ' or newer'
                                        if cli < MCP_VERSION or bridge and final and daemon < MCP_VERSION else None))
            if reason:
                detail['skipped'].append({'field': 'mcp_servers.' + native_name, 'reason': reason})
            else:
                servers.append((native_name, definition, detail))
        if text is not None:
            if instructions_replaced:
                detail['suppressed'].append('instructions')
            elif server_reason:
                detail['skipped'].append({'field': 'instructions', 'reason': server_reason})
            else:
                instructions.append((name, number, text, detail))
                detail['instructions']['state'] = 'prepared'
        if bridge and not final:
            detail['awaiting_daemon_version'] = True
    claims = {}
    for key, value, detail in configs:
        claims.setdefault(key, []).append((value, detail))
    for key, owners in claims.items():
        if any(value != owners[0][0] for value, _ in owners):
            for _, detail in owners:
                detail['skipped'].append({'field': key, 'reason': 'conflicting defaults from Codex plugin entries'})
            continue
        prepared.defaults[key] = owners[0][0]
        for _, detail in owners:
            detail['config_keys'].append(key)
    claims = {}
    for name, definition, detail in servers:
        claims.setdefault(name, []).append((definition, detail))
    for name, owners in claims.items():
        if len(owners) != 1:
            for _, detail in owners:
                detail['skipped'].append({'field': 'mcp_servers.' + name, 'reason': 'MCP namespace collision'})
            continue
        definition, detail = owners[0]
        applied = False
        for field_name, value in definition.items():
            fields = value.items() if isinstance(value, dict) else [(None, value)]
            for subkey, item in fields:
                path = 'mcp_servers.' + name + '.' + field_name + ('.' + subkey if subkey is not None else '')
                if any(overlaps(path, key) for key, _ in user):
                    detail['suppressed'].append(path)
                else:
                    prepared.defaults[path] = item
                    applied = True
        if applied:
            detail['mcp_servers'].append(name)
            detail['mcp_state'] = 'requested' if bridge and final else 'prepared'
    for name, _, text, _ in sorted(instructions, key=lambda value: (value[0], value[1])):
        prepared.bootstrap += f'\n\n<plugin_instructions name="{name}">\n{text}\n</plugin_instructions>'
    session_defaults = [(key, value) for key, value in prepared.defaults.items() if not key.startswith('tui.')]
    prepared.session_config = config_tree(session_defaults)
    if bridge:
        launch_env = {**context.environment, **plan.environment}
        target_id = context.values.get('agent_id', directory.name)
        if context.environment.get('MV2_AGENT_ID') != target_id:
            launch_env = {key: value for key, value in launch_env.items() if not key.startswith('MV2_')}
        for name, server in prepared.session_config.get('mcp_servers', {}).items():
            prepared.mcp_environment[name] = {key: launch_env[key] for key in server.get('env_vars', [])
                                              if key in launch_env}
    forwarded = [(key, value) for key, value in user if any(overlaps(key, ns) for ns in namespaces)
                 and (context.mode == 'spawn' or not key.startswith('mcp_servers'))]
    prepared.user_config = config_tree(forwarded)
    for record in plan.plugins.values():
        details = record.get('codex', [])
        for detail in details:
            if detail['state'] != 'skipped':
                accepted = bool(detail['config_keys'] or detail['mcp_servers'] or
                                detail.get('instructions', {}).get('state') == 'prepared')
                if not accepted and detail['skipped']:
                    detail.update(state='skipped', reason='; '.join(item['reason'] for item in detail['skipped']))
                elif not accepted and detail['suppressed']:
                    detail['state'] = 'suppressed'
                elif bridge and final and accepted:
                    detail['state'] = 'requested'
        if details and all(detail['state'] == 'skipped' for detail in details):
            record.update(state='skipped', reason='; '.join(detail['reason'] for detail in details))
    return prepared
