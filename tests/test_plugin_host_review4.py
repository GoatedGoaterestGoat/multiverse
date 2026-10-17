"""PR #20 review 4, the host: siblings never stall or repeat, a worker that keeps hanging is stopped, and a stuck
call's child processes go with its worker."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import host
from test_plugin_delivery import Fixture


class SiblingsTest(Fixture):
    def setUp(self):
        super().setUp()
        self.module.HOLD["test"] = threading.Event()  # slow never returns on event 1 here
        self.patches = [mock.patch.object(host, "HANDLER_SECONDS", 0.1), mock.patch.object(host, "TIMEOUTS_PER_EVENT", 1000)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_sibling_far_ahead_of_a_stuck_handler_keeps_going(self):
        self.emit(self.root, 2600)  # more than the 4 reads of 500 one poll makes from the cursor
        self.assertTrue(self.deliver_until(lambda: len(self.seen("fast")) >= 2600, seconds=30), len(self.seen("fast")))
        self.assertEqual(self.seen("fast"), list(range(1, 2601)))

    def test_a_worker_that_starts_again_never_repeats_a_siblings_events(self):
        self.emit(self.root, 30)
        self.assertTrue(self.deliver_until(lambda: len(self.seen("fast")) >= 30, seconds=10))
        for table in (host._finished, host._timeouts, host._journals):
            table.clear()  # what a fresh worker process starts with
        for _ in range(3):
            host._deliver(self.loaded, self.mv, self.root, "pair")
        self.assertEqual(self.seen("fast"), list(range(1, 31)))  # nothing again after the restart
        self.assertEqual(host._read_cursor(self.root, "pair"), 0)  # slow is still on event 1


class HungRestartsTest(Fixture):
    def test_a_worker_restarting_itself_for_hung_calls_says_so_in_its_exit_code(self):
        host._abandoned.clear()
        self.addCleanup(host._abandoned.clear)
        self.module.HOLD["test"] = threading.Event()
        self.emit(self.root, 2)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1), mock.patch.object(host.signal, "signal"):
            self.assertEqual(host.work(self.root, "pair"), host.HUNG_EXIT)

    def test_hung_restarts_are_counted_however_far_apart_and_then_the_plugin_is_stopped(self):
        restarts, hung, stopped = {}, {}, {}
        clock = [0.0]
        with mock.patch.object(host.time, "monotonic", lambda: clock[0]):
            for attempt in range(host.HUNG_RESTARTS):
                clock[0] += 3 * host.RESTART_WINDOW  # a run at real limits is longer than the window
                self.assertTrue(host._may_restart(self.root, "pair", host.HUNG_EXIT, restarts, hung, stopped))
            clock[0] += 3 * host.RESTART_WINDOW
            self.assertFalse(host._may_restart(self.root, "pair", host.HUNG_EXIT, restarts, hung, stopped))
        self.assertIn("kept hanging", stopped["pair"])
        self.assertIn("left stopped", stopped["pair"])
        # An ordinary exit far apart from the others is still restarted, as before.
        self.assertTrue(host._may_restart(self.root, "other", 1, restarts, hung, stopped))


class ProcessGroupTest(Fixture):
    def test_workers_get_a_process_group_of_their_own(self):
        with mock.patch.object(host.subprocess, "Popen") as popen:
            host._start_worker(self.root, "pair")
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))

    def test_a_stuck_calls_child_process_goes_with_its_worker(self):
        child_pid = self.work / "child.pid"
        # A worker that leaves a child behind as it exits, as a hung subprocess.run in a handler would.
        script = ("import subprocess, sys; child = subprocess.Popen(['sleep', '300']); "
                  f"open({str(child_pid)!r}, 'w').write(str(child.pid))")
        worker = subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(_kill_group, worker.pid)  # whatever happens, nothing is left running
        deadline = time.monotonic() + 10
        while not host._exited(worker) and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(child_pid.read_text())
        self.addCleanup(lambda: _kill(pid))
        self.assertTrue(host._exited(worker))
        host._reap(worker)
        for _ in range(100):
            if not _alive(pid):
                break
            time.sleep(0.02)
        self.assertFalse(_alive(pid), "the child of a stuck call outlived its worker")
        self.assertIsNotNone(worker.returncode)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of a process already gone may linger as a zombie until init reaps it.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


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

