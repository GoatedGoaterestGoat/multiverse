"""A plugin call that never returns costs the dashboard one daemon thread, never its executor and never its time."""
import importlib
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write
from multiverse.ui import inspector, plugin_service
from multiverse.ui.backend import LocalBackend
from multiverse.ui.plugin_service import PluginService

PLUGIN = '''
import threading
from multiverse.plugins import api

HANG = threading.Event()

def register(mv):
    @mv.dashboard.inspector
    def stuck(row):
        HANG.wait(60)
        return "never"

    @mv.dashboard.inspector
    def fine(row):
        return "fine"

    @mv.dashboard.panel("slow")
    def slow(context):
        HANG.wait(60)
        return api.Markdown("late")

    mv.dashboard.action("Hang", lambda context: HANG.wait(60))
    mv.dashboard.action("Quick", lambda context: "quick")
'''


def plugin_threads():
    return [thread for thread in threading.enumerate() if thread.name.startswith("plugin ")]


def pool_threads():
    return [thread for thread in threading.enumerate() if "ThreadPoolExecutor" in thread.name]


class HangTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("sticky", {"module": "plugin",
                                     "extends": ["dashboard.inspector", "dashboard.action", "dashboard.panel"]},
                          {"plugin.py": PLUGIN})
        self.enable("sticky")
        self.agent("worker")
        self.service = PluginService()
        self.loaded_plugin = self.loaded("sticky")
        self.addCleanup(self.release)

    def release(self):
        importlib.import_module(f"{self.loaded_plugin.package}.plugin").HANG.set()
        for thread in plugin_threads():
            thread.join(2)

    async def test_every_dashboard_plugin_call_is_time_bounded(self):
        """Every dashboard extension call (inspector fact, palette action, panel) returns within its budget, finds a
        busy site busy instead of starting a second thread, and runs on a daemon thread, never a pool, and the
        inspector stops waiting on plugin facts at its own limit; fragile because ThreadPoolExecutor threads are joined
        at exit, so moving a call site to an executor makes the dashboard hang on quit."""
        with self.subTest("an inspector fact"):
            row = {"name": "w", "id": "i", "universe": str(self.root)}
            with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.2):
                started = time.monotonic()
                first = self.service.facts(self.root, row)
                for _ in range(8):
                    self.service.facts(self.root, row)
                elapsed = time.monotonic() - started
            self.assertEqual(first, [("sticky", "unavailable"), ("sticky", "fine")])
            self.assertLess(elapsed, 1.0)  # one budget for the first scan; the others find the call still busy
            stuck = [thread for thread in plugin_threads() if "inspector" in thread.name]
            self.assertEqual(len(stuck), 1)
            self.assertTrue(all(thread.daemon for thread in stuck))
            self.assertEqual(pool_threads(), [])
        with self.subTest("a palette action"):
            entry = next(entry for entry in self.service.registry(self.root)["actions"].values() if entry[1] == "Hang")
            with mock.patch.object(plugin_service, "ACTION_TIMEOUT", 0.3):
                with self.assertRaisesRegex(RuntimeError, "took longer than 0.3 s"):
                    self.service.run_action(self.root, entry, None)
                with self.assertRaisesRegex(RuntimeError, "still running"):
                    self.service.run_action(self.root, entry, None)
            self.assertEqual(pool_threads(), [])
            stuck = [thread for thread in plugin_threads() if "Hang" in thread.name]
            self.assertEqual(len(stuck), 1)
            self.assertTrue(stuck[0].daemon)
            quick = next(entry for entry in self.service.registry(self.root)["actions"].values() if entry[1] == "Quick")
            self.assertEqual(self.service.run_action(self.root, quick, None), "quick")
        with self.subTest("the inspector's own limit on the facts"):
            async with self.dashboard() as d:
                with mock.patch.object(d.app.plugin_service, "facts", lambda root, row: time.sleep(2.0) or []), \
                        mock.patch.object(inspector, "PLUGIN_FACTS_TIMEOUT", 0.2):
                    d.select("worker")
                    await d.press("p")
                    await d.until(lambda: d.app.query_one("#inspect-plugins").display, "the timeout line", timeout=4)
                self.assertEqual(str(d.app.query_one("#inspect-plugins").render()), "plugins: unavailable")
        with self.subTest("a panel"):
            write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name"],
                                                        "plugin_panels": "none",
                                                        "widgets": [{"type": "slow", "title": "Slow"}]})
            with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.2):
                started = time.monotonic()
                first = LocalBackend().snapshot(self.root)["widgets"]
                again = LocalBackend().snapshot(self.root)["widgets"]
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.5)
            self.assertIn("could not be shown", first[0]["text"])
            self.assertIn("could not be shown", again[0]["text"])
            stuck = [thread for thread in plugin_threads() if "panel" in thread.name]
            self.assertEqual(len(stuck), 1)
            self.assertTrue(stuck[0].daemon)
            self.assertEqual(pool_threads(), [])


if __name__ == "__main__":
    unittest.main()
