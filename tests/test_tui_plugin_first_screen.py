"""The first screen never waits for a plugin: one whose register() hangs shows up on the Plugins screen as failed."""
import asyncio
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

SLOW = '''
import time

def shout(row):
    return row["name"].upper()

def register(mv):
    time.sleep(6)   # a register() that hangs
    mv.dashboard.column("shout", "Shout", 10, shout, compact=True)
'''
FAST = '''
def shout(row):
    return row["name"].upper()

def register(mv):
    mv.dashboard.column("fast", "Fast", 10, shout, compact=True)
'''

LATE = '''
import time

def register(mv):
    time.sleep(1.2)   # slow, but it does load
    mv.dashboard.action("Late action", lambda context: "done late")
'''


class FirstScreenTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(loader, "LOAD_SECONDS", 2.0))
        self.write_plugin("slow", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": SLOW})
        self.write_plugin("quick", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": FAST})
        self.enable("slow")
        self.enable("quick")
        self.agent("worker")
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa",
                                                    "columns": ["name", "slow:shout", "quick:fast"]})

    async def test_the_first_screen_does_not_wait_for_a_register_that_hangs(self):
        started = time.monotonic()
        async with self.dashboard() as d:
            took = time.monotonic() - started
            self.assertLess(took, 1.2, f"the first refresh waited {took:.1f} s for a plugin")
            self.assertTrue(d.app.visible_rows)
            # The plugin that loaded quickly shows up on a later refresh; the hung one is left out meanwhile.
            await d.until(lambda: "WORKER" in " ".join(str(c) for r in d.table() for c in r), "the quick plugin's column",
                          timeout=8)

    async def test_a_widget_of_a_plugin_that_is_still_loading_does_not_hold_up_the_first_screen(self):
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name"],
                                                    "widgets": [{"type": "slow:thing", "title": "Slow"},
                                                                {"type": "markdown", "title": "Fine", "text": "right here"}]})
        started = time.monotonic()
        async with self.dashboard() as d:
            took = time.monotonic() - started
            self.assertLess(took, 1.2, f"the first refresh waited {took:.1f} s for a plugin's widget")
            await d.until(lambda: "right here" in d.local_view(), "the other widget")
            shown = lambda: " ".join(d.local_view())
            self.assertIn("still loading", shown())
            # Its register() never finishes, so once the load gives up the widget says why, not "still loading".
            deadline = time.monotonic() + 8
            while "still loading" in shown():
                self.assertLess(time.monotonic(), deadline, "the widget kept saying 'still loading'")
                await d.refresh()
                await asyncio.sleep(0.2)
            self.assertIn("failed to load", shown())

    async def test_a_register_that_fails_or_times_out_is_on_the_plugins_screen(self):
        async with self.dashboard() as d:
            await d.open_plugins()
            await d.until(lambda: any(item["name"] == "slow" and item["state"] == "failed" for item in d.screen.items),
                          "slow to be listed as failed", timeout=10)
            slow = next(item for item in d.screen.items if item["name"] == "slow")
            self.assertIn("longer than", slow["problem"])

    async def test_a_slow_plugins_palette_entry_appears_once_it_has_loaded(self):
        self.write_plugin("late", {"module": "plugin", "extends": ["dashboard.action"]}, {"plugin.py": LATE})
        self.enable("late")
        started = time.monotonic()
        async with self.dashboard() as d:
            self.assertLess(time.monotonic() - started, 1.2, "the first screen waited for the slow plugin")
            titles = lambda: [name for name, _, _ in d.app.plugin_palette()]
            self.assertNotIn("late: Late action", titles())
            deadline = time.monotonic() + 8
            while "late: Late action" not in titles():  # each refresh looks at what has loaded by now
                self.assertLess(time.monotonic(), deadline, "the late plugin's palette entry never appeared")
                await d.refresh()
                await asyncio.sleep(0.2)

    async def test_the_plugins_screen_lists_at_once_while_a_register_still_hangs(self):
        self.enterContext(mock.patch.object(loader, "LOAD_SECONDS", 5.0))  # still hanging when the screen opens
        async with self.dashboard() as d:
            started = time.monotonic()
            d.app.action_plugins()
            while not getattr(d.screen, "items", None):  # not d.until: that waits for every worker, this one included
                self.assertLess(time.monotonic() - started, 6, "the Plugins screen never listed anything")
                await asyncio.sleep(0.05)
            took = time.monotonic() - started
            self.assertLess(took, 1.5, f"the Plugins screen showed 'Reading…' for {took:.1f} s")
            self.assertIn("quick", [item["name"] for item in d.screen.items])
            details = lambda: str(d.screen.query_one("#plugin-details").render())
            while "slow is still loading" not in details():  # the problems, read without waiting out the load
                self.assertLess(time.monotonic() - started, 1.5, f"no 'still loading' line: {details()!r}")
                await asyncio.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
