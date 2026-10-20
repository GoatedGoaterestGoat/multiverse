"""Reconcile an old acceptance only after its exact process and conversation are settled."""
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from ..harnesses.types import NativeResult
from ..store import read
from ..universe import mailbox, storage
from . import attachments


def check(directory, adapter, session, executor, message, tracking):
    """No native submission: missing evidence keeps an acceptance unconfirmed."""
    from .native import adapter_hook, observe
    from .lifecycle import launch_ready
    hook = adapter_hook(adapter, 'accepted_message_evidence')
    if hook is None or tracking.get('submission_state') != 'accepted':
        return None
    attempt = (tracking.get('attempts') or [{}])[-1]
    old = attempt.get('launch_id')
    if (not isinstance(old, str) or old == session.launch_id or not session.native_id
            or session.status != 'idle'):
        return None
    try:
        if str(UUID(old)) != old:
            return None
        facts = executor.facts
        request = storage.launch_request(directory, facts)
        if not launch_ready(adapter, executor.properties, facts, directory, request,
                            observe(executor.properties, facts)['live']):
            return None
        cleanup = read(directory / 'launches' / old / 'cleanup.json')
        if (not isinstance(cleanup, dict) or cleanup.get('schema') != 1 or cleanup.get('launch_id') != old
                or any(type(cleanup.get(key)) is not int or cleanup[key] <= 0 for key in ('pid', 'child_pid'))):
            return None
        # A completed journal alone is not process-exit proof. Sample the saved
        # runner and child independently, including unknown inspection/reuse.
        old_facts = {'launch_id': old, 'runner_pid': cleanup['pid'], 'runner_started': cleanup.get('pid_started'),
                     'pid': cleanup['child_pid'], 'pid_started': cleanup.get('child_started')}
        if attachments.recorded_process_problem(directory, old_facts) or attachments.problem(directory, old_facts):
            return None
        saved = tracking.get('delivery_context')
        if isinstance(saved, dict):
            if saved.get('launch_id') != old:
                return None
            native_id, home, workspace = saved.get('native_id'), saved.get('home'), saved.get('workspace')
            text, since, framed = saved.get('text'), saved.get('since'), True
        else:
            # Historical receipts predate exact framing snapshots. The adapter
            # may prove absence of the body, but must not confirm a body match.
            native_id = attempt.get('native_session_id') or tracking.get('target_session') or message.get('target_session')
            request = storage.launch_request(directory, {'launch_id': old})
            environment = storage.request_environment(request, (*getattr(adapter, 'storage_env', ()), 'HOME'))
            home_hook = adapter_hook(adapter, 'session_home')
            keys = getattr(adapter, 'storage_env', ())
            primary = environment.get(keys[0]) if keys else None
            base = environment.get('HOME')
            proven = any(isinstance(value, str) and Path(value).is_absolute() for value in (primary, base))
            home = home_hook(environment) if home_hook and proven else None
            # Old requests had no workspace. Empty means every transcript
            # folder is a candidate; an unreadable one must block absence.
            workspace = request.get('workspace') or ''
            text, since, framed = message.get('text'), attempt.get('at'), False
        if (native_id != session.native_id or not isinstance(home, str) or not Path(home).is_absolute()
                or home != session.home or not isinstance(workspace, str)
                or bool(workspace) and not Path(workspace).is_absolute() or framed and not workspace
                or not isinstance(text, str) or not text or not isinstance(since, str)):
            return None
        # Keep the accepting launch's workspace/framing even after later renames.
        evidence_session = replace(session, workspace=workspace, launch_id=old)
        # Final/confirmed acceptances still consume their stored text row. Read
        # one ledger snapshot, then release its lock before provider evidence.
        cohort = mailbox.acceptance_contexts(directory, native_id, home, message['text']) if framed else ()
        result = hook(evidence_session, message['id'], text, since, home, framed=framed, cohort=cohort)
        delivered = result.value.get('delivered') if isinstance(result.value, dict) else None
        if result.state != 'ok' or type(delivered) is not bool:
            return NativeResult('unknown', result.reason or 'Accepted-message transcript evidence is incomplete')
        if delivered:
            return NativeResult('native_queued', 'Message confirmed in its conversation', {'transcript_confirmed': True})
        return NativeResult('unavailable', 'Message was lost when the agent stopped',
                            {'lost_when_stopped': True, 'transcript_confirmed': False})
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        return NativeResult('unknown', f'Cannot verify the earlier acceptance: {error}')
