"""Antigravity CLI native terminal lifecycle and local bridge adapter ("auto" by default).

Bootstraps each launch via `--add-dir <launch_dir>` containing `AGENTS.md` (and `.agents/hooks.json`
in bridge mode) plus session-only `--settings <launch_dir>/settings.json` for `statusLine` and
minimal `permissions.allow` rules covering mv2's state/run directories.

Live message delivery activates once the agent runs its first `mv2` shell callback (such as
`mv2 report`), which captures `ANTIGRAVITY_LS_ADDRESS` and `ANTIGRAVITY_CSRF_TOKEN` into a
launch-private `0600` file (`launches/<launch_id>/agy-ls.json`, removed when the runner exits normally):
- `steer` (and idle `follow-up`) submit via `agy agentapi send-message`
- busy `follow-up` queues into `launches/<launch_id>/agy-followups.json` and delivers via the
  `Stop` hook (`{"decision": "continue", "reason": ...}`), with an idle-transition recheck.
"""
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from uuid import uuid4

from ...plugins import harness as plugin_harness
from ...store import now, read, retry_missing
from ..base import ConfiguredAdapter, PARENT_AGY
from ..types import (AttachmentSpec, Capability, CommandSpec, LaunchRefused, NativeEndpoint, NativeResult,
                     ShellCallbackResult, normalize_session_id)
from . import bridge as agy_bridge
from . import models as agy_models
from . import pane as agy_pane
from . import storage
from . import transcripts as agy_transcripts


MIN_BRIDGE_VERSION = (1, 5, 0)
BRIDGE = agy_bridge.BRIDGE
MODES = {'steer': 'steer', 'follow-up': 'follow-up'}
LAUNCH_ONLY = 'This agent is launch-only'
NOT_RUNNING = "This agent isn't running: resume it first"
NOT_REGISTERED = (
    'Antigravity bridge activates on the first mv2 command inside this session '
    '(or stop the idle agent with mv2 kill so retry wakes it)'
)

_versions = {}
_RULE = re.compile(r'^\s*[─━]{8,}')
_TRUST_MARKER = 'Do you trust the contents of this project?'
_ACCESSING_WORKSPACE = re.compile(r'Accessing workspace:\s*(.+)')
_TRUST_CHOICE = re.compile(r'^\s*(?:[>❯]\s*\S|1\.\s*Yes\b|Yes,\s*I\s+trust\b)', re.IGNORECASE)
_READY_HINT = '? for shortcuts'
_PROMPT_LINE = re.compile(r'^\s*[>❯]\s*')


def agy_version(executable: str, *, path: str | None = None):
    """Parse `(major, minor, patch)` from `agy --version`, cached per resolved binary identity."""
    found_path = shutil.which(executable, path=path)
    if found_path is None:
        return None
    try:
        real = os.path.realpath(found_path)
        info = os.stat(real)
    except OSError:
        return None
    key = (real, info.st_mtime_ns, info.st_size)
    if key not in _versions:
        try:
            output = subprocess.run(
                [found_path, '--version'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.search(r'(\d+)\.(\d+)\.(\d+)', output)
        if not found:
            return None
        _versions[key] = tuple(int(part) for part in found.groups())
    return _versions[key]


def bridge_problem(executable: str, *, path: str | None = None) -> str | None:
    """Why `executable` cannot run the bridge, or None."""
    version = agy_version(executable, path=path)
    if version is not None and version < MIN_BRIDGE_VERSION:
        return (f'Antigravity CLI {".".join(map(str, version))} is older than '
                f'{".".join(map(str, MIN_BRIDGE_VERSION))}, which the mv2 bridge needs')
    return None


@dataclass(frozen=True)
class Bridge:
    launched: bool
    registered: bool
    ready: bool
    reason: str | None
    launch_only: bool = False
    final: bool = False


def bridge_of(enabled, session) -> Bridge:
    launch = session.native_mode or {}
    plain = launch.get('mode') == 'plain'
    launched = bool(enabled) and not plain
    registered = bool(
        launched
        and session.native_id
        and session.endpoint
        and session.endpoint.transport == 'unix-json'
        and session.bridge == BRIDGE
    )
    ready = registered and session.state == 'running'
    launch_only = not enabled or (plain and launch.get('policy') != 'plain')
    if ready:
        reason = None
    elif launch_only:
        cause = ('its harness sets inbox_bridge to false' if not enabled
                 else str(launch.get('fallback_reason') or 'it was launched without the bridge'))
        reason = f'{LAUNCH_ONLY}: {cause}'
    elif session.state in ('stopped', 'dormant', 'failed'):
        reason = NOT_RUNNING
    else:
        reason = NOT_REGISTERED
    return Bridge(launched, registered, ready, reason, launch_only, final=launch_only and not enabled)


def unavailable(bridge: Bridge) -> NativeResult:
    if bridge.launch_only:
        return NativeResult('unavailable', bridge.reason, {'launch_only': True, 'final': bridge.final})
    if bridge.reason == NOT_REGISTERED:
        return NativeResult('unavailable', bridge.reason, {'definite_failure': True})
    return NativeResult('unavailable', bridge.reason)


def _bridge_command(*args: str) -> str:
    return shlex.join([sys.executable, str(Path(__file__).with_name('bridge.py')), *args])


def _hooks_config(launch_id: str | None = None) -> str:
    extra: tuple[str, ...] = ()
    if isinstance(launch_id, str) and launch_id:
        try:
            extra = ('--launch-id', normalize_session_id(launch_id))
        except ValueError:
            extra = ()
    config = {
        'mv2-bridge': {
            'SessionStart': [
                {'type': 'command', 'command': _bridge_command('hook', 'SessionStart', *extra), 'timeout': 10},
            ],
            'PreInvocation': [
                {'type': 'command', 'command': _bridge_command('hook', 'PreInvocation', *extra), 'timeout': 10},
            ],
            'Stop': [
                {'type': 'command', 'command': _bridge_command('hook', 'Stop', *extra), 'timeout': 15},
            ],
        },
    }
    return json.dumps(config, indent=2, ensure_ascii=False) + '\n'


def _strip_jsonc(text: str) -> str:
    """Strip JSONC `//` line comments, `/* ... */` block comments, and trailing commas outside strings."""
    out = []
    idx = 0
    length = len(text)
    in_string = False
    escaped = False
    while idx < length:
        ch = text[idx]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            idx += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            idx += 1
            continue
        if ch == '/' and idx + 1 < length and text[idx + 1] == '/':
            idx += 2
            while idx < length and text[idx] not in ('\n', '\r'):
                idx += 1
            continue
        if ch == '/' and idx + 1 < length and text[idx + 1] == '*':
            end = text.find('*/', idx + 2)
            if end < 0:
                raise ValueError('Unterminated block comment in JSONC')
            out.extend('\n' for c in text[idx:end + 2] if c == '\n')
            idx = end + 2
            continue
        out.append(ch)
        idx += 1
    no_comments = ''.join(out)
    cleaned = []
    idx = 0
    length = len(no_comments)
    in_string = False
    escaped = False
    while idx < length:
        ch = no_comments[idx]
        if in_string:
            cleaned.append(ch)
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            idx += 1
            continue
        if ch == '"':
            in_string = True
            cleaned.append(ch)
            idx += 1
            continue
        if ch == ',':
            look = idx + 1
            while look < length and no_comments[look] in ' \t\r\n':
                look += 1
            if look < length and no_comments[look] in '}]':
                idx += 1
                continue
        cleaned.append(ch)
        idx += 1
    return ''.join(cleaned)


def _extract_user_settings(
    argv: list[str],
    *,
    forbidden_roots: Sequence[Path] = (),
    sources: dict[str, str] | None = None,
) -> tuple[list[str], dict]:
    """Remove any user `--settings` flag from `argv` and load its JSON/JSONC object for merging."""
    kept = []
    merged = {}
    index = 0
    while index < len(argv):
        arg = argv[index]
        raw_value = None
        advance = 1
        if arg == '--settings':
            if index + 1 >= len(argv):
                raise ValueError('Antigravity --settings flag requires a JSON object or file path')
            raw_value = argv[index + 1]
            advance = 2
        elif arg.startswith('--settings='):
            raw_value = arg.split('=', 1)[1]
        if raw_value is not None:
            if raw_value.lstrip().startswith('{'):
                source_label = 'harness.json --settings'
                try:
                    loaded = json.loads(_strip_jsonc(raw_value))
                except ValueError as error:
                    raise ValueError(f'Invalid inline Antigravity --settings JSON: {error}') from None
            else:
                source_label = raw_value
                candidate = Path(raw_value).expanduser()
                try:
                    resolved_candidate = candidate.resolve()
                except (OSError, RuntimeError) as error:
                    raise ValueError(f'Cannot resolve Antigravity --settings file {raw_value!r}: {error}') from None
                if any(resolved_candidate == root or resolved_candidate.is_relative_to(root) for root in forbidden_roots):
                    raise ValueError(
                        f'Antigravity --settings file cannot live inside sandbox-writable state: {raw_value!r}'
                    )
                try:
                    fd = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK)
                except FileNotFoundError:
                    raise ValueError(f'Antigravity --settings file does not exist: {raw_value!r}') from None
                except OSError as error:
                    raise ValueError(f'Cannot read Antigravity --settings file {raw_value!r}: {error}') from None
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ValueError(f'Antigravity --settings path is not a regular file: {raw_value!r}')
                    os.set_blocking(fd, True)
                    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
                        fd = -1
                        loaded = json.loads(_strip_jsonc(handle.read()))
                except ValueError as error:
                    if str(error).startswith('Antigravity --settings'):
                        raise
                    raise ValueError(f'Cannot read Antigravity --settings file {raw_value!r}: {error}') from None
                except OSError as error:
                    raise ValueError(f'Cannot read Antigravity --settings file {raw_value!r}: {error}') from None
                finally:
                    if fd >= 0:
                        os.close(fd)
            if not isinstance(loaded, dict):
                raise ValueError(f'Antigravity --settings {raw_value!r} must be a JSON object')
            if sources is not None:
                for key in loaded:
                    sources[key] = source_label
            merged.update(loaded)
            index += advance
            continue
        kept.append(arg)
        index += 1
    return kept, merged


_BROAD_READ_ROOTS = frozenset((
    Path('/'), Path('/usr'), Path('/usr/local'), Path('/opt'), Path('/opt/homebrew'), Path('/opt/local'),
    Path('/var'), Path('/private'), Path('/private/var'), Path('/tmp'), Path('/private/tmp'),
    Path('/System'), Path('/Library'), Path('/Users'), Path('/home'), Path('/etc'),
))

# Wide exclusion list for rules mv2 derives itself (plugin registries, MV2_PLUGIN_PATH, venv/Python,
# resume records, plugin --add-dir, etc.); mv2 never needs any of these subtrees.
_DERIVED_SENSITIVE_HOME_REL = (
    ('.ssh',),
    ('.gnupg',),
    ('.aws',),
    ('.kube',),
    ('.docker',),
    ('.password-store',),
    ('.gemini',),
    ('.claude',),
    ('.codex',),
    ('.pi',),
    ('.multiverse', 'plugin-secrets'),
    ('Library', 'Keychains'),
    ('Library', 'Cookies'),
    ('Library', 'Application Support'),
    ('.local', 'share', 'keyrings'),
    ('.config', 'gh'),
    ('.config', 'gcloud'),
)

# Narrow hard exclusions for person-written rules (harness --settings and personal agy settings):
# ~/.ssh, ~/.gnupg, ~/Library/Keychains, Antigravity 1.5.16 sign-in and file-backed secret stores
# (~/.gemini/antigravity-browser-profile, ~/.gemini/jetski-standalone-oauth-token,
# ~/.gemini/antigravity-cli/{antigravity,jetski,jetski-cli}-oauth-token,
# ~/.gemini/antigravity/antigravity-oauth-token, ~/.gemini/antigravity-cli/mcp_oauth_tokens.json,
# ~/.gemini/antigravity-cli/cache/acp-auth-state.json, and os.UserConfigDir()/antigravity/plugin-secrets.json
# on macOS/Linux), and ~/.multiverse/plugin-secrets.
_PERSON_SENSITIVE_HOME_REL = (
    ('.ssh',),
    ('.gnupg',),
    ('Library', 'Keychains'),
    ('.gemini', 'antigravity-browser-profile'),
    ('.gemini', 'jetski-standalone-oauth-token'),
    ('.gemini', 'antigravity-cli', 'antigravity-oauth-token'),
    ('.gemini', 'antigravity-cli', 'jetski-oauth-token'),
    ('.gemini', 'antigravity-cli', 'jetski-cli-oauth-token'),
    ('.gemini', 'antigravity', 'antigravity-oauth-token'),
    ('.gemini', 'antigravity-cli', 'mcp_oauth_tokens.json'),
    ('.gemini', 'antigravity-cli', 'cache', 'acp-auth-state.json'),
    ('Library', 'Application Support', 'antigravity', 'plugin-secrets.json'),
    ('.config', 'antigravity', 'plugin-secrets.json'),
    ('.multiverse', 'plugin-secrets'),
)

_PERMISSION_RULE_RE = re.compile(r'^\s*([A-Za-z0-9_:-]+)(?:\((.*)\))?\s*\Z')
_GLOB_CHARS = ('*', '?', '[')
_FILE_RULE_TOOLS = frozenset((
    'read_file', 'write_file', 'edit_file', 'view_file', 'list_dir',
    'find_by_name', 'grep_search', 'replace_file_content', 'multi_replace_file_content',
))


def _is_file_rule_tool(tool: str) -> bool:
    lowered = tool.lower()
    return lowered in _FILE_RULE_TOOLS or 'file' in lowered or 'dir' in lowered


def _expand_tilde_path(raw: str, home_dir: Path | None) -> str | None:
    """Expand leading `~` or `~/` against `home_dir`; return None for unsupported `~name` or unknown home."""
    if raw == '~' or raw.startswith('~/'):
        target_home = home_dir
        if target_home is None:
            try:
                target_home = Path.home().resolve()
            except (OSError, RuntimeError):
                return None
        rel = raw[2:].lstrip('/') if raw.startswith('~/') else ''
        return str(target_home / rel) if rel else str(target_home)
    if raw.startswith('~'):
        return None
    return raw


def _is_valid_sandbox_file_path(path_str: str) -> bool:
    """True when `path_str` is an absolute non-glob path accepted by Antigravity's OS sandbox."""
    if not path_str or '\x00' in path_str or '\n' in path_str or '\r' in path_str:
        return False
    if not os.path.isabs(path_str):
        return False
    if any(ch in path_str for ch in _GLOB_CHARS):
        return False
    return True


def _is_valid_ask_file_path(path_str: str) -> bool:
    """True when `path_str` is a non-glob path enforced by Antigravity 1.5.16's `permissions.ask` matcher."""
    if not path_str or '\x00' in path_str or '\n' in path_str or '\r' in path_str:
        return False
    if path_str.startswith(('~', '$')):
        return False
    if any(ch in path_str for ch in _GLOB_CHARS):
        return False
    return True


def _sandbox_enabled_for_argv(argv: Sequence[str]) -> bool:
    """True unless CLI argv disables Antigravity 1.5.16's OS sandbox (`--dangerously-skip-permissions` or `--sandbox=false`).

    Live probes on agy 1.5.16 show exebox runs by default (even with no `--sandbox` flag and even when
    settings.json sets `sandbox: false` or `sandbox: {"enabled": false}`); only CLI flags turn exebox off.
    """
    enabled = True
    for arg in argv[1:]:
        if arg == '--dangerously-skip-permissions':
            return False
        if arg.startswith('--dangerously-skip-permissions='):
            if arg.split('=', 1)[1].strip().lower() in ('1', 't', 'true'):
                return False
        elif arg == '--sandbox':
            enabled = True
        elif arg.startswith('--sandbox='):
            val = arg.split('=', 1)[1].strip().lower()
            if val in ('0', 'f', 'false'):
                enabled = False
            elif val in ('1', 't', 'true'):
                enabled = True
    return enabled


def _prepare_permission_rule(
    rule: str,
    home_dir: Path | None,
    *,
    expand_tilde: bool = True,
) -> tuple[str, str | None, bool] | None:
    """Parse a permission rule and (when `expand_tilde`) expand leading `~` / `~/` in file-rule paths.

    Returns `(prepared_rule, file_path, is_file_rule)`, or `None` when the rule cannot be parsed or expanded.
    """
    if not isinstance(rule, str) or '\x00' in rule or '\n' in rule or '\r' in rule:
        return None
    match = _PERMISSION_RULE_RE.match(rule)
    if not match:
        return None
    tool_raw = match.group(1)
    arg = match.group(2)
    if arg is None:
        return None
    raw = arg.strip()
    if not raw:
        return None
    if not _is_file_rule_tool(tool_raw):
        return (rule.strip(), None, False)
    if not expand_tilde:
        if raw.startswith('~'):
            return None
        return (f'{tool_raw}({raw})', raw, True)
    expanded = _expand_tilde_path(raw, home_dir)
    if expanded is None:
        return None
    return (f'{tool_raw}({expanded})', expanded, True)


def _home_roots(home_dir: Path | None) -> tuple[Path, ...]:
    roots: list[Path] = []
    if home_dir is not None:
        roots.append(home_dir)
    try:
        host_home = Path.home().resolve()
    except (OSError, RuntimeError):
        host_home = None
    if host_home is not None and host_home not in roots:
        roots.append(host_home)
    return tuple(roots)


def _path_key(path: Path) -> Path:
    return Path(*(part.lower() for part in path.parts)) if sys.platform == 'darwin' else path


def _extra_sensitive_paths(environment: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Return absolute sensitive paths derived from `XDG_CONFIG_HOME` (Linux `os.UserConfigDir()`)."""
    paths: list[Path] = []
    for source in (environment, os.environ):
        raw = source.get('XDG_CONFIG_HOME') if isinstance(source, Mapping) else None
        if isinstance(raw, str) and raw and os.path.isabs(raw):
            try:
                candidate = Path(raw).resolve() / 'antigravity' / 'plugin-secrets.json'
            except (OSError, RuntimeError):
                continue
            if candidate not in paths:
                paths.append(candidate)
    return tuple(paths)


def _sensitive_targets(
    home_dir: Path | None,
    sensitive_rels: Sequence[tuple[str, ...]],
    extra_sensitive: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    targets = [base.joinpath(*rel) for base in _home_roots(home_dir) for rel in sensitive_rels]
    extras = _extra_sensitive_paths() if extra_sensitive is None else extra_sensitive
    for item in extras:
        if item not in targets:
            targets.append(item)
    return tuple(targets)


def _is_sensitive_path(
    path: Path,
    home_dir: Path | None,
    *,
    sensitive_rels: Sequence[tuple[str, ...]] = _DERIVED_SENSITIVE_HOME_REL,
    extra_sensitive: Sequence[Path] | None = None,
) -> bool:
    """True when `path` equals, sits inside, or encloses a sensitive secret/credential path."""
    key = _path_key(path)
    for base in _home_roots(home_dir):
        base_key = _path_key(base)
        if key == base_key or base_key.is_relative_to(key):
            return True
    for sensitive in _sensitive_targets(home_dir, sensitive_rels, extra_sensitive):
        sensitive_key = _path_key(sensitive)
        if key == sensitive_key or key.is_relative_to(sensitive_key) or sensitive_key.is_relative_to(key):
            return True
    return False


def _is_safe_read_dir(path: Path, home_dir: Path | None) -> bool:
    """True when `path` is a narrow directory safe to grant read-only (never a broad OS, $HOME or secret root)."""
    if not path.is_absolute() or len(path.parts) <= 2 or path in _BROAD_READ_ROOTS:
        return False
    if _is_sensitive_path(path, home_dir):
        return False
    for base in _home_roots(home_dir):
        if path in (
            base / '.local',
            base / '.local' / 'share',
            base / '.multiverse',
            base / '.config',
            base / 'Library',
        ):
            return False
    return True


def _glob_matches_rel(rel_parts: tuple[str, ...], tail_pattern: str) -> bool:
    """True when `tail_pattern` matches `rel_parts`, any ancestor prefix, or a direct child under it."""
    raw_parts = [part for part in tail_pattern.split('/') if part and part != '.']
    if not raw_parts or '..' in raw_parts:
        return True
    norm_tail = '/'.join(raw_parts)
    patterns = [norm_tail]
    cur = norm_tail
    while cur.startswith('**/'):
        cur = cur[3:]
        if cur:
            patterns.append(cur)
    if '**/' in norm_tail:
        collapsed = norm_tail.replace('**/', '')
        if collapsed and collapsed not in patterns:
            patterns.append(collapsed)
    targets = ['/'.join(rel_parts[:depth]) for depth in range(1, len(rel_parts) + 1)]
    # Also test a synthetic child so patterns like `**/.ssh/*` catch protected directories.
    targets.append('/'.join((*rel_parts, '_')))
    if sys.platform == 'darwin':
        patterns = [p.lower() for p in patterns]
        targets = [t.lower() for t in targets]
    return any(fnmatch.fnmatchcase(target, pat) for target in targets for pat in patterns)


def _rule_allows_sensitive(
    rule: str,
    home_dir: Path | None,
    *,
    workspace: Path | None = None,
    sensitive_rels: Sequence[tuple[str, ...]] = _DERIVED_SENSITIVE_HOME_REL,
    broad_roots: bool = True,
    extra_sensitive: Sequence[Path] | None = None,
) -> bool:
    """True when a file permission allow rule covers a protected path in `sensitive_rels`, $HOME or a broad wildcard."""
    if not isinstance(rule, str):
        return False
    match = _PERMISSION_RULE_RE.match(rule)
    if not match:
        return False
    if not _is_file_rule_tool(match.group(1)):
        return False
    arg = match.group(2)
    if arg is None:
        return True
    raw = arg.strip()
    if not raw or raw in ('*', '**', '/*', '/**', '~', '~/*', '~/**') or raw.startswith('*'):
        return True
    glob_pos = min((pos for pos in (raw.find('*'), raw.find('?'), raw.find('[')) if pos >= 0), default=-1)
    if glob_pos < 0:
        candidates: list[Path] = []
        if raw == '~' or raw.startswith('~/'):
            rel = raw[2:].lstrip('/') if raw.startswith('~/') else ''
            for base in _home_roots(home_dir):
                candidates.append(base / rel if rel else base)
        elif os.path.isabs(raw):
            candidates.append(Path(raw))
        elif workspace is not None:
            candidates.append(workspace / raw)
        else:
            rel_parts = Path(raw).parts
            cmp_parts = tuple(p.lower() for p in rel_parts) if sys.platform == 'darwin' else rel_parts
            if '..' in rel_parts or any(
                cmp_parts[:len(rel)] == (tuple(p.lower() for p in rel) if sys.platform == 'darwin' else rel)
                for rel in sensitive_rels
            ):
                return True
            return False
        for cand in candidates:
            forms: list[Path] = []
            try:
                forms.append(cand.resolve())
            except (OSError, RuntimeError):
                pass
            lexical = Path(os.path.normpath(str(cand)))
            if lexical not in forms:
                forms.append(lexical)
            for check_path in forms:
                if _is_sensitive_path(
                    check_path, home_dir, sensitive_rels=sensitive_rels, extra_sensitive=extra_sensitive,
                ):
                    return True
                if broad_roots and check_path in _BROAD_READ_ROOTS:
                    return True
        return False
    before_glob = raw[:glob_pos]
    slash = before_glob.rfind('/')
    dir_prefix = ('/' if slash == 0 else raw[:slash]) if slash >= 0 else ''
    tail_pattern = raw[slash + 1:] if slash >= 0 else raw
    if '..' in tail_pattern.split('/'):
        return True
    dir_candidates: list[Path] = []
    if dir_prefix == '~' or dir_prefix.startswith('~/'):
        rel = dir_prefix[2:].lstrip('/') if dir_prefix.startswith('~/') else ''
        for base in _home_roots(home_dir):
            dir_candidates.append(base / rel if rel else base)
    elif dir_prefix and os.path.isabs(dir_prefix):
        dir_candidates.append(Path(dir_prefix))
    elif workspace is not None:
        dir_candidates.append(workspace / dir_prefix if dir_prefix else workspace)
    else:
        if '..' in Path(raw).parts:
            return True
        return any(_glob_matches_rel(rel, raw) for rel in sensitive_rels)
    for dcand in dir_candidates:
        try:
            dres = dcand.resolve()
        except (OSError, RuntimeError):
            dres = Path(os.path.normpath(str(dcand)))
        if broad_roots and dres in _BROAD_READ_ROOTS:
            return True
        dres_key = _path_key(dres)
        for sensitive in _sensitive_targets(home_dir, sensitive_rels, extra_sensitive):
            sens_key = _path_key(sensitive)
            if dres_key == sens_key or dres_key.is_relative_to(sens_key):
                return True
            if sens_key.is_relative_to(dres_key):
                if _glob_matches_rel(sens_key.relative_to(dres_key).parts, tail_pattern):
                    return True
    return False


def _read_trusted_plugin_pins(home_dir: Path | None) -> dict[str, dict]:
    """Read person-owned `<home>/.multiverse/trusted-plugins.json` read-only; returns valid pinned directories."""
    if home_dir is None:
        return {}
    from ...plugins import trust as plugin_trust
    pins_path = home_dir / '.multiverse' / plugin_trust.FILE
    try:
        data = plugin_trust.read_regular(pins_path)
    except (OSError, ValueError):
        return {}
    raw_plugins = data.get('plugins') if isinstance(data, dict) else None
    if not isinstance(raw_plugins, dict):
        return {}
    valid: dict[str, dict] = {}
    for folder, record in raw_plugins.items():
        if (isinstance(folder, str) and os.path.isabs(folder)
                and isinstance(record, dict) and plugin_trust.valid_fingerprint(record.get('fingerprint'))):
            try:
                valid[str(Path(folder).resolve())] = record
            except (OSError, RuntimeError):
                continue
    return valid


def _read_plugin_manifest(directory: Path) -> dict | None:
    """Read regular non-symlink `plugin.json` directly inside `directory`, or None."""
    from ...plugins import trust as plugin_trust
    try:
        data = plugin_trust.read_regular(directory / 'plugin.json')
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _add_plugin_read_dirs(rules: list[str], raw: str | Path, home_dir: Path | None, *,
                          allow_parent_of_plugins: bool = False) -> None:
    """Grant read-only access only to plugin directories holding a regular `plugin.json`."""
    try:
        expanded = _expand_tilde_path(raw, home_dir) if isinstance(raw, str) else str(raw)
        if expanded is None:
            return
        candidate = Path(expanded)
        if not candidate.is_absolute():
            return
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return
    if _read_plugin_manifest(resolved) is not None:
        _add_read_dir(rules, expanded, home_dir)
        return
    if not allow_parent_of_plugins:
        return
    try:
        with os.scandir(resolved) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    child_resolved = Path(entry.path).resolve()
                except (OSError, RuntimeError, ValueError):
                    continue
                if _read_plugin_manifest(child_resolved) is not None:
                    _add_read_dir(rules, entry.path, home_dir)
    except OSError:
        return


def _add_read_dir(rules: list[str], raw: str | Path, home_dir: Path | None) -> None:
    """Append read-only `read_file(...)` for `raw` (resolved and symlink spelling when distinct) if narrow."""
    try:
        expanded = _expand_tilde_path(raw, home_dir) if isinstance(raw, str) else str(raw)
        if expanded is None:
            return
        candidate = Path(expanded)
        if not candidate.is_absolute():
            return
        norm = Path(os.path.normpath(str(candidate)))
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return
    if not _is_safe_read_dir(resolved, home_dir):
        return
    rules.append(f'read_file({resolved})')
    if norm != resolved and _is_safe_read_dir(norm, home_dir):
        rules.append(f'read_file({norm})')


def _pyvenv_home(cfg_path: Path) -> Path | None:
    """Read the `home = ...` directory from a regular non-symlink `pyvenv.cfg` safely without blocking."""
    try:
        fd = os.open(cfg_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        os.set_blocking(fd, True)
        with os.fdopen(fd, 'r', encoding='utf-8', errors='ignore') as handle:
            fd = -1
            text = handle.read(65536)
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() == 'home':
            raw = value.strip()
            if raw and os.path.isabs(raw):
                return Path(raw)
    return None


def _python_read_rules(rules: list[str], mv2_python: str, home_dir: Path | None) -> None:
    """Allow reading mv2's venv, its resolved interpreter and base Python install (e.g. uv-managed Python).

    A `pyvenv.cfg` `home` entry is accepted only when both `home` and its base directory resolve to the exact
    same directories as `MV2_PYTHON`, so a tampered `pyvenv.cfg` inside a writable workspace cannot widen reads.
    """
    py_path = Path(mv2_python)
    venv_prefix = py_path.parent.parent
    try:
        real_py = py_path.resolve()
    except (OSError, RuntimeError):
        real_py = None
    real_base = (
        (real_py.parent.parent if real_py.parent.name in ('bin', 'Scripts') else real_py.parent)
        if real_py is not None else None
    )
    candidates: list[Path] = []
    cfg_path = venv_prefix / 'pyvenv.cfg'
    if not venv_prefix.is_symlink() and cfg_path.is_file() and not cfg_path.is_symlink():
        _add_read_dir(rules, venv_prefix, home_dir)
        cfg_home = _pyvenv_home(cfg_path)
        if cfg_home is not None and real_py is not None and real_base is not None:
            cfg_base = cfg_home.parent if cfg_home.name in ('bin', 'Scripts') else cfg_home
            try:
                cfg_home_resolved = cfg_home.resolve()
                cfg_base_resolved = cfg_base.resolve()
                real_base_resolved = real_base.resolve()
            except (OSError, RuntimeError):
                cfg_home_resolved = cfg_base_resolved = real_base_resolved = None
            if (cfg_home_resolved is not None and cfg_home_resolved == real_py.parent
                    and cfg_base_resolved == real_base_resolved):
                candidates.append(cfg_base)
    if real_base is not None:
        candidates.append(real_base)
    added_base = False
    for base in candidates:
        before = len(rules)
        _add_read_dir(rules, base, home_dir)
        if len(rules) > before:
            added_base = True
    if (not added_base and real_py is not None and real_py.is_absolute()
            and real_py not in _BROAD_READ_ROOTS and not _is_sensitive_path(real_py, home_dir)):
        rules.append(f'read_file({real_py})')


def _sandbox_allow_rules(environment: Mapping[str, str] | None) -> tuple[str, ...]:
    """Minimal permission rules mv2 needs inside Antigravity's OS sandbox.

    Write access is granted strictly to mv2's state/run/tmux-servers directories; read-only access covers the
    universe metadata, mv2's own CLI/source/Python installation, and plugin folders verified outside `state/`.
    """
    if not environment:
        return ()
    from ...plugins import config as plugin_config
    from ...plugins import registry as plugin_registry
    rules: list[str] = []
    home_raw = environment.get('HOME')
    home_dir = Path(home_raw).resolve() if isinstance(home_raw, str) and home_raw and os.path.isabs(home_raw) else None
    mv2_universe = environment.get('MV2_UNIVERSE')
    universe_chain: list[Path] = []
    if isinstance(mv2_universe, str) and mv2_universe and os.path.isabs(mv2_universe):
        root = Path(mv2_universe).resolve()
        if not _is_sensitive_path(root, home_dir) and root not in _BROAD_READ_ROOTS:
            rules.append(f'read_file({root})')
            state_root = root / 'state'
            state_safe = False
            if not state_root.is_symlink():
                try:
                    state_resolved = state_root.resolve()
                except (OSError, RuntimeError):
                    state_resolved = None
                if (state_resolved is not None and state_resolved.is_relative_to(root)
                        and not _is_sensitive_path(state_resolved, home_dir)):
                    rules.append(f'write_file({state_resolved})')
                    state_safe = True
            agent_dir_raw = environment.get('MV2_AGENT_DIR')
            launch_id_raw = environment.get('MV2_LAUNCH_ID')
            if (state_safe and isinstance(agent_dir_raw, str) and agent_dir_raw and os.path.isabs(agent_dir_raw)
                    and isinstance(launch_id_raw, str) and launch_id_raw
                    and '/' not in launch_id_raw and '\\' not in launch_id_raw and launch_id_raw not in ('.', '..')):
                launch_path = (Path(agent_dir_raw) / 'launches' / launch_id_raw).resolve()
                if launch_path.is_relative_to(state_root.resolve()) and not _is_sensitive_path(launch_path, home_dir):
                    # Explicit launch-dir allow rule keeps resumed sessions' --add-dir launch folder writable for
                    # agy-ls.json and .mv2-listen-proof under Antigravity's macOS Seatbelt profile.
                    rules.append(f'write_file({launch_path})')
            universe_chain.append(root)
            # Subverse agents also lock <top_universe>/state/.sessions.lock on session-bind.
            top = root
            while top.parent.name == 'subverses' and (top.parent.parent / 'universe.json').is_file():
                top = top.parent.parent
                if not _is_sensitive_path(top, home_dir) and top not in _BROAD_READ_ROOTS:
                    universe_chain.append(top)
            if top != root and not _is_sensitive_path(top, home_dir) and top not in _BROAD_READ_ROOTS:
                rules.append(f'read_file({top})')
                top_state = top / 'state'
                if not top_state.is_symlink():
                    try:
                        top_state_resolved = top_state.resolve()
                    except (OSError, RuntimeError):
                        top_state_resolved = None
                    if (top_state_resolved is not None and top_state_resolved.is_relative_to(top)
                            and not _is_sensitive_path(top_state_resolved, home_dir)):
                        rules.append(f'write_file({top_state_resolved})')
    run_dir = environment.get('MV2_RUN_DIR')
    if isinstance(run_dir, str) and run_dir and os.path.isabs(run_dir):
        try:
            run_resolved = Path(run_dir).resolve()
        except (OSError, RuntimeError):
            run_resolved = None
        if run_resolved is not None and _is_safe_read_dir(run_resolved, home_dir):
            rules.append(f'write_file({run_resolved})')
    if home_dir is not None:
        mv2_home = home_dir / '.multiverse'
        rules.append(f'write_file({mv2_home / "tmux-servers"})')
        # Read-only access to personal plugins, trust pins and cached copies (never ~/.multiverse root or secrets).
        rules.append(f'read_file({mv2_home / "plugins"})')
        rules.append(f'read_file({mv2_home / "trusted-plugins.json"})')
        rules.append(f'read_file({mv2_home / "plugin-cache"})')
        rules.append(f'read_file({mv2_home / "plugin-packages"})')
    mv2_source = environment.get('MV2_SOURCE')
    if isinstance(mv2_source, str) and mv2_source and os.path.isabs(mv2_source):
        src_resolved = Path(mv2_source).resolve()
        if not _is_sensitive_path(src_resolved, home_dir) and src_resolved not in _BROAD_READ_ROOTS:
            rules.append(f'read_file({src_resolved})')
        checkout_plugins = src_resolved.parent / 'plugins'
        if ((src_resolved / 'multiverse').is_dir() and not checkout_plugins.is_symlink()
                and checkout_plugins.is_dir()):
            _add_read_dir(rules, checkout_plugins, home_dir)
    mv2_cli = environment.get('MV2_CLI')
    if isinstance(mv2_cli, str) and mv2_cli and os.path.isabs(mv2_cli):
        cli_resolved = Path(mv2_cli).resolve()
        if not _is_sensitive_path(cli_resolved, home_dir) and cli_resolved not in _BROAD_READ_ROOTS:
            rules.append(f'read_file({cli_resolved})')
    mv2_python = environment.get('MV2_PYTHON')
    if isinstance(mv2_python, str) and mv2_python and os.path.isabs(mv2_python):
        _python_read_rules(rules, mv2_python, home_dir)
    plugin_path = environment.get('MV2_PLUGIN_PATH')
    if isinstance(plugin_path, str) and plugin_path:
        for raw_entry in plugin_path.split(os.pathsep):
            entry = raw_entry.strip()
            if not entry:
                continue
            expanded_entry = _expand_tilde_path(entry, home_dir)
            if expanded_entry is None:
                continue
            _add_plugin_read_dirs(rules, expanded_entry, home_dir, allow_parent_of_plugins=True)
    trusted_pins = _read_trusted_plugin_pins(home_dir)
    if trusted_pins:
        for scope in universe_chain:
            cfg = plugin_config.load(scope)
            if cfg.problem:
                continue
            try:
                recorded = plugin_registry.read(scope)
            except Exception:
                continue
            for name, item in recorded.items():
                if cfg.enabled.get(name) is False:
                    continue
                directory = item.get('directory') if isinstance(item, dict) else None
                if not isinstance(directory, str) or not os.path.isabs(directory):
                    continue
                try:
                    resolved = Path(directory).resolve()
                except (OSError, RuntimeError):
                    continue
                if str(resolved) not in trusted_pins:
                    continue
                manifest = _read_plugin_manifest(resolved)
                if manifest is None:
                    continue
                manifest_name = manifest.get('name')
                if isinstance(manifest_name, str) and manifest_name:
                    if manifest_name != name:
                        continue
                    manifest_default = manifest.get('default') if isinstance(manifest.get('default'), str) else 'off'
                    if not cfg.is_on(manifest_name, manifest_default):
                        continue
                elif cfg.enabled.get(name) is not True:
                    continue
                _add_read_dir(rules, resolved, home_dir)
    extra_sensitive = _extra_sensitive_paths(environment)
    final_rules: list[str] = []
    for rule in dict.fromkeys(rules):
        cleaned = _process_derived_rule(rule, home_dir, extra_sensitive=extra_sensitive)
        if cleaned is not None and cleaned not in final_rules:
            final_rules.append(cleaned)
    return tuple(final_rules)


def _load_personal_permissions(environment: Mapping[str, str] | None) -> dict:
    """Read personal `permissions` from `~/.gemini/antigravity-cli/settings.json` read-only so `--settings` preserves them."""
    path = storage.agy_home(environment) / 'settings.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ValueError(f'Cannot read Antigravity personal settings {str(path)!r}: {error}') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f'Antigravity personal settings path is not a regular file: {str(path)!r}')
        os.set_blocking(fd, True)
        with os.fdopen(fd, 'r', encoding='utf-8') as handle:
            fd = -1
            content = handle.read()
        loaded = json.loads(_strip_jsonc(content))
    except ValueError as error:
        if str(error).startswith('Antigravity personal settings'):
            raise
        raise ValueError(f'Cannot parse Antigravity personal settings {str(path)!r}: {error}') from None
    except OSError as error:
        raise ValueError(f'Cannot read Antigravity personal settings {str(path)!r}: {error}') from None
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(loaded, dict):
        raise ValueError(f'Antigravity personal settings {str(path)!r} must be a JSON object')
    raw_perms = loaded.get('permissions')
    if raw_perms is None:
        return {}
    if not isinstance(raw_perms, dict):
        raise ValueError(f'Antigravity personal settings "permissions" in {str(path)!r} must be a JSON object')
    perms = {}
    for key, value in raw_perms.items():
        if key in ('allow', 'ask', 'deny'):
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f'Antigravity personal settings "permissions.{key}" must be a list of strings')
            perms[key] = list(value)
        else:
            perms[key] = value
    return perms


def _process_derived_rule(
    rule: str,
    home_dir: Path | None,
    *,
    extra_sensitive: Sequence[Path] | None,
) -> str | None:
    """Expand leading `~` / `~/` in a derived rule, enforce wide exclusions, and require a valid sandbox path."""
    prepared = _prepare_permission_rule(rule, home_dir)
    if prepared is None:
        return None
    rule_text, file_path, is_file_rule = prepared
    if is_file_rule and _rule_allows_sensitive(rule_text, home_dir, extra_sensitive=extra_sensitive):
        return None
    if is_file_rule and (file_path is None or not _is_valid_sandbox_file_path(file_path)):
        return None
    return rule_text


def _format_permission_refusal(
    refused: Sequence[tuple[str, str, str]],
    *,
    sandbox_on: bool = True,
    expand_tilde: bool = True,
) -> str:
    """Build an actionable LaunchRefused reason naming each unsupported deny/ask rule and its source file."""
    unique = list(dict.fromkeys(refused))
    has_deny = any(bucket == 'permissions.deny' for bucket, _, _ in unique)
    has_ask = any(bucket == 'permissions.ask' for bucket, _, _ in unique)
    items = '; '.join(
        f'{bucket} {rule if rule.strip() else repr(rule)} in {source}'
        for bucket, rule, source in unique
    )
    tilde_hint = '' if expand_tilde else ' (and no unexpanded ~ in launch-only mode)'
    if has_deny and sandbox_on:
        deny_part = (
            f'Antigravity 1.5.16 needs an absolute path with no glob{tilde_hint} for permissions.deny '
            '(its OS sandbox rejects non-absolute or globbed deny rules and fails every sandboxed command)'
        )
        lead = f'{deny_part} and a non-glob path for permissions.ask' if has_ask else deny_part
    elif has_deny and has_ask:
        lead = (
            f'Antigravity 1.5.16 ignores globs and unexpandable paths{tilde_hint} in permissions.deny and '
            'permissions.ask (use an absolute or workspace-relative path with no glob)'
        )
    elif has_deny:
        lead = (
            f'Antigravity 1.5.16 ignores globs and unexpandable paths{tilde_hint} in permissions.deny '
            '(use an absolute or workspace-relative path with no glob)'
        )
    else:
        lead = (
            f'Antigravity 1.5.16 ignores globs and unexpandable paths{tilde_hint} in permissions.ask '
            '(use an absolute or workspace-relative path with no glob)'
        )
    return (
        f'Refusing Antigravity launch: {lead}; mv2 will not drop those rules: {items}. '
        'Fix those rules in the named file(s).'
    )


def _process_person_rule(
    item: str,
    key: str,
    home_dir: Path | None,
    *,
    source: str,
    from_personal: bool,
    workspace: Path | None,
    dropped: list[str] | None,
    invalid: list[str] | None,
    ignored_personal: list[tuple[str, str]] | None,
    refused: list[tuple[str, str, str]] | None,
    extra_sensitive: Sequence[Path] | None,
    sandbox_on: bool = True,
    expand_tilde: bool = True,
) -> str | None:
    """Expand leading `~` / `~/` (when bridged); enforce allow drops, personal ignores, and deny/harness-ask refusals."""
    prepared = _prepare_permission_rule(item, home_dir, expand_tilde=expand_tilde)
    if key == 'allow':
        if prepared is None:
            if invalid is not None:
                invalid.append(item)
            return None
        rule_text, file_path, is_file_rule = prepared
        if is_file_rule and _rule_allows_sensitive(
            rule_text, home_dir, workspace=workspace,
            sensitive_rels=_PERSON_SENSITIVE_HOME_REL, broad_roots=False,
            extra_sensitive=extra_sensitive,
        ):
            if dropped is not None:
                dropped.append(item)
            return None
        if is_file_rule and (file_path is None or not _is_valid_sandbox_file_path(file_path)):
            if invalid is not None:
                invalid.append(item)
            return None
        return rule_text
    if key == 'deny':
        if sandbox_on:
            # With exebox on, plain agy 1.5.16 rejects any non-absolute or globbed deny rule from personal or --settings.
            if prepared is None:
                if refused is not None:
                    refused.append(('permissions.deny', item, source))
                return None
            rule_text, file_path, is_file_rule = prepared
            if is_file_rule and (file_path is None or not _is_valid_sandbox_file_path(file_path)):
                if refused is not None:
                    refused.append(('permissions.deny', item, source))
                return None
            return rule_text
        # With exebox off (--dangerously-skip-permissions / --sandbox=false), CoversPath still enforces absolute and
        # workspace-relative non-glob deny rules while silently ignoring globs / unexpandable / malformed rules.
        if prepared is None:
            if from_personal:
                if ignored_personal is not None:
                    ignored_personal.append(('permissions.deny', item))
            elif refused is not None:
                refused.append(('permissions.deny', item, source))
            return None
        rule_text, file_path, is_file_rule = prepared
        if is_file_rule and (file_path is None or not _is_valid_ask_file_path(file_path)):
            if from_personal:
                if ignored_personal is not None:
                    ignored_personal.append(('permissions.deny', item))
            elif refused is not None:
                refused.append(('permissions.deny', item, source))
            return None
        return rule_text
    # key == 'ask': plain Antigravity 1.5.16 enforces absolute and workspace-relative non-glob ask rules without
    # passing ask to exebox, and silently ignores globs / unexpandable / malformed ask rules.
    if prepared is None:
        if from_personal:
            if ignored_personal is not None:
                ignored_personal.append(('permissions.ask', item))
        elif refused is not None:
            refused.append(('permissions.ask', item, source))
        return None
    rule_text, file_path, is_file_rule = prepared
    if is_file_rule and (file_path is None or not _is_valid_ask_file_path(file_path)):
        if from_personal:
            if ignored_personal is not None:
                ignored_personal.append(('permissions.ask', item))
        elif refused is not None:
            refused.append(('permissions.ask', item, source))
        return None
    return rule_text


def _validate_plain_permissions(
    personal: Mapping[str, object],
    override: object,
    *,
    home_dir: Path | None,
    workspace: Path | None,
    sandbox_on: bool,
    personal_source: str,
    override_source: str,
) -> None:
    """Refuse launch-only (`plain`) launches when personal or harness `--settings` deny/ask rules would cripple or fail."""
    refused: list[tuple[str, str, str]] = []
    for key in ('deny', 'ask'):
        value = personal.get(key) if isinstance(personal, Mapping) else None
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    _process_person_rule(
                        item, key, home_dir, source=personal_source, from_personal=True,
                        workspace=workspace, dropped=None, invalid=None,
                        ignored_personal=None, refused=refused, extra_sensitive=None,
                        sandbox_on=sandbox_on, expand_tilde=False,
                    )
    if isinstance(override, Mapping):
        for key in ('deny', 'ask'):
            value = override.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        _process_person_rule(
                            item, key, home_dir, source=override_source, from_personal=False,
                            workspace=workspace, dropped=None, invalid=None,
                            ignored_personal=None, refused=refused, extra_sensitive=None,
                            sandbox_on=sandbox_on, expand_tilde=False,
                        )
    if refused:
        raise LaunchRefused(_format_permission_refusal(refused, sandbox_on=sandbox_on, expand_tilde=False))


def _merge_permissions(personal: Mapping[str, object], override: object, allow_rules: Sequence[str], *,
                       home_dir: Path | None = None, workspace: Path | None = None,
                       dropped: list[str] | None = None,
                       invalid: list[str] | None = None,
                       ignored_personal: list[tuple[str, str]] | None = None,
                       sandbox_on: bool = True,
                       personal_source: str = '~/.gemini/antigravity-cli/settings.json',
                       override_source: str = 'harness.json --settings',
                       extra_sensitive: Sequence[Path] | None = None) -> dict | None:
    """Layer personal, harness `--settings`, and mv2 `allow_rules` with Antigravity 1.5.16 sandbox safety.

    Every rule expands leading `~` / `~/` against `home_dir` before exclusion checks run.
    - `allow`: protected credential paths go to `dropped`; unsupported/malformed allow rules go to `invalid`.
    - `deny`: when `sandbox_on` is True, non-absolute, globbed, unexpandable or malformed deny rules (personal or
      harness) raise `LaunchRefused` naming each rule and source file, because exebox rejects them and breaks every
      sandboxed command; when `sandbox_on` is False (`--dangerously-skip-permissions` / `--sandbox=false`), absolute
      and workspace-relative non-glob deny rules stay enforced while globbed/unexpandable rules are left out with a
      notice for personal settings and raise `LaunchRefused` for harness `--settings`.
    - `ask`: absolute and workspace-relative non-glob rules stay enforced; globs, unexpandable paths and malformed
      rules are left out with a notice when from personal settings (plain agy ignores them) and raise
      `LaunchRefused` when passed through harness `--settings`.
    """
    if override is not None and not isinstance(override, dict):
        raise ValueError('Antigravity settings "permissions" must be a JSON object')
    if not personal and override is None and not allow_rules:
        return None
    perms: dict[str, object] = {}
    refused: list[tuple[str, str, str]] = []
    for key, value in personal.items():
        if key in ('allow', 'ask', 'deny') and isinstance(value, list):
            kept: list[str] = []
            for item in value:
                cleaned = _process_person_rule(
                    item, key, home_dir, source=personal_source, from_personal=True,
                    workspace=workspace, dropped=dropped, invalid=invalid,
                    ignored_personal=ignored_personal, refused=refused, extra_sensitive=extra_sensitive,
                    sandbox_on=sandbox_on, expand_tilde=True,
                )
                if cleaned is not None and cleaned not in kept:
                    kept.append(cleaned)
            perms[key] = kept
        elif isinstance(value, list):
            perms[key] = list(value)
        else:
            perms[key] = value
    if isinstance(override, dict):
        for key, value in override.items():
            if key in ('allow', 'ask', 'deny'):
                if value is None:
                    continue
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError(f'Antigravity settings "permissions.{key}" must be a list of strings')
                existing = list(perms.get(key) or ())
                for item in value:
                    cleaned = _process_person_rule(
                        item, key, home_dir, source=override_source, from_personal=False,
                        workspace=workspace, dropped=dropped, invalid=invalid,
                        ignored_personal=ignored_personal, refused=refused, extra_sensitive=extra_sensitive,
                        sandbox_on=sandbox_on, expand_tilde=True,
                    )
                    if cleaned is not None and cleaned not in existing:
                        existing.append(cleaned)
                perms[key] = existing
            else:
                perms[key] = value
    if refused:
        raise LaunchRefused(_format_permission_refusal(refused, sandbox_on=sandbox_on, expand_tilde=True))
    if allow_rules:
        allow = list(perms.get('allow') or ())
        for rule in allow_rules:
            cleaned = _process_derived_rule(rule, home_dir, extra_sensitive=extra_sensitive)
            if cleaned is not None and cleaned not in allow:
                allow.append(cleaned)
        perms['allow'] = allow
    return perms


def _settings_config(base: dict, allow_rules: Sequence[str] = (),
                     personal_permissions: Mapping[str, object] | None = None, *,
                     home_dir: Path | None = None, workspace: Path | None = None,
                     dropped: list[str] | None = None,
                     invalid: list[str] | None = None,
                     ignored_personal: list[tuple[str, str]] | None = None,
                     sandbox_on: bool = True,
                     personal_source: str = '~/.gemini/antigravity-cli/settings.json',
                     override_source: str = 'harness.json --settings',
                     extra_sensitive: Sequence[Path] | None = None) -> str:
    merged = dict(base)
    merged['statusLine'] = {
        'type': 'command',
        'command': _bridge_command('statusline'),
        'stack_with_default': True,
    }
    perms = _merge_permissions(
        personal_permissions or {}, merged.get('permissions'), allow_rules,
        home_dir=home_dir, workspace=workspace, dropped=dropped, invalid=invalid,
        ignored_personal=ignored_personal, sandbox_on=sandbox_on,
        personal_source=personal_source, override_source=override_source,
        extra_sensitive=extra_sensitive,
    )
    if perms is not None:
        merged['permissions'] = perms
    return json.dumps(merged, indent=2, ensure_ascii=False) + '\n'


def _nonempty_tail(text: str, limit: int) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()][-limit:]


def _startup_prompt(text: str | None) -> dict | None:
    """Detect Antigravity's active bottom workspace-trust dialog, ignoring older scrollback text."""
    if not isinstance(text, str) or not text:
        return None
    tail = _nonempty_tail(text, 16)
    marker_idx = None
    for idx in range(len(tail) - 1, -1, -1):
        if _TRUST_MARKER in tail[idx]:
            marker_idx = idx
            break
    if marker_idx is None:
        return None
    below = tail[marker_idx + 1:]
    if not any(_TRUST_CHOICE.match(line) for line in below):
        return None
    detail = None
    window_start = max(0, marker_idx - 4)
    for idx in range(window_start, marker_idx + 1):
        line = tail[idx]
        match = _ACCESSING_WORKSPACE.search(line)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                detail = candidate[:500]
        elif line.strip() == 'Accessing workspace:' and idx + 1 < marker_idx:
            candidate = tail[idx + 1].strip()
            if candidate:
                detail = candidate[:500]
    return {'title': 'Trust this workspace', 'detail': detail}


def _screen_ready(text: str | None) -> bool:
    """Detect Antigravity's active bottom composer/footer, ignoring scrollback text and trust dialogs."""
    if not isinstance(text, str) or not text:
        return False
    if _startup_prompt(text) is not None:
        return False
    tail = _nonempty_tail(text, 12)
    if any(_TRUST_MARKER in line for line in tail):
        return False
    footer = tail[-6:]
    if any(_READY_HINT in line for line in footer):
        return True
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    raw_tail = lines[-14:]
    return any(
        _PROMPT_LINE.match(line) and idx > 0 and _RULE.match(raw_tail[idx - 1])
        and any(_RULE.match(below) for below in raw_tail[idx + 1:idx + 6])
        for idx, line in enumerate(raw_tail)
    )


_agentapi_recipient = agy_bridge.agentapi_recipient


class AgyAdapter(ConfiguredAdapter):
    resume_prompt_args = ('--prompt-interactive={task}',)
    # Antigravity has no mod-injected wire UUID in its transcript, and its bridge activates via an
    # interactive prompt + shell callback rather than a promptless _queue_wake resume.
    delivery_attempt_ids = False
    package = Path(__file__).parent
    supports_bridge = True
    family = 'agy'
    bridge_policy = 'auto'
    storage_env = ()
    # `--effort LEVEL`: Antigravity's own flag for the current session only; never edits settings.json.
    supports_launch_effort = True

    @classmethod
    def configured(cls, name, settings):
        raw = settings.get('inbox_bridge')
        adapter = super().configured(name, {**settings, 'inbox_bridge': True if raw is None else raw})
        adapter.bridge_policy = 'auto' if raw is None else 'required' if raw is True else 'plain'
        return adapter

    def spawn(self, context):
        command = super().spawn(context)
        return self._finalize(command, context, has_prompt=bool(context.values.get('task')))

    def resume(self, session, context):
        command = super().resume(session, context)
        native_id = normalize_session_id(session.native_id)
        home = (session.home if isinstance(session.home, str) and os.path.isabs(session.home)
                else str(storage.agy_home(context.environment)))
        exists = storage.conversation_exists(native_id, home)
        if exists is False:
            raise ValueError(
                f'No saved Antigravity conversation found for session {native_id} under {home}; '
                'refusing to start a fresh session on resume'
            )
        if exists is None:
            raise ValueError(
                f'Cannot read Antigravity storage under {home}; restore read access before resuming'
            )
        return self._finalize(command, context, has_prompt=context.resume_prompt is not None)

    def _finalize(self, command: CommandSpec, context, *, has_prompt: bool) -> CommandSpec:
        argv = list(command.argv)
        prompt_arg = None
        if has_prompt:
            # Locate our `--prompt-interactive=...` argument so we always keep it last on argv.
            expected_text = context.resume_prompt if context.mode == 'resume' else context.values.get('task', '')
            expected_arg = f'--prompt-interactive={expected_text}'
            for idx in range(len(argv) - 1, 0, -1):
                if argv[idx] == expected_arg:
                    prompt_arg = argv.pop(idx)
                    break

        launch_dir = context.values.get('launch_dir') or (
            str(Path(context.values['bootstrap_file']).parent) if context.values.get('bootstrap_file') else ''
        )
        effort = getattr(context, 'effort', None)
        if effort:
            argv.append(f'--effort={effort}')
        launch_files = {'AGENTS.md': context.values.get('bootstrap', '')}
        policy = self.bridge_policy
        raw_path = (context.environment or {}).get('PATH')
        exe_path = raw_path if isinstance(raw_path, str) else None
        problem = None if policy == 'plain' else bridge_problem(argv[0], path=exe_path)
        if problem is not None and policy == 'required':
            raise ValueError(problem)
        bridge = policy != 'plain' and problem is None
        sandbox_env = dict(context.environment or {})
        extra_notices: list[str] = []
        if context.mode == 'resume':
            # On resume, lifecycle merges agent-writable properties.json['native_environment'] into context.environment
            # and reads runtime.json['run_directory'] into MV2_RUN_DIR; never let saved agent state smuggle HOME,
            # MV2_PLUGIN_PATH or an unverified MV2_RUN_DIR into sandbox or personal-settings resolution.
            from ...runtime import paths as runtime_paths
            host_home_env = os.environ.get('HOME')
            if isinstance(host_home_env, str) and host_home_env and os.path.isabs(host_home_env):
                sandbox_env['HOME'] = host_home_env
            else:
                sandbox_env['HOME'] = str(Path.home())
            host_plugin_path = os.environ.get('MV2_PLUGIN_PATH')
            if isinstance(host_plugin_path, str) and host_plugin_path:
                sandbox_env['MV2_PLUGIN_PATH'] = host_plugin_path
            else:
                sandbox_env.pop('MV2_PLUGIN_PATH', None)
            raw_resume_run = sandbox_env.get('MV2_RUN_DIR')
            if isinstance(raw_resume_run, str) and raw_resume_run and os.path.isabs(raw_resume_run):
                try:
                    host_run = runtime_paths.run_directory()
                    if Path(raw_resume_run).resolve() != host_run:
                        sandbox_env.pop('MV2_RUN_DIR', None)
                        extra_notices.append(
                            f'Skipped sandbox write access to recorded run directory {raw_resume_run!r} on resume '
                            f'because it differs from this shell\'s MV2_RUN_DIR ({host_run}); resume with matching '
                            'MV2_RUN_DIR if sandboxed mv2 commands need it.'
                        )
                except (OSError, RuntimeError, ValueError):
                    sandbox_env.pop('MV2_RUN_DIR', None)
        home_raw = sandbox_env.get('HOME')
        home_dir = Path(home_raw).resolve() if isinstance(home_raw, str) and home_raw and os.path.isabs(home_raw) else None
        raw_workspace = context.values.get('workspace')
        workspace_root = None
        if isinstance(raw_workspace, str) and raw_workspace and os.path.isabs(raw_workspace):
            try:
                workspace_root = Path(raw_workspace).resolve()
            except (OSError, RuntimeError):
                workspace_root = None
        forbidden_settings_roots: list[Path] = []
        uni_root = None
        raw_uni = sandbox_env.get('MV2_UNIVERSE')
        if isinstance(raw_uni, str) and raw_uni and os.path.isabs(raw_uni):
            try:
                uni_root = Path(raw_uni).resolve()
                forbidden_settings_roots.append(uni_root / 'state')
                top = uni_root
                while top.parent.name == 'subverses' and (top.parent.parent / 'universe.json').is_file():
                    top = top.parent.parent
                if top != uni_root:
                    forbidden_settings_roots.append(top / 'state')
            except (OSError, RuntimeError):
                uni_root = None
        raw_run = sandbox_env.get('MV2_RUN_DIR')
        if isinstance(raw_run, str) and raw_run and os.path.isabs(raw_run):
            try:
                forbidden_settings_roots.append(Path(raw_run).resolve())
            except (OSError, RuntimeError):
                pass
        if home_dir is not None:
            forbidden_settings_roots.append(home_dir / '.multiverse' / 'tmux-servers')
        sandbox_on = _sandbox_enabled_for_argv(argv)
        user_settings = {}
        user_settings_sources: dict[str, str] = {}
        personal_permissions = {}
        personal_settings_source = str(storage.agy_home(sandbox_env) / 'settings.json')
        user_settings_loaded = False
        personal_loaded = False
        if bridge:
            plain_argv = list(argv)
            bridge_argv, user_settings = _extract_user_settings(
                argv, forbidden_roots=forbidden_settings_roots, sources=user_settings_sources,
            )
            user_settings_loaded = True
            try:
                personal_permissions = _load_personal_permissions(sandbox_env)
            except ValueError as error:
                if policy == 'required':
                    raise
                bridge = False
                problem = str(error)
                argv = plain_argv
            else:
                personal_loaded = True
                argv = bridge_argv

        plan = plugin_harness.plan_for(
            context, 'agy', self.name,
            version=lambda: agy_version(argv[0], path=exe_path),
            bridge=bridge,
            base_environment=command.environment,
        )
        extra_sensitive = _extra_sensitive_paths(sandbox_env)
        if plan is not None:
            for directory in plan.agy_add_dirs:
                expanded_add = _expand_tilde_path(directory, home_dir) if isinstance(directory, str) else None
                if expanded_add is None or not os.path.isabs(expanded_add):
                    continue
                try:
                    resolved_add = Path(expanded_add).resolve()
                except (OSError, RuntimeError):
                    continue
                if _is_sensitive_path(resolved_add, home_dir, extra_sensitive=extra_sensitive) or resolved_add in _BROAD_READ_ROOTS:
                    continue
                argv.extend(['--add-dir', expanded_add])
            command = replace(command, environment=plan.environment, plugins=plan.record())

        if not bridge:
            if not user_settings_loaded:
                try:
                    _, user_settings = _extract_user_settings(argv, sources=user_settings_sources)
                except ValueError:
                    user_settings = {}
            if not personal_loaded and (problem is None or 'personal settings' not in problem):
                try:
                    personal_permissions = _load_personal_permissions(sandbox_env)
                except ValueError:
                    personal_permissions = {}
            _validate_plain_permissions(
                personal_permissions, user_settings.get('permissions'),
                home_dir=home_dir, workspace=workspace_root, sandbox_on=sandbox_on,
                personal_source=personal_settings_source,
                override_source=user_settings_sources.get('permissions', 'harness.json --settings'),
            )
            if prompt_arg is not None:
                argv.append(prompt_arg)
            return replace(
                command,
                argv=tuple(argv),
                launch_files=launch_files,
                mouse=True,
                runner_listen=True,
                native_mode={'policy': policy, 'mode': 'plain', 'fallback_reason': problem},
            )

        allow_rules = _sandbox_allow_rules(sandbox_env)
        dropped_rules: list[str] = []
        invalid_rules: list[str] = []
        ignored_personal: list[tuple[str, str]] = []
        launch_files['settings.json'] = _settings_config(
            user_settings, allow_rules, personal_permissions,
            home_dir=home_dir, workspace=workspace_root,
            dropped=dropped_rules, invalid=invalid_rules, ignored_personal=ignored_personal,
            sandbox_on=sandbox_on,
            personal_source=personal_settings_source,
            override_source=user_settings_sources.get('permissions', 'harness.json --settings'),
            extra_sensitive=extra_sensitive,
        )
        if dropped_rules:
            unique_dropped = list(dict.fromkeys(dropped_rules))
            label = 'rule' if len(unique_dropped) == 1 else 'rules'
            extra_notices.append(
                f'Dropped excluded permissions.allow {label} covering protected credential paths: '
                f'{", ".join(unique_dropped)}.'
            )
        if invalid_rules:
            unique_invalid = [rule if rule.strip() else repr(rule) for rule in dict.fromkeys(invalid_rules)]
            label = 'rule' if len(unique_invalid) == 1 else 'rules'
            extra_notices.append(
                f'Dropped unsupported permissions.allow {label} (Antigravity file allow rules require absolute '
                f'paths or ~/ without globs): {", ".join(unique_invalid)}.'
            )
        if ignored_personal:
            for bucket in ('permissions.deny', 'permissions.ask'):
                bucket_rules = [
                    rule if rule.strip() else repr(rule)
                    for b, rule in dict.fromkeys(ignored_personal) if b == bucket
                ]
                if not bucket_rules:
                    continue
                label = 'rule' if len(bucket_rules) == 1 else 'rules'
                extra_notices.append(
                    f'Left out unsupported personal {bucket} {label} from {personal_settings_source} '
                    f'(plain Antigravity 1.5.16 ignores globs, unexpandable paths and malformed rules here): '
                    f'{", ".join(bucket_rules)}.'
                )
        hook_launch_id = sandbox_env.get('MV2_LAUNCH_ID')
        if not isinstance(hook_launch_id, str) or not hook_launch_id:
            hook_launch_id = Path(launch_dir).name if launch_dir else None
        launch_files['.agents/hooks.json'] = _hooks_config(hook_launch_id)
        if launch_dir:
            argv.extend(['--settings', str(Path(launch_dir) / 'settings.json')])
        if prompt_arg is not None:
            argv.append(prompt_arg)
        socket = context.values['control_socket']
        attachment = None if has_prompt else AttachmentSpec(work_owner='terminal', readiness='process')
        notices = (*extra_notices,) if has_prompt else (
            *extra_notices,
            (
                'An idle Antigravity agent starts a turn for an mv2 message only once its live '
                'connection is up (after its first mv2 command); messages sent before then stay '
                'queued until the next turn or live connection.'
            )
            if context.mode == 'resume'
            else (
                'Antigravity starts a conversation on its first prompt (pass --task to start with '
                'one); an idle agent starts a turn for an mv2 message only once its live connection '
                'is up after its first mv2 command.'
            ),
        )
        return replace(
            command,
            argv=tuple(argv),
            control_endpoint=NativeEndpoint('unix-json', socket),
            attachment=attachment,
            notices=notices,
            launch_files=launch_files,
            cleanup_files=(agy_bridge.LS_FILE,),
            shell_callback=True,
            mouse=True,
            runner_listen=True,
            native_mode={'policy': policy, 'mode': 'bridge', 'fallback_reason': None},
        )

    def shell_callback(self, session, environment) -> ShellCallbackResult | None:
        if not session.session_file or not session.launch_id:
            return None
        agent_dir = Path(session.session_file).parent
        try:
            facts = retry_missing(lambda: read(agent_dir / 'runtime.json'))
            if isinstance(facts, dict) and facts.get('binding_conflict'):
                return None
        except (OSError, ValueError):
            pass
        req_kind, expected = agy_bridge._launch_expected_id(agent_dir, session.launch_id)
        raw_conv = environment.get('ANTIGRAVITY_CONVERSATION_ID')
        conv_id = None
        if isinstance(raw_conv, str) and raw_conv.strip():
            try:
                conv_id = normalize_session_id(raw_conv.strip())
            except ValueError:
                conv_id = None
        if req_kind == 'invalid' and (session.native_id is None or (conv_id is not None and conv_id != session.native_id)):
            return None
        if req_kind == 'resume' and conv_id is not None and conv_id != expected:
            return None
        prepared = agy_bridge.prepare_shell_callback(
            agent_dir, session.launch_id, session.native_id, session.bridge,
            environment,
        )
        if prepared is None:
            return None
        native_id, bridge_name, files = prepared
        return ShellCallbackResult(native_id=native_id, bridge=bridge_name, launch_files=files)

    def _bridge(self, session) -> Bridge:
        return bridge_of(getattr(self, 'settings', {}).get('inbox_bridge'), session)

    def session_home(self, environment) -> str:
        return os.path.realpath(storage.agy_home(environment))

    def models(self, *, environment):
        """Checked Antigravity CLI model catalog (models.py), offline and independent of account state."""
        return agy_models.catalog(environment)

    def model_names(self, *, environment):
        """Human labels for Antigravity base IDs and effort-suffixed spellings (models.py)."""
        return agy_models.names(environment)

    def check_model(self, model, *, effort=None, environment):
        """Validate a requested model and optional session effort against the checked Antigravity table."""
        return agy_models.check(model, effort, environment)

    def display_pane(self, text: str) -> str:
        """The live pane without Antigravity's input box and footer (pane.py)."""
        return agy_pane.display(text)

    def transcript_view(self, session, *, tip=None, reader=None):
        """Read-only metadata view of this conversation's Antigravity JSONL transcript."""
        return agy_transcripts.view(session, tip=tip, reader=reader)

    def transcript_bodies(self, session, view, indexes, *, max_bytes, reader=None):
        """Read verified JSON bodies for rows in `view`, raising TranscriptRowsChanged on rewrites."""
        return agy_transcripts.bodies(session, view, indexes, max_bytes=max_bytes, reader=reader)

    def transcript_readable(self, session, *, reader=None):
        """Whether this exact conversation's Antigravity transcript is readable, absent or unknown."""
        return agy_transcripts.readable(session, reader=reader)

    def project_turn_event(self, session, view, transition, *, reader=None):
        """Project a native turn transition onto exact row positions within `view`."""
        return agy_transcripts.project_turn_event(session, view, transition, reader=reader)

    def session_storage_evidence(self, session, home) -> NativeResult:
        return storage.session_storage_evidence(session, home)

    def attachment_ready(self, session) -> bool:
        return not session.endpoint or self._bridge(session).ready

    def connection_problem(self, session) -> str | None:
        if session.state != 'running' or not session.endpoint or not self._bridge(session).launched:
            return None
        if session.bridge == BRIDGE and session.session_file and session.launch_id:
            agent_dir = Path(session.session_file).parent
            ls_file = agy_bridge.ls_path(agent_dir, session.launch_id)
            try:
                os.lstat(ls_file)
            except FileNotFoundError:
                return None
            except OSError:
                return (
                    'Antigravity language-server connection info cannot be read for this launch; '
                    'run an mv2 command inside the session or restart it'
                )
            ls_info = agy_bridge.read_ls_info(agent_dir, session.launch_id)
            if ls_info is None:
                return (
                    'Antigravity language-server connection info is invalid for this launch; '
                    'run an mv2 command inside the session or restart it'
                )
            if (ls_info.get('conversation_id') is not None
                    and session.native_id
                    and ls_info['conversation_id'] != session.native_id):
                return (
                    'Antigravity bridge credentials belong to another conversation; '
                    'run an mv2 command inside this session'
                )
        return None

    def workspace_trust(self, workspace, *, environment=None) -> str:
        """Read-only check of Antigravity's `trustedWorkspaces` in `<agy_home>/settings.json`.

        Returns `'trusted'`, `'untrusted'`, or `'unknown'`. Never writes to Antigravity's home.
        """
        if not isinstance(workspace, (str, os.PathLike)):
            return 'unknown'
        raw = os.fspath(workspace)
        if not isinstance(raw, str) or not raw.strip() or not os.path.isabs(raw):
            return 'unknown'
        try:
            resolved = os.path.realpath(raw)
        except OSError:
            return 'unknown'
        source = os.environ if environment is None else environment
        raw_home = source.get('HOME') if hasattr(source, 'get') else None
        if not isinstance(raw_home, str) or not raw_home or not os.path.isabs(raw_home):
            return 'unknown'
        home_path = Path(raw_home)
        try:
            if not home_path.is_dir():
                return 'unknown'
        except OSError:
            return 'unknown'
        settings_path = home_path / '.gemini' / 'antigravity-cli' / 'settings.json'
        try:
            fd = os.open(settings_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return 'unknown'
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > (4 << 20):
                return 'unknown'
            os.set_blocking(fd, True)
            with os.fdopen(fd, 'r', encoding='utf-8') as handle:
                fd = -1
                loaded = json.loads(_strip_jsonc(handle.read()))
        except (OSError, UnicodeDecodeError, ValueError):
            return 'unknown'
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(loaded, dict):
            return 'unknown'
        raw_trusted = loaded.get('trustedWorkspaces')
        if raw_trusted is None:
            return 'untrusted'
        if not isinstance(raw_trusted, list) or not all(isinstance(item, str) for item in raw_trusted):
            return 'unknown'
        return 'trusted' if resolved in set(raw_trusted) else 'untrusted'

    def launch_prompt(self, evidence):
        return _startup_prompt(evidence.get('text') if isinstance(evidence, dict) else None)

    def launch_started(self, evidence):
        return _screen_ready(evidence.get('text') if isinstance(evidence, dict) else None)

    def capabilities(self, session, executor=None) -> dict[str, Capability]:
        result = super().capabilities(session, executor)
        if not session.agent_id:
            return {**result, **self._would_support(executor)}
        bridge = self._bridge(session)
        result['session_name'] = Capability(
            'unavailable', 'Antigravity CLI has no non-interactive conversation rename API',
        )
        result['send_steer'] = (
            Capability('supported', mechanism='agy-agentapi', required=True) if bridge.ready
            else Capability('unavailable', bridge.reason,
                            mechanism='launch-only' if bridge.final else None, required=True)
        )
        result['send_follow_up'] = (
            Capability('supported', mechanism='agy-stop-hook', required=True) if bridge.ready
            else Capability('unavailable', bridge.reason,
                            mechanism='launch-only' if bridge.final else None, required=True)
        )
        result['queued'] = (
            Capability('supported', mechanism='agy-stop-hook') if bridge.ready
            else Capability('unavailable', bridge.reason,
                            mechanism='launch-only' if bridge.final else None)
        )
        if bridge.launched and session.session_file:
            result['get_delivery'] = Capability('supported', mechanism='agy-transcript')
        if session.native_id:
            result['get_usage'] = Capability('supported', mechanism='agy-transcript')
        return result

    def _would_support(self, executor) -> dict[str, Capability]:
        if self.bridge_policy == 'plain':
            found, why = None, 'Agents of this harness are launch-only: it sets inbox_bridge to false'
        elif executor is None:
            found = (f'Through the Antigravity hooks/agentapi bridge, on Antigravity CLI '
                     f'{".".join(map(str, MIN_BRIDGE_VERSION))} or newer; --probe checks the installed version')
            why = None
        else:
            problem = bridge_problem(self.spec['spawn'][0])
            found = None if problem else 'Through the Antigravity hooks/agentapi bridge'
            why = problem and (f'Launches of this harness refuse to start: {problem}' if self.bridge_policy == 'required'
                               else f'Agents of this harness launch without the bridge: {problem}')
        mechanisms = {
            'send_steer': 'agy-agentapi',
            'send_follow_up': 'agy-stop-hook',
            'get_delivery': 'agy-transcript',
            'queued': 'agy-stop-hook',
        }
        result = {
            name: (Capability('supported', found, mechanism, required=name.startswith('send_')) if found
                   else Capability('unavailable', why,
                                   mechanism='launch-only' if self.bridge_policy == 'plain' and name.startswith('send_') else None,
                                   required=name.startswith('send_')))
            for name, mechanism in mechanisms.items()
        }
        result['session_name'] = Capability(
            'unavailable', 'Antigravity CLI has no non-interactive conversation rename API',
        )
        result['get_usage'] = Capability('supported', mechanism='agy-transcript')
        return result

    def send(self, session, message, executor) -> NativeResult:
        mode = MODES.get(message.delivery)
        if mode is None:
            return NativeResult('unavailable', f'Unknown delivery mode: {message.delivery}')
        bridge = self._bridge(session)
        if not bridge.ready:
            return unavailable(bridge)
        if not session.session_file or not session.launch_id:
            return NativeResult('unavailable', 'No agent directory or launch ID recorded')
        agent_dir = Path(session.session_file).parent
        current_facts = None
        try:
            current_facts = retry_missing(lambda: read(agent_dir / 'runtime.json'))
            if isinstance(current_facts, dict) and current_facts.get('binding_conflict'):
                return NativeResult('unavailable', str(current_facts['binding_conflict']))
        except (OSError, ValueError):
            pass
        ls_info = agy_bridge.read_ls_info(agent_dir, session.launch_id)
        if ls_info is not None and ls_info.get('launch_id') != session.launch_id:
            ls_info = None
        if ls_info is not None and ls_info.get('conversation_id') is not None and ls_info['conversation_id'] != session.native_id:
            return NativeResult(
                'unavailable',
                'Antigravity bridge credentials belong to another conversation; run an mv2 command inside this session',
            )

        pinned_address = None
        if ls_info is not None:
            try:
                pinned_address = agy_bridge.pin_loopback_endpoint(ls_info['ls_address'], timeout=5.0)
            except (ValueError, RuntimeError) as error:
                return NativeResult('error', str(error))

        current_status = current_facts.get('status') if isinstance(current_facts, dict) else None
        text = message.framed or message.text
        submitted_at = now()
        text_sha256 = storage.message_hash(text)
        claimed_for_agentapi = False
        claim_token = None

        should_queue = (ls_info is None) or (
            mode == 'follow-up' and (
                current_status != 'idle'
                or agy_bridge.should_queue_idle_followup(
                    agent_dir, session.launch_id, session.native_id,
                    live_facts=current_facts if isinstance(current_facts, dict) else None,
                )
            )
        )
        if should_queue:
            executor.check_session()
            try:
                agy_bridge.write_receipt(
                    agent_dir,
                    message.id,
                    launch_id=session.launch_id,
                    native_id=session.native_id,
                    route='stop-hook',
                    submitted_at=submitted_at,
                    text_sha256=text_sha256,
                    delivered_at=None,
                    acknowledged=True,
                )
            except (OSError, ValueError) as error:
                detail = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
                return NativeResult(
                    'error',
                    f'Cannot persist Antigravity delivery receipt before send: {detail}',
                )
            try:
                agy_bridge.enqueue_followup(agent_dir, session.launch_id, message.id, session.native_id, text)
            except (OSError, ValueError) as error:
                agy_bridge.remove_receipt(agent_dir, message.id)
                detail = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
                return NativeResult('error', detail)
            # A new send on an idle session resumes any cancel/error-paused queue in FIFO order
            # (earlier paused head runs first; this message stays queued behind it).
            if current_status == 'idle':
                agy_bridge.resume_paused_for_new_send(agent_dir, session.launch_id, session.native_id)
            if ls_info is None:
                # Recheck whether agentapi credentials appeared concurrently before deciding to stay queued.
                latest_ls = agy_bridge.read_ls_info(agent_dir, session.launch_id)
                if (latest_ls is not None and latest_ls.get('launch_id') == session.launch_id
                        and (latest_ls.get('conversation_id') in (None, session.native_id))):
                    try:
                        pinned_address = agy_bridge.pin_loopback_endpoint(latest_ls['ls_address'], timeout=5.0)
                        ls_info = latest_ls
                    except (ValueError, RuntimeError):
                        ls_info = None
            if ls_info is None:
                latest_status = current_status
                try:
                    latest_facts = retry_missing(lambda: read(agent_dir / 'runtime.json'))
                    if isinstance(latest_facts, dict) and latest_facts.get('launch_id') == session.launch_id:
                        latest_status = latest_facts.get('status')
                except (OSError, ValueError):
                    pass
                if latest_status not in ('working', 'waiting'):
                    return NativeResult(
                        'native_queued',
                        'Queued for next turn; waiting for Antigravity live connection to start a turn while idle',
                        {'route': 'stop-hook', 'transcript_confirmed': False, 'queued': True, 'idle_start_error': True},
                    )
                return NativeResult(
                    'native_queued',
                    'Queued for end-of-turn delivery by the Antigravity Stop hook',
                    {'route': 'stop-hook', 'transcript_confirmed': False, 'queued': True},
                )
            # Recheck runtime status under the queue lock to close the race where the turn went idle just before enqueue.
            claim_token = str(uuid4())
            claim = agy_bridge.claim_if_idle_head(
                agent_dir, session.launch_id, message.id, session.native_id, claim_token=claim_token,
            )
            if claim == 'queued':
                agy_bridge.kick_idle_flush_if_ready(agent_dir, session.launch_id, session.native_id)
                return NativeResult(
                    'native_queued',
                    'Queued for end-of-turn delivery by the Antigravity Stop hook',
                    {'route': 'stop-hook', 'transcript_confirmed': False, 'queued': True},
                )
            if claim == 'handed_off':
                return NativeResult(
                    'native_queued',
                    'Handed off to Antigravity at turn end by the Stop hook',
                    {'route': 'stop-hook', 'transcript_confirmed': False},
                )
            if claim == 'submitting':
                return NativeResult(
                    'native_queued',
                    'Handed off for idle delivery via Antigravity agentapi',
                    {'route': 'agentapi', 'transcript_confirmed': False},
                )
            if claim != 'claimed':
                return NativeResult(
                    'unknown',
                    'Queued follow-up state changed before idle handoff; check delivery before retrying',
                    {'route': 'stop-hook', 'transcript_confirmed': False},
                )
            claimed_for_agentapi = True

        # Persist agentapi receipt before submitting so an uncertain exit can still reconcile against transcript.
        # For a claimed queued follow-up, commit the agentapi receipt via CAS under the queue lock so a stalled
        # claimer cannot submit after the stale-claim reaper already returned the entry to queued or Stop handed it off.
        if claimed_for_agentapi:
            try:
                executor.check_session()
                commit = agy_bridge.commit_followup_agentapi_receipt(
                    agent_dir,
                    session.launch_id,
                    message.id,
                    claim_token=claim_token,
                    native_id=session.native_id,
                    submitted_at=submitted_at,
                    text_sha256=text_sha256,
                )
            except Exception:
                if agy_bridge.release_followup_claim(
                    agent_dir, session.launch_id, message.id, reset_idle_flush=True, claim_token=claim_token,
                ):
                    return NativeResult(
                        'native_queued',
                        'Queued for end-of-turn delivery by the Antigravity Stop hook',
                        {'route': 'stop-hook', 'transcript_confirmed': False, 'queued': True},
                    )
                raise
            if commit == 'handed_off':
                return NativeResult(
                    'native_queued',
                    'Handed off to Antigravity at turn end by the Stop hook',
                    {'route': 'stop-hook', 'transcript_confirmed': False},
                )
            if commit == 'queued':
                agy_bridge.kick_idle_flush_if_ready(agent_dir, session.launch_id, session.native_id)
                return NativeResult(
                    'native_queued',
                    'Queued for end-of-turn delivery by the Antigravity Stop hook',
                    {'route': 'stop-hook', 'transcript_confirmed': False, 'queued': True},
                )
            if commit == 'submitting':
                return NativeResult(
                    'native_queued',
                    'Handed off for idle delivery via Antigravity agentapi',
                    {'route': 'agentapi', 'transcript_confirmed': False},
                )
            if commit != 'committed':
                return NativeResult(
                    'unknown',
                    'Queued follow-up state changed before idle handoff; check delivery before retrying',
                    {'route': 'stop-hook', 'transcript_confirmed': False},
                )
        else:
            try:
                agy_bridge.write_receipt(
                    agent_dir,
                    message.id,
                    launch_id=session.launch_id,
                    native_id=session.native_id,
                    route='agentapi',
                    submitted_at=submitted_at,
                    text_sha256=text_sha256,
                    delivered_at=None,
                    acknowledged=False,
                )
            except (OSError, ValueError) as error:
                detail = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
                return NativeResult(
                    'error',
                    f'Cannot persist Antigravity delivery receipt before send: {detail}',
                )
        command = CommandSpec(
            (self.spec['spawn'][0], 'agentapi', 'send-message', session.native_id, text),
            unset_env=PARENT_AGY,
            environment={
                'ANTIGRAVITY_LS_ADDRESS': pinned_address,
                'ANTIGRAVITY_CSRF_TOKEN': ls_info['csrf_token'],
            },
        )
        # Stamp idle_flushed BEFORE submitting so a fast turn that completes before agentapi returns
        # clears the marker via PreInvocation/Stop instead of having it stamped afterwards.
        pre_turn_seq = (
            agy_bridge.begin_idle_turn(agent_dir, session.launch_id, message_id=message.id)
            if (not claimed_for_agentapi and current_status == 'idle')
            else None
        )
        try:
            completed = executor.command(command, timeout=15)
        except BaseException:
            if pre_turn_seq is not None:
                agy_bridge.cancel_idle_turn_if_unstarted(agent_dir, session.launch_id, pre_turn_seq)
            raise
        if completed.returncode != 0:
            pre_connect = agy_bridge.is_pre_connect_agentapi_error(completed.stdout, completed.stderr)
            if pre_turn_seq is not None:
                agy_bridge.cancel_idle_turn_if_unstarted(agent_dir, session.launch_id, pre_turn_seq)
            if claimed_for_agentapi:
                try:
                    agy_bridge.remove_followup(
                        agent_dir, session.launch_id, message.id,
                        reset_idle_flush=pre_connect, claim_token=claim_token,
                    )
                except (OSError, ValueError):
                    pass
            detail = (completed.stderr or completed.stdout or '').strip()
            if pre_connect:
                agy_bridge.remove_receipt(agent_dir, message.id)
                return NativeResult(
                    'error',
                    f'agy agentapi send-message failed before submission (exit {completed.returncode})'
                    + (f': {detail}' if detail else ''),
                )
            return NativeResult(
                'unknown',
                f'agy agentapi send-message exited {completed.returncode}; acceptance unknown'
                + (f': {detail}' if detail else ''),
                {'route': 'agentapi', 'transcript_confirmed': False},
            )
        if claimed_for_agentapi:
            try:
                agy_bridge.remove_followup(agent_dir, session.launch_id, message.id, claim_token=claim_token)
            except (OSError, ValueError):
                pass
        recipient = _agentapi_recipient(completed.stdout)
        if recipient is None:
            if pre_turn_seq is not None:
                agy_bridge.cancel_idle_turn_if_unstarted(agent_dir, session.launch_id, pre_turn_seq)
            return NativeResult(
                'unknown',
                'agy agentapi send-message exited 0 without confirming recipientId; acceptance unknown',
                {'route': 'agentapi', 'transcript_confirmed': False},
            )
        if recipient != session.native_id:
            if pre_turn_seq is not None:
                agy_bridge.cancel_idle_turn_if_unstarted(agent_dir, session.launch_id, pre_turn_seq)
            return NativeResult(
                'unknown',
                f'agy agentapi send-message reported unexpected recipient {recipient!r}',
                {'route': 'agentapi', 'transcript_confirmed': False},
            )
        agy_bridge.write_receipt(
            agent_dir,
            message.id,
            launch_id=session.launch_id,
            native_id=session.native_id,
            route='agentapi',
            submitted_at=submitted_at,
            text_sha256=text_sha256,
            delivered_at=now(),
            acknowledged=True,
        )
        return NativeResult(
            'native_queued',
            'Submitted to the Antigravity session via agentapi',
            {'route': 'agentapi', 'transcript_confirmed': False},
        )

    def get_delivery(self, session, message_id, executor=None) -> NativeResult:
        if not session.session_file:
            return NativeResult('unavailable', 'No agent directory recorded')
        try:
            identifier = normalize_session_id(message_id)
        except ValueError:
            return NativeResult('error', 'Message ID must be a UUID')
        agent_dir = Path(session.session_file).parent
        receipt = agy_bridge.read_receipt(agent_dir, identifier)
        if receipt is None:
            return NativeResult('unknown', 'No Antigravity delivery receipt recorded for this message')
        if receipt is False:
            return NativeResult('unknown', "Can't read the Antigravity delivery receipt for this message")

        home = storage.recorded_home(session)
        confirmed = None
        if home and receipt.get('text_sha256'):
            cohort = agy_bridge.receipt_cohort(
                agent_dir, identifier,
                native_id=receipt['native_id'],
                route=receipt['route'],
                text_sha256=receipt['text_sha256'],
            )
            if cohort is not None:
                confirmed = storage.transcript_has_message(
                    receipt['native_id'],
                    home,
                    text_sha256=receipt['text_sha256'],
                    route=receipt['route'],
                    cohort_since=cohort[0],
                    cohort_target=cohort[1],
                )
        if confirmed is True:
            return NativeResult(
                'native_queued',
                'Message confirmed in the Antigravity conversation transcript',
                {'transcript_confirmed': True, 'route': receipt.get('route')},
            )

        launch_id = receipt['launch_id']
        if receipt.get('route') == 'stop-hook':
            state, paused_reason = agy_bridge.followup_status(agent_dir, launch_id, identifier)
            if state in ('queued', 'agentapi_submitting'):
                # Receipt route is still stop-hook, so no agentapi receipt write or submission ever ran.
                current_conv = None
                try:
                    current_conv = retry_missing(lambda: read(agent_dir / 'properties.json')).get('native_session_id')
                except (OSError, ValueError):
                    pass
                if current_conv and receipt.get('native_id') and current_conv != receipt['native_id']:
                    return NativeResult(
                        'error',
                        'Queued follow-up was not delivered before the Antigravity conversation switched',
                        {'transcript_confirmed': False, 'route': 'stop-hook', 'definite_failure': True},
                    )
                still_running = session.state == 'running' and session.launch_id == launch_id
                if not still_running:
                    return NativeResult(
                        'error',
                        'Queued follow-up was not delivered before the Antigravity launch ended',
                        {'transcript_confirmed': False, 'route': 'stop-hook', 'definite_failure': True},
                    )
                if paused_reason == 'cancel':
                    return NativeResult(
                        'native_queued',
                        'Queued follow-up is paused after turn cancellation; resumes on the next turn',
                        {'transcript_confirmed': False, 'route': 'stop-hook', 'queued': True,
                         'paused': True, 'paused_reason': 'cancel'},
                    )
                if paused_reason == 'error':
                    return NativeResult(
                        'native_queued',
                        'Queued follow-up is paused after turn error; resumes on the next turn',
                        {'transcript_confirmed': False, 'route': 'stop-hook', 'queued': True,
                         'paused': True, 'paused_reason': 'error'},
                    )
                return NativeResult(
                    'native_queued',
                    'Queued for end-of-turn delivery by the Antigravity Stop hook',
                    {'transcript_confirmed': False, 'route': 'stop-hook', 'queued': True},
                )

        return NativeResult(
            'unknown',
            'Message is not yet confirmed in the Antigravity conversation transcript',
            {'transcript_confirmed': False, 'route': receipt.get('route')},
        )

    def accepted_message_evidence(self, session, message_id, text, since, home, *, framed=True, cohort=(),
                                  receipts=None, active_acceptances=()) -> NativeResult:
        """Reconcile a previous launch's accepted message once that launch ended and a later launch is idle."""
        if not isinstance(home, str) or not os.path.isabs(home):
            return NativeResult('unknown', f'Antigravity storage {home!r} is not an absolute path')
        if not session.session_file or not session.native_id:
            return NativeResult('unknown', 'No agent directory or conversation ID recorded')
        try:
            identifier = normalize_session_id(message_id)
            canonical = normalize_session_id(session.native_id)
        except ValueError:
            return NativeResult('unknown', 'Message or conversation ID is not a valid UUID')
        agent_dir = Path(session.session_file).parent
        receipt = agy_bridge.read_receipt(agent_dir, identifier)
        if not isinstance(receipt, dict) or not receipt.get('text_sha256') or receipt.get('native_id') != canonical:
            return NativeResult('unknown', 'No readable Antigravity delivery receipt for this acceptance')
        cohort = agy_bridge.receipt_cohort(
            agent_dir, identifier,
            native_id=receipt['native_id'],
            route=receipt['route'],
            text_sha256=receipt['text_sha256'],
        )
        if cohort is None:
            return NativeResult('unknown', 'Antigravity delivery receipt cohort could not be established')
        found = storage.transcript_has_message(
            canonical, home,
            text_sha256=receipt['text_sha256'],
            route=receipt['route'],
            cohort_since=cohort[0],
            cohort_target=cohort[1],
        )
        if found is True:
            return NativeResult('ok', value={'delivered': True})
        if found is False and storage.has_transcript(canonical, home) is True:
            return NativeResult('ok', value={'delivered': False})
        return NativeResult('unknown', 'Antigravity transcript evidence is incomplete or absent')

    def get_usage(self, session, executor=None) -> NativeResult:
        if not session.native_id:
            return NativeResult('unavailable', 'No Antigravity session ID recorded')
        reported = {}
        if session.session_file:
            try:
                facts = retry_missing(lambda: read(Path(session.session_file).parent / 'runtime.json'))
                mod = facts.get('usage')
                if isinstance(mod, dict) and session.launch_id and facts.get('launch_id') == session.launch_id:
                    reported = {key: mod[key] for key in ('context', 'cost_usd', 'rate_limits') if key in mod}
                    if 'reported_at' in mod:
                        reported['context_reported_at'] = mod['reported_at']
            except (OSError, ValueError, TypeError):
                pass

        home = storage.recorded_home(session)
        try:
            usage, meta = storage.measured_usage(session.native_id, home) if home else (None, {})
        except OSError as error:
            return NativeResult('error', f"Antigravity's transcript storage can't be read: {error}")

        context = dict(reported['context']) if isinstance(reported.get('context'), dict) else {}
        responded = storage.moment(meta.get('responded_at')) if meta else None
        reported_at = storage.moment(reported.get('context_reported_at'))
        if meta.get('context_tokens') is not None and (
            context.get('tokens') is None or (responded and (reported_at is None or reported_at < responded))
        ):
            context = {key: value for key, value in context.items() if key not in ('tokens', 'percent')}
            context['tokens'] = meta['context_tokens']
        if meta.get('responded_at'):
            context['responded_at'] = meta['responded_at']
        if context:
            reported['context'] = context

        if usage is None:
            if reported:
                return NativeResult('ok', 'Reported by Antigravity statusLine',
                                    {'native_session_id': session.native_id, **reported})
            return NativeResult('unavailable', 'Antigravity has not written a transcript for this session yet')
        return NativeResult('ok', 'Measured from the Antigravity session transcript',
                            {**asdict(usage), **reported})

    def queued_prompts(self, session, executor=None) -> NativeResult:
        """Read this launch's queued mv2 Stop-hook follow-ups without changing delivery or returning text."""
        bridge = self._bridge(session)
        if not bridge.ready:
            if bridge.final:
                return NativeResult(
                    'unavailable',
                    "This agent runs without mv2's Antigravity bridge (launch-only), so mv2 can't see its queued follow-ups",
                )
            return unavailable(bridge)
        if not session.session_file or not session.launch_id or not session.native_id:
            return NativeResult('unavailable', 'No Antigravity launch and conversation binding recorded')
        agent_dir = Path(session.session_file).parent
        observed_at = now()
        state, data, reason = agy_bridge.read_followup_queue(agent_dir, session.launch_id)
        if state == 'missing':
            return NativeResult('ok', value={
                'coverage': 'complete', 'reason': None, 'observed_at': observed_at, 'entries': [],
            })
        if state != 'ok' or data is None:
            return NativeResult('ok', value={
                'coverage': 'unknown', 'reason': reason or "Antigravity's follow-up queue can't be read",
                'observed_at': observed_at, 'entries': [],
            })
        entries = []
        seen = set()
        coverage = 'complete'
        note = None
        for item in data['entries']:
            if not isinstance(item, dict):
                coverage = 'unknown'
                note = note or "Antigravity's follow-up queue has an unreadable entry"
                continue
            if item.get('native_id') != session.native_id:
                continue
            item_state = item.get('state', 'queued')
            if item_state == 'handed_off':
                continue
            if item_state == 'agentapi_submitting':
                coverage = 'unknown'
                note = note or 'A queued follow-up is currently being submitted to Antigravity'
                continue
            if item_state != 'queued':
                coverage = 'unknown'
                note = note or "Antigravity's follow-up queue has an entry in an unknown state"
                continue
            message_id = item.get('id')
            if (not isinstance(message_id, str) or not message_id or '\0' in message_id
                    or len(message_id.encode('utf-8')) > 128):
                coverage = 'unknown'
                note = note or "Antigravity's follow-up queue has an invalid message ID"
                continue
            if message_id in seen:
                coverage = 'unknown'
                note = note or "Antigravity's follow-up queue has a duplicate message ID"
                continue
            seen.add(message_id)
            raw_since = item.get('queued_at')
            since = raw_since if storage.moment(raw_since) is not None else None
            entries.append({'message_id': message_id, 'mode': 'follow-up', 'since': since, 'native_id': None})
        return NativeResult('ok', value={
            'coverage': coverage, 'reason': note, 'observed_at': observed_at, 'entries': entries,
        })
