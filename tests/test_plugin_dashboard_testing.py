"""multiverse.plugins.dashboard_testing: what a plugin's dashboard tests may rely on, tried with a small plugin."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase

PLUGIN = '''
def register(mv):
    mv.dashboard.screen("board", "screens:Board")
    mv.dashboard.action("Say hello", lambda context: f"hello {context.row['name'] if context.row else '-'}")
'''
SCREENS = '''
from multiverse.plugins.ui import FieldScreen
from textual.widgets import Static

class Board(FieldScreen):
    def compose(self):
        yield Static("board", id="board")
        yield Static("", classes="cerror")
    def action_save(self):
        self.dismiss("board closed")
'''


class DashboardTestingTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("greeter", {"module": "plugin", "api": "1.9",
                                      "extends": ["dashboard.screen", "dashboard.action"]},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS})
        self.enable("greeter")
        self.agent("worker", runtime={"status": "working"})
        self.agent("builder")

    async def test_leaving_the_dashboard_waits_for_the_scan_an_action_started(self):
        """An action ends by starting a scan, and leaving the dashboard waits for that scan too; fragile because
        waiting for the workers that exist misses the one the last of them starts, and its thread, still running when
        the test's cleanups put the real HOME back, writes mv2's state into the user's home, which happened only in
        full runs (the home guard's 'a test wrote mv2's real state')."""
        from multiverse.ui.backend import LocalBackend
        armed, finished, real = [], threading.Event(), LocalBackend.snapshot

        def snapshot(backend, *args, **kwargs):
            if not armed:
                return real(backend, *args, **kwargs)
            time.sleep(0.4)
            try:
                return real(backend, *args, **kwargs)
            finally:
                finished.set()

        with mock.patch.object(LocalBackend, "snapshot", snapshot):
            async with self.dashboard() as d:
                armed.append(True)
                d.app.submit_operation("An action", lambda: None)
                await d.until(lambda: not d.app.operating, "the action to end")
            self.assertTrue(finished.is_set(), "the dashboard was left while the scan the action started still ran")

    async def test_the_plugin_host_is_never_started_by_a_test(self):
        """The published dashboard test helper enters on the universe's agents, offers the plugins' palette
        entries, refreshes and opens the Plugins screen without ever starting the plugin host; a break goes unnoticed
        because tests using the helper pass either way, while a helper that starts the host leaks tmux sessions from
        plugin authors' runs."""
        from unittest import mock
        from multiverse.plugins import host
        with mock.patch.object(host, "start") as start:
            async with self.dashboard() as d:
                headers, rows = d.table()
                self.assertEqual(sorted(row[0].strip(" ├─╰│▾▸") for row in rows
                                        if "worker" in row[0] or "builder" in row[0]), ["builder", "worker"])
                self.assertIn("2 agents", d.header)
                self.assertIn("greeter: Say hello", d.palette())
                self.assertIn("greeter: board", d.palette())
                self.assertEqual(d.root, self.root)
                self.assertNotIn("host off", d.header)
                self.agent("third")
                await d.refresh()
                self.assertIn("3 agents", d.header)
                await d.open_plugins()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
