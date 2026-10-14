"""Starting and checking the plugin host never races itself (PR #20 review)."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import host
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import now, write


class HostStartTest(PluginTestCase):
    def status(self, **values):
        write(host.host_directory(self.root) / "status.json", {"state": "running", "pid": os.getpid(),
                                                               "updated_at": now(), **values})

    def test_a_status_check_never_takes_the_supervisors_lock(self):
        held = host._held(self.root)  # a supervisor holds it
        self.addCleanup(held.close)
        with mock.patch.object(host.fcntl, "flock", side_effect=AssertionError("a status check took the lock")):
            self.assertFalse(host.running(self.root))  # no heartbeat yet
            self.status()
            self.assertTrue(host.running(self.root))

    def test_running_needs_a_live_pid_and_a_recent_heartbeat(self):
        self.status()
        self.assertTrue(host.running(self.root))
        self.status(updated_at="2000-01-01T00:00:00+00:00")
        self.assertFalse(host.running(self.root))  # stale: the supervisor stopped beating
        self.status(pid=2 ** 22 + 12345)
        self.assertFalse(host.running(self.root))  # no such process
        self.status(state="stopped")
        self.assertFalse(host.running(self.root))

    def test_a_second_start_that_finds_the_session_is_already_starting(self):
        from multiverse.terminal import tmux

        def duplicate(*arguments, **options):
            self.status()  # the first start's supervisor comes up
            raise RuntimeError("duplicate session: mv2-plugins-1234abcd")
        with mock.patch.object(tmux.Tmux, "create", duplicate):
            result = host.start(self.root)
        self.assertEqual((result["started"], result["running"]), (False, True))


HOLDER = """
import os
import socket

def register(mv):
    def start(context):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(os.environ["HOLDER_SOCKET"])
        server.listen(8)
        server.settimeout(0.1)
        with open(os.environ["HOLDER_PID"], "w") as out:
            out.write(str(os.getpid()))
        try:
            while not context.stopping.is_set():
                try:
                    server.accept()[0].close()
                except socket.timeout:
                    pass
        finally:
            server.close()
    mv.service(start)
"""


class SupervisorGoneTest(PluginTestCase):
    """A worker whose supervisor died abruptly (kill -9, OOM) stops, instead of holding its service's socket."""

    def test_a_worker_stops_when_its_supervisor_is_killed(self):
        import signal
        import socket
        import subprocess
        import time
        self.write_plugin("holder", {"module": "plugin", "extends": ["service"]}, {"plugin.py": HOLDER})
        self.enable("holder")
        address, recorded = self.work / "h.sock", self.work / "holder.pid"
        environment = {**os.environ, "HOLDER_SOCKET": str(address), "HOLDER_PID": str(recorded),
                       "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        supervisor = subprocess.Popen([sys.executable, "-m", "multiverse.plugins.host", str(self.root)],
                                      env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)  # a plain process: no tmux, no real host
        workers = []

        def cleanup():
            for pid in [supervisor.pid, *workers]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            supervisor.wait(10)
        self.addCleanup(cleanup)

        def answers():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                try:
                    client.connect(str(address))
                    return True
                except OSError:
                    return False

        def alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (recorded.exists() and recorded.read_text() and answers()):
            time.sleep(0.1)
        self.assertTrue(answers(), "the service never came up")
        workers.append(int(recorded.read_text()))
        supervisor.send_signal(signal.SIGKILL)
        supervisor.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and alive(workers[0]):
            time.sleep(0.1)
        self.assertFalse(alive(workers[0]), "the worker outlived its supervisor")
        self.assertFalse(answers(), "the service still holds its socket")
        log = (host.host_directory(self.root) / "host.log").read_text()
        self.assertIn("holder: supervisor gone; stopping", log)
