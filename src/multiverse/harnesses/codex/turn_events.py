"""Metadata-only consumption observations from the owning Codex connection."""
from collections import OrderedDict
import hashlib

from ..types import TurnEnded, TurnJoined, TurnStarted
from .receipt_events import identifier


MAX_PENDING = 256
MAX_TASK_BYTES = 64 << 10
END_STATES = {'completed': 'finished', 'interrupted': 'interrupted', 'failed': 'error'}


def task_digest(value):
    """Compare only a bounded startup argument, without forwarding its contents."""
    if not isinstance(value, str) or not value or len(value) > MAX_TASK_BYTES:
        return None
    try:
        raw = value.encode('utf-8')
    except UnicodeError:
        return None
    return hashlib.sha256(raw).hexdigest() if len(raw) <= MAX_TASK_BYTES else None


def end_ref(at, duration):
    """The app-server and the rollout report the same native completion timing."""
    if type(at) is not int or not 0 <= at < 2**63:
        return None
    if type(duration) is int and 0 <= duration < 2**63:
        return f'end_{at}_{duration}'
    return None


def request_key(value):
    if type(value) is int and -(2**63) <= value < 2**63 or identifier(value):
        return type(value), value
    return None


def remember(records, key, value):
    records[key] = value
    records.move_to_end(key)
    if len(records) > MAX_PENDING:
        records.popitem(last=False)


class Observations:
    """Keep an ordered buffer of opaque IDs for one owning native conversation."""
    def __init__(self, startup_task=None):
        self.startup_task = startup_task
        self.native_id = None
        self.active = None
        self.started = OrderedDict()
        self.requests = OrderedDict()
        self.items = OrderedDict()
        self.ends = OrderedDict()
        self.end_refs = OrderedDict()
        self.ready = []

    def synchronize(self, native_id):
        if self.native_id == native_id:
            return
        if self.native_id is not None:
            self.startup_task = None
        self.native_id, self.active = native_id, None
        self.started.clear()
        self.requests.clear()
        self.items.clear()
        self.ends.clear()
        self.end_refs.clear()
        self.ready.clear()

    def outgoing(self, value):
        method = value.get('method')
        params = value.get('params')
        if (method not in ('turn/start', 'turn/steer') or not isinstance(params, dict)
                or params.get('threadId') != self.native_id):
            return
        task, self.startup_task = self.startup_task, None
        client = params.get('clientUserMessageId')
        key = request_key(value.get('id'))
        expected = params.get('expectedTurnId') if method == 'turn/steer' else None
        if (key is None or not identifier(client) or method == 'turn/steer' and not identifier(expected)
                or params.get('turnTrigger') not in (None, 'user')):
            return
        first = False
        if task is not None and method == 'turn/start':
            blocks = params.get('input')
            if (isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict)
                    and blocks[0].get('type') == 'text'):
                first = task_digest(blocks[0].get('text')) == task
        remember(self.requests, key, (client, expected, first))

    def incoming(self, value):
        if 'id' in value:
            if 'method' not in value and 'error' in value:
                self.requests.pop(request_key(value['id']), None)
            return
        params = value.get('params')
        if not isinstance(params, dict) or params.get('threadId') != self.native_id:
            return
        method = value.get('method')
        if method in ('turn/started', 'turn/completed'):
            self.lifecycle(method, params.get('turn'))
        elif method in ('item/started', 'item/completed'):
            item = params.get('item')
            if not isinstance(item, dict):
                return
            if (isinstance(self.active, str) and params.get('turnId') == self.active
                    and item.get('type') in ('agentMessage', 'reasoning')):
                # Model activity proves that the turn was already underway.
                self.started[self.active] = True
            if method == 'item/completed' and item.get('type') == 'userMessage':
                self.input(params.get('turnId'), item)

    def append(self, transition):
        if len(self.ready) < MAX_PENDING:
            self.ready.append(transition)

    def lifecycle(self, method, turn):
        if not isinstance(turn, dict) or not identifier(turn.get('id')):
            return
        identity = turn['id']
        if method == 'turn/started':
            self.active = identity
            remember(self.started, identity, self.started.get(identity, False))
            self.ends.pop(identity, None)  # Recovery can continue the same native turn.
            return
        state = END_STATES.get(turn.get('status')) if isinstance(turn.get('status'), str) else None
        if state is None:
            return
        ref = end_ref(turn.get('completedAt'), turn.get('durationMs'))
        occurrence = (identity, ref, state)
        if self.ends.get(identity) == (ref, state) or ref is not None and occurrence in self.end_refs:
            return
        remember(self.ends, identity, (ref, state))
        if ref is not None:
            remember(self.end_refs, occurrence, None)
        if self.active == identity:
            self.active = None
        self.append(TurnEnded(turn=identity, ref=ref, kind='prompt', state=state))

    def input(self, turn, item):
        if not identifier(turn):
            return
        identity, client = item.get('id'), item.get('clientId')
        if not identifier(identity) or (turn, identity) in self.items or turn in self.ends:
            return
        attempt = client if identifier(client) else None
        matches = [(key, expected, first) for key, (saved, expected, first) in self.requests.items()
                   if attempt is not None and saved == attempt and expected in (None, turn)]
        source = 'person' if len(matches) == 1 else 'harness'
        known_steer = len(matches) == 1 and matches[0][1] == turn
        started = turn == self.active and self.started.get(turn) is False and not known_steer
        joined = known_steer or turn == self.active and self.started.get(turn) is True
        if not started and not joined:
            return  # There is no evidence that this input starts or joins the turn.
        remember(self.items, (turn, identity), None)
        for key, _, _ in matches:
            self.requests.pop(key, None)
        if turn == self.active:
            self.started[turn] = True
        if started:
            self.append(TurnStarted(turn=turn, kind='prompt', ref=identity, source=source, attempt=attempt,
                                    first_task=len(matches) == 1 and matches[0][2]))
        else:
            self.append(TurnJoined(turn=turn, ref=identity, source=source, attempt=attempt))

    def take(self):
        edges, self.ready = self.ready, []
        return (self.native_id, tuple(edges)) if edges else None
