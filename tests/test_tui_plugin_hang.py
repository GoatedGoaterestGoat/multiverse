"""A plugin call that never returns costs the dashboard one daemon thread, never its executor and never its exit."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase
from multiverse.ui import plugin_service
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

    mv.dashboard.action("Hang", lambda context: HANG.wait(60))
    mv.dashboard.action("Quick", lambda context: "quick")
'''


def plugin_threads():
    return [thread for thread in threading.enumerate() if thread.name.startswith("plugin ")]


def pool_threads():
    return [thread for thread in threading.enumerate() if "ThreadPoolExecutor" in thread.name]


class HangTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("sticky", {"module": "plugin", "extends": ["dashboard.inspector", "dashboard.action"]},
                          {"plugin.py": PLUGIN})
        self.enable("sticky")
        self.service = PluginService()
        self.loaded_plugin = self.loaded("sticky")
        self.addCleanup(self.release)

    def release(self):
        import importlib
        importlib.import_module(f"{self.loaded_plugin.package}.plugin").HANG.set()
        for thread in plugin_threads():
            thread.join(2)

    def test_a_hung_inspector_fact_costs_one_daemon_thread_however_many_scans(self):
        row = {"name": "w", "id": "i", "universe": str(self.root)}
        with mock.patch("multiverse.plugins.loader.SYNCHRONOUS_BUDGET", 0.2):
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

    def test_a_hung_action_neither_uses_a_pool_nor_keeps_the_process_alive_and_says_so_when_run_again(self):
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

    def test_the_process_exits_with_a_hung_action_still_running(self):
        import subprocess
        code = (
            "import sys, threading, time\n"
            "from multiverse.plugins import loader\n"
            "from multiverse.ui import plugin_service\n"
            "from multiverse.ui.plugin_service import PluginService\n"
            "from pathlib import Path\n"
            "plugin_service.ACTION_TIMEOUT = 0.2\n"
            "service = PluginService()\n"
            "entry = next(e for e in service.registry(Path(sys.argv[1]))['actions'].values() if e[1] == 'Hang')\n"
            "try:\n    service.run_action(Path(sys.argv[1]), entry, None)\nexcept RuntimeError as error:\n    print(error)\n")
        import os
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, text=True,
                                timeout=20, env=environment)
        self.assertIn("took longer than 0.2 s", result.stdout, result.stderr)


if __name__ == "__main__":
    unittest.main()
