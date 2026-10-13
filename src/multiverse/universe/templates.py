"""Template installs and refresh: copying definitions in, and taking template updates later."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from ..store import now, read, uid, write
from . import trash
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
# What each definition file looked like when it was installed or last refreshed,
# so a refresh can tell local edits from template updates.
MANIFEST = "template-files.json"
# Universe-owned entries that a template refresh never reads or writes.
UNMANAGED = {"universe.json", MANIFEST, "state", "trash", "subverses"}


def template_source(template):
    """An explicit directory, then ~/.multiverse/templates/NAME, then a bundled template."""
    source = Path(template).expanduser()
    if not source.is_dir():
        personal = Path.home() / ".multiverse" / "templates" / template
        source = personal if personal.is_dir() else TEMPLATES / template
    if not (source / "universe.json").is_file():
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


def record_install(directory, template, source=""):
    """Write the manifest of what a template install or refresh left in place."""
    files = {name: fingerprint(path) for name, path in definition_files(directory).items()}
    write(Path(directory) / MANIFEST, {"schema": 1, "template": str(template), "source": source, "files": files})


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
    return candidate if root.parent.name == "subverses" and (candidate / "universe.json").is_file() else None


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


def _plugins_here(root):
    """Names of the plugins found for this universe (manifests only), or None when they cannot be read."""
    try:
        from ..plugins import entries
        return {found.name for found in entries(root)}
    except Exception:  # refresh never fails over plugins
        return None


def refresh_plan(root, template=None, take=()):
    """Compare a universe's definition files with its template; change nothing.

    The manifest says what each file looked like when installed or last
    refreshed. A file that still matches it takes template updates; a locally
    edited file is kept, and a conflict when both changed. Without a manifest
    (universes installed before refresh existed), a file that differs from the
    template is `unknown` and changes only when named in `take`. A template's
    `plugin-settings/NAME.json` is `deferred` while plugin NAME is not here (a
    checkout without its submodule: only its manifest knows its pre-plugin
    settings file) and while the universe still has that plugin's pre-plugin
    settings file (until `mv2 plugins migrate`); one the universe wrote itself
    is `local`.
    """
    root = Path(root)
    meta = read(root / "universe.json")
    manifest = read(root / MANIFEST) if (root / MANIFEST).is_file() else None
    name = template or (manifest or {}).get("template") or meta.get("template")
    if not name:
        raise ValueError(f"{root} records no template; pass --template")
    base = template_source(name)
    source = base / ((manifest or {}).get("source") or "") if not template else base
    if not (source / "universe.json").is_file():
        raise ValueError(f"Template {name} has no universe at {source}")
    upstream, local = definition_files(source), definition_files(root)
    recorded = (manifest or {}).get("files", {})
    awaiting = _awaiting_migration(root)
    here = _plugins_here(root)
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
        plugin = path[len("plugin-settings/"):-len(".json")] if path.startswith("plugin-settings/") and path.endswith(".json") else None
        if plugin and here is not None and plugin not in here and mine is None and path not in take:
            # Without the plugin nothing says whether a pre-plugin file here holds its real settings.
            changes.append({"path": path, "action": "deferred",
                            "reason": f"the {plugin} plugin is not here: run mv2 plugins install {plugin} first"})
            continue
        if path in awaiting and mine is None and path not in take:
            # A template's plugin settings must not go in front of this universe's real, unmigrated ones.
            changes.append({"path": path, "action": "deferred",
                            "reason": f"{awaiting[path]} is still here: run mv2 plugins migrate first"})
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
        else:
            action, reason = "conflict", "edited locally and changed in the template"
        changes.append({"path": path, "action": action, "reason": reason})
    return {"universe": str(root), "name": meta.get("name"), "template": str(name),
            "source": str(source), "has_manifest": manifest is not None,
            "changes": changes, "current": current}


def refresh_apply(root, plan):
    """Write a plan's adds and updates; replaced files go to trash/<stamp>-refresh."""
    root, source = Path(root), Path(plan["source"])
    writes = [c for c in plan["changes"] if c["action"] in ("add", "update")]
    backup = None
    for change in writes:
        target, origin = root / change["path"], source / change["path"]
        if target.exists():
            if backup is None:
                backup = trash.entry(root, "refresh")
            saved = backup / change["path"]
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".write-", dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(origin, temp)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    if backup is not None:
        write(backup / trash.RECORD, {"kind": "refresh", "name": "refresh", "origin": str(root),
                                "files": [c["path"] for c in writes if (backup / c["path"]).exists()],
                                "removed_at": now()})
    # Files kept as they are count as local edits of the current template from
    # now on: later template changes show up as conflicts, never silent overwrites.
    kept = {c["path"] for c in plan["changes"] if c["action"] in ("unknown", "local", "conflict", "deleted")}
    previous = read(root / MANIFEST).get("files", {}) if (root / MANIFEST).is_file() else {}
    upstream = definition_files(source)
    files = {}
    for path, origin in upstream.items():
        if path in kept:
            files[path] = previous.get(path) or fingerprint(origin)
        elif (root / path).is_file():
            files[path] = fingerprint(root / path)
    for change in plan["changes"]:
        if change["action"] == "removed" and change["path"] in previous:
            files[change["path"]] = previous[change["path"]]
    relative = os.path.relpath(source, template_source(plan["template"]))
    write(root / MANIFEST, {"schema": 1, "template": plan["template"],
                            "source": "" if relative == "." else Path(relative).as_posix(), "files": files})
    _event(root, "refresh.applied", {"written": [c["path"] for c in writes]})
    return {"written": [c["path"] for c in writes], "trash": str(backup) if backup else None}
