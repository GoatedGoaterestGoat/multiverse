"""Persist native user-item positives without submitting or reading native history."""
from pathlib import Path

from ..harnesses.types import SessionHandle
from ..store import lock
from ..universe import agents, mailbox, records
from . import native, navigation, queued_prompts


FIELDS = frozenset(('kind', 'native_session_id', 'turn_id', 'item_id', 'client_message_id'))
DIAGNOSTICS = {
    'capacity': 'Delivery observation capacity exceeded; bounded receipt lookup remains available',
    'unavailable': 'Delivery observation unavailable; bounded receipt lookup remains available',
    'save_failed': 'Delivery observation could not be saved; bounded receipt lookup remains available',
}


def metadata(value):
    """Only a fixed marker and four bounded IDs may enter the background queue."""
    if not isinstance(value, dict) or set(value) != FIELDS or value.get('kind') != 'user_message':
        return None
    for key in FIELDS - {'kind'}:
        item = value[key]
        if not isinstance(item, str) or not item or '\0' in item:
            return None
        try:
            if len(item.encode('utf-8')) > 4096:
                return None
        except UnicodeError:
            return None
    return dict(value)


def _scoped(attempt, state, session, wire, logical, owner):
    if (attempt.get('message_id') not in (None, logical)
            or attempt.get('agent_id') not in (None, session.agent_id)
            or attempt.get('agent_id') is None and attempt.get('launch_id') != session.launch_id):
        return False
    if not queued_prompts._scope(attempt, state, session, wire, logical, owner):
        return False
    saved = attempt.get('delivery_context')
    if saved is None and attempt is owner:
        saved = state.get('delivery_context')
    if saved is None:
        return attempt.get('launch_id') == session.launch_id  # Unambiguous legacy current-launch ID.
    return (isinstance(saved, dict) and saved.get('launch_id') == attempt.get('launch_id')
            and saved.get('native_id') == session.native_id
            and saved.get('native_message_id', logical) == wire
            and all(isinstance(saved.get(key), str) and Path(saved[key]).is_absolute()
                    and saved[key] == getattr(session, key) for key in ('home', 'workspace')))


class DeliveryPublisher:
    """False means a busy publication lock; the bounded worker can try again."""
    def __init__(self, directory, launch_id, harness='codex', *, agent_id):
        self.directory, self.launch_id, self.harness = Path(directory), launch_id, harness
        self.agent_id = agent_id

    def diagnostic(self, code):
        reason = DIAGNOSTICS.get(code)
        if reason is None:
            return True
        try:
            agents.mutate_runtime(self.directory, self.launch_id,
                lambda facts: {'error': reason} if not facts.get('ending_at')
                    and facts.get('state') in ('starting', 'running') else None, blocking=False)
        except BlockingIOError:
            return False
        except (OSError, ValueError):
            pass
        return True

    def __call__(self, event):
        event = metadata(event)
        if event is None or not isinstance(self.agent_id, str) or not self.agent_id:
            return True
        directory = self.directory
        try:
            # Never wait for a send whose RPC may still be passing through the relay.
            with lock(directory / '.launch.lock', create_parent=False, blocking=False), \
                    lock(directory / '.delivery.lock', create_parent=False, blocking=False):
                properties = records.properties(directory / 'properties.json')
                facts = agents.runtime(directory)
                if (properties.get('id') != self.agent_id
                        or facts.get('launch_id') != self.launch_id or facts.get('ending_at')
                        or facts.get('state') not in ('starting', 'running') or navigation.pending(facts)
                        or properties.get('native_session_id') != event['native_session_id']):
                    return True
                session = SessionHandle.from_records(properties, facts, directory)
                if (session.harness != self.harness or not isinstance(session.native_mode, dict)
                        or session.native_mode.get('mode') != 'bridge'
                        or (session.native_relay or {}).get('state') != 'ready'):
                    return True
                wire = event['client_message_id']
                # Canonical snapshots include retired-field normalization and
                # legacy receipt merging, exactly as the final inbox-locked CAS.
                matched = []
                for logical, state in mailbox.delivery_observation_snapshot(directory).items():
                    attempts = state.get('attempts', [])
                    owner = queued_prompts._context_owner(state, attempts, logical)
                    for index, attempt in enumerate(attempts):
                        if (attempt.get('native_session_id') == session.native_id
                                and attempt.get('native_message_id', logical) == wire
                                and _scoped(attempt, state, session, wire, logical, owner)):
                            matched.append((logical, state, index))
                if len(matched) != 1:
                    return True  # An old logical ID shared by retries cannot identify one attempt.
                logical, state, index = matched[0]
                return mailbox.confirm_delivery_attempt(directory, logical, state, index, event,
                                                       current_index=native._receipt_attempt(state)[0])
        except BlockingIOError:
            return False
