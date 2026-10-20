"""Normalize primary turn events; side traffic never changes native queue pause."""


class PrimaryEvents:
    def __init__(self):
        self.turn_id = None
        self.active = False

    def observe(self, direction, value, native_id):
        if not isinstance(value, dict) or not isinstance(value.get('params'), dict):
            return None
        params = value['params']
        if params.get('threadId') != native_id:
            return None
        method = value.get('method')
        if direction == 'outgoing':
            if method != 'turn/interrupt' or type(value.get('id')) not in (str, int):
                return None
            turn_id = params.get('turnId')
            kind, status = 'interrupt', None
        elif direction == 'incoming' and 'id' not in value and method in ('turn/started', 'turn/completed'):
            turn = params.get('turn')
            if not isinstance(turn, dict):
                return None
            turn_id = turn.get('id')
            kind, status = ('started', None) if method == 'turn/started' else ('completed', turn.get('status'))
            if kind == 'completed' and status not in ('completed', 'failed', 'interrupted'):
                return None
        else:
            return None
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 4096 or '\0' in turn_id:
            return None
        if kind == 'completed':
            if self.turn_id is not None and turn_id != self.turn_id:
                return None  # A late old completion cannot pause the newer turn.
            self.active = False
        elif kind == 'interrupt':
            if self.active and self.turn_id != turn_id:
                return None
        else:
            self.active = True
        self.turn_id = turn_id
        event = {'kind': kind, 'native_session_id': native_id, 'turn_id': turn_id}
        if kind == 'completed':
            # The public API flattens all abort reasons. This label never stops
            # Codex from draining its own queue; the next native start clears it.
            event['status'] = 'interrupted' if status == 'interrupted' else 'finished'
        return event


def restart_claimed(session, message_id=None):
    """Only the one core invocation holding a claimed new-message intent may start."""
    state = session.queue_state
    if not isinstance(state, dict) or state.get('native_session_id') != session.native_id:
        return False
    restart = state.get('restart')
    return (isinstance(restart, dict) and restart.get('state') == 'claimed'
            and isinstance(restart.get('id'), str) and bool(restart['id'])
            and (message_id is None or restart['id'] == message_id))
