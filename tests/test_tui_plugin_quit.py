"""q leaves the dashboard even while a plugin's palette action has not returned, and the process then really exits."""
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

# The dashboard as a person runs it, headless: open it, start the action, press q, and report how long exit took and
# whether quitting ended any agent.
RUNNER = '''
import asyncio, sys, time
from pathlib import Path
from multiverse.ui.app import MultiverseApp
from multiverse.ui.backend import LocalBackend
from multiverse.ui.plugin_service import PluginService

ENDED = []

class Watched(LocalBackend):
    def stop(self, root, reference):
        ENDED.append(("stop", reference))
        return super().stop(root, reference)

    def kill(self, root, reference):
        ENDED.append(("kill", reference))
        return super().kill(root, reference)

    def remove_agent(self, root, reference):
        ENDED.append(("remove", reference))
        return super().remove_agent(root, reference)

async def main(root):
    app = MultiverseApp(root, Watched(), refresh_interval=0, plugin_service=PluginService())
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
print("ended agents", ENDED, flush=True)
print("exited after %.1f" % (time.monotonic() - started), flush=True)
'''
# The plugin service alone, as a short-lived process: the action outlives its time limit, and the process must exit.
SERVICE = '''
import sys
from pathlib import Path
from multiverse.ui import plugin_service
from multiverse.ui.plugin_service import PluginService

plugin_service.ACTION_TIMEOUT = 0.2
service = PluginService()
entry = next(e for e in service.registry(Path(sys.argv[1]))['actions'].values() if e[1] == 'Hang forever')
try:
    service.run_action(Path(sys.argv[1]), entry, None)
except RuntimeError as error:
    print(error)
'''

class QuitTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("sticky", {"module": "plugin", "extends": ["dashboard.action"]}, {"plugin.py": PLUGIN})
        self.enable("sticky")
        self.agent("worker")

    def run_python(self, code):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("MV2_")}
        environment.update(HOME=str(self.home), PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                           MV2_PLUGIN_PATH=os.environ["MV2_PLUGIN_PATH"])
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, text=True,
                                timeout=90, env=environment)
        return result, time.monotonic() - started

    def test_the_process_exits_promptly_after_q_with_a_plugin_action_still_running(self):
        """q works and the dashboard process really exits while a plugin action is stuck, ending no agent, and a
        process that only ran the action exits too; fragile because exit depends on thread kinds and on q not being
        blocked by operating, and a break shows only with a hung plugin, leaving the terminal stuck (5297d22)."""
        with self.subTest("the dashboard"):
            result, took = self.run_python(RUNNER)
            self.assertIn("operating True", result.stdout, result.stderr[-1500:])
            self.assertIn("running after q False", result.stdout, result.stderr[-1500:])
            self.assertIn("ended agents []", result.stdout, result.stderr[-1500:])
            self.assertLess(took, 15, f"the process lingered {took:.0f} s after q with the action still running\n"
                                      f"{result.stdout}")
        with self.subTest("the plugin service alone"):
            result, took = self.run_python(SERVICE)
            self.assertIn("took longer than 0.2 s", result.stdout, result.stderr[-1500:])
            self.assertLess(took, 15, f"the process lingered {took:.0f} s with the action still running")

if __name__ == "__main__":
    unittest.main()
