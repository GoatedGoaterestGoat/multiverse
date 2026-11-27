"""The records ~/.multiverse keeps for plugin folders, which `mv2 plugins prune` removes once a folder is gone or the
person names it: trust pins (trusted-plugins.json), fingerprint memos (plugin-fingerprints.json), the cached copies
only those pins named (plugin-cache), and the package scans of Pythons that are gone (installed-plugins.json).

It never touches a plugin's folder, nor the records of a plugin mv2 plugins install placed: mv2 plugins remove
clears those. find() only reads; remove() removes each entry only if it is still as find() listed it, under the lock
that entry's writers take."""
import os
import shlex
import shutil
import stat
from pathlib import Path

from ..store import lock, read, write
from . import installer, loader, trust


def _gone(path):
    """True when nothing is at `path` any more (a missing parent, or a parent that isn't a folder, counts too);
    None when that can't be told (no permission to look, say); else False."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return None
    return False


def _spellings(folder):
    """`folder` as given (made absolute) and where it leads, so a record under either spelling is found."""
    folder = Path(folder).expanduser()
    found = {os.path.normpath(os.path.abspath(folder))}
    try:
        found.add(str(folder.resolve()))
    except (OSError, RuntimeError):
        pass
    return found


def _under(path, folders):
    """Whether `path` is one of `folders` or inside one (by whole path parts: /a/plug is not under /a/plugins)."""
    path = os.path.normpath(path)
    return any(path == folder or path.startswith(folder.rstrip(os.sep) + os.sep) for folder in folders)


def _installed(path):
    """Why the plugin folder at `path` is left to mv2 plugins remove: mv2 plugins install placed it (the marker
    inside it, or an install record that names it). None when it didn't. A record that can't be read counts as
    naming it: nothing of an install is removed without knowing."""
    folder = Path(path)
    if folder.is_dir() and installer.marked(folder):
        return f"placed by mv2 plugins install; remove it with mv2 plugins remove {folder.name}"
    places = []
    if folder.parent == installer.Place().plugins.resolve():
        places.append(installer.Place())
    if folder.parent.name == "plugins":
        places.append(installer.Place(folder.parent.parent))
    for where in places:
        try:
            records = where.records()
        except installer.InstallError as error:
            return f"its install record can't be read, so it is left as it is ({error})"
        if folder.name in records:
            again = (f"mv2 -u {shlex.quote(str(where.root))} plugins remove {folder.name} --here"
                     if where.kind == "universe" else f"mv2 plugins remove {folder.name}")
            return f"placed by mv2 plugins install; remove it with {again}"
    return None


def _cache(name):
    """A memo-like file in ~/.multiverse read for pruning: (data, problem). Missing reads as {}; one that can't be
    read is left as it is, with the reason."""
    path = trust.user_directory() / name
    try:
        data = read(path)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as error:
        return None, f"can't be read ({error}), so it is left as it is"
    if not isinstance(data, dict):
        return None, "isn't a map, so it is left as it is"
    return data, None


def find(folders=()):
    """What prune would remove: {"folders", "pins": [{"path", "record", "gone"}], "memos", "copies",
    "environments", "left": [{"path", "why"}]}. With `folders`, the records of plugins at or under them, there or
    not; without, those of plugin folders that are gone. Raises trust.MapUnreadable when trusted-plugins.json is
    there but can't be read: nothing is decided without it."""
    named = set()
    for folder in folders:
        named |= _spellings(folder)

    def wanted(path):
        if named:
            return _under(path, named)
        return _gone(path) is True

    left, skipped = [], set()
    pins, kept = [], []
    for path, record in trust._records(strict=True).items():
        if not wanted(path):
            if not named and _gone(path) is None:
                left.append({"path": path, "why": "can't be checked (no permission to look), so it is left"})
            kept.append(record)
            continue
        why = _installed(path)
        if why is not None:
            left.append({"path": path, "why": why})
            skipped.add(path)
            kept.append(record)
            continue
        pins.append({"path": path, "record": record, "gone": _gone(path) is True})
    still = {record.get("fingerprint") for record in kept if isinstance(record, dict)}
    copies = []
    for item in pins:
        value = item["record"].get("fingerprint") if isinstance(item["record"], dict) else None
        if not trust.valid_fingerprint(value) or value in still:
            continue  # a damaged pin names no copy; one another pin names stays
        copy = trust.cache_directory(value)
        if not os.path.lexists(copy) or str(copy) in copies or any(item["path"] == str(copy) for item in left):
            continue
        if trust.used_lately(copy):  # perhaps a running plugin's files: left to the cache prune, as after untrust
            left.append({"path": str(copy), "why": f"used in the last {trust.UNUSED_DAYS} days, so it stays until a "
                                                   f"cache prune finds it unused that long"})
            continue
        copies.append(str(copy))
    memos = []
    data, problem = _cache(trust.FINGERPRINTS)
    if problem:
        left.append({"path": str(trust.user_directory() / trust.FINGERPRINTS), "why": problem})
    pinned = {item["path"] for item in pins}
    going = set()
    for copy in copies:  # a copy's memo is keyed by where it resolves
        going |= _spellings(copy)
    for key in data or {}:
        if key.startswith(trust.COPY_MEMO):  # a cached copy's own memo: it goes with its copy, and only then
            if key[len(trust.COPY_MEMO):] in going:
                memos.append(key)
        elif key not in skipped and wanted(key) and (key in pinned or _installed(key) is None):
            memos.append(key)
    environments = []
    data, problem = _cache(loader.INSTALLED)
    if problem:
        left.append({"path": str(trust.user_directory() / loader.INSTALLED), "why": problem})
    found = (data or {}).get("environments")
    for executable in found if isinstance(found, dict) else {}:
        if wanted(executable):
            environments.append(executable)
    return {"folders": sorted(named), "pins": pins, "memos": memos, "copies": copies,
            "environments": environments, "left": left}


def empty(plan):
    return not any(plan[part] for part in ("pins", "memos", "copies", "environments"))


def remove(plan):
    """Remove what `plan` (from find) listed, under the lock each file's writers take. A pin goes only if it is
    still as listed, and a copy only if no pin names it now and nothing used it since: a pin trusted again since,
    or such a copy, is left. A memo goes only with what it describes, decided under the trust lock once the pins
    and copies are: a folder's once no pin names that folder, a copy's once that copy is gone; one left with its
    pin or copy saves hashing it again. Package scans go whenever they are still there. Returns what was removed
    and what was left."""
    done = {"pins": [], "memos": [], "copies": [], "environments": [], "left": []}
    home = trust.user_directory()
    with lock(home / ".trusted-plugins.lock"):
        records = trust._records(strict=True)
        for item in plan["pins"]:
            if item["path"] in records and records[item["path"]] == item["record"]:
                del records[item["path"]]
                done["pins"].append(item["path"])
            elif item["path"] in records:
                done["left"].append({"path": item["path"], "why": "trusted again since it was listed, so it is left"})
        if done["pins"]:
            write(home / trust.FILE, {"schema": 1, "plugins": records})
        named = {record.get("fingerprint") for record in records.values() if isinstance(record, dict)}
        for copy in plan["copies"]:
            if "sha256:" + Path(copy).name in named:
                done["left"].append({"path": copy, "why": "a plugin trusted since uses it, so it is left"})
                continue
            if trust.used_lately(copy):
                done["left"].append({"path": copy, "why": "used since it was listed, so it stays until a cache "
                                                          "prune finds it unused"})
                continue
            try:
                if not stat.S_ISDIR(os.lstat(copy).st_mode):
                    done["left"].append({"path": copy, "why": "isn't a folder, so it is left"})
                    continue
                shutil.rmtree(copy)
                done["copies"].append(copy)
            except FileNotFoundError:
                continue
            except OSError as error:
                done["left"].append({"path": copy, "why": f"couldn't be removed ({error})"})
        if plan["memos"]:  # inside the trust lock, so no pin comes or goes while its memo is decided
            _drop(home / trust.FINGERPRINTS, ".plugin-fingerprints.lock", None, plan, "memos", done,
                  lambda key: os.path.lexists(key[len(trust.COPY_MEMO):]) if key.startswith(trust.COPY_MEMO)
                  else key in records)
    if plan["environments"]:
        _drop(home / loader.INSTALLED, f".{loader.INSTALLED}.lock", "environments", plan, "environments", done,
              lambda key: False)
    return done


def _drop(path, lock_name, key, plan, part, done, still_needed):
    """Remove the entries `plan` lists under `part` from the map at `path` (under `key` in it, if any), under its
    writers' lock, each only if `still_needed(entry)` is false."""
    with lock(path.with_name(lock_name)):
        data, problem = _cache(path.name)
        if problem:
            done["left"].append({"path": str(path), "why": problem})
            return
        entries = data.get(key) if key else data
        if not isinstance(entries, dict):
            return
        for entry in plan[part]:
            if not still_needed(entry) and entries.pop(entry, None) is not None:
                done[part].append(entry)
        if done[part]:
            write(path, data)
