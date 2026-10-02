import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from .store import now, read, uid, write

TEMPLATES = Path(__file__).parent / "templates"
KINDS = ("skills", "commands", "workflows", "harnesses")
# What each definition file looked like when it was installed or last refreshed,
# so a refresh can tell local edits from template updates.
MANIFEST = "template-files.json"
# Universe-owned entries that a template refresh never reads or writes.
UNMANAGED = {"universe.json", MANIFEST, "state", "trash", "subverses"}


def slug(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        raise ValueError("Use 1–64 letters, numbers, underscores or hyphens for names")
    return name


class UniverseNotFoundError(ValueError):
    """No universe exists at the selected path or its ancestors."""


def resolve(path=None):
    path = Path(path or os.environ.get("MV2_UNIVERSE", Path.cwd())).resolve()
    for directory in (path, *path.parents):
        for candidate in (directory, directory / ".multiverse"):
            if (candidate / "universe.json").is_file():
                if read(candidate / "universe.json").get("schema") != 1:
                    raise ValueError(f"Unsupported universe schema: {candidate}")
                return candidate
    raise UniverseNotFoundError(f"No universe found at or above {path}; run mv2 init")


def workspace(root):
    return (root / read(root / "universe.json")["workspace"]).resolve()


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
    return destination


def refresh_plan(root, template=None, take=()):
    """Compare a universe's definition files with its template; change nothing.

    The manifest says what each file looked like when installed or last
    refreshed. A file that still matches it takes template updates; a locally
    edited file is kept, and a conflict when both changed. Without a manifest
    (universes installed before refresh existed), a file that differs from the
    template is `unknown` and changes only when named in `take`.
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
        else:
            action, reason = "conflict", "edited locally and changed in the template"
        changes.append({"path": path, "action": action, "reason": reason})
    return {"universe": str(root), "name": meta.get("name"), "template": str(name),
            "source": str(source), "has_manifest": manifest is not None,
            "changes": changes, "current": current}


def refresh_apply(root, plan):
    """Write a plan's adds and updates; replaced files go to trash/<stamp>-refresh."""
    from .relocate import RECORD, _entry
    root, source = Path(root), Path(plan["source"])
    writes = [c for c in plan["changes"] if c["action"] in ("add", "update")]
    backup = None
    for change in writes:
        target, origin = root / change["path"], source / change["path"]
        if target.exists():
            if backup is None:
                backup = _entry(root, "refresh")
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
        write(backup / RECORD, {"kind": "refresh", "name": "refresh", "origin": str(root),
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
    return {"written": [c["path"] for c in writes], "trash": str(backup) if backup else None}


def children(root):
    return sorted(p.parent for p in (root / "subverses").glob("*/universe.json"))


def tree(root):
    yield root
    for child in children(root):
        yield from tree(child)


def parent(root):
    candidate = root.parent.parent
    return candidate if root.parent.name == "subverses" and (candidate / "universe.json").is_file() else None


def catalog(root):
    result = []
    for kind in KINDS:
        filename = "harness.json" if kind == "harnesses" else "entry.json"
        for path in sorted((root / kind).glob(f"*/{filename}")):
            meta = read(path)
            result.append({"kind": kind, "name": path.parent.name,
                           "description": meta.get("description", ""), "path": str(path.parent)})
    return result


def command(root, query, arguments=""):
    candidates = [e for e in catalog(root) if e["kind"] in ("commands", "workflows", "skills")
                  and query in (e["name"], f'{e["kind"]}/{e["name"]}')]
    if len(candidates) != 1:
        raise ValueError(f"Expected one exact match for {query!r}; found {len(candidates)}. Use mv2 catalog.")
    entry = candidates[0]
    filename = "SKILL.md" if entry["kind"] == "skills" else "prompt.md"
    return Path(entry["path"], filename).read_text() + (f"\nUser arguments:\n{arguments}\n" if arguments else "")


def bootstrap(root, properties):
    # Only the short bootstrap and identity are automatic. Catalog bodies stay on disk.
    return (root / "bootstrap.md").read_text().strip() + "\n\n" + "\n".join([
        f'Universe: {read(root / "universe.json")["name"]} ({properties["universe_id"]})',
        f'Agent: {properties["name"]} ({properties["id"]})',
        f"Universe files: {root}",
        f'Your state: {root / "state" / "agents" / properties["id"]}',
    ]) + "\n"
