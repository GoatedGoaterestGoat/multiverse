"""A universe on disk: its directory, workspace and nested subverses.

Submodules hold the rest of what lives in a universe directory: agent records
(agents), message logs (mailbox), definitions installed from a template
(templates), what agents can look up (catalog), and moving or removing things
(relocate, trash).
"""
import os
from pathlib import Path
import re

from ..store import read


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


def children(root):
    return sorted(p.parent for p in (root / "subverses").glob("*/universe.json"))


def tree(root):
    yield root
    for child in children(root):
        yield from tree(child)


def parent(root):
    candidate = root.parent.parent
    return candidate if root.parent.name == "subverses" and (candidate / "universe.json").is_file() else None


def exact(value, root=None):
    """A universe directory named exactly, never an ancestor found by searching.

    Accepts a universe directory, a workspace containing `.multiverse`, or,
    when `root` is given, a subverse path such as `cad-lab/verification`
    relative to it. Operations below take exact paths; callers resolve names.
    """
    path = Path(value).expanduser()
    for candidate in (path, path / ".multiverse"):
        if (candidate / "universe.json").is_file():
            return candidate.resolve()
    if root is not None and not path.is_absolute():
        nested = Path(root)
        for part in path.parts:
            nested = nested / "subverses" / slug(part)
        if (nested / "universe.json").is_file():
            return nested.resolve()
    raise ValueError(f"Not a universe or subverse: {value}")
