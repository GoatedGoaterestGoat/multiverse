"""Ask the owned runner about its child through a private launch-local file."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
from uuid import UUID, uuid4

from . import launch_files
from .process import valid_start


NAME = '.mv2-listen-proof'
SECONDS = 0.3
POLL_SECONDS = 0.05
RUNNER_POLL_SECONDS = 0.1  # Leave time for another runner poll and a listener read.
LIMIT = 512
NONCE_END = 17
FLAGS = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW
PROCESSES = ('runner_pid', 'runner_started', 'pid', 'pid_started')


def pid_namespace():
    if not sys.platform.startswith('linux'):
        return None
    try:
        info = os.stat('/proc/self/ns/pid')
    except OSError:
        return None
    return [info.st_dev, info.st_ino]


def direct_checks_ok(facts):
    if not sys.platform.startswith('linux'):
        return True
    own = pid_namespace()
    return own is not None and own == facts.get('runner_pid_namespace')


def _open_launch(directory, launch_id):
    if not isinstance(launch_id, str) or str(UUID(launch_id)) != launch_id:
        raise ValueError('Listen proof requires a launch UUID')
    directory = Path(directory)
    return launch_files._open_launch_root(directory / 'launches' / launch_id, agent_dir=directory)


def _request_scope(facts):
    runner, runner_started, child, child_started = (facts.get(key) for key in PROCESSES)
    if (type(runner) is not int or runner <= 0 or type(child) is not int or child <= 0
            or not valid_start(runner_started) or not valid_start(child_started)):
        return None
    return json.dumps((facts['launch_id'], runner, runner_started, child, child_started)).encode('ascii')


def _identity(info):
    return [info.st_dev, info.st_ino]


def _private_file(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)


def _current_file(launch_fd, identity):
    info = os.stat(NAME, dir_fd=launch_fd, follow_symlinks=False)
    return _private_file(info) and _identity(info) == identity


@contextmanager
def _locked(fd):
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read(fd):
    return os.pread(fd, LIMIT + 1, 0)


def _write(fd, data):
    if os.pwrite(fd, data, 0) != len(data):
        raise OSError('Incomplete listen proof write')
    os.ftruncate(fd, len(data))


def _pending(data, scope):
    return data.startswith(b'?') and data[NONCE_END:] == scope


def _exchange(fd, launch_fd, identity, scope):
    deadline = time.monotonic() + SECONDS
    challenge = None
    while time.monotonic() < deadline:
        try:
            with _locked(fd):
                if not _current_file(launch_fd, identity):
                    return False
                record = _read(fd)
                if challenge is not None and record == b'!' + challenge[1:]:
                    return time.monotonic() < deadline
                if challenge is None or record != challenge:
                    # Concurrent listeners can share a request that is still unanswered.
                    if _pending(record, scope):
                        challenge = record
                    else:
                        challenge = b'?' + uuid4().bytes + scope
                        _write(fd, challenge)
        except BlockingIOError:
            pass  # A busy file is an unanswered check, never proof of exit.
        left = deadline - time.monotonic()
        if left > 0:
            time.sleep(min(POLL_SECONDS, left))
    return False


def check(directory, facts):
    """True only for a fresh answer about the process pair in this snapshot."""
    try:
        identity = facts.get('runner_listen_file')
        scope = _request_scope(facts)
        if not identity or scope is None or len(scope) + NONCE_END > LIMIT:
            return False
        launch_fd = _open_launch(directory, facts['launch_id'])
        try:
            fd = os.open(NAME, FLAGS, dir_fd=launch_fd)
            try:
                info = os.fstat(fd)
                if not _private_file(info) or _identity(info) != identity:
                    return False
                if not _exchange(fd, launch_fd, identity, scope):
                    return False
                current_launch = _open_launch(directory, facts['launch_id'])
                try:
                    return (_identity(os.fstat(current_launch)) == _identity(os.fstat(launch_fd))
                            and _current_file(current_launch, identity))
                finally:
                    os.close(current_launch)
            finally:
                os.close(fd)
        finally:
            os.close(launch_fd)
    except (OSError, ValueError, KeyError, TypeError):
        return False


class RunnerProof:
    """Answer live-child checks on the file this runner created for its launch."""

    def __init__(self, directory, launch_id, current):
        self.directory, self.launch_id, self.current = directory, launch_id, current
        self.stopped = threading.Event()
        self.launch_fd = self.fd = None
        self.identity = None
        self.thread = threading.Thread(target=self._run, name='listen proof', daemon=True)

    def start(self):
        self.launch_fd = _open_launch(self.directory, self.launch_id)
        self.fd = os.open(NAME, FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.launch_fd)
        info = os.fstat(self.fd)
        self.identity = _identity(info)
        if not _private_file(info):
            raise ValueError('Listen proof must be a private regular file')
        self.thread.start()

    def _run(self):
        while not self.stopped.wait(RUNNER_POLL_SECONDS):
            try:
                with _locked(self.fd):
                    record = _read(self.fd)
                    if NONCE_END < len(record) <= LIMIT and record.startswith(b'?'):
                        facts = self.current()
                        if facts is not None and _pending(record, _request_scope(facts)):
                            _write(self.fd, b'!' + record[1:])
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def close(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join(1)
            if self.thread.is_alive():
                raise TimeoutError('Listen proof is still closing')
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.launch_fd is not None:
            try:
                if self.identity is not None:
                    info = os.stat(NAME, dir_fd=self.launch_fd, follow_symlinks=False)
                    if _identity(info) == self.identity:
                        os.unlink(NAME, dir_fd=self.launch_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(self.launch_fd)
                self.launch_fd = None
