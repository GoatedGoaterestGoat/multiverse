"""A universe on disk: its directory, workspace and nested subverses.

Submodules hold the rest of what lives in a universe directory: agent records
(agents), message logs (mailbox), definitions installed from a template
(templates), what agents can look up (catalog), and moving or removing things
(relocate, trash).
"""
import os
import errno
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
import stat

from ..store import read, retry_missing, is_file_replaced, missing_records


_selected_root = ContextVar('selected_universe', default=None)


@contextmanager
def selection(root):
    """Keep nested readers at an already selected directory without caching its marker or granting authority."""
    if root is None:
        yield
        return
    token = _selected_root.set(Path(root).resolve())
    try:
        yield
    finally:
        _selected_root.reset(token)


def slug(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        raise ValueError("Use 1–64 letters, numbers, underscores or hyphens for names")
    return name


class UniverseNotFoundError(ValueError):
    """No universe exists within the requested selection or discovery scope."""


def resolve(path=None, *, search_parents=True):
    return resolution(path, search_parents=search_parents)[0]


def resolution(path=None, observe=None, *, search_parents=True):
    """Resolve with one missing-marker batch; an exact selection checks only the path and its .multiverse."""
    if path == '' and not search_parents:
        raise UniverseNotFoundError('Universe path must not be empty; omit -u to use automatic discovery')
    path = Path(path if path is not None else (os.environ.get("MV2_UNIVERSE") or Path.cwd())).resolve()
    selected_here = path == _selected_root.get()
    if selected_here:
        search_parents = False
    candidates = []
    found = False
    for directory in (path, *path.parents) if search_parents else (path,):
        for candidate in (directory,) if selected_here else (directory, directory / '.multiverse'):
            candidates.append(candidate)
            try:
                found = stat.S_ISREG((candidate / 'universe.json').stat().st_mode)
            except OSError as error:
                if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP):
                    continue
                raise
            if found:
                break
        if found:
            break
    extra = observe(candidates[-1]) if found and observe is not None else ()
    absent = missing_records([*(candidate / 'universe.json' for candidate in candidates), *extra])
    for candidate in candidates:
        if candidate / 'universe.json' not in absent and is_file_replaced(candidate / 'universe.json'):
            if retry_missing(lambda: read(candidate / 'universe.json')).get('schema') != 1:
                raise ValueError(f"Unsupported universe schema: {candidate}")
            return candidate, absent
    if not search_parents:
        raise UniverseNotFoundError(f"Not a universe or workspace containing .multiverse: {path}")
    raise UniverseNotFoundError(f"No universe found at or above {path}; run mv2 init")


def workspace(root):
    return (root / retry_missing(lambda: read(root / "universe.json"))["workspace"]).resolve()


def children(root):
    candidates = sorted(path for path in (root / "subverses").glob("*") if path.is_dir())
    # Every topology walk observes these markers afresh, in shared rounds rather than one wait per folder.
    absent = missing_records(path / 'universe.json' for path in candidates)
    return [path for path in candidates
            if path / 'universe.json' not in absent and is_file_replaced(path / 'universe.json')]


def tree(root):
    yield root
    for child in children(root):
        yield from tree(child)


def parent(root):
    candidate = root.parent.parent
    return candidate if root.parent.name == "subverses" and is_file_replaced(candidate / "universe.json") else None


def exact(value, root=None):
    """A universe directory named exactly, never an ancestor found by searching.

    Accepts a universe directory, a workspace containing `.multiverse`, or,
    when `root` is given, a subverse path such as `cad-lab/verification`
    relative to it. Operations below take exact paths; callers resolve names.
    """
    path = Path(value).expanduser()
    for candidate in (path, path / ".multiverse"):
        if is_file_replaced(candidate / "universe.json"):
            return candidate.resolve()
    if root is not None and not path.is_absolute():
        nested = Path(root)
        for part in path.parts:
            try:
                nested = nested / "subverses" / slug(part)
            except ValueError:
                raise ValueError(f"Not a universe or subverse: {value}") from None
        if is_file_replaced(nested / "universe.json"):
            return nested.resolve()
    raise ValueError(f"Not a universe or subverse: {value}")
