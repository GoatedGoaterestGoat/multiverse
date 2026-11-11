"""Bounded, read-only queue observations joined to mv2's exact delivery identities."""
from contextvars import copy_context
from datetime import datetime
import json
from pathlib import Path
import threading
import time

from .. import universe
from ..harnesses.types import NativeResult, QueuedPrompt, QueueReport, SessionHandle
from ..plugins import access
from ..store import now, open_regular, open_replaced
from ..universe import agents, mailbox, records
from . import effects, native

BUDGET = 2.0
ENTRIES = 1024
PAYLOAD_LIMIT = 256 << 10
HISTORY_LIMIT = 8 << 20
_running = {}
_lock = threading.Lock()


def _unknown(reason, unconfirmed=None):
    return QueueReport(state='unavailable', reason=reason, observed_at=now()), frozenset(), unconfirmed


def _text(value, limit=512):
    return isinstance(value, str) and bool(value) and '\0' not in value and len(value.encode('utf-8')) <= limit


def _time(value):
    if value is None:
        return None
    if not _text(value, 128):
        raise ValueError('Invalid queue timestamp')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Queue timestamps must include a timezone')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Repeated queue evidence field')
        result[key] = value
    return result


def _bytes(path, *, replaced=False):
    try:
        with (open_replaced(path) if replaced else open_regular(path)) as stream:
            value = stream.read(HISTORY_LIMIT + 1)
    except FileNotFoundError:
        return b''
    if len(value) > HISTORY_LIMIT:
        raise ValueError('The message history exceeds the queue-read budget')
    return value


def _context_owner(state, attempts, logical_id):
    """Top-level context may be borrowed only by one unambiguously corresponding attempt."""
    saved = state.get('delivery_context')
    if not isinstance(saved, dict):
        return None
    owner = None
    for attempt in attempts:
        if (isinstance(attempt, dict) and attempt.get('launch_id') == saved.get('launch_id')
                and attempt.get('native_session_id') == saved.get('native_id')
                and attempt.get('native_message_id', logical_id) == saved.get('native_message_id', logical_id)):
            if owner is not None:
                return None
            owner = attempt
    return owner


def _scope(attempt, state, session, identifier, logical_id, context_owner):
    """An older launch needs saved delivery scope; a matching native ID alone is insufficient."""
    if not session.launch_id or not session.native_id:
        return False
    if attempt.get('launch_id') == session.launch_id:
        return True
    saved = attempt.get('delivery_context')
    if saved is None and attempt is context_owner:
        saved = state.get('delivery_context')
    if (not isinstance(saved, dict) or not _text(attempt.get('launch_id'), 128)
            or saved.get('launch_id') != attempt['launch_id'] or saved.get('native_id') != session.native_id
            or saved.get('native_message_id', logical_id) != identifier):
        return False
    return all(isinstance(saved.get(key), str) and Path(saved[key]).is_absolute()
               and saved[key] == getattr(session, key) for key in ('home', 'workspace'))


def _ledger(directory, session):
    """Match client IDs, including per-attempt IDs, without reading or changing inbox cursors."""
    raw = _bytes(directory / 'inbox.jsonl')
    if raw and not raw.endswith(b'\n'):
        raise ValueError('The message history is being written or is incomplete')
    states = json.loads(_bytes(directory / 'inbox-state.json', replaced=True) or b'{}', object_pairs_hook=_unique)
    if not isinstance(states, dict):
        raise ValueError('The delivery ledger is not an object')
    found, confirmed, unproven = {}, set(), set()
    for line in raw.splitlines():
        message = json.loads(line, object_pairs_hook=_unique)
        if not isinstance(message, dict) or not _text(message.get('id'), 128):
            raise ValueError('The message history has an invalid identity')
        state = states.get(message['id'], {})
        if not isinstance(state, dict) or not isinstance(state.get('attempts', []), list):
            raise ValueError('The message delivery history is invalid')
        attempts = state.get('attempts', [])
        latest = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
        context_owner = _context_owner(state, attempts, message['id'])
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise ValueError('The message delivery attempt is invalid')
            if not session.native_id or attempt.get('native_session_id') != session.native_id:
                continue
            identifier = attempt.get('native_message_id', message['id'])
            mode = attempt.get('mode', message.get('delivery'))
            if not _text(identifier, 128) or mode not in ('follow-up', 'steer'):
                raise ValueError('The native message identity or mode is invalid')
            if not _scope(attempt, state, session, identifier, message['id'], context_owner):
                unproven.add(identifier)
                continue
            sender = message.get('from')
            sender = sender.get('agent_id') if isinstance(sender, dict) else None
            sender = sender if _text(sender, 128) else None
            metadata = (message['id'], mode, sender)
            if identifier in found and found[identifier] != metadata:
                raise ValueError('The native message identity is ambiguous')
            found[identifier] = metadata
            receipt = attempt.get('delivery_receipt')
            receipt = receipt.get('value') if isinstance(receipt, dict) else None
            initial = attempt.get('value')
            current = state.get('native_receipt') if attempt is latest else None
            if (any(isinstance(value, dict) and value.get('transcript_confirmed') is True
                    for value in (receipt, initial, current))
                    or attempt is latest and mailbox._dated(state.get('transcript_confirmed_at'))):
                confirmed.add(identifier)
    unconfirmed = frozenset(sender for identifier, (_, mode, sender) in found.items()
                            if identifier not in confirmed and mode == 'follow-up' and sender is not None)
    return found, unconfirmed, frozenset(unproven)


def _same_session(directory, session, executor):
    current = agents.runtime(directory)
    properties = records.properties(directory / 'properties.json')
    selected = SessionHandle.from_records(properties, current, directory)
    fields = ('agent_id', 'harness', 'workspace', 'launch_id', 'native_id', 'home', 'endpoint', 'service',
              'pid', 'pid_started', 'bridge')
    original_generation = executor.facts.get('session_generation', 0)
    current_generation = current.get('session_generation', 0)
    return (all(getattr(session, key) == getattr(selected, key) for key in fields)
            and type(original_generation) is int and type(current_generation) is int
            and original_generation >= 0 and original_generation == current_generation
            and executor.facts.get('state') == current.get('state'))


def _read(root, agent, progress):
    directory, adapter, session, executor = native.context(root, agent, reconcile=False)
    ledger, unconfirmed, unproven = _ledger(directory, session)
    if not _same_session(directory, session, executor):
        return _unknown('The launch or conversation changed while reading its delivery history')
    if not session.launch_id or not session.native_id:
        return _unknown('The current launch and conversation are not bound', unconfirmed)
    # Keep this completed, immutable ledger snapshot even if the optional harness read times out.
    progress['unconfirmed'] = unconfirmed
    try:
        return _catalog(adapter, session, executor, ledger, unconfirmed, unproven)
    finally:
        if not _same_session(directory, session, executor):
            progress['unconfirmed'] = None
            raise ValueError('The launch or conversation changed while reading its queue')


def _catalog(adapter, session, executor, ledger, unconfirmed, unproven):
    if not callable(native.adapter_hook(adapter, 'queued_prompts')):
        return _unknown('This harness cannot report its queued mv2 messages', unconfirmed)
    executor.check_session()
    reply = effects.call(adapter, 'queued_prompts', session, executor)
    if not isinstance(reply, NativeResult) or reply.state != 'ok':
        reason = getattr(reply, 'reason', None)
        return _unknown(reason if _text(reason) else 'The harness queue is unavailable', unconfirmed)
    value = reply.value
    if (not isinstance(value, dict) or value.get('coverage') not in ('complete', 'unknown')
            or not isinstance(value.get('entries'), (list, tuple)) or len(value['entries']) > ENTRIES
            or len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > PAYLOAD_LIMIT):
        raise ValueError('Invalid or oversized queue report')
    reason = value.get('reason')
    if reason is not None and not _text(reason):
        raise ValueError('Invalid queue coverage reason')
    if value['coverage'] == 'unknown' and not reason:
        reason = 'The harness cannot enumerate all of its queued mv2 messages'
    observed_at = _time(value.get('observed_at'))
    if observed_at is None:
        raise ValueError('The queue report has no observation time')
    entries, senders, seen = [], set(), set()
    coverage = value['coverage']
    for entry in value['entries']:
        if not isinstance(entry, dict) or not _text(entry.get('message_id'), 128):
            raise ValueError('Invalid queued message identity')
        identifier = entry['message_id']
        if identifier in seen:
            raise ValueError('Repeated queued message identity')
        seen.add(identifier)
        since = _time(entry.get('since'))
        native_id = entry.get('native_id')
        if native_id is not None and not _text(native_id):
            raise ValueError('Invalid native queue identity')
        if identifier not in ledger:
            if identifier in unproven:
                coverage = 'unknown'
                reason = 'A queued message has no verified delivery scope for the current conversation'
            continue  # Typed input and other clients never become mv2 messages by text or position.
        _, mode, sender = ledger[identifier]
        entries.append(QueuedPrompt(message_id=identifier, mode=mode, since=since, native_id=native_id))
        if mode == 'follow-up' and sender is not None:
            senders.add(sender)
    executor.check_session()
    return QueueReport(state='ok', coverage=coverage, reason=reason,
                       observed_at=observed_at, entries=tuple(entries)), frozenset(senders), unconfirmed


def for_send(root, agent):
    """Return report, queued sender IDs and unconfirmed sender IDs (None for unreadable history)."""
    effects.require('queued_prompts', agent)
    deadline = time.monotonic() + BUDGET
    key = (str(root), str(agent))
    with _lock:
        if key in _running:
            previous = _running[key]
            worker = previous.get('worker')
            if worker is None or worker.ident is None or worker.is_alive():
                return _unknown('An earlier queue read is still running', previous.get('unconfirmed'))
            _running.pop(key)
        progress = {}
        _running[key] = progress
    result, ended = {}, threading.Event()
    context = copy_context()

    def read():
        try:
            with access.within('observe', 'queued prompts'):
                result['value'] = _read(universe.resolve(root), agent, progress)
        except Exception:
            result['value'] = _unknown('The queue or its delivery evidence could not be verified',
                                       progress.get('unconfirmed'))
        finally:
            ended.set()

    worker = threading.Thread(target=context.run, args=(read,), name='mv2-queued-prompts', daemon=True)
    progress['worker'] = worker
    try:
        worker.start()
    except RuntimeError:
        with _lock:
            if _running.get(key) is progress:
                _running.pop(key)
        return _unknown('The queue reader could not start')
    if not ended.wait(max(0, deadline - time.monotonic())):
        return _unknown('Reading the harness queue took longer than 2 s', progress.get('unconfirmed'))
    # Thread-local cleanup can run after ended is set; it shares the same deadline.
    worker.join(max(0, deadline - time.monotonic()))
    if worker.is_alive():
        return _unknown('Reading the harness queue took longer than 2 s', progress.get('unconfirmed'))
    with _lock:
        if _running.get(key) is progress:
            _running.pop(key)
    return result['value']


def report(root, agent):
    """Report queued mv2 inputs only; absence never establishes delivery or loss."""
    return for_send(root, agent)[0]
