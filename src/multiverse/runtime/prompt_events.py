"""Record metadata-only edges for waiting prompts and retained questions."""

import json
import logging
import os
from dataclasses import asdict, replace
from pathlib import Path
import re
import time
from uuid import uuid4

from ..harnesses.types import NativeResult, PromptClosed, PromptOpened, PromptOutcomeQuery, SessionHandle, TurnStarted
from ..store import lock, now, open_replaced, read
from . import turn_events


_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_LABEL = re.compile(r'[a-z][a-z0-9_-]{0,63}')
_STATES = frozenset(('person', 'gone', 'answered', 'submitted', 'sent_first', 'unknown', 'refused'))
_MAX_TRANSITIONS = 256
_MAX_JSON = 64 * 1024
_CLOSED_LIMIT = 512
_TURN_WORK_LIMIT = 512
_TURN_EDGE = '_turn.edge'
_TURN_BOUNDARY = '_turn.boundary'
_SAVED_TURNS = '_turn.batch'
_SAVED_PROMPTS = '_prompt.batch'
_WAIT_STATUS = '_prompt.status'
_PRIVATE_WORK = (_TURN_EDGE, _TURN_BOUNDARY, _WAIT_STATUS)
_TURN_KINDS = ('turn.started', 'turn.joined', 'turn.ended')
_TURN_PROJECTION_SECONDS = 60
_TURN_ROW_WAIT = .5
_UNREAD_PROPERTIES = object()


def _valid(transition):
    return (isinstance(transition, (PromptOpened, PromptClosed))
            and isinstance(transition.id, str) and _ID.fullmatch(transition.id)
            and isinstance(transition.kind, str) and _LABEL.fullmatch(transition.kind)
            and type(transition.requires_waiting) is bool
            and (transition.requires_waiting or transition.kind == 'question')
            and (not isinstance(transition, PromptClosed)
                 or isinstance(transition.state, str) and transition.state in _STATES
                 and (transition.cause is None
                      or isinstance(transition.cause, str) and _LABEL.fullmatch(transition.cause))))


def parse(value):
    """Decode the in-session callback without incorporating input into diagnostics."""
    try:
        if not isinstance(value, str) or len(value.encode()) > _MAX_JSON:
            raise ValueError
        rows = json.loads(value)
        if not isinstance(rows, list) or len(rows) > _MAX_TRANSITIONS:
            raise ValueError
        transitions = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError
            edge = row.pop('type', None)
            if edge == 'opened':
                transition = PromptOpened(**row)
            elif edge == 'closed':
                transition = PromptClosed(**row)
            else:
                raise ValueError
            if not _valid(transition):
                raise ValueError
            transitions.append(transition)
        return tuple(transitions)
    except (TypeError, ValueError, RecursionError):
        raise ValueError('Invalid prompt-event metadata') from None


def _new_ledger(launch=None, native_id=None):
    return {'schema': 1, 'launch': launch, 'native_id': native_id, 'generation': None,
            'open': {}, 'closed': [], 'pending': [], 'turn_scope': False}


def _enqueue(ledger, kind, launch, data, *, at=None, key=None):
    row = {'key': key if key is not None else uuid4().hex, 'kind': kind, 'launch': launch,
           'data': data, 'at': at if at is not None else now()}
    ledger['pending'].append(row)
    return row


def _prompt_scope(ledger):
    return ledger['launch'], ledger['native_id'], ledger['generation']


def _wait_target(scope, identity):
    return json.dumps([*scope, identity], separators=(',', ':'))


def _answer_outcome(agent_dir, ledger, transition):
    from .prompts import _record

    try:
        record = _record(agent_dir, transition.id, missing_ok=True)
        if record is None or (record['session']['launch_id'], record['session']['native_id']) != (
                ledger['launch'], ledger['native_id']):
            return transition
        saved = replace(transition, state=record['result']['state'], cause=record['result']['cause'])
        return saved if _valid(saved) else replace(saved, cause=None)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return transition


def _edge(agent_dir, ledger, transition, *, observed=True):
    """An opening edge and its closure are immutable once accepted."""
    identity = transition.id
    target = _wait_target(_prompt_scope(ledger), identity)
    if identity in ledger['closed'] or target in ledger.get('close_rows', {}):
        return False
    opened = ledger['open'].get(identity)
    opened_was_missing = opened is None
    if opened_was_missing:
        opened = {'id': identity, 'kind': transition.kind, 'requires_waiting': transition.requires_waiting}
        ledger['open'][identity] = opened
        _enqueue(ledger, 'prompt.opened', ledger['launch'], opened)
    if isinstance(transition, PromptClosed):
        if observed:
            transition = _answer_outcome(agent_dir, ledger, transition)
        close = _enqueue(ledger, 'prompt.closed', ledger['launch'], {
            'id': identity, 'kind': opened['kind'], 'state': transition.state, 'cause': transition.cause,
        })
        del ledger['open'][identity]
        ledger['closed'].append(identity)
        if target in ledger.get('wait_targets', {}):
            ledger.setdefault('close_rows', {})[target] = close
        del ledger['closed'][:-_CLOSED_LIMIT]
    return isinstance(transition, PromptClosed) or opened_was_missing


def _close_open(agent_dir, ledger, cause):
    for opened in tuple(ledger['open'].values()):
        _edge(agent_dir, ledger, PromptClosed(**opened, state='gone', cause=cause), observed=False)
    if ledger['turn_scope']:
        _enqueue(ledger, _TURN_BOUNDARY, ledger['launch'], {})
        ledger['turn_scope'] = False


def _select(agent_dir, ledger, launch, native_id, generation):
    if (ledger['launch'], ledger['native_id'], ledger['generation']) == (launch, native_id, generation):
        return False
    _close_open(agent_dir, ledger, 'switched' if ledger['launch'] == launch else 'stopped')
    ledger.update(launch=launch, native_id=native_id, generation=generation, closed=[])
    return True


def _load(path, *, expected_stamp=None):
    try:
        with open_replaced(path) as stream:
            ledger = json.load(stream)
            if expected_stamp is not None and _file_stamp(os.fstat(stream.fileno())) != expected_stamp:
                return None
    except FileNotFoundError:
        return None if expected_stamp is not None else _new_ledger()
    if (not isinstance(ledger, dict) or type(ledger.get('schema')) is not int or ledger['schema'] != 1
            or not isinstance(ledger.get('open'), dict) or not isinstance(ledger.get('closed'), list)
            or not isinstance(ledger.get('pending'), list) or 'generation' not in ledger
            or not isinstance(ledger.get('turns', {}), dict)):
        raise ValueError('Invalid saved prompt events')
    if 'turn_scope' not in ledger:
        ledger['turn_scope'] = bool(ledger.get('turns'))
        ledger['turn_launch'] = ledger['launch']
    overflow = ledger.get('overflow')
    if (not isinstance(ledger.get('front', []), list)
            or not isinstance(ledger.get('close_rows', {}), dict)
            or not isinstance(ledger.get('superseded_closes', {}), dict)
            or not isinstance(ledger.get('wait_targets', {}), dict)
            or overflow is not None and (not isinstance(overflow, dict)
                or any(type(overflow.get(key)) is not int for key in ('clean', 'head', 'tail'))
                or not 0 <= overflow['clean'] <= overflow['head'] <= overflow['tail'])):
        raise ValueError('Invalid ordered event overflow')
    return ledger


def _edit(agent_dir, reduce, *, create=True, blocking=True, expected_stamp=None):
    """Persist the ordered outbox before admitting attempts to publish it."""
    from ..plugins import agent_files

    path = Path(agent_dir) / 'prompts' / 'journal' / 'events.json'
    guard = path.with_name('.lock')
    if not create and not guard.is_file():
        return
    if path.parent.parent.is_symlink() or path.parent.is_symlink():
        return
    if create:
        path.parent.parent.mkdir(exist_ok=True)
        path.parent.mkdir(exist_ok=True)
    with lock(guard, create_parent=False, blocking=blocking):
        ledger = _load(path, expected_stamp=expected_stamp)
        if ledger is not None and reduce(ledger):
            _spill_pending(path, ledger)
            try:
                agent_files.write_json(path, ledger)
            except OSError:
                # The same serialized state and event keys are safe to write again under this lock.
                agent_files.write_json(path, ledger)
            return True


def _overflow_path(path, index):
    return path.with_name(f'overflow-{index}.json')


def _overflowed(ledger):
    overflow = ledger.get('overflow')
    return overflow is not None and overflow['head'] < overflow['tail']


def _spill_pending(path, ledger):
    """Write immutable overflow before its pointer is committed with the shortened active queue."""
    from ..plugins import agent_files

    pending = ledger['pending']
    extra = len(pending) - _TURN_WORK_LIMIT
    if extra <= 0:
        return
    overflow = dict(ledger.get('overflow', {'clean': 0, 'head': 0, 'tail': 0}))
    for first in range(0, extra, _TURN_WORK_LIMIT):
        rows = pending[first:min(first + _TURN_WORK_LIMIT, extra)]
        for row in rows:
            if row['kind'] == _TURN_EDGE:
                row['data']['unresolved'] = True
        segment = _overflow_path(path, overflow['tail'])
        try:
            agent_files.write_json(segment, rows)
        except OSError:
            # No ledger references this segment yet. Retry its atomic write in place.
            try:
                agent_files.write_json(segment, rows)
            except OSError:
                # Keep an admitted status in order even if the separate spill
                # cannot be saved. This reserve is also bounded, and the next
                # successful write can spill it in normal order.
                if len(pending) > 2 * _TURN_WORK_LIMIT:
                    raise
                for row in pending[:extra]:
                    if row['kind'] == _TURN_EDGE:
                        row['data']['unresolved'] = True
                return
        overflow['tail'] += 1
    ledger['overflow'] = overflow
    del pending[:extra]


def _head(ledger):
    return ledger.get('front') or ([] if _overflowed(ledger) else ledger['pending'])


def _promote(agent_dir, *, blocking):
    """Copy the oldest segment into the ledger, retaining its source until publication is acknowledged."""
    path = Path(agent_dir) / 'prompts' / 'journal' / 'events.json'
    stamp = outbox_stamp(agent_dir)
    if stamp is None:
        return

    def promote(ledger):
        overflow = ledger.get('overflow')
        tail = overflow['tail'] if overflow is not None else 0
        # The staging lock and checked ledger establish that these prepares
        # were never admitted. No committed segment has an index at or above tail.
        for number in range(tail, tail + turn_events.MAX_RELAY_BATCHES):
            try:
                _overflow_path(path, number).unlink()
            except FileNotFoundError:
                break
        if overflow is None:
            return False
        in_flight = bool(ledger.get('front'))
        complete = overflow['head'] - int(in_flight)
        removed = overflow['clean'] < complete
        if removed:
            _overflow_path(path, overflow['clean']).unlink(missing_ok=True)
            overflow['clean'] += 1
        if in_flight or not _overflowed(ledger):
            return removed
        segment = _overflow_path(path, overflow['head'])
        with open_replaced(segment) as stream:
            rows = json.load(stream)
        if not isinstance(rows, list) or not rows:
            raise ValueError('Invalid ordered event overflow segment')
        ledger['front'] = rows
        overflow['head'] += 1
        return True

    _edit(agent_dir, promote, create=False, blocking=blocking, expected_stamp=stamp)


def stage(agent_dir, launch_id, native_session_id, transitions, *, generation=None):
    """Make the edges durable without invoking the plugin journal."""
    from ..universe import agents

    return agents.stage_native_batch(agent_dir, launch_id, native_session_id, transitions, generation=generation)


def record(agent_dir, launch_id, native_session_id, transitions):
    """Accept and try to publish edges for this exact launch and native conversation."""
    stage(agent_dir, launch_id, native_session_id, transitions)
    publish(agent_dir)


def _task_launch(agent_dir, launch_id):
    from ..harnesses.types import normalize_session_id

    try:
        if normalize_session_id(launch_id) != launch_id:
            return False
        request = read(agent_dir / 'launches' / launch_id / 'request.json')
        return isinstance(request, dict) and request.get('task_launch') is True
    except (OSError, ValueError):
        return False


def stage_turns(root, agent_dir, launch_id, native_id, edges, *, generation=None, task_launch=None):
    """Keep native metadata in its original scope so publication can resolve cursors later."""
    from ..universe import agents

    return agents.stage_native_batch(agent_dir, launch_id, native_id, edges, generation=generation, root=root,
                                     task_launch=task_launch)


def prepare_native_batch(agent_dir, facts, native_id, transitions, *, root=None, task_launch=None, relay=False,
                         warn_prompt_scope=False, properties=_UNREAD_PROPERTIES):
    """Bind a validated batch to its observed launch and native scope before admission."""
    from ..plugins import events
    from ..universe import records

    if not isinstance(native_id, str) or not native_id:
        return None
    generation = facts.get('session_generation', 0)
    if type(generation) is not int:
        return None
    try:
        props = (records.properties(agent_dir / 'properties.json')
                 if properties is _UNREAD_PROPERTIES else properties)
        if props is None:
            raise ValueError('Unavailable conversation binding')
    except (OSError, ValueError):
        if warn_prompt_scope:
            logging.getLogger(__name__).warning('Could not verify the conversation for prompt transitions')
        return None
    if props.get('native_session_id') != native_id:
        if warn_prompt_scope:
            logging.getLogger(__name__).warning('Ignored prompt transitions for another conversation')
        return None
    if not transitions or facts.get('state') not in ('starting', 'running', 'ending'):
        return None
    try:
        scope = {'native_id': native_id, 'generation': generation}
        if root is None:
            if len(transitions) > _MAX_TRANSITIONS or not all(map(_valid, transitions)):
                return None
            encoded = [{'closed': isinstance(edge, PromptClosed), 'data': asdict(edge)} for edge in transitions]
            return _SAVED_PROMPTS, {'scope': scope, 'edges': encoded}
        limit = turn_events.MAX_BATCH * (turn_events.MAX_RELAY_BATCHES if relay else 1)
        if (len(transitions) > limit or not all(map(turn_events.valid, transitions))
                or not events.subscribers(root, _TURN_KINDS)):
            return None
        scope.update(agent_id=props['id'], harness=props['harness'], workspace=props['workspace'],
                     home=props.get('native_session_home'))
        if task_launch is None:
            task_launch = (any(isinstance(edge, TurnStarted) and edge.first_task for edge in transitions)
                           and _task_launch(agent_dir, facts['launch_id']))
        return _SAVED_TURNS, {'scope': scope, 'edges': [turn_events.encode(edge) for edge in transitions],
                              'task_launch': task_launch is True}
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        return None


def finish(agent_dir, launch_id, *, native_id=None, cause='stopped', previous=False):
    """Close the exact ending scope, or the older scope when a launch replaces it."""
    def update(ledger):
        current = None
        if previous:
            from ..universe import agents

            current = agents.runtime(Path(agent_dir)).get('launch_id')
        return _close_scope(agent_dir, ledger, launch_id, current, native_id=native_id,
                            cause=cause, previous=previous)

    try:
        _edit(agent_dir, update, create=False)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass


def _close_scope(agent_dir, ledger, launch_id, current_launch, *, native_id=None, cause='stopped', previous=False):
    if (not (ledger['open'] or ledger['turn_scope']) or (ledger['launch'] == launch_id) == previous
            or native_id is not None and ledger['native_id'] != native_id
            or previous and current_launch != launch_id):
        return False
    _close_open(agent_dir, ledger, cause)
    return True


def _journal_event(agent_dir, row):
    from ..plugins import events

    return events.prompt_event(agent_dir, row['kind'], row['launch'], row['data'], key=row['key'], at=row.get('at'))


def publish(agent_dir, *, resolve_turns=False, indexed_only=False, recovery_stop=None):
    """Publish one ordered prefix at a time without holding the staging lock across journal I/O."""
    from ..plugins import events

    folder = Path(agent_dir) / 'prompts' / 'journal'
    if not (folder / '.lock').is_file() or folder.parent.is_symlink() or folder.is_symlink():
        return
    # Keep the fallback journal tail useful if the private receipts cannot be read.
    limit = max(1, min(events.TAIL_BYTES, events.SEGMENT_BYTES) // events.EVENT_BYTES - 1)
    try:
        with lock(folder / '.publish.lock', create_parent=False, blocking=False):
            if recovery_stop is not None:
                if not recovery_stop.is_set():
                    _promote(agent_dir, blocking=False)
                    _publish_prefix(agent_dir, limit, recovery_stop=recovery_stop)
                return
            projected = False
            while True:
                if resolve_turns:
                    _promote(agent_dir, blocking=True)
                if _publish_prefix(agent_dir, limit):
                    continue
                if not resolve_turns or projected or not _resolve_turn_prefix(agent_dir, indexed_only=indexed_only):
                    break
                projected = True
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        pass


def _turn_prefix(ledger):
    """Group pending edges of the head conversation across intervening journal entries."""
    pending = _head(ledger)
    if not pending or pending[0]['kind'] not in _PRIVATE_WORK:
        return ()
    head = pending[0]
    if head['kind'] != _TURN_EDGE:
        return (head,)
    scope = (head['launch'], head['data']['scope'])
    rows = []
    for row in pending:
        if row['kind'] in (_TURN_BOUNDARY, _WAIT_STATUS):
            break
        if row['kind'] == _TURN_EDGE:
            if (row['launch'], row['data']['scope']) != scope:
                break
            rows.append(row)
            if len(rows) == _TURN_WORK_LIMIT:
                break
    return tuple(rows)


def _file_stamp(status):
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns


def outbox_stamp(agent_dir):
    """A stat-only check for changes to the outbox an idle host already saw."""
    path = Path(agent_dir) / 'prompts' / 'journal' / 'events.json'
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return _file_stamp(status)


def turn_work(agent_dir, stamp):
    """Whether the outbox is empty and the private head, if a worker can project it."""
    empty, keys = False, None

    def capture(ledger):
        nonlocal empty, keys
        empty = not _head(ledger) and not _overflowed(ledger)
        keys = tuple(row['key'] for row in _turn_prefix(ledger)) or None
        return False

    try:
        _edit(agent_dir, capture, create=False, blocking=False, expected_stamp=stamp)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass
    return empty, keys


def abandon_turn_projection(agent_dir, keys):
    """A failed worker's batch keeps its positions in the outbox but has no further reads."""
    wait = None

    def update(ledger):
        nonlocal wait
        rows = _turn_prefix(ledger)
        if not rows or rows[0]['key'] != keys[0]:
            return False
        if rows[0]['kind'] == _WAIT_STATUS:
            wait = rows[0]
            return False
        if rows[0]['kind'] != _TURN_EDGE:
            return False
        admitted = set(keys)
        for row in rows:
            if row['key'] not in admitted:
                break
            row['data']['unresolved'] = True
        return True

    try:
        _edit(agent_dir, update, create=False, blocking=False)
        if wait is not None:
            resolve_wait_status(agent_dir, wait, after_failure=True)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass


def _project_pending_turns(agent_dir, rows, *, indexed_only=False):
    from ..plugins import events
    from . import transcripts

    scope = rows[0]['data']['scope']
    session = SessionHandle(agent_id=scope['agent_id'], harness=scope['harness'], workspace=scope['workspace'],
                            launch_id=rows[0]['launch'], native_id=scope['native_id'], home=scope['home'],
                            session_file=str(Path(agent_dir) / 'session.jsonl'))
    edges = tuple(turn_events.decode(row['data']['edge']) for row in rows)
    eligible = [index for index, (row, edge) in enumerate(zip(rows, edges))
                if edge.ref is not None and not row['data'].get('unresolved')]
    references = {}
    try:
        joined = turn_events.attribution(agent_dir, session, scope['generation'], edges,
                                         deadline=time.monotonic() + _TURN_ROW_WAIT)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        joined = {}
    if eligible and (root := events.root_of(agent_dir)) is not None:
        try:
            proved = transcripts.turn_references(root, agent_dir, session.launch_id, session.native_id,
                                                  scope['generation'], tuple(edges[index] for index in eligible),
                                                  storage=session, row_wait=_TURN_ROW_WAIT,
                                                  prepare_index=not indexed_only, indexed_only=indexed_only,
                                                  deadline=time.monotonic() + _TURN_PROJECTION_SECONDS)
        except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
            proved = ()  # Keep the observed lifecycle event even if this transcript cannot be proved.
        references.update(zip(eligible, proved))
    return {row['key']: (edge, references.get(index, transcripts.TurnReference()), index in joined, joined.get(index))
            for index, (row, edge) in enumerate(zip(rows, edges))}


def _resolve_turn_prefix(agent_dir, *, indexed_only=False):
    """Only the host calls this under the publication lock. Transcript I/O never holds the outbox lock."""
    from .transcripts import TurnReference

    rows = ()

    def capture(ledger):
        nonlocal rows
        rows = _turn_prefix(ledger)
        return False

    _edit(agent_dir, capture, create=False)
    if not rows:
        return False
    if rows[0]['kind'] == _WAIT_STATUS:
        return resolve_wait_status(agent_dir, rows[0])
    boundary = rows[0]['kind'] == _TURN_BOUNDARY
    projections = {} if boundary else _project_pending_turns(agent_dir, rows, indexed_only=indexed_only)
    keys = [row['key'] for row in rows]
    replaced = False

    def update(ledger):
        nonlocal replaced
        if [row['key'] for row in _turn_prefix(ledger)][:len(keys)] != keys:
            return False
        turns = ledger.setdefault('turns', {})
        launch = rows[0]['launch']
        if ledger.get('turn_launch') != launch:
            ledger['turn_launch'] = launch
            turns.clear()
        head = _head(ledger)
        pending = []
        for row in head:
            key = row['key']
            if boundary and key == keys[0]:
                ended = turn_events.close_active(turns)
                task_seen = turns.get('task_seen') is True
                turns.clear()
                if task_seen:
                    turns['task_seen'] = True
                made = () if ended is None else (('turn.ended', ended),)
            elif key in projections:
                edge, reference, mv2, message = projections[key]
                if row['data'].get('unresolved'):
                    reference = TurnReference()  # A concurrent writer may have downgraded this entry.
                made = turn_events.reduce(turns, edge, reference, mv2=mv2, message=message,
                                          task_launch=row['data']['task_launch'])
            else:
                pending.append(row)
                continue
            for index, (kind, data) in enumerate(made):
                pending.append({'key': key if index == 0 else uuid4().hex, 'kind': kind,
                                'launch': row['launch'], 'data': turn_events.public(data), 'at': row.get('at')})
        head[:] = pending
        replaced = True
        return True

    _edit(agent_dir, update, create=False)
    return replaced


def _publish_prefix(agent_dir, limit, *, recovery_stop=None):
    from ..plugins import events

    rows = []
    superseded = set()

    def capture(ledger):
        pending = _head(ledger)
        rows.extend(pending if recovery_stop is None else pending[:limit])
        superseded.update(ledger.get('superseded_closes', ()))
        return False

    _edit(agent_dir, capture, create=False, blocking=recovery_stop is None)
    keys = []
    written = 0
    for row in rows:
        if recovery_stop is not None and recovery_stop.is_set():
            break
        if row['kind'] in _PRIVATE_WORK:
            break  # A private row needs proof before it can be published.
        if row['key'] in superseded:
            keys.append(row['key'])
            continue
        disposition = _journal_event(agent_dir, row)
        if disposition not in ('written', 'unsubscribed'):
            break
        if row.get('wait_proof'):
            try:
                _wait_proof_path(agent_dir, row['key']).unlink(missing_ok=True)
            except OSError:
                break  # The journal receipt makes the append safe to retry until cleanup succeeds.
        keys.append(row['key'])
        if disposition == 'written':
            written += 1
            if written == limit:
                break
    if not keys:
        return False
    acknowledged = False

    def acknowledge(ledger):
        nonlocal acknowledged
        head = _head(ledger)
        if [row['key'] for row in head[:len(keys)]] != keys:
            return False
        replaced = ledger.get('superseded_closes', {})
        for row in head[:len(keys)]:
            replaced.pop(row['key'], None)
        del head[:len(keys)]
        acknowledged = True
        return True

    _edit(agent_dir, acknowledge, create=False, blocking=recovery_stop is None)
    if acknowledged:
        events.acknowledge(agent_dir, keys)
    return acknowledged and (written == limit or len(keys) == len(rows))


def wants_status_intent(agent_dir):
    """A status that contributes to an ordered lane needs a durable position on commit."""
    from ..plugins import events

    try:
        if outbox_stamp(agent_dir) is not None:
            return True
        root = events.root_of(agent_dir)
        return root is not None and bool(events.subscribers(root, ('agent.status', *_TURN_KINDS)))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def _stage_saved_batch(agent_dir, ledger, launch_id, kind, data, at):
    scope = data['scope']
    _select(agent_dir, ledger, launch_id, scope['native_id'], scope['generation'])
    if kind == _SAVED_TURNS:
        ledger['turn_scope'] = True
        for edge in data['edges']:
            _enqueue(ledger, _TURN_EDGE, launch_id,
                     {'edge': edge, 'scope': scope, 'task_launch': data['task_launch']}, at=at)
    elif kind == _SAVED_PROMPTS:
        for item in data['edges']:
            edge = (PromptClosed if item['closed'] else PromptOpened)(**item['data'])
            _edge(agent_dir, ledger, edge)
    else:
        raise ValueError('Invalid saved native batch')


def stage_ordered(agent_dir, kind, launch_id, data, *, at=None, terminal=False, key=None, required=False,
                  current_launch=None, close_scope=None, blocking=True, predecessors=()):
    """Keep a subsequent event in the agent's ordered journal without publishing it."""
    from ..plugins import events

    if isinstance(data.get('reason'), str):
        data = {**data, 'reason': events.short_reason(data['reason'])}
    staged = None
    status = kind == 'agent.status'

    def update(ledger):
        nonlocal staged
        if key is not None and ledger.get('last_status_intent') == key:
            # This exact runtime intent already committed to the outbox. Its
            # row might be on disk or already acknowledged.
            staged = True
            return False
        for saved_kind, saved_data in predecessors:
            _stage_saved_batch(agent_dir, ledger, launch_id, saved_kind, saved_data, at)
        if kind in (_SAVED_TURNS, _SAVED_PROMPTS):
            _stage_saved_batch(agent_dir, ledger, launch_id, kind, data, at)
            ledger['last_status_intent'] = key
            staged = True
            return True
        if close_scope is not None:
            _close_scope(agent_dir, ledger, launch_id, current_launch, **close_scope)
        opened = []
        if status:
            matching_scope = ledger['launch'] == launch_id
            if not matching_scope and current_launch in (None, launch_id):
                _close_open(agent_dir, ledger, 'stopped')
            elif matching_scope and data['from'] == 'waiting':
                if terminal:
                    _close_open(agent_dir, ledger, 'stopped')
                else:
                    opened = [item for item in ledger['open'].values() if item['requires_waiting']]
        if not (required or predecessors or opened or watch_turns or _head(ledger) or _overflowed(ledger)
                or status and ledger['launch'] == launch_id and ledger['turn_scope']):
            return False
        if opened:
            wait = {'status': data, 'opened': opened, 'native_id': ledger['native_id'],
                    'generation': ledger['generation']}
            staged = _enqueue(ledger, _WAIT_STATUS, launch_id, wait, at=at, key=key)
            targets = ledger.setdefault('wait_targets', {})
            for item in opened:
                target = _wait_target(_prompt_scope(ledger), item['id'])
                targets.setdefault(target, []).append(staged['key'])
        else:
            staged = _enqueue(ledger, kind, launch_id, data, at=at, key=key)
        if key is not None:
            ledger['last_status_intent'] = key
        return True

    try:
        watch_turns = (not required and status and (root := events.root_of(agent_dir)) is not None
                       and bool(events.subscribers(root, _TURN_KINDS)))
        _edit(agent_dir, update, create=required or watch_turns or bool(predecessors), blocking=blocking)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False  # A failed outbox read or save gives no authority to bypass older events.
    return staged


def publish_ordered(agent_dir, kind, launch_id, data, *, at=None, close_scope=None):
    """When earlier edges are pending, queue the following lifecycle event behind them."""
    from ..universe import agents

    staged = agents.stage_after_status(agent_dir, kind, launch_id, data, at=at, close_scope=close_scope)
    if staged:
        publish(agent_dir)
    return staged is not None


def _wait_receipts(agent_dir, launch_id, data):
    """Prove only closures in the waiting scope captured with the status commit."""
    from ..plugins import events
    from . import effects, native

    try:
        if (root := events.root_of(agent_dir)) is None:
            return ()
        _, adapter, session, executor = native.context(root, agent_dir.name)
        if (session.launch_id != launch_id or session.native_id != data['native_id']
                or executor.facts.get('session_generation', 0) != data['generation']
                or not callable(native.adapter_hook(adapter, 'prompt_outcome'))):
            return ()
        executor.check_session()
        closed = []
        for item in data['opened']:
            answer = PromptOutcomeQuery(id=item['id'])
            try:
                reply = effects.call(adapter, 'prompt_outcome', session, answer, executor)
            except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
                continue  # Another prompt's receipt can still prove its own closure.
            if (isinstance(reply, NativeResult) and reply.state == 'ok' and isinstance(reply.value, dict)
                    and reply.value.get('closed') is True):
                edge = PromptClosed(**item, state=reply.value.get('state'), cause=reply.value.get('cause'))
                if _valid(edge):
                    closed.append(edge)
        return tuple(closed)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        return ()  # An unavailable or incomplete receipt cannot prove a prompt ended.


def _finish_wait(ledger, agent_dir, reserved, closed):
    for queue in (ledger.get('front', ()), ledger['pending']):
        for index, row in enumerate(queue):
            if row['key'] == reserved['key'] and row['kind'] == _WAIT_STATUS:
                data = row['data']
                scope = row['launch'], data['native_id'], data['generation']
                tail = len(ledger['pending'])
                if scope == _prompt_scope(ledger):
                    for edge in closed:
                        if edge.id in ledger['open']:
                            _edge(agent_dir, ledger, edge)
                preceding = ledger['pending'][tail:]
                del ledger['pending'][tail:]
                emitted = {item['data']['id'] for item in preceding}
                for edge in closed:
                    if edge.id in emitted:
                        continue
                    later = _take_wait_close(ledger, queue, index, _wait_target(scope, edge.id))
                    if later is not None:
                        if later['data']['cause'] in ('switched', 'stopped'):
                            later = {**later, 'data': {**later['data'], 'state': edge.state, 'cause': edge.cause}}
                        preceding.append(later)
                        emitted.add(edge.id)
                targets = ledger.get('wait_targets', {})
                for item in data['opened']:
                    target = _wait_target(scope, item['id'])
                    waiting = targets.get(target, [])
                    if row['key'] in waiting:
                        waiting.remove(row['key'])
                    if not waiting:
                        targets.pop(target, None)
                        ledger.get('close_rows', {}).pop(target, None)
                status = {**row, 'kind': 'agent.status', 'data': data['status'], 'wait_proof': True}
                queue[index:index + 1] = [*preceding, status]
                return True
    return False


def _take_wait_close(ledger, queue, index, target):
    """A duplicate close admitted during receipt proof belongs before the reserved status."""
    saved = ledger.get('close_rows', {})
    close = saved.get(target)
    if not close:
        return None
    key = close['key']
    sources = [(queue, index + 1)]
    if queue is not ledger['pending']:
        sources.append((ledger['pending'], 0))
    for rows, start in sources:
        for i in range(start, len(rows)):
            if rows[i]['key'] == key:
                saved[target] = {}
                return rows.pop(i)
    if queue is ledger.get('front') and _overflowed(ledger):
        # The duplicate is staged in an immutable later segment. Move its outcome
        # without reading or rewriting that segment from the report path.
        saved[target] = {}
        ledger.setdefault('superseded_closes', {})[key] = True
        return {**close, 'key': uuid4().hex}
    return None


def _wait_proof_path(agent_dir, key):
    if not isinstance(key, str) or not _ID.fullmatch(key):
        raise ValueError('Invalid wait receipt key')
    return Path(agent_dir) / 'prompts' / 'journal' / f'wait-proof-{key}.json'


def resolve_wait_status(agent_dir, reserved, *, after_failure=False):
    """Fill a reserved status after a local receipt read, outside both staging and runtime locks."""
    if reserved['kind'] != _WAIT_STATUS:
        return False
    from ..plugins import agent_files

    key = reserved['key']
    try:
        proof = _wait_proof_path(agent_dir, key)
        data = reserved['data']
        scope = reserved['launch'], data['native_id'], data['generation']
        captured = {(item['id'], item['kind'], item['requires_waiting']) for item in data['opened']}
        with lock(proof.with_name('.wait-proof.lock'), create_parent=False, blocking=False):
            try:
                with open_replaced(proof) as stream:
                    closed = tuple(PromptClosed(**value) for value in json.load(stream))
                if not all(_valid(edge) and (edge.id, edge.kind, edge.requires_waiting) in captured for edge in closed):
                    raise ValueError('Invalid staged wait receipt')
            except (OSError, ValueError, TypeError):
                closed = ()
                active = False

                def capture(ledger):
                    nonlocal active
                    targets = ledger.get('wait_targets', {})
                    active = any(key in targets.get(_wait_target(scope, item['id']), ()) for item in data['opened'])
                    return False

                if not after_failure:
                    _edit(agent_dir, capture, create=False)
                    if active:
                        closed = tuple(edge for edge in _wait_receipts(agent_dir, reserved['launch'], data)
                                       if (edge.id, edge.kind, edge.requires_waiting) in captured)
                        if closed:
                            try:
                                agent_files.write_json(proof, [asdict(edge) for edge in closed])
                            except OSError:
                                pass  # Try to fill the current slot even if the proof file could not be saved.
            filled = _edit(agent_dir, lambda ledger: _finish_wait(ledger, agent_dir, reserved, closed), create=False)
            if filled:
                proof.unlink(missing_ok=True)
            return bool(filled)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False  # The host can resume a durable reservation.
