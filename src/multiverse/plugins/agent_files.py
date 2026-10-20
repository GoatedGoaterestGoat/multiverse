"""Plugin writes never make an agent's folder (state/agents/<id>/) or a universe's root again.

A plugin's late write (an event handler, a service, a background build, a log line) can come after the agent
or the universe it was for was removed or moved; making the parents of what it writes would bring the old
folder back with no records, an orphan. So the folder a write must find is never created here: an agent's
folder for paths in one, else a universe's root for its plugin state (state/plugins/...) or for a root a
caller names. Below it, directories are made one level at a time, each without its parents, and files are
written without creating their directory: a folder renamed away or deleted at any moment, even between two
steps, makes the write fail (Gone) instead of reappearing. No lock is needed against a removal that renames
the folder away first: a write already inside lands with it.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile

from .. import store


class Gone(FileNotFoundError):
    """The folder a write must find is no longer there."""


class AgentGone(Gone):
    """The agent a write was for is no longer there (removed, or moved to another universe)."""


class UniverseGone(Gone):
    """The universe a write was for is no longer there (removed, or moved)."""


def agent_folder(path):
    """The state/agents/<id> folder `path` is in, or is; None when it is in none."""
    parts = Path(os.path.abspath(path)).parts
    for index in range(len(parts) - 3, -1, -1):
        if parts[index] == "state" and parts[index + 1] == "agents":
            return Path(*parts[:index + 3])
    return None


def required_folder(path, root=None):
    """(the folder a write at `path` must find, AgentGone or UniverseGone), or (None, None): an agent's folder for
    paths in one; else the universe root `root` for paths in it, or the root of a universe whose plugin state
    (state/plugins/...) `path` is in."""
    folder = agent_folder(path)
    if folder is not None:
        return folder, AgentGone
    absolute = Path(os.path.abspath(path))
    if root is not None:
        root = Path(os.path.abspath(root))
        if absolute == root or root in absolute.parents:
            return root, UniverseGone
    parts = absolute.parts
    for index in range(len(parts) - 2, -1, -1):  # state/plugins itself too (events.log lives there)
        if parts[index] == "state" and parts[index + 1] == "plugins":
            return Path(*parts[:index]), UniverseGone
    return None, None


def _gone(folder, kind=AgentGone):
    what = "agent folder" if kind is AgentGone else "universe"
    return kind(f"the {what} {folder} is gone: it was removed or moved, and nothing of it is made again")


def ensure_dir(path, root=None):
    """Directory `path` with its missing parents; below the folder a write must find (required_folder), only
    while that folder exists."""
    path = Path(os.path.abspath(path))
    folder, kind = required_folder(path, root)
    if folder is None:
        path.mkdir(parents=True, exist_ok=True)
        return path
    missing, current = [], path
    while current != folder and not current.is_dir():
        missing.append(current)
        current = current.parent
    if current == folder and not folder.is_dir():
        raise _gone(folder, kind)
    for directory in reversed(missing):
        try:
            directory.mkdir()  # never with its parents: the one above must still be there
        except FileExistsError:
            if not directory.is_dir():
                raise
        except FileNotFoundError:
            raise _gone(folder, kind) from None
    return path


def write_json(path, value, root=None):
    """store.write's atomic JSON write; below the folder a write must find, nothing is created above the file's
    directory while that folder is gone."""
    path = Path(path)
    folder, kind = required_folder(path, root)
    if folder is None:
        return store.write(path, value)
    ensure_dir(path.parent, root)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    except FileNotFoundError:
        raise _gone(folder, kind) from None
    try:
        with os.fdopen(descriptor, "w") as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except FileNotFoundError:
        raise _gone(folder, kind) from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path, root=None):
    """store.lock (exclusive, waiting); below the folder a write must find, its lock file is never made while that
    folder is gone."""
    path = Path(path)
    folder, kind = required_folder(path, root)
    if folder is None:
        with store.lock(path):
            yield
        return
    ensure_dir(path.parent, root)
    try:
        handle = path.open("a")
    except FileNotFoundError:
        raise _gone(folder, kind) from None
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
