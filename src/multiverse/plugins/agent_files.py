"""Plugin writes under an agent's folder (state/agents/<id>/) never create that folder.

A plugin's late write (an event handler, a service, a background build) can come after the agent was removed
or moved; making the parents of what it writes would bring the old folder back as an agent with no records,
an orphan. So an agent's folder is never created here. Below it, directories are made one level at a time,
each without its parents, and files are written without creating their directory: a folder renamed away or
deleted at any moment, even between two steps, makes the write fail (Gone) instead of reappearing. No lock
is needed against a removal that renames the folder away first: a write already inside lands with it.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile

from .. import store


class Gone(FileNotFoundError):
    """The agent whose folder a write was for is no longer there (removed, or moved to another universe)."""


def agent_folder(path):
    """The state/agents/<id> folder `path` is in, or is; None when it is in none."""
    parts = Path(os.path.abspath(path)).parts
    for index in range(len(parts) - 3, -1, -1):
        if parts[index] == "state" and parts[index + 1] == "agents":
            return Path(*parts[:index + 3])
    return None


def _gone(folder):
    return Gone(f"the agent folder {folder} is gone: it was removed or moved, and nothing of it is made again")


def ensure_dir(path):
    """Directory `path` with its missing parents; below an agent's folder, only while that folder exists."""
    path = Path(os.path.abspath(path))
    folder = agent_folder(path)
    if folder is None:
        path.mkdir(parents=True, exist_ok=True)
        return path
    missing, current = [], path
    while current != folder and not current.is_dir():
        missing.append(current)
        current = current.parent
    if current == folder and not folder.is_dir():
        raise _gone(folder)
    for directory in reversed(missing):
        try:
            directory.mkdir()  # never with its parents: the one above must still be there
        except FileExistsError:
            if not directory.is_dir():
                raise
        except FileNotFoundError:
            raise _gone(folder) from None
    return path


def write_json(path, value):
    """store.write's atomic JSON write; below an agent's folder, nothing is created above the file's directory
    while that folder is gone."""
    path = Path(path)
    folder = agent_folder(path)
    if folder is None:
        return store.write(path, value)
    ensure_dir(path.parent)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    except FileNotFoundError:
        raise _gone(folder) from None
    try:
        with os.fdopen(descriptor, "w") as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except FileNotFoundError:
        raise _gone(folder) from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path):
    """store.lock (exclusive, waiting); below an agent's folder, its lock file is never made while that folder is
    gone."""
    path = Path(path)
    folder = agent_folder(path)
    if folder is None:
        with store.lock(path):
            yield
        return
    ensure_dir(path.parent)
    try:
        handle = path.open("a")
    except FileNotFoundError:
        raise _gone(folder) from None
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
