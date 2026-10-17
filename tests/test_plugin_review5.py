"""PR #20 review 5 follow-ups: a hang mark that expires, no stale code from a late load, journals that start
again or lose their id, a stuck call's children after kill -9, and the host's stop reasons and counter."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import events, host, loader, trust
from multiverse.plugins.testing import PluginTestCase
from test_plugin_delivery import Fixture

SOURCE = Path(__file__).resolve().parents[1] / "src"

# Its register() takes whatever the file STEADY_DELAY (outside the plugin, so its files stay the same) says, or
# waits on STEADY_RELEASE when it says "hang".
STEADY = """
import os
import time

def register(mv):
    delay = open(os.environ["STEADY_DELAY"]).read().strip()
    if delay == "hang":
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not os.path.exists(os.environ["STEADY_RELEASE"]):
            time.sleep(0.02)
    else:
        time.sleep(float(delay))
    mv.dashboard.column("v", "V", 3, lambda row: "x")
"""
ACTIVE = "import sys; from multiverse.plugins import loader; print([p.name for p in loader.active(sys.argv[1])])"


class HangMarkTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.delay, self.release = self.work / "delay", self.work / "release"
        os.environ.update(STEADY_DELAY=str(self.delay), STEADY_RELEASE=str(self.release))
        self.addCleanup(self.release.touch)
        self.write_plugin("steady", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": STEADY})
        self.enable("steady")

    def in_a_new_process(self):
        """What one `mv2 spawn` sees: a process of its own that ends right after it asked."""
        run = subprocess.run([sys.executable, "-c", ACTIVE, str(self.root)], capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": str(SOURCE)}, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        return run.stdout.strip()

    def test_a_hang_mark_expires_so_a_plugin_that_loads_in_two_seconds_is_not_left_out_for_good(self):
        self.delay.write_text("hang")
        with mock.patch.object(loader, "LOAD_SECONDS", 0.3), self.assertRaises(loader.PluginError):
            loader.load(self.entry("steady"))  # it hung once: marked
        self.delay.write_text("1.0")  # from now on it loads in 1 s, longer than the short wait
        self.assertNotIn("steady", self.in_a_new_process())  # a fresh mark: left out, as meant
        record = trust.user_directory() / loader.LOAD_TIMEOUTS
        data = json.loads(record.read_text())
        for item in data["plugins"].values():
            item["until"] = time.time() - 1  # the mark is past its time
        record.write_text(json.dumps(data))
        self.assertIn("steady", self.in_a_new_process())  # waited for in full again, and loaded
        self.assertEqual(json.loads(record.read_text())["plugins"], {})  # its load cleared the mark


STALE_V1 = """
import os
import time
VERSION = "v1"

def register(mv):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not os.path.exists(os.environ["STEADY_RELEASE"]):
        time.sleep(0.02)
    mv.dashboard.column("v1", "V1", 3, lambda row: VERSION)
"""
FRESH_V2 = """
VERSION = "v2"

def register(mv):
    mv.dashboard.column("v2", "V2", 3, lambda row: VERSION)
"""


class StaleLateLoadTest(PluginTestCase):
    def test_a_late_load_never_installs_the_code_it_started_with_after_an_edit(self):
        release = self.work / "release"
        os.environ["STEADY_RELEASE"] = str(release)
        self.addCleanup(release.touch)
        directory = self.write_plugin("edited", {"module": "plugin", "extends": ["dashboard.column"]},
                                      {"plugin.py": STALE_V1})  # a developer's plugin (MV2_PLUGIN_PATH)
        self.enable("edited")
        with mock.patch.object(loader, "LOAD_SECONDS", 0.5):
            with self.assertRaises(loader.PluginError):
                loader.load(self.entry("edited"))  # v1 is still in its register()
            (Path(directory) / "plugin.py").write_text(FRESH_V2)
            loaded = loader.load(self.entry("edited"))
        self.assertEqual((loaded.module.VERSION, sorted(loaded.registrations.columns)), ("v2", ["v2"]))
        release.touch()  # v1's load ends now, late
        time.sleep(0.6)
        now = self.entry("edited")
        self.assertEqual(now.state, "loaded")
        self.assertEqual((now.loaded.module.VERSION, sorted(now.loaded.registrations.columns)), ("v2", ["v2"]))


class JournalTest(Fixture):
    def test_a_journal_removed_while_the_cursor_stays_is_delivered_from_its_start(self):
        self.emit(self.root, 5)
        self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 5))
        shutil.rmtree(events.directory(self.root))  # the journal goes; state/plugins/pair/events.cursor stays
        for index in range(3):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": str(index)})
        self.assertTrue(self.deliver_until(lambda: self.seen("fast")[5:] == [1, 2, 3], seconds=5), self.seen("fast"))

    def test_a_lost_or_unreadable_journal_id_is_not_a_journal_that_started_again(self):
        self.module.HOLD["test"] = threading.Event()
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1), mock.patch.object(host, "TIMEOUTS_PER_EVENT", 1000):
            self.emit(self.root, 100)
            self.assertTrue(self.deliver_until(lambda: len(self.seen("fast")) >= 100, seconds=10))
            for damage in ("deleted", "empty"):
                path = events.directory(self.root) / events.ID
                path.unlink() if damage == "deleted" else path.write_text("")
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"})
                for _ in range(3):
                    host._deliver(self.loaded, self.mv, self.root, "pair")
        fast = self.seen("fast")
        self.assertEqual(fast, list(range(1, 103)), f"{len(fast) - len(set(fast))} repeated")


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


class OrphanedChildTest(PluginTestCase):
    def test_a_stuck_calls_child_goes_when_the_supervisor_is_killed(self):
        self.write_plugin("stuck", {"module": "plugin", "extends": ["service"]}, {"plugin.py": STUCK_SERVICE})
        self.enable("stuck")
        recorded = self.work / "child.pid"
        supervisor = subprocess.Popen([sys.executable, "-m", "multiverse.plugins.host", str(self.root)],
                                      env={**os.environ, "STUCK_CHILD": str(recorded), "PYTHONPATH": str(SOURCE)},
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (supervisor.kill(), supervisor.wait(10)))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (recorded.exists() and recorded.read_text()):
            time.sleep(0.1)
        child = int(recorded.read_text())
        self.addCleanup(_kill, child)
        os.kill(supervisor.pid, signal.SIGKILL)
        supervisor.wait(10)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and _alive(child):
            time.sleep(0.1)
        self.assertFalse(_alive(child), "the stuck call's child outlived its worker")


class HostStopReasonsTest(PluginTestCase):
    def test_mv2_plugins_and_doctor_say_the_host_left_a_plugin_stopped(self):
        self.write_plugin("pair", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                          {"plugin.py": "def register(mv):\n    mv.events.on('agent.status', lambda event: None)\n"})
        self.enable("pair")
        status = host.host_directory(self.root) / "status.json"
        status.parent.mkdir(parents=True, exist_ok=True)
        reason = "its handlers kept hanging: its worker restarted 3 times; left stopped"
        status.write_text(json.dumps({"state": "stopped", "stopped": {"pair": reason}}))
        self.assertIn(f"pair: the plugin host left it stopped: {reason}", plugins.problems(self.root))
        [item] = [item for item in plugins.listing(self.root) if item["name"] == "pair"]
        self.assertIn(f"pair: the plugin host left it stopped: {reason}", item["errors"])

    def test_hung_restarts_far_apart_never_add_up_to_a_stop(self):
        restarts, hung, stopped = {}, {}, {}
        clock = [0.0]
        with mock.patch.object(host.time, "monotonic", lambda: clock[0]):
            for _ in range(6):  # weeks apart
                clock[0] += 14 * 24 * 3600
                self.assertTrue(host._may_restart(self.root, "pair", host.HUNG_EXIT, restarts, hung, stopped))
        self.assertEqual(stopped, {})


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class OlderHostTest(Fixture):
    """A plugin host started before an update keeps running the code it started with: what the new code writes
    must read as before to it (9d599d3's readers, quoted here), so it keeps its place instead of jumping to the
    journal's end and skipping what it has not delivered."""

    @staticmethod
    def old_cursor(path):
        return int(path.read_text().strip())  # 9d599d3 host._read_cursor; ValueError meant "start at the end"

    @staticmethod
    def old_counter(base):
        return int((base / "seq").read_text().strip() or "1")  # 9d599d3 events._append and _read

    def test_an_older_host_reads_the_new_cursor_and_counter_and_keeps_its_place(self):
        self.emit(self.root, 5)
        self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 5))
        self.assertEqual(self.old_cursor(host._cursor_path(self.root, "pair")), 5)
        self.assertEqual(self.old_counter(events.directory(self.root)), 6)
        # And what an older host writes still reads right to the new code.
        host._cursor_path(self.root, "pair").write_text("3\n")
        self.assertEqual(host._read_cursor(self.root, "pair"), 3)
        (events.directory(self.root) / "seq").write_text("6\n")
        self.assertIsNotNone(events.journal_id(self.root))
        self.assertEqual(events.emit(self.root, "agent.status", {"id": "a", "name": "w"})["seq"], 6)
