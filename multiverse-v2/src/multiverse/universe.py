import os
from pathlib import Path
import re
import shutil
import tempfile

from .store import now, read, uid, write

TEMPLATES = Path(__file__).parent / "templates"
KINDS = ("skills", "commands", "workflows", "harnesses")


def slug(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        raise ValueError("Use 1–64 letters, numbers, underscores or hyphens for names")
    return name


def resolve(path=None):
    path = Path(path or os.environ.get("MV2_UNIVERSE", Path.cwd())).resolve()
    for directory in (path, *path.parents):
        for candidate in (directory, directory / ".multiverse"):
            if (candidate / "universe.json").is_file():
                if read(candidate / "universe.json").get("schema") != 1:
                    raise ValueError(f"Unsupported universe schema: {candidate}")
                return candidate
    raise ValueError(f"No universe found at or above {path}; run mv2 init")


def workspace(root):
    return (root / read(root / "universe.json")["workspace"]).resolve()


def create(destination, name, template="base", workdir=None):
    """Instantiate definitions only; runtime state and IDs are never cloned."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError(f"Already exists: {destination}")
    source = Path(template).expanduser()
    if not source.is_dir():
        personal = Path.home() / ".multiverse" / "templates" / template
        source = personal if personal.is_dir() else TEMPLATES / template
    if not (source / "universe.json").is_file():
        raise ValueError(f"Unknown template: {template}")
    # Templates are trusted local code, but do not follow links outside a template.
    if any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("Template symlinks are unsupported; use ordinary files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".mv2-init-", dir=destination.parent))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("state", "__pycache__", ".git"))
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
            (directory / "state" / "agents").mkdir(parents=True)
            (directory / "subverses").mkdir(exist_ok=True)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


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
