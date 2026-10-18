"""PR #20 review 5 follow-ups: a hang record that is shared and cleared, no stale code from a late load, journals
that start again or lose their id, and an older host reading what the new code writes."""
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import events, harness, host, loader, trust
from multiverse.plugins.testing import PluginTestCase
from test_plugin_delivery import Fixture

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
    mv.launch.contribute("claude", lambda info: None)
"""


class HangMarkTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.delay, self.release = self.work / "delay", self.work / "release"
        os.environ.update(STEADY_DELAY=str(self.delay), STEADY_RELEASE=str(self.release))
        self.addCleanup(self.release.touch)
        self.write_plugin("steady", {"module": "plugin", "extends": ["launch"]}, {"plugin.py": STEADY})
        self.enable("steady")

    def in_a_new_process(self):
        """What one `mv2 spawn` sees: nothing loaded in memory, the record of a hang on disk. (The loads started
        here before go on in their threads, still hanging, as they never would in a process that ended.)"""
        loader.reset()
        return [found.name for found in loader.active(self.root)]

    def test_a_hang_record_is_shared_and_cleared(self):
        """A plugin's hang record makes a new process skip it quickly and say why in its launch plan, and goes on
        load, on a file change or on expiry; a break goes unnoticed because the plugin is left out of every
        short-lived spawn for good while launches still succeed (PR #20 review 4 and 5)."""
        record = trust.user_directory() / loader.LOAD_TIMEOUTS
        self.delay.write_text("hang")
        with mock.patch.object(loader, "LOAD_SECONDS", 0.3), self.assertRaises(loader.PluginError):
            loader.load(self.entry("steady"))  # it hung once: recorded
        directory = str(Path(self.entry("steady").directory).resolve())
        self.assertIn(directory, json.loads(record.read_text())["plugins"])
        with self.subTest("a new process skips it quickly and says why"):
            start = time.monotonic()
            self.assertNotIn("steady", self.in_a_new_process())  # a fresh record: left out, as meant
            self.assertLess(time.monotonic() - start, loader.KNOWN_SLOW_WAIT + 0.4)
            plan = harness.launch_plan(harness.LaunchInfo(self.root, "a", "w", "spawn", "claude", "claude",
                                                          (2, 1, 300), True))
            self.assertEqual(plan.plugins["steady"]["state"], "skipped")
            self.assertIn("still loading", plan.plugins["steady"]["reason"])
            self.assertTrue(any("plugin steady was left out" in problem for problem in plan.problems), plan.problems)
        with self.subTest("changed files are waited for in full again"):
            path = Path(directory) / "plugin.py"
            text, status = path.read_text(), path.stat()
            path.write_text(text.replace("time.sleep(0.02)", "time.sleep(0.01)"))
            loader.reset()
            self.assertFalse(loader._known_slow(self.entry("steady")))
            path.write_text(text)
            os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))  # the files it hung with, as they were
            loader.reset()
            self.assertTrue(loader._known_slow(self.entry("steady")))
        with self.subTest("an expired record is waited for in full, and its load clears it"):
            self.delay.write_text("1.0")  # from now on it loads in 1 s, longer than the short wait
            data = json.loads(record.read_text())
            for item in data["plugins"].values():
                item["until"] = time.time() - 1  # the record is past its time
            record.write_text(json.dumps(data))
            self.assertIn("steady", self.in_a_new_process())  # waited for in full again, and loaded
            self.assertEqual(json.loads(record.read_text())["plugins"], {})  # its load cleared the record


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
    def test_a_late_load_never_installs_stale_code(self):
        """A late first load attempt, with or without loader.reset() since, never installs the code it started with
        nor removes a later attempt's sys.modules entries; fragile because both attempts write sys.modules and the
        generation counter, and the plugin silently runs stale code or loses modules (2e26218)."""
        for name, reset in (("edited", False), ("reset", True)):
            with self.subTest(reset=reset):
                release = self.work / f"release-{name}"
                os.environ["STEADY_RELEASE"] = str(release)
                self.addCleanup(release.touch)
                directory = self.write_plugin(name, {"module": "plugin", "extends": ["dashboard.column"]},
                                              {"plugin.py": STALE_V1})  # a developer's plugin (MV2_PLUGIN_PATH)
                self.enable(name)
                with mock.patch.object(loader, "LOAD_SECONDS", 0.3):
                    with self.assertRaises(loader.PluginError):
                        loader.load(self.entry(name))  # v1 is still in its register()
                    if reset:
                        loader.reset()  # PluginTestCase calls it between tests while such threads may still run
                    (Path(directory) / "plugin.py").write_text(FRESH_V2)
                    loaded = loader.load(self.entry(name))
                self.assertEqual((loaded.module.VERSION, sorted(loaded.registrations.columns)), ("v2", ["v2"]))
                release.touch()  # v1's load ends now, late
                time.sleep(0.3)
                now = self.entry(name)
                self.assertEqual(now.state, "loaded")
                self.assertEqual((now.loaded.module.VERSION, sorted(now.loaded.registrations.columns)),
                                 ("v2", ["v2"]))
                self.assertIs(sys.modules.get(loaded.module.__name__), loaded.module,
                              "a late attempt removed a later one's modules")


class JournalTest(Fixture):
    def test_a_lost_journal_id_is_not_a_restarted_journal(self):
        """A missing or unreadable journal id is not taken for a restarted journal, while a really removed journal
        is delivered from its start; fragile because the three look alike on disk, and handlers silently get all
        events again."""
        self.module.HOLD["test"] = threading.Event()  # slow is stuck on event 1, so fast is far ahead of the cursor
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1), mock.patch.object(host, "TIMEOUTS_PER_EVENT", 1000):
            self.emit(self.root, 100)
            self.assertTrue(self.deliver_until(lambda: len(self.seen("fast")) >= 100, seconds=10))
            for damage in ("deleted", "empty"):
                with self.subTest(journal_id=damage):
                    path = events.directory(self.root) / events.ID
                    path.unlink() if damage == "deleted" else path.write_text("")
                    events.emit(self.root, "agent.status", {"id": "a", "name": "w"})
                    for _ in range(3):
                        host._deliver(self.loaded, self.mv, self.root, "pair")
                    fast = self.seen("fast")
                    self.assertEqual(fast, list(range(1, len(fast) + 1)), f"{len(fast) - len(set(fast))} repeated")
            self.assertEqual(self.seen("fast"), list(range(1, 103)))
        with self.subTest("the journal removed while the cursor stays"):
            self.module.HOLD["test"].set()
            self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 102))
            shutil.rmtree(events.directory(self.root))  # the journal goes; state/plugins/pair/events.cursor stays
            for index in range(3):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": str(index)})
            self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 3))
            self.assertEqual(self.seen("fast")[102:], [1, 2, 3])


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
        """A host still running pre-update code reads the new cursor and sequence files and keeps its place, also
        once a journal started again; a break goes unnoticed because the old host reads ValueError as start at the
        end and skips events after mv2 update (29a7188)."""
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
        with self.subTest("a journal that started again"):
            shutil.rmtree(events.directory(self.root))
            for _ in range(3):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"})
            journal = events.journal_id(self.root)
            host._cursor_journals.clear()  # a fresh host process: what it last wrote is not remembered
            host._write_cursor(self.root, "pair", 3, journal)
            self.assertEqual(self.old_cursor(host._cursor_path(self.root, "pair")), 3)
            self.assertEqual(host._read_cursor(self.root, "pair", journal), 3)
