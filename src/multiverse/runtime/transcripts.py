"""Bounded transcript reads pinned to an agent's recorded native generation.

Providers own native rows and links. Core owns identity, cursors, page bounds
and turns, and never parses a provider's row bodies.
"""
from contextlib import contextmanager
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime
import base64
import fcntl
import json
import os
from pathlib import Path
import time
from threading import Lock
from typing import Any

from .. import harnesses
from ..harnesses.transcript_groups import turn_anchors
from ..harnesses.types import (SessionHandle, RowFact, TranscriptView, TranscriptRowsChanged, TurnCursorProjection,
                              TurnProjection,
                              normalize_session_id)
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


class _ReadDeadline(Retry):
    """The read budget expired independently of snapshot validity."""


class _BeforeProviderDeadline(_ReadDeadline):
    """The budget expired before entering a transcript provider."""


class _PendingTranscript(Unavailable):
    """A provider reports an unfinished write. Only the event resolver waits for it."""


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
    retry: bool = False
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
    summary: str | None = None
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
    turn: str | None = None


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
    summary: str | None = None


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
    summary: str | None = None


def _binding(properties, facts):
    return ({key: properties.get(key) for key in (
        'id', 'harness', 'workspace', 'native_session_id', 'native_session_home',
        'native_session_transition', 'native_storage_environment', 'native_service_identity')},
        {key: facts.get(key) for key in ('launch_id', 'pid', 'pid_started', 'binding_conflict', 'session_generation')})


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

    def _check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise _ReadDeadline('Transcript read exceeded its deadline; retry with a smaller page')

    def check(self):
        self._check_deadline()
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


def _snapshot(root, reference, *, allow_navigation=False, reconcile=True, deadline=None):
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
        from . import navigation
        if reconcile and not (allow_navigation and navigation.pending(facts)):
            properties, facts = native.session_snapshot(root, directory, properties, facts, adapter=adapter,
                                                         launch_locked=False)
        with native._snapshot_lock(directory):
            current = records.properties(directory / 'properties.json')
            live = records.runtime(directory / 'runtime.json')
            if not allow_navigation:
                navigation.require_settled(live)
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
                if not allow_navigation:
                    navigation.require_settled(runtime)
                effective, _ = sessions.effective(directory, props)
                if _binding(effective, runtime) != expected:
                    raise Retry()
            except navigation.Switching as error:
                raise Retry(str(error)) from None
            except FileNotFoundError:
                raise _gone(directory) from None
            except records.RecordError as error:
                if not directory.is_dir():
                    raise _gone(directory) from None
                raise Unavailable(f'{error.file}: {error.reason}') from None
            except OSError:
                raise Unavailable('Native session records cannot be read reliably; repair the agent records') from None
        reader = ReadExecutor(deadline=time.monotonic() + READ_SECONDS if deadline is None else deadline,
                              _validate=validate)
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
        from .navigation import Switching
        if isinstance(error, Switching):
            raise Retry(str(error)) from None
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
    try:
        snapshot.reader.check()
    except _ReadDeadline as error:
        raise _BeforeProviderDeadline(str(error)) from None
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
    analysis: object


# Only native metadata is cached, never a snapshot, session claim or row body.
# A provider must return a new immutable view whenever any fact changes.
_VIEW_CACHE = OrderedDict()
_VIEW_LOCK = Lock()
VIEW_CACHE_SIZE = 4


@dataclass
class _Analysis:
    view: TranscriptView
    maps: dict
    stored_order: tuple
    history_set: frozenset
    context: frozenset
    groups: dict = field(default_factory=dict)


def _validate_view(view):
    if not isinstance(view, TranscriptView):
        raise Unavailable('The harness returned invalid transcript metadata', state='error')
    source = view.source
    if source.state != 'supported':
        error = _PendingTranscript if source.retry is True else Unavailable
        raise error(source.reason or 'The native transcript is unavailable', state=source.state)
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


def _selected_session(snapshot, generation):
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
    if session.native_id == snapshot.session.native_id:
        from .navigation import pending
        if pending(records.runtime(snapshot.directory / 'runtime.json')):
            raise Retry('Conversation switch is unresolved; retry after it completes')
    if session.native_id is None:
        raise Unavailable('No native conversation is bound to this agent yet')
    return session


def _load(root, directory, *, generation=None, tip=None, snapshot=None):
    if tip is not None and not isinstance(tip, str):
        raise ValueError('Transcript tip must be a row ref')
    snapshot = snapshot or _snapshot(root, directory, allow_navigation=generation is not None)
    session = _selected_session(snapshot, generation)
    view = _provider(snapshot, 'transcript_view', session, tip=tip)
    analysis = _analyze(view)
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
    return _Loaded(snapshot=snapshot, session=session, view=view, generation=selected_generation,
                   explicit=generation is not None, analysis=analysis, **analysis.maps)


def _analyze(view):
    with _VIEW_LOCK:
        cached = _VIEW_CACHE.get(id(view))
        if cached is not None and cached.view is view:
            _VIEW_CACHE.move_to_end(id(view))
            return cached
    _validate_view(view)
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
    analysis = _Analysis(view=view, stored_order=tuple(range(len(view.rows))),
                         history_set=frozenset(view.history), context=frozenset(view.history[view.context_from:]),
                         maps={'positions': {r.position: i for i, r in enumerate(view.rows)}, 'refs': refs,
                               'nearest': tuple(nearest),
                               'history_places': {index: place for place, index in enumerate(view.history)},
                               'history_nearest': tuple(history_nearest)})
    with _VIEW_LOCK:
        _VIEW_CACHE[id(view)] = analysis
        _VIEW_CACHE.move_to_end(id(view))
        while len(_VIEW_CACHE) > VIEW_CACHE_SIZE:
            _VIEW_CACHE.popitem(last=False)
    return analysis


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
    if history and index not in loaded.analysis.history_set and not (saved and saved.get('b')):
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
    location = (loaded.history_places if order is loaded.view.history else
                None if order is loaded.analysis.stored_order else {index: place for place, index in enumerate(order)})
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
    elif history and saved and saved.get('b') == 'context' and location is not None and index not in location:
        boundary = min(loaded.view.context_from, len(order))
    elif location is None or index in location:
        boundary = (index if location is None else location[index]) + (side == 'after')
    else:
        # A turn is a subset of history. A cursor on an embedded command is a
        # valid boundary between that prompt's rows too.
        history_places = loaded.history_places
        if index not in history_places:
            raise ValueError('The transcript position is outside this turn')
        boundary = sum(history_places[i] < history_places[index]
                       or history_places[i] == history_places[index] and side == 'after' for i in order)
    candidates = range(boundary) if before is not None else range(boundary, len(order))
    matches = (candidates if labels is None and kinds is None else
               [place for place in candidates if (labels is None or loaded.view.rows[order[place]].label in labels)
                and (kinds is None or loaded.view.rows[order[place]].kind in kinds)])
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
    history_set, context = loaded.analysis.history_set, loaded.analysis.context
    _, turn_ids = _grouped(loaded)
    result = tuple(Row(cursor=_cursor(loaded, i, history=history), ref=fact.ref, kind=fact.kind, label=fact.label,
                       source=fact.source, call_id=fact.call_id, abandoned=i not in history_set,
                       in_context=i in context, stop=fact.stop, ends_turn=fact.ends_turn, at=fact.at,
                       size=fact.size, data=values.get(i), format=loaded.view.source.format or '', turn=turn_ids.get(i))
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
                   tip=loaded.view.tip if history else None, gap=loaded.view.gap if history else None,
                   summary=loaded.view.summary if history else None)


def rows(root, directory, *, after=None, before=None, limit=200, max_bytes=DEFAULT_BYTES,
         history=False, labels=None, kinds=None, tip=None, generation=None, bodies=True):
    loaded = _load(root, directory, generation=generation, tip=tip)
    order = loaded.view.history if history else loaded.analysis.stored_order
    return _row_page(loaded, order, after=after, before=before, limit=limit, max_bytes=max_bytes,
                     history=history, labels=labels, kinds=kinds, bodies=bodies)


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.utcoffset() is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _build_groups(loaded):
    by_anchor = {}
    history = ((index, loaded.view.rows[index].label) for index in loaded.view.history)
    for index, label, anchor in turn_anchors(history):
        if anchor not in by_anchor:
            by_anchor[anchor] = {'kind': label if anchor is not None else 'preamble', 'indexes': []}
        by_anchor[anchor]['indexes'].append(index)
    groups = list(by_anchor.values())
    context = loaded.analysis.context
    result = []
    last_prompt = max((n for n, group in enumerate(groups) if group['kind'] == 'prompt'), default=-1)
    for n, group in enumerate(groups):
        indexes = group['indexes']
        first, last = indexes[0], indexes[-1]
        row, kind = loaded.view.rows[first], group['kind']
        turn_id = 'preamble' if kind == 'preamble' else row.ref or '@' + _cursor(loaded, first)
        if kind == 'command':
            # Its command and args are its first row's that names one, which needn't be its first row (Claude's
            # caveat names none); it is finished at a command_output row stored after the last row that names it, so
            # an echo alone doesn't finish it (Claude's older layout names it twice: the typed row and its echo), or
            # at any command_output row when none names one.
            naming = [n for n, i in enumerate(indexes) if loaded.view.rows[i].command]
            if naming:
                row = loaded.view.rows[indexes[naming[0]]]
            after = indexes[naming[-1] + 1:] if naming else indexes
            finished = any(loaded.view.rows[i].label == 'command_output' for i in after)
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
        first_row = loaded.view.rows[first]
        result.append((Turn(id=turn_id, kind=kind, command=row.command if kind == 'command' else None,
                            args=row.args if kind == 'command' else None, source=first_row.source, at=first_row.at,
                            start=_cursor(loaded, first, history=True, side='before'),
                            end=_cursor(loaded, last, history=True), rows=len(indexes),
                            in_context=bool(context.intersection(indexes)), finished=finished,
                            preview=row.preview[:200] if kind != 'preamble' else ''), tuple(indexes)))
    return result


def _grouped(loaded):
    # Cursors inside Turn depend on this scope. The cached value contains no
    # reader or authority and is used only after this page's fresh snapshot.
    key = (loaded.snapshot.universe_id, loaded.session.agent_id, loaded.generation)
    with _VIEW_LOCK:
        cached = loaded.analysis.groups.get(key)
    if cached is not None:
        return cached
    groups = tuple(_build_groups(loaded))
    result = (groups, {index: turn.id for turn, indexes in groups for index in indexes})
    with _VIEW_LOCK:
        if len(loaded.analysis.groups) >= VIEW_CACHE_SIZE:
            loaded.analysis.groups.pop(next(iter(loaded.analysis.groups)))
        loaded.analysis.groups[key] = result
    return result


def _groups(loaded):
    return _grouped(loaded)[0]


@dataclass(frozen=True, kw_only=True)
class TurnReference:
    """Public turn and row identity established by one immutable transcript view."""
    turn: str | None = None
    ref: str | None = None
    generation: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class _TurnRead:
    snapshot: _Snapshot
    generation: str
    view: TranscriptView | None
    references: tuple[TurnReference, ...]
    pending_rows: bool


class TurnIndexRequired(Exception):
    """Projection needs a full read in an isolated process."""


def turn_references(root, directory, launch_id, native_id, generation, transitions, *, deadline, storage=None,
                    row_wait=None, prepare_index=False, indexed_only=False):
    """Resolve one scoped event batch off-loop, waiting briefly for its reported rows."""
    unresolved = (TurnReference(),) * len(transitions)
    has_rows = any(edge.ref is not None for edge in transitions)
    last = None
    binding_retry = None
    while time.monotonic() < min(deadline, binding_retry if binding_retry is not None else deadline):
        waiting_for_binding = False
        try:
            options = {'storage': storage} if storage is not None else {}
            if prepare_index:
                options['prepare_index'] = True
            if indexed_only:
                options['indexed_only'] = True
            last = _read_turn_references(root, directory, launch_id, native_id, generation, transitions, deadline,
                                         last, **options)
            if last is None:
                return unresolved  # The launch or conversation moved; no earlier view is current.
            if not last.pending_rows:
                return last.references + unresolved[len(last.references):]
        except _BeforeProviderDeadline:
            break  # No new transcript read began. Recheck the saved view's ownership below.
        except _ReadDeadline:
            # The deadline elapsed before the next view was established.
            return unresolved
        except Retry:
            last = None
            if not has_rows:
                return unresolved
            waiting_for_binding = True
        except _PendingTranscript:
            last = None
            if not has_rows:
                return unresolved
        except (AgentGone, Unavailable, TranscriptRowsChanged, OSError, ValueError, TypeError, KeyError):
            return unresolved
        moment = time.monotonic()
        if row_wait is not None and waiting_for_binding:
            # A busy binding can be retried briefly. Once it is available, the
            # provider still has its original read budget to prove stored rows.
            if binding_retry is None:
                binding_retry = min(deadline, moment + row_wait)
        else:
            binding_retry = None
            if row_wait is not None:
                deadline = min(deadline, moment + row_wait)
        remaining = (binding_retry if binding_retry is not None else deadline) - moment
        if remaining > 0:
            time.sleep(min(.025, remaining))
    if last is None:
        return unresolved
    try:
        last.snapshot.reader._validate()
    except (AgentGone, Unavailable, OSError, ValueError, TypeError, KeyError):
        return unresolved
    return last.references


def _read_turn_references(root, directory, launch_id, native_id, generation, transitions, deadline, previous,
                          *, storage=None, prepare_index=False, indexed_only=False):
    try:
        snap = _snapshot(root, directory, allow_navigation=True, reconcile=False, deadline=deadline)
    except _ReadDeadline as error:
        raise _BeforeProviderDeadline(str(error)) from None
    facts = records.runtime(snap.directory / 'runtime.json')
    current = ((snap.session.launch_id, snap.session.native_id, facts.get('session_generation', 0)) ==
               (launch_id, native_id, generation))
    if storage is None and not current:
        return None
    selected = None
    if storage is not None:
        if any(getattr(storage, field) != getattr(snap.session, field) for field in ('agent_id', 'harness', 'workspace')):
            return None
        if storage.home is not None:
            selected = _generation(storage)
        elif not current:
            return None
        if native.adapter_hook(snap.adapter, 'turn_cursor_projection') is not None:
            indexed = _indexed_turn_references(snap, selected, transitions)
            if indexed is not None:
                return indexed
            if prepare_index and previous is None and native.adapter_hook(snap.adapter, 'transcript_index') is not None:
                session = _selected_session(snap, selected)
                if session.home is not None and _provider(snap, 'transcript_index', session) is True:
                    indexed = _indexed_turn_references(snap, selected, transitions)
                    if indexed is not None:
                        return indexed
    if indexed_only:
        raise TurnIndexRequired
    loaded = _load(root, directory, generation=selected, snapshot=snap)
    project = native.adapter_hook(snap.adapter, 'project_turn_event')
    groups, by_row = _grouped(loaded)
    anchors = {indexes[0]: turn for turn, indexes in groups if turn.kind in ('prompt', 'command')}
    results = []
    fallback = ()
    pending_rows = False
    try:
        snap.reader._check_deadline()
        if (previous is not None and previous.generation == loaded.generation
                and previous.snapshot.universe_id == snap.universe_id and previous.view == loaded.view):
            snap.reader._check_deadline()
            previous.snapshot.reader._validate()
            fallback = previous.references
        for edge in transitions:
            snap.reader._check_deadline()
            if project is None:
                anchor = loaded.refs.get(edge.turn)
                row = loaded.refs.get(edge.ref)
                pending_rows |= edge.ref is not None and edge.ref not in loaded.refs
            else:
                chosen = _provider(snap, 'project_turn_event', loaded.session, loaded.view, edge)
                if not isinstance(chosen, TurnProjection):
                    results.append(TurnReference())
                    continue
                anchor = loaded.positions.get(chosen.turn_position)
                row = loaded.positions.get(chosen.row_position)
                pending_rows |= edge.ref is not None and row is None
            turn = anchors.get(anchor)
            if row is not None and (row not in by_row or turn is not None and turn.id != by_row[row]):
                row = None
            ref = _cursor(loaded, row) if row is not None else None
            results.append(TurnReference(turn=turn.id if turn is not None else None, ref=ref,
                                         generation=loaded.generation if ref is not None else None,
                                         kind=turn.kind if turn is not None else None))
        snap.reader.check()
    except _ReadDeadline:
        # Recheck only ownership. No further transcript reads may start after the cutoff.
        snap.reader._validate()
        # The unvisited suffix can keep its proof only from an identical validated view.
        results.extend(fallback[len(results):])
        pending_rows = False
    return _TurnRead(snap, loaded.generation, loaded.view, tuple(results), pending_rows)


def _indexed_turn_references(snapshot, selected, transitions):
    session = _selected_session(snapshot, selected)
    if session.home is None or selected is not None and _generation(session) != selected:
        return None  # The ordinary view supplies and proves legacy storage.
    projections = _provider(snapshot, 'turn_cursor_projection', session, transitions)
    if projections is None:
        return None
    if not isinstance(projections, tuple) or len(projections) != len(transitions):
        raise Unavailable('The harness returned invalid indexed turn evidence')
    generation = _generation(session)

    def cursor(position, nearest):
        return _pack(CURSOR_PREFIX, {'u': snapshot.universe_id, 'a': session.agent_id, 'g': generation,
                                    'p': position, 'r': nearest, 'h': False, 's': 'after', 'b': None})

    ready, pending_rows = [], False
    for edge, proof in zip(transitions, projections):
        snapshot.reader._check_deadline()
        if (not isinstance(proof, TurnCursorProjection) or type(proof.pending) is not bool
                or proof.kind not in (None, 'prompt', 'command')
                or any(value is not None and (not isinstance(value, str) or not value)
                       for value in (proof.turn_position, proof.turn_ref, proof.turn_nearest,
                                     proof.row_position, proof.row_nearest))):
            raise Unavailable('The harness returned invalid indexed turn evidence')
        pending_rows |= edge.ref is not None and proof.pending
        if proof.turn_position is None or proof.kind is None or proof.row_position is None:
            ready.append(TurnReference())
            continue
        anchor = proof.turn_ref or '@' + cursor(proof.turn_position, proof.turn_nearest)
        ready.append(TurnReference(turn=anchor, ref=cursor(proof.row_position, proof.row_nearest),
                                   generation=generation, kind=proof.kind))
    snapshot.reader.check()
    return _TurnRead(snapshot, generation, None, tuple(ready), pending_rows)


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
                    tip=loaded.view.tip, gap=loaded.view.gap, summary=loaded.view.summary)


def turn(root, directory, turn_id, *, after=None, limit=200, max_bytes=DEFAULT_BYTES, tip=None, generation=None):
    loaded = _load(root, directory, generation=generation, tip=tip)
    selected = next((indexes for item, indexes in _groups(loaded) if item.id == turn_id), None)
    if selected is None:
        raise ValueError('Unknown transcript turn')
    return _row_page(loaded, selected, after=after, limit=limit, max_bytes=max_bytes, history=True)


def info(root, directory, *, generation=None, tip=None):
    try:
        snapshot = _snapshot(root, directory, allow_navigation=generation is not None)
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
                                  earlier=tuple(earlier), retry=error.retry is True)
        view = loaded.view
        context_start = (view.history[view.context_from] if view.context_from < len(view.history) else None)
        grouped = _groups(loaded)
        snapshot.reader.check()
        return TranscriptInfo(state='supported', format=view.source.format, version=view.source.version,
                              native_id=loaded.session.native_id, generation=loaded.generation,
                              start=_cursor(loaded), end=_cursor(loaded, len(view.rows) - 1 if view.rows else None, boundary='end'),
                              rows=len(view.rows), bytes=view.source.size, updated_at=view.source.updated_at,
                              tip=view.tip, gap=view.gap, summary=view.summary, turns=len(grouped),
                              context_rows=len(view.history) - view.context_from,
                              context_turns=sum(item.in_context for item, _ in grouped),
                              context_start=(_cursor(loaded) if not view.context_from else
                                             _cursor(loaded, context_start, history=True, side='before', boundary='context')),
                              history_version=view.history_version, stable_rows=view.source.stable_rows,
                              earlier=tuple(earlier))
    except Unavailable as error:
        return TranscriptInfo(state=error.state, reason=str(error), retry=error.retry is True)
