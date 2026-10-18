"""q leaves the dashboard even while a plugin's palette action has not returned, and the process then really exits."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase

PLUGIN = '''
import threading

HANG = threading.Event()

def register(mv):
    mv.dashboard.action("Hang forever", lambda context: HANG.wait(120))
    mv.dashboard.action("Quick", lambda context: "quick done")
'''

# The dashboard as a person runs it, headless: open it, start the action, press q, and report how long exit took.
RUNNER = '''
import asyncio, sys, time
from pathlib import Path
from multiverse.ui.app import MultiverseApp
from multiverse.ui.backend import LocalBackend
from multiverse.ui.plugin_service import PluginService

async def main(root):
    app = MultiverseApp(root, LocalBackend(), refresh_interval=0, plugin_service=PluginService())
    async with app.run_test(size=(120, 30)) as pilot:
        for _ in range(300):
            if app.snapshot is not None and app.plugin_actions:
                break
            await pilot.pause(0.02)
        key = next(key for key in app.plugin_actions if key[1] == "Hang forever")
        app.run_plugin_action(key)
        for _ in range(100):
            if app.operating:
                break
            await pilot.pause(0.02)
        print("operating", app.operating, flush=True)
        await pilot.press("q")
        for _ in range(200):
            if not app.is_running:
                break
            await asyncio.sleep(0.02)
        print("running after q", app.is_running, flush=True)

started = time.monotonic()
asyncio.run(main(Path(sys.argv[1])))
print("exited after %.1f" % (time.monotonic() - started), flush=True)
'''


class QuitTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("sticky", {"module": "plugin", "extends": ["dashboard.action"]}, {"plugin.py": PLUGIN})
        self.enable("sticky")
        self.agent("worker")

    async def test_q_quits_while_a_plugin_action_is_still_running(self):
        import importlib
        from multiverse.ui.app import MultiverseApp
        from multiverse.ui.backend import LocalBackend
        from multiverse.ui.plugin_service import PluginService
        gate = importlib.import_module(f"{self.loaded('sticky').package}.plugin")
        self.addCleanup(gate.HANG.set)
        app = MultiverseApp(self.root, LocalBackend(), refresh_interval=0, plugin_service=PluginService())
        async with app.run_test(size=(120, 30)) as pilot:
            for _ in range(300):
                if app.snapshot is not None and app.plugin_actions:
                    break
                await pilot.pause(0.02)
            app.run_plugin_action(next(key for key in app.plugin_actions if key[1] == "Hang forever"))
            for _ in range(100):
                if app.operating:
                    break
                await pilot.pause(0.02)
            self.assertTrue(app.operating, "the action never started")
            await pilot.press("q")
            for _ in range(150):  # not d.until: that waits for every worker, the stuck action's included
                if not app.is_running:
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(app.is_running, "q was refused while a plugin action was running")

    async def test_q_is_still_refused_while_one_of_the_dashboards_own_operations_runs(self):
        import threading
        from multiverse.ui.app import MultiverseApp
        from multiverse.ui.backend import LocalBackend
        gate = threading.Event()
        self.addCleanup(gate.set)
        app = MultiverseApp(self.root, LocalBackend(), refresh_interval=0)
        async with app.run_test(size=(120, 30)) as pilot:
            for _ in range(300):
                if app.snapshot is not None:
                    break
                await pilot.pause(0.02)
            app.submit_operation("Kill worker", lambda: gate.wait(30))
            await pilot.press("q")
            await pilot.pause(0.1)
            self.assertTrue(app.is_running)
            self.assertIn("still running", str(app.query_one("#status").render()))
            gate.set()

    def test_the_process_exits_promptly_after_q_with_a_plugin_action_still_running(self):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("MV2_")}
        environment.update(HOME=str(self.home), PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                           MV2_PLUGIN_PATH=os.environ["MV2_PLUGIN_PATH"])
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", RUNNER, str(self.root)], capture_output=True, text=True,
                                timeout=90, env=environment)
        took = time.monotonic() - started
        self.assertIn("operating True", result.stdout, result.stderr[-1500:])
        self.assertIn("running after q False", result.stdout, result.stderr[-1500:])
        self.assertLess(took, 15, f"the process lingered {took:.0f} s after q with the action still running\n{result.stdout}")


if __name__ == "__main__":
    unittest.main()
