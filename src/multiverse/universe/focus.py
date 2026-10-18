"""An agent's focus: while it lasts, only inbox-only messages reach the agent.

An agent focuses when its answer needs the person's attention, so that steer and
follow-up messages cannot start other work and bury the question. `focus.json` in
the agent directory holds the focus while it lasts:

    {"since": ISO time, "until": ISO time or null, "turns": count or null, "by": "agent" or "user"}

`until` ends it at a time. `turns` counts the turn ends still to come: when the
harness status goes from working or waiting to idle, `turn_ended` counts one, and
the last removes the file. With neither, it lasts until `end`. Readers never
remove an expired record; the next `start` or `end` replaces it.

The tmux status line reads this file with its own copy of `active`.
"""
from datetime import datetime, timedelta, timezone
import re

from ..store import lock, read, write

FILE = "focus.json"
# A day is the longest timed focus: one that outlives its question is forgotten.
LONGEST = 86400
DURATION = re.compile(r"(\d+(?:\.\d+)?)([smhd])")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(value):
    """`5m`, `20m`, `1h`, `1h30m` or `90s` in seconds."""
    text = str(value).strip().lower()
    parts = DURATION.findall(text)
    if not parts or "".join(number + unit for number, unit in parts) != text:
        raise ValueError("Give the focus a duration such as 5m, 20m, 1h or 1h30m")
    total = sum(float(number) * UNITS[unit] for number, unit in parts)
    if not 0 < total <= LONGEST:
        raise ValueError("A timed focus lasts more than 0 seconds and at most 1d; leave out --for to focus until unfocused")
    return total


def _time(value):
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def active(record, at=None):
    """Whether a focus record is still in effect. An unreadable one is not: it must never block messages."""
    if not isinstance(record, dict) or not isinstance(record.get("since"), str):
        return False
    until = record.get("until")
    if until is None:
        return True
    try:
        return _time(until) > (at or datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return False


def current(agent_dir):
    """The focus in effect, or None."""
    try:
        record = read(agent_dir / FILE)
    except (OSError, ValueError):
        return None
    return record if active(record) else None


def start(agent_dir, *, duration=None, turn=False, status=None, by="user"):
    """Focus for `duration` seconds, until the agent's next turn ends, or until `end`.

    An agent that is working when it focuses has its current turn to finish first,
    so its next turn is the one after that. Refocusing keeps the original `since`.
    """
    if duration is not None and turn:
        raise ValueError("Choose a duration or the next turn, not both")
    stamp = datetime.now(timezone.utc)
    with lock(agent_dir / ".focus.lock"):
        previous = current(agent_dir)
        record = {"since": previous["since"] if previous else stamp.isoformat(),
                  "until": (stamp + timedelta(seconds=duration)).isoformat() if duration is not None else None,
                  "turns": (2 if status == "working" else 1) if turn else None, "by": by}
        write(agent_dir / FILE, record)
    return record


def end(agent_dir):
    """Remove the focus. Returns the record that was in effect, or None."""
    with lock(agent_dir / ".focus.lock"):
        record = current(agent_dir)
        (agent_dir / FILE).unlink(missing_ok=True)
    return record


def turn_ended(agent_dir):
    """Count one turn end for a focus that lasts turns; the last one ends it.

    The status change that calls this has been saved already; failing here must not fail it.
    """
    if not (agent_dir / FILE).exists():
        return  # Never create a lock file in an agent directory that was removed.
    try:
        with lock(agent_dir / ".focus.lock"):
            record = current(agent_dir)
            turns = record.get("turns") if record else None
            if isinstance(turns, int) and not isinstance(turns, bool):
                if turns <= 1:
                    (agent_dir / FILE).unlink(missing_ok=True)
                else:
                    write(agent_dir / FILE, {**record, "turns": turns - 1})
    except OSError:
        pass  # The focus stays on; unfocus still ends it.


def ends(record, own=False):
    """When the focus ends, as a person would say it: `until 14:20 (18 min left)`."""
    if record.get("until"):
        until = _time(record["until"])
        left = max(0, (until - datetime.now(timezone.utc)).total_seconds())
        local = until.astimezone()
        clock = local.strftime("%H:%M") if left < 12 * 3600 else local.strftime("%b %d %H:%M")
        return f"until {clock} ({remaining(left)} left)"
    if record.get("turns"):
        return f"until {'your' if own else 'its'} next turn ends"
    return "until unfocused"


def remaining(left):
    """`45 s`, `18 min` or `2 h 5 min`."""
    if left < 60:
        return f"{int(left)} s"
    minutes = int(-(-left // 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h" + (f" {minutes % 60} min" if minutes % 60 else "")


def short(record):
    """A focus in a few characters for a table cell: `18m`, `2h05m`, `turn` or `on`."""
    if record.get("until"):
        left = max(0, (_time(record["until"]) - datetime.now(timezone.utc)).total_seconds())
        minutes = int(-(-left // 60))
        return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"
    return "turn" if record.get("turns") else "on"
