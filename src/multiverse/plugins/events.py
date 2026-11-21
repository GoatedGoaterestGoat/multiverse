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
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from ..store import lock, now, open_regular, retry_missing, is_file_replaced
from . import config, loader
from .manifest import CORE_EVENTS

DIRECTORY = "events"
SEGMENT_BYTES = 1024 * 1024
EVENT_BYTES = 16 * 1024
REASON_CHARACTERS = 200  # a core event's reason, cut to its first line
LOCK_SECONDS = 0.2  # the longest emit waits for the journal; past it, the event goes to a spill file
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


def _read_journal_text(path):
    with open_regular(path) as stream:
        return stream.read().decode("utf-8")


def journal_id(root):
    """The id of `root`'s journal, made when it is first written; None while there is none (or before mv2 made
    them: such a journal gets one at its next event)."""
    for name in (ID, ID_COPY):
        try:
            path = directory(root) / name
            value = retry_missing(lambda: _read_journal_text(path), path=path).strip()
        except (OSError, UnicodeError):
            continue
        if _ID_FORMAT.fullmatch(value):
            return value
    return None


_ID_FORMAT = re.compile(r"[0-9a-f]{32}")


def _counter(base):
    """The next sequence number, from the counter file `seq`; None when it is not there or cannot be read."""
    try:
        path = Path(base) / "seq"
        parts = retry_missing(lambda: _read_journal_text(path), path=path).split()
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


def subscribers(root, kind, admit=False):
    """Names of the enabled plugins subscribed to any of the given event kinds: the authority, from the types of
    what runs (a pin made before types were kept with it counts for nothing until they're proved). With `admit`,
    for journal admission (admits()), such a plugin counts too, conservatively.

    From manifests alone, so recording costs a command no trust check and no import: a plugin that is on but cannot
    run yet (not trusted, say) has its events recorded too, for when it can; the host verifies trust before it
    delivers anything."""
    if loader.safe_mode():
        return []
    from . import config, registry, trust

    kinds = (kind,) if isinstance(kind, str) else tuple(kind)

    def matches(patterns):
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns for name in kinds)

    found = []

    def pins():  # the paths trusted-plugins.json pins: read once, the first time it's needed, never waiting for
        if not found:  # anything (trust.pins_now), None when they can't be read for sure
            found.append(trust.pins_now())
        return found[0]

    # A plugin's event types are those of what runs. A folder the universe records runs at its pinned copy in every
    # process: that copy's types, else the ones recorded for it, also where this process finds another copy of that
    # name. A copy this process finds and runs verified (a universe plugin, one the installer placed, a plugin found
    # only here) has its approved bytes' types, kept with its pin; one never approved, its manifest's; one whose
    # approval can't be read, none. A pin made before types were kept with it is admitted conservatively (its
    # manifest's and its record's types; nothing hashed here) until the person's listing proves and keeps its types.
    # Any other plugin this process finds is found alike by every process, so its manifest as it is now decides: a
    # type it starts or stops following counts at once, with no listing. The record decides for a plugin this
    # process can't find only when another process found it where this one can't look (an agent's own find); a
    # plugin of a shared source that is gone is gone.
    recorded = (registry.known(root) or {}) if root is not None else {}
    pinned = {name for name, value in recorded.items() if value.get("directory") is not None}
    live, failed = {}, False
    try:
        for source, directory, manifest in loader.manifest_sources(root):
            if manifest.name in pinned:
                continue
            default = manifest.default if source == "bundled" else "off"
            patterns = manifest.events
            if source in ("universe", "personal", "recorded") or not registry.shared(source, directory):
                state, approved = registry.approved_events(directory, pins())
                if state == "approved":
                    patterns = approved
                elif state == "unknown":  # its approval can't be read: no authority; admitted conservatively
                    patterns = [*manifest.events, *recorded.get(manifest.name, {}).get("events", [])] if admit else []
                elif state == "legacy":
                    # A pin made before types were kept with it: nothing proves its types until the person's
                    # listing keeps them (nothing is hashed here), so it subscribes to none; for journal admission
                    # alone (admits), conservatively its manifest's and its record's.
                    patterns = [*manifest.events, *recorded.get(manifest.name, {}).get("events", [])] if admit else []
            live.setdefault(manifest.name, []).append((default, list(patterns)))
    except (OSError, RuntimeError):
        live, failed = {}, True  # discovery failed: the record alone
    possible = set()
    for name, value in recorded.items():
        if name in pinned:
            state, approved = registry.approved_events(value["directory"], pins())
            if state == "approved":
                patterns = approved
            elif admit:  # not approved (older pin, no pin, pins unreadable): no authority, admitted by its record
                patterns = [*value["events"], *(_manifest_events(value["directory"]) if state == "legacy" else [])]
            else:
                patterns = []
        elif failed or name not in live and value.get("private"):
            patterns = value["events"]
        else:
            continue
        if matches(patterns):
            possible.add(name)
    visible = {(name, default) for name, copies in live.items() for default, patterns in copies if matches(patterns)}
    if not possible and not visible:
        return []  # No switch value can enable an applicable subscription.
    settings = config.load(root, observed=True) if root is not None else config.Config()
    return sorted({name for name in possible if settings.is_on(name)}
                  | {name for name, default in visible if settings.is_on(name, default)})


SPILL = "spill"  # state/events/spill/: events a process couldn't journal at once, merged in by the next writer
MERGE_BYTES = 2 * 1024 * 1024  # the most the plugin host's poll merges at once; the rest waits for the next
EMIT_MERGE_BYTES = 64 * 1024  # the most a recorder merges before its own event: recording stays quick
CLAIM = ".merging-"  # a spill file a merge took, renamed aside with an id of its own
_spill_name = None  # (pid, file name, stamp): this process's own spill file


def _manifest_events(directory):
    """The event types in `directory`'s own plugin.json, read without waiting (trust.read_regular), or none."""
    from . import manifest as manifests, trust
    try:
        return list(manifests.load_bytes(directory, trust.read_regular(Path(directory) / manifests.FILE,
                                                                        raw=True)).events)
    except (OSError, ValueError):
        return []


def admits(root, kind):
    """Whether an event of `kind` (or any of several) is to be journaled: a subscriber, or, conservatively, a plugin
    that is on whose pin was made before types were kept with it and whose manifest or record could follow it
    (nothing proves which until the person's listing keeps them, and a lookup never hashes). More is journaled,
    never lost; the host delivers only what the verified plugin follows."""
    return bool(subscribers(root, kind, True))


def _spill_path(root, fresh=False):
    """This process's own spill file: named by when it was first needed, then its pid, so names sort by time.
    `fresh`: a new one, whose name sorts after every earlier file of this process."""
    global _spill_name
    if fresh or _spill_name is None or _spill_name[0] != os.getpid():  # a forked child gets a file of its own
        stamp = time.time_ns()
        if _spill_name is not None and _spill_name[0] == os.getpid():
            stamp = max(stamp, _spill_name[2] + 1)  # after this process's last file, whatever the clock did
        _spill_name = (os.getpid(), f"{stamp:020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl", stamp)
    return directory(root) / SPILL / _spill_name[1]


def _spill_folder(root, create=False):
    """(descriptor, state) of the spill folder: state "ok" with a descriptor of a real folder (never a link);
    "absent" when there is none; "unsafe" when it's there but isn't a folder, or can't be opened. Only "absent"
    proves there is no backlog."""
    path = directory(root) / SPILL
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unsafe"
    if not stat.S_ISDIR(status.st_mode):
        return None, "unsafe"
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW), "ok"
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unsafe"


def _write_all(descriptor, data):
    while data:
        written = os.write(descriptor, data)
        if written <= 0:
            raise OSError("spill write made no progress")
        data = data[written:]


def _spill(root, event):
    """Append `event` to this process's spill file; returns whether it did. It takes that file's lock only for the
    one line and never waits for it: if the file is held, the event goes to a new file of this process, whose name
    sorts after it. If a merger took the file meanwhile (the name now holds another file, or none), it writes a new
    one under the name. A torn last line (a killed writer) is sealed first, so this line stays whole. Each line
    carries a replay key of its own, so a merge cut off right after appending it never appends it twice. Bounded: a
    few tries, never a wait on a lock; files are opened in the verified spill folder, never through a link."""
    # "at" first, so a merge learns when a row was recorded from its first bytes, before reading it whole.
    line = _line({**({"at": event["at"]} if "at" in event else {}), **event, REPLAY: "spill/" + uuid.uuid4().hex})
    folder, _ = _spill_folder(root, create=True)
    if folder is None:
        return False
    try:
        name = _spill_path(root).name
        for _ in range(8):
            try:
                descriptor = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     0o600, dir_fd=folder)
            except OSError:
                name = _spill_path(root, fresh=True).name
                continue
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    name = _spill_path(root, fresh=True).name
                    continue
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    name = _spill_path(root, fresh=True).name  # held: a new file of this process, after it
                    continue
                try:
                    same = status.st_ino == os.stat(name, dir_fd=folder, follow_symlinks=False).st_ino
                except FileNotFoundError:
                    same = False
                if not same:
                    continue  # a merger took it: O_CREAT makes a new one under the name
                size = os.fstat(descriptor).st_size
                if size and os.pread(descriptor, 1, size - 1) != b"\n":
                    _write_all(descriptor, b"\n")  # seal a torn line, so it never swallows this one
                _write_all(descriptor, line)
                return True
            finally:
                os.close(descriptor)  # closing releases the lock
        return False
    finally:
        os.close(folder)


DAMAGED = object()  # progress that is there but isn't progress (malformed): recovered from the journal itself


def _progress(folder, name):
    """(offset, pending replay key or None, recovering, when) of a claimed spill file; (0, None, False, None) only
    when there is none yet, DAMAGED when it's malformed. An I/O error reading it raises: merging that file waits, and
    never starts it over. `recovering`: its rows are being reconciled with the journal (it was damaged once), until
    it's done. `when`: when the row at `offset` was recorded, once a merge learned it (None until then, or when what
    was saved isn't a time: it's learned again)."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except FileNotFoundError:
        return 0, None, False, None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return DAMAGED
        data = os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        return DAMAGED
    if not isinstance(value, dict):
        return DAMAGED
    offset, pending, recovering = value.get("offset"), value.get("pending"), value.get("recover", False)
    if (type(offset) is not int or offset < 0 or not (pending is None or isinstance(pending, str))
            or type(recovering) is not bool):
        return DAMAGED
    when = value.get("when")
    try:
        usable = type(when) in (int, float) and math.isfinite(when)
    except OverflowError:  # an integer too large for a time: learned again, the rest of the progress kept
        usable = False
    if not usable:
        when = None
    return offset, pending, recovering, when


def _save_progress(folder, name, offset, pending, recovering=False, when=None):
    """Write a claimed file's progress in place of `name`, through a temporary file made only for this write."""
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    try:
        record = {"offset": offset, "pending": pending, **({"recover": True} if recovering else {}),
                  **({"when": when} if when is not None else {})}
        _write_all(descriptor, (json.dumps(record) + "\n").encode())
    finally:
        os.close(descriptor)
    os.replace(temporary, name, src_dir_fd=folder, dst_dir_fd=folder)


def _claim(folder, name, names=None):
    """Rename waiting spill file `name` aside under its own lock (taken without waiting), with an id of its own so
    no progress left by an earlier claim applies to it; the claimed name, "" when there is nothing to claim, or
    None when a spiller holds it (it may be mid-row) or it can't be read now. A claim of a name claimed before sorts
    after every claim of it still waiting, whatever the clock did, so one process's claims of a reused name keep
    their order. `names`: the folder's names, when the caller has listed them."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.unlink(name, dir_fd=folder)  # not a spill file: nothing to merge from it
            return ""
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # never mid-line of a spiller
        except BlockingIOError:
            return None
        prefix, last = f"{CLAIM}{name[:-len('.jsonl')]}-", 0
        for other in os.listdir(folder) if names is None else names:
            if other.startswith(prefix) and other.endswith(".jsonl"):
                number = other[len(prefix):].split("-")[0]
                if number.isdigit():
                    last = max(last, int(number))
        claimed = f"{prefix}{max(time.time_ns(), last + 1):020d}-{uuid.uuid4().hex[:8]}.jsonl"
        try:
            os.rename(name, claimed, src_dir_fd=folder, dst_dir_fd=folder)
        except OSError:
            return None
        return claimed
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _producer(name):
    """(stamp, process) a spill file's name gives: one process's files follow each other by stamp."""
    parts = (name[len(CLAIM):] if name.startswith(CLAIM) else name)[:-len(".jsonl")].split("-")
    try:
        return int(parts[0]), parts[1] if len(parts) > 1 else name
    except ValueError:
        return 0, name


def _order(name):
    return _producer(name)[0], name


class _Budget:
    """What a merge may still read and append, in bytes: lookahead counts as well as rows appended."""

    def __init__(self, total):
        self.left = total


PREFIX = 128  # enough of a spilled row's first bytes to hold its "at"
_AT = re.compile(rb'\{"at":"([^"\\]{1,64})"')


def _when(data):
    """When a row was recorded, from its first bytes ("at" first, as spilled rows are); None when they don't say."""
    match = _AT.match(data)
    if match is None:
        return None
    try:
        return datetime.fromisoformat(match.group(1).decode()).timestamp()
    except (ValueError, UnicodeError):
        return None


def _event_when(event):
    try:
        return datetime.fromisoformat(event.get("at")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


class _Claimed:
    """One claimed spill file, read from its saved progress. No descriptor is kept between reads, so however many
    files a merge has claimed, it holds none open."""

    def __init__(self, folder, name, budget):
        self.folder, self.name, self.budget = folder, name, budget
        self.stamp, self.process = _producer(name)
        self.progress = name + ".offset"
        found = _progress(folder, self.progress)
        self.offset, self.pending, self.recover, self.known = (0, None, True, None) if found is DAMAGED else found
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"{name} is not a spill file")
        finally:
            os.close(descriptor)
        self.buffer, self.read_to, self.head, self.ended = b"", self.offset, None, False

    def _read(self, want):
        """Read up to `want` more bytes, never past the budget: True, False at the file's end, None when spent."""
        size = min(want, self.budget.left)
        if size <= 0:
            return None
        descriptor = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.folder)
        try:
            chunk = os.pread(descriptor, size, self.read_to)
        finally:
            os.close(descriptor)
        self.budget.left -= len(chunk)
        if not chunk:
            return False
        self.buffer += chunk
        self.read_to += len(chunk)
        return True

    def _whole(self):
        """Read the head row through its newline: True, False when the file ends first (torn for good: no spiller
        writes here), None when the budget is spent."""
        while b"\n" not in self.buffer:
            got = self._read(4096)
            if not got:
                return got
        return True

    def peek(self):
        """When the next row was recorded: from its progress when a merge learned it before, else read no further
        than it takes to tell (a spilled row's first bytes), and kept in its progress for later merges. None at its
        end or when the budget is spent (`ended` tells them apart). Rows that aren't events (a torn line) are passed
        over."""
        if self.head is None and self.known is not None and not self.buffer:
            self.head = self.known
            return self.head
        while self.head is None:
            if b"\n" not in self.buffer and len(self.buffer) < PREFIX:
                got = self._read(PREFIX - len(self.buffer))
                if got is None:
                    return None
                if got is False:
                    self.ended = True  # its end; a last line with no newline is torn for good
                    return None
                continue
            when = _when(self.buffer)
            if when is None:  # not a spilled row's start: the whole row tells
                got = self._whole()
                if got is None:
                    return None
                if got is False:
                    self.ended = True
                    return None
                event = self._event()
                if event is None:
                    self.consume()
                    self.save(self.offset, self.pending)  # passed for good: no later merge reads it again
                    continue
                when = _event_when(event)
            self.head = self.known = when
            self.save(self.offset, self.pending)
        return self.head

    def _event(self):
        try:
            event = json.loads(self.buffer[:self.buffer.index(b"\n") + 1])
        except (ValueError, UnicodeError):
            return None
        return event if isinstance(event, dict) else None

    def row(self):
        """The head row as (event, line bytes), read whole but not yet passed: None when the budget is spent first,
        False when it isn't an event (it's passed over)."""
        got = self._whole()
        if got is None:
            return None
        if got is False:
            self.head, self.ended = None, True
            return False
        event = self._event()
        if event is None:
            self.consume()
            self.save(self.offset, self.pending)  # passed for good: no later merge reads it again
            return False
        return event, self.buffer[:self.buffer.index(b"\n") + 1]

    def consume(self):
        """Pass the head row (it's in the journal now, or isn't an event)."""
        end = self.buffer.index(b"\n") + 1
        self.buffer, self.offset, self.head, self.known = self.buffer[end:], self.offset + end, None, None

    def save(self, offset, pending=None):
        _save_progress(self.folder, self.progress, offset, pending, self.recover,
                       self.known if offset == self.offset else None)

    def close(self):
        pass  # nothing held: each read opens and closes the file


def _journal_keys(root):
    """{replay key: seq} of every row in the journal that has one: how a claim whose progress was damaged tells which
    of its rows are in, and makes their numbers readable. A segment that can't be read raises: no proof either way."""
    keys = {}
    for _, path in segments(root):
        data = path.read_bytes()
        for line in data.splitlines():
            if REPLAY.encode() not in line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict) and isinstance(event.get(REPLAY), str) and type(event.get("seq")) is int:
                keys[event[REPLAY]] = event["seq"]
    return keys


def _merge_spills(root, budget=MERGE_BYTES, single=False):
    """Append spilled events to the journal under its lock (the caller holds it), and return whether every spilled
    event is in the journal now; when not, newer events must wait behind them.

    Every waiting file is claimed first, in the order it was made (renamed aside under its own lock). One a spiller
    holds (it may be mid-row), or that can't be read now, holds everything back: nothing is appended before it's
    claimed. One process's files are merged in succession, oldest first; between processes, the earliest recorded
    next row goes first, and nothing is appended until every process's next row is known. When each file's next row
    was recorded is read from its first bytes and kept in its progress, so a later merge goes on from what an
    earlier one learned. Before each row is appended its offset and replay key are saved, and right after, the
    offset past it, so at most one row is ever in doubt. A file whose progress was damaged is reconciled with the
    journal's replay keys until it's done. `budget` counts every byte read or appended, the append reserved before
    it's made; no file is held open between reads. `single` (a recorder): merge only one spill file, never one with
    a row in doubt or damaged progress, and leave the rest to the plugin host's poll."""
    folder, state = _spill_folder(root)
    if folder is None:
        return state == "absent"  # an unsafe or unreadable folder never proves the backlog empty
    try:
        names = sorted(os.listdir(folder))
        claimed = [name for name in names if name.startswith(CLAIM) and name.endswith(".jsonl")]
        waiting = sorted((name for name in names if not name.startswith(".") and name.endswith(".jsonl")),
                         key=_order)
        if not claimed and not waiting:
            return True
        if single and len(claimed) + len(waiting) > 1:
            return False
        for name in names:  # progress whose claim is gone: left by a cut-off cleanup
            if name.startswith(CLAIM) and name.endswith(".offset") and name[:-len(".offset")] not in claimed:
                os.unlink(name, dir_fd=folder)
        queues = {}  # process -> its claimed files, oldest first
        for name in sorted(claimed, key=_order):
            queues.setdefault(_producer(name)[1], []).append(name)
        held = False
        for name in waiting:
            taken = _claim(folder, name, names)
            if taken is None:
                held = True  # it may hold an older row than any known: everything waits behind it
                break
            if taken:
                names.append(taken)
                queues.setdefault(_producer(taken)[1], []).append(taken)
        for queue in queues.values():  # claimed now and before, in the order each process made them
            queue.sort(key=_order)
        if held and (single or not queues):
            return False
        spend, heads, known, base = _Budget(budget), {}, None, directory(root)
        if single and queues:
            [[name]] = queues.values()
            reader = _Claimed(folder, name, spend)
            reader.close()
            if reader.recover or reader.pending is not None:
                return False  # a row in doubt, or damaged progress, is proved from the journal: the host's work

        def finish(process, name=None):
            """The process's file (its oldest, unless named) is merged to its end: remove it and its progress."""
            name = queues[process].pop(0) if name is None else queues[process].pop(queues[process].index(name))
            os.unlink(name, dir_fd=folder)
            try:
                os.unlink(name + ".offset", dir_fd=folder)
            except FileNotFoundError:
                pass
            if not queues[process]:
                del queues[process]

        def learn(process):
            """When the process's next row was recorded, into `heads`; False when the budget ran out first."""
            heads.pop(process, None)
            while process in queues:
                reader = _Claimed(folder, queues[process][0], spend)
                try:
                    when = reader.peek()
                finally:
                    reader.close()
                if when is not None:
                    heads[process] = (when, reader.stamp)
                    return True
                if not reader.ended:
                    return False
                finish(process)
            return True
        unknown = False
        # A row a cut-off merge may already have appended is settled first, in every file, while the journal's tail
        # still proves it: appending anything before could push it out of that tail and append it twice.
        for process, name in [(process, name) for process, queue in queues.items() for name in queue]:
            reader = _Claimed(folder, name, spend)
            try:
                if reader.pending is None:
                    continue
                got = reader.row()
                if got is None:
                    unknown = True  # the budget ran out: nothing is appended before it's settled
                    break
                if got is not False:
                    sequence = _replayed(root, reader.pending) if got[0].get(REPLAY) == reader.pending else None
                    reader.pending = None
                    if sequence is not None:  # in the journal: once, and readable
                        reader.consume()
                        if (_counter(base) or 1) <= sequence:
                            _write_counter(base, sequence + 1)
                    reader.save(reader.offset)  # not in: it goes in its turn, as any other row
            finally:
                reader.close()
            if reader.ended:
                finish(process, name)
        unknown = unknown or not all([learn(process) for process in list(queues)])  # learned, kept for the next merge
        while not held and not unknown and heads:
            process = min(heads, key=heads.get)
            reader = _Claimed(folder, queues[process][0], spend)
            try:
                if reader.peek() is None:
                    got = None if not reader.ended else False
                else:
                    got = reader.row()
                if got is None:
                    break  # the budget ran out: it waits
                if got is not False:
                    event, line = got
                    key = event.get(REPLAY)
                    sequence = None
                    if reader.pending is not None and key == reader.pending:
                        sequence = _replayed(root, key)  # appended just before a merge was cut off?
                    elif reader.recover and isinstance(key, str):
                        if known is None:
                            known = _journal_keys(root)
                        sequence = known.get(key)  # its progress was damaged: the journal itself says so
                    if sequence is not None:  # already in the journal: once, and readable
                        reader.consume()
                        if (_counter(base) or 1) <= sequence:
                            _write_counter(base, sequence + 1)
                        reader.save(reader.offset)
                    else:
                        if spend.left < len(line):
                            break  # no room to append it: it waits, the budget's limit kept
                        spend.left -= len(line)
                        if isinstance(key, str):
                            reader.save(reader.offset, key)
                        _append(root, {**event, "seq": 0})
                        reader.consume()
                        reader.save(reader.offset)  # committed: no row of this file in doubt
            finally:
                reader.close()
            if reader.ended:
                finish(process)
            if not learn(process):
                break
        return not held and not queues
    finally:
        os.close(folder)


def merge(root):
    """Merge spilled events into the journal now, if its lock is free within a moment (the plugin host's poll).
    Never raises."""
    try:
        try:
            waiting = any(name.endswith(".jsonl") for name in os.listdir(directory(root) / SPILL))
        except OSError:
            return
        if not waiting:  # nothing spilled: never takes the journal's lock, so a poll costs nothing
            return
        with _bounded_lock(directory(root) / ".lock") as held:
            if held:
                _merge_spills(root)
    except Exception as error:
        _log(root, f"merging spilled events: {type(error).__name__}: {error}")


def emit(root, kind, agent=None, launch=None, data=None):
    """Record one event in `root`'s journal when a plugin there subscribes to it. Never raises, and never waits
    more than a moment: when the journal stays busy, the event goes to this process's spill file, which the next
    writer to hold the journal merges in first, so nothing is dropped and each process's events keep their order.

    `agent` is {"id", "name"} (or an agent directory's properties). Returns the
    event when it was journaled now, or None (nobody subscribes, spilled, or it couldn't be written).
    """
    try:
        if root is None or not admits(root, kind):
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
            # Spilled events first, this process's own earlier ones among them: in order. While some are still
            # waiting (a merge goes a bit at a time, or failed), this one waits behind them, in a spill file.
            if held:
                try:
                    drained = _merge_spills(root, EMIT_MERGE_BYTES, single=True)
                except Exception as error:
                    _log(root, f"merging spilled events: {type(error).__name__}: {error}")
                    drained = False
                if drained:
                    return _append(root, event)
        if not _spill(root, event):
            _log(root, f"{kind}: dropped: the journal and its spill file were busy")
        return None
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
    with open_regular(path, "a") as handle:
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


def prompt_event(agent_dir, kind, launch, data, *, key, properties=None, at=None):
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
        if root is None or not admits(root, kind):
            return "unsubscribed"
        if not isinstance(key, str) or not key:
            raise ValueError("a prompt event needs core's replay key")
        agent = ({"id": properties.get("id"), "name": properties.get("name")} if isinstance(properties, dict)
                 else agent_of(agent_dir))
        event = {"schema": 1, "seq": 0, "at": at if isinstance(at, str) else now(),
                 "type": kind, "agent": agent, "launch": launch,
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
                if not _merge_spills(root, EMIT_MERGE_BYTES, single=True):
                    return "retry"  # older spilled events go first; a merge takes them a bit at a time
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
        text = _read_journal_text(path)
    except FileNotFoundError:
        if not (directory(root) / RECEIPTS_WRITTEN).exists():
            return {}
        try:
            text = retry_missing(lambda: _read_journal_text(path), path=path)
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


def _replace_journal_text(path, text):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_receipts(root, receipts):
    base = directory(root)
    marker = base / RECEIPTS_WRITTEN
    if not marker.is_file():
        _replace_journal_text(marker, "")  # from now on a missing receipts file is a gap
    _replace_journal_text(base / RECEIPTS, json.dumps(receipts, separators=(",", ":")) + "\n")


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
        with open_regular(path) as handle:
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
            path = base / name
            stored = retry_missing(lambda: _read_journal_text(path), path=path).strip()
        except (OSError, UnicodeError):
            stored = ""
        if stored != known:
            _replace_journal_text(base / name, known + "\n")
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
    with open_regular(current, "ab+") as handle:
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
    _replace_journal_text(base / "seq", f"{next_sequence}\n")


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
        with open_regular(path) as handle:
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
        with open_regular(path) as handle:  # FileNotFoundError: pruned meanwhile, read() starts over
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
        loader.append_log(Path(root) / "state" / "plugins" / "events.log", f"{now()} {text}", blocking=False)
    except (OSError, TypeError):
        pass


def agent_of(agent_dir):
    """{"id", "name"} of an agent directory, or None."""
    from ..universe import records

    try:
        properties = records._object(Path(agent_dir) / "properties.json")
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

