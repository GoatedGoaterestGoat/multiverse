"""A launch's runner can attest to its own child without guest process visibility."""
import json
import os
import socket
import stat
import sys
import threading
import time
from uuid import UUID

from . import paths
from .process import valid_start


SECONDS = 0.15
LIMIT = 512
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


def _address(facts):
    launch_id = facts['launch_id']
    if str(UUID(launch_id)) != launch_id:
        raise ValueError('Listen proof requires a launch UUID')
    root = paths.recorded_directory(facts['run_directory'])
    return paths.socket_path(root / 'listen' / (launch_id + '.sock'), variable='MV2_RUN_DIR', kind='Listen')


def _request(facts):
    runner, runner_started, child, child_started = (facts.get(key) for key in PROCESSES)
    if (type(runner) is not int or runner <= 0 or type(child) is not int or child <= 0
            or not valid_start(runner_started) or not valid_start(child_started)):
        return None
    return json.dumps((facts['launch_id'], runner, runner_started, child, child_started)).encode('ascii')


def _read(client):
    deadline = time.monotonic() + SECONDS
    data = bytearray()
    while len(data) <= LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        client.settimeout(remaining)
        chunk = client.recv(LIMIT + 1 - len(data))
        if not chunk:
            return data
        data.extend(chunk)
    return None


def check(facts):
    """True only for a fresh reply about the process pair in this snapshot."""
    try:
        request = _request(facts)
        if request is None:
            return False
        path = _address(facts)
        def matches():
            info = path.lstat()
            return (stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                    and [info.st_dev, info.st_ino] == facts.get('runner_listen_socket'))
        if not matches():
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(SECONDS)
            client.connect(os.fspath(path))
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            return client.recv(1) == b'1' and matches()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


class RunnerProof:
    """One connection per check. Replies carry neither transcript nor process details."""

    def __init__(self, facts, current):
        self.path = _address(facts)
        self.current = current
        self.stopped = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.identity = None
        self.thread = threading.Thread(target=self._run, name='listen proof', daemon=True)

    def start(self):
        paths.prepare(self.path.parent.parent)
        parent = self.path.parent
        parent.mkdir(mode=0o700, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Listen socket directory must belong to this user with mode 0700')
        self.listener.bind(os.fspath(self.path))
        info = self.path.lstat()
        self.identity = info.st_dev, info.st_ino
        self.listener.listen(8)
        self.listener.settimeout(SECONDS)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with client:
                try:
                    request = _read(client)
                    if request:
                        facts = self.current()
                        if facts is not None and request == _request(facts):
                            client.sendall(b'1')
                except (OSError, ValueError, KeyError, TypeError):
                    pass

    def close(self):
        self.stopped.set()
        self.listener.close()
        if self.thread.ident is not None:
            self.thread.join(1)
            if self.thread.is_alive():
                raise TimeoutError('Listen proof is still closing')
        if self.identity is not None:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
