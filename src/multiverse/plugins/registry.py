"""What every mv2 process in a universe must know of the plugins that are on there, however this one finds them.

A plugin can be found only in the person's own environment (a folder in their MV2_PLUGIN_PATH, a package of
another Python) while an agent's process records its events and launches its harness. So
state/plugins/registry.json keeps, for each plugin that's on: the event types it subscribes to, the source that
found it, and, when another process may lack that source, the folder it is in.

- Events are recorded from the recorded patterns alone: no plugin is found or imported for it. A recorded
  folder's patterns are those of the copy pinned for it, read again as the record is written.
- A recorded folder is one more place discovery looks (source "recorded"), as a candidate only: it runs at the
  content pinned for it in trusted-plugins.json, as a trusted universe plugin does, never as approval or a grant.
  Only the person's process adds a folder, and only turning the plugin on moves one; an agent's keeps what is there.
  The pin is made when the person turns the plugin on, for a folder nothing pinned before; after an edit,
  `mv2 plugins trust NAME` approves it.
"""
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import stat
import threading

from ..store import lock, write
from .manifest import NAME

FILE = "registry.json"  # in the universe's state/plugins/
READ_LIMIT = 1024 * 1024  # a record larger than this isn't read
# Sources every process of this user and this mv2 finds alike: nothing to record but the patterns.
SHARED = frozenset({"universe", "personal", "checkout", "bundled", "recorded"})
_cache = {}


def path(root):
    return Path(root) / "state" / "plugins" / FILE


def read(root):
    """{name: {"events": [patterns], "source": str or None, "directory": absolute path or None}}; {} when there is
    none or it can't be read. Kept while the file is unchanged."""
    return known(root) or {}


def known(root):
    """As read(), but None when there is no record or it can't be read: then nothing may be decided from it."""
    file = path(root)
    try:
        descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > READ_LIMIT:
            return None
        key = (status.st_ino, status.st_size, status.st_mtime_ns)
        cached = _cache.get(str(file))
        if cached and cached[0] == key:
            return cached[1]
        raw = os.read(descriptor, READ_LIMIT + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        data = json.loads(raw) if len(raw) <= READ_LIMIT else None
    except (ValueError, UnicodeError):
        data = None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return None
    result = {}
    for name, value in plugins.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(value, dict):
            continue
        events, source, directory = value.get("events"), value.get("source"), value.get("directory")
        result[name] = {
            "events": list(events) if isinstance(events, list) and all(isinstance(p, str) for p in events) else [],
            "source": source if isinstance(source, str) else None,
            "directory": directory if isinstance(directory, str) and os.path.isabs(directory) else None}
    _cache[str(file)] = (key, result)
    return result


def candidates(root):
    """[(source it was found by, folder)] of recorded folders, for discovery to look in as well."""
    return [(value["source"], Path(value["directory"])) for _, value in sorted(read(root).items())
            if value["directory"] is not None]


@contextmanager
def writing(root, name):
    """The registry's writer lock, held by whoever records it: an enable of `name` holds it from recording the
    plugin through publishing its switch, so no listing in between, here or anywhere, forgets what was prepared."""
    file = path(root)
    file.parent.mkdir(parents=True, exist_ok=True)
    with lock(file.with_name(".registry.lock")):
        held = _held.__dict__.setdefault("files", {})
        held[str(file)] = name
        try:
            yield
        finally:
            held.pop(str(file), None)


_held = threading.local()  # .files: {registry file: the plugin being turned on} whose writer lock this thread holds


def record(root, current, *, person, approved=None, admitted=None):
    """Bring the record in line with `current` (the universe's entries): each plugin that's on, its event patterns
    and its source. A plugin that's on but which this process can't find keeps its record. From the person's own
    process (`person`), a plugin found by a source another process may lack gets its folder recorded; from an
    agent's, folders are kept as they were. Only `approved`, the plugin the person is turning on now, is pinned at
    `admitted` ((folder, fingerprint) read when the enable was admitted), and only when nothing pinned that folder
    before. The person's listing records the folder of a plugin that's on and never recorded, pinned at what this
    process already runs; it never pins over a pin, so an edit waits for mv2 plugins trust. Returns whether it
    wrote; never raises."""
    try:
        if not person:
            approved = None  # only the person's own choice moves or pins a folder: an agent's enable is no choice
        before = read(root)
        after = {}
        preparing = getattr(_held, "files", {}).get(str(path(root)))  # turned on by this thread, not yet published
        for found in current:
            if (not found.on and not (person and found.name == approved and admitted is not None)
                    and found.name != preparing):
                continue  # the plugin being turned on is recorded before its switch is published
            kept = before.get(found.name) or {}
            if person and found.name == approved and admitted is not None:
                # The person's choice, published from what was admitted, whatever discovery finds now: its folder,
                # its source, its pin and the patterns of exactly those bytes.
                directory, value, _, admitted_source = admitted
                _pin_new(directory, value)
                after[found.name] = {"events": _pinned_events(directory) or [], "source": admitted_source,
                                     "directory": str(directory)}
                continue
            if found.manifest is None:  # on, but not found here (or unreadable): its record stays
                if kept:
                    after[found.name] = kept
                continue
            if kept.get("directory") is not None and found.name != approved:
                # A recorded folder keeps its record; its patterns are those of the copy pinned for it, the bytes
                # every process runs, never of whatever this listing saw (an edited folder, an older listing, or a
                # same-name copy this process finds first).
                pinned = _pinned_events(kept["directory"])
                after[found.name] = {**kept, "events": pinned if pinned is not None else kept["events"]}
                continue
            entry = {"events": sorted(found.manifest.events), "source": found.source, "directory": None}
            if found.source not in SHARED and found.source != "recorded":
                if not person:
                    after[found.name] = entry  # an agent's own find: its patterns only, never a folder
                    continue
                # On by the person's switch (plugins.json), found by this, the person's own process, and never
                # recorded: recorded now, pinned at one snapshot of the bytes this process already runs as found,
                # checked as turning it on checks it (its name, and whether this mv2 can load it), so it approves
                # nothing new. A folder already pinned is never pinned again.
                try:
                    directory, value, admitted_manifest = snapshot(found.directory)
                except (OSError, ValueError):
                    continue  # changing as it was read: recorded by a later listing
                if admitted_manifest.name != found.name or _incompatible(admitted_manifest):
                    continue  # not the plugin that is on any more, or one this mv2 can't load: nothing recorded
                _pin_new(directory, value)
                entry.update(events=_pinned_events(directory) or [], directory=str(directory))
            elif found.source == "recorded":
                continue  # recorded, but its record is gone: a later listing records it again
            after[found.name] = entry
        if after == before:
            return False
        file = path(root)
        file.parent.mkdir(parents=True, exist_ok=True)
        with (nullcontext() if str(file) in getattr(_held, "files", {}) else lock(file.with_name(".registry.lock"))):
            # What `current` saw may be older than the record and plugins.json now: merged with them here, so an
            # older listing never forgets a plugin turned on since, nor brings back one turned off.
            from . import config
            switches = config.load(root).enabled
            fresh = read(root)
            final = {name: entry for name, entry in after.items()
                     if switches.get(name) is not False or name in (approved, preparing)}
            for name, entry in fresh.items():
                if name not in final:
                    if switches.get(name) is True or name == preparing:  # being turned on: kept, seen or not
                        final[name] = entry
                elif (name != approved and entry["directory"] is not None
                      and final[name]["directory"] != entry["directory"]):
                    # A listing never moves a recorded folder: one that started earlier may have seen where it was
                    # before. Only turning the plugin on (`approved`) chooses where it is.
                    final[name] = entry
            for name, entry in final.items():  # checked again as it's published: the pin may have moved since
                if entry["directory"] is not None:
                    pinned = _pinned_events(entry["directory"])
                    if pinned is not None:
                        final[name] = {**entry, "events": pinned}
            if final == fresh:
                return False
            write(file, {"schema": 1, "plugins": final})
        return True
    except Exception:
        return False


def snapshot(directory):
    """(folder, fingerprint, manifest) of a plugin as one admitted state: the fingerprint read first, then exactly
    those bytes copied (trust.cached refuses a plugin that changed meanwhile) and its manifest read from the copy.
    ValueError when it changed while this was taken."""
    from . import manifest as manifests, trust
    folder = Path(directory).resolve()
    value = trust.current(folder)
    return folder, value, manifests.load(trust.cached(folder, value))


def _pin_new(directory, value=None):
    """Pin a folder the person just turned on at `value` (the fingerprint admitted; its content now when not given),
    unless anything pinned it before, decided under the trust lock (an edit then waits for mv2 plugins trust), or
    the installer manages it (its pin is the one it was installed at)."""
    from . import installer, trust
    if installer.in_personal(directory) or installer.marked(directory):
        return
    return trust.pin_first(directory, value if value is not None else trust.current(directory))


def _pinned_at(directory, value):
    """Whether `directory`'s pin is exactly the fingerprint `value` (whatever its files are now)."""
    from . import trust
    pinned = trust._records().get(str(Path(directory).resolve()))
    return isinstance(pinned, dict) and pinned.get("fingerprint") == value


_pinned = {}  # fingerprint -> the event patterns of the plugin copy pinned at it (a copy never changes)


def _pinned_events(directory):
    """The event patterns of the copy pinned for `directory`, the exact bytes approved and run; None when nothing is
    pinned there or that copy can't be had (it changed before it was copied): then nothing new is learned."""
    from . import manifest as manifests, trust
    record = trust._records().get(str(Path(directory).resolve()))
    value = record.get("fingerprint") if isinstance(record, dict) else None
    if not isinstance(value, str):
        return None
    if value not in _pinned:
        try:
            _pinned[value] = sorted(manifests.load(trust.cached(Path(directory), value)).events)
        except (OSError, ValueError):
            return None
    return list(_pinned[value])


def _incompatible(manifest):
    from . import loader
    return loader.compatible(manifest) is not None
