"""The first screen never waits for a plugin: not for one whose register() hangs, nor for the theme plugin's.

A slow plugin's register() waits on a gate file the test opens, so the tests depend on what is on screen while the
plugin is still loading, not on how fast a busy machine draws it. Where a test bounds a time, the bound is generous
(QUICK) and far below the full wait it rules out (HUNG_LOAD)."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

HUNG_LOAD = 20.0  # LOAD_SECONDS while the test shows nothing waits for a load: a full wait takes this long
QUICK = 4.0       # what the first screen may take on a busy machine; it waits loader.REFRESH_WAIT for plugins at most

GATED = '''
import os, time
from pathlib import Path

def wait_for_gate(name):
    gate, deadline = Path(os.environ[name]), time.monotonic() + 30
    while not gate.exists() and time.monotonic() < deadline:   # hangs until the test opens the gate
        time.sleep(0.05)
'''
SLOW = GATED + '''
def shout(row):
    return row["name"].upper()

def register(mv):
    wait_for_gate("MV2_TEST_SLOW_GATE")
    mv.dashboard.column("shout", "Shout", 10, shout, compact=True)
'''
FAST = '''
def shout(row):
    return row["name"].upper()

def register(mv):
    mv.dashboard.column("fast", "Fast", 10, shout, compact=True)
'''
LATE = GATED + '''
def register(mv):
    wait_for_gate("MV2_TEST_LATE_GATE")   # slow, but it does load
    mv.dashboard.action("Late action", lambda context: "done late")
'''
STUCK = GATED + '''
def register(mv):
    wait_for_gate("MV2_TEST_STUCK_GATE")   # still loading when the dashboard quits
    mv.dashboard.theme({"accent": "#445566"})
'''
LOOK = "def register(mv):\n    mv.dashboard.theme({'accent': '#112233'})\n"


class FirstScreenTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(loader, "LOAD_SECONDS", HUNG_LOAD))
        self.gates = {name: self.root / f"{name.lower()}.open" for name in ("SLOW", "LATE", "STUCK")}
        self.enterContext(mock.patch.dict(os.environ, {f"MV2_TEST_{name}_GATE": str(path) for name, path in self.gates.items()}))
        for path in self.gates.values():
            self.addCleanup(path.touch)  # no load thread is left hanging after the test
        self.write_plugin("slow", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": SLOW})
        self.write_plugin("quick", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": FAST})
        self.write_plugin("late", {"module": "plugin", "extends": ["dashboard.action"]}, {"plugin.py": LATE})
        self.write_plugin("stuck", {"module": "plugin", "extends": ["dashboard.theme"]}, {"plugin.py": STUCK})
        self.write_plugin("look", {"module": "plugin", "extends": ["dashboard.theme"]}, {"plugin.py": LOOK})
        for name in ("slow", "quick", "late", "stuck", "look"):
            self.enable(name)
        self.agent("worker")
        write(self.root / "views" / "default.json", {
            "title": "T", "accent": "#89b4fa", "columns": ["name", "slow:shout", "quick:fast"],
            "widgets": [{"type": "slow:thing", "title": "Slow"},
                        {"type": "markdown", "title": "Fine", "text": "right here"}]})

    def open_gate(self, name):
        self.gates[name].touch()

    def loading(self, name):
        """Whether the plugin's load is still under way (its register() waiting on its gate)."""
        return next(item["state"] for item in plugins.listing(self.root) if item["name"] == name) == "loading"

    def choose_theme(self, name):
        data = json.loads((self.root / "plugins.json").read_text())
        data.pop("theme", None)
        (self.root / "plugins.json").write_text(json.dumps({**data, **({"theme": name} if name else {})}))

    async def test_the_first_screen_never_waits_for_a_plugin_load(self):
        """With hung register() calls, the theme plugin's among them, the theme is decided and the first screen and
        its rows appear within QUICK, a widget says still loading, its column, palette entry and widget appear later,
        and q quits while one still loads; fragile because any new plugin call on the first refresh blocks it again,
        and ordinary plugins load instantly in development."""
        with self.subTest("a theme plugin that hangs"):
            self.choose_theme("stuck")
            with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.3):
                started = time.monotonic()
                self.assertIsNone(plugins.theme_colors(self.root))  # the dashboard keeps its own colors
                self.assertLess(time.monotonic() - started, 1)
        with self.subTest("a theme beside a plugin still loading"):
            self.choose_theme("look")
            started = time.monotonic()
            loaded, colors = plugins.theme_colors(self.root)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual((loaded.name, colors), ("look", {"accent": "#112233"}))
        self.choose_theme(None)
        started = time.monotonic()
        async with self.dashboard() as d:
            took = time.monotonic() - started
            titles = lambda: [name for name, _, _ in d.app.plugin_palette()]
            shown = lambda: " ".join(d.local_view())
            with self.subTest("the first screen"):
                self.assertTrue(self.loading("slow"), "the first screen came only after the hung plugin's load ended")
                self.assertTrue(self.loading("late"), "the first screen came only after the slow plugin had loaded")
                self.assertLess(took, QUICK, f"the first refresh waited {took:.1f} s for a plugin")
                self.assertTrue(d.app.visible_rows)
                await d.until(lambda: "right here" in d.local_view(), "the other widget")
                self.assertIn("still loading", shown())  # drawn while the plugin's load was still going
                self.assertNotIn("late: Late action", titles())
            with self.subTest("a plugin that loaded quickly"):
                # It shows up on a later refresh; the hung one is left out meanwhile.
                await d.until(lambda: "WORKER" in " ".join(str(c) for r in d.table() for c in r),
                              "the quick plugin's column", timeout=8)
            with self.subTest("the Plugins screen while plugins still load"):
                # Not d.until: it waits for every worker. The list comes first and the problems line follows.
                started = time.monotonic()
                d.app.action_plugins()
                while not getattr(d.screen, "items", None):
                    self.assertLess(time.monotonic() - started, QUICK,
                                    "the Plugins screen sat on 'Reading…' while plugins loaded")
                    await asyncio.sleep(0.05)
                names = [item["name"] for item in d.screen.items]
                self.assertTrue({"slow", "late", "quick"} <= set(names), names)
                details = d.screen.query_one("#plugin-details")
                while "still loading" not in str(details.render()):
                    self.assertLess(time.monotonic() - started, QUICK,
                                    "the problems line did not say a plugin is still loading")
                    await asyncio.sleep(0.05)
                main = d.app.screen_stack[0]
                await d.pilot.press("escape")
                for _ in range(100):
                    if d.app.screen is main:
                        break
                    await asyncio.sleep(0.05)
                self.assertIs(d.app.screen, main)
            with self.subTest("a slow plugin's palette entry"):
                self.open_gate("LATE")
                deadline = time.monotonic() + 8
                while "late: Late action" not in titles():  # each refresh looks at what has loaded by now
                    self.assertLess(time.monotonic(), deadline, "the late plugin's palette entry never appeared")
                    await d.refresh()
                    await asyncio.sleep(0.2)
            with self.subTest("a slow plugin's widget"):
                # Once its load ends the widget says what is really wrong (this plugin has no such widget type).
                self.open_gate("SLOW")
                deadline = time.monotonic() + 8
                while "still loading" in shown():
                    self.assertLess(time.monotonic(), deadline, "the widget kept saying 'still loading'")
                    await d.refresh()
                    await asyncio.sleep(0.2)
                self.assertIn("unknown widget type", shown())
            with self.subTest("q while a plugin still loads"):
                self.assertTrue(self.loading("stuck"))
                await d.pilot.press("q")
                for _ in range(100):  # not d.until: that waits for every worker
                    if not d.app.is_running:
                        break
                    await asyncio.sleep(0.05)
                self.assertFalse(d.app.is_running, "q did not quit while a plugin was still loading")
                loads = [thread for thread in threading.enumerate() if thread.name.startswith("plugin stuck")]
                self.assertTrue(loads)
                self.assertTrue(all(thread.daemon for thread in loads), "a load would keep the process from exiting")

if __name__ == "__main__":
    unittest.main()
