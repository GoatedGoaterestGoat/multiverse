"""Loading a plugin has a time limit, and a plugin still loading elsewhere holds nobody up for long
(PR #20 re-check, partly fixed: register() had no limit, active() waited out another plugin's import)."""
import os
from pathlib import Path
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase

HANG = """
import os
import time

def register(mv):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not os.path.exists(os.environ["SLOW_RELEASE"]):
        time.sleep(0.02)
"""
SLOW = HANG
COLUMN = HANG + '    mv.dashboard.column("late", "Late", 4, lambda row: "x")\n'
WIDGETS = HANG + '    mv.dashboard.panel("thing", "plugin:Thing")\n    mv.dashboard.settings(screen="plugin:Screen")\n'


class LoadBudgetTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("slow", {"module": "plugin"}, {"plugin.py": SLOW})
        self.write_plugin("slow2", {"module": "plugin"}, {"plugin.py": SLOW})
        self.write_plugin("swift", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
        self.write_plugin("late", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": COLUMN})
        self.write_plugin("widgets", {"module": "plugin", "extends": ["dashboard.panel", "dashboard.settings"],
                                      "settings": {"version": 1, "defaults": "defaults.json"}},
                          {"plugin.py": WIDGETS, "defaults.json": '{"schema": 1}'})
        self.release = None
        self.addCleanup(self.settle)

    def settle(self):
        """Let every register() still hanging return, then forget it all, as a new process would."""
        if self.release is not None:
            self.release.touch()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(thread.name.startswith("plugin ")
                                                  and thread.name.endswith((": load", ": loading"))
                                                  for thread in threading.enumerate()):
            time.sleep(0.02)
        (self.home / ".multiverse" / loader.LOAD_TIMEOUTS).unlink(missing_ok=True)  # no record of earlier hangs
        loader.reset()

    def case(self, name, *plugins_on):
        """A universe of its own with `plugins_on` on, and a register() that hangs until self.release exists."""
        self.settle()
        self.release = self.work / f"release-{name}"
        os.environ["SLOW_RELEASE"] = str(self.release)
        root = self.subverse(name)
        for plugin in plugins_on:
            self.enable(plugin, root=root)
        return root

    def names(self, root, **options):
        return sorted(loaded.name for loaded in loader.active(root, **options))

    def wait_for(self, check, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.02)
        return False

    def test_a_hung_register_costs_callers_at_most_the_budget(self):
        """With a register() that hangs, active() and the dashboard's wrappers return in time for every wait value
        under one shared deadline, the plugin fails at LOAD_SECONDS and still lands once register returns;
        fragile because it is cross-thread lock and deadline code, and a freeze shows only with a slow plugin
        (PR #20 re-check)."""
        with self.subTest("failed at the limit, landing once it returns"):
            root = self.case("limit", "slow", "swift")
            with mock.patch.object(loader, "LOAD_SECONDS", 0.3):
                start = time.monotonic()
                self.assertEqual(self.names(root), ["swift"])
                self.assertLess(time.monotonic() - start, 2)
                found = loader.entry(root, "slow")
                self.assertEqual(found.state, "failed")
                self.assertIn("took longer than 0.3 s to load", found.problem)
                self.release.touch()  # a slow cold import that does finish, later
                self.assertTrue(self.wait_for(lambda: loader.entry(root, "slow").state == "loaded"),
                                loader.entry(root, "slow").problem)
            self.assertEqual(self.names(root), ["slow", "swift"])
        with self.subTest("failed at the limit while nobody waits"):
            root = self.case("background", "slow")
            with mock.patch.object(loader, "LOAD_SECONDS", 0.3):
                loader.active(root, wait=0)
                time.sleep(0.8)
                found = loader.entry(root, "slow")
            self.assertEqual(found.state, "failed")
            self.assertIn("took longer than 0.3 s to load", found.problem)
        with self.subTest("a caller that must not wait skips a plugin loading elsewhere"):
            root = self.case("elsewhere", "slow", "swift")
            other = threading.Thread(target=lambda: loader.load(loader.entry(root, "slow")), daemon=True)
            other.start()
            time.sleep(0.2)  # the other thread is inside slow's register()
            start = time.monotonic()
            self.assertEqual(self.names(root, wait=loader.REFRESH_WAIT), ["swift"])
            self.assertLess(time.monotonic() - start, 1)
            self.release.touch()
            other.join(10)
            self.assertEqual(self.names(root), ["slow", "swift"])
        with self.subTest("waiting zero never loads in the caller's thread"):
            root = self.case("zero", "slow", "swift")
            start = time.monotonic()
            # Nobody was loading them: both start now, in the background. swift may already be done by the time
            # the answer is made (then it is in it); slow, still in its register(), never is.
            self.assertNotIn("slow", self.names(root, wait=0))
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertTrue(self.wait_for(lambda: loader.entry(root, "swift").state == "loaded"))
            self.assertEqual(self.names(root, wait=0), ["swift"])
        with self.subTest("one shared deadline however many plugins still load"):
            root = self.case("shared", "slow", "slow2", "swift")
            start = time.monotonic()
            self.assertEqual(self.names(root, wait=0.4), ["swift"])
            self.assertLess(time.monotonic() - start, 0.9)  # not 0.4 s per plugin still loading
        with self.subTest("the dashboard's wrappers skip a plugin still loading"):
            root = self.case("wrappers", "late")
            other = threading.Thread(target=lambda: loader.load(loader.entry(root, "late")), daemon=True)
            other.start()
            time.sleep(0.2)
            start = time.monotonic()
            self.assertEqual(plugins.dashboard_columns(root), {})
            self.assertEqual((plugins.inspector_facts(root), plugins.dashboard_panels(root)), ([], []))
            self.assertLess(time.monotonic() - start, 1)
            self.release.touch()
            other.join(10)
            self.assertIn("late:late", plugins.dashboard_columns(root))
            loaded = loader.entry(root, "late").loaded
            self.assertIs(loader.loaded_by_package(loaded.package + ".plugin"), loaded)
            self.assertIsNone(loader.loaded_by_package("mv2_plugins.nothing_here"))
        with self.subTest("a widget and the plugins list never wait for it"):
            root = self.case("widget", "widgets")
            start = time.monotonic()
            with self.assertRaisesRegex(loader.StillLoading, "needs plugin widgets, which is still loading"):
                plugins.dashboard_panel(root, "widgets:thing", wait=loader.REFRESH_WAIT)
            [item] = [item for item in plugins.listing(root) if item["name"] == "widgets"]
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual((item["state"], item["settings"]["screen"]), ("loading", None))
            generation = loader.generation()
            self.release.touch()
            self.assertTrue(self.wait_for(lambda: loader.generation() != generation))
            self.assertEqual(plugins.dashboard_panel(root, "widgets:thing", wait=0)[1], "plugin:Thing")
            [item] = [item for item in plugins.listing(root) if item["name"] == "widgets"]
            self.assertEqual((item["state"], item["settings"]["screen"]), ("loaded", "plugin:Screen"))
