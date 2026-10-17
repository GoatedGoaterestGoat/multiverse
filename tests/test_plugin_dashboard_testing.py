"""multiverse.plugins.dashboard_testing: what a plugin's dashboard tests may rely on, tried with a small plugin."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

PLUGIN = '''
from multiverse.plugins import api

def register(mv):
    mv.dashboard.settings(screen="screens:Own")
    mv.dashboard.screen("board", "screens:Board")
    mv.dashboard.action("Say hello", lambda context: f"hello {context.row['name'] if context.row else '-'}")
    mv.dashboard.action("Break", lambda context: 1 / 0)
    mv.dashboard.column("shout", "Shout", 8, lambda row: row["name"].upper(), compact=True)

    @mv.dashboard.panel("note", auto=True)
    def note(context):
        return api.Markdown("a note from the plugin")
'''
SCREENS = '''
from multiverse.plugins.ui import FieldScreen, field
from textual.widgets import Static

class Own(FieldScreen):
    def compose(self):
        yield Static("", classes="cerror")
        yield from self.field_rows([field("n", ("n",), "N", "int", {})])
    def action_save(self):
        self.dismiss("own saved " + self.values()["n"])

class Board(FieldScreen):
    def compose(self):
        yield Static("board", id="board")
        yield Static("", classes="cerror")
    def action_save(self):
        self.dismiss("board closed")
'''
NO_SETTINGS = '''
def register(mv):
    mv.dashboard.action("Nothing", lambda context: None)
'''


class DashboardTestingTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("greeter", {"module": "plugin", "api": "1.9", "settings": {"version": 1},
                                      "extends": ["dashboard.settings", "dashboard.screen", "dashboard.action",
                                                  "dashboard.column", "dashboard.panel"]},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS})
        self.write_plugin("quiet", {"module": "plugin", "extends": ["dashboard.action"]}, {"plugin.py": NO_SETTINGS})
        self.enable("greeter")
        self.enable("quiet")
        self.agent("worker", runtime={"status": "working"})
        self.agent("builder")

    async def test_the_dashboard_is_ready_on_entry_and_shows_the_universes_agents_and_the_plugins_palette(self):
        async with self.dashboard() as d:
            headers, rows = d.table()
            self.assertEqual(sorted(row[0].strip(" ├─╰│▾▸") for row in rows if "worker" in row[0] or "builder" in row[0]),
                             ["builder", "worker"])
            self.assertIn("2 agents", d.header)
            self.assertIn("greeter: Say hello", d.palette())
            self.assertIn("greeter: board", d.palette())
            self.assertEqual(d.root, self.root)
            self.assertNotIn("host off", d.header)

    async def test_open_settings_returns_the_plugins_own_screen_and_its_result_becomes_the_status(self):
        async with self.dashboard() as d:
            screen = await d.open_settings("greeter")
            self.assertEqual(type(screen).__name__, "Own")
            screen.widgets["n"].value = "5"
            await d.press("ctrl+enter")
            await d.until(lambda: d.screen is not screen, "the screen to close")
            await d.close_screen()  # the Plugins screen
            await d.until(lambda: "own saved 5" in d.status, "the status line")

    async def test_a_plugin_without_settings_fails_the_test_with_the_reason(self):
        async with self.dashboard() as d:
            with self.assertRaises(self.failureException) as raised:
                await d.open_settings("quiet")
            self.assertIn("has no settings", str(raised.exception))
            with self.assertRaises(self.failureException) as raised:
                await d.open_settings("ghost")
            self.assertIn("does not list 'ghost'", str(raised.exception))

    async def test_palette_actions_run_for_the_selected_row_and_report_back(self):
        async with self.dashboard() as d:
            d.select("worker")
            self.assertIsNone(await d.run_action("greeter: Say hello"))
            self.assertIn("hello worker", d.status)
            await d.run_action("greeter: Break")
            self.assertIn("failed", d.status)
            await d.refresh()
            self.assertIn("1 plugin error", d.header)
            with self.assertRaises(self.failureException):
                await d.run_action("greeter: nothing like it")

    async def test_a_palette_entry_that_opens_a_screen_returns_it(self):
        async with self.dashboard() as d:
            screen = await d.run_action("greeter: board")
            self.assertEqual(type(screen).__name__, "Board")
            await d.press("ctrl+enter")
            await d.until(lambda: "board closed" in d.status, "the status line")

    async def test_plugin_columns_and_panels_show_in_a_portal_and_the_scope_comes_and_goes(self):
        sub = self.subverse("ops")
        self.enable("greeter", root=sub)
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name", "greeter:shout"]})
        write(sub / "views" / "default.json", {"title": "Ops", "accent": "#89b4fa", "columns": ["name"]})
        async with self.dashboard() as d:
            headers, rows = d.table()
            self.assertIn("Shout", headers)
            self.assertIn("WORKER", [cell for row in rows for cell in row])
            self.assertIn("a note from the plugin", d.local_view())
            await d.enter("Ops")  # a subverse is shown under its view title
            self.assertEqual(d.root, sub)
            self.assertIn("a note from the plugin", d.local_view())
            self.assertIn("greeter: Say hello", d.palette())  # the palette is the opened scope's, in every portal
            await d.back()
            self.assertEqual(d.root, self.root)

    async def test_refresh_reads_the_universe_again(self):
        async with self.dashboard() as d:
            self.assertIn("2 agents", d.header)
            self.agent("third")
            await d.refresh()
            self.assertIn("3 agents", d.header)

    async def test_the_plugin_host_is_never_started_by_a_test(self):
        from unittest import mock
        from multiverse.plugins import host
        with mock.patch.object(host, "start") as start:
            async with self.dashboard() as d:
                await d.open_plugins()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
