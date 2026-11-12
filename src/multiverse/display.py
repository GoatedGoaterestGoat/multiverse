"""Text for people: how the command line and the dashboard show agents, usage and launches.

The tmux status line keeps its own copies of a few of these: tmux runs it with
`python -S`, outside this package's imports.
"""
from datetime import datetime, timezone


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


def live_label(row):
    """A proven in-progress preparation has no native process to call alive yet."""
    facts = row.get('runtime') or {}
    if row['live'] == 'unknown' and facts.get('preparing') is True and facts.get('state') == 'starting':
        return 'starting'
    return row['live']


def state(row):
    """What a person asks first: what a live agent is doing, otherwise why it is not live."""
    if row["live"] == "alive":
        return row["status"] if row.get("status") not in (None, "unknown", "—") else "alive"
    return live_label(row)


def model_parts(row):
    """(name, effort) for an agent's Model cell, or (None, None) when nothing is known:

    1. a running agent: the model and effort its session last reported (`usage.value`);
    2. otherwise, or before a reading lands: its recorded model;
    3. no recorded model: its harness's default (`settings.model`), what a launch would use;
    4. none of these: nothing, which reads `-`.

    The name is the listing's display name for that model (custom, else the table's), else the spelling as
    recorded. The effort is a running agent's session's where it reports one (with or without its model), else
    the recorded effort; never a default."""
    def text(value):
        return isinstance(value, str) and value.strip()

    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    observed = usage.get("value") if usage.get("state") == "ok" and isinstance(usage.get("value"), dict) else {}
    running = row.get("live") == "alive"
    # The effort follows the session on its own: a session can report its effort without its model.
    effort = (observed["effort"] if running and text(observed.get("effort")) else
              row.get("effort") if text(row.get("effort")) else None)
    if running and text(observed.get("model")):
        name, model = row.get("observed_model_name"), observed["model"]
    elif text(row.get("model")):
        name, model = row.get("model_name"), row["model"]
    elif text(row.get("default_model")):
        name, model = row.get("default_model_name"), row["default_model"]
    else:
        return None, None
    return clean(name if text(name) else model).strip(), clean(effort).strip() if effort else None


def model_label(row, effort=True):
    """An agent's model as people name it, `Opus 5.5`, with its effort when asked, `Opus 5.5 (xhigh)`; `-` when
    nothing is known. See model_parts for which model it is."""
    name, chosen = model_parts(row)
    if name is None:
        return "-"
    return name + (f" ({chosen})" if effort and chosen else "")


def _target(value):
    """The name of what a blocked agent waits on, and whether the listing could still find it; (None, False) for
    anything that isn't a readable `blocked_on`."""
    if not isinstance(value, dict):
        return None, False
    if value.get("kind") in ("user", "self"):
        return value["kind"], False
    name = clean(value.get("name")).strip() if value.get("kind") == "agent" and isinstance(value.get("name"), str) else ""
    if not name:
        return None, False
    if name in ("user", "self"):
        name = f"agent {name}"
    place = value.get("universe")
    if isinstance(place, str) and place:
        if place.startswith("/"):  # outside this universe's subverses: the universe's own folder name
            parts = place.rstrip("/").split("/")
            place = parts[-2] if parts[-1] == ".multiverse" and len(parts) > 1 else parts[-1]
        name += f" in {clean(place)}"
    return name, bool(value.get("gone"))


def blocked_label(value):
    """What a blocked agent waits on: `lifecycle`, `qa-claude in qa`, `user`, `self`.

    `value` is a listing row's `blocked_on`. An agent named like one of the two other sources reads `agent user`,
    so it is never taken for the person; one the listing can no longer find adds `: gone`. Anything else is None."""
    name, gone = _target(value)
    return name and name + (": gone" if gone else "")


def summary_parts(row):
    """What a blocked agent waits on, where the row carries it (`Waiting for user`, `Waiting for ada (gone)` for
    one the listing can no longer find), else None; and its summary on one line."""
    summary = " ".join(str(row.get("summary") or "").split())
    name, gone = _target(row.get("blocked_on")) if row.get("phase") == "blocked" else (None, False)
    return (f"Waiting for {name}" + (" (gone)" if gone else "") if name else None), summary


def summary_label(row):
    """An agent's summary, led by what a blocked one waits on where the row carries it:
    `Waiting for user: decide the model list`, or `Waiting for user` with no summary."""
    lead, summary = summary_parts(row)
    return f"{lead}: {summary}" if lead and summary else lead or summary


def _now():
    return datetime.now(timezone.utc)


def report_age(row):
    """How long ago a working or blocked agent last reported, so how stale its summary is: `32s`, `10m`, `2h`,
    `3d`. None for any other phase, or a record whose report time isn't a readable timestamp with its time zone
    (core writes UTC; a naive or date-only one is damaged, not guessed at)."""
    if row.get("phase") not in ("working", "blocked") or not isinstance(row.get("reported_at"), str):
        return None
    try:
        at = datetime.fromisoformat(row["reported_at"])
        if at.tzinfo is None:
            return None
        seconds = int(max(0, (_now() - at).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def phase_label(row):
    """An agent's reported phase, with how long ago it reported: `working (10m)`, `blocked (50m)`. What a blocked
    agent waits on is in its summary (`summary_label`)."""
    phase, age = row.get("phase"), report_age(row)
    return f"{phase} ({age})" if phase and age else phase


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
    return "\n".join([summary, *notice_lines(record.get('notices'))])


def notice_lines(*groups):
    """Optional presentation strings: one line each, no duplicates or coercion."""
    result = []
    seen = set()
    for group in groups:
        if not isinstance(group, (list, tuple)):
            continue
        for value in group:
            if isinstance(value, str):
                line = clean(value).strip()
                if line and line not in seen:
                    seen.add(line)
                    result.append(line)
    return result


def agent_name(root, reference):
    """The agent's name for a sentence, or the reference as given when its record can't be read."""
    from .universe import agents
    from .store import read
    try:
        return read(agents.directory(root, reference) / "properties.json").get("name") or reference
    except (OSError, ValueError, KeyError, TypeError):
        return reference


def sentence(text):
    """`text` cleaned for one line, starting with a capital and ending with a period."""
    text = " ".join(clean(text).split()).rstrip(".")
    return (text[:1].upper() + text[1:] + ".") if text else ""


def cause(error, name, failure):
    """The reason behind a failed launch, without the record ID and recovery advice around it."""
    from .universe.records import RecordError
    reason = f'{error.file}: {error.reason}' if isinstance(error, RecordError) else error.__cause__ or error
    return ValueError(f"{name} {failure}: {reason}")


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
    """The latest request's size as core measured it: {'tokens', 'window', 'percent', 'last_known'} or None."""
    value = result.get('value') if result.get('state') == 'ok' else None
    found = value.get('context') if isinstance(value, dict) else None
    used = _number(found.get('tokens')) if isinstance(found, dict) else None
    if used is None:
        return None
    window = _number(found.get('window')) or None
    percent = _number(found.get('percent'))
    if percent is None and window:
        percent = used * 100 / window
    return {'tokens': used, 'window': window, 'percent': percent, 'last_known': found.get('last_known') is True}


def context_display(result):
    """'82k/200k' once the window is known, '82k' before, N/A with no reading."""
    found = usage_context(result)
    if found is None:
        return 'N/A'
    return tokens(found['tokens']) + (f"/{tokens(found['window'])}" if found['window'] else '')


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
        line += f" ({found['percent']:.0f}%)"
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
