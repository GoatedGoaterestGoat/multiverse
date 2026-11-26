"""Antigravity CLI local storage, transcript usage and delivery confirmation helpers.

Antigravity CLI always stores conversations under `$HOME/.gemini/antigravity-cli/`:
- `conversations/<uuid>.db`
- `brain/<uuid>/.system_generated/logs/transcript.jsonl`
- `brain/<uuid>/.system_generated/logs/transcript_full.jsonl`
"""
from collections.abc import Sequence
from datetime import datetime, timedelta
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ...store import now
from ..types import NativeResult, Usage, normalize_session_id


AGED = re.compile(r' \(sent \d+ (?:min|hours?|days?) ago\)(?=:$)')
SENDER = re.compile(r'^Message from .+:$')
MAX_TRANSCRIPT_LINE = 16 << 20
_SQLITE_HEADER = b'SQLite format 3\x00'
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_USER_REQUEST_BLOCK = re.compile(r'<USER_REQUEST>\s*((?:(?!<USER_REQUEST>).)*?)\s*</USER_REQUEST>', re.DOTALL)
_SYSTEM_MESSAGE_BLOCK = re.compile(r'<SYSTEM_MESSAGE>\s*((?:(?!<SYSTEM_MESSAGE>).)*?)\s*</SYSTEM_MESSAGE>', re.DOTALL)
_MESSAGE_ENVELOPE = re.compile(r'^\[Message\]\s+((?:[^\n]*?\s)?)content=(.*)$', re.DOTALL)
_ENVELOPE_SENDER = re.compile(r'(?:^|\s)sender=(\S+)')
_ENVELOPE_PRIORITY = re.compile(r'(?:^|\s)priority=(\S+)')
_RESTART_NOTICE_PREFIX = '[Notice] All your subagents and background tasks have been stopped due to server restart'
_STOP_HOOK_ENVELOPE = re.compile(r'^Stop hook blocked termination:\s*(.*)$', re.DOTALL)
WAKE_PING = '[mv2] Check the user message injected in this turn (if none is present, ignore this wake ping).'


def agy_home(environment=None) -> Path:
    """Antigravity CLI storage root for `environment`: `<HOME>/.gemini/antigravity-cli`."""
    source = os.environ if environment is None else environment
    home = source.get('HOME') if isinstance(source, dict) or hasattr(source, 'get') else None
    if isinstance(home, str) and home:
        return Path(home) / '.gemini' / 'antigravity-cli'
    return Path.home() / '.gemini' / 'antigravity-cli'


def recorded_home(session, environment=None) -> str | None:
    """Recorded storage root for `session`, else from `environment`'s HOME; never leaks caller HOME when pinned."""
    home = getattr(session, 'home', None)
    if isinstance(home, str) and os.path.isabs(home):
        return home
    if environment is not None:
        base = environment.get('HOME')
        if isinstance(base, str) and os.path.isabs(base):
            return os.path.realpath(Path(base) / '.gemini' / 'antigravity-cli')
        return None
    return os.path.realpath(agy_home())


def conversation_db(native_id: str, home) -> Path:
    return Path(home) / 'conversations' / f'{normalize_session_id(native_id)}.db'


def brain_dir(native_id: str, home) -> Path:
    return Path(home) / 'brain' / normalize_session_id(native_id)


def transcript_paths(native_id: str, home) -> tuple[Path, Path]:
    logs = brain_dir(native_id, home) / '.system_generated' / 'logs'
    return logs / 'transcript_full.jsonl', logs / 'transcript.jsonl'


def moment(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def without_age(text: str) -> str:
    """Strip mv2's age suffix from the sender line so framed texts compare consistently."""
    first, newline, rest = text.partition('\n')
    return AGED.sub('', first, count=1) + newline + rest if SENDER.match(first) else text


def message_hash(text: str) -> str:
    return hashlib.sha256(without_age(text).strip().encode('utf-8')).hexdigest()


def _open_under_home(root: Path, *parts: str):
    """Open a regular non-symlink file strictly under `root`, walking components with O_NOFOLLOW.

    Returns an open binary file object, None when absent/symlink/non-regular, or raises OSError when unreadable.
    """
    try:
        cur = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=cur)
            except OSError as error:
                if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                    return None
                raise
            os.close(cur)
            cur = child
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=cur)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                return None
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return None
            return os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(cur)


def _iter_jsonl_handle(handle):
    """Yield (row_dict | None) for each line; None marks a damaged or unfinished line."""
    while True:
        raw = handle.readline(MAX_TRANSCRIPT_LINE + 1)
        if not raw:
            break
        if len(raw) > MAX_TRANSCRIPT_LINE:
            while raw and not raw.endswith(b'\n'):
                raw = handle.readline(MAX_TRANSCRIPT_LINE)
            yield None
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            yield None
            continue
        yield parsed if isinstance(parsed, dict) else None


def _has_valid_db(canonical: str, root: Path) -> bool | None:
    """True if `conversations/<id>.db` is a regular SQLite file, False if absent/invalid, None if unreadable."""
    try:
        handle = _open_under_home(root, 'conversations', f'{canonical}.db')
    except OSError:
        return None
    if handle is None:
        return False
    try:
        with handle:
            header = handle.read(len(_SQLITE_HEADER))
            return header == _SQLITE_HEADER
    except OSError:
        return None


def _conversations_dir_readable(root: Path) -> bool | None:
    """True when `root/conversations` is a readable non-symlink directory, False when absent, None when unreadable/symlink."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return False
        return None
    try:
        try:
            info = os.stat('conversations', dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
        if not stat.S_ISDIR(info.st_mode):
            return False
        try:
            conv_fd = os.open('conversations', _DIR_FLAGS, dir_fd=root_fd)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return False
            return None
        try:
            os.listdir(conv_fd)
            return True
        except OSError:
            return None
        finally:
            os.close(conv_fd)
    finally:
        os.close(root_fd)


def conversation_exists(native_id: str, home) -> bool | None:
    """True when Antigravity has a valid saved `conversations/<id>.db` for `native_id`,
    False when definitely absent (even if a `brain/` transcript survives), None when storage cannot be read."""
    canonical = normalize_session_id(native_id)
    root = Path(home)
    db_ok = _has_valid_db(canonical, root)
    if db_ok is not False:
        return db_ok
    if _conversations_dir_readable(root) is None:
        return None
    return False


def session_storage_evidence(session, home) -> NativeResult:
    """Read-only exact conversation DB evidence under `home`, matching mv2's storage-adoption contract."""
    if not session.native_id:
        return NativeResult('unknown', 'No Antigravity session ID to look for')
    if not isinstance(home, str) or not os.path.isabs(home):
        return NativeResult('unknown', f'Antigravity storage {home!r} is not an absolute path')
    try:
        canonical = normalize_session_id(session.native_id)
    except ValueError:
        return NativeResult('unknown', 'Antigravity session ID is not a UUID')
    root = Path(home)
    db = root / 'conversations' / f'{canonical}.db'
    db_ok = _has_valid_db(canonical, root)
    if db_ok is True:
        return NativeResult('ok', value={'present': True})
    if db_ok is None:
        return NativeResult('unknown', f"Antigravity's conversation file {db} can't be read")
    folder = root / 'conversations'
    readable = _conversations_dir_readable(root)
    if readable is False:
        return NativeResult('ok', f"Antigravity's conversation store {folder} doesn't exist",
                            value={'present': False, 'store_read': False})
    if readable is None:
        return NativeResult('unknown', f"Antigravity's storage folder {folder} can't be read")
    return NativeResult('ok', value={'present': False, 'store_read': True})


def _nonneg_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _open_transcript(canonical: str, home, *, prefer_full: bool):
    """Open the preferred existing transcript under `home`, returning handle or None (raises OSError on unreadable)."""
    root = Path(home)
    order = ('transcript_full.jsonl', 'transcript.jsonl') if prefer_full else ('transcript.jsonl', 'transcript_full.jsonl')
    for name in order:
        handle = _open_under_home(root, 'brain', canonical, '.system_generated', 'logs', name)
        if handle is not None:
            return handle
    return None


def measured_usage(native_id: str, home) -> tuple[Usage | None, dict]:
    """Read cumulative session usage and latest turn metadata from Antigravity's transcript.jsonl."""
    canonical = normalize_session_id(native_id)
    handle = _open_transcript(canonical, home, prefer_full=False)
    if handle is None:
        return None, {}
    total_input = 0
    total_output = 0
    total_cached = 0
    total_cache_write = 0
    saw_usage = False
    saw_cached = False
    saw_cache_write = False
    complete = True
    latest_model = None
    latest_observed_at = None
    latest_context_tokens = None
    with handle:
        for row in _iter_jsonl_handle(handle):
            if row is None:
                complete = False
                continue
            if row.get('type') != 'PLANNER_RESPONSE':
                continue
            usage_block = row.get('usage') if isinstance(row.get('usage'), dict) else row
            uncached = _nonneg_int(usage_block.get('input_tokens'))
            cached = _nonneg_int(usage_block.get('cache_read_tokens'))
            if cached is None:
                cached = _nonneg_int(usage_block.get('cache_read_input_tokens'))
            cache_write = _nonneg_int(usage_block.get('cache_write_tokens'))
            if cache_write is None:
                cache_write = _nonneg_int(usage_block.get('cache_creation_input_tokens'))
            output = _nonneg_int(usage_block.get('output_tokens'))
            if any(value is not None for value in (uncached, cached, cache_write, output)):
                saw_usage = True
                step_input = (uncached or 0) + (cached or 0) + (cache_write or 0)
                total_input += step_input
                total_output += output or 0
                if cached is not None:
                    total_cached += cached
                    saw_cached = True
                if cache_write is not None:
                    total_cache_write += cache_write
                    saw_cache_write = True
                latest_context_tokens = step_input
            model = row.get('model')
            created_at = row.get('created_at')
            if isinstance(model, str) and model.strip() and not any(c in model for c in ('\0', '\n', '\r')):
                latest_model = model
            if moment(created_at) is not None:
                latest_observed_at = created_at
    if not saw_usage:
        return None, {}
    from . import models as agy_models
    model_id, effort = agy_models.split_model_effort(latest_model)
    usage = Usage(
        native_session_id=canonical,
        input_tokens=total_input,
        output_tokens=total_output,
        total_tokens=total_input + total_output,
        source='agy-transcript',
        reported_at=now(),
        cached_input_tokens=total_cached if saw_cached else None,
        cache_write_tokens=total_cache_write if saw_cache_write else None,
        complete=complete,
        model=model_id,
        effort=effort,
        observed_at=latest_observed_at,
    )
    meta = {}
    if latest_context_tokens is not None:
        meta['context_tokens'] = latest_context_tokens
    if latest_observed_at is not None:
        meta['responded_at'] = latest_observed_at
    return usage, meta


def _row_content(row: dict) -> str | None:
    if row.get('type') not in ('SYSTEM_MESSAGE', 'USER_INPUT'):
        return None
    for key in ('content', 'text', 'message'):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_agentapi_envelope(sender: str | None, priority: str | None, body: str) -> bool:
    """True only for `agentapi send-message` rows carrying a message (not restart notices or internal wake pings)."""
    if sender != 'system':
        return False
    if priority not in (None, 'MESSAGE_PRIORITY_HIGH', 'MESSAGE_PRIORITY_NORMAL'):
        return False
    stripped = body.lstrip()
    if stripped.startswith(_RESTART_NOTICE_PREFIX):
        return False
    if body.strip() == WAKE_PING:
        return False
    return True


def _extract_message_envelopes(row: dict) -> tuple[tuple[str | None, str | None, str], ...]:
    """Extract `(sender, priority, content)` triples from Antigravity `[Message] ... content=` SYSTEM_MESSAGE rows."""
    if row.get('type') != 'SYSTEM_MESSAGE':
        return ()
    content = _row_content(row)
    if content is None:
        return ()
    blocks = [match.group(1).strip() for match in _SYSTEM_MESSAGE_BLOCK.finditer(content)]
    if not blocks:
        blocks = [content.strip()]
    found = []
    for block in blocks:
        envelope = _MESSAGE_ENVELOPE.match(block)
        if envelope:
            header = envelope.group(1)
            sender_match = _ENVELOPE_SENDER.search(header)
            priority_match = _ENVELOPE_PRIORITY.search(header)
            sender = sender_match.group(1) if sender_match else None
            priority = priority_match.group(1) if priority_match else None
            found.append((sender, priority, envelope.group(2)))
    return tuple(found)


def _extracted_candidates(row: dict, route: str | None = None) -> tuple[str, ...]:
    """Candidate message payloads inside Antigravity's transcript row envelopes, constrained by `route`.

    - `route == 'hook-user-input'`: only `USER_INPUT` rows with `source == 'SYSTEM_SDK'` (injected by
      `PreInvocation` / `PostInvocation` hooks, never human `USER_EXPLICIT` prompts).
    - `route == 'agentapi'`: only `SYSTEM_MESSAGE` rows carrying `[Message] ... sender=system ... content=<payload>`
      with high/unset priority (excluding `MESSAGE_PRIORITY_LOW` server-restart notices and internal wake pings).
    - `route == 'stop-hook'`: only `SYSTEM_MESSAGE` rows carrying `Stop hook blocked termination: <payload>`.
    - `route is None`: any `USER_INPUT` or `SYSTEM_MESSAGE` payload (for route-agnostic inspection).
    """
    content = _row_content(row)
    if content is None:
        return ()
    row_type = row.get('type')
    if route == 'hook-user-input':
        if row_type != 'USER_INPUT' or row.get('source') != 'SYSTEM_SDK':
            return ()
        blocks = [match.group(1) for match in _USER_REQUEST_BLOCK.finditer(content)]
        return tuple(blocks) if blocks else (content,)
    if route == 'agentapi':
        return tuple(
            body for sender, priority, body in _extract_message_envelopes(row)
            if _is_agentapi_envelope(sender, priority, body)
        )
    if route == 'stop-hook':
        if row_type != 'SYSTEM_MESSAGE':
            return ()
        candidates = []
        for match in _SYSTEM_MESSAGE_BLOCK.finditer(content):
            envelope = _STOP_HOOK_ENVELOPE.match(match.group(1).strip())
            if envelope:
                candidates.append(envelope.group(1))
        if not candidates:
            envelope = _STOP_HOOK_ENVELOPE.match(content.strip())
            if envelope:
                candidates.append(envelope.group(1))
        return tuple(candidates)

    candidates = [content]
    for pattern in (_USER_REQUEST_BLOCK, _SYSTEM_MESSAGE_BLOCK):
        for match in pattern.finditer(content):
            inner = match.group(1)
            candidates.append(inner)
            stripped = inner.strip()
            envelope = _STOP_HOOK_ENVELOPE.match(stripped)
            if envelope:
                candidates.append(envelope.group(1))
    for sender, priority, body in _extract_message_envelopes(row):
        if _is_agentapi_envelope(sender, priority, body):
            candidates.append(body)
    stripped = content.strip()
    envelope = _STOP_HOOK_ENVELOPE.match(stripped)
    if envelope:
        candidates.append(envelope.group(1))
    return tuple(candidates)


def has_transcript(native_id: str, home) -> bool | None:
    """True when a regular transcript exists for `native_id`, False when cleanly absent, None when unreadable."""
    canonical = normalize_session_id(native_id)
    try:
        handle = _open_transcript(canonical, home, prefer_full=True)
    except OSError:
        return None
    if handle is None:
        return False
    handle.close()
    return True


def _match_times(canonical: str, home, *, needle_hash: str, needle_text: str | None,
                 route: str | None) -> tuple[list[datetime], bool, bool] | None:
    """Return `(matched_timestamps, damaged, saw_undated_match)` from one authoritative transcript, or None if unreadable."""
    try:
        handle = _open_transcript(canonical, home, prefer_full=True)
    except OSError:
        return None
    if handle is None:
        return [], False, False
    matched_times = []
    damaged = False
    saw_undated_match = False
    try:
        with handle:
            for row in _iter_jsonl_handle(handle):
                if row is None:
                    damaged = True
                    continue
                matched = False
                for candidate in _extracted_candidates(row, route=route):
                    normalized = without_age(candidate).strip()
                    if hashlib.sha256(normalized.encode('utf-8')).hexdigest() == needle_hash:
                        matched = True
                        break
                    if needle_text and needle_text == normalized:
                        matched = True
                        break
                if not matched:
                    continue
                when = moment(row.get('created_at'))
                if when is None:
                    saw_undated_match = True
                else:
                    matched_times.append(when)
    except OSError:
        return None
    matched_times.sort()
    return matched_times, damaged, saw_undated_match


def _max_cohort_matches(cutoffs: Sequence[datetime], times: Sequence[datetime]) -> int:
    """Maximum bipartite matching size between `[cutoff, +inf)` intervals and sorted transcript timestamps."""
    used = 0
    matched = 0
    for cutoff in sorted(cutoffs):
        while used < len(times) and times[used] < cutoff:
            used += 1
        if used >= len(times):
            break
        matched += 1
        used += 1
    return matched


def transcript_has_message(native_id: str, home, *, text: str | None = None,
                           text_sha256: str | None = None, since: str | None = None,
                           route: str | None = None,
                           cohort_since: Sequence[str] | None = None,
                           cohort_target: int | None = None) -> bool | None:
    """Whether Antigravity's transcript confirms this message at/after `since` (and for `route` / `cohort_since`).

    Undated transcript rows can never confirm a timestamped attempt (`since` or `cohort_since`).
    In a multi-attempt identical-text cohort, confirmation succeeds only when the target attempt is
    strictly required to account for the observed qualifying transcript rows (no other subset of cohort
    attempts can explain all matched rows without it). Returns True when confirmed, False when cleanly
    read without any qualifying match for the target, and None when unreadable, damaged, or ambiguous.
    """
    if not text and not text_sha256:
        return None
    canonical = normalize_session_id(native_id)
    needle_hash = text_sha256 or message_hash(text)
    needle_text = without_age(text).strip() if text else None
    found = _match_times(canonical, home, needle_hash=needle_hash, needle_text=needle_text, route=route)
    if found is None:
        return None
    matched_times, damaged, saw_undated_match = found

    if cohort_since is not None:
        if not cohort_since:
            return None
        target_index = len(cohort_since) - 1 if cohort_target is None else cohort_target
        if not isinstance(target_index, int) or isinstance(target_index, bool) or not (0 <= target_index < len(cohort_since)):
            return None
        cutoffs = []
        for stamp in cohort_since:
            parsed = moment(stamp)
            if parsed is None:
                return None
            cutoffs.append(parsed - timedelta(seconds=1))
        target_cutoff = cutoffs[target_index]
        if not any(when >= target_cutoff for when in matched_times):
            return None if (damaged or saw_undated_match) else False
        full = _max_cohort_matches(cutoffs, matched_times)
        without_target = _max_cohort_matches(
            [*cutoffs[:target_index], *cutoffs[target_index + 1:]],
            matched_times,
        )
        if without_target < full:
            return True
        return None

    if since is not None:
        start = moment(since)
        if start is None:
            return None
        cutoff = start - timedelta(seconds=1)
        if any(when >= cutoff for when in matched_times):
            return True
        return None if (damaged or saw_undated_match) else False

    if matched_times or saw_undated_match:
        return True
    return None if damaged else False
