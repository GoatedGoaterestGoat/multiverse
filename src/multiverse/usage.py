"""Display contract for measured native-session cumulative usage.

Input includes cached input; cache counts are subsets, never added twice.
Unknown counters are None. An observed zero is distinct from unavailable.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Usage:
    native_session_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    source: str
    reported_at: str
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    complete: bool = True


def display(result):
    value = result.get('value')
    if result.get('state') != 'ok' or not isinstance(value, dict):
        return 'N/A'
    total = value.get('total_tokens')
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return 'N/A'
    prefix = '' if value.get('complete', True) else '~'
    return prefix + (f'{total / 1000000:.1f}m' if total >= 1000000 else f'{total / 1000:.1f}k' if total >= 10000 else str(total))


def details(result):
    if result.get('state') != 'ok':
        return f"N/A ({result.get('state', 'unavailable')}): {result.get('reason', 'No measured usage')}"
    value = result.get('value') or {}
    lines = [context_line(result)] if context(result) else []
    lines += [f'Rate limit {label}: {percent:g}% used' + (f' (resets {resets})' if resets else '')
              for label, percent, resets, _ in rate_limit_windows(result)]
    cumulative = [f'{key}: {value[key]}' for key in ('native_session_id', 'input_tokens', 'output_tokens',
                  'cached_input_tokens', 'cache_write_tokens', 'total_tokens', 'complete', 'source', 'reported_at')
                  if key in value]
    if cumulative:
        lines += ['Cumulative over all requests, subagents included:', *cumulative]
    return '\n'.join(lines)


def count(value):
    """'82k', '8.2k', '1.0m' or a plain number; no decimals once it is 10k or more."""
    if value >= 1000000:
        return f'{value / 1000000:.1f}m'
    if value >= 10000:
        return f'{round(value / 1000)}k'
    return f'{value / 1000:.1f}k' if value >= 1000 else str(int(value))


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def context(result):
    """The latest request's size: {'tokens', 'window'?, 'percent'?} or None."""
    value = result.get('value') if result.get('state') == 'ok' else None
    found = value.get('context') if isinstance(value, dict) else None
    tokens = _number(found.get('tokens')) if isinstance(found, dict) else None
    if tokens is None:
        return None
    window = _number(found.get('window')) or None
    percent = _number(found.get('percent'))
    if percent is None and window:
        percent = tokens * 100 / window
    return {'tokens': tokens, 'window': window, 'percent': percent, 'estimated': found.get('estimated') is True,
            'last_known': found.get('last_known') is True}


def context_display(result):
    """'82k/200k' once the window is known, '82k' before, N/A with no reading."""
    found = context(result)
    if found is None:
        return 'N/A'
    return ('~' if found['estimated'] else '') + count(found['tokens']) + (f"/{count(found['window'])}" if found['window'] else '')


def context_level(result):
    """'high' from 90%, 'warn' from 70%, else None."""
    percent = (context(result) or {}).get('percent')
    return None if percent is None else 'high' if percent >= 90 else 'warn' if percent >= 70 else None


def cost_display(result):
    value = result.get('value') if result.get('state') == 'ok' else None
    cost = _number(value.get('cost_usd')) if isinstance(value, dict) else None
    return '—' if cost is None else f'${cost:.2f}'


def context_line(result):
    """'Context: 82k/200k (41%) · Cost: $1.23' for the inspector."""
    found = context(result)
    line = 'Context: ' + context_display(result)
    if found and found['percent'] is not None:
        line += f" ({found['percent']:.0f}%" + (', estimated after compaction)' if found['estimated'] else ')')
    elif found and found['estimated']:
        line += ' (estimated after compaction)'
    if found and found['last_known']:
        line += ' (last known)'
    cost = cost_display(result)
    return line + (f' · Cost: {cost}' if cost != '—' else '')


def rate_limit_windows(result):
    """[(label, percent_used, resets_at)] for the five-hour and seven-day windows."""
    value = result.get('value') if result.get('state') == 'ok' else None
    found = []
    for limit in value.get('rate_limits') or [] if isinstance(value, dict) else []:
        label = {'five_hour': '5h', 'seven_day': '7d'}.get(limit.get('window')) if isinstance(limit, dict) else None
        percent = _number(limit.get('percent_used')) if label else None
        if percent is not None:
            found.append((label, percent, limit.get('resets_at'), value.get('context_reported_at')))
    return found
