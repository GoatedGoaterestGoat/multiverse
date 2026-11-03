"""Local file protocol. Unix only, like tmux. No background service."""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid.uuid4())


def read(path):
    return json.loads(Path(path).read_text())


def open_regular(path, mode='rb'):
    """Open a data or lock file without waiting on a special file; verify the descriptor, not its pathname."""
    flags = {'rb': os.O_RDONLY, 'a': os.O_WRONLY | os.O_CREAT | os.O_APPEND}[mode]
    descriptor = os.open(path, flags | os.O_NONBLOCK, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, 'not a regular file', str(path))
        return os.fdopen(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise


def write(path, value):
    """Readers see the old or new complete document, never a partial write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def lock(path, *, create_parent=True, blocking=True):
    """Exclusive file lock; blocking=False raises BlockingIOError if already held."""
    path = Path(path)
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    with open_regular(path, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def existing_lock(path, *, blocking=True):
    """Lock an existing file, without recreating an agent; optionally refuse contention."""
    with path.open('r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
