"""Record metadata-only edges for waiting prompts and retained questions."""

import json
from dataclasses import replace
from pathlib import Path
import re
import time
from uuid import uuid4

from ..harnesses.types import NativeResult, PromptClosed, PromptOpened, PromptOutcomeQuery, SessionHandle, TurnStarted
from ..store import lock, open_replaced, read
from . import turn_events


_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_LABEL = re.compile(r'[a-z][a-z0-9_-]{0,63}')
_STATES = frozenset(('person', 'gone', 'answered', 'submitted', 'sent_first', 'unknown', 'refused'))
_MAX_TRANSITIONS = 256
_MAX_JSON = 64 * 1024
_CLOSED_LIMIT = 512
_TURN_READ_SECONDS = .5


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
            'open': {}, 'closed': [], 'pending': []}


def _enqueue(ledger, kind, launch, data):
    ledger['pending'].append({'key': uuid4().hex, 'kind': kind, 'launch': launch, 'data': data})


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
    if identity in ledger['closed']:
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
        _enqueue(ledger, 'prompt.closed', ledger['launch'], {
            'id': identity, 'kind': opened['kind'], 'state': transition.state, 'cause': transition.cause,
        })
        del ledger['open'][identity]
        ledger['closed'].append(identity)
        del ledger['closed'][:-_CLOSED_LIMIT]
    return isinstance(transition, PromptClosed) or opened_was_missing


def _close_open(agent_dir, ledger, cause):
    for opened in tuple(ledger['open'].values()):
        _edge(agent_dir, ledger, PromptClosed(**opened, state='gone', cause=cause), observed=False)
    end = turn_events.close_active(ledger.get('turns', {}))
    if end is not None:
        _enqueue(ledger, 'turn.ended', ledger['launch'], end)


def _select(agent_dir, ledger, launch, native_id, generation):
    if (ledger['launch'], ledger['native_id'], ledger['generation']) == (launch, native_id, generation):
        return False
    _close_open(agent_dir, ledger, 'switched' if ledger['launch'] == launch else 'stopped')
    task_seen = ledger['launch'] == launch and ledger.get('turns', {}).get('task_seen') is True
    ledger.update(launch=launch, native_id=native_id, generation=generation, closed=[])
    ledger['turns'] = {'task_seen': task_seen} if task_seen else {}
    return True


def _load(path):
    try:
        with open_replaced(path) as stream:
            ledger = json.load(stream)
    except FileNotFoundError:
        return _new_ledger()
    if (not isinstance(ledger, dict) or type(ledger.get('schema')) is not int or ledger['schema'] != 1
            or not isinstance(ledger.get('open'), dict) or not isinstance(ledger.get('closed'), list)
            or not isinstance(ledger.get('pending'), list) or 'generation' not in ledger
            or not isinstance(ledger.get('turns', {}), dict)):
        raise ValueError('Invalid saved prompt events')
    return ledger


def _edit(agent_dir, reduce, *, create=True, blocking=True):
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
        ledger = _load(path)
        if reduce(ledger):
            agent_files.write_json(path, ledger)


def stage(agent_dir, launch_id, native_session_id, transitions, *, generation=None):
    """Make the edges durable without invoking the plugin journal."""
    from ..universe import agents, records

    agent_dir = Path(agent_dir)
    if (len(transitions) > _MAX_TRANSITIONS
            or not all(_valid(transition) for transition in transitions)
            or not isinstance(native_session_id, str) or not native_session_id):
        return

    def update(ledger):
        facts = agents.runtime(agent_dir)
        if facts.get('launch_id') != launch_id or facts.get('state') not in ('starting', 'running', 'ending'):
            return False
        current = facts.get('session_generation', 0)
        if type(current) is not int or generation is not None and current != generation:
            return False
        if records.properties(agent_dir / 'properties.json').get('native_session_id') != native_session_id:
            return False
        changed = _select(agent_dir, ledger, launch_id, native_session_id, current)
        for transition in transitions:
            changed = _edge(agent_dir, ledger, transition) or changed
        return changed

    try:
        _edit(agent_dir, update, create=bool(transitions))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass  # Observation cannot fail the parent launch or its prompt handling.


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


def stage_turns(root, agent_dir, launch_id, native_id, edges, *, generation=None, task_launch=None, deadline=None):
    """Resolve observed turn metadata off-loop, and stage it only in its original conversation."""
    from ..universe import agents, records
    from . import transcripts

    limit = turn_events.MAX_BATCH
    if generation is not None:
        limit *= turn_events.MAX_RELAY_BATCHES
    if (not edges or len(edges) > limit or not all(turn_events.valid(edge) for edge in edges)
            or not isinstance(native_id, str) or not native_id):
        return

    def scope():
        facts = agents.runtime(agent_dir)
        if (facts.get('launch_id') != launch_id or facts.get('state') not in ('starting', 'running', 'ending')
                or type(facts.get('session_generation', 0)) is not int
                or generation is not None and facts.get('session_generation', 0) != generation):
            return None
        props = records.properties(agent_dir / 'properties.json')
        if props.get('native_session_id') == native_id:
            return facts, props
        return None

    try:
        current = scope()
        if current is None:
            return
        facts, props = current
        generation = facts.get('session_generation', 0)
        session = SessionHandle.from_records(props, facts, agent_dir)
        if deadline is None:
            deadline = time.monotonic() + _TURN_READ_SECONDS
        joined = turn_events.attribution(agent_dir, session, generation, edges, deadline=deadline)
        references = transcripts.turn_references(root, agent_dir, launch_id, native_id, generation, edges,
                                                 deadline=deadline)
        if task_launch is None:
            task_launch = (any(isinstance(edge, TurnStarted) and edge.first_task for edge in edges)
                           and _task_launch(agent_dir, launch_id))

        def update(ledger):
            live = scope()
            if live is None or any(live[1].get(key) != props.get(key) for key in ('id', 'harness', 'workspace')):
                return False
            changed = _select(agent_dir, ledger, launch_id, native_id, generation)
            turns = ledger.setdefault('turns', {})
            for index, (edge, reference) in enumerate(zip(edges, references)):
                for kind, data in turn_events.reduce(turns, edge, reference, mv2=index in joined,
                                                     message=joined.get(index), task_launch=task_launch):
                    _enqueue(ledger, kind, launch_id, data)
                    changed = True
            return changed

        _edit(agent_dir, update)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        pass  # Unavailable turn observations leave the status and lifecycle path usable.


def finish(agent_dir, launch_id, *, native_id=None, cause='stopped', previous=False):
    """Close the exact ending scope, or the older scope when a launch replaces it."""
    def update(ledger):
        if (not (ledger['open'] or ledger.get('turns', {}).get('active')) or (ledger['launch'] == launch_id) == previous
                or native_id is not None and ledger['native_id'] != native_id):
            return False
        if previous:
            from ..universe import agents

            if agents.runtime(Path(agent_dir)).get('launch_id') != launch_id:
                return False
        _close_open(agent_dir, ledger, cause)
        return True

    try:
        _edit(agent_dir, update, create=False)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass


def _journal_event(agent_dir, row):
    from ..plugins import events

    return events.prompt_event(agent_dir, row['kind'], row['launch'], row['data'], key=row['key'])


def publish(agent_dir):
    """Publish one ordered prefix at a time without holding the staging lock across journal I/O."""
    from ..plugins import events

    folder = Path(agent_dir) / 'prompts' / 'journal'
    if not (folder / '.lock').is_file() or folder.parent.is_symlink() or folder.is_symlink():
        return
    # Keep the fallback journal tail useful if the private receipts cannot be read.
    limit = max(1, min(events.TAIL_BYTES, events.SEGMENT_BYTES) // events.EVENT_BYTES - 1)
    try:
        with lock(folder / '.publish.lock', create_parent=False, blocking=False):
            while _publish_prefix(agent_dir, limit):
                pass
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        pass


def _publish_prefix(agent_dir, limit):
    from ..plugins import events

    rows = []

    def capture(ledger):
        rows.extend(ledger['pending'])
        return False

    _edit(agent_dir, capture, create=False)
    keys = []
    written = 0
    for row in rows:
        disposition = _journal_event(agent_dir, row)
        if disposition not in ('written', 'unsubscribed'):
            break
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
        if [row['key'] for row in ledger['pending'][:len(keys)]] != keys:
            return False
        del ledger['pending'][:len(keys)]
        acknowledged = True
        return True

    _edit(agent_dir, acknowledge, create=False)
    if acknowledged:
        events.acknowledge(agent_dir, keys)
    return acknowledged and (written == limit or len(keys) == len(rows))


def publish_ordered(agent_dir, kind, launch_id, data):
    """When earlier prompt edges are pending, queue the following lifecycle event behind them."""
    from ..plugins import events

    if isinstance(data.get('reason'), str):
        data = {**data, 'reason': events.short_reason(data['reason'])}
    staged = False

    def update(ledger):
        nonlocal staged
        if not ledger['pending']:
            return False
        _enqueue(ledger, kind, launch_id, data)
        staged = True
        return True

    try:
        _edit(agent_dir, update, create=False)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False
    if staged:
        publish(agent_dir)
    return staged


def leaving_wait(agent_dir, launch_id, facts):
    """Reconcile known blocking prompts before publishing the status event."""
    from ..plugins import events
    from . import effects, native

    if facts.get('state') in ('failed', 'stopped', 'dormant'):
        finish(agent_dir, launch_id)
        return

    opened = []
    native_id = generation = None

    def capture(ledger):
        nonlocal native_id, generation
        if ledger['launch'] == launch_id:
            opened.extend(item for item in ledger['open'].values() if item['requires_waiting'])
            native_id = ledger['native_id']
            generation = ledger['generation']
        return False

    try:
        _edit(agent_dir, capture, create=False)
        if not opened or (root := events.root_of(agent_dir)) is None:
            return
        _, adapter, session, executor = native.context(root, agent_dir.name)
        if (session.launch_id != launch_id or session.native_id != native_id
                or executor.facts.get('session_generation', 0) != generation
                or not callable(native.adapter_hook(adapter, 'prompt_outcome'))):
            return
        executor.check_session()
        closed = []
        for item in opened:
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
        stage(agent_dir, launch_id, native_id, tuple(closed), generation=generation)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        pass  # An unavailable or incomplete receipt cannot prove a prompt ended.
