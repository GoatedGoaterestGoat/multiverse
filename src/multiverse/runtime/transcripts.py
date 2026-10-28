"""Bounded transcript reads pinned to an agent's recorded native generation.

Providers own native rows and links. Core owns identity, cursors, page bounds
and turns, and never parses a provider's row bodies.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
import base64
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any

from .. import harnesses
from ..harnesses.types import SessionHandle, RowFact, TranscriptView, TranscriptRowsChanged, normalize_session_id
from ..plugins.access import Denied
from ..plugins.agent_files import AgentGone
from ..store import read
from ..universe import agents, records, sessions, storage
from . import effects, native

MAX_ROWS = 2000
MAX_TURNS = 500
MAX_BYTES = 16 << 20
DEFAULT_BYTES = 1 << 20
READ_SECONDS = 30
CURSOR_PREFIX = 'mv2-row-v1:'
GENERATION_PREFIX = 'mv2-generation-v1:'
MAX_CURSOR = 32768


class Changed(ValueError):
    def __init__(self, reason, resume, generation):
        self.reason, self.resume, self.generation = reason, resume, generation
        super().__init__(f'Transcript {reason}; resume from the returned cursor or read it again')


class Unavailable(ValueError):
    def __init__(self, reason, *, retry=False, state='unavailable'):
        self.retry, self.state = retry, state
        super().__init__(reason)


class Retry(Unavailable):
    def __init__(self, reason='Transcript identity is changing; retry shortly'):
        super().__init__(reason, retry=True)


@dataclass(frozen=True, kw_only=True)
class Generation:
    generation: str
    native_id: str
    reason: str
    ended_at: str
    readable: bool | None


@dataclass(frozen=True, kw_only=True)
class TranscriptInfo:
    state: str
    reason: str = ''
    format: str | None = None
    version: str | None = None
    native_id: str | None = None
    generation: str | None = None
    start: str | None = None
    end: str | None = None
    rows: int | None = None
    bytes: int | None = None
    updated_at: str | None = None
    tip: str | None = None
    gap: str | None = None
    turns: int | None = None
    context_rows: int | None = None
    context_turns: int | None = None
    context_start: str | None = None
    history_version: int | None = None
    stable_rows: bool = True
    earlier: tuple[Generation, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Row:
    cursor: str
    ref: str | None
    kind: str
    label: str
    source: str | None = None
    call_id: str | None = None
    abandoned: bool = False
    in_context: bool = True
    stop: str | None = None
    ends_turn: bool = False
    at: str | None = None
    size: int = 0
    data: dict | None = None
    format: str = ''


@dataclass(frozen=True, kw_only=True)
class RowPage:
    rows: tuple[Row, ...]
    next: str
    previous: str
    at_end: bool
    generation: str
    remaining: int | None = None
    tip: str | None = None
    gap: str | None = None


@dataclass(frozen=True, kw_only=True)
class Turn:
    id: str
    kind: str
    command: str | None = None
    args: str | None = None
    source: str | None = None
    at: str | None = None
    start: str = ''
    end: str = ''
    rows: int = 0
    in_context: bool = True
    finished: bool | None = None
    preview: str = ''


@dataclass(frozen=True, kw_only=True)
class TurnPage:
    turns: tuple[Turn, ...]
    next: str
    previous: str
    at_end: bool
    generation: str
    remaining: int | None = None
    tip: str | None = None
    gap: str | None = None


def _binding(properties, facts):
    return ({key: properties.get(key) for key in (
        'id', 'harness', 'workspace', 'native_session_id', 'native_session_home',
        'native_session_transition', 'native_storage_environment', 'native_service_identity')},
        {key: facts.get(key) for key in ('launch_id', 'pid', 'pid_started', 'binding_conflict')})


def _inode(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


@contextmanager
def _namespace(root):
    # Same order as session writes, but a reader never waits for a claimant.
    with (sessions.top(root) / 'state' / '.sessions.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Retry('Native conversation ownership is updating; retry shortly') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass(frozen=True, kw_only=True)
class ReadExecutor:
    """Deadline and snapshot validation only; no native control or transport."""
    deadline: float
    _validate: Any = field(repr=False)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise Retry('Transcript read exceeded its deadline; retry with a smaller page')
        self._validate()


@dataclass(frozen=True, kw_only=True)
class _Snapshot:
    root: Path
    directory: Path
    adapter: Any = field(repr=False)
    session: SessionHandle
    earlier: tuple
    universe_id: str
    reader: ReadExecutor = field(repr=False)


def _recorded_home(directory, properties, facts, adapter):
    home = properties.get('native_session_home')
    if home is not None:
        return sessions._stored_home(home)
    keys = tuple(getattr(adapter, 'storage_env', ()))
    selected = storage.recorded_environment(directory, properties, getattr(adapter, 'family', ''), keys, runtime=facts)
    hook = native.adapter_hook(adapter, 'session_home')
    if not keys or not selected or hook is None:
        return None
    selected = dict(selected)
    primary = selected.get(keys[0])
    if not primary:
        base = selected.get('HOME')
        if not isinstance(base, str) or not Path(base).is_absolute():
            return None
    for key, value in tuple(selected.items()):
        if value and not Path(value).is_absolute():
            # The launch's workspace, never this process's cwd or HOME.
            workspace = properties.get('workspace')
            if key == 'HOME' or value.startswith('~') or not isinstance(workspace, str) or not Path(workspace).is_absolute():
                return None
            selected[key] = os.path.normpath(os.path.join(workspace, value))
    home = hook(selected)
    return sessions._stored_home(home) if home is not None else None


def _gone(directory):
    return AgentGone(f'Agent folder {directory} is gone; it was removed or moved')


def _snapshot(root, reference):
    effects.require('transcript_view', getattr(reference, 'name', reference))
    root = Path(root).resolve()
    directory = reference if isinstance(reference, Path) else agents.directory(root, reference)
    directory = Path(directory)
    if directory.parent.resolve() != (root / 'state' / 'agents').resolve():
        raise Unavailable('Transcript agent is outside the selected universe')
    try:
        identity = _inode(directory)
        properties = records.properties(directory / 'properties.json')
        facts = records.runtime(directory / 'runtime.json')
        if properties.get('id') != directory.name or not properties.get('harness') or not properties.get('workspace'):
            raise Unavailable(f'Cannot identify the agent from {directory / "properties.json"}')
        adapter = harnesses.load(root, properties['harness'])
        # This may reconcile proven /clear evidence. Never claim to hold the
        # launch lock before native's nonblocking snapshot path has acquired it.
        properties, facts = native.session_snapshot(root, directory, properties, facts, adapter=adapter,
                                                     launch_locked=False)
        with native._snapshot_lock(directory):
            current = records.properties(directory / 'properties.json')
            live = records.runtime(directory / 'runtime.json')
            if _inode(directory) != identity or _binding(current, live) != _binding(properties, facts):
                raise Retry()
            effective, _ = sessions.effective(directory, current)
            if _binding(effective, live) != _binding(current, live):
                raise Retry('Native session journal advanced during this read; retry shortly')
            if live.get('binding_conflict'):
                raise Unavailable('Native conversation is claimed by another agent; restore this agent\'s own binding')
            earlier = sessions.generations(directory, current)
            home = _recorded_home(directory, current, live, adapter)
            native_id = current.get('native_session_id')
            if native_id is not None:
                normalize_session_id(native_id)
                with _namespace(root):
                    sessions.claim(root, directory, current, native_id, home)
            session = replace(SessionHandle.from_records(current, live, directory), home=home)
            universe_id = read(root / 'universe.json')['id']
            expected = _binding(current, live)
        def validate():
            try:
                if _inode(directory) != identity:
                    raise _gone(directory)
                props = records.properties(directory / 'properties.json')
                runtime = records.runtime(directory / 'runtime.json')
                effective, _ = sessions.effective(directory, props)
                if _binding(effective, runtime) != expected:
                    raise Retry()
            except FileNotFoundError:
                raise _gone(directory) from None
            except records.RecordError as error:
                if not directory.is_dir():
                    raise _gone(directory) from None
                raise Unavailable(f'{error.file}: {error.reason}') from None
            except OSError:
                raise Unavailable('Native session records cannot be read reliably; repair the agent records') from None
        reader = ReadExecutor(deadline=time.monotonic() + READ_SECONDS, _validate=validate)
        reader.check()
        return _Snapshot(root=root, directory=directory, adapter=adapter, session=session, earlier=earlier,
                         universe_id=universe_id, reader=reader)
    except FileNotFoundError:
        raise _gone(directory) from None
    except records.RecordError as error:
        if not directory.is_dir():
            raise _gone(directory) from None
        raise Unavailable(f'{error.file}: {error.reason}') from None
    except sessions.SessionConflict:
        raise Unavailable('Native conversation ownership cannot be verified; repair the conflicting agent records') from None
    except (Denied, Unavailable, AgentGone):
        raise
    except (OSError, KeyError, TypeError):
        raise Unavailable('Native session records cannot be read reliably; repair the agent records') from None
    except ValueError as error:
        if 'retry' in str(error).lower():
            raise Retry('Native session identity is updating; retry shortly') from None
        raise Unavailable('Native session identity or storage is not recorded reliably; repair the agent records') from None


def transcript_snapshot(root, reference):
    """The reconciled session and its validated earlier scopes, with no held locks."""
    result = _snapshot(root, reference)
    return result.adapter, result.session, result.earlier


def _pack(prefix, value):
    result = prefix + base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode()).decode().rstrip('=')
    if len(result) > MAX_CURSOR:
        raise ValueError('Transcript position is too large')
    return result


def _unpack(value, prefix):
    if not isinstance(value, str) or len(value) > MAX_CURSOR or not value.startswith(prefix):
        raise ValueError('Invalid transcript cursor or generation')
    try:
        body = value[len(prefix):]
        result = json.loads(base64.b64decode(body + '=' * (-len(body) % 4), altchars=b'-_', validate=True))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Invalid transcript cursor or generation') from None


def _generation(session):
    return _pack(GENERATION_PREFIX, {'h': session.harness, 'n': session.native_id, 's': session.home})


def _provider(snapshot, method, session, *args, **kwargs):
    if native.adapter_hook(snapshot.adapter, method) is None:
        raise Unavailable('Transcript reading is not implemented for this harness yet')
    snapshot.reader.check()
    try:
        result = effects.call(snapshot.adapter, method, session, *args, reader=snapshot.reader, **kwargs)
    except (Denied, AgentGone, Unavailable, TranscriptRowsChanged):
        raise
    except Exception:
        # A provider error can contain native data. It never becomes API text.
        raise Unavailable('The harness could not read its transcript', state='error') from None
    snapshot.reader.check()
    return result


@dataclass(frozen=True, kw_only=True)
class _Loaded:
    snapshot: _Snapshot
    session: SessionHandle
    view: TranscriptView
    generation: str
    explicit: bool
    positions: dict
    refs: dict
    nearest: tuple
    history_places: dict
    history_nearest: tuple


def _validate_view(view):
    if not isinstance(view, TranscriptView):
        raise Unavailable('The harness returned invalid transcript metadata', state='error')
    source = view.source
    if source.state != 'supported':
        raise Unavailable(source.reason or 'The native transcript is unavailable', state=source.state)
    positions = set()
    for row in view.rows:
        if (not isinstance(row, RowFact) or not isinstance(row.position, str) or not row.position
                or row.position in positions or not isinstance(row.size, int) or isinstance(row.size, bool)
                or row.size < 0 or not isinstance(row.kind, str) or not isinstance(row.label, str)
                or (row.ref is not None and not isinstance(row.ref, str))):
            raise Unavailable('The harness returned invalid transcript rows', state='error')
        positions.add(row.position)
    if (any(type(i) is not int or i < 0 or i >= len(view.rows) for i in view.history)
            or len(set(view.history)) != len(view.history) or type(view.context_from) is not int
            or not 0 <= view.context_from <= len(view.history)):
        raise Unavailable('The harness returned invalid transcript history', state='error')


def _load(root, directory, *, generation=None, tip=None, snapshot=None):
    if tip is not None and not isinstance(tip, str):
        raise ValueError('Transcript tip must be a row ref')
    snapshot = snapshot or _snapshot(root, directory)
    session = snapshot.session
    if generation is not None:
        _unpack(generation, GENERATION_PREFIX)
        if generation != _generation(session):
            selected = next((row for row in snapshot.earlier if _generation(replace(
                session, harness=row['harness'], native_id=row['native_id'], home=row['home'])) == generation), None)
            if selected is None:
                # An own-session provider may prove a home that the old record
                # lacks. Resolve that current view before rejecting its token.
                if session.home is not None:
                    raise Unavailable('Unknown transcript generation')
            else:
                session = replace(session, harness=selected['harness'], native_id=selected['native_id'], home=selected['home'])
                if session.home is None:
                    raise Unavailable('The earlier generation has no recorded storage')
    if session.native_id is None:
        raise Unavailable('No native conversation is bound to this agent yet')
    view = _provider(snapshot, 'transcript_view', session, tip=tip)
    _validate_view(view)
    home = view.source.home
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise Unavailable('The transcript provider did not identify its recorded storage')
    if session.home is None:
        own = os.environ.get('MV2_AGENT_DIR')
        if not own or os.path.realpath(own) != os.path.realpath(snapshot.directory):
            raise Unavailable('The transcript has no recorded storage; read it from its own agent session')
        session = replace(session, home=home)
    elif home != session.home:
        raise Unavailable('The transcript provider selected storage different from the recorded generation')
    if session.native_id != snapshot.session.native_id or session.home != snapshot.session.home:
        try:
            with native._snapshot_lock(snapshot.directory), _namespace(snapshot.root):
                snapshot.reader.check()
                data = records.properties(snapshot.directory / 'properties.json')
                sessions.claim(snapshot.root, snapshot.directory, data, session.native_id, session.home)
        except sessions.SessionConflict:
            raise Unavailable('Native conversation ownership cannot be verified; repair the conflicting agent records') from None
        except (Denied, AgentGone, Unavailable):
            raise
        except ValueError:
            raise Retry() from None
    selected_generation = _generation(session)
    if generation is not None and selected_generation != generation:
        raise Unavailable('Unknown transcript generation')
    nearest, refs, previous = [], {}, None
    for i, row in enumerate(view.rows):
        if row.ref is not None:
            # Ambiguous refs cannot locate a cursor; positions still work.
            refs[row.ref] = i if row.ref not in refs else None
            previous = row.ref
        nearest.append(previous)
    history_nearest, previous = [], None
    for index in view.history:
        previous = view.rows[index].ref or previous
        history_nearest.append(previous)
    return _Loaded(snapshot=snapshot, session=session, view=view, generation=selected_generation,
                   explicit=generation is not None, positions={r.position: i for i, r in enumerate(view.rows)},
                   refs=refs, nearest=tuple(nearest),
                   history_places={index: place for place, index in enumerate(view.history)},
                   history_nearest=tuple(history_nearest))


def _cursor(loaded, index=None, *, history=False, turn=None, edge=None, side='after', boundary=None):
    row = loaded.view.rows[index] if index is not None else None
    nearest = loaded.nearest[index] if row else None
    if row and side == 'before':
        place = loaded.history_places.get(index, 0) if history else index
        previous = loaded.history_nearest if history else loaded.nearest
        nearest = previous[place - 1] if place else None
    value = {'u': loaded.snapshot.universe_id, 'a': loaded.session.agent_id, 'g': loaded.generation,
             'p': row.position if row else None, 'r': nearest,
             'h': history, 's': side, 'b': boundary or ('start' if index is None else None)}
    if turn is not None:
        value.update(t=turn, e=edge)
    return _pack(CURSOR_PREFIX, value)


def _rewrite(loaded, nearest):
    index = loaded.refs.get(nearest)
    return Changed('rewritten', _cursor(loaded, index) if index is not None else None, loaded.generation)


def _resolve(loaded, value, *, history=False):
    """Return a stored-row index, with None denoting the start boundary."""
    if not isinstance(value, str):
        raise ValueError('A transcript position must be a cursor or row ref')
    if not value.startswith(CURSOR_PREFIX):
        index = loaded.refs.get(value)
        if index is None:
            raise ValueError('Unknown or ambiguous transcript row ref')
        saved = None
    else:
        saved = _unpack(value, CURSOR_PREFIX)
        if (set(saved) - {'u', 'a', 'g', 'p', 'r', 'h', 't', 'e', 's', 'b'}
                or not {'u', 'a', 'g', 'p', 'r', 'h'} <= saved.keys()
                or not isinstance(saved['g'], str) or type(saved['h']) is not bool
                or saved.get('s') not in ('before', 'after')
                or saved.get('b') not in (None, 'start', 'end', 'context')
                or (saved['p'] is not None and not isinstance(saved['p'], str))
                or (saved['r'] is not None and not isinstance(saved['r'], str))):
            raise ValueError('Invalid transcript cursor')
        if saved['u'] != loaded.snapshot.universe_id or saved['a'] != loaded.session.agent_id:
            raise ValueError('This cursor belongs to another agent')
        if saved['g'] != loaded.generation:
            if loaded.explicit:
                raise ValueError('This cursor belongs to another generation')
            raise Changed('replaced', _cursor(loaded), loaded.generation)
        if saved['p'] is None:
            return None
        index = loaded.positions.get(saved['p'])
        if index is None:
            raise _rewrite(loaded, saved['r'])
    if history and index not in loaded.view.history and not (saved and saved.get('b')):
        resume = None
        nearest = saved['r'] if saved else loaded.view.rows[index].ref
        if nearest is not None:
            old_view = _provider(loaded.snapshot, 'transcript_view', loaded.session, tip=nearest)
            _validate_view(old_view)
            # The provider can reconstruct the old branch, including preserved
            # rows whose history order differs from their storage order.
            old_positions = {old_view.rows[i].position for i in old_view.history}
            common = [i for i in loaded.view.history if loaded.view.rows[i].position in old_positions]
            if common:
                resume = _cursor(loaded, common[-1], history=True)
        raise Changed('diverged', resume, loaded.generation)
    return index


def _bound(value, name, maximum, minimum=1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')


def _filters(value, name):
    if value is None:
        return None
    if not isinstance(value, (set, frozenset, tuple, list)) or any(not isinstance(v, str) for v in value):
        raise ValueError(f'{name} must be a collection of strings')
    return frozenset(value)


def _row_page(loaded, order, *, after=None, before=None, limit=200, max_bytes=DEFAULT_BYTES,
              labels=None, kinds=None, history=False, bodies=True):
    _bound(limit, 'limit', MAX_ROWS)
    _bound(max_bytes, 'max_bytes', MAX_BYTES, 1024)
    if type(bodies) is not bool or type(history) is not bool:
        raise ValueError('bodies and history must be booleans')
    if after is not None and before is not None:
        raise ValueError('Use either after or before, not both')
    labels, kinds = _filters(labels, 'labels'), _filters(kinds, 'kinds')
    order = tuple(order)
    location = {index: place for place, index in enumerate(order)}
    anchor = before if before is not None else after
    index = _resolve(loaded, anchor, history=history) if anchor is not None else None
    saved = _unpack(anchor, CURSOR_PREFIX) if isinstance(anchor, str) and anchor.startswith(CURSOR_PREFIX) else None
    side = saved['s'] if saved else ('before' if before is not None else 'after')
    if index is None:
        boundary = 0
    elif history and saved and saved.get('b') == 'end':
        # An end cursor pins a stored prefix. It may end on metadata outside
        # history; the first newer stored row is the continuation in history.
        boundary = next((place for place, row in enumerate(order) if row > index), len(order))
    elif history and saved and saved.get('b') == 'context' and index not in location:
        boundary = min(loaded.view.context_from, len(order))
    elif index in location:
        boundary = location[index] + (side == 'after')
    else:
        # A turn is a subset of history. A cursor on an embedded command is a
        # valid boundary between that prompt's rows too.
        history_places = {row: place for place, row in enumerate(loaded.view.history)}
        if index not in history_places:
            raise ValueError('The transcript position is outside this turn')
        boundary = sum(history_places[i] < history_places[index]
                       or history_places[i] == history_places[index] and side == 'after' for i in order)
    candidates = list(range(boundary)) if before is not None else list(range(boundary, len(order)))
    matches = [place for place in candidates if (labels is None or loaded.view.rows[order[place]].label in labels)
               and (kinds is None or loaded.view.rows[order[place]].kind in kinds)]
    chosen, size = [], 0
    for place in reversed(matches) if before is not None else matches:
        amount = loaded.view.rows[order[place]].size
        if chosen and (len(chosen) >= limit or bodies and size + amount > max_bytes):
            break
        chosen.append(place)
        size += amount
    if before is not None:
        chosen.reverse()
    indexes = [order[place] for place in chosen]
    values = {}
    selected = [i for i in indexes if loaded.view.rows[i].size <= MAX_BYTES] if bodies else []
    if selected:
        try:
            contents = _provider(loaded.snapshot, 'transcript_bodies', loaded.session, loaded.view, selected,
                                 max_bytes=MAX_BYTES)
        except TranscriptRowsChanged:
            fresh = _load(loaded.snapshot.root, loaded.snapshot.directory,
                          generation=loaded.generation if loaded.explicit else None, tip=loaded.view.tip)
            nearest = loaded.nearest[index] if index is not None else None
            raise _rewrite(fresh, nearest) from None
        if (not isinstance(contents, (list, tuple)) or len(contents) != len(selected)
                or any(body is not None and not isinstance(body, dict) for body in contents)):
            raise Unavailable('The harness returned invalid transcript bodies', state='error')
        values = dict(zip(selected, contents))
    history_set = set(loaded.view.history)
    context = set(loaded.view.history[loaded.view.context_from:])
    result = tuple(Row(cursor=_cursor(loaded, i, history=history), ref=fact.ref, kind=fact.kind, label=fact.label,
                       source=fact.source, call_id=fact.call_id, abandoned=i not in history_set,
                       in_context=i in context, stop=fact.stop, ends_turn=fact.ends_turn, at=fact.at,
                       size=fact.size, data=deepcopy(values.get(i)), format=loaded.view.source.format or '')
                   for i in indexes for fact in (loaded.view.rows[i],))
    if chosen:
        next_cursor = _cursor(loaded, indexes[-1], history=history)
        previous = _cursor(loaded, indexes[0], history=history, side='before')
    else:
        # Even an empty filtered page advances over skipped rows.
        end = len(order) if before is None else boundary
        next_cursor = (anchor if not candidates and saved else
                       _cursor(loaded, order[end - 1] if end else None, history=history, boundary='end'))
        previous = _cursor(loaded, None if before is not None else index, history=history)
    remaining = len(matches) - len(chosen)
    loaded.snapshot.reader.check()
    return RowPage(rows=result, next=next_cursor, previous=previous, at_end=remaining == 0,
                   generation=loaded.generation, remaining=remaining,
                   tip=loaded.view.tip if history else None, gap=loaded.view.gap if history else None)


def rows(root, directory, *, after=None, before=None, limit=200, max_bytes=DEFAULT_BYTES,
         history=False, labels=None, kinds=None, tip=None, generation=None, bodies=True):
    loaded = _load(root, directory, generation=generation, tip=tip)
    order = loaded.view.history if history else range(len(loaded.view.rows))
    return _row_page(loaded, order, after=after, before=before, limit=limit, max_bytes=max_bytes,
                     history=history, labels=labels, kinds=kinds, bodies=bodies)


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.utcoffset() is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _groups(loaded):
    groups, prompt, command, latest, preamble = [], None, None, None, None
    for index in loaded.view.history:
        row = loaded.view.rows[index]
        if row.label in ('prompt', 'command'):
            group = {'kind': row.label, 'indexes': []}
            groups.append(group)
            latest = group
            if row.label == 'prompt':
                prompt = group
            else:
                command = group
        elif row.label == 'command_output' and command is not None:
            group = command
        elif row.label == 'compaction' and latest is not None and latest['kind'] == 'command':
            group = latest
        elif prompt is not None:
            group = prompt
        else:
            if preamble is None:
                preamble = {'kind': 'preamble', 'indexes': []}
                groups.append(preamble)
            group = preamble
        group['indexes'].append(index)
    context = set(loaded.view.history[loaded.view.context_from:])
    result = []
    last_prompt = max((n for n, group in enumerate(groups) if group['kind'] == 'prompt'), default=-1)
    for n, group in enumerate(groups):
        indexes = group['indexes']
        first, last = indexes[0], indexes[-1]
        row, kind = loaded.view.rows[first], group['kind']
        turn_id = 'preamble' if kind == 'preamble' else row.ref or '@' + _cursor(loaded, first)
        if kind == 'command':
            finished = any(loaded.view.rows[i].label == 'command_output' for i in indexes)
        elif kind == 'prompt':
            finished = n < last_prompt
            ends = [loaded.view.rows[i] for i in indexes if loaded.view.rows[i].ends_turn]
            start = _timestamp(row.at)
            if not finished and ends:
                if start is not None and any((end := _timestamp(r.at)) is not None and end >= start for r in ends):
                    finished = True
                elif start is None or any(_timestamp(r.at) is None for r in ends):
                    finished = None
        else:
            finished = None
        result.append((Turn(id=turn_id, kind=kind, command=row.command if kind == 'command' else None,
                            args=row.args if kind == 'command' else None, source=row.source, at=row.at,
                            start=_cursor(loaded, first, history=True, side='before'),
                            end=_cursor(loaded, last, history=True), rows=len(indexes),
                            in_context=bool(context.intersection(indexes)), finished=finished,
                            preview=row.preview[:200] if kind != 'preamble' else ''), tuple(indexes)))
    return result


def _turn_boundary(loaded, groups, anchor, *, before=False):
    if anchor is None:
        return 0
    if not isinstance(anchor, str):
        raise ValueError('A transcript turn position must be an ID or cursor')
    ids = {item.id: n for n, (item, _) in enumerate(groups)}
    if anchor in ids:
        return ids[anchor] + (0 if before else 1)
    saved = _unpack(anchor, CURSOR_PREFIX) if isinstance(anchor, str) and anchor.startswith(CURSOR_PREFIX) else None
    index = _resolve(loaded, anchor, history=True)
    if saved is not None and 't' in saved:
        if not isinstance(saved['t'], str) or saved['t'] not in ids or saved.get('e') not in ('before', 'after'):
            raise ValueError('Unknown transcript turn cursor')
        return ids[saved['t']] + (saved['e'] == 'after')
    if index is None:
        return 0
    if saved and saved.get('b') == 'end':
        return sum(indexes[0] <= index for _, indexes in groups)
    # A raw row cursor denotes the containing turn, independent of overlapping
    # prompt/command spans; the envelope on turn-page cursors pins its turn.
    positions = {row: place for place, row in enumerate(loaded.view.history)}
    return sum(positions[indexes[0]] < positions[index]
               or positions[indexes[0]] == positions[index] and (not saved or saved['s'] == 'after')
               for _, indexes in groups)


def turns(root, directory, *, after=None, before=None, limit=50, tip=None, generation=None):
    _bound(limit, 'limit', MAX_TURNS)
    if after is not None and before is not None:
        raise ValueError('Use either after or before, not both')
    loaded = _load(root, directory, generation=generation, tip=tip)
    groups = _groups(loaded)
    boundary = _turn_boundary(loaded, groups, before if before is not None else after, before=before is not None)
    selected = groups[max(0, boundary - limit):boundary] if before is not None else groups[boundary:boundary + limit]
    if selected:
        first, last = selected[0], selected[-1]
        next_cursor = _cursor(loaded, last[1][-1], history=True, turn=last[0].id, edge='after')
        previous = _cursor(loaded, first[1][0], history=True, turn=first[0].id, edge='before')
    else:
        next_cursor = previous = after or before or _cursor(loaded)
    remaining = max(0, boundary - len(selected)) if before is not None else max(0, len(groups) - boundary - len(selected))
    loaded.snapshot.reader.check()
    return TurnPage(turns=tuple(item for item, _ in selected), next=next_cursor, previous=previous,
                    at_end=remaining == 0, generation=loaded.generation, remaining=remaining,
                    tip=loaded.view.tip, gap=loaded.view.gap)


def turn(root, directory, turn_id, *, after=None, limit=200, max_bytes=DEFAULT_BYTES, tip=None, generation=None):
    loaded = _load(root, directory, generation=generation, tip=tip)
    selected = next((indexes for item, indexes in _groups(loaded) if item.id == turn_id), None)
    if selected is None:
        raise ValueError('Unknown transcript turn')
    return _row_page(loaded, selected, after=after, limit=limit, max_bytes=max_bytes, history=True)


def info(root, directory, *, generation=None, tip=None):
    try:
        snapshot = _snapshot(root, directory)
        earlier = []
        for row in snapshot.earlier:
            old = replace(snapshot.session, harness=row['harness'], native_id=row['native_id'], home=row['home'])
            readable = None
            if old.home is not None and native.adapter_hook(snapshot.adapter, 'transcript_readable'):
                readable = _provider(snapshot, 'transcript_readable', old)
                if readable is not True and readable is not False:
                    readable = None
            earlier.append(Generation(generation=_generation(old), native_id=old.native_id,
                                      reason=row['reason'], ended_at=row['ended_at'], readable=readable))
        try:
            loaded = _load(root, directory, generation=generation, tip=tip, snapshot=snapshot)
        except Unavailable as error:
            return TranscriptInfo(state=error.state, reason=str(error), native_id=snapshot.session.native_id,
                                  earlier=tuple(earlier))
        view = loaded.view
        context_start = (view.history[view.context_from] if view.context_from < len(view.history) else None)
        grouped = _groups(loaded)
        snapshot.reader.check()
        return TranscriptInfo(state='supported', format=view.source.format, version=view.source.version,
                              native_id=loaded.session.native_id, generation=loaded.generation,
                              start=_cursor(loaded), end=_cursor(loaded, len(view.rows) - 1 if view.rows else None, boundary='end'),
                              rows=len(view.rows), bytes=view.source.size, updated_at=view.source.updated_at,
                              tip=view.tip, gap=view.gap, turns=len(grouped),
                              context_rows=len(view.history) - view.context_from,
                              context_turns=sum(item.in_context for item, _ in grouped),
                              context_start=(_cursor(loaded) if not view.context_from else
                                             _cursor(loaded, context_start, history=True, side='before', boundary='context')),
                              history_version=view.history_version, stable_rows=view.source.stable_rows,
                              earlier=tuple(earlier))
    except Unavailable as error:
        return TranscriptInfo(state=error.state, reason=str(error))
