"""Antigravity CLI bridge runtime: statusLine, hooks, shell handoff, follow-up queue and receipts.

Runs both as an imported module (from AgyAdapter) and as a direct CLI helper invoked by
Antigravity's `statusLine` and `.agents/hooks.json` handlers:

    python bridge.py statusline
    python bridge.py hook SessionStart|PreInvocation|PostInvocation|Stop
"""
from collections.abc import Mapping
from datetime import datetime, timezone
import errno
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from multiverse.harnesses.agy import storage, transcripts
from multiverse.harnesses.base import PARENT_AGY
from multiverse.harnesses.types import TurnEnded, TurnStarted, normalize_session_id
from multiverse.store import lock, now, read, retry_missing, write


BRIDGE = 'agy-bridge-v2'  # hook-injected user messages; launches record it in native_mode['bridge']
LEGACY_BRIDGES = ('agy-bridge-v1',)  # Stop-hook and agentapi text delivery; such a launch needs a restart
LS_FILE = 'agy-ls.json'
FOLLOWUPS_FILE = 'agy-followups.json'
FOLLOWUPS_LOCK = '.agy-followups.lock'
STATUS_CACHE_FILE = 'agy-status-cache.json'
SESSION_STATE_FILE = 'agy-session-state.json'
RECEIPTS_DIR = 'agy-receipts'
MAX_RECORD_BYTES = 1 << 16
MAX_QUEUE_BYTES = 256 << 10
MAX_FOLLOWUPS = 128
MAX_HANDED_OFF_RETAINED = 64
MAX_POST_CONTINUATIONS = 15  # chained force_continue proven live on 1.5.17 up to 15
MAX_RECEIPTS_SCAN = 100_000
IDLE_FLUSH_GRACE_SECONDS = 5.0
TURN_ACTIVE_IDLE_CONFIRMATIONS = 2
TURN_ACTIVE_IDLE_GRACE_SECONDS = 1.5
TURN_ACTIVE_MAX_IDLE_SECONDS = 5.0
SUBMITTING_TIMEOUT_SECONDS = 20.0
WAKE_PING = storage.WAKE_PING
VALID_RECEIPT_ROUTES = frozenset(('agentapi', 'stop-hook', 'hook-user-input'))

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LOOPBACK_HOSTS = frozenset(('127.0.0.1', 'localhost', '::1'))
_PRE_CONNECT_ERROR = re.compile(
    r'^(?:Error:\s*)?(?:'
    r'ANTIGRAVITY_LS_ADDRESS is not set'
    r'|ANTIGRAVITY_CSRF_TOKEN is not set'
    r'|Error while dialing:'
    r'|rpc error:\s*code = (?:Unauthenticated|InvalidArgument|NotFound)\b'
    r'|rpc error:\s*code = Unavailable desc = (?:connection error:\s*desc = "?)?transport:\s*Error while dialing:'
    r')'
)
_RECEIPT_INDEX_FILE = '.index.json'
_RECEIPT_CACHE_MAX_DIRS = 16
# Process-local stat-verified receipt cache keyed by `(dir_dev, dir_ino)`:
# `{filename: (stat_sig, (native_id, route, text_sha256, submitted_at, launch_id, id))}`.
_RECEIPT_CACHE: dict[tuple[int, int], dict[str, tuple[tuple[int, ...], tuple[str, ...]]]] = {}


def agentapi_recipient(stdout: str) -> str | None:
    """Extract recipientId from `agy agentapi send-message` JSON output, or None."""
    text = (stdout or '').strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    response = data.get('response') if isinstance(data.get('response'), dict) else data
    sent = response.get('sendMessage') if isinstance(response.get('sendMessage'), dict) else None
    recipient = sent.get('recipientId') if isinstance(sent, dict) else None
    return recipient if isinstance(recipient, str) and recipient else None


def is_pre_connect_agentapi_error(stdout: str, stderr: str) -> bool:
    """True only when `agy agentapi send-message` output proves failure strictly before server acceptance."""
    raw_out = (stdout or '').strip()
    if raw_out:
        try:
            data = json.loads(raw_out)
        except ValueError:
            data = None
        if isinstance(data, dict):
            error_text = data.get('error')
            return isinstance(error_text, str) and bool(_PRE_CONNECT_ERROR.match(error_text.strip()))
    # Non-JSON CLI failure (e.g. stderr before JSON serialization): match anchored error lines only.
    source = (stderr or '').strip() or raw_out
    if not source:
        return False
    return any(_PRE_CONNECT_ERROR.match(line.strip()) for line in source.splitlines() if line.strip())


def validate_ls_address(address) -> str:
    """Verify `address` is a bare loopback `host:port` with no scheme, path or credentials."""
    if not isinstance(address, str):
        raise ValueError('Antigravity language server address must be a string')
    value = address.strip()
    if not value or any(ch.isspace() or ch in '/?#@\\' for ch in value) or '://' in value:
        raise ValueError('Antigravity language server address must be loopback host:port')
    try:
        parts = urlsplit(f'http://{value}')
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ValueError('Invalid Antigravity language server address') from error
    if (host is None or host.lower() not in _LOOPBACK_HOSTS or port is None
            or not (1 <= port <= 65535) or parts.path or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None):
        raise ValueError('Antigravity language server address must be a loopback host:port')
    canonical = f'[{host}]:{port}' if ':' in host else f'{host.lower()}:{port}'
    if canonical != value.lower():
        raise ValueError('Antigravity language server address must be canonical host:port')
    return canonical


def _resolve_loopback(address: str) -> tuple[list[str], int, str]:
    """Return verified literal loopback IP(s), port, and Host header for `address`.

    If `localhost` resolves to any non-loopback IP, refuse before connecting.
    """
    canonical = validate_ls_address(address)
    parts = urlsplit(f'http://{canonical}')
    host = parts.hostname
    port = parts.port
    if host == 'localhost':
        try:
            infos = socket.getaddrinfo('localhost', port, type=socket.SOCK_STREAM)
        except OSError as error:
            raise RuntimeError(f'Antigravity language server cannot resolve localhost: {error}') from None
        ips = []
        for _, _, _, _, sockaddr in infos:
            raw_ip = sockaddr[0]
            if not isinstance(raw_ip, str) or '%' in raw_ip:
                raise RuntimeError('Refusing ambiguous localhost resolution')
            try:
                parsed = ipaddress.ip_address(raw_ip)
            except ValueError:
                raise RuntimeError('Refusing invalid localhost address') from None
            if not parsed.is_loopback:
                raise RuntimeError(f'Refusing non-loopback address {raw_ip!r} for localhost')
            if str(parsed) not in ips:
                ips.append(str(parsed))
        if not ips:
            raise RuntimeError('Antigravity language server localhost resolved to no loopback addresses')
        return ips, port, canonical
    parsed = ipaddress.ip_address(host)
    if not parsed.is_loopback:
        raise ValueError('Antigravity language server address must be loopback')
    return [str(parsed)], port, canonical


def loopback_endpoints(address: str) -> list[str]:
    """Return verified literal numeric loopback `ip:port` / `[ip]:port` endpoints for `address`."""
    ips, port, _ = _resolve_loopback(address)
    return [f'[{ip}]:{port}' if ':' in ip else f'{ip}:{port}' for ip in ips]


def pin_loopback_endpoint(address: str, *, timeout: float = 2.0) -> str:
    """Resolve `address` to verified loopback IP(s) and return the first TCP-reachable numeric endpoint."""
    ips, port, _ = _resolve_loopback(address)
    last_error = None
    for ip in ips:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return f'[{ip}]:{port}' if ':' in ip else f'{ip}:{port}'
        except OSError as error:
            last_error = error
    raise RuntimeError(f'Antigravity language server unreachable: {last_error}') from None


def validate_csrf_token(token) -> str:
    """Verify the CSRF token is non-empty printable ASCII without whitespace."""
    if (not isinstance(token, str) or not token or len(token) > 512
            or not all(33 <= ord(ch) <= 126 for ch in token)):
        raise ValueError('Invalid Antigravity CSRF token')
    return token


def _fsync_dir(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
            raise


def _write_private_json(path: Path, value: dict, *, max_bytes: int | None = None) -> None:
    """Atomically write a 0600 JSON file without following symlinks or writing through hard links."""
    path = Path(path)
    encoded = (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError(f'{path.name} exceeds {max_bytes} byte limit')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_fd = os.open(path.parent, _DIR_FLAGS)
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
                    or existing.st_nlink > 1):
                raise ValueError(f'Refusing to overwrite linked or non-regular file: {path.name}')
        temp = f'.write-{uuid4().hex}'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        replaced = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as out:
                fd = -1
                out.write(encoded)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            replaced = True
            _fsync_dir(parent_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            if not replaced:
                try:
                    os.unlink(temp, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(parent_fd)


def _open_private_regular(path: Path, *, require_private_mode: bool = True):
    """Open `path` anchored on its non-symlink parent with O_NOFOLLOW, checking regular/owner/0600 mode."""
    path = Path(path)
    parent_fd = os.open(path.parent, _DIR_FLAGS)
    try:
        fd = retry_missing(
            lambda: os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent_fd),
            path=path,
        )
    finally:
        os.close(parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, 'not a regular file', str(path))
        if info.st_uid != os.getuid():
            raise OSError(errno.EACCES, 'file is not owned by this user', str(path))
        if require_private_mode and (stat.S_IMODE(info.st_mode) & 0o077) != 0:
            raise OSError(errno.EACCES, 'private file permissions are too broad', str(path))
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def _read_private_json(path: Path, limit: int = MAX_RECORD_BYTES, *, require_private_mode: bool = True):
    """Return dict, None when absent, or False when unreadable/symlink/bad-mode/oversized/malformed."""
    try:
        with _open_private_regular(path, require_private_mode=require_private_mode) as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    if len(data) > limit:
        return False
    try:
        parsed = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return False
    return parsed if isinstance(parsed, dict) else False


def ls_path(agent_dir, launch_id: str) -> Path:
    return Path(agent_dir) / 'launches' / normalize_session_id(launch_id) / LS_FILE


def read_ls_info(agent_dir, launch_id: str) -> dict | None:
    """Read and validate this launch's private 0600 loopback language-server connection info, or None."""
    try:
        canonical = normalize_session_id(launch_id)
    except ValueError:
        return None
    record = _read_private_json(Path(agent_dir) / 'launches' / canonical / LS_FILE, require_private_mode=True)
    if not isinstance(record, dict):
        return None
    if record.get('schema') != 1 or record.get('launch_id') != canonical:
        return None
    try:
        address = validate_ls_address(record.get('ls_address'))
        token = validate_csrf_token(record.get('csrf_token'))
    except ValueError:
        return None
    conversation_id = record.get('conversation_id')
    if conversation_id is not None:
        try:
            conversation_id = normalize_session_id(conversation_id)
        except ValueError:
            conversation_id = None
    return {
        'schema': 1,
        'launch_id': canonical,
        'ls_address': address,
        'csrf_token': token,
        'conversation_id': conversation_id,
        'registered_at': record.get('registered_at'),
    }


def _ls_record(canonical_launch: str, address: str, token: str, conversation_id: str | None) -> dict:
    return {
        'schema': 1,
        'launch_id': canonical_launch,
        'ls_address': address,
        'csrf_token': token,
        'conversation_id': conversation_id,
        'registered_at': now(),
    }


def write_ls_info(agent_dir, launch_id: str, ls_address: str, csrf_token: str,
                  conversation_id: str | None = None) -> Path:
    canonical_launch = normalize_session_id(launch_id)
    address = validate_ls_address(ls_address)
    token = validate_csrf_token(csrf_token)
    canonical_conv = normalize_session_id(conversation_id) if conversation_id else None
    path = Path(agent_dir) / 'launches' / canonical_launch / LS_FILE
    _write_private_json(
        path,
        _ls_record(canonical_launch, address, token, canonical_conv),
    )
    return path


def clear_ls_info(launch_dir) -> None:
    try:
        (Path(launch_dir) / LS_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def prepare_shell_callback(agent_dir: Path, launch_id: str, current_native_id: str | None,
                           current_bridge: str | None, env: dict, *,
                           agent_name: str | None = None) -> tuple[str | None, str, dict[str, str]] | None:
    """Validate tool-shell env and prepare `(native_id, bridge_id, launch_files)` for core's shell_callback hook.

    Returns None when credentials are absent/invalid, the conversation conflicts with the bound session, or this
    launch's exact credentials and bridge state are already recorded.
    """
    raw_address = env.get('ANTIGRAVITY_LS_ADDRESS')
    raw_token = env.get('ANTIGRAVITY_CSRF_TOKEN')
    if not raw_address or not raw_token or not launch_id:
        return None
    try:
        ls_address = validate_ls_address(raw_address)
        csrf_token = validate_csrf_token(raw_token)
        canonical_launch = normalize_session_id(launch_id)
    except ValueError:
        return None
    raw_conv = env.get('ANTIGRAVITY_CONVERSATION_ID')
    conversation_id = None
    if isinstance(raw_conv, str) and raw_conv.strip():
        try:
            conversation_id = normalize_session_id(raw_conv.strip())
        except ValueError:
            conversation_id = None
    if conversation_id is not None and current_native_id is not None and current_native_id != conversation_id:
        # Never rebind an already-bound conversation from an in-session tool shell.
        return None

    existing = read_ls_info(agent_dir, canonical_launch)
    already_synced = (
        existing is not None
        and existing['ls_address'] == ls_address
        and existing['csrf_token'] == csrf_token
        and existing['conversation_id'] == conversation_id
        and current_bridge == BRIDGE
        and (conversation_id is None or current_native_id == conversation_id)
    )
    if already_synced:
        return None

    payload = _ls_record(canonical_launch, ls_address, csrf_token, conversation_id)
    content = json.dumps(payload, indent=2, ensure_ascii=False) + '\n'
    return conversation_id, BRIDGE, {LS_FILE: content}


def _followups_paths(agent_dir, launch_id: str) -> tuple[Path, Path]:
    launch = Path(agent_dir) / 'launches' / normalize_session_id(launch_id)
    return launch / FOLLOWUPS_FILE, launch / FOLLOWUPS_LOCK


def _idle_flushed_active(idle_flushed: bool, idle_flushed_at: str | None) -> bool:
    """True while `idle_flushed` is still within its bounded grace period (missing/corrupt stamps expire)."""
    if not idle_flushed or not isinstance(idle_flushed_at, str) or not idle_flushed_at:
        return False
    stamped = storage.moment(idle_flushed_at)
    if stamped is None:
        return False
    age = (datetime.now(timezone.utc) - stamped).total_seconds()
    return age < IDLE_FLUSH_GRACE_SECONDS


def _opt_timestamp(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonneg_int(value, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _opt_str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _load_followups_record_locked(path: Path) -> dict:
    """Load full follow-up queue state record; raises ValueError on damaged/oversized files."""
    try:
        with _open_private_regular(path, require_private_mode=True) as handle:
            data = handle.read(MAX_QUEUE_BYTES + 1)
    except FileNotFoundError:
        return {
            'entries': [],
            'idle_flushed': False,
            'idle_flushed_at': None,
            'turn_seq': 0,
            'turn_active': False,
            'turn_active_at': None,
            'idle_after_turn': 0,
            'idle_after_turn_at': None,
            'turn_open': False,
            'turn_id': None,
            'pending_turn_attempt': None,
            'pending_turn_source': None,
            'pending_idle_attempt': None,
            'pending_idle_attempt_at': None,
            'last_init_steps': None,
            'post_continue_streak': 0,
            'last_post_end_step': None,
        }
    except OSError as error:
        detail = error.strerror or str(error)
        raise ValueError(f'Antigravity follow-up queue cannot be read safely: {detail}') from None
    if len(data) > MAX_QUEUE_BYTES:
        raise ValueError(f'Antigravity follow-up queue exceeds {MAX_QUEUE_BYTES} byte limit')
    try:
        record = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f'Antigravity follow-up queue is damaged: {error}') from None
    if (not isinstance(record, dict) or record.get('schema') != 1
            or not isinstance(record.get('entries'), list)
            or any(not isinstance(entry, dict) for entry in record['entries'])):
        raise ValueError('Antigravity follow-up queue has an invalid schema')
    raw_source = record.get('pending_turn_source')
    raw_last_steps = record.get('last_init_steps')
    last_steps = (raw_last_steps if type(raw_last_steps) is int and raw_last_steps >= 1 else None)
    raw_last_post_step = record.get('last_post_end_step')
    last_post_step = (raw_last_post_step if type(raw_last_post_step) is int and raw_last_post_step >= 0 else None)
    return {
        'entries': list(record['entries']),
        'idle_flushed': bool(record.get('idle_flushed')),
        'idle_flushed_at': _opt_timestamp(record.get('idle_flushed_at')),
        'turn_seq': _nonneg_int(record.get('turn_seq'), 0),
        'turn_active': bool(record.get('turn_active')),
        'turn_active_at': _opt_timestamp(record.get('turn_active_at')),
        'idle_after_turn': _nonneg_int(record.get('idle_after_turn'), 0),
        'idle_after_turn_at': _opt_timestamp(record.get('idle_after_turn_at')),
        'turn_open': bool(record.get('turn_open')),
        'turn_id': _opt_str(record.get('turn_id')),
        'pending_turn_attempt': _opt_str(record.get('pending_turn_attempt')),
        'pending_turn_source': raw_source if raw_source in ('person', 'harness') else None,
        'pending_idle_attempt': _opt_str(record.get('pending_idle_attempt')),
        'pending_idle_attempt_at': _opt_timestamp(record.get('pending_idle_attempt_at')),
        'last_init_steps': last_steps,
        'post_continue_streak': _nonneg_int(record.get('post_continue_streak'), 0),
        'last_post_end_step': last_post_step,
    }


def _load_followups_state_locked(path: Path) -> tuple[list[dict], bool, str | None, int, bool]:
    """Load `(entries, idle_flushed, idle_flushed_at, turn_seq, turn_active)`."""
    rec = _load_followups_record_locked(path)
    return rec['entries'], rec['idle_flushed'], rec['idle_flushed_at'], rec['turn_seq'], rec['turn_active']


def _load_followups_doc_locked(path: Path) -> tuple[list[dict], bool]:
    """Load `(entries, idle_flushed)`; returns `([], False)` only when absent and raises on damaged files."""
    rec = _load_followups_record_locked(path)
    return rec['entries'], rec['idle_flushed']


def _load_followups_locked(path: Path) -> list[dict]:
    """Load the follow-up queue; returns [] only when absent and raises ValueError on damaged/oversized files."""
    entries, _ = _load_followups_doc_locked(path)
    return entries


def _save_followups_locked(path: Path, launch_id: str, entries: list[dict], *,
                           idle_flushed: bool = False, idle_flushed_at: str | None = None,
                           turn_seq: int = 0, turn_active: bool = False,
                           turn_active_at: str | None = None,
                           idle_after_turn: int = 0, idle_after_turn_at: str | None = None,
                           turn_open: bool = False, turn_id: str | None = None,
                           pending_turn_attempt: str | None = None,
                           pending_turn_source: str | None = None,
                           pending_idle_attempt: str | None = None,
                           pending_idle_attempt_at: str | None = None,
                           last_init_steps: int | None = None,
                           post_continue_streak: int = 0,
                           last_post_end_step: int | None = None) -> None:
    canonical_launch = normalize_session_id(launch_id)
    # Never keep message text on already handed-off entries so delivered prompts cannot exhaust the queue budget.
    cleaned = []
    for entry in entries:
        if entry.get('state') == 'handed_off' and 'text' in entry:
            entry = {k: v for k, v in entry.items() if k != 'text'}
        cleaned.append(entry)
    entries = cleaned
    # Trim excess historical handed_off entries while keeping all queued/submitting entries intact.
    handed_off = [idx for idx, entry in enumerate(entries) if entry.get('state') == 'handed_off']
    if len(handed_off) > MAX_HANDED_OFF_RETAINED:
        drop = set(handed_off[:-MAX_HANDED_OFF_RETAINED])
        entries = [entry for idx, entry in enumerate(entries) if idx not in drop]
    flushed = bool(idle_flushed)
    stamped_at = (idle_flushed_at or now()) if flushed else None
    active = bool(turn_active)
    active_at = (turn_active_at or now()) if active else None
    idle_count = int(idle_after_turn) if active else 0
    idle_at = (idle_after_turn_at or now()) if (active and idle_count > 0) else None
    has_idle_attempt = isinstance(pending_idle_attempt, str) and bool(pending_idle_attempt)
    idle_attempt_at = (pending_idle_attempt_at or stamped_at or now()) if has_idle_attempt else None
    payload = {
        'schema': 1,
        'launch_id': canonical_launch,
        'idle_flushed': flushed,
        'idle_flushed_at': stamped_at,
        'turn_seq': int(turn_seq),
        'turn_active': active,
        'turn_active_at': active_at,
        'idle_after_turn': idle_count,
        'idle_after_turn_at': idle_at,
        'turn_open': bool(turn_open),
        'turn_id': turn_id if turn_open and isinstance(turn_id, str) and turn_id else None,
        'pending_turn_attempt': pending_turn_attempt if isinstance(pending_turn_attempt, str) and pending_turn_attempt else None,
        'pending_turn_source': pending_turn_source if pending_turn_source in ('person', 'harness') else None,
        'pending_idle_attempt': pending_idle_attempt if has_idle_attempt else None,
        'pending_idle_attempt_at': idle_attempt_at,
        'last_init_steps': last_init_steps if type(last_init_steps) is int and last_init_steps >= 1 else None,
        'post_continue_streak': max(0, int(post_continue_streak)),
        'last_post_end_step': last_post_end_step if type(last_post_end_step) is int and last_post_end_step >= 0 else None,
        'entries': entries,
    }
    _write_private_json(path, payload, max_bytes=MAX_QUEUE_BYTES)


def _save_record_locked(path: Path, launch_id: str, rec: dict) -> None:
    _save_followups_locked(
        path, launch_id, rec['entries'],
        idle_flushed=rec['idle_flushed'],
        idle_flushed_at=rec['idle_flushed_at'],
        turn_seq=rec['turn_seq'],
        turn_active=rec['turn_active'],
        turn_active_at=rec['turn_active_at'],
        idle_after_turn=rec['idle_after_turn'],
        idle_after_turn_at=rec['idle_after_turn_at'],
        turn_open=rec.get('turn_open', False),
        turn_id=rec.get('turn_id'),
        pending_turn_attempt=rec.get('pending_turn_attempt'),
        pending_turn_source=rec.get('pending_turn_source'),
        pending_idle_attempt=rec.get('pending_idle_attempt'),
        pending_idle_attempt_at=rec.get('pending_idle_attempt_at'),
        last_init_steps=rec.get('last_init_steps'),
        post_continue_streak=rec.get('post_continue_streak', 0),
        last_post_end_step=rec.get('last_post_end_step'),
    )


def _turn_end_edges(turn_id: str | None, state: str) -> tuple[TurnEnded, ...]:
    if not isinstance(turn_id, str) or not turn_id:
        return ()
    end_ref = f'end:{turn_id}' if turn_id.startswith(('step-', 'init:', 'post:')) else None
    return (TurnEnded(turn=turn_id, kind='prompt', ref=end_ref, state=state),)


def _universe_root(agent_dir) -> Path | None:
    root = Path(agent_dir).parent.parent.parent
    return root if (root / 'universe.json').exists() else None


def _reap_followups_locked(agent_dir, rec: dict, *, live_facts: dict | None = None,
                           observed_idle: bool = False, conv_id: str | None = None,
                           pause_on_idle_reap: bool = True) -> tuple[bool, bool, str | None]:
    """Re-check and expire bounded delivery state against live evidence so no missed Stop hook strands or auto-flushes.

    Returns `(changed, cancelled_by_idle, closed_turn_id)` where `cancelled_by_idle` is True when an active turn
    returned to unbroken `idle` without `Stop` (as Antigravity 1.5.16 does on `Esc`), pausing queued follow-ups with
    `paused_reason='cancel'` instead of flushing them on `statusLine` renders or keystrokes, and `closed_turn_id`
    names any open turn closed by that cancellation.

    Handles:
    1. Expired `idle_flushed` (> IDLE_FLUSH_GRACE_SECONDS or missing timestamp).
    2. Stale `agentapi_submitting` claims (> SUBMITTING_TIMEOUT_SECONDS or missing timestamp): reverts to `queued`
       when the receipt is still `stop-hook` (zero agentapi I/O occurred), or transitions to `handed_off` when the
       receipt already moved to `agentapi` (preserving at-most-once while unblocking later follow-ups).
    3. Missing-`Stop` return to idle (`Esc` cancel or crashed `Stop`): after TURN_ACTIVE_IDLE_CONFIRMATIONS
       consecutive idle statusLine renders or TURN_ACTIVE_IDLE_GRACE_SECONDS of unbroken idle (or on live send
       re-check once runtime.json has observed `idle` past TURN_ACTIVE_MAX_IDLE_SECONDS after `turn_active_at`),
       clears `turn_active` and pauses queued entries with `paused_reason='cancel'` when `pause_on_idle_reap` is True.
       Paused entries (`cancel`, `error`, `subagent`) are never auto-unpaused by idle statusLine renders.
    """
    changed = False
    cancelled_by_idle = False
    closed_turn_id = None
    now_dt = datetime.now(timezone.utc)

    # 1. Self-expiring idle_flushed and pending_idle_attempt gates.
    if rec['idle_flushed'] and not _idle_flushed_active(rec['idle_flushed'], rec['idle_flushed_at']):
        rec['idle_flushed'] = False
        rec['idle_flushed_at'] = None
        changed = True
    if rec.get('pending_idle_attempt') and not _idle_flushed_active(True, rec.get('pending_idle_attempt_at')):
        rec['pending_idle_attempt'] = None
        rec['pending_idle_attempt_at'] = None
        changed = True

    # 2. Stale wake_pending / legacy agentapi_submitting claim reaper (e.g., SIGKILL / crash mid-flush).
    for entry in rec['entries']:
        entry_state = entry.get('state')
        if entry_state not in ('wake_pending', 'agentapi_submitting'):
            continue
        claimed_dt = storage.moment(entry.get('claimed_at'))
        if claimed_dt is not None and (now_dt - claimed_dt).total_seconds() < SUBMITTING_TIMEOUT_SECONDS:
            continue
        if entry_state == 'wake_pending':
            # PreInvocation never handed off this entry (only WAKE_PING was sent over agentapi),
            # so returning it to queued can never double-deliver its text.
            entry['state'] = 'queued'
            entry.pop('claimed_at', None)
            entry.pop('claim_token', None)
            entry.pop('wake_sent_at', None)
            if rec.get('pending_idle_attempt') == entry.get('id'):
                rec['idle_flushed'] = False
                rec['idle_flushed_at'] = None
                rec['pending_idle_attempt'] = None
                rec['pending_idle_attempt_at'] = None
            changed = True
            continue
        entry_id = entry.get('id')
        receipt = None
        if isinstance(entry_id, str):
            try:
                receipt = read_receipt(agent_dir, entry_id)
            except ValueError:
                receipt = None
        if isinstance(receipt, dict) and receipt.get('route') == 'stop-hook':
            # Crash happened before switching the receipt to agentapi: no native send began.
            entry['state'] = 'queued'
            entry.pop('claimed_at', None)
            entry.pop('claim_token', None)
        else:
            # agentapi submission may have started: never auto-resend, unblock subsequent entries.
            entry['state'] = 'handed_off'
            entry['handed_off_at'] = now()
            entry.pop('text', None)
            entry.pop('claimed_at', None)
            entry.pop('claim_token', None)
        changed = True

    # 3. Missing-Stop return-to-idle detector (agy 1.5.16 skips Stop on Esc during tool calls and text generation).
    if rec['turn_active']:
        clear_turn = False
        if observed_idle:
            rec['idle_after_turn'] = rec['idle_after_turn'] + 1
            if rec['idle_after_turn_at'] is None:
                rec['idle_after_turn_at'] = now()
            changed = True
            first_idle = storage.moment(rec['idle_after_turn_at'])
            idle_elapsed = (now_dt - first_idle).total_seconds() if first_idle is not None else TURN_ACTIVE_IDLE_GRACE_SECONDS
            if (rec['idle_after_turn'] >= TURN_ACTIVE_IDLE_CONFIRMATIONS
                    or idle_elapsed >= TURN_ACTIVE_IDLE_GRACE_SECONDS):
                clear_turn = True
        elif isinstance(live_facts, dict) and live_facts.get('status') == 'idle':
            started = storage.moment(rec['turn_active_at'])
            idle_since = (
                storage.moment(live_facts.get('status_changed_at'))
                or storage.moment(live_facts.get('observed_at'))
            )
            if started is None:
                clear_turn = True
            elif (now_dt - started).total_seconds() >= TURN_ACTIVE_MAX_IDLE_SECONDS and (
                    idle_since is None
                    or idle_since < started
                    or (now_dt - idle_since).total_seconds() >= TURN_ACTIVE_IDLE_GRACE_SECONDS):
                clear_turn = True
        if clear_turn:
            rec['turn_active'] = False
            rec['turn_active_at'] = None
            rec['idle_after_turn'] = 0
            rec['idle_after_turn_at'] = None
            rec['idle_flushed'] = False
            rec['idle_flushed_at'] = None
            rec['pending_idle_attempt'] = None
            rec['turn_seq'] = rec['turn_seq'] + 1
            if pause_on_idle_reap:
                if rec.get('turn_open'):
                    closed_turn_id = rec.get('turn_id')
                    rec['turn_open'] = False
                    rec['turn_id'] = None
                for entry in rec['entries']:
                    if (entry.get('state', 'queued') == 'queued'
                            and (conv_id is None or entry.get('native_id') == conv_id)):
                        entry['paused'] = True
                        entry['paused_reason'] = 'cancel'
                cancelled_by_idle = True
            changed = True

    return changed, cancelled_by_idle, closed_turn_id


def _entry_mode(entry: dict) -> str:
    return 'steer' if entry.get('mode') == 'steer' else 'follow-up'


def enqueue_message(agent_dir, launch_id: str, message_id: str, native_id: str, text: str, *,
                    mode: str = 'follow-up') -> None:
    """Append one message (`steer` or `follow-up`) to this launch's hook delivery queue."""
    if mode not in ('steer', 'follow-up'):
        raise ValueError(f'Unknown Antigravity queue mode: {mode}')
    queue_path, lock_path = _followups_paths(agent_dir, launch_id)
    canonical_msg = normalize_session_id(message_id)
    canonical_conv = normalize_session_id(native_id)
    with lock(lock_path):
        rec = _load_followups_record_locked(queue_path)
        entries = rec['entries']
        if any(entry.get('id') == canonical_msg and entry.get('native_id') == canonical_conv
               and entry.get('state', 'queued') == 'queued' for entry in entries):
            return
        entries = [entry for entry in entries if entry.get('id') != canonical_msg]
        queued_count = sum(1 for entry in entries if entry.get('state', 'queued') in ('queued', 'wake_pending'))
        if queued_count >= MAX_FOLLOWUPS:
            raise ValueError('Antigravity follow-up queue is full')
        entries.append({
            'id': canonical_msg,
            'native_id': canonical_conv,
            'mode': mode,
            'text': text,
            'text_sha256': storage.message_hash(text),
            'state': 'queued',
            'queued_at': now(),
        })
        rec['entries'] = entries
        _save_record_locked(queue_path, launch_id, rec)


def enqueue_followup(agent_dir, launch_id: str, message_id: str, native_id: str, text: str) -> None:
    """Append one follow-up message to this launch's hook delivery queue."""
    enqueue_message(agent_dir, launch_id, message_id, native_id, text, mode='follow-up')


def release_wake_claim(agent_dir, launch_id: str, message_id: str, *,
                       claim_token: str, remove_entry: bool = False,
                       drop_receipt: bool = False, reset_idle_flush: bool = True) -> bool:
    """Atomically revert or remove an unhanded `wake_pending` claim matching `claim_token`.

    Returns True when released/removed, False if `PreInvocation` already handed the entry off.
    """
    queue_path, lock_path = _followups_paths(agent_dir, launch_id)
    canonical_msg = normalize_session_id(message_id)
    with lock(lock_path):
        rec = _load_followups_record_locked(queue_path)
        for idx, entry in enumerate(rec['entries']):
            if entry.get('id') == canonical_msg:
                if entry.get('state') != 'wake_pending' or entry.get('claim_token') != claim_token:
                    return False
                if remove_entry:
                    rec['entries'].pop(idx)
                    if drop_receipt:
                        remove_receipt(agent_dir, canonical_msg)
                else:
                    entry['state'] = 'queued'
                    entry.pop('claimed_at', None)
                    entry.pop('claim_token', None)
                    entry.pop('wake_sent_at', None)
                if reset_idle_flush:
                    rec['idle_flushed'] = False
                    rec['idle_flushed_at'] = None
                    if rec.get('pending_idle_attempt') == canonical_msg:
                        rec['pending_idle_attempt'] = None
                        rec['pending_idle_attempt_at'] = None
                _save_record_locked(queue_path, launch_id, rec)
                return True
    return False


def mark_wake_sent(agent_dir, launch_id: str, message_id: str, *, claim_token: str) -> str:
    """Stamp `wake_sent_at` on a live `wake_pending` claim; returns `'wake_pending'`, `'handed_off'`, or `'other'`."""
    queue_path, lock_path = _followups_paths(agent_dir, launch_id)
    canonical_msg = normalize_session_id(message_id)
    with lock(lock_path):
        rec = _load_followups_record_locked(queue_path)
        for entry in rec['entries']:
            if entry.get('id') == canonical_msg:
                state = entry.get('state', 'queued')
                if state == 'handed_off':
                    return 'handed_off'
                if state == 'wake_pending' and entry.get('claim_token') == claim_token:
                    entry['wake_sent_at'] = now()
                    _save_record_locked(queue_path, launch_id, rec)
                    return 'wake_pending'
                return 'other'
    return 'other'


def remove_followup(agent_dir, launch_id: str, message_id: str, *,
                    reset_idle_flush: bool = False, claim_token: str | None = None) -> bool:
    """Remove a specific follow-up entry (returns True if removed, False if absent)."""
    queue_path, lock_path = _followups_paths(agent_dir, launch_id)
    canonical_msg = normalize_session_id(message_id)
    with lock(lock_path):
        rec = _load_followups_record_locked(queue_path)
        if claim_token is not None:
            remaining = [
                entry for entry in rec['entries']
                if not (entry.get('id') == canonical_msg and entry.get('claim_token') == claim_token)
            ]
        else:
            remaining = [entry for entry in rec['entries'] if entry.get('id') != canonical_msg]
        if len(remaining) == len(rec['entries']):
            if reset_idle_flush and rec['idle_flushed'] and claim_token is None:
                rec['idle_flushed'] = False
                rec['idle_flushed_at'] = None
                _save_record_locked(queue_path, launch_id, rec)
            return False
        rec['entries'] = remaining
        if reset_idle_flush:
            rec['idle_flushed'] = False
            rec['idle_flushed_at'] = None
        _save_record_locked(queue_path, launch_id, rec)
        return True


def _clear_idle_flushed(agent_dir, launch_id: str) -> None:
    """Clear idle_flushed and reset any idle-after-turn streak when the session is actively working/waiting."""
    try:
        queue_path, lock_path = _followups_paths(agent_dir, launch_id)
        if not queue_path.exists():
            return
        with lock(lock_path, create_parent=False):
            rec = _load_followups_record_locked(queue_path)
            if rec['idle_flushed'] or rec['idle_after_turn'] > 0 or rec['idle_after_turn_at'] is not None:
                rec['idle_flushed'] = False
                rec['idle_flushed_at'] = None
                rec['idle_after_turn'] = 0
                rec['idle_after_turn_at'] = None
                _save_record_locked(queue_path, launch_id, rec)
    except (OSError, ValueError):
        pass


def _fifo_head_queued(entries: list[dict], canonical_conv: str, *, mode: str | None = None) -> dict | None:
    for entry in entries:
        if (entry.get('state', 'queued') == 'queued'
                and entry.get('native_id') == canonical_conv
                and isinstance(entry.get('text'), str)
                and (mode is None or _entry_mode(entry) == mode)):
            return entry
    return None


def _hook_delivery_live(facts: dict, props: dict, canonical_launch: str, canonical_conv: str | None) -> bool:
    """True while this launch may hand message text to agy: still running, not ending, bound to `canonical_conv`,
    and made for hook-injected user messages (an older launch's hooks.json and agy may not inject them)."""
    return (isinstance(facts, dict) and isinstance(props, dict)
            and canonical_conv is not None
            and facts.get('launch_id') == canonical_launch
            and facts.get('state') in ('starting', 'running')
            and not facts.get('ending_at')
            and not facts.get('cleanup_retired')
            and not facts.get('binding_conflict')
            and facts.get('native_bridge') == BRIDGE
            and props.get('native_session_id') == canonical_conv)


def _wake_in_flight(entries: list[dict], canonical_conv: str) -> bool:
    """True while a wake for `canonical_conv` (or a legacy agentapi submission) waits for its PreInvocation."""
    return any(entry.get('state') in ('wake_pending', 'agentapi_submitting')
               and entry.get('native_id') == canonical_conv for entry in entries)


def _next_queued_entry(entries: list[dict], canonical_conv: str) -> dict | None:
    """Return the next eligible queued head for `canonical_conv`: unpaused FIFO `steer` first, else unpaused FIFO `follow-up`."""
    steer_head = _fifo_head_queued(entries, canonical_conv, mode='steer')
    if steer_head is not None:
        return None if steer_head.get('paused') is True else steer_head
    followup_head = _fifo_head_queued(entries, canonical_conv, mode='follow-up')
    if followup_head is not None:
        return None if followup_head.get('paused') is True else followup_head
    return None


def claim_if_idle_head(agent_dir, launch_id: str, message_id: str, native_id: str, *,
                       claim_token: str | None = None, mode: str = 'follow-up') -> str:
    """Atomically recheck idle state under the queue lock and claim `message_id` as `wake_pending` if it is the next head.

    Returns `'claimed'`, `'queued'` (stay queued for hooks/pause/FIFO), `'handed_off'`, `'submitting'`, or `'missing'`.
    """
    queue_path, lock_path = _followups_paths(agent_dir, launch_id)
    canonical_launch = normalize_session_id(launch_id)
    canonical_msg = normalize_session_id(message_id)
    canonical_conv = normalize_session_id(native_id)
    token = claim_token or str(uuid4())
    with lock(lock_path):
        try:
            current_facts = retry_missing(lambda: read(Path(agent_dir) / 'runtime.json'))
            current_props = retry_missing(lambda: read(Path(agent_dir) / 'properties.json'))
        except (OSError, ValueError):
            return 'queued'
        rec = _load_followups_record_locked(queue_path)
        reaped, cancelled, closed_turn = _reap_followups_locked(
            agent_dir, rec, live_facts=current_facts, conv_id=canonical_conv,
        )
        if cancelled:
            # This send is a new idle send: it resumes what the reaper just paused, as resume_paused_for_new_send does.
            _resume_paused_locked(rec, canonical_conv, mode)
        entries = rec['entries']
        target = None
        for entry in entries:
            if entry.get('id') == canonical_msg:
                target = entry
                break
        if target is None:
            if reaped:
                _save_record_locked(queue_path, canonical_launch, rec)
            if cancelled:
                _sync_runtime_queue_state_locked(
                    agent_dir, canonical_launch, canonical_conv, rec['entries'],
                    root=_universe_root(agent_dir), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
            return 'missing'
        state = target.get('state', 'queued')
        if state == 'handed_off':
            if reaped:
                _save_record_locked(queue_path, canonical_launch, rec)
            if cancelled:
                _sync_runtime_queue_state_locked(
                    agent_dir, canonical_launch, canonical_conv, rec['entries'],
                    root=_universe_root(agent_dir), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
            return 'handed_off'
        if state in ('wake_pending', 'agentapi_submitting'):
            if reaped:
                _save_record_locked(queue_path, canonical_launch, rec)
            if cancelled:
                _sync_runtime_queue_state_locked(
                    agent_dir, canonical_launch, canonical_conv, rec['entries'],
                    root=_universe_root(agent_dir), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
            return 'submitting'
        if state != 'queued':
            if reaped:
                _save_record_locked(queue_path, canonical_launch, rec)
            if cancelled:
                _sync_runtime_queue_state_locked(
                    agent_dir, canonical_launch, canonical_conv, rec['entries'],
                    root=_universe_root(agent_dir), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
            return 'missing'
        if (current_facts.get('status') != 'idle'
                or current_facts.get('launch_id') != canonical_launch
                or current_facts.get('state') not in ('starting', 'running')
                or current_facts.get('ending_at')
                or current_facts.get('cleanup_retired')
                or current_facts.get('binding_conflict')
                or current_props.get('native_session_id') != canonical_conv):
            if reaped:
                _save_record_locked(queue_path, canonical_launch, rec)
            if cancelled:
                _sync_runtime_queue_state_locked(
                    agent_dir, canonical_launch, canonical_conv, rec['entries'],
                    root=_universe_root(agent_dir), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
            return 'queued'
        if (rec['turn_active']
                or _idle_flushed_active(rec['idle_flushed'], rec['idle_flushed_at'])
                or _wake_in_flight(entries, canonical_conv)):
            if reaped:
                _save_record_locked(queue_path, canonical_launch, rec)
            if cancelled:
                _sync_runtime_queue_state_locked(
                    agent_dir, canonical_launch, canonical_conv, rec['entries'],
                    root=_universe_root(agent_dir), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
            return 'queued'
        head = _next_queued_entry(entries, canonical_conv)
        if head is None or head.get('id') != canonical_msg:
            if reaped:
                _save_record_locked(queue_path, canonical_launch, rec)
            if cancelled:
                _sync_runtime_queue_state_locked(
                    agent_dir, canonical_launch, canonical_conv, rec['entries'],
                    root=_universe_root(agent_dir), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
            return 'queued'
        stamped = now()
        target['state'] = 'wake_pending'
        target['claimed_at'] = stamped
        target['claim_token'] = token
        rec['idle_flushed'] = True
        rec['idle_flushed_at'] = stamped
        rec['pending_idle_attempt'] = canonical_msg
        rec['pending_idle_attempt_at'] = stamped
        rec['turn_active'] = False
        rec['turn_active_at'] = None
        rec['idle_after_turn'] = 0
        rec['idle_after_turn_at'] = None
        _save_record_locked(queue_path, canonical_launch, rec)
        return 'claimed'


def _sync_runtime_queue_state_locked(agent_dir, launch_id: str, native_id: str | None, entries: list[dict], *,
                                     clear_stale_launch: bool = False,
                                     root: Path | None = None, turn_edges: tuple = ()) -> None:
    """Persist or clear `runtime.json['queue_state']` (and optional `turn_edges`) fenced by launch/conversation."""
    try:
        from multiverse.universe import agents
        canonical_launch = normalize_session_id(launch_id)
        canonical_conv = normalize_session_id(native_id) if native_id else None
        paused = [
            entry for entry in entries
            if entry.get('state', 'queued') == 'queued'
            and entry.get('paused') is True
            and entry.get('paused_reason') in ('cancel', 'error')
            and canonical_conv is not None
            and entry.get('native_id') == canonical_conv
        ]
        paused_ids = [entry['id'] for entry in paused if isinstance(entry.get('id'), str) and entry['id']]
        desired = (
            {
                'native_session_id': canonical_conv,
                'launch_id': canonical_launch,
                'state': 'paused',
                'reason': paused[0]['paused_reason'],
                'paused_count': len(paused),
                'paused_message_ids': paused_ids,
            }
            if paused else None
        )

        def _update(facts):
            if (facts.get('launch_id') != canonical_launch
                    or facts.get('state') not in ('starting', 'running')
                    or facts.get('ending_at')
                    or facts.get('cleanup_retired')
                    or facts.get('binding_conflict')):
                return None
            try:
                bound_conv = retry_missing(lambda: read(Path(agent_dir) / 'properties.json')).get('native_session_id')
            except (OSError, ValueError):
                return None
            current = facts.get('queue_state')
            if desired is not None:
                # Fence publication under the runtime commit lock: never overwrite after a rebind.
                if canonical_conv != bound_conv:
                    return None
                if current == desired:
                    return None
                return {'queue_state': desired}
            if current is None:
                return None
            if isinstance(current, dict):
                cur_launch = current.get('launch_id')
                cur_conv = current.get('native_session_id')
                if clear_stale_launch:
                    # SessionStart cleanup clears only pauses carried over from an older launch or unbound conversation.
                    if cur_launch == canonical_launch and (cur_conv is None or cur_conv == bound_conv):
                        return None
                elif canonical_conv is not None:
                    # A conversation-scoped callback may clear only a pause owned by its own conversation scope.
                    if cur_launch is not None and cur_launch != canonical_launch:
                        return None
                    if cur_conv is not None and cur_conv != canonical_conv:
                        return None
                    if cur_conv is None and canonical_conv != bound_conv:
                        return None
                else:
                    # Unscoped cleanup clears only pauses whose conversation is no longer bound.
                    if cur_launch == canonical_launch and (cur_conv is None or cur_conv == bound_conv):
                        return None
            return {'queue_state': None}

        turn_batch = (root, canonical_conv, tuple(turn_edges)) if (root is not None and canonical_conv and turn_edges) else None
        agents.mutate_runtime(Path(agent_dir), canonical_launch, _update, turn_batch=turn_batch)
    except (OSError, ValueError):
        pass


def _pause_followups(agent_dir, launch_id: str, native_id: str, *, reason: str, turn_active: bool,
                     root: Path | None = None, end_state: str | None = None,
                     next_source: str | None = None) -> None:
    """Pause queued follow-ups after USER_CANCELED, ERROR, or non-idle Stop and emit TurnEnded when open."""
    try:
        queue_path, lock_path = _followups_paths(agent_dir, launch_id)
        canonical_launch = normalize_session_id(launch_id)
        canonical_conv = normalize_session_id(native_id)
        if not queue_path.exists():
            return
        with lock(lock_path, create_parent=False):
            current_facts = retry_missing(lambda: read(Path(agent_dir) / 'runtime.json'))
            current_props = retry_missing(lambda: read(Path(agent_dir) / 'properties.json'))
            if (current_facts.get('launch_id') != canonical_launch
                    or current_facts.get('state') not in ('starting', 'running')
                    or current_facts.get('ending_at')
                    or current_facts.get('cleanup_retired')
                    or current_facts.get('binding_conflict')
                    or current_props.get('native_session_id') != canonical_conv):
                return
            rec = _load_followups_record_locked(queue_path)
            closed_turn = rec.get('turn_id') if rec.get('turn_open') else None
            changed = (bool(rec['idle_flushed']) or (rec['turn_active'] != bool(turn_active))
                       or bool(rec.get('turn_open'))
                       or (next_source is not None and rec.get('pending_turn_source') != next_source))
            for entry in rec['entries']:
                if entry.get('state', 'queued') == 'queued' and entry.get('native_id') == canonical_conv:
                    if entry.get('paused') is not True or entry.get('paused_reason') != reason:
                        entry['paused'] = True
                        entry['paused_reason'] = reason
                        changed = True
            if changed:
                rec['idle_flushed'] = False
                rec['idle_flushed_at'] = None
                rec['pending_idle_attempt'] = None
                rec['pending_idle_attempt_at'] = None
                rec['turn_seq'] = rec['turn_seq'] + 1
                rec['turn_active'] = bool(turn_active)
                rec['turn_active_at'] = now() if turn_active else None
                rec['idle_after_turn'] = 0
                rec['idle_after_turn_at'] = None
                rec['turn_open'] = False
                rec['turn_id'] = None
                rec['post_continue_streak'] = 0
                if next_source in ('person', 'harness'):
                    rec['pending_turn_source'] = next_source
                _save_record_locked(queue_path, canonical_launch, rec)
            edges = _turn_end_edges(closed_turn, end_state) if end_state else ()
            _sync_runtime_queue_state_locked(
                agent_dir, canonical_launch, canonical_conv, rec['entries'],
                root=root, turn_edges=edges,
            )
    except (OSError, ValueError):
        pass


def _resume_paused_locked(rec: dict, canonical_conv: str, mode: str) -> bool:
    """Unpause cancel/error-paused queued entries for a new idle send (`follow-up` unpauses all; `steer` unpauses steers)."""
    unpaused = False
    for entry in rec['entries']:
        if (entry.get('state', 'queued') == 'queued'
                and entry.get('native_id') == canonical_conv
                and entry.get('paused') is True
                and entry.get('paused_reason') in ('cancel', 'error')
                and (mode != 'steer' or _entry_mode(entry) == 'steer')):
            entry.pop('paused', None)
            entry.pop('paused_reason', None)
            unpaused = True
    return unpaused


def resume_paused_for_new_send(agent_dir, launch_id: str, native_id: str, *, mode: str = 'follow-up') -> bool:
    """Reap a turn that ended without `Stop`, then unpause cancel/error-paused entries for a new idle send.

    Reaping first matters: the reaper pauses every queued entry of a cancelled turn, including the message
    this send just queued, so resuming before reaping would leave the new message paused.
    """
    try:
        queue_path, lock_path = _followups_paths(agent_dir, launch_id)
        canonical_launch = normalize_session_id(launch_id)
        canonical_conv = normalize_session_id(native_id)
        if not queue_path.exists():
            return False
        with lock(lock_path, create_parent=False):
            current_facts = retry_missing(lambda: read(Path(agent_dir) / 'runtime.json'))
            current_props = retry_missing(lambda: read(Path(agent_dir) / 'properties.json'))
            if (current_facts.get('launch_id') != canonical_launch
                    or current_facts.get('state') not in ('starting', 'running')
                    or current_facts.get('status') != 'idle'
                    or current_facts.get('ending_at')
                    or current_facts.get('cleanup_retired')
                    or current_facts.get('binding_conflict')
                    or current_props.get('native_session_id') != canonical_conv):
                return False
            rec = _load_followups_record_locked(queue_path)
            reaped, cancelled, closed_turn = _reap_followups_locked(
                agent_dir, rec, live_facts=current_facts, conv_id=canonical_conv,
            )
            unpaused = _resume_paused_locked(rec, canonical_conv, mode)
            if not unpaused and not reaped:
                return False
            if unpaused:
                rec['idle_flushed'] = False
                rec['idle_flushed_at'] = None
                rec['turn_active'] = False
                rec['turn_active_at'] = None
                rec['idle_after_turn'] = 0
                rec['idle_after_turn_at'] = None
            _save_record_locked(queue_path, canonical_launch, rec)
            edges = _turn_end_edges(closed_turn, 'interrupted') if cancelled else ()
            _sync_runtime_queue_state_locked(
                agent_dir, canonical_launch, canonical_conv, rec['entries'],
                root=_universe_root(agent_dir), turn_edges=edges,
            )
            return unpaused
    except (OSError, ValueError):
        return False


def _read_transcript_tail(agent_dir: Path, conv_id: str | None, env: Mapping[str, str] | None = None, *,
                          limit_bytes: int = 65536, covers=None) -> list[dict] | None:
    """Read the tail JSONL rows of `conv_id`'s recorded transcript safely without following symlinks.

    When `covers(rows)` is false for a truncated window (the needed row is larger than it), reread enough to hold
    one maximal row.
    """
    if not conv_id:
        return None
    try:
        canonical_conv = normalize_session_id(conv_id)
    except ValueError:
        return None
    home = None
    try:
        props = retry_missing(lambda: read(agent_dir / 'properties.json'))
        raw_home = props.get('native_session_home') if isinstance(props, dict) else None
        if isinstance(raw_home, str) and os.path.isabs(raw_home):
            home = raw_home
    except (OSError, ValueError):
        home = None
    if not home:
        home = os.path.realpath(storage.agy_home(env))
    try:
        _, handle, info = transcripts._open_transcript_file(home, canonical_conv)
    except transcripts.Unreadable:
        return None
    if handle is None or info is None:
        return None
    try:
        with handle:
            offset = max(0, info.st_size - limit_bytes)
            rows = _parse_transcript_window(handle, offset)
            if covers is not None and offset > 0 and not covers(rows or []):
                rows = _parse_transcript_window(handle, max(0, info.st_size - storage.MAX_TRANSCRIPT_LINE - limit_bytes))
            return rows
    except OSError:
        return None


def _is_human_opener_at_step(rows: list[dict] | None, target_step: int | None) -> bool:
    """True when `rows` proves `step-{target_step}` is a human `USER_EXPLICIT` `USER_INPUT`."""
    if not rows or target_step is None or target_step < 0:
        return False
    for row in reversed(rows):
        if row.get('step_index') == target_step:
            return row.get('type') == 'USER_INPUT' and row.get('source') == 'USER_EXPLICIT'
    return False


def _last_non_system_row(rows: list[dict] | None) -> dict | None:
    if not rows:
        return None
    for row in reversed(rows):
        if row.get('type') != 'SYSTEM_MESSAGE':
            return row
    return None


def _is_turn_ending_planner_response(rows: list[dict] | None, initial_num_steps=None) -> tuple[bool, int | None]:
    """True with `step_index` when the last non-system step is a completed `PLANNER_RESPONSE` without tool calls.

    With `initial_num_steps` (PostInvocation's payload), the response must belong to this invocation
    (`step_index >= initialNumSteps`), never an earlier turn's reply read before agy wrote this one.
    """
    row = _last_non_system_row(rows)
    if not isinstance(row, dict) or row.get('type') != 'PLANNER_RESPONSE':
        return False, None
    if str(row.get('status') or '').upper() != 'DONE':
        return False, None
    tools = row.get('tool_calls')
    if isinstance(tools, list) and len(tools) > 0:
        return False, None
    step_idx = row.get('step_index')
    if type(initial_num_steps) is int and initial_num_steps >= 1 and not (type(step_idx) is int and step_idx >= initial_num_steps):
        return False, None
    return True, (step_idx if type(step_idx) is int and step_idx >= 0 else None)


def _on_turn_start(agent_dir, launch_id: str, native_id: str | None, *,
                   invocation_num=None, initial_num_steps=None,
                   env: Mapping[str, str] | None = None) -> tuple[tuple[TurnStarted, ...], list[str]]:
    """Advance `turn_seq`, handle `PreInvocation` `userMessage` injections, and return `(TurnStarted..., injected_texts)`."""
    try:
        queue_path, lock_path = _followups_paths(agent_dir, launch_id)
        canonical_launch = normalize_session_id(launch_id)
        canonical_conv = normalize_session_id(native_id) if native_id else None
        inv_num = invocation_num if isinstance(invocation_num, int) and not isinstance(invocation_num, bool) and invocation_num >= 0 else None
        init_steps = (
            initial_num_steps
            if isinstance(initial_num_steps, int) and not isinstance(initial_num_steps, bool) and initial_num_steps >= 1
            else None
        )
        # Prove any launch startup prompt (--task or wake resume_prompt) against the exact stored opening
        # USER_INPUT row before taking the queue lock; fail closed (unattributed) when proof is unavailable.
        startup_expectation = None
        startup_proof = 'unavailable'
        if canonical_conv is not None and _peek_turn_seq(queue_path) == 0:
            startup_expectation = _launch_startup_expectation(Path(agent_dir), canonical_launch, canonical_conv)
            if startup_expectation is not None:
                startup_proof = _prove_startup_input(
                    Path(agent_dir), canonical_conv, init_steps, startup_expectation[2], env=env,
                )
        tail_rows = None
        if canonical_conv is not None and inv_num == 0 and init_steps is not None:
            tail_rows = _read_transcript_tail(
                Path(agent_dir), canonical_conv, env,
                covers=lambda rows: any(row.get('step_index') == init_steps - 1 for row in rows),
            )
        with lock(lock_path):
            rec = _load_followups_record_locked(queue_path)
            # Recheck under the lock: once mv2 kill or a rebind starts, a handoff would be reported failed and resent.
            try:
                live_facts = retry_missing(lambda: read(Path(agent_dir) / 'runtime.json'))
                live_props = retry_missing(lambda: read(Path(agent_dir) / 'properties.json'))
            except (OSError, ValueError):
                live_facts, live_props = {}, {}
            can_hand_off = _hook_delivery_live(live_facts, live_props, canonical_launch, canonical_conv)
            for entry in rec['entries']:
                if (entry.get('state', 'queued') == 'queued'
                        and (canonical_conv is None or entry.get('native_id') == canonical_conv)):
                    entry.pop('paused', None)
                    entry.pop('paused_reason', None)
            idle_attempt_active = _idle_flushed_active(
                bool(rec.get('pending_idle_attempt')), rec.get('pending_idle_attempt_at'),
            )
            # On resume/wake, Antigravity can re-invoke PreInvocation(invocationNum=0) with the same
            # initialNumSteps ~0.16 s later when it attaches its low-priority server-restart notice while the
            # wake turn is still open. Treat that duplicate invocation as part of the open turn, not a new turn.
            same_open_turn = (
                bool(rec.get('turn_open'))
                and inv_num == 0
                and init_steps is not None
                and rec.get('last_init_steps') == init_steps
                and not rec.get('pending_turn_attempt')
                and not (idle_attempt_active and rec.get('pending_idle_attempt'))
            )
            starting_new = (not rec.get('turn_open')) or (inv_num == 0 and not same_open_turn)
            rec['turn_seq'] = rec['turn_seq'] + 1
            rec['turn_active'] = True
            rec['turn_active_at'] = now()
            rec['idle_after_turn'] = 0
            rec['idle_after_turn_at'] = None

            injected_texts: list[str] = []
            wake_handed_off_id: str | None = None
            human_opener = (
                inv_num == 0
                and init_steps is not None
                and _is_human_opener_at_step(tail_rows, init_steps - 1)
            )
            if can_hand_off:
                # 1. Resolve any wake_pending entry on invocationNum == 0.
                if inv_num == 0:
                    for entry in rec['entries']:
                        if (entry.get('state') == 'wake_pending'
                                and entry.get('native_id') == canonical_conv
                                and isinstance(entry.get('text'), str)):
                            if human_opener:
                                # A person typed this turn's prompt before the wake ping arrived: the turn is theirs.
                                idle_attempt_active = False
                                rec['pending_idle_attempt'] = None
                                rec['pending_idle_attempt_at'] = None
                            if _entry_mode(entry) == 'follow-up' and human_opener:
                                # The follow-up waits for the person's turn to end (PostInvocation injects it then).
                                entry['state'] = 'queued'
                                entry.pop('claimed_at', None)
                                entry.pop('claim_token', None)
                                entry.pop('wake_sent_at', None)
                            else:
                                wake_text = entry['text']
                                mark_receipt_delivered(agent_dir, entry['id'])
                                entry['state'] = 'handed_off'
                                entry['handed_off_at'] = now()
                                entry.pop('text', None)
                                entry.pop('claimed_at', None)
                                entry.pop('claim_token', None)
                                entry.pop('wake_sent_at', None)
                                injected_texts.append(wake_text)
                                if not human_opener:
                                    wake_handed_off_id = entry['id']
                            break
                # 2. Drain any queued steer entries via PreInvocation userMessage injection.
                for entry in rec['entries']:
                    if (entry.get('state', 'queued') == 'queued'
                            and entry.get('native_id') == canonical_conv
                            and _entry_mode(entry) == 'steer'
                            and entry.get('paused') is not True
                            and isinstance(entry.get('text'), str)):
                        steer_text = entry['text']
                        mark_receipt_delivered(agent_dir, entry['id'])
                        entry['state'] = 'handed_off'
                        entry['handed_off_at'] = now()
                        entry.pop('text', None)
                        injected_texts.append(steer_text)
                        if wake_handed_off_id is None and starting_new and not human_opener and not rec.get('pending_turn_attempt'):
                            wake_handed_off_id = entry['id']

            edges: tuple[TurnStarted, ...] = ()
            if starting_new:
                attempt = rec.get('pending_turn_attempt')
                source = rec.get('pending_turn_source')
                is_post_logged = False
                first_task = False
                if attempt:
                    source = 'harness'
                elif wake_handed_off_id is not None:
                    attempt = wake_handed_off_id
                    source = 'harness'
                    is_post_logged = True
                    rec['post_continue_streak'] = 0
                elif idle_attempt_active and rec.get('pending_idle_attempt'):
                    attempt = rec.get('pending_idle_attempt')
                    source = 'harness'
                    is_post_logged = True
                    rec['post_continue_streak'] = 0
                else:
                    rec['post_continue_streak'] = 0
                    if human_opener:
                        # A typed prompt beats the harness hint a background Stop left for its task notification.
                        source = 'person'
                    if rec['turn_seq'] == 1 and startup_expectation is not None:
                        kind_exp, wake_msg, _ = startup_expectation
                        if kind_exp == 'wake' and wake_msg is not None:
                            if startup_proof == 'matched':
                                attempt = wake_msg
                                source = 'harness'
                            elif startup_proof == 'unavailable':
                                source = 'harness'
                        elif kind_exp == 'task' and startup_proof == 'matched':
                            first_task = True
                    source = source if source in ('person', 'harness') else 'person'
                last_steps = rec.get('last_init_steps')
                if init_steps is not None:
                    # Antigravity appends human USER_INPUT and PostInvocation-injected USER_INPUT before
                    # PreInvocation (step-{K-1}), but appends PreInvocation-injected USER_INPUT and
                    # background task notifications after PreInvocation (step-{K}).
                    # Also use post:{K} if K <= last_init_steps (the previous turn ended with zero extra steps).
                    if is_post_logged or (last_steps is not None and init_steps <= last_steps):
                        turn_id = f'post:{init_steps}:{rec["turn_seq"]}'
                    else:
                        turn_id = f'init:{init_steps}:{rec["turn_seq"]}'
                    ref = turn_id
                    rec['last_init_steps'] = init_steps
                else:
                    turn_id = f'turn-{rec["turn_seq"]}'
                    ref = None
                rec['turn_open'] = True
                rec['turn_id'] = turn_id
                rec['pending_turn_attempt'] = None
                rec['pending_turn_source'] = None
                edges = (TurnStarted(
                    turn=turn_id, kind='prompt', ref=ref, source=source,
                    attempt=attempt, first_task=first_task,
                ),)
            rec['idle_flushed'] = False
            rec['idle_flushed_at'] = None
            rec['pending_idle_attempt'] = None
            rec['pending_idle_attempt_at'] = None
            _save_record_locked(queue_path, canonical_launch, rec)
            _sync_runtime_queue_state_locked(agent_dir, canonical_launch, canonical_conv, rec['entries'])
            return edges, injected_texts
    except (OSError, ValueError):
        return (), []


def _on_conversation_switch(agent_dir, launch_id: str, departed_conv: str | None = None) -> None:
    """Reset turn/idle-flush gates and paused queue state owned by the departed conversation on /clear."""
    try:
        queue_path, lock_path = _followups_paths(agent_dir, launch_id)
        if queue_path.exists():
            with lock(lock_path, create_parent=False):
                rec = _load_followups_record_locked(queue_path)
                if (rec['idle_flushed'] or rec['turn_active'] or rec['idle_after_turn'] > 0
                        or rec.get('turn_open') or rec.get('pending_turn_attempt')
                        or rec.get('pending_turn_source') or rec.get('pending_idle_attempt')
                        or rec.get('last_init_steps') is not None
                        or rec.get('post_continue_streak') or rec.get('last_post_end_step') is not None):
                    rec['idle_flushed'] = False
                    rec['idle_flushed_at'] = None
                    rec['turn_seq'] = rec['turn_seq'] + 1
                    rec['turn_active'] = False
                    rec['turn_active_at'] = None
                    rec['idle_after_turn'] = 0
                    rec['idle_after_turn_at'] = None
                    rec['turn_open'] = False
                    rec['turn_id'] = None
                    rec['pending_turn_attempt'] = None
                    rec['pending_turn_source'] = None
                    rec['pending_idle_attempt'] = None
                    rec['pending_idle_attempt_at'] = None
                    rec['last_init_steps'] = None
                    rec['post_continue_streak'] = 0
                    rec['last_post_end_step'] = None
                    _save_record_locked(queue_path, launch_id, rec)
                _sync_runtime_queue_state_locked(agent_dir, launch_id, departed_conv, [])
        else:
            _sync_runtime_queue_state_locked(agent_dir, launch_id, departed_conv, [])
    except (OSError, ValueError):
        pass


def followup_status(agent_dir, launch_id: str, message_id: str) -> tuple[str | None, str | None]:
    """Return `(state, paused_reason)` for `message_id`."""
    try:
        queue_path, lock_path = _followups_paths(agent_dir, launch_id)
        canonical_msg = normalize_session_id(message_id)
    except ValueError:
        return None, None
    if not queue_path.exists():
        return None, None
    try:
        with lock(lock_path, create_parent=False):
            entries = _load_followups_locked(queue_path)
    except (OSError, ValueError):
        return 'error', None
    for entry in entries:
        if entry.get('id') == canonical_msg:
            paused = entry.get('paused_reason') if entry.get('paused') is True else None
            return entry.get('state', 'queued'), paused
    return None, None


def followup_state(agent_dir, launch_id: str, message_id: str) -> str | None:
    """Return `'queued'`, `'handed_off'`, `'agentapi_submitting'`, None (absent), or `'error'` (damaged)."""
    state, _ = followup_status(agent_dir, launch_id, message_id)
    return state


def has_queued_followup(agent_dir, launch_id: str, message_id: str) -> bool:
    return followup_state(agent_dir, launch_id, message_id) == 'queued'


def read_followup_queue(agent_dir, launch_id: str) -> tuple[str, dict | None, str | None]:
    """Read-only, anchored inspection of this launch's Stop-hook follow-up queue for QueueProvider.

    Returns `('missing', None, None)`, `('ok', data, None)`, or `('invalid', None, reason)`.
    Never mutates or creates files and never exposes message text.
    """
    try:
        canonical_launch = normalize_session_id(launch_id)
    except ValueError:
        return 'invalid', None, 'Invalid launch ID for Antigravity follow-up queue'
    agent = Path(agent_dir)
    agent_fd = launches_fd = launch_fd = fd = -1
    try:
        agent_fd = os.open(agent, _DIR_FLAGS)
        launches_fd = os.open('launches', _DIR_FLAGS, dir_fd=agent_fd)
        launch_fd = os.open(canonical_launch, _DIR_FLAGS, dir_fd=launches_fd)
        fd = os.open(FOLLOWUPS_FILE, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=launch_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return 'invalid', None, "Antigravity's follow-up queue is not a regular file"
        if info.st_uid != os.getuid() or (stat.S_IMODE(info.st_mode) & 0o077) != 0:
            return 'invalid', None, "Antigravity's follow-up queue has invalid ownership or permissions"
        if info.st_size > MAX_QUEUE_BYTES:
            return 'invalid', None, "Antigravity's follow-up queue exceeds the size limit"
        with os.fdopen(fd, 'rb') as handle:
            fd = -1
            raw = handle.read(MAX_QUEUE_BYTES + 1)
        if len(raw) > MAX_QUEUE_BYTES:
            return 'invalid', None, "Antigravity's follow-up queue exceeds the size limit"
        data = json.loads(raw.decode('utf-8'))
    except FileNotFoundError:
        return 'missing', None, None
    except (OSError, UnicodeDecodeError, ValueError):
        return 'invalid', None, "Antigravity's follow-up queue can't be read"
    finally:
        for handle_fd in (fd, launch_fd, launches_fd, agent_fd):
            if handle_fd >= 0:
                os.close(handle_fd)
    if (not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1
            or not isinstance(data.get('entries'), list)):
        return 'invalid', None, "Antigravity's follow-up queue can't be read"
    if data.get('launch_id') != canonical_launch:
        return 'invalid', None, "Antigravity's follow-up queue belongs to another launch"
    return 'ok', data, None


def receipt_path(agent_dir, message_id: str) -> Path:
    return Path(agent_dir) / RECEIPTS_DIR / f'{normalize_session_id(message_id)}.json'


def write_receipt(agent_dir, message_id: str, *, launch_id: str, native_id: str,
                  route: str, submitted_at: str, text_sha256: str,
                  delivered_at: str | None = None, acknowledged: bool = True) -> Path:
    """Write a private delivery receipt (metadata + SHA-256 hash only; never raw message text)."""
    canonical_msg = normalize_session_id(message_id)
    path = receipt_path(agent_dir, canonical_msg)
    _write_private_json(path, {
        'schema': 1,
        'id': canonical_msg,
        'launch_id': normalize_session_id(launch_id),
        'native_id': normalize_session_id(native_id),
        'route': route,
        'submitted_at': submitted_at,
        'text_sha256': text_sha256,
        'delivered_at': delivered_at,
        'acknowledged': bool(acknowledged),
    })
    return path


def remove_receipt(agent_dir, message_id: str) -> None:
    """Remove a private delivery receipt safely without following symlinks."""
    try:
        canonical_msg = normalize_session_id(message_id)
        dir_fd = os.open(Path(agent_dir) / RECEIPTS_DIR, _DIR_FLAGS)
    except (OSError, ValueError):
        return
    try:
        os.unlink(f'{canonical_msg}.json', dir_fd=dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def read_receipt(agent_dir, message_id: str):
    """Return validated receipt dict, None when absent, or False when unreadable/symlink/bad-mode/malformed."""
    canonical_msg = normalize_session_id(message_id)
    record = _read_private_json(receipt_path(agent_dir, canonical_msg), require_private_mode=True)
    if record is None or record is False:
        return record
    if (record.get('schema') != 1 or record.get('id') != canonical_msg
            or record.get('route') not in VALID_RECEIPT_ROUTES
            or not isinstance(record.get('launch_id'), str)
            or not isinstance(record.get('native_id'), str)):
        return False
    return record


def _read_receipt_at(dir_fd: int, name: str, canonical_msg: str):
    """Read and validate one private 0600 receipt directly from an anchored receipts `dir_fd`."""
    try:
        fd = retry_missing(lambda: os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd))
    except FileNotFoundError:
        return None
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or (stat.S_IMODE(info.st_mode) & 0o077) != 0):
            return False
        with os.fdopen(fd, 'rb') as handle:
            fd = -1
            data = handle.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_RECORD_BYTES:
        return False
    try:
        record = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return False
    if (not isinstance(record, dict)
            or record.get('schema') != 1 or record.get('id') != canonical_msg
            or record.get('route') not in VALID_RECEIPT_ROUTES
            or not isinstance(record.get('launch_id'), str)
            or not isinstance(record.get('native_id'), str)):
        return False
    return record


def _receipt_stat_sig(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_mode, info.st_uid)


def _load_disk_receipt_index(folder: Path) -> dict[str, tuple[tuple[int, ...], tuple[str, ...]]]:
    """Best-effort load of private 0600 `.index.json`; every entry is still stat-verified against live files."""
    try:
        with _open_private_regular(folder / _RECEIPT_INDEX_FILE, require_private_mode=True) as handle:
            data = handle.read(16 << 20)
        raw = json.loads(data.decode('utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get('schema') != 1 or not isinstance(raw.get('entries'), dict):
        return {}
    loaded = {}
    for name, item in raw['entries'].items():
        if (isinstance(name, str) and isinstance(item, list) and len(item) == 2
                and isinstance(item[0], list) and len(item[0]) == 6
                and all(isinstance(v, int) for v in item[0])
                and isinstance(item[1], list) and len(item[1]) == 6
                and all(isinstance(v, str) for v in item[1])):
            loaded[name] = (tuple(item[0]), tuple(item[1]))
    return loaded


def _save_disk_receipt_index(folder: Path, cache: dict[str, tuple[tuple[int, ...], tuple[str, ...]]]) -> None:
    try:
        serialized = {
            'schema': 1,
            'entries': {name: [list(sig), list(summary)] for name, (sig, summary) in cache.items()},
        }
        _write_private_json(folder / _RECEIPT_INDEX_FILE, serialized, max_bytes=16 << 20)
    except (OSError, ValueError):
        pass


def receipt_cohort(agent_dir, target_id: str, *, native_id: str, route: str,
                   text_sha256: str) -> tuple[list[str], int] | None:
    """Return `(ordered_submitted_at, target_index)` across all active receipts for `(native_id, route, hash)`.

    Includes later receipts in the same cohort so an indeterminate earlier attempt cannot steal a later
    attempt's transcript row, while excluding peer `stop-hook` follow-ups that are still queued (un-handed-off).
    Returns None if the receipts directory or any receipt file is unreadable, damaged, or exceeds the scan limit.
    Uses a stat-verified index so repeated checks over thousands of receipts avoid re-opening unchanged files.
    """
    canonical_target = normalize_session_id(target_id)
    target_filename = f'{canonical_target}.json'
    folder = Path(agent_dir) / RECEIPTS_DIR
    uid = os.getuid()
    try:
        dir_fd = os.open(folder, _DIR_FLAGS)
    except OSError:
        return None
    matches = []
    found_target = False
    dirty = False
    new_cache: dict[str, tuple[tuple[int, ...], tuple[str, ...]]] = {}
    dir_key = None
    try:
        try:
            dir_stat = os.fstat(dir_fd)
            dir_key = (dir_stat.st_dev, dir_stat.st_ino)
            names = sorted(name for name in os.listdir(dir_fd) if name.endswith('.json') and not name.startswith('.'))
        except OSError:
            return None
        if len(names) > MAX_RECEIPTS_SCAN:
            return None
        cached_dir = _RECEIPT_CACHE.get(dir_key)
        if cached_dir is None:
            cached_dir = _load_disk_receipt_index(folder) if len(names) >= 64 else {}
        for name in names:
            stem = name[:-5]
            try:
                canonical_stem = normalize_session_id(stem)
            except ValueError:
                continue
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                return None
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                    or (stat.S_IMODE(info.st_mode) & 0o077) != 0
                    or info.st_size > MAX_RECORD_BYTES):
                return None
            sig = _receipt_stat_sig(info)
            summary = None
            if name != target_filename:
                hit = cached_dir.get(name)
                if hit is not None and hit[0] == sig:
                    summary = hit[1]
            if summary is None:
                record = _read_receipt_at(dir_fd, name, canonical_stem)
                if not isinstance(record, dict):
                    return None  # Unreadable or damaged receipt makes cohort allocation unknown.
                rec_hash = record.get('text_sha256')
                submitted_at = record.get('submitted_at')
                if (not isinstance(rec_hash, str)
                        or not isinstance(submitted_at, str)
                        or storage.moment(submitted_at) is None):
                    return None
                summary = (
                    record['native_id'],
                    record['route'],
                    rec_hash,
                    submitted_at,
                    record['launch_id'],
                    record['id'],
                )
                dirty = True
            new_cache[name] = (sig, summary)
            r_native, r_route, r_hash, r_submitted, r_launch, r_id = summary
            if r_native == native_id and r_route == route and r_hash == text_sha256:
                matches.append((r_submitted, r_launch, r_id))
                if r_id == canonical_target:
                    found_target = True
    finally:
        os.close(dir_fd)
    if dir_key is not None:
        if len(_RECEIPT_CACHE) >= _RECEIPT_CACHE_MAX_DIRS and dir_key not in _RECEIPT_CACHE:
            _RECEIPT_CACHE.pop(next(iter(_RECEIPT_CACHE)))
        _RECEIPT_CACHE[dir_key] = new_cache
        if dirty and len(new_cache) >= 64:
            _save_disk_receipt_index(folder, new_cache)
    if not found_target:
        return None
    cohort = []
    for r_submitted, r_launch, r_id in matches:
        if r_id != canonical_target:
            if route == 'hook-user-input' and followup_state(agent_dir, r_launch, r_id) in ('queued', 'wake_pending'):
                # A queued or wake-pending entry has not yet been injected into the transcript by hooks.
                continue
            if route == 'stop-hook' and followup_state(agent_dir, r_launch, r_id) == 'queued':
                # A follow-up still sitting un-handed-off in its launch's queue has not produced a transcript row.
                continue
        cohort.append((r_submitted, r_id))
    cohort.sort()
    times = [submitted_at for submitted_at, _ in cohort]
    for idx, (_, identifier) in enumerate(cohort):
        if identifier == canonical_target:
            return times, idx
    return None


def receipt_cohort_since(agent_dir, target_id: str, *, native_id: str, route: str,
                         text_sha256: str) -> list[str] | None:
    """Return ordered `submitted_at` timestamps up to and including `target_id` for identical `(native_id, route, hash)`."""
    cohort = receipt_cohort(agent_dir, target_id, native_id=native_id, route=route, text_sha256=text_sha256)
    if cohort is None:
        return None
    times, idx = cohort
    return times[:idx + 1]


def mark_receipt_delivered(agent_dir, message_id: str, delivered_at: str | None = None) -> None:
    record = read_receipt(agent_dir, message_id)
    if not isinstance(record, dict):
        return
    record['delivered_at'] = delivered_at or now()
    _write_private_json(receipt_path(agent_dir, message_id), record)


def _session_state_path(directory: Path, launch_id: str) -> Path:
    return directory / 'launches' / normalize_session_id(launch_id) / SESSION_STATE_FILE


def _read_session_state(directory: Path, launch_id: str) -> dict:
    data = _read_private_json(_session_state_path(directory, launch_id), require_private_mode=True)
    if not isinstance(data, dict) or data.get('schema') != 1:
        return {'schema': 1, 'observed_expected': False, 'departed': []}
    departed = data.get('departed')
    return {
        'schema': 1,
        'observed_expected': bool(data.get('observed_expected')),
        'departed': [item for item in departed if isinstance(item, str)] if isinstance(departed, list) else [],
    }


def _peek_turn_seq(queue_path: Path) -> int:
    """Unlocked fast check for turn_seq so turns 2+ skip startup-input transcript verification."""
    record = _read_private_json(queue_path, limit=MAX_QUEUE_BYTES, require_private_mode=True)
    if record is None:
        return 0
    if not isinstance(record, dict) or record.get('schema') != 1:
        return -1
    return _nonneg_int(record.get('turn_seq'), 0)


def _launch_startup_expectation(
    directory: Path, launch_id: str, conv_id: str | None,
) -> tuple[str, str | None, str] | None:
    """Return `('wake', wake_id, text)` or `('task', None, text)` for this launch's CLI startup prompt, or None."""
    from multiverse.runtime import launch_files
    try:
        req = launch_files.read_request(directory, launch_id)
    except (OSError, ValueError):
        return None
    if not isinstance(req, dict):
        return None
    req_env = req.get('env')
    if isinstance(req_env, dict):
        if req_env.get('MV2_LAUNCH_ID') not in (None, launch_id):
            return None
        if req_env.get('MV2_AGENT_ID') not in (None, directory.name):
            return None
    argv = req.get('argv')
    if not isinstance(argv, list) or not argv or not isinstance(argv[-1], str):
        return None
    prefix = '--prompt-interactive='
    if not argv[-1].startswith(prefix):
        return None
    expected_text = argv[-1][len(prefix):]
    if not expected_text.strip():
        return None
    try:
        if len(expected_text.encode('utf-8')) > storage.MAX_TRANSCRIPT_LINE:
            return None
    except UnicodeError:
        return None
    raw_wake = req.get('resume_message_id')
    if isinstance(raw_wake, str) and raw_wake:
        if req.get('task_launch') is True or not conv_id:
            return None
        expected_native = req.get('expected_native_id')
        if not isinstance(expected_native, str) or not expected_native:
            return None
        try:
            wake_id = normalize_session_id(raw_wake)
            if normalize_session_id(expected_native) != normalize_session_id(conv_id):
                return None
        except ValueError:
            return None
        return 'wake', wake_id, expected_text
    if raw_wake is None and req.get('task_launch') is True and req.get('expected_native_id') is None:
        return 'task', None, expected_text
    return None


def _extract_opener_user_text(row: dict) -> str | None:
    """Extract the exact prompt text from a stored `USER_INPUT` opener row, or None."""
    if row.get('type') != 'USER_INPUT' or row.get('source') not in (None, 'USER_EXPLICIT'):
        return None
    content = row.get('content')
    if not isinstance(content, str) or not content:
        return None
    stripped = content.lstrip()
    if stripped.startswith('<USER_REQUEST>'):
        after = stripped[len('<USER_REQUEST>'):]
        before_meta = after.split('\n<ADDITIONAL_METADATA>', 1)[0]
        end = before_meta.rfind('</USER_REQUEST>')
        return before_meta[:end] if end >= 0 else None
    if '<USER_REQUEST>' in content:
        matches = storage._USER_REQUEST_BLOCK.findall(content)
        return matches[0] if len(matches) == 1 else None
    return content


def _parse_transcript_window(handle, offset: int) -> list[dict] | None:
    """Parse JSONL dict rows from `offset` to EOF; returns None if any line is damaged or oversized."""
    handle.seek(offset)
    if offset > 0:
        if not handle.readline(storage.MAX_TRANSCRIPT_LINE + 1).endswith(b'\n'):
            return None
    rows: list[dict] = []
    while True:
        raw = handle.readline(storage.MAX_TRANSCRIPT_LINE + 1)
        if not raw:
            break
        if len(raw) > storage.MAX_TRANSCRIPT_LINE:
            return None
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped.decode('utf-8'))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(parsed, dict):
            return None
        rows.append(parsed)
    return rows


def _locate_opener_in_rows(rows: list[dict], target_step: int, *, truncated_prefix: bool) -> tuple[str, dict | None]:
    matches = [idx for idx, row in enumerate(rows) if type(row.get('step_index')) is int and row['step_index'] == target_step]
    if len(matches) > 1:
        return 'unreadable', None
    if not matches:
        steps = [row['step_index'] for row in rows if type(row.get('step_index')) is int]
        if truncated_prefix and (not steps or min(steps) > target_step):
            return 'need_full', None
        max_step = max(steps, default=-1)
        return ('pending', None) if max_step < target_step else ('mismatch', None)
    idx = matches[0]
    if rows[idx].get('type') == 'USER_INPUT':
        return 'found', rows[idx]
    _, label, *_ = transcripts._classify_row(rows[idx], turn_open=False)
    if label in ('injected', 'meta'):
        cursor = idx - 1
        while cursor >= 0:
            _, cur_label, *_ = transcripts._classify_row(rows[cursor], turn_open=False)
            if cur_label not in ('injected', 'meta'):
                break
            cursor -= 1
        if cursor < 0:
            return ('need_full', None) if truncated_prefix else ('mismatch', None)
        if rows[cursor].get('type') == 'USER_INPUT':
            return 'found', rows[cursor]
    return 'mismatch', None


def _read_opener_at_step(home: str, canonical_conv: str, target_step: int,
                         expected_bytes: int) -> tuple[str, dict | None]:
    """Read the exact stored opening `USER_INPUT` at `step-{target_step}`, safely without following symlinks."""
    try:
        _, handle, info = transcripts._open_transcript_file(home, canonical_conv)
    except transcripts.Unreadable:
        return 'unreadable', None
    if handle is None or info is None:
        return 'pending', None
    try:
        with handle:
            window = min(storage.MAX_TRANSCRIPT_LINE, max(65536, expected_bytes * 4 + 16384))
            offset = max(0, info.st_size - window)
            rows = _parse_transcript_window(handle, offset)
            if rows is None:
                return 'unreadable', None
            state, opener = _locate_opener_in_rows(rows, target_step, truncated_prefix=offset > 0)
            if state == 'need_full':
                full_rows = _parse_transcript_window(handle, 0)
                if full_rows is None:
                    return 'unreadable', None
                return _locate_opener_in_rows(full_rows, target_step, truncated_prefix=False)
            return state, opener
    except OSError:
        return 'unreadable', None


def _prove_startup_input(
    directory: Path,
    conv_id: str | None,
    init_steps: int | None,
    expected_text: str,
    env: Mapping[str, str] | None = None,
    *,
    wait_seconds: float = 0.15,
) -> str:
    """Return `'matched'`, `'mismatch'`, or `'unavailable'` for the opener at `step-{init_steps - 1}`."""
    if not conv_id or init_steps is None or init_steps < 1 or not expected_text.strip():
        return 'unavailable'
    try:
        canonical_conv = normalize_session_id(conv_id)
    except ValueError:
        return 'unavailable'
    home = None
    try:
        props = retry_missing(lambda: read(directory / 'properties.json'))
        raw_home = props.get('native_session_home') if isinstance(props, dict) else None
        if isinstance(raw_home, str) and os.path.isabs(raw_home):
            home = raw_home
    except (OSError, ValueError):
        home = None
    if not home:
        home = os.path.realpath(storage.agy_home(env))
    expected_norm = expected_text.strip()
    expected_bytes = len(expected_text.encode('utf-8'))
    target_step = init_steps - 1
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        state, opener = _read_opener_at_step(home, canonical_conv, target_step, expected_bytes)
        if state == 'found' and isinstance(opener, dict):
            stored = _extract_opener_user_text(opener)
            if not isinstance(stored, str):
                return 'unavailable'
            return 'matched' if stored.strip() == expected_norm else 'mismatch'
        if state == 'mismatch':
            return 'mismatch'
        if state != 'pending':
            return 'unavailable'
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 'unavailable'
        time.sleep(min(0.025, remaining))


def _launch_expected_id(directory: Path, launch_id: str) -> tuple[str, str | None]:
    """Classify launch request proof as `('fresh', None)`, `('resume', uuid)` or `('invalid', None)`."""
    from multiverse.runtime import launch_files
    try:
        req = launch_files.read_request(directory, launch_id)
    except (OSError, ValueError):
        return 'invalid', None
    if not isinstance(req, dict) or 'expected_native_id' not in req:
        return 'invalid', None
    expected = req['expected_native_id']
    if expected is None:
        return 'fresh', None
    if isinstance(expected, str) and expected:
        try:
            return 'resume', normalize_session_id(expected)
        except ValueError:
            return 'invalid', None
    return 'invalid', None


def _sync_ls_conversation_fenced(directory: Path, launch_id: str, conv_id: str) -> None:
    """Update launch-local agy-ls.json after /clear strictly inside core's launch+runtime cleanup fence."""
    from multiverse.universe import agents
    try:
        canonical_launch = normalize_session_id(launch_id)
        canonical_conv = normalize_session_id(conv_id)
    except ValueError:
        return
    with lock(directory / '.launch.lock', create_parent=False), lock(directory / '.runtime.lock', create_parent=False):
        existing_ls = read_ls_info(directory, canonical_launch)
        if existing_ls is None or existing_ls.get('conversation_id') == canonical_conv:
            return
        try:
            current_props = retry_missing(lambda: read(directory / 'properties.json'))
            current_facts = agents.runtime(directory)
        except (OSError, ValueError):
            return
        if (current_facts.get('launch_id') != canonical_launch
                or current_facts.get('state') not in ('starting', 'running')
                or current_facts.get('ending_at')
                or current_facts.get('cleanup_retired')
                or current_facts.get('binding_conflict')
                or (current_facts.get('native_mode') or {}).get('mode') != 'bridge'
                or current_props.get('native_session_id') != canonical_conv):
            return
        try:
            info = os.stat(ls_path(directory, canonical_launch), follow_symlinks=False)
        except OSError:
            return
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or (stat.S_IMODE(info.st_mode) & 0o077) != 0):
            return
        write_ls_info(
            directory, canonical_launch, existing_ls['ls_address'], existing_ls['csrf_token'], canonical_conv,
        )


def _departed_for_launch(directory: Path, launch_id: str, state: dict) -> set[str]:
    departed = set(state.get('departed', ()))
    try:
        from multiverse.universe import sessions
        for row in sessions.journal(directory):
            if row.get('launch') == launch_id and isinstance(row.get('from'), str):
                departed.add(row['from'])
    except Exception:
        pass
    return departed


def _observe_conversation(root: Path, directory: Path, launch_id: str, properties: dict,
                          conv_id: str, source: dict) -> str:
    """Apply ordered session binding/switching with request-proof, resume-mismatch and delayed-callback fences.

    Returns `'ok'` when `conv_id` is now the verified active conversation, `'ignore'` for a delayed callback from
    a conversation already left in this launch, or `'conflict'` when launch proof is missing/damaged or a resumed
    launch reports an unexpected ID before its resumed conversation was ever observed.
    """
    from multiverse.universe import agents
    current = properties.get('native_session_id')
    state = _read_session_state(directory, launch_id)
    req_kind, expected = _launch_expected_id(directory, launch_id)
    departed = _departed_for_launch(directory, launch_id, state)

    if req_kind == 'invalid':
        if current == conv_id:
            return 'ok'
        if conv_id in departed:
            return 'ignore'
        agents.update_runtime(
            directory, launch_id,
            binding_conflict=(
                f'Cannot verify Antigravity launch proof for {launch_id}; '
                f'refusing automatic bind or rebind to {conv_id}'
            ),
        )
        return 'conflict'

    if req_kind == 'resume' and not state['observed_expected']:
        if conv_id == expected:
            state['observed_expected'] = True
            _write_private_json(_session_state_path(directory, launch_id), state)
            if current is None:
                agents.bind_session(root, directory.name, conv_id, launch_id)
            elif current != conv_id:
                agents.update_runtime(
                    directory, launch_id,
                    binding_conflict=(
                        f'Saved agent session ({current}) is different from resumed target ({expected}); '
                        'refusing automatic rebind'
                    ),
                )
                return 'conflict'
            return 'ok'
        agents.update_runtime(
            directory, launch_id,
            binding_conflict=(
                f'Antigravity reported a different conversation ({conv_id}) than resumed session ({expected}); '
                'refusing automatic rebind'
            ),
        )
        return 'conflict'

    if current == conv_id:
        return 'ok'

    if conv_id in departed:
        # Delayed callback from a conversation this launch already moved away from.
        return 'ignore'

    if agents.runtime(directory).get('binding_conflict'):
        return 'conflict'

    if current is None:
        agents.bind_session(root, directory.name, conv_id, launch_id)
        return 'ok'

    home = os.path.realpath(storage.agy_home(source))
    agents.rebind_session(root, directory.name, current, conv_id, 'clear', launch_id, home=home)
    if current not in state['departed']:
        state['departed'].append(current)
    _write_private_json(_session_state_path(directory, launch_id), state)

    _sync_ls_conversation_fenced(directory, launch_id, conv_id)
    _on_conversation_switch(directory, launch_id, departed_conv=current)
    return 'ok'


def _map_status(payload: dict) -> str:
    if payload.get('tool_confirmation_pending') is True:
        return 'waiting'
    state = payload.get('agent_state')
    if state == 'idle':
        return 'idle'
    if state in ('working', 'tool_use', 'reviewing'):
        return 'working'
    return 'unknown'


def _number(value, low=0.0, high=None):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= low
    return value if ok and (high is None or value <= high) else None


def _extract_usage(payload: dict) -> dict | None:
    """Convert Antigravity's statusLine JSON payload into mv2's `runtime_update.usage` shape."""
    context = {}
    cw = payload.get('context_window')
    if isinstance(cw, dict):
        window = _number(cw.get('context_window_size'), low=1)
        if window is not None:
            context['window'] = int(window)
        percent = _number(cw.get('used_percentage'), low=0, high=100)
        if percent is not None:
            context['percent'] = round(float(percent), 2)
        current = cw.get('current_usage')
        if isinstance(current, dict):
            parts = [
                _number(current.get(key), low=0)
                for key in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
            ]
            if any(part is not None for part in parts):
                context['tokens'] = int(sum(part or 0 for part in parts))

    rate_limits = []
    quota = payload.get('quota')
    if isinstance(quota, dict):
        for bucket, info in sorted(quota.items()):
            if not isinstance(bucket, str) or not isinstance(info, dict):
                continue
            rem = _number(info.get('remaining_fraction'), low=0, high=1)
            if rem is None:
                continue
            item = {'window': bucket[:32], 'percent_used': round((1.0 - float(rem)) * 100.0, 2)}
            reset = info.get('reset_time')
            if isinstance(reset, str) and reset:
                item['resets_at'] = reset[:40]
            rate_limits.append(item)

    cost_usd = None
    cost = payload.get('cost')
    if isinstance(cost, dict):
        total = _number(cost.get('total_usd'), low=0)
        if total is not None:
            cost_usd = float(total)

    if not context and not rate_limits and cost_usd is None:
        return None
    result = {'context': context, 'rate_limits': rate_limits}
    if cost_usd is not None:
        result['cost_usd'] = cost_usd
    return result


def _status_line_text(directory: Path, properties: dict, facts: dict) -> str:
    """Concise single-line mv2 status bar shown below Antigravity's default status line."""
    parts = []
    name = properties.get('name')
    if isinstance(name, str) and name.strip():
        parts.append(f'mv2:{name.strip()}')
    phase = properties.get('phase')
    if isinstance(phase, str) and phase.strip():
        parts.append(f'phase {phase.strip()}')
    try:
        from multiverse.universe import mailbox
        unread = len(mailbox.unread(directory, blocking=False))
        if unread:
            parts.append(f'inbox {unread}')
    except Exception:
        pass
    if facts.get('native_bridge') == BRIDGE:
        parts.append('bridge')
    return ' · '.join(parts)


def _flush_idle_followup(directory: Path, launch_id: str, properties: dict, conv_id: str) -> bool:
    """Wake an idle session via a hidden `agentapi` ping so `PreInvocation` injects the queued head as `USER_INPUT`.

    Re-checks and reaps bounded delivery state (`idle_flushed`, stale `wake_pending` / `agentapi_submitting`,
    and sticky `turn_active`) under the queue lock before claiming. Sets a self-expiring `idle_flushed` marker
    on claim so consecutive idle statusLine callbacks cannot send a second wake ping before the turn starts.
    """
    from multiverse.runtime import launch_files
    queue_path, lock_path = _followups_paths(directory, launch_id)
    if not queue_path.exists():
        return False
    try:
        ls_info = read_ls_info(directory, launch_id)
        if ls_info is None or ls_info['launch_id'] != launch_id:
            return False
        if ls_info.get('conversation_id') is not None and ls_info['conversation_id'] != conv_id:
            return False
        with lock(lock_path, create_parent=False):
            current_facts = retry_missing(lambda: read(directory / 'runtime.json'))
            rec = _load_followups_record_locked(queue_path)
            reaped, cancelled, closed_turn = _reap_followups_locked(
                directory, rec, live_facts=current_facts, conv_id=conv_id,
            )
            if reaped:
                _save_record_locked(queue_path, launch_id, rec)
            if cancelled:
                _sync_runtime_queue_state_locked(
                    directory, launch_id, conv_id, rec['entries'],
                    root=_universe_root(directory), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
            if (rec['turn_active']
                    or _idle_flushed_active(rec['idle_flushed'], rec['idle_flushed_at'])
                    or _wake_in_flight(rec['entries'], conv_id)):
                return False
            if _next_queued_entry(rec['entries'], conv_id) is None:
                return False

        try:
            pinned_address = pin_loopback_endpoint(ls_info['ls_address'], timeout=2.0)
        except (ValueError, RuntimeError):
            return False

        claimed_id = None
        claim_token = str(uuid4())
        with lock(lock_path, create_parent=False):
            current_facts = retry_missing(lambda: read(directory / 'runtime.json'))
            current_props = retry_missing(lambda: read(directory / 'properties.json'))
            if (current_facts.get('status') != 'idle'
                    or not _hook_delivery_live(current_facts, current_props, launch_id, conv_id)):
                return False
            rec = _load_followups_record_locked(queue_path)
            reaped, cancelled, closed_turn = _reap_followups_locked(
                directory, rec, live_facts=current_facts, conv_id=conv_id,
            )
            if (rec['turn_active']
                    or _idle_flushed_active(rec['idle_flushed'], rec['idle_flushed_at'])
                    or _wake_in_flight(rec['entries'], conv_id)):
                if reaped:
                    _save_record_locked(queue_path, launch_id, rec)
                if cancelled:
                    _sync_runtime_queue_state_locked(
                        directory, launch_id, conv_id, rec['entries'],
                        root=_universe_root(directory), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                    )
                return False
            head = _next_queued_entry(rec['entries'], conv_id)
            if head is None:
                if reaped:
                    _save_record_locked(queue_path, launch_id, rec)
                if cancelled:
                    _sync_runtime_queue_state_locked(
                        directory, launch_id, conv_id, rec['entries'],
                        root=_universe_root(directory), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                    )
                return False
            claimed_id = head['id']
            stamped = now()
            head['state'] = 'wake_pending'
            head['claimed_at'] = stamped
            head['claim_token'] = claim_token
            rec['idle_flushed'] = True
            rec['idle_flushed_at'] = stamped
            rec['pending_idle_attempt'] = claimed_id
            rec['pending_idle_attempt_at'] = stamped
            rec['turn_active'] = False
            rec['turn_active_at'] = None
            rec['idle_after_turn'] = 0
            rec['idle_after_turn_at'] = None
            _save_record_locked(queue_path, launch_id, rec)
        if claimed_id is None:
            return False

        executable = 'agy'
        try:
            req = launch_files.read_request(directory, launch_id)
            argv = req.get('argv')
            if isinstance(argv, list) and argv and isinstance(argv[0], str) and argv[0]:
                executable = argv[0]
        except (OSError, ValueError):
            pass

        child_env = {k: v for k, v in os.environ.items()
                     if not k.startswith('MV2_') and k not in PARENT_AGY}
        child_env['ANTIGRAVITY_LS_ADDRESS'] = pinned_address
        child_env['ANTIGRAVITY_CSRF_TOKEN'] = ls_info['csrf_token']
        workspace = properties.get('workspace')
        cwd = workspace if isinstance(workspace, str) and os.path.isdir(workspace) else None
        try:
            completed = subprocess.run(
                [executable, 'agentapi', 'send-message', conv_id, WAKE_PING],
                cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=10,
            )
        except OSError:
            release_wake_claim(directory, launch_id, claimed_id, claim_token=claim_token, reset_idle_flush=True)
            return False
        except subprocess.SubprocessError:
            mark_wake_sent(directory, launch_id, claimed_id, claim_token=claim_token)
            return True

        if completed.returncode != 0:
            if is_pre_connect_agentapi_error(completed.stdout, completed.stderr):
                release_wake_claim(directory, launch_id, claimed_id, claim_token=claim_token, reset_idle_flush=True)
                return False
            mark_wake_sent(directory, launch_id, claimed_id, claim_token=claim_token)
            return True

        mark_wake_sent(directory, launch_id, claimed_id, claim_token=claim_token)
        return True
    except Exception:
        return False


def _inspect_idle_followup(directory: Path, launch_id: str, conv_id: str, *,
                           observed_idle: bool = False, live_facts: dict | None = None,
                           sync_queue_state: bool = True) -> tuple[bool, bool, str | None]:
    """Re-check/reap bounded queue state on idle; returns `(flushable, cancelled, closed_turn_id)`."""
    queue_path, lock_path = _followups_paths(directory, launch_id)
    if not queue_path.exists():
        return False, False, None
    cancelled = False
    closed_turn = None
    try:
        with lock(lock_path, create_parent=False):
            rec = _load_followups_record_locked(queue_path)
            reaped, cancelled, closed_turn = _reap_followups_locked(
                directory, rec, live_facts=live_facts, observed_idle=observed_idle, conv_id=conv_id,
            )
            if reaped:
                _save_record_locked(queue_path, launch_id, rec)
            if cancelled and sync_queue_state:
                _sync_runtime_queue_state_locked(
                    directory, launch_id, conv_id, rec['entries'],
                    root=_universe_root(directory), turn_edges=_turn_end_edges(closed_turn, 'interrupted'),
                )
    except (OSError, ValueError):
        return False, False, None
    if rec['turn_active'] or _idle_flushed_active(rec['idle_flushed'], rec['idle_flushed_at']):
        return False, cancelled, closed_turn
    if _wake_in_flight(rec['entries'], conv_id):
        return False, cancelled, closed_turn
    if _next_queued_entry(rec['entries'], conv_id) is None:
        return False, cancelled, closed_turn
    if read_ls_info(directory, launch_id) is None:
        return False, cancelled, closed_turn
    return True, cancelled, closed_turn


def _has_flushable_followup(directory: Path, launch_id: str, conv_id: str, *,
                            observed_idle: bool = False, live_facts: dict | None = None) -> bool:
    """Re-check/reap bounded queue state under lock before spawning the detached `flush-idle` helper."""
    flushable, _, _ = _inspect_idle_followup(
        directory, launch_id, conv_id, observed_idle=observed_idle, live_facts=live_facts, sync_queue_state=True,
    )
    return flushable


def kick_idle_flush_if_ready(agent_dir, launch_id: str, conv_id: str) -> bool:
    """Spawn detached `flush-idle` if the session is currently idle and a queued FIFO head is flushable."""
    directory = Path(agent_dir)
    try:
        facts = retry_missing(lambda: read(directory / 'runtime.json'))
        properties = retry_missing(lambda: read(directory / 'properties.json'))
    except (OSError, ValueError):
        return False
    if (facts.get('launch_id') != launch_id or facts.get('state') not in ('starting', 'running')
            or facts.get('status') != 'idle'
            or facts.get('ending_at') or facts.get('cleanup_retired')
            or facts.get('binding_conflict')
            or properties.get('native_session_id') != conv_id):
        return False
    if not _has_flushable_followup(directory, launch_id, conv_id, live_facts=facts):
        return False
    root = directory.parent.parent.parent
    if not (root / 'universe.json').exists():
        return False
    return _spawn_idle_flush({
        'MV2_UNIVERSE': str(root),
        'MV2_AGENT_ID': directory.name,
        'MV2_LAUNCH_ID': launch_id,
    })


def _spawn_idle_flush(source) -> bool:
    """Launch `flush-idle` in its own session so statusLine returns at once and agy cannot cancel in-flight bookkeeping."""
    helper_env = {k: v for k, v in os.environ.items() if k not in PARENT_AGY}
    for key, value in source.items():
        if key not in PARENT_AGY:
            helper_env[key] = value
    src_root = str(Path(__file__).resolve().parents[3])
    existing_pp = helper_env.get('PYTHONPATH', '')
    if src_root not in existing_pp.split(os.pathsep):
        helper_env['PYTHONPATH'] = f'{src_root}{os.pathsep}{existing_pp}' if existing_pp else src_root
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), 'flush-idle'],
            env=helper_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except OSError:
        return False


def handle_flush_idle(env=None) -> bool:
    """Entry point for the detached `flush-idle` helper process."""
    source = os.environ if env is None else env
    root_raw = source.get('MV2_UNIVERSE')
    agent_id = source.get('MV2_AGENT_ID')
    launch_id = source.get('MV2_LAUNCH_ID')
    if not (root_raw and agent_id and launch_id):
        return False
    root = Path(root_raw)
    from multiverse.universe import agents
    directory = agents.directory(root, agent_id, allow_damaged=True)
    properties = retry_missing(lambda: read(directory / 'properties.json'))
    facts = retry_missing(lambda: read(directory / 'runtime.json'))
    if (facts.get('launch_id') != launch_id or facts.get('state') not in ('starting', 'running')
            or facts.get('status') != 'idle'
            or facts.get('ending_at') or facts.get('cleanup_retired')
            or facts.get('binding_conflict')):
        return False
    conv_id = properties.get('native_session_id')
    if not isinstance(conv_id, str) or not conv_id:
        return False
    return _flush_idle_followup(directory, launch_id, properties, conv_id)


def _ensure_bridge_ready(directory: Path, launch_id: str, facts: dict) -> None:
    """Mark a bridged Antigravity launch ready once hooks or statusLine bind its conversation."""
    if (facts.get('native_bridge') == BRIDGE
            or (facts.get('native_mode') or {}).get('mode') != 'bridge'
            or (facts.get('native_mode') or {}).get('bridge') != BRIDGE
            or not facts.get('control_endpoint')
            or facts.get('binding_conflict')):
        return
    try:
        from multiverse.universe import agents
        agents.mutate_runtime(
            directory, launch_id,
            lambda cur: (
                {'native_bridge': BRIDGE}
                if (cur.get('native_bridge') != BRIDGE
                    and (cur.get('native_mode') or {}).get('mode') == 'bridge'
                    and (cur.get('native_mode') or {}).get('bridge') == BRIDGE
                    and cur.get('control_endpoint')
                    and not cur.get('binding_conflict'))
                else None
            ),
        )
    except (OSError, ValueError):
        pass


def handle_statusline(payload: dict, env=None) -> str:
    """Process one Antigravity statusLine JSON event and return the rendered line."""
    source = os.environ if env is None else env
    root_raw = source.get('MV2_UNIVERSE')
    agent_id = source.get('MV2_AGENT_ID')
    launch_id = source.get('MV2_LAUNCH_ID')
    if not (root_raw and agent_id and launch_id):
        return ''
    root = Path(root_raw)
    from multiverse.universe import agents
    directory = agents.directory(root, agent_id, allow_damaged=True)
    properties = retry_missing(lambda: read(directory / 'properties.json'))
    facts = retry_missing(lambda: read(directory / 'runtime.json'))
    if (facts.get('launch_id') != launch_id or facts.get('state') not in ('starting', 'running')
            or facts.get('ending_at') or facts.get('cleanup_retired')):
        return ''
    if facts.get('binding_conflict'):
        return ''

    raw_conv = payload.get('conversation_id') or payload.get('session_id')
    if isinstance(raw_conv, str) and raw_conv.strip():
        try:
            conv_id = normalize_session_id(raw_conv.strip())
        except ValueError:
            conv_id = None
        if conv_id is not None:
            outcome = _observe_conversation(root, directory, launch_id, properties, conv_id, source)
            if outcome != 'ok':
                return _status_line_text(directory, properties, facts) if outcome == 'ignore' else ''
            _ensure_bridge_ready(directory, launch_id, facts)
            properties = retry_missing(lambda: read(directory / 'properties.json'))
            facts = retry_missing(lambda: read(directory / 'runtime.json'))

    status = _map_status(payload)
    usage = _extract_usage(payload)
    fingerprint = {'status': status, 'usage': usage}
    cache_path = directory / 'launches' / launch_id / STATUS_CACHE_FILE
    cached = _read_private_json(cache_path, require_private_mode=True)
    changes = {}
    if status != facts.get('status'):
        changes['status'] = status
    if usage is not None and usage != (cached.get('usage') if isinstance(cached, dict) else None):
        changes['usage'] = {**usage, 'reported_at': now()}
    bound_conv = properties.get('native_session_id')
    flushable = False
    idle_turn_edges = ()
    if status == 'idle' and isinstance(bound_conv, str) and bound_conv:
        # Check idle reap BEFORE publishing status='idle' so Esc-cancel TurnEnded precedes agent.status(to='idle').
        flushable, cancelled, closed_turn = _inspect_idle_followup(
            directory, launch_id, bound_conv, observed_idle=True, sync_queue_state=False,
        )
        idle_turn_edges = _turn_end_edges(closed_turn, 'interrupted')
        if cancelled or closed_turn is not None:
            queue_path, lock_path = _followups_paths(directory, launch_id)
            if queue_path.exists():
                try:
                    with lock(lock_path, create_parent=False):
                        rec_after = _load_followups_record_locked(queue_path)
                    _sync_runtime_queue_state_locked(
                        directory, launch_id, bound_conv, rec_after['entries'],
                        root=root, turn_edges=idle_turn_edges,
                    )
                    idle_turn_edges = ()
                except (OSError, ValueError):
                    pass
    if changes or idle_turn_edges:
        turn_batch = (root, bound_conv, idle_turn_edges) if (idle_turn_edges and isinstance(bound_conv, str) and bound_conv) else None
        agents.mutate_runtime(
            directory, launch_id,
            lambda _facts: changes if changes else None,
            status_observed='status' in changes,
            turn_batch=turn_batch,
        )
    if cached != fingerprint:
        _write_private_json(cache_path, fingerprint)
    if status != 'idle':
        _clear_idle_flushed(directory, launch_id)
    elif flushable:
        _spawn_idle_flush(source)
    return _status_line_text(directory, properties, facts)


def handle_hook(event: str, payload: dict, env=None, *, expected_launch_id: str | None = None) -> dict:
    """Process one Antigravity hook event (`SessionStart`, `PreInvocation`, `Stop`)."""
    source = os.environ if env is None else env
    root_raw = source.get('MV2_UNIVERSE')
    agent_id = source.get('MV2_AGENT_ID')
    launch_id = source.get('MV2_LAUNCH_ID')
    if not (root_raw and agent_id and launch_id):
        return {}
    if expected_launch_id is not None:
        try:
            if normalize_session_id(expected_launch_id) != normalize_session_id(launch_id):
                return {}
        except ValueError:
            return {}
    root = Path(root_raw)
    from multiverse.universe import agents
    directory = agents.directory(root, agent_id, allow_damaged=True)
    properties = retry_missing(lambda: read(directory / 'properties.json'))
    facts = retry_missing(lambda: read(directory / 'runtime.json'))
    if (facts.get('launch_id') != launch_id or facts.get('state') not in ('starting', 'running')
            or facts.get('ending_at') or facts.get('cleanup_retired')):
        return {}

    raw_conv = payload.get('conversationId') or source.get('ANTIGRAVITY_CONVERSATION_ID')
    conv_id = None
    if isinstance(raw_conv, str) and raw_conv.strip():
        try:
            conv_id = normalize_session_id(raw_conv.strip())
        except ValueError:
            conv_id = None

    if event in ('SessionStart', 'PreInvocation'):
        outcome = 'ok'
        if conv_id is not None:
            outcome = _observe_conversation(root, directory, launch_id, properties, conv_id, source)
        if outcome == 'ok':
            target_conv = conv_id or retry_missing(lambda: read(directory / 'properties.json')).get('native_session_id')
            if target_conv:
                _ensure_bridge_ready(directory, launch_id, facts)
        if outcome == 'ok' and event == 'SessionStart':
            # Clear any stale paused queue_state copied from a prior ended launch on resume.
            existing_qs = facts.get('queue_state')
            if isinstance(existing_qs, dict) and existing_qs.get('launch_id') != launch_id:
                _sync_runtime_queue_state_locked(directory, launch_id, conv_id, [], clear_stale_launch=True)
        if outcome == 'ok' and event == 'PreInvocation':
            target_conv = conv_id or retry_missing(lambda: read(directory / 'properties.json')).get('native_session_id')
            start_edges, injected_texts = _on_turn_start(
                directory, launch_id, target_conv,
                invocation_num=payload.get('invocationNum'),
                initial_num_steps=payload.get('initialNumSteps'),
                env=source,
            )
            turn_batch = (root, target_conv, start_edges) if (start_edges and target_conv) else None
            need_status = facts.get('status') not in ('working', 'waiting')
            if need_status or turn_batch is not None:
                agents.mutate_runtime(
                    directory, launch_id,
                    lambda current: {'status': 'working'} if current.get('status') not in ('working', 'waiting') else None,
                    status_observed=True,
                    turn_batch=turn_batch,
                )
            if injected_texts:
                return {'injectSteps': [{'userMessage': text} for text in injected_texts]}
        return {}

    if event == 'PostInvocation':
        if facts.get('binding_conflict'):
            return {}
        target_conv = conv_id or properties.get('native_session_id')
        if not target_conv or properties.get('native_session_id') != target_conv:
            return {}
        initial_steps = payload.get('initialNumSteps')
        initial_steps = initial_steps if type(initial_steps) is int and initial_steps >= 1 else None
        tail_rows = _read_transcript_tail(
            directory, target_conv, source,
            covers=(lambda rows: any(row.get('type') != 'SYSTEM_MESSAGE' and type(row.get('step_index')) is int
                                     and row['step_index'] >= initial_steps for row in rows))
            if initial_steps is not None else None,
        )
        turn_ending, end_step = _is_turn_ending_planner_response(tail_rows, initial_steps)
        if not turn_ending:
            return {}
        queue_path, lock_path = _followups_paths(directory, launch_id)
        if not queue_path.exists():
            return {}
        with lock(lock_path):
            current_facts = retry_missing(lambda: read(directory / 'runtime.json'))
            current_props = retry_missing(lambda: read(directory / 'properties.json'))
            if not _hook_delivery_live(current_facts, current_props, launch_id, target_conv):
                return {}
            rec = _load_followups_record_locked(queue_path)
            if end_step is not None and rec.get('last_post_end_step') == end_step:
                return {}
            reaped, _, _ = _reap_followups_locked(
                directory, rec, live_facts=current_facts, conv_id=target_conv, pause_on_idle_reap=False,
            )
            post_streak = rec.get('post_continue_streak', 0)
            chosen = _next_queued_entry(rec['entries'], target_conv) if post_streak < MAX_POST_CONTINUATIONS else None
            if chosen is None:
                if reaped:
                    _save_record_locked(queue_path, launch_id, rec)
                return {}
            text = chosen['text']
            # Persist receipt delivery BEFORE transitioning the queue entry so an OSError keeps it queued.
            mark_receipt_delivered(directory, chosen['id'])
            chosen['state'] = 'handed_off'
            chosen['handed_off_at'] = now()
            chosen.pop('text', None)
            closed_turn = rec.get('turn_id') if rec.get('turn_open') else None
            end_edges = _turn_end_edges(closed_turn, 'finished')
            rec['idle_flushed'] = False
            rec['idle_flushed_at'] = None
            rec['pending_idle_attempt'] = None
            rec['pending_idle_attempt_at'] = None
            rec['turn_seq'] = rec['turn_seq'] + 1
            rec['turn_active'] = True
            rec['turn_active_at'] = now()
            rec['idle_after_turn'] = 0
            rec['idle_after_turn_at'] = None
            rec['turn_open'] = False
            rec['turn_id'] = None
            rec['pending_turn_attempt'] = chosen['id']
            rec['pending_turn_source'] = 'harness'
            rec['post_continue_streak'] = post_streak + 1
            if end_step is not None:
                rec['last_post_end_step'] = end_step
            _save_record_locked(queue_path, launch_id, rec)
            _sync_runtime_queue_state_locked(
                directory, launch_id, target_conv, rec['entries'],
                root=root, turn_edges=end_edges,
            )
            return {
                'injectSteps': [{'userMessage': text}],
                'terminationBehavior': 'force_continue',
            }

    if event == 'Stop':
        if facts.get('binding_conflict'):
            return {}
        target_conv = conv_id or properties.get('native_session_id')
        if not target_conv or properties.get('native_session_id') != target_conv:
            return {}
        reason = str(payload.get('terminationReason') or '').upper()
        if 'CANCEL' in reason:
            _pause_followups(
                directory, launch_id, target_conv, reason='cancel', turn_active=False,
                root=root, end_state='interrupted',
            )
            return {}
        if 'ERROR' in reason or payload.get('error'):
            _pause_followups(
                directory, launch_id, target_conv, reason='error', turn_active=False,
                root=root, end_state='error',
            )
            return {}
        queue_path, lock_path = _followups_paths(directory, launch_id)
        if not queue_path.exists():
            return {}
        should_flush = False
        with lock(lock_path):
            current_facts = retry_missing(lambda: read(directory / 'runtime.json'))
            current_props = retry_missing(lambda: read(directory / 'properties.json'))
            if (current_facts.get('launch_id') != launch_id
                    or current_facts.get('state') not in ('starting', 'running')
                    or current_facts.get('ending_at')
                    or current_facts.get('cleanup_retired')
                    or current_facts.get('binding_conflict')
                    or current_props.get('native_session_id') != target_conv):
                return {}
            rec = _load_followups_record_locked(queue_path)
            reaped, _, _ = _reap_followups_locked(
                directory, rec, live_facts=current_facts, conv_id=target_conv, pause_on_idle_reap=False,
            )
            closed_turn = rec.get('turn_id') if rec.get('turn_open') else None
            end_edges = _turn_end_edges(closed_turn, 'finished')
            # Background work still running (fullyIdle false) does not hold messages back, but the next turn
            # it starts on its own is a task notification, so it is the harness's turn, not the person's.
            background = payload.get('fullyIdle') is False and rec.get('pending_turn_source') != 'harness'
            if background:
                rec['pending_turn_source'] = 'harness'
            # A continuation that never reached PreInvocation must not claim the next turn.
            stale_attempt = rec.get('pending_turn_attempt') is not None
            if stale_attempt:
                rec['pending_turn_attempt'] = None
                if payload.get('fullyIdle') is not False:
                    rec['pending_turn_source'] = None
            if (reaped or background or stale_attempt or rec['idle_flushed'] or rec['turn_active']
                    or rec['idle_after_turn'] > 0 or rec.get('turn_open')
                    or rec.get('post_continue_streak', 0) > 0):
                rec['idle_flushed'] = False
                rec['idle_flushed_at'] = None
                rec['pending_idle_attempt'] = None
                rec['pending_idle_attempt_at'] = None
                rec['turn_seq'] = rec['turn_seq'] + 1
                rec['turn_active'] = False
                rec['turn_active_at'] = None
                rec['idle_after_turn'] = 0
                rec['idle_after_turn_at'] = None
                rec['turn_open'] = False
                rec['turn_id'] = None
                rec['post_continue_streak'] = 0
                _save_record_locked(queue_path, launch_id, rec)
            _sync_runtime_queue_state_locked(
                directory, launch_id, target_conv, rec['entries'],
                root=root, turn_edges=end_edges,
            )
            should_flush = (
                not _wake_in_flight(rec['entries'], target_conv)
                and _next_queued_entry(rec['entries'], target_conv) is not None
                and read_ls_info(directory, launch_id) is not None
            )
        if should_flush:
            _spawn_idle_flush(source)
        return {}

    return {}


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    mode = args[0] if args else ''
    if mode == 'flush-idle':
        try:
            handle_flush_idle()
        except Exception:
            pass
        return 0
    try:
        raw = sys.stdin.read(1 << 20)
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    if mode == 'statusline':
        try:
            line = handle_statusline(payload)
            if line:
                print(line, flush=True)
        except Exception:
            pass  # Never fail non-zero: 30 consecutive failures disable Antigravity's statusLine.
        return 0

    if mode == 'hook':
        event = args[1] if len(args) > 1 else ''
        expected_launch_id = None
        idx = 2
        while idx < len(args):
            if args[idx] == '--launch-id' and idx + 1 < len(args):
                expected_launch_id = args[idx + 1]
                idx += 2
                continue
            if args[idx].startswith('--launch-id='):
                expected_launch_id = args[idx].split('=', 1)[1]
            idx += 1
        try:
            response = handle_hook(event, payload, expected_launch_id=expected_launch_id)
        except Exception:
            response = {}
        print(json.dumps(response, ensure_ascii=False), flush=True)
        return 0

    return 0


if __name__ == '__main__':
    sys.exit(main())
