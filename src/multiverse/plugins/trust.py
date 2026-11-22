"""Trust for plugins a universe carries in its own `plugins/` directory.

A repository can carry `.multiverse/plugins/`, and opening it must not run its
code. The user trusts such a plugin at its current content: its path and a
SHA-256 over every one of its files (only `.git` is left out) go in
`~/.multiverse/trusted-plugins.json`, outside every universe. Any change to the
files, compiled bytecode and `node_modules` included, means trusting it again.

What runs is never the universe's directory itself but a copy of exactly the
trusted bytes, in `~/.multiverse/plugin-cache/<sha256>/`, made by hashing the
files as they are copied: the plugin is imported from it and its Claude
directory loaded from it. The copy leaves out compiled Python bytecode, so a
.pyc planted beside a reviewed .py never runs; and what Claude writes into a
directory it loads (tsconfig.json, types) lands in the copy, never changing the
trusted plugin.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import time

from ..store import lock, now, read, retry_missing, write

FILE = "trusted-plugins.json"
CACHE = "plugin-cache"
FINGERPRINTS = "plugin-fingerprints.json"
UNUSED_DAYS = 14  # a copy nothing trusts and nothing used for this long is removed
STAGING_HOURS = 1  # a copy still being made after this long was left by a process that died  # each trusted directory's fingerprint, by the signature of its files
COMPLETE = ".mv2-complete"
LISTED = ".mv2-files"  # in a copy: the files it was made with, which must stay exactly as trusted
# Not part of a plugin: version control, and the caches Python and test tools write while a plugin is developed or
# its tests run. None of it runs: mv2 runs a trusted plugin from a copy that leaves these out too, and Python
# compiles that copy's own bytecode. Everything else is part of the plugin, whether it runs from source or not.
SKIPPED = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIPPED_FILES = {".coverage"}
SKIPPED_SUFFIXES = (".pyc", ".pyo")
# Code a reviewer cannot read: trust warns about it.
COMPILED = (".so", ".dylib", ".dll", ".pyd", ".node", ".wasm")


def user_directory():
    return Path.home() / ".multiverse"


def _skipped(relative):
    """Whether a path inside a plugin is not part of it (SKIPPED)."""
    relative = Path(relative)
    return (any(part in SKIPPED for part in relative.parts) or relative.name in SKIPPED_FILES
            or relative.suffix in SKIPPED_SUFFIXES)


def _files(directory):
    """(relative path, path) of every file in a plugin, in a fixed order; ValueError for a symbolic link."""
    directory = Path(directory)
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if _skipped(relative):
            continue
        if path.is_symlink():
            raise ValueError(f"{path}: plugins in a universe may not contain symbolic links")
        if path.is_file():
            yield relative, path


def _add(digest, relative, data):
    digest.update(relative.as_posix().encode() + b"\0" + str(len(data)).encode() + b"\0" + data)


def fingerprint(directory):
    """SHA-256 over every file's relative path and bytes, in a fixed order: everything but what is not part of a
    plugin (SKIPPED: `.git`, bytecode, test caches).

    Raises ValueError for a symbolic link, as templates do: trust covers the
    plugin's own files, never something a link points to.
    """
    digest = hashlib.sha256()
    for relative, path in _files(directory):
        _add(digest, relative, path.read_bytes())
    return "sha256:" + digest.hexdigest()


def fingerprint_with(directory, name="plugin.json"):
    """(fingerprint, the bytes of `name` exactly as hashed, or None): what a fingerprint approves, read in the same
    pass. Reads only the plugin's regular files: writes nothing, takes no lock."""
    digest, kept = hashlib.sha256(), None
    for relative, path in _files(directory):
        data = path.read_bytes()
        if relative.as_posix() == name:
            kept = data
        _add(digest, relative, data)
    return "sha256:" + digest.hexdigest(), kept


def events_of(directory, raw):
    """The sorted event types of plugin.json bytes `raw`, or None when they don't make a manifest."""
    from . import manifest as manifests
    try:
        return sorted(manifests.load_bytes(directory, raw).events)
    except (OSError, ValueError):
        return None


def signature(directory):
    """A digest of every file's path, size, modification and change times, inode and mode (no contents).

    Cheap: one stat per file. It changes whenever a file is added, removed or
    written; the change time cannot be set back by hand. Used to skip hashing a
    directory again, and to tell when a plugin's files changed."""
    digest = hashlib.sha256()
    base = str(Path(directory))
    for top, directories, files in os.walk(base):
        directories[:] = sorted(name for name in directories if name not in SKIPPED)
        for name in sorted(files):
            path = os.path.join(top, name)
            if _skipped(os.path.relpath(path, base)):
                continue
            status = os.lstat(path)
            digest.update(f"{os.path.relpath(path, base)}\0{status.st_size}\0{status.st_mtime_ns}\0"
                          f"{status.st_ctime_ns}\0{status.st_ino}\0{status.st_mode}\n".encode())
    return digest.hexdigest()


def current(directory):
    """The fingerprint of `directory`, hashed again only when its files changed since it was last hashed.

    Safe to trust: what runs is the copy made by cached(), which hashes the
    bytes again as it copies them and keeps nothing that does not match."""
    directory = Path(directory).resolve()
    before = signature(directory)
    record = _fingerprints().get(str(directory))
    if isinstance(record, dict) and record.get("signature") == before and isinstance(record.get("fingerprint"), str):
        return record["fingerprint"]
    value = fingerprint(directory)
    if signature(directory) == before:  # unchanged while it was hashed: worth keeping
        _remember(directory, before, value)
    return value


def _fingerprints():
    try:
        data = retry_missing(lambda: read(user_directory() / FINGERPRINTS))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


READ_LIMIT = 4 * 1024 * 1024  # a trust map or memo larger than this isn't read where nothing may wait


def read_regular(path, limit=READ_LIMIT, raw=False):
    """A JSON file (its bytes, with `raw`) read without ever waiting: opened without following a link or blocking
    (a FIFO there is refused, never waited on), only as a regular file of at most `limit` bytes, never retried.
    FileNotFoundError when it's absent; OSError or ValueError for anything else it can't be."""
    import stat as stats
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        status = os.fstat(descriptor)
        if not stats.S_ISREG(status.st_mode) or status.st_size > limit:
            raise OSError(f"{path} isn't a regular file of at most {limit} bytes")
        data = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise OSError(f"{path} is larger than {limit} bytes")
    return data if raw else json.loads(data)


def pins_now():
    """The paths trusted-plugins.json pins ({path: record}), read without waiting (read_regular): {} when it was
    never written, None when it can't be read for sure (missing though its writers' lock is there, not a regular
    file, damaged)."""
    path = user_directory() / FILE
    try:
        data = read_regular(path)
    except FileNotFoundError:
        return None if (user_directory() / ".trusted-plugins.lock").exists() else {}
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else None


def copy_events(value):
    """The event types of the cached copy made at fingerprint `value`, from the plugin.json bytes inside the hash
    that proves the copy is those bytes (its files as listed when it was made, or for an older copy its own files
    or all of them, as _copy_intact checks); None when there's no complete copy or it doesn't hash to `value`."""
    if not valid_fingerprint(value):
        return None  # no path is made from a damaged pin
    target = cache_directory(value)
    if not (target / COMPLETE).is_file():
        return None
    try:
        listed = read_regular(target / LISTED)
    except FileNotFoundError:
        listed = None
    except (OSError, ValueError):
        return None
    present = _present(target)  # the files in the copy, as _copy_intact lists them: nothing outside it
    if listed is not None:
        if (not isinstance(listed, list) or not all(isinstance(item, str) for item in listed)
                or not set(listed) <= set(present)):
            return None  # a damaged list, or one naming what isn't a file of the copy: nothing read
        candidates = [sorted(listed)]
    else:
        candidates = [[name for name in present if not _bookkeeping(target, Path(name))], present]
    for names in candidates:
        digest, kept = hashlib.sha256(), None
        for name in names:
            path = target / name
            if path.is_symlink() or not path.is_file():
                break
            data = path.read_bytes()
            if name == "plugin.json":
                kept = data
            _add(digest, Path(name), data)
        else:
            if "sha256:" + digest.hexdigest() == value:
                return events_of(target, kept)
    return None


def keep_events(directory, value, events):
    """Keep `events` with the pin of `directory` when it still pins `value` and keeps no types yet (a pin made
    before they were kept): written under the trust lock; True when it wrote."""
    directory = Path(directory).resolve()
    path = user_directory() / FILE
    with lock(user_directory() / ".trusted-plugins.lock"):
        records = _records()
        record = records.get(str(directory))
        if not isinstance(record, dict) or record.get("fingerprint") != value or "events" in record:
            return False
        records[str(directory)] = {**record, "events": sorted(events)}
        write(path, {"schema": 1, "plugins": records})
    return True


def _pin_record(value, events):
    record = {"fingerprint": value, "trusted_at": now()}
    if events is not None:
        record["events"] = sorted(events)  # the approved copy's event types: the journal reads them, never hashes
    return record


def _remember(directory, value_signature, value):
    try:
        with lock(user_directory() / ".plugin-fingerprints.lock"):
            records = _fingerprints()
            records[str(directory)] = {"signature": value_signature, "fingerprint": value}
            write(user_directory() / FINGERPRINTS, records)
    except OSError:
        pass  # only a cache: the next process hashes again


def compiled(directory):
    """What a reviewer cannot read: compiled files and node_modules, as relative paths (node_modules once)."""
    found = set()
    for relative, _ in _files(directory):
        if "node_modules" in relative.parts:
            found.add(Path(*relative.parts[:relative.parts.index("node_modules") + 1]).as_posix() + "/")
        elif relative.suffix in COMPILED:
            found.add(relative.as_posix())
    return sorted(found)


FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")  # what fingerprint() writes, and so the only pin a pin can be


def valid_fingerprint(value):
    """Whether `value` is a fingerprint as fingerprint() writes it: only such a pin approves anything or names a
    cached copy."""
    return isinstance(value, str) and FINGERPRINT.fullmatch(value) is not None


def cache_directory(value):
    return user_directory() / CACHE / value.split(":", 1)[-1]


def cached(directory, value):
    """The copy of the plugin at `directory` whose fingerprint is `value`, made now if needed; its path.

    The bytes are hashed as they are copied, so the copy is exactly what was
    trusted: if the plugin changed meanwhile, nothing is kept and ValueError
    says so. What is not part of a plugin (SKIPPED: bytecode and test caches)
    is neither hashed nor copied, so Python compiles the copy's own; the copy
    is used only once complete."""
    target = cache_directory(value)
    if (target / COMPLETE).is_file():
        if not _copy_intact(target, value):
            # Its trusted files changed after it was made: an edit there is tampering, and never runs.
            raise ValueError(f"the cached copy of this plugin at {target} was edited after it was trusted, so it "
                             f"won't run: remove that folder, and mv2 makes it again from the trusted plugin")
        try:
            os.utime(target / COMPLETE)  # used: kept while anything uses it (prune_cache)
        except OSError:
            pass
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".copy-", dir=target.parent))
    try:
        digest = hashlib.sha256()
        for relative, path in _files(directory):
            data = path.read_bytes()
            _add(digest, relative, data)
            copy = staging / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(data)
            os.chmod(copy, path.stat().st_mode & 0o777)
        if "sha256:" + digest.hexdigest() != value:
            raise ValueError(f"{directory} changed while it was copied; review it, then trust it again")
        (staging / LISTED).write_text(json.dumps([relative.as_posix() for relative, _ in _files(staging)]) + "\n")
        (staging / COMPLETE).write_text(value + "\n")
        try:
            staging.rename(target)
        except OSError:  # another process made the same copy first
            if not (target / COMPLETE).is_file():
                raise
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _bookkeeping(target, relative):
    """Whether `relative`, a file added to a copy, is what Claude Code writes into a plugin directory it loads, at
    an interactive start (checked on 2.1.327-2.1.328): `tsconfig.json` beside the directory's `.claude-plugin/`, and
    anything under `.claude-plugin/types/`, whose bytes follow the running Claude version. None of it runs."""
    parts = relative.parts
    for index in range(len(parts) - 2):
        if parts[index] == ".claude-plugin" and parts[index + 1] == "types":
            return True
    return relative.name == "tsconfig.json" and (Path(target) / relative.parent / ".claude-plugin").is_dir()


def _digest(target, names):
    digest = hashlib.sha256()
    for name in names:
        path = Path(target) / name
        if path.is_symlink() or not path.is_file():
            return None
        _add(digest, Path(name), path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _present(target):
    """Every file in a copy that Python or Claude could load, as relative names: what _files lists, and bytecode
    too, since Python imports a lone `module.pyc` or `package/__init__.pyc` as it is, wherever it sits. A copy's
    `__pycache__` folders are removed first: mv2 writes none there (loader._keep_bytecode_out), and one that stays
    counts as added. Only folders no import can name (`.git` and test caches) and the copy's own records are left
    out."""
    for folder in sorted(target.rglob("__pycache__"), reverse=True):
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder, ignore_errors=True)
    found = []
    for path in sorted(target.rglob("*")):
        relative = path.relative_to(target)
        if relative.as_posix() in (COMPLETE, LISTED) or relative.name in SKIPPED_FILES:
            continue
        if any(part in SKIPPED and part != "__pycache__" for part in relative.parts):
            continue
        if path.is_symlink() or path.is_file():
            found.append(relative.as_posix())
    return sorted(found)


def _copy_intact(target, value):
    """Whether a copy is still exactly what was trusted as `value`: every file it was made with (`.mv2-files`) the
    trusted bytes, and nothing added but Claude's bookkeeping (_bookkeeping), since an added module could run. A
    copy with no list (made before it was kept, or with it removed) must hash to `value` over its files, Claude's
    additions left out or not, and is then given its list. The listed files' stats are kept, so an unchanged copy
    is hashed once."""
    target = Path(target)
    present = _present(target)
    try:
        listed = json.loads((target / LISTED).read_text())
    except FileNotFoundError:
        listed = None
    except (OSError, ValueError):
        return False
    if listed is None:
        own = [name for name in present if not _bookkeeping(target, Path(name))]
        for names in (own, present):
            if _digest(target, names) == value:
                try:
                    (target / LISTED).write_text(json.dumps(names) + "\n")  # checked now: kept from here on
                except OSError:
                    pass
                return True
        return False
    if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
        return False
    if not set(listed) <= set(present):
        return False  # a trusted file is gone
    if any(not _bookkeeping(target, Path(name)) for name in set(present) - set(listed)):
        return False  # something added that could run
    stats = hashlib.sha256()
    for name in sorted(listed):
        status = os.lstat(target / name)
        stats.update(f"{name}\0{status.st_size}\0{status.st_mtime_ns}\0{status.st_ctime_ns}\0"
                     f"{status.st_ino}\0{status.st_mode}\n".encode())
    key = "copy:" + str(target.resolve())
    record = _fingerprints().get(key)
    if isinstance(record, dict) and record.get("signature") == stats.hexdigest():
        return record.get("fingerprint") == value
    found = _digest(target, sorted(listed))
    if found is not None:
        _remember(key, stats.hexdigest(), found)
    return found == value


def prune_cache():
    """Remove copies in ~/.multiverse/plugin-cache that no trusted plugin points at and that nothing used for
    UNUSED_DAYS (a launch or load marks the copy it uses), and staging directories a process left behind.
    A copy something still trusts is never removed. Returns the paths removed; never raises."""
    removed = []
    base = user_directory() / CACHE
    try:
        children = list(base.iterdir())
    except OSError:
        return removed
    trusted = {record.get("fingerprint") for record in _records().values() if isinstance(record, dict)}
    now_ = time.time()
    for path in children:
        try:
            if path.name.startswith(".copy-"):
                if now_ - path.stat().st_mtime < STAGING_HOURS * 3600:
                    continue
            elif "sha256:" + path.name in trusted:
                continue
            else:
                marker = path / COMPLETE
                used = marker.stat().st_mtime if marker.is_file() else path.stat().st_mtime
                if now_ - used < UNUSED_DAYS * 86400:
                    continue
            shutil.rmtree(path)
            removed.append(path)
        except OSError:
            continue
    return removed


def _records():
    path = user_directory() / FILE
    try:
        data = retry_missing(lambda: read(path))
    except (OSError, ValueError):
        return {}
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else {}


def state(directory):
    """`trusted`, `changed` (trusted at other content) or `untrusted`."""
    return verify(directory)[0]


def verify(directory):
    """(state, fingerprint): the fingerprint is the trusted one when the state is `trusted`, else None."""
    directory = Path(directory).resolve()
    record = _records().get(str(directory))
    if not isinstance(record, dict):
        return "untrusted", None
    try:
        value = current(directory)
    except (OSError, ValueError):
        return "changed", None
    return ("trusted", value) if record.get("fingerprint") == value else ("changed", None)


def trust(directory):
    """Trust the plugin at `directory` as it is now; returns its fingerprint."""
    directory = Path(directory).resolve()
    before = signature(directory)
    value, raw = fingerprint_with(directory)
    if signature(directory) == before:
        _remember(directory, before, value)
    events = events_of(directory, raw)  # of exactly the bytes trusted; no copy made, so an edit after still won't run
    path = user_directory() / FILE
    with lock(user_directory() / ".trusted-plugins.lock"):
        records = _records()
        records[str(directory)] = _pin_record(value, events)
        write(path, {"schema": 1, "plugins": records})
    prune_cache()
    return value


def pin(directory, value, events=None):
    """Trust whatever at `directory` has fingerprint `value`, without reading it: the installer pins what the
    person approved before the files arrive there, so they're never there unpinned. `events`: the event types of
    those bytes, when known."""
    directory = Path(directory).resolve()
    with lock(user_directory() / ".trusted-plugins.lock"):
        records = _records()
        records[str(directory)] = _pin_record(value, events)
        write(user_directory() / FILE, {"schema": 1, "plugins": records})


def pin_first(directory, value, events=None):
    """Pin `directory` at fingerprint `value` only if nothing pins it yet, deciding and writing under the trust
    lock; True when it pinned. A trust map that is there but can't be read pins nothing. `events`: the event types
    of those bytes, when known."""
    directory = Path(directory).resolve()
    path = user_directory() / FILE
    with lock(user_directory() / ".trusted-plugins.lock"):
        try:
            data = retry_missing(lambda: read(path))
        except FileNotFoundError:
            data = {"plugins": {}}
        except (OSError, ValueError):
            return False
        records = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(records, dict) or str(directory) in records:
            return False
        records[str(directory)] = _pin_record(value, events)
        write(path, {"schema": 1, "plugins": records})
        return True


def forget(directory):
    """Stop trusting the plugin at `directory`; True when it was trusted."""
    directory = Path(directory).resolve()
    with lock(user_directory() / ".trusted-plugins.lock"):
        records = _records()
        removed = records.pop(str(directory), None) is not None
        if removed:
            write(user_directory() / FILE, {"schema": 1, "plugins": records})
    prune_cache()
    return removed
