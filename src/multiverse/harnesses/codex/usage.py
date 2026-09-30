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


def measured(session):
    if not session.native_id or not session.launch_id or not session.session_file:
        return NativeResult('unavailable', 'No bound Codex launch for context usage')
    try:
        facts = read(Path(session.session_file).parent / 'runtime.json')
        usage = facts.get('usage')
        if (facts.get('launch_id') == session.launch_id and isinstance(usage, dict)
                and usage.get('native_session_id') == session.native_id
                and usage.get('source') == 'codex-app-server'
                and isinstance(usage.get('context'), dict)
                and counter(usage['context'].get('tokens')) is not None):
            return NativeResult('ok', 'Last measured context reported by Codex app-server', {
                'context': usage['context'], 'context_source': usage['source'],
                'context_reported_at': usage.get('reported_at')})
    except (OSError, ValueError, TypeError):
        pass
    return NativeResult('unavailable', 'Codex has not reported context usage for this launch yet')
