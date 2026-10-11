"""Trust for plugins a universe carries in its own `plugins/` directory.

A repository can carry `.multiverse/plugins/`, and opening it must not run its
code. The user trusts such a plugin at its current content: its path and a
SHA-256 over its files go in `~/.multiverse/trusted-plugins.json`, outside every
universe. Any change to the files means trusting it again.
"""
import hashlib
from pathlib import Path

from ..store import lock, now, read, write

FILE = "trusted-plugins.json"
# Not part of what the user reviewed: caches and version control.
SKIPPED = {"__pycache__", ".git", "node_modules"}


def user_directory():
    return Path.home() / ".multiverse"


def fingerprint(directory):
    """SHA-256 over every file's relative path and bytes, in a fixed order.

    Raises ValueError for a symbolic link, as templates do: trust covers the
    plugin's own files, never something a link points to.
    """
    directory = Path(directory)
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if any(part in SKIPPED for part in relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"{path}: plugins in a universe may not contain symbolic links")
        if path.is_file():
            data = path.read_bytes()
            digest.update(relative.as_posix().encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return "sha256:" + digest.hexdigest()


def _records():
    path = user_directory() / FILE
    try:
        data = read(path)
    except (OSError, ValueError):
        return {}
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else {}


def state(directory):
    """`trusted`, `changed` (trusted at other content) or `untrusted`."""
    directory = Path(directory).resolve()
    record = _records().get(str(directory))
    if not isinstance(record, dict):
        return "untrusted"
    try:
        return "trusted" if record.get("fingerprint") == fingerprint(directory) else "changed"
    except (OSError, ValueError):
        return "changed"


def trust(directory):
    """Trust the plugin at `directory` as it is now; returns its fingerprint."""
    directory = Path(directory).resolve()
    value = fingerprint(directory)
    path = user_directory() / FILE
    with lock(user_directory() / ".trusted-plugins.lock"):
        records = _records()
        records[str(directory)] = {"fingerprint": value, "trusted_at": now()}
        write(path, {"schema": 1, "plugins": records})
    return value


def forget(directory):
    """Stop trusting the plugin at `directory`; True when it was trusted."""
    directory = Path(directory).resolve()
    with lock(user_directory() / ".trusted-plugins.lock"):
        records = _records()
        removed = records.pop(str(directory), None) is not None
        if removed:
            write(user_directory() / FILE, {"schema": 1, "plugins": records})
    return removed
