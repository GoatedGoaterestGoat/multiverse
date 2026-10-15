"""Loading a plugin has a time limit, and a plugin still loading elsewhere holds nobody up for long
(PR #20 re-check, partly fixed: register() had no limit, active() waited out another plugin's import)."""
import os
from pathlib import Path
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase

SLOW = """
import os
import time

def register(mv):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not os.path.exists(os.environ["SLOW_RELEASE"]):
        time.sleep(0.02)
"""


class LoadBudgetTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.release = self.work / "release"
        os.environ["SLOW_RELEASE"] = str(self.release)
        self.addCleanup(self.release.touch)
        self.write_plugin("slow", {"module": "plugin"}, {"plugin.py": SLOW})
        self.write_plugin("swift", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
        self.enable("slow")
        self.enable("swift")

    def test_a_plugin_that_never_finishes_loading_is_left_out_in_time(self):
        with mock.patch.object(loader, "LOAD_SECONDS", 0.3):
            start = time.monotonic()
            names = [loaded.name for loaded in loader.active(self.root)]
            self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(names, ["swift"])
        found = loader.entry(self.root, "slow")
        self.assertEqual(found.state, "failed")
        self.assertIn("took longer than 0.3 s to load", found.problem)

    def test_a_caller_that_must_not_wait_skips_a_plugin_loading_elsewhere(self):
        other = threading.Thread(target=lambda: loader.load(loader.entry(self.root, "slow")), daemon=True)
        other.start()
        time.sleep(0.2)  # the other thread is inside slow's register()
        start = time.monotonic()
        names = [loaded.name for loaded in loader.active(self.root, wait=0)]
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(names, ["swift"])
        self.release.touch()
        other.join(10)
        self.assertEqual(sorted(loaded.name for loaded in loader.active(self.root)), ["slow", "swift"])


COLUMN = """
import os
import time

def register(mv):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not os.path.exists(os.environ["SLOW_RELEASE"]):
        time.sleep(0.02)
    mv.dashboard.column("late", "Late", 4, lambda row: "x")
"""


class DashboardNeverWaitsTest(PluginTestCase):
    """The dashboard's wrappers never wait for a plugin another thread is loading; it shows on the next refresh."""

    def test_dashboard_wrappers_skip_a_plugin_still_loading(self):
        from multiverse import plugins
        release = self.work / "release"
        os.environ["SLOW_RELEASE"] = str(release)
        self.addCleanup(release.touch)
        self.write_plugin("late", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": COLUMN})
        self.enable("late")
        other = threading.Thread(target=lambda: loader.load(loader.entry(self.root, "late")), daemon=True)
        other.start()
        time.sleep(0.2)
        start = time.monotonic()
        self.assertEqual(plugins.dashboard_columns(self.root), {})
        self.assertEqual((plugins.inspector_facts(self.root), plugins.dashboard_panels(self.root)), ([], []))
        self.assertLess(time.monotonic() - start, 1)
        release.touch()
        other.join(10)
        self.assertIn("late:late", plugins.dashboard_columns(self.root))
        loaded = loader.entry(self.root, "late").loaded
        self.assertIs(loader.loaded_by_package(loaded.package + ".plugin"), loaded)
        self.assertIsNone(loader.loaded_by_package("mv2_plugins.nothing_here"))
