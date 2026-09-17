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
    return '\n'.join(f'{key}: {value[key]}' for key in ('native_session_id', 'input_tokens', 'output_tokens',
                     'cached_input_tokens', 'cache_write_tokens', 'total_tokens', 'complete', 'source', 'reported_at') if key in value)
