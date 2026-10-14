"""The event journal: what happened in a universe, for plugins' event handlers.

Core records an event where it happens (a runner, the mod's status report, a
send) and the plugin host delivers it where plugins run. A universe writes a
journal only while a plugin that is on there subscribes to the event's type
(manifest `events`), so a universe that uses none writes nothing. Message text
never goes in.

    state/events/
      000000001041.jsonl   a closed segment, named by its first sequence number
      000000002388.jsonl   the segment being written
      seq                  the next sequence number
      .lock                held while appending, rolling and pruning

A segment rolls at 1 MiB or when a new UTC day begins. Closed segments are
deleted once their newest event is older than `retain_days`, and the oldest
go while they exceed `max_mb` (plugins.json `events`). The segment being
written is never deleted.

Recording is best effort and never holds up the change it reports: emit
never raises, waits at most a moment for the journal's lock, and an event it
cannot write is logged to state/plugins/events.log and dropped. Delivery is
different: an event in the journal reaches each handler at least once (the
host keeps a cursor per plugin). One event is at most 16 KiB; larger data is
replaced by a note of its size. Sequence numbers survive a crash between
writing an event and counting it, and a torn last line is sealed and skipped.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import fnmatch
import json
from pathlib import Path
import time

from ..store import lock, now
from . import config, loader
from .manifest import CORE_EVENTS

DIRECTORY = "events"
SEGMENT_BYTES = 1024 * 1024
EVENT_BYTES = 16 * 1024
REASON_CHARACTERS = 200  # a core event's reason, cut to its first line
LOCK_SECONDS = 0.2  # the longest emit waits for the journal; past it, the event is dropped and logged
TAIL_BYTES = 64 * 1024
DEFAULT_RETAIN_DAYS = 7
DEFAULT_MAX_MB = 32
NAME_WIDTH = 12


def directory(root):
    return Path(root) / "state" / DIRECTORY


def segments(root):
    """Closed and current segments, oldest first: [(first sequence, path)]."""
    found = []
    for path in directory(root).glob("*.jsonl"):
        if path.stem.isdigit():
            found.append((int(path.stem), path))
    return sorted(found)


def subscribers(root, kind):
    """Names of the plugins on here whose manifest subscribes to events of `kind`."""
    if loader.safe_mode():
        return []
    return [found.name for found in loader.entries(root)
            if found.usable and any(fnmatch.fnmatchcase(kind, pattern) for pattern in found.manifest.events)]


def emit(root, kind, agent=None, launch=None, data=None):
    """Record one event in `root`'s journal when a plugin there subscribes to it. Never raises.

    `agent` is {"id", "name"} (or an agent directory's properties). Returns the
    event, or None when nobody subscribes or it could not be written.
    """
    try:
        if root is None or not subscribers(root, kind):
            return None
        agent_ref = None
        if agent:
            agent_ref = {"id": agent.get("id"), "name": agent.get("name")}
        data = dict(data or {})
        if kind in CORE_EVENTS and isinstance(data.get("reason"), str):
            data["reason"] = short_reason(data["reason"])
        event = {"schema": 1, "seq": 0, "at": now(), "type": kind, "agent": agent_ref, "launch": launch,
                 "data": data}
        with _bounded_lock(directory(root) / ".lock") as held:
            if not held:
                _log(root, f"{kind}: dropped: the journal was busy")
                return None
            return _append(root, event)
    except Exception as error:
        _log(root, f"{kind}: {type(error).__name__}: {error}")
        return None


def short_reason(text, limit=REASON_CHARACTERS):
    """A core event's reason: its first line, at most `limit` characters. A launch's reason can end with the
    terminal's last lines, which may hold a prompt or a message: those never reach the journal."""
    line = next((part.strip() for part in str(text).splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[:limit - 1] + "…"


@contextmanager
def _bounded_lock(path, seconds=None):
    """Yield True holding `path`'s lock, or False when it stays busy for `seconds` (LOCK_SECONDS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + (LOCK_SECONDS if seconds is None else seconds)
    with path.open("a") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.01)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _append(root, event):
    base = directory(root)
    counter = base / "seq"
    found = segments(root)
    try:
        sequence = int(counter.read_text().strip() or "1")
    except (OSError, ValueError):
        sequence = 1
    if found:
        # A crash after writing an event but before counting it: never reuse its number.
        sequence = max(sequence, _last_sequence(found[-1][1]) + 1, found[-1][0])
    event["seq"] = sequence
    line = _line(event)
    current = found[-1][1] if found else None
    rolled = current is None or _full(current)
    if rolled:
        current = base / f"{sequence:0{NAME_WIDTH}d}.jsonl"
    with current.open("ab+") as handle:
        if handle.tell():
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                handle.write(b"\n")  # seal a torn last line; readers skip it
        handle.write(line)
    temporary = base / ".seq.tmp"
    temporary.write_text(f"{sequence + 1}\n")
    temporary.replace(counter)
    if rolled and found:
        prune(root, locked=True)  # the segment just closed counts now
    return event


def _line(event):
    line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(line) > EVENT_BYTES:
        event = {**event, "data": {"truncated": True, "bytes": len(line)}}
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(line) > EVENT_BYTES:
        raise ValueError(f"event is {len(line)} bytes even without its data")
    return line


def _full(path):
    """Whether a segment is done: 1 MiB, or written on an earlier UTC day."""
    try:
        status = path.stat()
    except OSError:
        return True
    written = datetime.fromtimestamp(status.st_mtime, timezone.utc).date()
    return status.st_size >= SEGMENT_BYTES or written != datetime.now(timezone.utc).date()


def _last_sequence(path):
    """The last complete event's sequence number in a segment (its tail only), else one before its first."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TAIL_BYTES))
            lines = handle.read().splitlines()
        for line in reversed(lines):
            try:
                return int(json.loads(line)["seq"])
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return int(path.stem) - 1


def retention(root):
    """(retain_days, max_mb) from plugins.json `events`, else the defaults."""
    settings = config.load(root).events
    return settings.get("retain_days", DEFAULT_RETAIN_DAYS), settings.get("max_mb", DEFAULT_MAX_MB)


def prune(root, locked=False):
    """Delete closed segments past retention; returns the paths deleted. Never touches the current segment."""
    if not locked:
        with lock(directory(root) / ".lock"):
            return prune(root, locked=True)
    days, megabytes = retention(root)
    closed = segments(root)[:-1]
    removed = []
    cutoff = time.time() - days * 86400
    for _, path in list(closed):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    remaining = [(first, path) for first, path in closed if path not in removed]
    sizes = {path: (path.stat().st_size if path.exists() else 0) for _, path in remaining}
    total = sum(sizes.values())
    for _, path in remaining:
        if total <= megabytes * 1024 * 1024:
            break
        try:
            path.unlink()
            removed.append(path)
            total -= sizes[path]
        except OSError:
            continue
    return removed


def read(root, after=0, limit=500):
    """(events with seq > after, oldest first, at most `limit`; a gap notice or None).

    The gap is {"missed": n, "from": seq, "to": seq} when events after `after`
    were pruned before they could be read; reading continues from the oldest
    kept event. It takes no lock: a segment pruned while it is read is read
    again from the new oldest, so a gap is never silent.
    """
    for _ in range(5):
        try:
            return _read(root, after, limit)
        except FileNotFoundError:
            continue
    return [], None


def _read(root, after, limit):
    found = segments(root)
    if not found:
        return [], None
    gap = None
    oldest = found[0][0]
    if after + 1 < oldest:
        gap = {"missed": oldest - after - 1, "from": after + 1, "to": oldest - 1}
    events = []
    for index, (first, path) in enumerate(found):
        following = found[index + 1][0] if index + 1 < len(found) else None
        if following is not None and following <= after + 1:
            continue
        with path.open("rb") as handle:  # FileNotFoundError: pruned meanwhile, read() starts over
            data = handle.read()
        for line in data.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a line being written
            if isinstance(event, dict) and isinstance(event.get("seq"), int) and event["seq"] > after:
                events.append(event)
                if len(events) >= limit:
                    return events, gap
    return events, gap


def summary(root):
    """For `mv2 plugins events`: size, segments, oldest and newest events, and each plugin's cursor here."""
    found = segments(root)
    size = sum(path.stat().st_size for _, path in found if path.exists())
    events, _ = read(root, 0, limit=1)
    newest = None
    if found:
        last = _last_sequence(found[-1][1])
        newest = last if last >= found[-1][0] else None
    cursors = {}
    for path in sorted((Path(root) / "state" / "plugins").glob("*/events.cursor")):
        try:
            cursors[path.parent.name] = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
    days, megabytes = retention(root)
    return {"universe": str(root), "segments": len(found), "bytes": size,
            "oldest": events[0] if events else None, "newest_seq": newest, "cursors": cursors,
            "retain_days": days, "max_mb": megabytes}


def _log(root, text):
    try:
        path = Path(root) / "state" / "plugins" / "events.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(f"{now()} {text}\n")
    except (OSError, TypeError):
        pass


def agent_of(agent_dir):
    """{"id", "name"} of an agent directory, or None."""
    try:
        properties = json.loads((Path(agent_dir) / "properties.json").read_text())
        return {"id": properties.get("id"), "name": properties.get("name")}
    except (OSError, ValueError, AttributeError):
        return None


def for_agent(agent_dir, kind, launch=None, data=None, properties=None):
    """Emit an agent's event in its own universe's journal; the one call core's emitters make. Never raises."""
    try:
        root = root_of(agent_dir)
        if root is None:
            return None
        agent = ({"id": properties.get("id"), "name": properties.get("name")} if isinstance(properties, dict)
                 else agent_of(agent_dir))
        return emit(root, kind, agent, launch, data)
    except Exception:
        return None


def root_of(agent_dir):
    """The universe an agent directory belongs to (state/agents/<id>), or None."""
    directory_ = Path(agent_dir)
    root = directory_.parent.parent.parent
    return root if directory_.parent.name == "agents" and (root / "universe.json").is_file() else None

