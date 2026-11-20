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
import re
import time
import uuid

from ..store import lock, now, retry_missing, is_file_replaced
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


ID = "journal-id"  # made with a journal, gone when its state is removed: a reader tells a journal that started again


ID_COPY = "journal-id.copy"  # a second copy, so a journal-id that was lost or cannot be read is not taken for a new
# journal. Files of their own: the counter keeps the format every mv2 since the journal began reads (a plugin host
# started before an update runs the old code until it stops).


def journal_id(root):
    """The id of `root`'s journal, made when it is first written; None while there is none (or before mv2 made
    them: such a journal gets one at its next event)."""
    for name in (ID, ID_COPY):
        try:
            value = retry_missing((directory(root) / name).read_text).strip()
        except OSError:
            continue
        if _ID_FORMAT.fullmatch(value):
            return value
    return None


_ID_FORMAT = re.compile(r"[0-9a-f]{32}")


def _counter(base):
    """The next sequence number, from the counter file `seq`; None when it is not there or cannot be read."""
    try:
        parts = retry_missing((Path(base) / "seq").read_text).split()
        return int(parts[0]) if parts and parts[0].isdigit() else None
    except (OSError, ValueError):
        return None


def segments(root):
    """Closed and current segments, oldest first: [(first sequence, path)]."""
    found = []
    for path in directory(root).glob("*.jsonl"):
        if path.stem.isdigit():
            found.append((int(path.stem), path))
    return sorted(found)


def subscribers(root, kind):
    """Names of the plugins on here whose manifest subscribes to events of `kind`.

    From manifests alone (loader.declared), so recording costs a command no
    trust check: a plugin that is on but cannot run yet (not trusted, say) has
    its events recorded too, for when it can; the host verifies trust before it
    delivers anything."""
    if loader.safe_mode():
        return []
    if not any(fnmatch.fnmatchcase(kind, pattern)
               for manifest in loader.manifest_records(root) for pattern in manifest.events):
        return []  # With no subscribing manifest, plugins.json cannot change the answer.
    return sorted({found.name for found in loader.declared(root)
                   if found.on and any(fnmatch.fnmatchcase(kind, pattern) for pattern in found.manifest.events)})


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


REPLAY = "_replay"  # core's replay key on a prompt event: kept in the journal, never shown to a plugin
RECEIPTS = "replay.json"  # {replay key: seq} of prompt events written and not yet acknowledged by core
RECEIPT_LIMIT = 4096  # the oldest receipts go past this many: core acknowledges long before
RECEIPTS_WRITTEN = ".replay.written"  # exists once receipts were written: a missing file then is a replacement gap


def prompt_event(agent_dir, kind, launch, data, *, key, properties=None):
    """Record one of an agent's prompt events (prompt.opened, prompt.closed, agent.prompt) for core's prompt
    ledger, saying what became of it. Never raises.

    'written': it is in the journal, by this call or an earlier one with the same `key` (an append whose
    acknowledgement core lost writes nothing twice). 'unsubscribed': no plugin on in that universe subscribes to
    `kind`, so there is nothing to write. 'retry': the journal was busy, or couldn't be written (logged): keep it
    and record it again later, in order. `key` is core's own replay key, unique per pending event; it is kept in
    the journal line and stripped from every read, so a plugin never sees it, and a private receipt keeps it until
    core acknowledges it (acknowledge), while the bounded receipt is retained (RECEIPT_LIMIT). A receipt that
    couldn't be saved is covered by the journal's tail. A crash between the append and its receipt can still write
    it twice, which delivery at least once allows."""
    root = None
    try:
        root = root_of(agent_dir)
        if root is None or not subscribers(root, kind):
            return "unsubscribed"
        if not isinstance(key, str) or not key:
            raise ValueError("a prompt event needs core's replay key")
        agent = ({"id": properties.get("id"), "name": properties.get("name")} if isinstance(properties, dict)
                 else agent_of(agent_dir))
        event = {"schema": 1, "seq": 0, "at": now(), "type": kind, "agent": agent, "launch": launch,
                 "data": dict(data or {}), REPLAY: key}
        base = directory(root)
        with _bounded_lock(base / ".lock") as held:
            if not held:
                _log(root, f"{kind}: kept for later: the journal was busy")
                return "retry"
            receipts = _receipts(root)
            # A miss in the receipts is checked against the journal's tail too: an append whose receipt couldn't
            # be saved, or receipts that couldn't be read.
            if key in receipts:
                return "written"
            sequence = _replayed(root, key)
            if sequence is None:
                sequence = _append(root, event)["seq"]
            elif (_counter(base) or 1) <= sequence:
                # An interrupted append may have written the event without its
                # counter. Incremental readers must be able to see the row.
                _write_counter(base, sequence + 1)
            if receipts is not _UNREADABLE:
                receipts[key] = sequence
                if len(receipts) > RECEIPT_LIMIT:
                    receipts = dict(sorted(receipts.items(), key=lambda item: item[1])[-RECEIPT_LIMIT:])
                _write_receipts(root, receipts)
            return "written"
    except Exception as error:
        if root is not None:
            _log(root, f"{kind}: kept for later: {type(error).__name__}: {error}")
        return "retry"


class _Unreadable(dict):
    """Receipts that couldn't be read: the journal's tail is looked at instead."""


_UNREADABLE = _Unreadable()


def _receipts(root):
    """The receipts, {} when never written, or _UNREADABLE. Once written (RECEIPTS_WRITTEN exists), a missing
    file is retried through a replacement, so an atomic rewrite never reads as an empty map."""
    path = directory(root) / RECEIPTS
    try:
        text = path.read_text()
    except FileNotFoundError:
        if not (directory(root) / RECEIPTS_WRITTEN).exists():
            return {}
        try:
            text = retry_missing(path.read_text, path=path)
        except (OSError, UnicodeError):
            return _UNREADABLE
    except (OSError, UnicodeError):  # not UTF-8 is as unreadable as damaged: the tail decides
        return _UNREADABLE
    try:
        data = json.loads(text)
    except ValueError:
        return _UNREADABLE
    return {key: seq for key, seq in data.items() if isinstance(key, str) and isinstance(seq, int)} \
        if isinstance(data, dict) else _UNREADABLE


def _write_receipts(root, receipts):
    (directory(root) / RECEIPTS_WRITTEN).touch()  # first: from now on a missing receipts file is a gap
    temporary = directory(root) / f".{RECEIPTS}.tmp"
    temporary.write_text(json.dumps(receipts, separators=(",", ":")) + "\n")
    temporary.replace(directory(root) / RECEIPTS)


def acknowledge(agent_dir, keys):
    """Core acknowledged these prompt events' replay keys: their receipts go. True when done; never raises (an
    acknowledgement that couldn't be made leaves receipts that only cost a little space until RECEIPT_LIMIT)."""
    try:
        root = root_of(agent_dir)
        if root is None or not (directory(root) / RECEIPTS).exists():
            return True
        with _bounded_lock(directory(root) / ".lock") as held:
            if not held:
                return False
            receipts = _receipts(root)
            if receipts is _UNREADABLE:
                return False
            kept = {key: seq for key, seq in receipts.items() if key not in set(keys)}
            if kept != receipts:
                _write_receipts(root, kept)
            return True
    except Exception:
        return False


def _replayed(root, key):
    """Return the sequence for an exact replay key in the bounded tail of the latest two segments."""
    needle = json.dumps(key, ensure_ascii=False).encode()
    for _, path in segments(root)[-2:]:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - TAIL_BYTES)
            handle.seek(max(0, start - 1))
            clipped = start > 0 and handle.read(1) not in (b"\n", b"\r")
            lines = handle.read().splitlines()
        # The preceding byte distinguishes a clipped row from a complete row
        # that starts at the tail boundary. The reader also accepts complete
        # JSON at the end of a segment without a final delimiter.
        for line in lines[1 if clipped else 0:]:
            if needle not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (isinstance(event, dict) and event.get(REPLAY) == key and type(event.get("seq")) is int
                    and event["seq"] > 0):
                return event["seq"]
    return None


def _append(root, event):
    base = directory(root)
    known = journal_id(root) or uuid.uuid4().hex
    for name in (ID, ID_COPY):  # a new journal, or one of its two id files lost: written again from the other
        try:
            stored = retry_missing((base / name).read_text).strip()
        except OSError:
            stored = ""
        if stored != known:
            temporary = base / f".{name}.tmp"
            temporary.write_text(known + "\n")
            temporary.replace(base / name)
    found = segments(root)
    sequence = _counter(base) or 1
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
    _write_counter(base, sequence + 1)
    if rolled and found:
        prune(root, locked=True)  # the segment just closed counts now
    return event


def _write_counter(base, next_sequence):
    temporary = base / ".seq.tmp"
    temporary.write_text(f"{next_sequence}\n")
    temporary.replace(base / "seq")


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
    number = _counter(directory(root))  # nothing written since `after` (a worker polls every half second):
    if number is not None and number <= after + 1:  # no segment is read at all
        return [], gap
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
                event.pop(REPLAY, None)  # core's replay key: never a plugin's
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
            cursors[path.parent.name] = int(retry_missing(path.read_text).strip())
        except (OSError, ValueError):
            continue
    days, megabytes = retention(root)
    return {"universe": str(root), "segments": len(found), "bytes": size,
            "oldest": events[0] if events else None, "newest_seq": newest, "cursors": cursors,
            "retain_days": days, "max_mb": megabytes}


def _log(root, text):
    try:
        loader.append_log(Path(root) / "state" / "plugins" / "events.log", f"{now()} {text}")
    except (OSError, TypeError):
        pass


def agent_of(agent_dir):
    """{"id", "name"} of an agent directory, or None."""
    try:
        properties = json.loads(retry_missing((Path(agent_dir) / "properties.json").read_text))
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
    return root if directory_.parent.name == "agents" and is_file_replaced(root / "universe.json") else None

