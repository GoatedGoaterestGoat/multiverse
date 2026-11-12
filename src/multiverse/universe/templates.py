"""Template installs and refresh: copying definitions in, and taking template updates later."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from ..store import retry_missing, is_file_replaced, exists_replaced, now, read, uid, write
from . import shipped_harnesses, trash
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
# What each definition file looked like when it was installed or last refreshed,
# so a refresh can tell local edits from template updates.
MANIFEST = "template-files.json"
# Universe-owned entries that a template refresh never reads or writes.
UNMANAGED = {"universe.json", MANIFEST, "state", "trash", "subverses"}
MISSING = object()


def mergeable(path):
    """Settings a refresh merges key by key instead of as a whole file: each harness's harness.json."""
    parts = Path(path).parts
    return len(parts) == 3 and parts[0] == "harnesses" and parts[2] == "harness.json"


# Merged as one value, never key by key: a model subset is one choice, and combining two (an alias in one, its
# canonical ID in the other) would name a model twice and refuse every launch.
WHOLE = {("settings", "models")}


def _text(path):
    try:
        return Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _text_fingerprint(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _object(text):
    try:
        value = json.loads(text) if text is not None else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _same(a, b):
    if a is MISSING or b is MISSING:
        return a is b
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _merge(base, mine, theirs, path, notes):
    """Three-way merge of JSON objects, recursing into objects; lists, WHOLE paths and other values are whole
    values. A key only the template changed is taken, one only the universe changed is kept, and one both
    changed keeps the universe's value and is noted as kept."""
    result = {}
    for key in [*mine, *(key for key in theirs if key not in mine)]:
        b, m, t = base.get(key, MISSING), mine.get(key, MISSING), theirs.get(key, MISSING)
        here = (*path, key)
        if _same(m, t) or _same(t, b):
            value = m
        elif _same(m, b):
            value = t
            notes.append((here, "added" if m is MISSING else "removed" if t is MISSING else "changed"))
        elif (isinstance(m, dict) and isinstance(t, dict) and (b is MISSING or isinstance(b, dict))
              and here not in WHOLE):
            value = _merge({} if b is MISSING else b, m, t, here, notes)
        else:
            value = m
            notes.append((here, "kept"))
        if value is not MISSING:
            result[key] = value
    return result


def _at(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


def _rebase(theirs, base, conflicts):
    """The base after a merge: the template's version, except that each conflicting key keeps its old base
    value, so it is reported again while every merged key counts from the template's version (and a later
    edit or deletion of it is the universe's own)."""
    result = json.loads(json.dumps(theirs))
    for path in conflicts:
        parent, value = _at(result, path[:-1]), _at(base, path)
        if isinstance(parent, dict):
            if value is MISSING:
                parent.pop(path[-1], None)
            else:
                parent[path[-1]] = value
    return result


def _keys(notes, *kinds):
    return ", ".join(".".join(path) for path, kind in notes if kind in kinds)


def _dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _base_for(path, recorded, bases):
    """The text a harness file's recorded fingerprint names: its saved base, else the version an earlier mv2's
    bundled template shipped (a universe from before bases were saved). None when neither matches, such as a
    template of your own: mv2 then can't tell a key you deleted from one the template added, so it merges
    nothing."""
    if not isinstance(recorded, str):  # a damaged record names no version
        return None
    saved = bases.get(path)
    if isinstance(saved, str) and _text_fingerprint(saved) == recorded:
        return saved
    return shipped_harnesses.TEXTS.get(recorded)


def _merge_change(change, mine_text, theirs_text, base_text):
    """Turn a harness file's conflict into a three-way merge from the text it is based on, where all three are
    JSON objects. The plan carries the base this refresh leaves (_base), which apply records."""
    mine, theirs, base = _object(mine_text), _object(theirs_text), _object(base_text)
    if mine is None or theirs is None or base is None:
        return
    notes = []
    merged = _merge(base, mine, theirs, (), notes)
    taken, kept = [n for n in notes if n[1] != "kept"], [n for n in notes if n[1] == "kept"]
    words = "; ".join(filter(None, (
        f"adds {_keys(taken, 'added')}" if _keys(taken, "added") else "",
        f"changes {_keys(taken, 'changed')}" if _keys(taken, "changed") else "",
        f"removes {_keys(taken, 'removed')}" if _keys(taken, "removed") else "",
        # A kept key is one you both changed: a conflict, said so before --apply.
        f"conflict on {_keys(kept, 'kept')}, yours kept" if kept else "")))
    if not _same(merged, mine):
        change.update(action="merge", reason="merges the template's changes: " + words)
        change["_text"] = _dump(merged)
    elif not kept:
        change.update(action="local", reason="edited locally; already has the template's changes")
    else:
        change["reason"] += f": conflict on {_keys(kept, 'kept')}, yours kept"
    # With no conflicting key, the template's version (exactly as merged, even if the template moves on before
    # apply) becomes the base; with conflicts, the template's version with each conflicting key at its old base
    # value, so those keep being reported and only those.
    conflicts = [path for path, _ in kept]
    change["_advance"] = True
    change["_base"] = theirs_text if not conflicts else _dump(_rebase(theirs, base, conflicts))


def template_source(template):
    """An explicit directory, then ~/.multiverse/templates/NAME, then a bundled template."""
    source = Path(template).expanduser()
    if not source.is_dir():
        personal = Path.home() / ".multiverse" / "templates" / template
        source = personal if personal.is_dir() else TEMPLATES / template
    if not (source / "universe.json").is_file():
        if source.is_dir():
            raise ValueError(f"{source} is not a template: it has no universe.json")
        raise ValueError(f"Unknown template: {template}")
    # Templates are trusted local code, but do not follow links outside a template.
    if any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("Template symlinks are unsupported; use ordinary files")
    return source


def definition_files(directory):
    """Template-managed files by relative path: never identity, state, trash or nested universes."""
    directory = Path(directory)
    files = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if (relative.parts[0] in UNMANAGED or path.is_dir()
                or any(part.startswith(".") or part == "__pycache__" for part in relative.parts)):
            continue
        files[relative.as_posix()] = path
    return files


def fingerprint(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _current(path):
    """A file's fingerprint now, None when it is absent."""
    return fingerprint(path) if is_file_replaced(path) else None


def record_install(directory, template, source=""):
    """Write the manifest of what a template install or refresh left in place, with each harness file's text as
    the base a later refresh merges from."""
    found = definition_files(directory)
    files = {name: fingerprint(path) for name, path in found.items()}
    bases = {name: text for name, path in found.items() if mergeable(name) and (text := _text(path)) is not None}
    write(Path(directory) / MANIFEST, {"schema": 1, "template": str(template), "source": source, "files": files,
                                       "bases": bases})


def create(destination, name, template="base", workdir=None):
    """Instantiate definitions only; runtime state and IDs are never cloned."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError(f"Already exists: {destination}")
    source = template_source(template)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".mv2-init-", dir=destination.parent))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("state", "trash", "__pycache__", ".git"))
        roots = [staging, *sorted((staging / "subverses").rglob("universe.json"))]
        for item in roots:
            directory = item.parent if item.name == "universe.json" else item
            meta = read(directory / "universe.json")
            meta.update(id=uid(), created_at=now(), template=str(template))
            actual = destination / directory.relative_to(staging)
            meta["workspace"] = os.path.relpath(Path(workdir or destination.parent).resolve(), actual)
            if directory == staging:
                meta["name"] = name
            write(directory / "universe.json", meta)
            record_install(directory, template, directory.relative_to(staging).as_posix().removeprefix("."))
            (directory / "state" / "agents").mkdir(parents=True)
            (directory / "subverses").mkdir(exist_ok=True)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    # Its own journal, and its parent's for a subverse.
    for scope in filter(None, (destination, _parent(destination))):
        _event(scope, "universe.created", {"name": name, "path": str(destination), "template": str(template)})
    return destination


def _parent(root):
    candidate = root.parent.parent
    return candidate if root.parent.name == "subverses" and is_file_replaced(candidate / "universe.json") else None


def _event(root, kind, data):
    """Record an event for plugins that subscribe to it; best effort, never raises."""
    try:
        from ..plugins import events
        events.emit(root, kind, None, None, data)
    except Exception:
        pass


def _awaiting_migration(root):
    """{"plugin-settings/NAME.json": legacy file} for plugins whose pre-plugin settings file is still here."""
    try:
        from ..plugins import awaiting_migration
        return awaiting_migration(root)
    except Exception:  # refresh never fails over plugins
        return {}


def refresh_plan(root, template=None, take=()):
    """Compare a universe's definition files with its template; change nothing.

    The manifest says what each file looked like when installed or last
    refreshed. A file that still matches it takes template updates; a locally
    edited file is kept, and a conflict when both changed. Without a manifest
    (universes installed before refresh existed), a file that differs from the
    template is `unknown` and changes only when named in `take`. While a
    plugin's pre-plugin settings file waits here to be converted, whatever the
    plugin's state (installed or not: core knows the optional plugins in
    plugins/known.json), the template's `plugin-settings/NAME.json` and
    `plugins.json` are `deferred`: the old file decides both when it is
    converted. A plugin settings file the universe wrote itself is `local`.
    """
    root = Path(root)
    meta = read(root / "universe.json")
    manifest = retry_missing(lambda: read(root / MANIFEST)) if is_file_replaced(root / MANIFEST) else None
    name = template or (manifest or {}).get("template") or meta.get("template")
    if not name:
        raise ValueError(f"{root} records no template; pass --template")
    try:
        base = template_source(name)
    except ValueError as error:
        if template:
            raise
        raise ValueError(f"{meta.get('name') or root.name} ({root}) was made from template {name}, which can't be "
                         f"found ({error}). Compare it with another one: mv2 -u {root} refresh --template NAME_OR_PATH") from None
    source = base / ((manifest or {}).get("source") or "") if not template else base
    if not (source / "universe.json").is_file():
        raise ValueError(f"Template {name} has no universe at {source}")
    upstream, local = definition_files(source), definition_files(root)
    recorded = (manifest or {}).get("files", {})
    bases = (manifest or {}).get("bases") if isinstance((manifest or {}).get("bases"), dict) else {}
    awaiting = _awaiting_migration(root)
    waiting = ", ".join(sorted(set(awaiting.values())))
    take = set(take)
    unknown_take = take - set(upstream)
    if unknown_take:
        raise ValueError(f"Not in the template: {', '.join(sorted(unknown_take))}")
    changes, current = [], 0
    for path in sorted(set(upstream) | set(recorded)):
        theirs = fingerprint(upstream[path]) if path in upstream else None
        mine = fingerprint(local[path]) if path in local else None
        before = recorded.get(path)
        if theirs is None:
            if mine is not None:
                changes.append({"path": path, "action": "removed", "reason": "no longer in the template; kept"})
            continue
        if mine == theirs:
            current += 1
            continue
        if path not in take and ((path in awaiting and mine is None) or (path == "plugins.json" and awaiting)):
            # A template's plugin settings and switch must not go in front of this universe's real, unconverted
            # ones: converting the old file sets both.
            changes.append({"path": path, "action": "deferred",
                            "reason": f"{waiting} is still here and not converted: run mv2 plugins migrate --apply "
                                      f"first (installing its plugin if it is not installed)"})
            continue
        if path in take:
            action, reason = ("add", "taken from the template") if mine is None else ("update", "taken from the template")
        elif manifest is None:
            action, reason = "unknown", "no install record; differs from the template"
        elif mine is None:
            action, reason = ("add", "new in the template") if before is None else ("deleted", "deleted locally; kept deleted")
        elif mine == before:
            action, reason = "update", "template changed; no local edits"
        elif theirs == before:
            action, reason = "local", "edited locally; template unchanged"
        elif before is None and path.startswith("plugin-settings/"):
            # Written here, by mv2 plugins migrate from the universe's own settings: they stay.
            action, reason = "local", "the universe's own plugin settings; kept"
        elif before is None and path == "plugins.json":
            # Written here too (a conversion sets the switch from the old file): the universe's own choice.
            action, reason = "local", "the universe's own plugin switches; kept"
        else:
            action, reason = "conflict", "edited locally and changed in the template"
        # _mine: the file this plan was made from, so apply never writes over a later edit.
        change = {"path": path, "action": action, "reason": reason, "_mine": mine}
        # A harness file you edited, whose template changed, merges from the text its recorded fingerprint names.
        # Without that (no install record, or no known text) it stays a whole-file conflict or unknown.
        if mergeable(path) and action == "conflict" and (base := _base_for(path, before, bases)) is not None:
            _merge_change(change, _text(local[path]), _text(upstream[path]), base)
        changes.append(change)
    return {"universe": str(root), "name": meta.get("name"), "template": str(name),
            "source": str(source), "has_manifest": manifest is not None,
            "changes": changes, "current": current}


def refresh_apply(root, plan):
    """Write a plan's adds and updates; replaced files go to trash/<stamp>-refresh."""
    root, source = Path(root), Path(plan["source"])
    planned = [c for c in plan["changes"] if c["action"] in ("add", "update", "merge")]
    skipped, writes, backup = [], [], None
    for change in planned:
        target, origin = root / change["path"], source / change["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".write-", dir=target.parent)
        os.close(fd)
        try:
            if change["action"] == "merge":
                Path(temp).write_bytes(change["_text"].encode("utf-8"))
            else:
                shutil.copyfile(origin, temp)
            saved = None
            if exists_replaced(target):
                if backup is None:
                    backup = trash.entry(root, "refresh")
                saved = backup / change["path"]
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            # The last step before publication: a file edited since the plan was made, even while an earlier one
            # was written or this one copied to the trash, is left as saved and reported, never written over, and
            # its trash copy dropped.
            if "_mine" in change and _current(target) != change["_mine"]:
                if saved is not None:
                    saved.unlink(missing_ok=True)
                skipped.append(change["path"])
                continue
            os.replace(temp, target)
            writes.append(change)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    if backup is not None and not any((backup / c["path"]).exists() for c in writes):
        shutil.rmtree(backup, ignore_errors=True)  # every file it held was skipped: no trash entry for nothing
        backup = None
    if backup is not None:
        write(backup / trash.RECORD, {"kind": "refresh", "name": "refresh", "origin": str(root),
                                "files": [c["path"] for c in writes if (backup / c["path"]).exists()],
                                "removed_at": now()})
    # Files kept as they are count as local edits of the current template from
    # now on: later template changes show up as conflicts, never silent overwrites.
    kept = {c["path"] for c in plan["changes"] if c["action"] in ("unknown", "local", "conflict", "deleted")}
    previous = retry_missing(lambda: read(root / MANIFEST)).get("files", {}) if is_file_replaced(root / MANIFEST) else {}
    # Only a fingerprint is a record: a damaged entry ([], {}, null, "") counts as none, so it never lends a file
    # the template's version as its base.
    previous = ({path: value for path, value in previous.items() if isinstance(value, str) and value}
                if isinstance(previous, dict) else {})
    upstream = definition_files(source)
    changes = {c["path"]: c for c in plan["changes"]}
    files = {}
    for path, origin in upstream.items():
        change = changes.get(path) or {}
        if (path not in previous and mergeable(path) and change.get("action") in ("unknown", "conflict")
                and not change.get("_advance")):
            # A harness file kept as it is, with no version mv2 knows it comes from: record none, on every apply,
            # so it stays a whole-file conflict until a write (--take) or a merge gives it one. Recording the
            # template's (or its own) would make a later merge read the template's keys as ones the universe
            # deleted (or added), and a permission flag could come back.
            continue
        if path in skipped:
            if path in previous:
                files[path] = previous[path]
        elif change.get("_advance"):
            # Merged, or already holding the template's changes: the base this plan worked out, even if the
            # template changed since.
            files[path] = _text_fingerprint(change["_base"])
        elif path in kept:
            files[path] = previous.get(path) or fingerprint(origin)
        elif is_file_replaced(root / path):
            files[path] = fingerprint(root / path)
    for path in skipped:  # left as saved, so its record stays as it was, even if its template file is gone too
        if path not in files and path in previous:
            files[path] = previous[path]
    for path, change in changes.items():
        # A planned merge whose template file is gone by now was still written: keep it on record, so the next
        # refresh reports it removed, with the version it merged as its base.
        if path not in files and path not in skipped and change.get("_advance"):
            files[path] = _text_fingerprint(change["_base"])
    for change in plan["changes"]:
        if change["action"] == "removed" and change["path"] in previous:
            files[change["path"]] = previous[change["path"]]
    # Each harness file's base is whichever text the recorded fingerprint names: the old base, the template's
    # version or the file itself; none when none of them is.
    old = retry_missing(lambda: read(root / MANIFEST)) if is_file_replaced(root / MANIFEST) else {}
    old_bases = old.get("bases", {})
    bases = {}
    for path, recorded in files.items():
        if not mergeable(path):
            continue
        candidates = ((changes.get(path) or {}).get("_base") if path not in skipped else None,
                      old_bases.get(path) if isinstance(old_bases, dict) else None,
                      _text(source / path), _text(root / path))
        for text in candidates:
            if isinstance(text, str) and _text_fingerprint(text) == recorded:
                bases[path] = text
                break
    relative = os.path.relpath(source, template_source(plan["template"]))
    write(root / MANIFEST, {"schema": 1, "template": plan["template"],
                            "source": "" if relative == "." else Path(relative).as_posix(), "files": files,
                            "bases": bases})
    _event(root, "refresh.applied", {"written": [c["path"] for c in writes], "skipped": skipped})
    return {"written": [c["path"] for c in writes], "trash": str(backup) if backup else None, "skipped": skipped}
