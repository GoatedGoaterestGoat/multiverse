"""Persist native pause evidence and grant one restart to new submitted input.

Native code drains its own queue. There is deliberately no idle-time worker:
only a new message's delivery may explicitly restart the FIFO head.
"""
from dataclasses import replace

from . import effects
from ..store import lock, now, read, write
from ..universe import agents


def _saved_state(facts):
    state = facts.get('queue_state')
    if state is None:
        return {}
    if (not isinstance(state, dict)
            or state.get('restart') is not None and not isinstance(state['restart'], dict)):
        raise ValueError('runtime.json: queue_state and its restart claim must be objects; repair the record')
    return state


def record_primary_event(directory, launch_id, event):
    """Save queue authority before forwarding the owning terminal's event."""
    if (not isinstance(event, dict) or event.get('kind') not in ('interrupt', 'started', 'completed')
            or any(not isinstance(event.get(key), str) or not event[key] or len(event[key]) > 4096
                   or '\0' in event[key] for key in ('native_session_id', 'turn_id'))
            or event['kind'] == 'completed' and event.get('status') not in ('interrupted', 'finished')):
        raise ValueError('Invalid primary queue event')
    properties = read(directory / 'properties.json')
    if properties.get('native_session_id') != event['native_session_id']:
        raise ValueError('Primary queue event belongs to a different conversation')

    # Queue readers need the durable state immediately. Ordered plugin events
    # use their own admission and recovery paths and must not hold up this write.
    from .navigation import pending
    with lock(directory / '.runtime.lock', create_parent=False):
        facts = agents.runtime(directory)
        if facts.get('launch_id') != launch_id:
            raise ValueError('Outdated launch; refusing to update current runtime')
        if pending(facts) or read(directory / 'properties.json').get('native_session_id') != event['native_session_id']:
            return None
        if facts.get('ending_at') or facts.get('state') not in ('starting', 'running'):
            return None
        previous = _saved_state(facts)
        if previous.get('native_session_id') != event['native_session_id']:
            previous = {}
        if ((event['kind'] == 'completed' or event['kind'] == 'interrupt' and previous.get('state') == 'running')
                and previous.get('turn_id')
                and previous['turn_id'] != event['turn_id']):
            return None  # A newer turn wins over late completion/interrupt traffic.
        state = {'interrupt': 'interrupting', 'started': 'running', 'completed':
                 'paused' if event.get('status') == 'interrupted' else 'idle'}[event['kind']]
        facts['queue_state'] = {**previous, 'native_session_id': event['native_session_id'],
                                'turn_id': event['turn_id'], 'state': state}
        facts['observed_at'] = now()
        write(directory / 'runtime.json', facts)
        return facts


def claim_restart(directory, session, message_id):
    """Called under delivery locks; the returned handle belongs to one send only."""
    effects.require('continue_queued', session.agent_id)
    if not session.native_id or not isinstance(message_id, str) or not message_id:
        return session
    if read(directory / 'properties.json').get('native_session_id') != session.native_id:
        raise ValueError('Conversation changed before queue restart could be claimed')
    def changes(facts):
        if facts.get('state') != 'running' or facts.get('ending_at') or facts.get('native_refusal'):
            return None
        previous = _saved_state(facts)
        if previous.get('native_session_id') != session.native_id:
            previous = {'native_session_id': session.native_id, 'state': 'idle'}
        if (previous.get('restart') or {}).get('id') == message_id:
            return None
        return {'queue_state': {**previous, 'restart': {'id': message_id, 'state': 'claimed'}}}
    changed = agents.mutate_runtime(directory, session.launch_id, changes)
    return replace(session, queue_state=changed['queue_state']) if changed else session
