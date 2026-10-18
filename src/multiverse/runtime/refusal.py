"""Latch adapter-observed native launch refusals without retrying native work."""
import json
import time
from datetime import datetime, timezone

from ..store import lock, now, read, write
from ..universe import agents
from ..universe.mailbox import end_startup_messages, inbox, update_message

# Leave five seconds before the default readiness wait.
STARTUP_WINDOW_SECONDS = 55


def startup_deadline(facts):
    """Freeze launch age into a monotonic deadline once per observer/wait."""
    remaining = STARTUP_WINDOW_SECONDS
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(facts['started_at'])).total_seconds()
        remaining -= min(remaining, max(0, age))
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return time.monotonic() + remaining


def settle_startup(directory, launch_id, state, *, deadline=None):
    def changes(facts):
        observed = (facts.get('native_startup_observation') or {}).get('state')
        if (facts.get('state') not in ('starting', 'running') or facts.get('ending_at')
                or facts.get('native_refusal') or observed not in (None, 'pending')
                or (facts.get('attachment') or {}).get('readiness') != 'process'
                or (deadline is not None and time.monotonic() >= deadline)):
            return None
        return {'native_startup_observation': {'state': state, 'at': now()}}
    return agents.mutate_runtime(directory, launch_id, changes) is not None


def record(directory, launch_id, evidence, *, observation_deadline=None):
    if (not isinstance(evidence, dict) or not isinstance(evidence.get('message'), str)
            or not evidence['message'].strip() or not isinstance(evidence.get('source'), str)
            or len(json.dumps(evidence, ensure_ascii=False).encode()) > 16384):
        raise ValueError('Invalid native launch refusal evidence')
    if agents.runtime(directory).get('launch_id') != launch_id:
        raise ValueError('Outdated launch; refusing native failure update')
    with lock(directory / 'launches' / launch_id / '.refusal.lock', create_parent=False):
        facts = agents.runtime(directory)
        if facts.get('launch_id') != launch_id:
            raise ValueError('Outdated launch; refusing native failure update')
        properties = read(directory / 'properties.json')
        if evidence.get('native_id') not in (None, properties.get('native_session_id')):
            raise ValueError('Native refusal belongs to another conversation')
        if facts.get('native_refusal'):
            return facts['native_refusal']
        saved = {**evidence, 'at': now(), 'launch_id': launch_id}
        changes = dict(native_refusal=saved, reason=evidence['message'], error=evidence['message'],
                       startup_delivery='failed', delivery_error=evidence['message'])
        if observation_deadline is not None:
            def screen_changes(current):
                observed = (current.get('native_startup_observation') or {}).get('state')
                if (time.monotonic() >= observation_deadline or observed not in (None, 'pending')
                        or current.get('ending_at') or current.get('state') not in ('starting', 'running')):
                    return None
                return changes
            if agents.mutate_runtime(directory, launch_id, screen_changes) is None:
                return None  # Expiry and screen refusal serialize on the runtime lock.
        write(directory / 'launches' / launch_id / 'native-refusal.json', saved)
        if observation_deadline is None:
            agents.update_runtime(directory, launch_id, **changes)
    # Only definitely unsubmitted core input fails. A CLI starting prompt could
    # already have reached native code, so it stays uncertain and never replays.
    end_startup_messages(directory, launch_id, evidence['message'])
    for message in inbox(directory, all_messages=True, limit=None)['messages']:
        tracking = message.get('tracking') or {}
        if (tracking.get('delivery_method') == 'resume_prompt' and tracking.get('wake_launch_id') == launch_id
                and tracking.get('submission_state') in ('submitting', 'unknown', 'accepted')):
            attempts = tracking.get('attempts') or [{}]
            last = {**attempts[-1], 'state': 'unknown', 'ok': False, 'reason': evidence['message']}
            update_message(directory, message['id'], submission_state='unknown', attempts=[*attempts[:-1], last])
    return saved
