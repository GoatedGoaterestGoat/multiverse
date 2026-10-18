"""multiverse.plugins.dashboard_testing: what a plugin's dashboard tests may rely on, tried with a small plugin."""
from pathlib import Path
import sys
import unittest

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
