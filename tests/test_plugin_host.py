"""The plugin host: checking and starting it never races itself, its workers' exits are seen and budgeted, and a
worker never outlives its supervisor (PR #20 reviews 4 and 5)."""
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import host
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import now, write

SOURCE = Path(__file__).resolve().parents[1] / "src"


class HostStartTest(PluginTestCase):
    def status(self, **values):
        write(host.host_directory(self.root) / "status.json", {"state": "running", "pid": os.getpid(),
                                                               "updated_at": now(), **values})

    def test_running_needs_a_live_pid_and_a_recent_heartbeat(self):
        """host.running() takes no lock and needs a live pid and a fresh heartbeat, which keeps beating while the
        supervisor's loop stalls; fragile because adding the supervisor's lock to a status read is easy, and a break
        hangs the dashboard or starts a duplicate supervisor."""
        with self.subTest("a supervisor holds its lock"):
            held = host._held(self.root)
            try:
                with mock.patch.object(host.fcntl, "flock", side_effect=AssertionError("a status check took the lock")):
                    self.assertFalse(host.running(self.root))  # no heartbeat yet
                    self.status()
                    self.assertTrue(host.running(self.root))
            finally:
                held.close()
        for values in ({"updated_at": "2000-01-01T00:00:00+00:00"},  # stale: the supervisor stopped beating
                       {"pid": 2 ** 22 + 12345},  # no such process
                       {"state": "stopped"}):
            with self.subTest(**values):
                self.status(**values)
                self.assertFalse(host.running(self.root))
        with self.subTest("its loop stalls"):
            stopping, stalled, calls = threading.Event(), threading.Event(), []

            def needed(root):
                calls.append(1)
                if len(calls) == 2:
                    stalled.set()
                    stopping.wait(4)  # a slow scan, stopping workers, the hourly prune
                return {}
            with mock.patch.object(host, "needed", needed), mock.patch.object(host, "HEARTBEAT_SECONDS", 1.0), \
                    mock.patch.object(host, "SUPERVISE_SECONDS", 0.1):
                thread = threading.Thread(target=host.supervise, args=(self.root,),
                                          kwargs={"exit_when_idle": False, "stopping": stopping}, daemon=True)
                thread.start()
                self.addCleanup(thread.join, 10)
                self.addCleanup(stopping.set)
                self.assertTrue(stalled.wait(5))
                time.sleep(2.5)  # well past HEARTBEAT_SECONDS with the loop stuck
                self.assertTrue(host.running(self.root))
        stopping.set()
        thread.join(10)  # the stalled one above holds the host lock
        with self.subTest("it stops a worker that finishes its call first"):
            stopping, stopped = threading.Event(), threading.Event()
            ready = self.work / "finishing.ready"
            worker = subprocess.Popen([sys.executable, "-c", "import signal, sys, time; signal.signal(signal.SIGTERM, "
                                       "lambda *_: (time.sleep(3), exit(0))); open(sys.argv[1], 'w').close(); "
                                       "time.sleep(60)", str(ready)], start_new_session=True)
            self.addCleanup(host._reap, worker)
            self.assertTrue(_until(ready.exists, 10))  # its SIGTERM handler is in place
            with mock.patch.object(host, "needed", lambda root: {"slow": [root]}), \
                    mock.patch.object(host, "_start_worker", lambda root, name: worker), \
                    mock.patch.object(host, "HEARTBEAT_SECONDS", 1.0), mock.patch.object(host, "SUPERVISE_SECONDS", 0.1):
                thread = threading.Thread(target=lambda: (host.supervise(self.root, stopping=stopping), stopped.set()),
                                          daemon=True)
                thread.start()
                self.addCleanup(thread.join, 10)
                self.assertTrue(_until(lambda: host.running(self.root), 5))
                stopping.set()
                time.sleep(2.0)  # well past HEARTBEAT_SECONDS while its worker still finishes
                self.assertTrue(host.running(self.root), "a host stopping its workers looked stopped")
                self.assertTrue(stopped.wait(10))
                self.assertEqual(worker.returncode, 0, "the worker was killed before it finished")
                self.assertFalse(host.running(self.root))

    def test_a_second_start_that_finds_the_session_is_already_starting(self):
        """Concurrent host starts never make two supervisors or overwrite a running one's status, whether the other
        start's session or its supervisor comes first; fragile because it races tmux session creation, and it
        happens only when the dashboard and the CLI start the host at the same moment."""
        from multiverse.terminal import tmux
        path = host.host_directory(self.root) / "status.json"

        def duplicate(*arguments, **options):
            self.status()  # the first start's supervisor comes up
            raise RuntimeError("duplicate session: mv2-plugins-1234abcd")

        def quicker(*arguments, **options):
            self.status()  # the supervisor comes up and writes its status before create() even returns
            return {"socket": "s", "session": "mv2-plugins-x", "pane": "%1",
                    "server_identity": {"pid": 1234, "epoch": 5678}}
        with self.subTest("its session is there already"):
            with mock.patch.object(tmux.Tmux, "create", duplicate):
                result = host.start(self.root)
            self.assertEqual((result["started"], result["running"]), (False, True))
        path.unlink()
        with self.subTest("its supervisor wrote first"):
            with mock.patch.object(tmux.Tmux, "create", quicker):
                start = time.monotonic()
                result = host.start(self.root)
            recorded = json.loads(path.read_text())
            self.assertEqual((recorded["state"], recorded["pid"], recorded["session"]),
                             ("running", os.getpid(), "mv2-plugins-x"))
            self.assertTrue(result["running"])
            self.assertLess(time.monotonic() - start, 5)
            self.assertEqual(recorded['server_identity'], {'pid': 1234, 'epoch': 5678})
            host._write_status(self.root, now(), {}, {}, {})
            self.assertEqual(json.loads(path.read_text())['server_identity'], {'pid': 1234, 'epoch': 5678})
            calls = []
            def stop_session(backend, *args):
                calls.append((backend.identity, args))
            with mock.patch.object(host, 'running', return_value=False), mock.patch.object(tmux.Tmux, 'run', stop_session):
                host.stop(self.root)
            self.assertEqual(calls, [({'pid': 1234, 'epoch': 5678}, ('kill-session', '-t', 'mv2-plugins-x'))])
            for missing in (True, False):
                with self.subTest(legacy_host_without_identity=missing):
                    record = json.loads(path.read_text())
                    if missing:
                        record.pop('server_identity', None)
                    else:
                        record['server_identity'] = None
                    write(path, record)
                    with mock.patch.object(host, 'running', return_value=False), \
                            mock.patch.object(tmux, 'Tmux', side_effect=AssertionError('Unproven host pane targeted')) as terminal:
                        host.stop(self.root)
                    terminal.assert_not_called()
        with self.subTest("a supervisor that does not exit keeps its session"):
            ready = self.work / "stubborn.ready"
            stubborn = subprocess.Popen([sys.executable, "-c", "import signal, sys, time; signal.signal(signal.SIGTERM, "
                                         "signal.SIG_IGN); open(sys.argv[1], 'w').close(); time.sleep(60)", str(ready)])
            self.addCleanup(lambda: (stubborn.kill(), stubborn.wait()))
            self.assertTrue(_until(ready.exists, 10))  # it ignores SIGTERM now
            self.status(pid=stubborn.pid, session="mv2-plugins-x", socket="s",
                        server_identity={"pid": 1234, "epoch": 5678})
            calls = []
            with mock.patch.object(host, "STOP_SECONDS", -14.0), \
                    mock.patch.object(tmux.Tmux, "run", lambda backend, *args: calls.append(args)):
                result = host.stop(self.root)
            self.assertEqual((result["exited"], calls), (False, []), "the session of a running supervisor was removed")


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

STUCK_SERVICE = """
import os
import subprocess
import threading

def register(mv):
    def start(context):
        def run():
            child = subprocess.Popen(["sleep", "300"])
            with open(os.environ["STUCK_CHILD"], "w") as handle:
                handle.write(str(child.pid))
            child.wait()  # a call that never returns, with a child process
        threading.Thread(target=run, daemon=True).start()
    mv.service(start)
"""

DRAINING_SERVICE = """
import os
import subprocess
import time
from pathlib import Path

def register(mv):
    def start(context):
        child = subprocess.Popen(["sleep", "300"])
        Path(os.environ["ORPHAN_CHILD"]).write_text(str(child.pid))
        context.stopping.wait()
        Path(os.environ["ORPHAN_DRAINING"]).write_text("1")
        time.sleep(120)  # still finishing when its supervisor dies
    mv.service(start)
"""

SLOW_LOADER = """
import os
import time

def register(mv):
    time.sleep(1.5)  # loading takes a while
    mv.service(lambda context: context.stopping.wait())
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = _process_state(pid)  # a killed child of a process already gone may linger as a zombie until init reaps it
    return bool(state) and state != "Z"


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _process_state(pid):
    """ps's state letter for `pid` (Z for an unreaped zombie), or '' once it is gone."""
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1]


def _until(condition, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not condition():
        time.sleep(0.05)
    return condition()


def _without_waitid(test):
    """Run the rest of `test` as on macOS before Python 3.13, which has no os.waitid: the supervisor uses a kqueue."""
    if not hasattr(select, "kqueue"):
        test.skipTest("the path without os.waitid needs a kqueue")
    if hasattr(os, "waitid"):
        saved = os.waitid
        del os.waitid
        test.addCleanup(setattr, os, "waitid", saved)


class SupervisorGoneTest(PluginTestCase):
    """A worker whose supervisor died abruptly (kill -9, OOM) stops, instead of holding its service's socket."""

    def test_a_worker_stops_when_its_supervisor_is_killed(self):
        """After SIGKILL of the supervisor, also before its worker loaded the plugin, the worker and its stuck
        calls' children exit and the service socket is released, and an ended worker's children go when it is
        reaped; a break goes unnoticed because orphans pile up under the user and hold sockets the next host cannot
        bind (PR #20 review 5)."""
        log = host.host_directory(self.root) / "host.log"
        with self.subTest("killed while its services run"):
            for name, code in (("holder", HOLDER), ("stuck", STUCK_SERVICE)):
                self.write_plugin(name, {"module": "plugin", "extends": ["service"]}, {"plugin.py": code})
                self.enable(name)
            address, recorded, child_pid = self.work / "h.sock", self.work / "holder.pid", self.work / "child.pid"
            environment = {**os.environ, "HOLDER_SOCKET": str(address), "HOLDER_PID": str(recorded),
                           "STUCK_CHILD": str(child_pid), "PYTHONPATH": str(SOURCE)}
            supervisor = subprocess.Popen([sys.executable, "-m", "multiverse.plugins.host", str(self.root)],
                                          env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)  # a plain process: no tmux, no real host
            workers = []

            def cleanup():
                for pid in [supervisor.pid, *workers]:
                    _kill(pid)
                supervisor.wait(10)
            self.addCleanup(cleanup)

            def answers():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    try:
                        client.connect(str(address))
                        return True
                    except OSError:
                        return False
            self.assertTrue(_until(lambda: recorded.exists() and recorded.read_text() and answers()
                                   and child_pid.exists() and child_pid.read_text(), 60), "the services never came up")
            workers.append(int(recorded.read_text()))
            workers.append(int(child_pid.read_text()))
            supervisor.send_signal(signal.SIGKILL)
            supervisor.wait(10)
            _until(lambda: not any(_alive(pid) for pid in workers), 15)
            self.assertFalse(_alive(workers[0]), "the worker outlived its supervisor")
            self.assertFalse(answers(), "the service still holds its socket")
            self.assertFalse(_alive(workers[1]), "the stuck call's child outlived its worker")
            self.assertIn("holder: supervisor gone; stopping", log.read_text())
        with self.subTest("killed while its worker drains"):
            self.write_plugin("draining", {"module": "plugin", "extends": ["service"]}, {"plugin.py": DRAINING_SERVICE})
            self.enable("draining")
            child_pid, draining = self.work / "drain-child.pid", self.work / "draining"
            environment = {**os.environ, "ORPHAN_CHILD": str(child_pid), "ORPHAN_DRAINING": str(draining),
                           "PYTHONPATH": str(SOURCE)}
            # A supervisor stand-in: it starts the worker, says its pid, and waits to be killed.
            stand_in = subprocess.Popen([sys.executable, "-c", "import sys, time; from multiverse.plugins import host; "
                                         "print(host._start_worker(sys.argv[1], 'draining').pid, flush=True); "
                                         "time.sleep(120)", str(self.root)], env=environment, text=True,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (stand_in.kill(), stand_in.wait(10), stand_in.stdout.close()))
            worker = int(stand_in.stdout.readline())
            self.addCleanup(_kill_group, worker)
            self.assertTrue(_until(lambda: child_pid.exists() and child_pid.read_text(), 60), "its service never came up")
            child = int(child_pid.read_text())
            self.addCleanup(_kill, child)
            os.kill(worker, signal.SIGUSR1)
            self.assertTrue(_until(draining.exists, 20), "its service never began to finish")
            stand_in.kill()
            stand_in.wait(10)
            self.assertTrue(_until(lambda: not _alive(worker), 15), "the draining worker outlived its supervisor")
            self.assertTrue(_until(lambda: not _alive(child), 5), "a child of the draining worker outlived it")
        with self.subTest("dead before its worker loaded the plugin"):
            self.write_plugin("late", {"module": "plugin", "extends": ["service"]}, {"plugin.py": SLOW_LOADER})
            self.enable("late")
            started = self.work / "worker.pid"
            # A supervisor that starts the worker and is gone at once.
            subprocess.run([sys.executable, "-c", "import sys; from multiverse.plugins import host; "
                            "open(sys.argv[2], 'w').write(str(host._start_worker(sys.argv[1], 'late').pid))",
                            str(self.root), str(started)], env={**os.environ, "PYTHONPATH": str(SOURCE)},
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                           timeout=60)
            pid = int(started.read_text())
            self.addCleanup(_kill_group, pid)
            self.assertTrue(_until(lambda: not _alive(pid), 20), "the worker outlived the supervisor that started it")
            self.assertIn("late: supervisor gone; stopping", log.read_text())
        for waitid in (True, False):
            with self.subTest("an ended worker's stuck child goes when it is reaped", waitid=waitid):
                if not waitid:
                    _without_waitid(self)
                marker = self.work / f"child-{waitid}.pid"
                # A worker that leaves a child behind as it exits, as a hung subprocess.run in a handler would.
                script = ("import subprocess, sys; child = subprocess.Popen(['sleep', '300']); "
                          f"open({str(marker)!r}, 'w').write(str(child.pid))")
                worker = subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.addCleanup(_kill_group, worker.pid)  # whatever happens, nothing is left running
                self.assertTrue(_until(lambda: host._exited(worker), 10))
                child = int(marker.read_text())
                self.addCleanup(_kill, child)
                host._reap(worker)
                self.assertTrue(_until(lambda: not _alive(child), 2), "the child of a stuck call outlived its worker")
                self.assertIsNotNone(worker.returncode)

    def test_a_worker_started_without_its_supervisors_pid_takes_its_parent(self):
        """A new worker started by an older supervisor still running after mv2 update watches its parent; a break
        goes unnoticed because tests run matching versions, while in the field every plugin stops after each update
        (e825cd0)."""
        self.write_plugin("holder", {"module": "plugin", "extends": ["service"]}, {"plugin.py": HOLDER})
        self.enable("holder")
        address, recorded = self.work / "h.sock", self.work / "holder.pid"
        environment = {**os.environ, "HOLDER_SOCKET": str(address), "HOLDER_PID": str(recorded),
                       "PYTHONPATH": str(SOURCE)}
        worker = subprocess.Popen([sys.executable, "-m", "multiverse.plugins.host", "--worker", str(self.root),
                                   "holder"], env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: worker.poll() is None and worker.kill() or worker.wait(10))
        self.assertTrue(_until(lambda: recorded.exists() and recorded.read_text(), 60), "the service never came up")
        self.assertIsNone(worker.poll(), "it took its live parent for a dead supervisor")
        worker.terminate()
        self.assertEqual(worker.wait(20), 0)
        self.assertNotIn("supervisor gone", (host.host_directory(self.root) / "host.log").read_text())


class WorkerExitTest(PluginTestCase):
    def test_restart_budget_by_exit_kind_and_spacing(self):
        """With a fake clock, hung exits count toward a stop however far apart, hangs weeks apart never add up,
        crashes within the window stop after three, and ordinary exits far apart restart; fragile because two
        opposite bugs already happened on these windows (reviews 4 and 5), and a break restarts a hanging plugin
        forever or stops a healthy one for good."""
        clock = [0.0]
        cases = (  # (exit code, seconds between exits, exits restarted, whether the next one is left stopped)
            ("hung far apart", host.HUNG_EXIT, 3 * host.RESTART_WINDOW, host.HUNG_RESTARTS, True),
            ("hung weeks apart", host.HUNG_EXIT, 14 * 24 * 3600, 6, False),
            ("crashing", 1, 1, host.RESTARTS, True),
            ("ordinary exits far apart", 1, 3 * host.RESTART_WINDOW, 6, False),
        )
        with mock.patch.object(host.time, "monotonic", lambda: clock[0]):
            for name, code, apart, restarted, stops in cases:
                with self.subTest(name):
                    restarts, hung, stopped = {}, {}, {}
                    for _ in range(restarted):
                        clock[0] += apart
                        self.assertTrue(host._may_restart(self.root, "pair", code, restarts, hung, stopped))
                    clock[0] += apart
                    self.assertEqual(host._may_restart(self.root, "pair", code, restarts, hung, stopped), not stops)
                    if stops:
                        self.assertIn("left stopped", stopped["pair"])
                    else:
                        self.assertEqual(stopped, {})

    def test_a_worker_exit_is_seen_without_reaping_with_or_without_waitid(self):
        """A worker gets a process group of its own, and its exit is seen without reaping it, with and without
        os.waitid (the kqueue path), so its process group can still be killed; fragile because the kqueue branch
        runs only where os.waitid is missing, so a break goes unnoticed on a newer dev Python and leaves orphans
        (e825cd0)."""
        with mock.patch.object(host.subprocess, "Popen") as popen:
            host._start_worker(self.root, "pair")
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))
        for waitid in (True, False):
            with self.subTest(waitid=waitid):
                if not waitid:
                    _without_waitid(self)
                worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"],
                                          start_new_session=True)
                self.addCleanup(host._reap, worker)
                self.assertFalse(host._exited(worker))
                self.assertTrue(_until(lambda: host._exited(worker), 10))
                self.assertTrue(host._exited(worker))  # and it stays seen
                self.assertIsNone(worker.returncode)
                self.assertEqual(_process_state(worker.pid), "Z", "the check reaped the worker")
                host._reap(worker)
                self.assertEqual(worker.returncode, 0)
                # One that ended before its first check.
                ended = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
                self.addCleanup(host._reap, ended)
                _until(lambda: _process_state(ended.pid) == "Z", 10)
                self.assertTrue(host._exited(ended))
                self.assertEqual(_process_state(ended.pid), "Z", "the check reaped the worker")


SLOW_HANDLER = """
import os
import time
from pathlib import Path

def register(mv):
    def write(text):
        with Path(os.environ["SLOW_LOG"]).open("a") as out:
            out.write(f"{text} {os.getpid()}\\n")

    def first(event):
        write(f"start {event.data['n']}")
        gate, deadline = event.data.get("gate"), time.monotonic() + 30
        while gate and not Path(gate).exists() and time.monotonic() < deadline:
            time.sleep(0.05)  # in flight until the test sees the host draining
        write(f"end {event.data['n']}")
    mv.events.on("agent.reported", first)
    mv.events.on("agent.reported", lambda event: write(f"second {event.data['n']}"))

    def service(context):
        context.stopping.wait()
        time.sleep(6)  # finishing a request: longer than a person's stop gives a service
        write("service done")
    mv.service(service)
"""


class RefreshTest(PluginTestCase):
    def test_update_restarts_a_host_on_the_code_from_before_and_cuts_nothing_in_flight(self):
        """mv2 update's migrate step (run with the new code by old and new updaters) has a host on the code from
        before drain and run the new code in place, stops and starts one from an older mv2, and leaves a current one,
        another mv2's and safe mode alone; while draining, the worker starts no new handler call, not even another
        handler's of the same event, and finishes the one in flight, so no event is lost or repeated; fragile because
        tests always run one version, while in the field a host started before an update mixes old and new modules
        and its plugins fail until it restarts (PR #32 review)."""
        from multiverse import plugins
        from multiverse.plugins import events
        from multiverse.terminal import tmux
        from multiverse.tools import plugins as tool
        current, request = host.code(), host.host_directory(self.root) / host.RESTART
        moved = {**current, "signature": "0" * 64}
        cases = (  # (what status.json records about the host, what refresh does, its state)
            ("an older mv2's host records no code", {}, "stop and start", "restarted"),
            ("files moved, and nothing takes the request", {"code": moved}, "request", "failed"),
            ("a restart under way", {"code": moved, "restarting": True}, None, "restarting"),
            ("it runs this code", {"code": current}, None, "current"),
            ("another mv2's host", {"code": {"source": "/elsewhere/src", "signature": "1" * 64}}, None, "other"),
        )
        for name, recorded, does, state in cases:
            with self.subTest(name):
                request.unlink(missing_ok=True)
                write(host.host_directory(self.root) / "status.json", {"state": "running", "pid": os.getpid(),
                      "updated_at": now(), "started_at": "s", **recorded})
                calls = []
                with mock.patch.object(host, "stop", lambda root: calls.append("stop") or {"running": False}), \
                        mock.patch.object(host, "start", lambda root: calls.append("start") or {"running": True}), \
                        mock.patch.object(host, "RESTART_WAIT", 0.3):
                    result = host.refresh(self.root)
                self.assertEqual(result["state"], state)
                self.assertEqual(calls, ["stop", "start"] if does == "stop and start" else [])
                self.assertEqual(request.exists(), does == "request")
        request.unlink(missing_ok=True)
        outcomes = (  # (what the host's status shows after the request, what refresh says)
            ("a new start on this code", {"started_at": "new", "code": current}, "restarted"),
            ("a new start on other code", {"started_at": "new", "code": moved}, "failed"),
            ("still draining", {"started_at": "old", "code": moved, "restarting": True}, "restarting"),
            ("gone", {"started_at": "old", "code": moved, "restarting": True, "pid": 2 ** 22 + 12345}, "failed"),
        )
        for name, shown, state in outcomes:
            with self.subTest("what the restart came to", shows=name), mock.patch.object(host, "RESTART_WAIT", 0.3):
                write(host.host_directory(self.root) / "status.json",
                      {"state": "running", "pid": os.getpid(), "updated_at": now(), **shown})
                self.assertEqual(host._restart_outcome(self.root.resolve(), "old")["state"], state)
        from multiverse.runtime import process as processes

        def helper(name):  # an owned stand-in at the recorded pid: it notes a SIGTERM, then exits
            ready, signalled = self.work / f"{name}.ready", self.work / f"{name}.signalled"
            child = subprocess.Popen([sys.executable, "-c", "import pathlib, signal, sys, time\n"
                                      "signal.signal(signal.SIGTERM, lambda *_: (pathlib.Path(sys.argv[2]).touch(), "
                                      "sys.exit(0)))\npathlib.Path(sys.argv[1]).touch()\ntime.sleep(60)",
                                      str(ready), str(signalled)])
            self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(10)))
            threading.Thread(target=child.wait, daemon=True).start()  # reaped, as a host's pane would be
            self.assertTrue(_until(ready.exists, 10))
            return child, signalled
        proofs = (  # (what status.json says about the pid, what tmux shows in its pane, whether it is signalled)
            ("an older mv2's host: a pid alone, now another process's", {}, None, False),
            ("its process start is another's", {"pid_started": processes.process_started(os.getpid())}, None, False),
            ("its process start matches", "own start", None, True),
            ("an older mv2's host whose pane runs another pid", {}, "other", False),
            ("an older mv2's host whose pane is dead, showing that pid", {}, "dead", False),
            ("an older mv2's host whose pane runs that pid", {}, "same", True),
        )
        for name, extra, pane, signalled in proofs:
            with self.subTest("only a proven host is signalled", case=name):
                child, mark = helper(name.split(":")[0].replace(" ", "-").replace("'", ""))
                if extra == "own start":
                    extra = {"pid_started": processes.process_started(child.pid)}
                terminal = {"socket": "s", "session": "mv2-plugins-x", "pane": "%1",
                            "server_identity": {"pid": 1234, "epoch": 5678}} if pane else {}
                write(host.host_directory(self.root) / "status.json", {"state": "running", "pid": child.pid,
                      "updated_at": now(), "started_at": "s", **extra, **terminal})
                shown = {"same": f"0 {child.pid}", "other": f"0 {child.pid + 100000}", "dead": f"1 {child.pid}"}.get(pane)
                with mock.patch.object(host, "start", side_effect=AssertionError("started")), \
                        mock.patch.object(tmux.Tmux, "run", lambda backend, *args: shown):
                    if signalled:
                        self.assertTrue(host.stop(self.root)["exited"])
                    else:
                        result = host.refresh(self.root) if not extra.get("pid_started") else host.stop(self.root)
                        self.assertIn("could not be proven", str(result.get("reason") or result.get("refused")))
                self.assertEqual(_until(mark.exists, 3 if signalled else 0.5), signalled,
                                 "an unproven pid was signalled" if not signalled else "the proven host was not stopped")
        with self.subTest("mv2 plugins host says it runs the code from before"):
            # What a person checks after the first update from an mv2 whose updater shows no host result.
            elsewhere = {"source": "/elsewhere/src", "signature": "1" * 64}
            for recorded, stale in (({}, True), ({"code": moved}, True), ({"code": current}, False),
                                    ({"code": elsewhere}, False)):
                write(host.host_directory(self.root) / "status.json", {"state": "running", "pid": os.getpid(),
                      "updated_at": now(), "started_at": "s", **recorded})
                result = tool.run(self.root, plugin_action="host", name="status")
                said = tool.text("host", result)
                self.assertEqual((result["stale"], "runs the code from before an update" in said), (stale, stale))
                self.assertEqual("It runs the mv2 in /elsewhere/src" in said, recorded.get("code") == elsewhere)
        (host.host_directory(self.root) / "status.json").unlink()
        with self.subTest("a call past its limit is waited for"):
            self.write_plugin("calls", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
            self.enable("calls")
            loaded = host.loader.load(host.loader.entry(self.root, "calls"))
            self.assertEqual(host.loader.guarded(loaded, "h", time.sleep, 1.0, timeout=0.1, key="k").state, "timeout")
            draining, began = threading.Event(), time.monotonic()
            draining.set()
            host._finish_calls(loaded, draining, os.getppid())
            self.assertGreater(time.monotonic() - began, 0.7, "a drain left a call running past its limit")
            self.assertEqual(host.loader.running_calls(loaded), 0)
        with self.subTest("a drain the moment a worker starts waits for it to be ready"):
            self.write_plugin("early", {"module": "plugin", "extends": ["service"]},
                              {"plugin.py": "def register(mv):\n    mv.service(lambda context: context.stopping.wait())\n"})
            self.enable("early")
            worker = host._start_worker(self.root, "early")
            self.addCleanup(_kill_group, worker.pid)
            worker.send_signal(signal.SIGUSR1)  # before its interpreter is even up
            self.assertEqual(worker.wait(60), 0, "a drain ended a worker that was still starting")
            self.enable("early", on=False)
        with self.subTest("a draining worker whose supervisor is gone stops waiting for its services"):
            self.write_plugin("deaf", {"module": "plugin", "extends": ["service"]},
                              {"plugin.py": "import threading\ndef register(mv):\n"
                                            "    mv.service(lambda context: threading.Event().wait(30))\n"})
            self.enable("deaf")
            deaf = host.loader.load(host.loader.entry(self.root, "deaf"))
            services = host._Services(deaf, host.api.Plugin(deaf), self.root, "deaf", threading.Event())
            services.start([self.root])
            stopper = threading.Thread(target=services.stop, kwargs={"wait": None, "supervisor": -1}, daemon=True)
            stopper.start()
            stopper.join(5)
            self.assertFalse(stopper.is_alive(), "it waited on for a supervisor that was gone")
            self.enable("deaf", on=False)  # its service never returns: no host below may run it
        with self.subTest("a stop ends a drain that would wait forever"):
            (host.host_directory(self.root) / "status.json").unlink(missing_ok=True)  # the table's stand-in
            stopping = threading.Event()
            # A worker whose call never returns: it ignores a drain (SIGUSR1) and ends on a stop (SIGTERM).
            ready = self.work / "stuck.ready"
            worker = subprocess.Popen([sys.executable, "-c", "import signal, sys, time; signal.signal(signal.SIGUSR1, "
                                       "signal.SIG_IGN); open(sys.argv[1], 'w').close(); time.sleep(60)", str(ready)],
                                      start_new_session=True)
            self.addCleanup(host._reap, worker)
            self.assertTrue(_until(ready.exists, 10))  # it ignores a drain now
            answer = []
            with mock.patch.object(host, "needed", lambda root: {"stuck": [root]}), \
                    mock.patch.object(host, "_start_worker", lambda root, name: worker), \
                    mock.patch.object(host, "SUPERVISE_SECONDS", 0.1):
                thread = threading.Thread(target=lambda: answer.append(host.supervise(self.root, stopping=stopping)),
                                          daemon=True)
                thread.start()
                self.addCleanup(thread.join, 20)
                self.assertTrue(_until(lambda: host.running(self.root) and host.status(self.root).get("workers"), 5))
                write(request, {"for": host.status(self.root)["started_at"], "pid": os.getpid()})
                self.assertTrue(_until(lambda: host.status(self.root).get("restarting"), 5))
                time.sleep(0.5)
                self.assertTrue(thread.is_alive())  # draining, with no deadline
                stopping.set()
                thread.join(20)
            self.assertEqual(answer, [0], "a stop during a drain still restarted")
            self.assertIsNotNone(worker.returncode)
        with self.subTest("plugins are off or no host runs"), \
                mock.patch.object(host, "stop", side_effect=AssertionError("stopped")):
            with mock.patch.dict(os.environ, {"MV2_PLUGINS": "off"}):
                self.assertEqual(host.refresh(self.root)["state"], "skipped")
            (host.host_directory(self.root) / "status.json").unlink()
            self.assertEqual(host.refresh(self.root)["state"], "not_running")
            self.assertFalse(request.exists())
        with self.subTest("a real host, a handler call in flight"):
            self.write_plugin("slow", {"module": "plugin", "extends": ["events", "service"], "events": ["agent.reported"]},
                              {"plugin.py": SLOW_HANDLER})
            plugins.set_enabled(self.root, "slow", True)  # its cursor starts now, as `mv2 plugins enable` does
            log, supervisors, created = self.work / "slow.log", [], []
            environment = {**os.environ, "SLOW_LOG": str(log), "PYTHONPATH": str(SOURCE)}

            def create(backend, name, cwd, command, *arguments, **options):  # a plain process: no tmux
                supervisors.append(subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                threading.Thread(target=supervisors[-1].wait, daemon=True).start()  # reaped as tmux reaps a pane
                created.append(name)
                return {"socket": "s", "session": name, "pane": "%1", "server_identity": None}

            def cleanup():
                for supervisor in supervisors:
                    if supervisor.poll() is None:
                        supervisor.terminate()
                    supervisor.wait(30)
            self.addCleanup(cleanup)
            with mock.patch.object(tmux.Tmux, "create", create):
                self.assertTrue(host.start(self.root)["running"])
            self.assertTrue(_until(lambda: any(info["state"] == "running" for info in
                                               (host.status(self.root).get("workers") or {}).values()), 30))
            started = host.status(self.root)["started_at"]
            # A request left for an earlier supervisor never restarts this one.
            write(request, {"for": "an earlier start", "pid": supervisors[0].pid})
            self.assertTrue(_until(lambda: not request.exists(), 10))
            self.assertEqual(host.status(self.root)["started_at"], started)
            gate = self.work / "call.gate"
            events.emit(self.root, "agent.reported", {"id": "a", "name": "w"}, data={"n": 1, "gate": str(gate)})
            events.emit(self.root, "agent.reported", {"id": "a", "name": "w"}, data={"n": 2})
            self.assertTrue(_until(lambda: log.exists() and "start 1" in log.read_text(), 30))
            codes, answered = iter([moved]), []  # the host's code differs from this one's at first; then they agree
            with mock.patch.object(host, "code", lambda: next(codes, current)):
                updating = threading.Thread(target=lambda: answered.append(  # what mv2 update runs
                    tool.run(self.root, plugin_action="migrate", apply=True)), daemon=True)
                updating.start()
                self.assertTrue(_until(lambda: host.status(self.root).get("restarting"), 30), "it never began to drain")
                time.sleep(1.0)  # its workers have the drain signal (sent right after "restarting" is written)
                gate.touch()  # the call in flight ends now, while its worker drains
                updating.join(60)
            result = answered[0]
            self.assertEqual(result["host"]["state"], "restarted", result.get("host"))
            recorded = host.status(self.root)
            self.assertEqual((len(supervisors), recorded["pid"]), (1, supervisors[0].pid), "not restarted in place")
            self.assertNotEqual(recorded["started_at"], started)
            self.assertEqual((recorded["code"], recorded.get("restarting")), (current, None))
            self.assertTrue(processes.start_matches(recorded.get("pid_started"), supervisors[0].pid),
                            "the restarted host cannot prove its pid")
            self.assertEqual((recorded.get("session"), recorded.get("pane")), (created[0], "%1"), "where it runs was lost")
            self.assertTrue(_until(lambda: "second 2" in log.read_text(), 30), "the events behind it never came")
            calls = [line.split() for line in log.read_text().splitlines()]
            done = [call for call in calls if call[:2] == ["service", "done"]]
            self.assertEqual([pid for *_, pid in done], [calls[0][2]], "the draining worker's service was cut")
            calls = [call for call in calls if call not in done]
            self.assertEqual([what for what, _, _ in calls],
                             ["start", "end", "second", "start", "end", "second"], calls)
            self.assertEqual([number for _, number, _ in calls], ["1", "1", "1", "2", "2", "2"], "an event came twice")
            old, new = calls[0][2], calls[2][2]
            self.assertEqual(calls[1][2], old, "the call in flight was cut")
            self.assertNotEqual(new, old, "the draining worker started another handler's call")
            self.assertEqual({pid for _, _, pid in calls[2:]}, {new})


class RunningSourceTest(PluginTestCase):
    def test_the_host_and_a_plugins_helpers_import_the_mv2_that_started_them(self):
        """The plugin host's supervisor and workers, and a helper command a plugin starts, import the multiverse
        package of the process that started them, not one the interpreter finds itself; fragile because an
        interpreter's own path (an editable install of another checkout, a stale install after git pull) holds
        another mv2, which runs the host or a helper as other code, and nothing shows it."""
        stub = self.work / "other-mv2"
        for module in ("multiverse/__init__.py", "multiverse/plugins/__init__.py"):
            (stub / module).parent.mkdir(parents=True, exist_ok=True)
            (stub / module).write_text("")
        for module in ("multiverse/plugins/__main__.py", "multiverse/plugins/host.py"):
            (stub / module).write_text("print('the other mv2')\n")
        self.write_plugin("helping", {"module": "plugin", "extends": ["helper"]}, {"plugin.py": (
            "def register(mv):\n"
            "    mv.helper.action('hello', lambda *arguments: print('hello from helping') or 0)\n")})
        self.enable("helping")
        from multiverse.plugins import api, loader
        mv = api.Plugin(loader.load(self.entry("helping")))
        argvs = {"helper": mv.helper.command("hello", universe=self.root)}
        with mock.patch.object(subprocess, "Popen") as popen:
            host._start_worker(self.root, "helping")
        argvs["worker"] = [*popen.call_args.args[0][:-5], "--help"]  # its own options, then asked for usage only
        with mock.patch("multiverse.terminal.tmux.Tmux.create", side_effect=RuntimeError("stop here")) as create:
            with self.assertRaises(RuntimeError):
                host.start(self.root)
        argvs["supervisor"] = [*create.call_args.args[2][:-1], "--help"]
        other = {**os.environ, "PYTHONPATH": str(stub)}  # the interpreter's own path holds another mv2
        for name, argv in argvs.items():
            with self.subTest(name):
                done = subprocess.run(argv, env=other, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                      timeout=60)
                self.assertNotIn("the other mv2", done.stdout + done.stderr)
                self.assertEqual(done.returncode, 0, done.stderr[-400:])
                self.assertIn("hello from helping" if name == "helper" else "multiverse.plugins.host", done.stdout)
