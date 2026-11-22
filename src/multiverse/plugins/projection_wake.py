"""Best-effort wakeups for the host's durable projection lane."""
from contextlib import contextmanager
import os
from pathlib import Path
import select
import stat

from .. import universe


def _path(root):
    return Path(root) / 'state' / 'plugins' / 'host' / 'turn-wake'


def _open(path, mode):
    if not stat.S_ISFIFO(path.lstat().st_mode):
        raise OSError('The turn wake path is not a FIFO')
    descriptor = os.open(path, mode | os.O_NONBLOCK | os.O_NOFOLLOW)
    if stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    raise OSError('The turn wake path changed while opening')


@contextmanager
def receiver(root):
    path = _path(root)
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkfifo(path, 0o600)
        except FileExistsError:
            pass
        descriptor = _open(path, os.O_RDWR)
    except OSError:
        pass  # Periodic scans also recover a missed or unavailable wake.
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def wake(root, *, tree=False):
    """Nudge an existing receiver without waiting for it or creating state."""
    try:
        if tree:
            root = Path(root)
            while (parent := universe.parent(root)) is not None:
                root = parent
        descriptor = _open(_path(root), os.O_WRONLY)
        try:
            os.write(descriptor, b'\x00')
        finally:
            os.close(descriptor)
    except OSError:
        pass


def wait(descriptor, stopping, timeout):
    if descriptor is None:
        stopping.wait(timeout)
    elif select.select([descriptor], [], [], timeout)[0]:
        try:
            os.read(descriptor, 4096)
        except BlockingIOError:
            pass
