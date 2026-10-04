"""Measured context from Codex's exact-thread app-server notifications."""
from pathlib import Path

from ..types import NativeResult
from ...store import read


def counter(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1 else None


def notification(value, native_id):
    if not native_id or value.get('method') != 'thread/tokenUsage/updated':
        return None
    params = value.get('params')
    if not isinstance(params, dict) or params.get('threadId') != native_id:
        return None
    usage = params.get('tokenUsage')
    last = usage.get('last') if isinstance(usage, dict) else None
    tokens = counter(last.get('totalTokens')) if isinstance(last, dict) else None
    if tokens is None:
        return None
    # Codex includes cached input and reasoning output in totalTokens already.
    context = {'tokens': tokens}
    window = counter(usage.get('modelContextWindow'))
    if window:
        context['window'] = window
        # Codex 0.159.2 TokenUsage::percent_of_context_window_remaining uses
        # last.totalTokens with a fixed baseline and positive half-up rounding.
        # Keep raw counts for the column, and native percentage for the inspector.
        baseline = 12000
        if window <= baseline:
            remaining_percent = 0
        else:
            effective = window - baseline
            remaining = max(0, effective - max(0, tokens - baseline))
            remaining_percent = int(remaining * 100 / effective + .5)
        context['percent'] = 100 - remaining_percent
    return {'native_session_id': native_id, 'context': context, 'source': 'codex-app-server'}


def last_measurement(facts, native_id):
    """Newest saved measurement for this exact thread, including across resumes."""
    if not native_id or not isinstance(facts, dict):
        return None
    for key in ('usage', 'last_usage'):
        usage = facts.get(key)
        if (isinstance(usage, dict) and usage.get('native_session_id') == native_id
                and usage.get('source') == 'codex-app-server'
                and isinstance(usage.get('context'), dict)
                and counter(usage['context'].get('tokens')) is not None):
            return usage
    return None


def measured(session, *, bridge=True):
    if not session.native_id or not session.launch_id or not session.session_file:
        return NativeResult('unavailable', 'No bound Codex launch for context usage')
    try:
        facts = read(Path(session.session_file).parent / 'runtime.json')
        usage = last_measurement(facts, session.native_id)
        if usage is not None and facts.get('launch_id') == session.launch_id:
            relay = facts.get('native_relay')
            last_known = (usage is not facts.get('usage') or not bridge
                          or facts.get('state') != 'running' or 'exit_code' in facts
                          or bool(facts.get('ending_at'))
                          or isinstance(relay, dict) and relay.get('state') == 'lost')
            return NativeResult('ok', 'Last known context reported by Codex app-server' if last_known
                                else 'Last measured context reported by Codex app-server', {
                'context': {**usage['context'], **({'last_known': True} if last_known else {})},
                'context_source': usage['source'],
                'context_reported_at': usage.get('reported_at')})
    except (OSError, ValueError, TypeError):
        pass
    return NativeResult('unavailable', 'Codex has no saved context usage for this thread yet')
