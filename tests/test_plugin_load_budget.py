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
        names = [loaded.name for loaded in loader.active(self.root, wait=loader.REFRESH_WAIT)]
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(names, ["swift"])
        self.release.touch()
        other.join(10)
        self.assertEqual(sorted(loaded.name for loaded in loader.active(self.root)), ["slow", "swift"])


class BackgroundLoadTest(PluginTestCase):
    """A dashboard is drawn while plugins load: nothing it asks on a refresh waits out a register()."""

    def setUp(self):
        super().setUp()
        self.release = self.work / "release"
        os.environ["SLOW_RELEASE"] = str(self.release)
        self.addCleanup(self.release.touch)
        self.write_plugin("slow", {"module": "plugin"}, {"plugin.py": SLOW})
        self.write_plugin("swift", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
        self.enable("slow")
        self.enable("swift")

    def test_waiting_zero_never_loads_in_the_callers_thread(self):
        start = time.monotonic()
        # Nobody was loading them: both start now, in the background. swift may already be done by the time the
        # answer is made (then it is in it); slow, still in its register(), never is, and nothing waited for it.
        self.assertNotIn("slow", [loaded.name for loaded in loader.active(self.root, wait=0)])
        self.assertLess(time.monotonic() - start, 0.3)
        for _ in range(100):  # swift finishes on its own, in the background
            if loader.entry(self.root, "swift").state == "loaded":
                break
            time.sleep(0.02)
        self.assertEqual([loaded.name for loaded in loader.active(self.root, wait=0)], ["swift"])

    def test_one_shared_deadline_however_many_plugins_still_load(self):
        self.write_plugin("slow2", {"module": "plugin"}, {"plugin.py": SLOW})
        self.enable("slow2")
        start = time.monotonic()
        names = [loaded.name for loaded in loader.active(self.root, wait=0.4)]
        self.assertLess(time.monotonic() - start, 0.9)  # not 0.4 s per plugin still loading
        self.assertEqual(names, ["swift"])

    def test_a_register_that_never_returns_is_failed_after_the_limit_and_says_so(self):
        with mock.patch.object(loader, "LOAD_SECONDS", 0.3):
            loader.active(self.root, wait=0)
            time.sleep(0.8)
            found = loader.entry(self.root, "slow")
        self.assertEqual(found.state, "failed")
        self.assertIn("took longer than 0.3 s to load", found.problem)


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


THEME = "def register(mv):\n    mv.dashboard.theme({'accent': '#112233'})\n"


class ThemeBeforeTheFirstScreenTest(PluginTestCase):
    """The theme is applied before the dashboard opens: only the theme plugin loads then, for a bounded time."""

    def setUp(self):
        super().setUp()
        self.release = self.work / "release"
        os.environ["SLOW_RELEASE"] = str(self.release)
        self.addCleanup(self.release.touch)
        self.write_plugin("slow", {"module": "plugin"}, {"plugin.py": SLOW})
        self.write_plugin("look", {"module": "plugin", "extends": ["dashboard.theme"]}, {"plugin.py": THEME})
        self.enable("slow")
        self.enable("look")

    def choose(self, name):
        import json
        data = json.loads((self.root / "plugins.json").read_text())
        (self.root / "plugins.json").write_text(json.dumps({**data, "theme": name}))
        loader.reset()

    def test_another_plugin_still_loading_does_not_hold_up_the_theme(self):
        from multiverse import plugins
        self.choose("look")
        start = time.monotonic()
        loaded, colors = plugins.theme_colors(self.root)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual((loaded.name, colors), ("look", {"accent": "#112233"}))

    def test_a_theme_plugin_that_hangs_leaves_the_dashboard_its_own_colors_in_time(self):
        from multiverse import plugins
        self.choose("slow")
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.3):
            start = time.monotonic()
            self.assertIsNone(plugins.theme_colors(self.root))
            self.assertLess(time.monotonic() - start, 1)


WIDGETS = """
import os
import time

def register(mv):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not os.path.exists(os.environ["SLOW_RELEASE"]):
        time.sleep(0.02)
    mv.dashboard.panel("thing", "plugin:Thing")
    mv.dashboard.settings(screen="plugin:Screen")
"""


class StillLoadingOnTheDashboardTest(PluginTestCase):
    """What the dashboard reads about a plugin still loading answers at once: listed `loading`, its widget
    'still loading' (not a failure), its settings screen offered once it has loaded."""

    def setUp(self):
        super().setUp()
        self.release = self.work / "release"
        os.environ["SLOW_RELEASE"] = str(self.release)
        self.addCleanup(self.release.touch)
        self.write_plugin("slow", {"module": "plugin", "extends": ["dashboard.panel", "dashboard.settings"],
                                   "settings": {"version": 1, "defaults": "defaults.json"}},
                          {"plugin.py": WIDGETS, "defaults.json": '{"schema": 1}'})
        self.enable("slow")

    def test_a_widget_and_the_plugins_list_never_wait_for_it(self):
        from multiverse import plugins
        start = time.monotonic()
        with self.assertRaisesRegex(loader.StillLoading, "needs plugin slow, which is still loading"):
            plugins.dashboard_panel(self.root, "slow:thing", wait=loader.REFRESH_WAIT)
        [item] = plugins.listing(self.root)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual((item["state"], item["settings"]["screen"]), ("loading", None))
        generation = loader.generation()
        self.release.touch()
        for _ in range(250):
            if loader.generation() != generation:
                break
            time.sleep(0.02)
        self.assertEqual(plugins.dashboard_panel(self.root, "slow:thing", wait=0)[1], "plugin:Thing")
        [item] = plugins.listing(self.root)
        self.assertEqual((item["state"], item["settings"]["screen"]), ("loaded", "plugin:Screen"))

