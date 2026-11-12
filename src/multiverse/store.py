"""Local file protocol. Unix only, like tmux. No background service."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time
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


# Explicit opt-in for read operations on replaced records, not for generic read() or writes.
MISSING_ATTEMPTS = 5
MISSING_BUDGET = .05
MISSING_DELAY = .005


_observed_absence = ContextVar('observed_absence', default=frozenset())


@contextmanager
def observations(absent):
    """An explicit diagnostic scope, never consulted by authority readers or writes."""
    token = _observed_absence.set(frozenset(absent))
    try:
        yield
    finally:
        _observed_absence.reset(token)


def retry_missing(operation, *, deadline=None, path=None, observed=False):
    """Retry only ENOENT during a replaced-record read, then preserve the reader's absence policy.

    Both the attempt count and retry delay are bounded. This cannot put a deadline
    on a filesystem call that itself stalls. Never pass a mutating operation.
    A known record path may corroborate absence with its parent: replacing a
    file does not remove its containing directory. Parent-directory replacement
    and filesystem availability are outside this per-record retry contract.
    """
    retry_deadline = None
    for attempt in range(MISSING_ATTEMPTS):
        try:
            return operation()
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            selected = path if path is not None else error.filename
            if observed and selected is not None and Path(selected) in _observed_absence.get():
                raise
            if selected is not None and _missing_parent(selected):
                raise
            if retry_deadline is None:
                local_deadline = time.monotonic() + MISSING_BUDGET
                retry_deadline = min(local_deadline, deadline) if deadline is not None else local_deadline
            remaining = retry_deadline - time.monotonic()
            if attempt + 1 == MISSING_ATTEMPTS or remaining <= 0:
                raise
            time.sleep(min(MISSING_DELAY, remaining))


def _missing_parent(path):
    try:
        Path(path).parent.stat()
    except OSError as error:
        return error.errno == errno.ENOENT
    return False


def missing_records(paths, *, observed=False):
    """Observe optional records together; only repeated ENOENT becomes diagnostic absence.

    This operation-local snapshot is for observation, never a substitute for a
    fresh authority read under its lock. Present and unreadable paths remain the
    normal reader's responsibility. Each round shares one retry delay.
    """
    def missing(candidates):
        pending = []
        for path in candidates:
            try:
                path.stat()
            except OSError as error:
                if error.errno == errno.ENOENT:
                    pending.append(path)
        return pending

    paths = tuple(dict.fromkeys(Path(path) for path in paths))
    known = frozenset(paths) & _observed_absence.get() if observed else frozenset()
    pending = missing(path for path in paths if path not in known)
    if not pending:
        return known
    deadline = time.monotonic() + MISSING_BUDGET
    for _ in range(1, MISSING_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(MISSING_DELAY, remaining))
        pending = missing(pending)
        if not pending:
            break
    return known | frozenset(pending)


def open_replaced(path, *, deadline=None):
    """Read an atomically replaced regular record without mistaking one missing open for absence."""
    return retry_missing(lambda: open_regular(path), deadline=deadline, path=path)


def exists_replaced(path):
    """Probe presence only when content is read elsewhere, retaining genuine absence."""
    try:
        retry_missing(Path(path).stat, path=path)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP):
            return False
        raise
    return True


def is_file_replaced(path):
    """The regular-file predicate for known replaced records; other path types keep their meaning."""
    try:
        return stat.S_ISREG(retry_missing(Path(path).stat, path=path).st_mode)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP):
            return False
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
