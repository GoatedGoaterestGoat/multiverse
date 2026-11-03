"""Launch-fenced conversation selection; native request parsing belongs to adapters."""
from pathlib import Path

from .. import harnesses
from ..harnesses.types import NativeResult, SessionHandle, normalize_session_id
from ..store import existing_lock, lock, now, read, uid
from ..universe import agents, mailbox, records, sessions
from . import effects


class Switching(ValueError):
    """Current input cannot select a conversation while navigation is unresolved."""


def pending(facts):
    value = facts.get('navigation')
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('state') not in ('checking', 'admitted', 'committed', 'aborted'):
        raise Switching('Conversation switch record is damaged; repair runtime.json before continuing')
    return value if value['state'] in ('checking', 'admitted') else None


def require_settled(facts):
    if pending(facts):
        raise Switching('Conversation switch is unresolved; input stays saved. Close this terminal, then run mv2 resume to restore the recorded conversation')


def queue_for(facts, native_id):
    current = facts.get('queue_state')
    if isinstance(current, dict) and current.get('native_session_id') == native_id:
        return current
    history = facts.get('queue_history') or {}
    if not isinstance(history, dict):
        raise ValueError('runtime.json: queue_history must be an object')
    state = history.get(native_id)
    return state if isinstance(state, dict) and state.get('native_session_id') == native_id else None


def _messages(directory, native_id, waiting_token=None):
    for message in mailbox.inbox(directory, all_messages=True, limit=None)['messages']:
        if message.get('delivery') == 'inbox-only':
            continue
        tracking = message['tracking']
        if (tracking.get('target_session') or message.get('target_session')) not in (None, native_id):
            continue
        state = tracking.get('submission_state', 'pending')
        if (state == 'pending' and not tracking.get('dismissed_at')
                and tracking.get('navigation_wait') not in (None, waiting_token)):
            raise ValueError('Saved input from the previous switch is still awaiting delivery')
        if state in ('submitting', 'unknown'):
            raise ValueError('A message has uncertain acceptance; check delivery before switching conversations')
        if state == 'accepted' and (tracking.get('native_receipt') or {}).get('transcript_confirmed') is not True:
            raise ValueError('An accepted message may still be queued; check delivery and finish its work before switching conversations')


def _current(root, directory, launch, previous):
    facts = records.runtime(directory / 'runtime.json')
    if (facts.get('launch_id') != launch or facts.get('state') != 'running'
            or facts.get('ending_at') or facts.get('native_refusal') or facts.get('binding_conflict')):
        raise ValueError('The exact running launch is no longer available for a conversation switch')
    properties = agents.session_properties(root, directory, launch_locked=True)
    if properties.get('native_session_id') != previous:
        raise ValueError('Conversation changed before navigation; refusing the stale selection')
    home = sessions.canonical_home(properties.get('native_session_home'))
    if (facts.get('native_relay') or {}).get('state') != 'ready':
        raise ValueError('Conversation switching requires a verified running bridge')
    if not facts.get('startup_checked') or facts.get('startup_delivery') not in (None, 'ready'):
        raise ValueError('Launch input is still binding or being delivered')
    return properties, facts, home


def begin(root, directory, launch, event):
    """Fence first, observe outside locks, then revalidate before native navigation."""
    directory, root = Path(directory), Path(root)
    previous = normalize_session_id(event.get('previous'))
    target = event.get('target')
    if target is not None:
        target = normalize_session_id(target)
    reason = event.get('reason')
    if reason not in ('new', 'clear', 'resume') or (reason == 'resume') != (target is not None):
        raise ValueError('Invalid conversation selection')
    correlation = event.get('correlation')
    if not isinstance(correlation, str) or not correlation or len(correlation) > 256:
        raise ValueError('Conversation selection requires exact request correlation')
    with existing_lock(directory / '.launch.lock'):
        properties, facts, home = _current(root, directory, launch, previous)
        require_settled(facts)
        if facts.get('status') != 'idle':
            raise ValueError('Finish current work before switching conversations; idle state must be confirmed')
        from . import slash
        if slash._pending(directory):
            raise ValueError('A managed command is unsettled; read its result before switching conversations')
        _messages(directory, previous)
        if target is not None:
            effective, rows = sessions.effective(directory, properties)
            sessions.require_unretired(directory, target)
            if target == previous or (target, home) not in sessions.claims(effective, rows):
                raise ValueError('Resume only a different conversation in this agent\'s own unretired lineage')
        marker = {'id': uid(), 'launch_id': launch, 'state': 'checking', 'previous': previous,
                  'target': target, 'reason': reason, 'home': home, 'correlation': correlation, 'at': now()}
        agents.update_runtime(directory, launch, navigation=marker, recovery_snapshot=None)
        session = SessionHandle.from_records(properties, facts, directory)
    try:
        # This probe never loads/resumes a thread. The fence prevents new core
        # submissions while the adapter observes exact native work and queue.
        from . import recovery
        result = recovery.check(properties, facts, directory)
        if result.state != 'ok':
            raise ValueError(result.reason or 'Native idle and empty queue could not be confirmed')
        if target is not None:
            adapter = harnesses.load(root, properties['harness'])
            probe = getattr(adapter, 'navigation_target', None)
            if probe is None:
                raise ValueError('The harness cannot verify the saved conversation for navigation')
            result = effects.call(adapter, 'navigation_target', session, target)
            if not isinstance(result, NativeResult) or result.state != 'ok':
                raise ValueError(result.reason if isinstance(result, NativeResult) else 'Saved conversation cannot be verified')
        with existing_lock(directory / '.launch.lock'):
            properties, current, current_home = _current(root, directory, launch, previous)
            saved = pending(current)
            if not saved or saved.get('id') != marker['id'] or current_home != home:
                raise ValueError('Conversation selection changed while checking native readiness')
            if current.get('status') != 'idle':
                raise ValueError('Native work began during the conversation readiness check')
            _messages(directory, previous, marker['id'])
            agents.update_runtime(directory, launch, navigation={**saved, 'state': 'admitted'})
        return {'token': marker['id'], 'previous': previous, 'target': target}
    except Exception:
        # Nothing was forwarded by the relay yet. This definite pre-send
        # refusal can release only its own marker, never another selection.
        abort(root, directory, launch, {'token': marker['id']})
        flush(root, directory, launch, {'token': marker['id']})
        raise


def commit(root, directory, launch, event):
    """Commit the journal before clearing the fence or publishing native success."""
    directory, root = Path(directory), Path(root)
    target = normalize_session_id(event.get('target'))
    token = event.get('token')
    with existing_lock(directory / '.launch.lock'):
        facts = records.runtime(directory / 'runtime.json')
        marker = facts.get('navigation')
        if facts.get('launch_id') != launch or not isinstance(marker, dict) or marker.get('id') != token:
            raise ValueError('Conversation selection belongs to an outdated launch or request')
        if marker.get('state') == 'committed' and marker.get('committed_target') == target:
            return {'native_id': target, 'generation': facts.get('session_generation', 0),
                    'queue_state': queue_for(facts, target)}
        if marker.get('state') != 'admitted' or facts.get('state') != 'running' or facts.get('ending_at'):
            raise ValueError('Conversation selection is not admitted')
        if marker.get('target') not in (None, target) or target == marker.get('previous'):
            raise ValueError('Native reply differs from the admitted conversation selection')
        home = sessions.canonical_home(event.get('home'))
        if home != marker['home']:
            raise ValueError('Native reply changed conversation storage')
        # Repair may already return target after an append-before-pointer crash.
        properties = agents.session_properties(root, directory, launch_locked=True)
        if properties.get('native_session_id') not in (marker['previous'], target):
            raise ValueError('Conversation changed before navigation committed')
        history = facts.get('queue_history', {})
        if not isinstance(history, dict):
            raise ValueError('runtime.json: queue_history must be an object')
        history = dict(history)
        old_queue = queue_for(facts, marker['previous'])
        if old_queue is not None:
            history[marker['previous']] = old_queue
        restored = queue_for({'queue_history': history}, target) or {'native_session_id': target, 'state': 'unknown'}
        generation = facts.get('session_generation', 0)
        version = facts.get('native_status_version', 0)
        if any(type(value) is not int or value < 0 for value in (generation, version)):
            raise ValueError('runtime.json: conversation and status generations must be nonnegative integers')
        generation += 1
        agents._rebind_session(root, directory, marker['previous'], target, marker['reason'], launch, home=home)
        agents.update_runtime(directory, launch, navigation={**marker, 'state': 'committed', 'committed_target': target},
                              session_generation=generation, queue_history=history, queue_state=restored,
                              usage=None, recovery_snapshot=None, status='unknown', native_status_value='unknown',
                              native_status_version=version + 1)
        return {'native_id': target, 'generation': generation, 'queue_state': restored}


def abort(root, directory, launch, event):
    """Only for a definite pre-forward or correlated native refusal."""
    directory = Path(directory)
    with existing_lock(directory / '.launch.lock'):
        facts = records.runtime(directory / 'runtime.json')
        marker = pending(facts)
        if facts.get('launch_id') != launch or not marker or marker.get('id') != event.get('token'):
            return
        # A journal append is not reversible even if the properties write failed.
        properties = records.properties(directory / 'properties.json')
        effective, _ = sessions.effective(directory, properties)
        if effective.get('native_session_id') != marker.get('previous'):
            raise ValueError('The conversation journal already advanced; selection cannot be aborted')
        agents.update_runtime(directory, launch, navigation={**marker, 'state': 'aborted'})


def defer(directory, message, tracking, facts, *, retry=False, new_message=False):
    """Save only proven unsent input for the matching fence's one flush."""
    marker = pending(facts)
    if marker is None:
        return None
    state = tracking.get('submission_state', 'pending')
    if state not in ('pending', 'failed'):
        return {'state': 'unknown', 'ok': False, 'message_id': message['id'],
                'reason': 'Earlier acceptance is unchanged while the conversation switch is unresolved; check delivery'}
    if state == 'failed' and not retry:
        return {**(tracking.get('last_receipt') or (tracking.get('attempts') or [{}])[-1]),
                'state': 'error', 'ok': False, 'message_id': message['id'], 'reused': True}
    mailbox.update_message(directory, message['id'], navigation_wait=marker['id'],
                           navigation_new=bool(new_message or tracking.get('navigation_new')))
    return {'state': 'pending', 'ok': True, 'message_id': message['id'],
            'reason': 'Saved until the conversation switch has a definite result; not submitted',
            'value': {'navigation_wait': marker['id']}}


def flush(root, directory, launch, event):
    """Once resolved, retry only input explicitly deferred by this fence."""
    directory = Path(directory)
    from . import native
    with lock(directory / '.send.lock', create_parent=False):
        with existing_lock(directory / '.launch.lock'):
            facts = records.runtime(directory / 'runtime.json')
            marker = facts.get('navigation') or {}
            if (facts.get('launch_id') != launch or marker.get('id') != event.get('token')
                    or marker.get('state') not in ('committed', 'aborted') or facts.get('ending_at')):
                return
            batch = [m for m in mailbox.inbox(directory, all_messages=True, limit=None)['messages']
                     if m['tracking'].get('navigation_wait') == event['token']
                     and m['tracking'].get('submission_state') in ('pending', 'failed')]
        for message in batch:
            native.deliver(root, directory.name, message['id'],
                           retry=message['tracking']['submission_state'] == 'failed',
                           new_message=message['tracking'].get('navigation_new') is True)


def end_waiting(directory, reason):
    """Fail unsent switch-held input before explicit resume replaces a stopped launch.

    The caller holds the launch lock and has proved the old attachment ended. A
    resolved marker can also have lost its flush, or a prior resume its marker:
    the message's own deferral is the evidence, not the current runtime marker.
    Keep original targets and receipts; explicit retry applies the usual lineage
    checks and never acquires a fresh-message queue restart intent.
    """
    count = 0
    for message in mailbox.inbox(directory, all_messages=True, limit=None)['messages']:
        tracking = message['tracking']
        token = tracking.get('navigation_wait')
        if (message.get('schema') != 2 or message.get('delivery') not in ('steer', 'follow-up')
                or not isinstance(token, str) or not token
                or tracking.get('submission_state') != 'pending'
                or tracking.get('dismissed_at') or tracking.get('uncheckable_reported_at')):
            continue
        receipt = {'state': 'unavailable', 'ok': False, 'reason': reason, 'at': now()}
        mailbox.update_message(directory, message['id'], submission_state='failed',
                               attempts=[*tracking.get('attempts', []), receipt], last_receipt=receipt)
        count += 1
    return count


def handler(root, directory, launch):
    def dispatch(action, event):
        if action not in ('begin', 'commit', 'abort', 'flush'):
            raise ValueError('Unknown conversation switch action')
        return globals()[action](root, directory, launch, event)
    return dispatch
