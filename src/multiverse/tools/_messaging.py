"""Human summaries of delivery evidence; internal receipts stay unchanged."""
import re
import json
from ..display import clean


def failure_reason(reason, text=''):
    reason = str(reason or 'the session could not accept the message')
    if text:
        # Replace whole reflected values once. Short conversational tokens are
        # not secrets and occur inside ordinary diagnostic prose.
        pieces = {text, json.dumps(text, ensure_ascii=False)[1:-1], *text.splitlines()}
        pieces = {piece for piece in pieces if len(piece.strip()) >= 7 or (len(piece.strip()) >= 3 and any(char.isspace() for char in piece.strip()))}
        if pieces:
            pattern = r'(?<!\w)(?:' + '|'.join(re.escape(piece) for piece in sorted(pieces, key=len, reverse=True)) + r')(?!\w)'
            reason = re.sub(pattern, 'the message', reason)
    lower = reason.lower()
    if 'cannot wake' in lower:
        return 'this harness cannot resume with a message'
    if 'different' in lower and ('session' in lower or 'thread' in lower):
        return 'the saved conversation does not match the running session'
    if 'not registered' in lower or 'inbox key' in lower:
        return 'messaging is not connected for this session'
    if 'not controllable' in lower or 'direct input' in lower:
        return 'the conversation is not ready to receive messages'
    if 'plain' in lower or 'bridge mode' in lower:
        return 'this running session does not support messaging'
    if 'binding' in lower or 'bound' in lower:
        return 'the session is still starting'
    # Identifiers in transport exceptions belong in --json, never the summary.
    reason = re.sub(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', 'the session', reason, flags=re.I)
    reason = re.sub(r'\b(?:thread|turn|msg|message|launch)[_-][a-zA-Z0-9_-]{6,}\b', 'the session', reason)
    reason = reason.splitlines()[0].split(';')[0].strip().rstrip('.')
    reason = ' '.join(reason.split())
    for original, replacement in (('Pre-submission session check failed:', 'session check failed:'),
                                   ('Native', 'Session'), ('native', 'session'),
                                   ('RPC', 'connection'), ('transcript', 'conversation')):
        reason = reason.replace(original, replacement)
    return (reason[:220].rstrip() + '…' if len(reason) > 220 else reason) or 'the session could not accept the message'


def delivery_line(name, receipt, *, delivery='steer', wake=False, check=False, accepted=False, text='', running=False):
    state = receipt.get('state')
    value = receipt.get('value')
    value = value if isinstance(value, dict) else {}
    reason = str(receipt.get('reason') or '')
    if state in ('error', 'unavailable'):
        # A file path is actionable evidence, not an opaque native identifier.
        why = clean(reason) if value.get('pre_submission_failure') is True else failure_reason(reason, text)
        if value.get('launch_only') is True:
            return (f"Not delivered to {name}: {why}. Saved; resend it with --delivery inbox-only "
                    f"for manual reading, or attach with mv2 attach {name}.")
        if value.get('pre_submission_failure') is True:
            return (f"Not delivered to {name}: {why}. Saved without submission; repair the named file, "
                    f"then mv2 deliver {name} --retry.")
        if value.get('repair_runtime'):
            return (f"Not delivered to {name}: {why}. Saved; restore {clean(value['repair_runtime'])}, "
                    f"or run mv2 kill {name} to close its verified terminal, then mv2 deliver {name} --retry.")
        if value.get('wake_refused') is True or value.get('definite_failure') is True:
            return (f"Not delivered to {name}: {why}. Saved; fix the reason, "
                    f"then mv2 deliver {name} --retry.")
        if check:
            return f"{name}: could not check delivery: {why}. Saved; resume it, then mv2 deliver {name} --check."
        if running:
            return (f"Not delivered to {name}: {why}. Saved; restart it with mv2 kill {name}, "
                    f"then mv2 resume {name}, then mv2 deliver {name} --retry.")
        return f"Not delivered to {name}: {why}. Saved; resume it, then mv2 deliver {name} --retry."
    if state == 'unknown':
        if accepted:
            return f'{name}: message accepted; no confirmation yet. Do not resend.'
        if value.get('evidence_problem'):
            return (f"{name}: cannot yet tell whether it received the message. {clean(value['evidence_problem']).rstrip('.')}; "
                    f"then run mv2 deliver {name} --check. Do not resend.")
        return f"{name}: cannot yet tell whether it received the message. Saved; run mv2 deliver {name} --check before sending again."
    if value.get('transcript_confirmed') is True:
        return f'{name}: message confirmed in its conversation.'
    if wake or 'interactive starting prompt' in reason or 'interactive prompt' in reason:
        return f'{name} was stopped; resuming it with your message.'
    if state == 'pending':
        return f'{name} is starting; your message will be delivered when ready.'
    if check:
        if 'queue' in reason.lower():
            return f'{name}: message is queued for its next turn. Do not resend.'
        return f'{name}: message accepted; no confirmation yet. Do not resend.'
    if value.get('idle_start_error'):
        return f'{name} has your message queued; its next turn has not started. Run mv2 deliver {name} --check; do not resend.'
    if value.get('queued_submission_id') and not value.get('turn_id'):
        return f'{name} has your message queued for its next turn.'
    if 'idle queue' in reason:
        return f'{name} started a turn with your message.'
    if delivery == 'follow-up':
        return f'{name} has your message for its next turn.'
    return f'Sent to {name}; it reads this at its next pause.'
