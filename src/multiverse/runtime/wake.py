"""Read-only rechecks of a failed launch's exact interactive starting prompt."""
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import UUID

from .. import harnesses
from ..harnesses.types import SessionHandle
from ..store import now, read
from ..universe import agents, records
from ..universe.mailbox import update_message


def prompt_context(adapter, session, prompt, since, environment):
    """Retain identity and storage, never credentials or a live control endpoint."""
    fields = ('agent_id', 'harness', 'workspace', 'launch_id', 'native_id', 'home')
    keys = getattr(adapter, 'storage_env', ())
    keys = keys if isinstance(keys, (tuple, list)) else ()
    return {'session': {key: value for key, value in asdict(session).items() if key in fields},
            'prompt': prompt, 'since': since,
            'environment': {'HOME': environment.get('HOME') or str(Path.home()),
                            **{key: environment.get(key, '') for key in keys}}}


def _legacy_context(root, directory, message, tracking, launch_id):
    """Old receipts can be qualified only while their original exit facts remain."""
    facts = agents.runtime(directory)
    if (facts.get('launch_id') != launch_id or type(facts.get('exit_code')) is not int
            or facts.get('state') not in ('failed', 'stopped') or facts.get('startup_checked') is True):
        raise ValueError('The original startup exit cannot be established; restore its launch records before checking again')
    properties = records.properties(directory / 'properties.json')
    target = tracking.get('target_session') or message.get('target_session')
    if not target or properties.get('native_session_id') != target or not properties.get('native_session_home'):
        raise ValueError('The original conversation or storage is missing; restore its launch records before checking again')
    launch = directory / 'launches' / launch_id
    request = read(launch / 'request.json')
    environment = request['env']
    if (request.get('resume_message_id') != message['id'] or environment.get('MV2_AGENT_ID') != directory.name
            or environment.get('MV2_LAUNCH_ID') != launch_id):
        raise ValueError(f'{launch / "request.json"} does not identify this wake; restore the launch record')
    adapter = harnesses.load(root, properties['harness'])
    session = SessionHandle.from_records(properties, facts, directory)
    # The saved home is authoritative. Never fill old evidence from the checker's environment.
    context = prompt_context(adapter, session, (launch / 'task.md').read_text(), facts.get('started_at'), environment)
    if 'HOME' not in environment:
        context['environment'].pop('HOME', None)
    return context


def check(root, directory, message, tracking):
    """Caller holds launch/delivery locks. This path never reconciles identity or sends."""
    from .native import adapter_hook
    message_id = message['id']
    previous = (tracking.get('attempts') or [{}])[-1]
    if tracking.get('submission_state') == 'accepted':
        value = tracking.get('native_receipt') or {}
        if value.get('transcript_confirmed') is True:
            return {**previous, 'ok': True, 'value': value, 'reused': True}
        return {'state': 'unknown', 'ok': False,
                'reason': 'Interactive resume prompt has no correlatable native receipt; no resend',
                'value': {'transcript_confirmed': None}}
    snapshot = tracking.get('wake_prompt_context')
    evidence, problem = None, None
    try:
        launch_id = tracking.get('wake_launch_id')
        if not isinstance(launch_id, str) or str(UUID(launch_id)) != launch_id:
            raise ValueError('The original launch ID is missing or damaged; repair the delivery record')
        if snapshot is None:
            snapshot = _legacy_context(root, directory, message, tracking, launch_id)
        session = SessionHandle(**snapshot['session'])
        target = tracking.get('target_session') or message.get('target_session')
        if (session.agent_id != directory.name or session.launch_id != launch_id
                or not target or session.native_id != target):
            raise ValueError('The saved startup evidence identifies a different launch or conversation; repair the delivery record')
        prompt, since, environment = snapshot['prompt'], snapshot['since'], snapshot['environment']
        if (not isinstance(prompt, str) or not prompt.strip() or not isinstance(since, str)
                or datetime.fromisoformat(since).tzinfo is None or not isinstance(environment, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items())):
            raise ValueError('The saved startup prompt, time or storage is damaged; repair the delivery record')
        adapter = harnesses.load(root, session.harness)
        hook = adapter_hook(adapter, 'startup_prompt_evidence')
        if hook is None:
            problem = 'This harness cannot check a starting prompt; inspect its original conversation before sending again'
        else:
            evidence = hook(session, prompt, since, dict(environment))
            if evidence is None:
                reason_hook = adapter_hook(adapter, 'startup_prompt_evidence_reason')
                if reason_hook is not None:
                    try:
                        reason = reason_hook(session, prompt, since, dict(environment))
                        problem = reason if isinstance(reason, str) and reason.strip() else None
                    except Exception:
                        pass  # An optional diagnostic must never alter the evidence.
    except Exception as error:
        evidence, problem = None, f'Cannot read the original startup evidence: {error}'
    confirmed, failed = evidence is True, evidence is False
    value = {'transcript_confirmed': True if confirmed else False if failed else None}
    if failed:
        value['definite_failure'] = True
    elif not confirmed:
        value['evidence_problem'] = problem or 'The original starting prompt is still unconfirmed; restore access to its conversation records'
    receipt = {**previous, 'state': 'ok' if confirmed else 'error' if failed else 'unknown',
               'ok': confirmed, 'message_id': message_id, 'checked_at': now(), 'value': value}
    if confirmed:
        receipt['reason'] = 'Starting prompt confirmed in its original conversation'
    elif failed:
        receipt['reason'] = previous.get('reason') or 'The harness recorded no consumption of this starting prompt'
    changes = {'submission_state': 'accepted' if confirmed else 'failed' if failed else 'unknown',
               'attempts': [*tracking.get('attempts', [])[:-1], receipt], 'native_receipt': value}
    if snapshot is not None and evidence in (True, False, None):
        changes['wake_prompt_context'] = snapshot
    if confirmed:
        changes['transcript_confirmed_at'] = now()
    update_message(directory, message_id, **changes)
    return receipt
