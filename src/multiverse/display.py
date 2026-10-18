"""Text for people: how the command line and the dashboard show agents, usage and launches.

The tmux status line keeps its own copies of a few of these: tmux runs it with
`python -S`, outside this package's imports.
"""


def clean(value):
    """Printable text only: terminal control characters become spaces."""
    return "".join(c if c.isprintable() else " " for c in str(value))


# Agents and launches

SUMMARY_LENGTH = 80
ERROR_LENGTH = 200


def shorten(text, limit=SUMMARY_LENGTH):
    """One line of at most `limit` characters, cut at a word boundary; only a single longer word is cut inside."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if not text[limit - 1].isspace() and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def error_text(error, tail=2):
    """A launch error on one line. Its first line says what happened and the lines at the end of the terminal
    usually say why, so a long one keeps the first line and the last `tail`, with `…` where lines were left out."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    if len(lines) > tail + 1:
        lines = [lines[0], "…", *lines[-tail:]]
    return " | ".join(line if line == "…" else shorten(line, ERROR_LENGTH) for line in lines)


def state(row):
    """What a person asks first: what a live agent is doing, otherwise why it is not live."""
    if row["live"] == "alive":
        return row["status"] if row.get("status") not in (None, "unknown", "—") else "alive"
    return row["live"]


HARNESS_NAMES = {"codex": "Codex", "claude": "Claude", "pi": "Pi"}


def mode(record):
    """How the agent is connected: `bridge` (native delivery) or `plain`."""
    runtime = record.get("runtime") or {}
    return (runtime.get("native_mode") or {}).get("mode") or ("bridge" if runtime.get("control_endpoint") else "plain")


def label(record):
    """`Codex, bridge`: the harness and how it is connected, as a person would say it."""
    harness = record.get("harness") or "agent"
    return f"{HARNESS_NAMES.get(harness, harness)}, {mode(record)}"


def outcome(record, verb, failure):
    """A launch summary and optional explanations; a failed wait raises with its reason."""
    name = record["name"]
    if record.get("ok") is False:
        raise ValueError(f"{name} {failure}: {record.get('error') or 'the launch did not finish'}")
    summary = f"{verb} {name} ({label(record)})" + (": ready." if record.get("ready") else ".")
    return with_notices(summary, record)


def with_notices(summary, record):
    notices = record.get("notices")
    lines = [clean(item).strip() for item in notices if isinstance(item, str) and item.strip()] if isinstance(notices, list) else []
    return "\n".join([summary, *lines])


def cause(error, name, failure):
    """The reason behind a failed launch, without the record ID and recovery advice around it."""
    return ValueError(f"{name} {failure}: {error.__cause__ or error}")


# Measured usage: a NativeResult dict whose value holds cumulative counters,
# the latest request's context and rate limits.

def usage_total(result):
    value = result.get('value')
    if result.get('state') != 'ok' or not isinstance(value, dict):
        return 'N/A'
    total = value.get('total_tokens')
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return 'N/A'
    prefix = '' if value.get('complete', True) else '~'
    return prefix + (f'{total / 1000000:.1f}m' if total >= 1000000 else f'{total / 1000:.1f}k' if total >= 10000 else str(total))


def usage_details(result):
    if result.get('state') != 'ok':
        return f"N/A ({result.get('state', 'unavailable')}): {result.get('reason', 'No measured usage')}"
    value = result.get('value') or {}
    lines = [context_line(result)] if usage_context(result) else []
    lines += [f'Rate limit {window}: {percent:g}% used' + (f' (resets {resets})' if resets else '')
              for window, percent, resets, _ in rate_limit_windows(result)]
    cumulative = [f'{key}: {value[key]}' for key in ('native_session_id', 'input_tokens', 'output_tokens',
                  'cached_input_tokens', 'cache_write_tokens', 'total_tokens', 'complete', 'source', 'reported_at')
                  if key in value]
    if cumulative:
        lines += ['Cumulative over all requests, subagents included:', *cumulative]
    return '\n'.join(lines)


def tokens(value):
    """'82k', '8.2k', '1.0m' or a plain number; no decimals once it is 10k or more."""
    if value >= 1000000:
        return f'{value / 1000000:.1f}m'
    if value >= 10000:
        return f'{round(value / 1000)}k'
    return f'{value / 1000:.1f}k' if value >= 1000 else str(int(value))


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def usage_context(result):
    """The latest request's size: {'tokens', 'window'?, 'percent'?} or None."""
    value = result.get('value') if result.get('state') == 'ok' else None
    found = value.get('context') if isinstance(value, dict) else None
    used = _number(found.get('tokens')) if isinstance(found, dict) else None
    if used is None:
        return None
    window = _number(found.get('window')) or None
    percent = _number(found.get('percent'))
    if percent is None and window:
        percent = used * 100 / window
    reason = found.get('estimated_reason')
    return {'tokens': used, 'window': window, 'percent': percent, 'estimated': found.get('estimated') is True,
            # What the estimate is (the words after "estimated"); records from before plugins set it are compaction's.
            # Plugin code sets it: one short line, so it cannot stretch the inspector.
            'estimated_reason': ' '.join(reason.split())[:80] if isinstance(reason, str) and reason.strip()
            else 'after compaction: conversation only',
            'last_known': found.get('last_known') is True}


def context_display(result):
    """'82k/200k' once the window is known, '82k' before, N/A with no reading."""
    found = usage_context(result)
    if found is None:
        return 'N/A'
    return ('~' if found['estimated'] else '') + tokens(found['tokens']) + (f"/{tokens(found['window'])}" if found['window'] else '')


def context_level(result):
    """'high' from 90%, 'warn' from 70%, else None."""
    percent = (usage_context(result) or {}).get('percent')
    return None if percent is None else 'high' if percent >= 90 else 'warn' if percent >= 70 else None


def cost_display(result):
    value = result.get('value') if result.get('state') == 'ok' else None
    cost = _number(value.get('cost_usd')) if isinstance(value, dict) else None
    return '—' if cost is None else f'${cost:.2f}'


def context_line(result):
    """'Context: 82k/200k (41%) · Cost: $1.23' for the inspector."""
    found = usage_context(result)
    line = 'Context: ' + context_display(result)
    if found and found['percent'] is not None:
        line += f" ({found['percent']:.0f}%" + (f", estimated {found['estimated_reason']})" if found['estimated'] else ')')
    elif found and found['estimated']:
        line += f" (estimated {found['estimated_reason']})"
    if found and found['last_known']:
        line += ' (last known)'
    cost = cost_display(result)
    return line + (f' · Cost: {cost}' if cost != '—' else '')


def rate_limit_windows(result):
    """[(label, percent_used, resets_at, reported_at)] for the five-hour and seven-day windows."""
    value = result.get('value') if result.get('state') == 'ok' else None
    found = []
    for limit in value.get('rate_limits') or [] if isinstance(value, dict) else []:
        window = {'five_hour': '5h', 'seven_day': '7d'}.get(limit.get('window')) if isinstance(limit, dict) else None
        percent = _number(limit.get('percent_used')) if window else None
        if percent is not None:
            found.append((window, percent, limit.get('resets_at'), value.get('context_reported_at')))
    return found
