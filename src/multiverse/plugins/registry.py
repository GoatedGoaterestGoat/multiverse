"""What every mv2 process in a universe must know of the plugins that are on there, however this one finds them.

A plugin can be found only in the person's own environment (a folder in their MV2_PLUGIN_PATH, a package found
only through their PYTHONPATH) while an agent's process records its events and launches its harness. So
state/plugins/registry.json keeps, for each plugin that's on: the event types it subscribes to, the source that
found it, and, when another process may lack that source, the folder it is in.

- A plugin's events are those of the files that run (events.subscribers): its approved copy's when it runs at
  approved content, its manifest's as found now (read, never imported) otherwise. The recorded patterns answer
  for a pinned folder whose copy can't be read, and for a plugin only an agent's process found ("private"). A
  recorded folder's patterns are those of the copy pinned for it, read again as the record is written.
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
import sys
import threading

from ..store import lock, write
from .manifest import NAME

FILE = "registry.json"  # in the universe's state/plugins/
READ_LIMIT = 1024 * 1024  # a record larger than this isn't read
# Sources every process of this user and this mv2 finds alike: nothing to record but the patterns.
SHARED = frozenset({"universe", "personal", "checkout", "bundled", "recorded"})
_cache = {}


def shared(source, directory=None):
    """Whether every process of this user and this mv2 finds a plugin of `source` at `directory` alike, so only its
    event types are recorded and it runs as it is, never pinned. True of every source but MV2_PLUGIN_PATH's ("path"),
    and of a package installed in the Python that runs mv2 (pip install, an upgrade in place, an editable install):
    one whose distribution was found in that Python's own sys.path, not through PYTHONPATH, whose package is the
    one that distribution installed (inside the same sys.path entry, or inside the project an editable install of
    it maps to), reached not through PYTHONPATH either. A package found any other way (another Python's, a folder
    found ahead of the distribution's own, or nothing proves where it came from) is not."""
    if source in SHARED:
        return True
    if source != "installed" or directory is None:
        return False
    from . import loader
    origin = loader.installed_origin(directory)
    if not isinstance(origin, dict) or not isinstance(origin.get("entry"), str):
        return False
    if not _spellings(origin["entry"]) & _own_entries() or _through_pythonpath(directory):
        return False
    homes = [origin["entry"]] + ([origin["editable"]] if isinstance(origin.get("editable"), str) else [])
    return any(_resolved_inside(directory, home) for home in homes)


def _resolved_inside(path, folder):
    """Whether `path`, where it leads, is inside where `folder` leads: a proof, so a link in a shared folder to one
    outside it proves nothing."""
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except (OSError, RuntimeError):
        return False


def _inside(path, folder):
    """Whether `path`, as given or where it leads, is `folder` or inside it, as given or where it leads."""
    return any(spelling == base or spelling.startswith(base.rstrip(os.sep) + os.sep)
               for spelling in _spellings(path) for base in _spellings(folder))


def _spellings(path):
    """The path as given (absolute) and where it leads: a link is judged by both."""
    found = {os.path.abspath(path)}
    try:
        found.add(str(Path(path).resolve()))
    except (OSError, RuntimeError):
        pass
    return found


def _private_entries():
    return {spelling for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry.strip()
            for spelling in _spellings(entry)}


def _own_entries():
    """The sys.path entries the Python running mv2 has of its own: all but the first (the script's folder or the
    current one) and those this process's PYTHONPATH adds."""
    private = _private_entries()
    return {spelling for entry in sys.path[1:] if entry for spelling in _spellings(entry)} - private


def _through_pythonpath(directory):
    """Whether `directory`, as given or where it leads, is inside a folder this process's PYTHONPATH names."""
    return any(_inside(directory, entry) for entry in _private_entries())


def approved_events(directory, pins):
    """The event patterns of the bytes approved for `directory`: ("none", None) when nothing pins it, ("approved",
    patterns) kept with its pin, ("unknown", None) when the pins (`pins`, the paths trusted-plugins.json pins, or
    None when that can't be read for sure) can't be, and ("legacy", None) for a pin made before types were kept with
    it (the caller admits conservatively; the person's listing proves and keeps its types). Reads the pin alone:
    no file of the plugin, no hash, no write, no lock."""
    from . import trust
    if pins is None:
        return "unknown", None
    try:
        folder = str(Path(directory).resolve())
    except (OSError, RuntimeError):
        return "unknown", None
    if folder not in pins:
        return "none", None
    record = pins[folder]
    if not isinstance(record, dict):
        return "unknown", None  # pinned, but its entry is damaged: never read as never pinned
    value = record.get("fingerprint")
    if not trust.valid_fingerprint(value):
        return "unknown", None  # a damaged pin approves nothing
    kept = record.get("events")
    if isinstance(kept, list) and all(isinstance(kind, str) for kind in kept):
        return "approved", sorted(kept)  # kept with the pin when it was made
    return "legacy", None


def _proved(folder, value):
    """The event types of `folder` when its files hash to `value`, read from exactly the plugin.json bytes hashed;
    else None. Hashes: for the person's listing, never for a lookup."""
    from . import trust
    try:
        now_value, raw = trust.fingerprint_with(folder)
    except (OSError, ValueError):
        return None
    return trust.events_of(folder, raw) if now_value == value else None


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
        if value.get("private") is True:
            result[name]["private"] = True
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
        if person:
            _keep_proved_events(current)  # first, so what it proves is recorded below
        approvals = _approvals()
        after = {}
        unpinned = {}  # name: the record this listing found to be of a shared package, exactly as it read it
        preparing = getattr(_held, "files", {}).get(str(path(root)))  # turned on by this thread, not yet published
        for found in current:
            if (not found.on and not (person and found.name == approved and admitted is not None)
                    and found.name != preparing):
                continue  # the plugin being turned on is recorded before its switch is published
            kept = before.get(found.name) or {}
            if person and found.name == approved and admitted is not None:
                # The person's choice, published from what was admitted, whatever discovery finds now: its folder,
                # its source, its pin and the patterns of exactly those bytes.
                directory, value, admitted_manifest, admitted_source = admitted
                _pin_new(directory, value, admitted_manifest.events)
                after[found.name] = {"events": _pinned_events(directory) or [], "source": admitted_source,
                                     "directory": str(directory)}
                continue
            if found.manifest is None:  # on, but not found here (or unreadable): its record stays
                if kept:
                    after[found.name] = kept
                continue
            if (person and kept.get("source") == "installed" and found.source == "installed"
                    and kept.get("directory") is not None and found.name != approved
                    and shared(found.source, found.directory) and _same_folder(found.directory, kept["directory"])):
                # A package an earlier listing recorded and pinned as found only here, which every process finds
                # alike (one of the Python running mv2, by where it was registered): recorded as shared, so it runs
                # as it is, an upgrade included.
                after[found.name] = {"events": sorted(found.manifest.events), "source": found.source,
                                     "directory": None}
                unpinned[found.name] = dict(kept)
                continue
            if kept.get("directory") is not None and found.name != approved:
                # A recorded folder keeps its record; its patterns are those of the copy pinned for it, the bytes
                # every process runs, never of whatever this listing saw (an edited folder, an older listing, or a
                # same-name copy this process finds first).
                pinned = _pinned_events(kept["directory"])
                after[found.name] = {**kept, "events": pinned if pinned is not None else kept["events"]}
                continue
            entry = {"events": _recorded_events(found, kept, approvals), "source": found.source, "directory": None}
            if not shared(found.source, found.directory) and found.source != "recorded":
                if not person:
                    # An agent's own find: its patterns only, never a folder, marked as found only there.
                    after[found.name] = {**entry, "private": True}
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
                _pin_new(directory, value, admitted_manifest.events)
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
                      and final[name]["directory"] != entry["directory"]
                      and not (final[name]["directory"] is None and unpinned.get(name) == entry)):
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


def _approvals():
    """{resolved folder: its pin record} from trusted-plugins.json, {} when it isn't there (a reset), None when it's
    there but can't be read (trust.pins_now): then a listing keeps the types established before."""
    from . import trust
    try:
        return trust.pins_now(wait=trust.written_before())  # a listing may wait out a replacement gap
    except Exception:
        return None


def _recorded_events(found, kept, approvals):
    """The event types a listing records for `found`: its manifest's, unless its folder is pinned. Then the types
    kept with its pin (the approved ones); for a pin made before types were kept with it, the manifest's only while
    it runs as found (unchanged since approved), else what was established before (an edit nobody approved is
    never recorded as its types); nothing, when the pins can't be read."""
    from . import trust
    manifest = sorted(found.manifest.events)
    if found.directory is None:
        return manifest
    try:
        folder = str(Path(found.directory).resolve())
    except (OSError, RuntimeError):
        return list(kept.get("events", []))
    if approvals is None:
        return list(kept.get("events", []))
    if folder not in approvals:
        return manifest
    pin = approvals[folder]
    if not isinstance(pin, dict) or not trust.valid_fingerprint(pin.get("fingerprint")):
        return list(kept.get("events", []))  # pinned, but its entry is damaged: what was established stays
    approved = pin.get("events")
    if isinstance(approved, list) and all(isinstance(kind, str) for kind in approved):
        return sorted(approved)
    return manifest if getattr(found, "state", None) in ("on", "loaded", "loading") else list(kept.get("events", []))


def _keep_proved_events(current):
    """Keep with each pin made before types were kept with it the types of exactly its pinned bytes, proved by
    hashing its folder, or else the copy made of it when it last ran: the person's listing, which may hash, so the
    journal never needs to."""
    from . import trust
    try:
        records = trust._records()
    except Exception:
        return
    for found in current:
        if found.directory is None:
            continue
        try:
            folder = Path(found.directory).resolve()
            record = records.get(str(folder))
            if (not isinstance(record, dict) or "events" in record
                    or not trust.valid_fingerprint(record.get("fingerprint"))):
                continue
            value = record["fingerprint"]
            kinds = _proved(folder, value) if trust.current(folder) == value else None
            if kinds is None:  # changed since it was approved: the copy it last ran from, if any
                kinds = trust.copy_events(value)
            if kinds is not None:
                trust.keep_events(folder, value, kinds)
        except (OSError, ValueError):
            continue


def _same_folder(directory, recorded):
    try:
        return str(Path(directory).resolve()) == recorded
    except (OSError, RuntimeError):
        return False


def snapshot(directory):
    """(folder, fingerprint, manifest) of a plugin as one admitted state: the fingerprint read first, then exactly
    those bytes copied (trust.cached refuses a plugin that changed meanwhile) and its manifest read from the copy.
    ValueError when it changed while this was taken."""
    from . import manifest as manifests, trust
    folder = Path(directory).resolve()
    value = trust.current(folder)
    return folder, value, manifests.load(trust.cached(folder, value))


def _pin_new(directory, value=None, events=None):
    """Pin a folder the person just turned on at `value` (the fingerprint admitted; its content now when not given),
    unless anything pinned it before, decided under the trust lock (an edit then waits for mv2 plugins trust), or
    the installer manages it (its pin is the one it was installed at)."""
    from . import installer, trust
    if installer.in_personal(directory) or installer.marked(directory):
        return
    if value is None:
        value, events = trust.current(directory), None
    return trust.pin_first(directory, value, events)


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
    if not trust.valid_fingerprint(value):
        return None
    kept = record.get("events")
    if isinstance(kept, list) and all(isinstance(kind, str) for kind in kept):
        return sorted(kept)
    if value not in _pinned:
        try:
            _pinned[value] = sorted(manifests.load(trust.cached(Path(directory), value)).events)
        except (OSError, ValueError):
            return None
    return list(_pinned[value])


def _incompatible(manifest):
    from . import loader
    return loader.compatible(manifest) is not None
