"""Validate and order metadata-only native turn edges for the event outbox."""

from dataclasses import asdict, dataclass
import json
import re
import time

from ..harnesses.types import TurnEnded, TurnJoined, TurnStarted
from ..universe import mailbox, records


_IDENTITY = re.compile(r'[A-Za-z0-9_.:-]{1,256}')
_END_STATES = ('finished', 'interrupted', 'error', 'unknown')
MAX_BATCH = 256
MAX_RELAY_BATCHES = 128
_RECENT_LIMIT = 512
_TYPES = {'started': TurnStarted, 'joined': TurnJoined, 'ended': TurnEnded}
_NAMES = {kind: name for name, kind in _TYPES.items()}


@dataclass(frozen=True)
class Attribution:
    message: str | None = None
    wake: bool = False


def _identity(value):
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


def valid(edge):
    if (not isinstance(edge, (TurnStarted, TurnJoined, TurnEnded)) or not _identity(edge.turn)
            or edge.ref is not None and not _identity(edge.ref)):
        return False
    if isinstance(edge, (TurnStarted, TurnEnded)) and edge.kind not in ('prompt', 'command'):
        return False
    if isinstance(edge, TurnEnded):
        return edge.state in _END_STATES
    return (edge.source in ('person', 'harness') and (edge.attempt is None or _identity(edge.attempt))
            and (not isinstance(edge, TurnStarted) or type(edge.first_task) is bool))


def encode(edge):
    return {'type': _NAMES[type(edge)], **asdict(edge)}


def decode(row):
    if not isinstance(row, dict):
        raise ValueError('Invalid turn-event metadata')
    fields = dict(row)
    try:
        edge = _TYPES[fields.pop('type')](**fields)
    except (KeyError, TypeError):
        raise ValueError('Invalid turn-event metadata') from None
    if not valid(edge):
        raise ValueError('Invalid turn-event metadata')
    return edge


def parse(value):
    """Decode only the in-session event fields, with a fixed error for rejected metadata."""
    try:
        if not isinstance(value, str) or len(value.encode()) > 64 * 1024:
            raise ValueError
        rows = json.loads(value)
        if not isinstance(rows, list) or len(rows) > MAX_BATCH:
            raise ValueError
        return tuple(map(decode, rows))
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError('Invalid turn-event metadata') from None


def public(data):
    """Expose a cursor only when all its public references were proved."""
    if all(isinstance(data.get(key), str) and data[key] for key in ('turn', 'ref', 'generation')):
        return {**data, 'resolved': True}
    return {**data, 'turn': None, 'ref': None, 'generation': None, 'resolved': False}


def _private_attempt(directory, session, generation, wire):
    """Recognize an owned command or retained answer without returning its content."""
    from ..harnesses.types import normalize_session_id
    from .prompts import _record

    try:
        if wire.startswith('prompt-'):
            result = _record(directory, wire.removeprefix('prompt-'), missing_ok=True)
            if (result is None or result.get('input_id') != wire or result.get('input_reserved') is not True
                    or result.get('session_generation') != generation):
                return False
        else:
            if normalize_session_id(wire) != wire:
                return False
            path = directory / 'slash' / (wire + '.json')
            if not path.is_file():
                return False
            result = records._object(path, missing_ok=True)
            if (result is None or result.get('schema') != 1 or result.get('id') != wire
                    or result.get('agent') != directory.name or result.get('result', {}).get('id') != wire):
                return False
        saved = result['session']
        return all(saved.get(key) == getattr(session, key)
                   for key in ('agent_id', 'launch_id', 'native_id', 'workspace', 'home'))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def _prompt_start(edge):
    return isinstance(edge, TurnStarted) and edge.kind == 'prompt'


def _wake_request(directory, session, generation):
    from . import launch_files

    if generation != 0 or not session.native_id:
        return None
    try:
        request = launch_files.read_request(directory, session.launch_id)
        message = request.get('resume_message_id')
        env = request['env']
        if (isinstance(message, str) and message and request.get('task_launch') is not True
                and request.get('expected_native_id') == session.native_id
                and env.get('MV2_LAUNCH_ID') == session.launch_id
                and env.get('MV2_AGENT_ID') == session.agent_id):
            return message
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        pass
    return None


def attribution(directory, session, generation, edges, *, deadline):
    """Join an adapter-observed attempt to exactly one recorded message or private input."""
    from . import delivery_observations, queued_prompts

    wires = {edge.attempt for edge in edges if isinstance(edge, (TurnStarted, TurnJoined)) and edge.attempt}
    starts = [edge for edge in edges if _prompt_start(edge)]
    if not wires and not any(edge.first_task for edge in starts):
        return {}
    by_wire = {}
    while True:
        try:
            snapshot = (mailbox.delivery_observation_snapshot(directory)
                        if (directory / 'inbox.jsonl').is_file() else {})
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {}
            time.sleep(min(.01, remaining))
            if time.monotonic() >= deadline:
                return {}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return {}
        else:
            break
    wake = None
    if any(edge.first_task or edge.attempt in snapshot for edge in starts):
        wake = _wake_request(directory, session, generation)
    if wake is not None:
        # The launch request identifies the CLI input. The delivery history can
        # still prove its attempt after a later retry changes the current receipt.
        attempts = [attempt for attempt in snapshot.get(wake, {}).get('attempts', [])
                    if attempt.get('launch_id') == session.launch_id]
        if (len(attempts) != 1 or attempts[0].get('mode') not in (None, 'resume_prompt')
                or attempts[0].get('native_message_id') is not None
                or attempts[0].get('state') not in ('ok', 'unknown')):
            wake = None
    for logical, state in snapshot.items():
        attempts = state.get('attempts', [])
        owner = queued_prompts._context_owner(state, attempts, logical)
        for attempt in attempts:
            wire = attempt.get('native_message_id', logical)
            if (isinstance(wire, str) and wire in wires and attempt.get('native_session_id') == session.native_id
                    and attempt.get('mode') in ('follow-up', 'steer')
                    and delivery_observations._scoped(attempt, state, session, wire, logical, owner)):
                by_wire.setdefault(wire, []).append((logical, attempt))

    private_wires = {wire for wire in wires if _private_attempt(directory, session, generation, wire)}
    resolved = {}
    for index, edge in enumerate(edges):
        if not isinstance(edge, (TurnStarted, TurnJoined)):
            continue
        wire = edge.attempt
        matches = by_wire.get(wire, ())
        if wire in private_wires and not matches:
            resolved[index] = Attribution()
        elif len(matches) == 1 and wire not in private_wires:
            logical, attempt = matches[0]
            receipt = attempt.get('delivery_receipt')
            saved = receipt.get('value') if isinstance(receipt, dict) else None
            if (isinstance(saved, dict) and saved.get('evidence') == 'native-user-item'
                    and (saved.get('turn_id'), saved.get('item_id'), saved.get('client_message_id')) !=
                    (edge.turn, edge.ref, wire)):
                continue
            resolved[index] = Attribution(logical)
        elif wake is not None and not matches and _prompt_start(edge) and (edge.first_task or wire == wake):
            resolved[index] = Attribution(wake, wake=True)
    return resolved


def close_active(turns):
    """Close the witnessed turn before losing its scope or starting another."""
    active = turns.pop('active', None)
    if active is not None:
        _remember(turns, active['identity'], [None, 'unknown'])
        return {'turn': active['turn'], 'kind': active['kind'], 'ref': None,
                'generation': None, 'state': 'unknown'}
    return None


def _remember(turns, identity, end):
    recent = turns.setdefault('recent', {})
    ends = recent.pop(identity, [])
    ends.append(end)
    recent[identity] = ends[-16:]
    if len(recent) > _RECENT_LIMIT:
        del recent[next(iter(recent))]


def reduce(turns, edge, reference, *, message=None, mv2=False, task_launch=False, wake=False):
    """Return ordered events using only prechecked public refs and exact delivery attribution."""
    active = turns.get('active')
    recent = turns.get('recent', {})
    same = active is not None and active['identity'] == edge.turn
    data = {'turn': reference.turn, 'ref': reference.ref, 'generation': reference.generation}
    if isinstance(edge, TurnEnded):
        end = [edge.ref, edge.state]
        if end in recent.get(edge.turn, ()):
            return ()
        if same:
            turns.pop('active')
        _remember(turns, edge.turn, end)
        return (('turn.ended', dict(data, kind=reference.kind or edge.kind, state=edge.state)),)

    if wake and turns.get('task_seen', False):
        mv2 = False
    data.update(source='mv2' if mv2 else edge.source, message=message if mv2 else None)
    if isinstance(edge, TurnJoined):
        if edge.ref is not None:
            joined = turns.setdefault('joined', [])
            pair = [edge.turn, edge.ref]
            if pair in joined:
                return ()
            joined.append(pair)
            del joined[:-_RECENT_LIMIT]
        return (('turn.joined', data),)

    if same or edge.turn in recent:
        return ()
    preceding = close_active(turns)
    kind = reference.kind or edge.kind
    turns['active'] = {'identity': edge.turn, 'turn': reference.turn, 'kind': kind}
    first_task = task_launch and edge.first_task and not turns.get('task_seen', False)
    if first_task or (wake and mv2):
        turns['task_seen'] = True
    if first_task:
        data.update(source='mv2', message=None)
    started = ('turn.started', dict(data, kind=kind, first_task=first_task))
    return (started,) if preceding is None else (('turn.ended', preceding), started)
