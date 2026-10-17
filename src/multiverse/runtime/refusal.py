"""Latch adapter-observed native launch refusals without retrying native work."""
import json

from ..store import lock, now, read, write
from ..universe import agents
from ..universe.mailbox import end_startup_messages, inbox, update_message


def record(directory, launch_id, evidence):
    if (not isinstance(evidence, dict) or not isinstance(evidence.get('message'), str)
            or not evidence['message'].strip() or not isinstance(evidence.get('source'), str)
            or len(json.dumps(evidence, ensure_ascii=False).encode()) > 16384):
        raise ValueError('Invalid native launch refusal evidence')
    with lock(directory / 'launches' / launch_id / '.refusal.lock'):
        facts = agents.runtime(directory)
        if facts.get('launch_id') != launch_id:
            raise ValueError('Outdated launch; refusing native failure update')
        properties = read(directory / 'properties.json')
        if evidence.get('native_id') not in (None, properties.get('native_session_id')):
            raise ValueError('Native refusal belongs to another conversation')
        if facts.get('native_refusal'):
            return facts['native_refusal']
        saved = {**evidence, 'at': now(), 'launch_id': launch_id}
        write(directory / 'launches' / launch_id / 'native-refusal.json', saved)
        agents.update_runtime(directory, launch_id, native_refusal=saved, reason=evidence['message'],
                              error=evidence['message'], startup_delivery='failed', delivery_error=evidence['message'])
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
