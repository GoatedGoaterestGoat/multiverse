"""Read Antigravity CLI's JSONL session transcripts without touching a live service.

Reads only the exact recorded conversation under `session.home`:
`brain/<uuid>/.system_generated/logs/transcript_full.jsonl`, falling back to `transcript.jsonl` when the full
log is absent. Walks path components with `O_DIRECTORY | O_NOFOLLOW` so links and special files fail closed.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from ..types import (
    RowFact, TranscriptRowsChanged, TranscriptSource, TranscriptView,
    TurnEnded, TurnProjection, TurnStarted, normalize_session_id,
)
from . import storage

BODY_LIMIT = 16 << 20
MAX_ROWS = 200_000
PREVIEW_LIMIT = 160
NO_STORAGE = "mv2 has no record of this agent's Antigravity storage; resume it once"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_TASK_SENDER = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/task-\d+$', re.IGNORECASE)


class Unreadable(ValueError):
    """Transcript storage or file cannot be read safely."""


@dataclass(frozen=True)
class _RowEntry:
    fact: RowFact
    offset: int
    size: int
    digest: str
    chain: str
    pre_logged: bool = False


@dataclass(frozen=True, kw_only=True)
class AgyView(TranscriptView):
    native_id: str
    transcript_name: str | None = None
    _entries: tuple[_RowEntry, ...] = ()
    _signature: tuple[int, int, int, int, int] | None = None


def _check(reader) -> None:
    if reader is not None:
        reader.check()


def _deadline(reader) -> None:
    if reader is not None and time.monotonic() >= getattr(reader, 'deadline', 0):
        reader.check()


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _same_prefix(before: tuple[int, int, int, int, int], after: tuple[int, int, int, int, int]) -> bool:
    return before[:2] == after[:2] and after[2] >= before[2]


def reading_home(session) -> str | None:
    """Recorded storage home for `session`; only falls back to caller env inside the agent's own session."""
    if isinstance(session.home, str) and os.path.isabs(session.home):
        return session.home
    own = os.environ.get('MV2_AGENT_DIR')
    if (not own or not session.session_file
            or os.path.realpath(own) != os.path.realpath(Path(session.session_file).parent)):
        return None
    return os.path.realpath(storage.agy_home(os.environ))


def _open_transcript_file(home: str, canonical_id: str, preferred_name: str | None = None):
    """Open the exact transcript file anchored under `home` without following links.

    Returns `(name, handle, stat_result)` or `(None, None, None)` when cleanly absent.
    Raises `Unreadable` on symlinks, non-regular files, or unreadable directories.
    """
    root = Path(home)
    try:
        cur = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None, None, None
        raise Unreadable(f"Antigravity storage {root} can't be read") from error
    try:
        for part in ('brain', canonical_id, '.system_generated', 'logs'):
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=cur)
            except OSError as error:
                if error.errno == errno.ENOENT:
                    return None, None, None
                if error.errno == errno.ELOOP:
                    raise Unreadable(f'Antigravity transcript path component {part!r} is a link') from error
                raise Unreadable(f"Antigravity transcript directory can't be read ({error})") from error
            os.close(cur)
            cur = child
        names = (preferred_name,) if preferred_name else ('transcript_full.jsonl', 'transcript.jsonl')
        for name in names:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=cur)
            except OSError as error:
                if error.errno == errno.ENOENT:
                    continue
                if error.errno == errno.ELOOP:
                    raise Unreadable(f'Antigravity transcript {name} is a link') from error
                raise Unreadable(f"Antigravity transcript {name} can't be read ({error})") from error
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise Unreadable(f'Antigravity transcript {name} is not a regular file')
                os.set_blocking(fd, True)
                return name, os.fdopen(fd, 'rb'), info
            except BaseException:
                os.close(fd)
                raise
        return None, None, None
    finally:
        os.close(cur)


def _preview(text: str | None) -> str:
    if not isinstance(text, str) or not text:
        return ''
    collapsed = ' '.join(text.split())
    if len(collapsed) <= PREVIEW_LIMIT:
        return collapsed
    return collapsed[:PREVIEW_LIMIT - 1].rstrip() + '…'


def _extract_user_preview(row: dict) -> str:
    content = row.get('content')
    if not isinstance(content, str):
        return ''
    match = storage._USER_REQUEST_BLOCK.search(content)
    return _preview(match.group(1) if match else content)


def _classify_row(row: dict, *, turn_open: bool) -> tuple[str, str, str | None, str | None, bool, str, bool, bool]:
    """Return `(kind, label, source, stop, ends_turn, preview, next_turn_open, pre_logged)` for one row.

    `pre_logged` is True for prompt rows Antigravity appends to the trajectory before firing `PreInvocation`
    (`USER_INPUT` and Stop-hook continuations), and False for `agentapi` and background task-notification
    messages, which Antigravity appends after `PreInvocation` at `step-{initialNumSteps}`.
    """
    raw_type = row.get('type')
    kind = raw_type if isinstance(raw_type, str) and raw_type.strip() and raw_type.isprintable() else 'unknown'
    raw_source = row.get('source')

    if kind == 'USER_INPUT':
        if raw_source == 'SYSTEM_SDK':
            if turn_open:
                return kind, 'mid_turn', 'plugin', None, False, '', True, False
            return kind, 'prompt', 'plugin', None, False, _extract_user_preview(row), True, True
        src = 'human' if raw_source == 'USER_EXPLICIT' else None
        return kind, 'prompt', src, None, False, _extract_user_preview(row), True, True

    if kind == 'PLANNER_RESPONSE':
        tools = row.get('tool_calls')
        has_tools = isinstance(tools, list) and len(tools) > 0
        status = str(row.get('status') or '').upper()
        stop = None
        ends_turn = False
        if status in ('CANCELLED', 'INTERRUPTED'):
            stop = 'interrupted'
            ends_turn = True
        elif not has_tools and status == 'DONE':
            text_present = any(
                isinstance(row.get(key), str) and row.get(key).strip()
                for key in ('content', 'response', 'text')
            )
            if text_present:
                stop = 'end_turn'
                ends_turn = True
        next_open = False if ends_turn else turn_open
        return kind, 'response', None, stop, ends_turn, '', next_open, False

    if kind == 'GENERIC':
        label = 'tool_result' if raw_source == 'MODEL' else 'meta'
        return kind, label, None, None, False, '', turn_open, False

    if kind == 'SYSTEM_MESSAGE':
        stop_payloads = storage._extracted_candidates(row, route='stop-hook')
        if stop_payloads:
            return kind, 'prompt', 'plugin', None, False, _preview(stop_payloads[0]), True, True
        envelopes = storage._extract_message_envelopes(row)
        if envelopes:
            system_bodies = [
                body for sender, priority, body in envelopes
                if storage._is_agentapi_envelope(sender, priority, body)
            ]
            if system_bodies:
                label = 'mid_turn' if turn_open else 'prompt'
                preview = _preview(system_bodies[0]) if label == 'prompt' else ''
                return kind, label, 'plugin', None, False, preview, True, False
            task_bodies = [
                body for sender, _priority, body in envelopes
                if isinstance(sender, str) and _TASK_SENDER.match(sender)
            ]
            if task_bodies:
                label = 'mid_turn' if turn_open else 'prompt'
                preview = _preview(task_bodies[0]) if label == 'prompt' else ''
                return kind, label, 'task-notification', None, False, preview, True, False
            return kind, 'injected', None, None, False, '', turn_open, False
        return kind, 'injected', None, None, False, '', turn_open, False

    if kind == 'CHECKPOINT':
        return kind, 'compaction', None, None, False, '', False, False

    return kind, 'meta', None, None, False, '', turn_open, False


def _empty(state: str, reason: str, *, home: str | None = None, native_id: str = '', retry: bool = False) -> AgyView:
    return AgyView(
        native_id=native_id,
        source=TranscriptSource(state=state, home=home, reason=reason, retry=retry),
        rows=(),
        history=(),
        context_from=0,
        tip=None,
        gap=None,
        history_version=0,
    )


def view(session, *, tip: str | None = None, reader=None) -> AgyView:
    """Build a metadata-only TranscriptView for `session`'s recorded Antigravity conversation."""
    _check(reader)
    if not session.native_id:
        return _empty('unavailable', 'No Antigravity conversation ID recorded')
    try:
        canonical = normalize_session_id(session.native_id)
    except ValueError:
        return _empty('error', 'Invalid Antigravity conversation ID')
    home = reading_home(session)
    if home is None:
        return _empty('unavailable', NO_STORAGE, native_id=canonical)
    try:
        name, handle, info = _open_transcript_file(home, canonical)
    except Unreadable as error:
        return _empty('error', str(error), home=home, native_id=canonical)
    if handle is None or info is None:
        return _empty(
            'unavailable',
            'Antigravity has not written a transcript for this conversation yet',
            home=home, native_id=canonical, retry=True,
        )

    entries: list[_RowEntry] = []
    turn_open = False
    last_step_index: int | None = None
    last_was_tool_call = False
    offset = 0
    chain = ''
    before_sig = _signature(info)
    try:
        with handle:
            while True:
                _deadline(reader)
                raw = handle.readline(BODY_LIMIT + 1)
                if not raw:
                    break
                if len(entries) >= MAX_ROWS:
                    raise Unreadable('Antigravity transcript exceeds the indexed row limit')
                # An oversized single line streams to its newline so its byte span and digest stay exact.
                oversized = False
                if len(raw) > BODY_LIMIT and not raw.endswith(b'\n'):
                    oversized = True
                    hasher = hashlib.sha256(raw)
                    size = len(raw)
                    while True:
                        _deadline(reader)
                        chunk = handle.readline(BODY_LIMIT)
                        if not chunk:
                            raw = b''  # Unfinished trailing line without newline: stop before admitting it.
                            break
                        hasher.update(chunk)
                        size += len(chunk)
                        if chunk.endswith(b'\n'):
                            break
                    if not raw:
                        break
                    digest = hasher.hexdigest()
                else:
                    if not raw.endswith(b'\n'):
                        break  # Unfinished trailing line waits for its newline.
                    size = len(raw)
                    digest = hashlib.sha256(raw).hexdigest()
                chain = hashlib.sha256(f'{chain}:{digest}'.encode('ascii')).hexdigest()
                position = f'{offset + size}:{chain}'

                parsed = None
                if not oversized and size <= BODY_LIMIT:
                    try:
                        value = json.loads(raw.decode('utf-8'))
                        if isinstance(value, dict):
                            parsed = value
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        parsed = None

                if parsed is None:
                    fact = RowFact(
                        position=position, ref=None, kind='unreadable', label='meta',
                        size=size, at=None, preview='',
                    )
                    pre_logged = False
                else:
                    step_index = parsed.get('step_index')
                    ref = f'step-{step_index}' if type(step_index) is int and step_index >= 0 else None
                    # An omitted step index directly after a PLANNER_RESPONSE with tool_calls (an Esc-cancelled
                    # tool call whose GENERIC step was never logged) ends the prior open turn before the next
                    # incoming SYSTEM_MESSAGE or hook-injected SYSTEM_SDK USER_INPUT prompt.
                    if (turn_open and (
                            parsed.get('type') == 'SYSTEM_MESSAGE'
                            or (parsed.get('type') == 'USER_INPUT' and parsed.get('source') == 'SYSTEM_SDK')
                    ) and last_was_tool_call
                            and type(step_index) is int and last_step_index is not None
                            and step_index > last_step_index + 1):
                        turn_open = False
                    created_at = parsed.get('created_at')
                    at = created_at if storage.moment(created_at) is not None else None
                    kind, label, source, stop, ends_turn, preview, turn_open, pre_logged = _classify_row(
                        parsed, turn_open=turn_open,
                    )
                    if type(step_index) is int and step_index >= 0:
                        last_step_index = step_index
                    tools = parsed.get('tool_calls')
                    last_was_tool_call = (kind == 'PLANNER_RESPONSE' and isinstance(tools, list) and len(tools) > 0)
                    fact = RowFact(
                        position=position, ref=ref, kind=kind, label=label,
                        source=source, stop=stop, ends_turn=ends_turn,
                        at=at, size=size, preview=preview,
                    )
                entries.append(_RowEntry(
                    fact=fact, offset=offset, size=size, digest=digest, chain=chain, pre_logged=pre_logged,
                ))
                offset += size
            after_sig = _signature(os.fstat(handle.fileno()))
            if not _same_prefix(before_sig, after_sig):
                raise Unreadable('Antigravity transcript changed while reading')
    except Unreadable as error:
        return _empty('error', str(error), home=home, native_id=canonical)
    except OSError as error:
        return _empty('error', f"Antigravity transcript can't be read ({error})", home=home, native_id=canonical)

    _check(reader)
    facts = tuple(entry.fact for entry in entries)
    cutoff = len(facts)
    if tip is not None:
        matches = [idx for idx, fact in enumerate(facts) if fact.ref == tip]
        if len(matches) == 1:
            cutoff = matches[0] + 1
            while cutoff < len(facts) and facts[cutoff].ref is None and facts[cutoff].label != 'compaction':
                cutoff += 1
    history = tuple(range(cutoff))
    context_from = max((place for place, idx in enumerate(history) if facts[idx].label == 'compaction'), default=0)
    latest_tip = next((facts[idx].ref for idx in reversed(history) if facts[idx].ref is not None), None)
    updated_at = next((fact.at for fact in reversed(facts) if fact.at is not None), None)
    if updated_at is None and info.st_mtime_ns:
        updated_at = datetime.fromtimestamp(info.st_mtime_ns / 1e9, tz=timezone.utc).isoformat()
    return AgyView(
        native_id=canonical,
        transcript_name=name,
        _entries=tuple(entries),
        _signature=before_sig,
        source=TranscriptSource(
            state='supported',
            home=home,
            format='agy-jsonl',
            size=offset,
            updated_at=updated_at,
        ),
        rows=facts,
        history=history,
        context_from=context_from,
        tip=latest_tip,
        gap=None,
        history_version=0,
    )


def _hash_span(handle, offset: int, size: int, reader=None) -> str:
    """Hash `size` bytes starting at `offset` in bounded chunks; raise TranscriptRowsChanged on short read."""
    handle.seek(offset)
    hasher = hashlib.sha256()
    remaining = size
    while remaining:
        _deadline(reader)
        chunk = handle.read(min(65536, remaining))
        if not chunk:
            raise TranscriptRowsChanged()
        hasher.update(chunk)
        remaining -= len(chunk)
    return hasher.hexdigest()


def _prove_prefix(handle, entries: tuple[_RowEntry, ...], last_index: int, reader=None) -> None:
    """Re-verify exact line hashes from byte 0 through `last_index` on the open descriptor."""
    handle.seek(0)
    for idx in range(last_index + 1):
        entry = entries[idx]
        hasher = hashlib.sha256()
        remaining = entry.size
        while remaining:
            _deadline(reader)
            chunk = handle.read(min(65536, remaining))
            if not chunk:
                raise TranscriptRowsChanged()
            hasher.update(chunk)
            remaining -= len(chunk)
        if hasher.hexdigest() != entry.digest:
            raise TranscriptRowsChanged()


def bodies(session, selected_view: TranscriptView, indexes, *, max_bytes: int, reader=None) -> list[dict | None]:
    """Read verified JSON row bodies for `indexes`, raising TranscriptRowsChanged if the prefix changed."""
    _check(reader)
    if not indexes:
        return []
    if not isinstance(selected_view, AgyView) or not selected_view.transcript_name or not selected_view._entries:
        raise TranscriptRowsChanged()
    home = reading_home(session)
    if home is None or home != selected_view.source.home:
        raise TranscriptRowsChanged()
    entries = selected_view._entries
    for number in indexes:
        if number < 0 or number >= len(entries) or entries[number].fact.position != selected_view.rows[number].position:
            raise TranscriptRowsChanged()
    last = max(indexes)
    cap = min(max_bytes, BODY_LIMIT)
    try:
        _, handle, info = _open_transcript_file(home, selected_view.native_id, preferred_name=selected_view.transcript_name)
    except Unreadable:
        raise TranscriptRowsChanged() from None
    if handle is None or info is None:
        raise TranscriptRowsChanged()
    try:
        with handle:
            stamp = _signature(os.fstat(handle.fileno()))
            if selected_view._signature is None or not _same_prefix(selected_view._signature, stamp):
                raise TranscriptRowsChanged()
            if stamp != selected_view._signature:
                _prove_prefix(handle, entries, last, reader)
            out: list[dict | None] = []
            for number in indexes:
                _check(reader)
                entry = entries[number]
                if entry.fact.kind == 'unreadable' or entry.size > cap:
                    if _hash_span(handle, entry.offset, entry.size, reader) != entry.digest:
                        raise TranscriptRowsChanged()
                    out.append(None)
                    continue
                handle.seek(entry.offset)
                raw = handle.read(entry.size)
                if len(raw) != entry.size or hashlib.sha256(raw).hexdigest() != entry.digest:
                    raise TranscriptRowsChanged()
                try:
                    parsed = json.loads(raw.decode('utf-8'))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    parsed = None
                out.append(parsed if isinstance(parsed, dict) else None)
            after = _signature(os.fstat(handle.fileno()))
            if after != stamp:
                _prove_prefix(handle, entries, last, reader)
                if not _same_prefix(stamp, _signature(os.fstat(handle.fileno()))):
                    raise TranscriptRowsChanged()
            return out
    except OSError:
        raise TranscriptRowsChanged() from None


def readable(session, *, reader=None) -> bool | None:
    """True when `session`'s transcript is readable, False when cleanly absent, None when unreadable/unknown."""
    _check(reader)
    home = reading_home(session)
    if not home or not session.native_id:
        return None
    try:
        canonical = normalize_session_id(session.native_id)
    except ValueError:
        return None
    try:
        _, handle, _ = _open_transcript_file(home, canonical)
    except Unreadable:
        return None
    if handle is None:
        return False
    handle.close()
    return True


def _step_number(ref: str | None) -> int | None:
    if not isinstance(ref, str) or not ref.startswith('step-'):
        return None
    suffix = ref[5:]
    return int(suffix) if suffix.isdecimal() else None


def _parse_pre_key(key: str | None) -> tuple[str, int] | None:
    if not isinstance(key, str) or not key.startswith(('init:', 'post:')):
        return None
    parts = key.split(':')
    if len(parts) < 2 or not parts[1].isdecimal():
        return None
    steps = int(parts[1])
    return (parts[0], steps) if steps >= 1 else None


def project_turn_event(session, selected_view: TranscriptView, transition, *, reader=None) -> TurnProjection:
    """Project one native turn transition onto exact row positions inside `selected_view`."""
    _check(reader)
    home = reading_home(session)
    if (not isinstance(selected_view, AgyView) or selected_view.source.state != 'supported'
            or not session.native_id or home is None or selected_view.source.home != home):
        return TurnProjection()
    try:
        canonical = normalize_session_id(session.native_id)
    except ValueError:
        return TurnProjection()
    if selected_view.native_id != canonical:
        return TurnProjection()
    rows = selected_view.rows
    entries = selected_view._entries
    by_ref: dict[str, int | None] = {}
    by_pos: dict[str, int] = {}
    max_step = -1
    for idx, row in enumerate(rows):
        by_pos[row.position] = idx
        if row.ref is not None:
            by_ref[row.ref] = idx if row.ref not in by_ref else None
            step_no = _step_number(row.ref)
            if step_no is not None and step_no > max_step:
                max_step = step_no

    def resolve_index(key: str | None) -> int | None:
        if not isinstance(key, str) or not key:
            return None
        if key in by_ref:
            return by_ref[key]
        if key in by_pos:
            return by_pos[key]
        return None

    def _is_pre_logged(idx: int) -> bool:
        return idx < len(entries) and entries[idx].pre_logged

    def resolve_turn_anchor(key: str | None) -> int | None:
        pre = _parse_pre_key(key)
        if pre is not None:
            mode, init_steps = pre
            if mode == 'init':
                # USER_INPUT and Stop-hook prompts (plus any trailing restart notice) are appended before
                # PreInvocation, so step-{init_steps - 1} is that pre-logged prompt or an injected/meta notice
                # immediately following it. Never walk back from a response/tool_result or a post-logged prompt.
                prev_idx = resolve_index(f'step-{init_steps - 1}')
                if prev_idx is not None:
                    if rows[prev_idx].label in ('prompt', 'command') and _is_pre_logged(prev_idx):
                        return prev_idx
                    if rows[prev_idx].label in ('injected', 'meta'):
                        cursor = prev_idx - 1
                        while cursor >= 0 and rows[cursor].label in ('injected', 'meta'):
                            cursor -= 1
                        if cursor >= 0 and rows[cursor].label in ('prompt', 'command') and _is_pre_logged(cursor):
                            return cursor
            # agentapi messages, background task notifications and PreInvocation-injected user messages are
            # appended after PreInvocation from step-{init_steps}; an mv2 wake ping there is skipped as injected.
            next_idx = resolve_index(f'step-{init_steps}')
            while next_idx is not None and next_idx < len(rows) and rows[next_idx].label in ('injected', 'meta'):
                next_idx += 1
            if next_idx is not None and next_idx < len(rows) and rows[next_idx].label in ('prompt', 'command'):
                return next_idx
            return None

        idx = resolve_index(key)
        if idx is None:
            # Handle a step index whose own row was not logged (only once the transcript has reached that step).
            step_no = _step_number(key)
            if step_no is None or max_step < step_no:
                return None
            for candidate in range(len(rows) - 1, -1, -1):
                c_step = _step_number(rows[candidate].ref)
                if c_step is not None and c_step <= step_no:
                    idx = candidate
                    break
            if idx is None:
                return None
        if rows[idx].label in ('prompt', 'command'):
            return idx
        # Walk back ONLY from housekeeping/meta rows attached right after the turn's prompt (never from a response).
        if rows[idx].label in ('injected', 'meta'):
            cursor = idx - 1
            while cursor >= 0 and rows[cursor].label in ('injected', 'meta'):
                cursor -= 1
            if cursor >= 0 and rows[cursor].label in ('prompt', 'command'):
                return cursor
        return None

    turn_key = getattr(transition, 'turn', None)
    ref_key = getattr(transition, 'ref', None)
    turn_idx = resolve_turn_anchor(turn_key)
    turn_pos = rows[turn_idx].position if turn_idx is not None else None

    if isinstance(transition, TurnEnded) and isinstance(ref_key, str) and ref_key.startswith('end:'):
        if turn_idx is None:
            return TurnProjection()
        last_idx = turn_idx
        next_boundary = False
        for idx in range(turn_idx + 1, len(rows)):
            if rows[idx].label in ('prompt', 'command', 'compaction'):
                next_boundary = True
                break
            last_idx = idx
        if transition.state == 'finished':
            has_end = next_boundary or any(rows[i].ends_turn for i in range(turn_idx, last_idx + 1))
            if not has_end:
                return TurnProjection(turn_position=turn_pos, row_position=None)
        return TurnProjection(turn_position=turn_pos, row_position=rows[last_idx].position)

    row_idx = resolve_index(ref_key)
    if (isinstance(transition, TurnStarted) and turn_idx is not None
            and ref_key == turn_key and (row_idx is None or rows[row_idx].label in ('injected', 'meta'))):
        # When PreInvocation's initialNumSteps landed on a post-prompt housekeeping notice, anchor start at the prompt.
        row_idx = turn_idx
    row_pos = rows[row_idx].position if row_idx is not None else None
    return TurnProjection(turn_position=turn_pos, row_position=row_pos)
