"""An agent's focus: while it lasts, only inbox-only messages reach the agent.

An agent focuses when its answer needs the person's attention, so that steer and
follow-up messages cannot start other work and bury the question. `focus.json` in
the agent directory holds the focus while it lasts:

    {"since": ISO time, "until": ISO time or null, "turns": 1, 2 or null, "by": "agent" or "user"}

`until` ends it at a time. `turns` counts the turn ends still to come: when the
harness status goes from working or waiting to idle, `turn_ended` counts one, and
the last removes the file. With neither, it lasts until `end`. Readers never
remove an expired record; the next `start` or `end` replaces it.

Starting a focus and counting a turn end both happen under the agent's runtime
lock, so a turn end saved before a `focus --turn` never counts against it.
`start` also holds the send lock, so a send that found no focus finishes first.
Lock order: send, runtime, focus.

The tmux status line reads this file with its own copy of `active`.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import stat

from ..store import lock, write

FILE = "focus.json"
# A day is the longest timed focus: one that outlives its question is forgotten.
LONGEST = 86400
DURATION = re.compile(r"(\d+(?:\.\d+)?)([smhd])")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# A focus record is a few short fields; anything larger is damage, not a focus. The tmux bar's limit.
READ_LIMIT = 1024 * 1024


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
    """Whether a focus record is still in effect. A damaged one is not: it must never block messages.

    In effect means a readable `since`, `by` the agent or the user, `turns` absent or 1 or 2 (all that
    `start` writes), and `until` absent or in the future but no further than the longest focus, which also
    keeps `ends` and `short` in range.
    """
    at = at or datetime.now(timezone.utc)
    try:
        _time(record["since"])
        turns = record.get("turns")
        if record.get("by") not in ("agent", "user") or turns is not None and (type(turns) is not int
                                                                               or turns not in (1, 2)):
            return False
        until = record.get("until")
        return until is None or at < _time(until) <= at + timedelta(seconds=LONGEST)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return False


def _read(agent_dir):
    """The focus record, or None for anything but a small JSON object in a regular file."""
    try:
        # Non-blocking, so a FIFO in its place never hangs a reader. The descriptor is closed here: wrapping
        # a directory's would fail and leak it.
        descriptor = os.open(agent_dir / FILE, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                data = source.read(READ_LIMIT + 1)
        finally:
            os.close(descriptor)
        record = json.loads(data) if len(data) <= READ_LIMIT else None
    except (OSError, ValueError, TypeError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def current(agent_dir):
    """The focus in effect, or None."""
    record = _read(agent_dir)
    return record if active(record) else None


class NoTurns(ValueError):
    """The harness is not reporting its status now, so no turn end would ever count."""


def start(agent_dir, *, duration=None, turn=False, by="user"):
    """Focus for `duration` seconds, until the agent's next turn ends, or until `end`.

    The status is read under the runtime lock, so a turn end is counted either
    before this focus exists or against it, never lost in between. An agent
    focusing itself is always mid-turn, and so is one that is working: that turn
    finishes first, so the next turn is the one after it. For one that is waiting on
    the person, or idle, the turn that takes the person's answer is the one that ends
    it. Refocusing keeps the original `since`.
    """
    if duration is not None and turn:
        raise ValueError("Choose a duration or the next turn, not both")
    from .agents import runtime
    # The send lock: a send or retry that found no focus finishes before this one starts.
    # No lock creates a folder, so a removed agent isn't recreated here. The write below still would, if the
    # agent were moved or removed while these are held.
    with (lock(agent_dir / ".send.lock", create_parent=False), lock(agent_dir / ".runtime.lock", create_parent=False),
          lock(agent_dir / ".focus.lock", create_parent=False)):
        status = runtime(agent_dir).get("status")
        if turn and status not in ("working", "waiting", "idle"):
            raise NoTurns(status)
        stamp = datetime.now(timezone.utc)
        previous = current(agent_dir)
        record = {"since": previous["since"] if previous else stamp.isoformat(),
                  "until": (stamp + timedelta(seconds=duration)).isoformat() if duration is not None else None,
                  "turns": (2 if by == "agent" or status == "working" else 1) if turn else None, "by": by}
        write(agent_dir / FILE, record)
    return record


def end(agent_dir):
    """Remove the focus. Returns the record that was in effect, or None."""
    with lock(agent_dir / ".focus.lock", create_parent=False):
        record = current(agent_dir)
        path = agent_dir / FILE
        if path.is_symlink() or not path.is_dir():  # a directory in its place is no focus, and not ours to remove
            path.unlink(missing_ok=True)
    return record


def turn_ended(agent_dir):
    """Count one turn end for a focus that lasts turns; the last one ends it.

    Runs under the runtime lock with the status change that ended the turn, which
    has been saved already: failing here must never fail it.
    """
    try:
        if not (agent_dir / FILE).exists():
            return  # Never create a lock file in an agent directory that was removed.
        with lock(agent_dir / ".focus.lock", create_parent=False):
            record = current(agent_dir)
            turns = record.get("turns") if record else None
            if turns is not None:
                if turns <= 1:
                    (agent_dir / FILE).unlink(missing_ok=True)
                else:
                    write(agent_dir / FILE, {**record, "turns": turns - 1})
    except Exception:
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
