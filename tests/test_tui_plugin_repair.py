"""Neither the plugin system failing nor a plugin failing to load takes the agent list, a refresh or the Plugins screen
down, a failed plugin repaired on disk loads without a restart, and Enter on a plugin says what it can do, or why not,
whatever state the plugin is in."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import DataTable, Static

from multiverse import plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.ui.plugins_screen import PluginSettingsScreen, PluginsScreen

BROKEN = '''
from multiverse.plugins import api

def register(mv):
    mv.dashboard.settings(fields=[api.Field("count", "Count", "KIND")])
    mv.dashboard.action("Repaired", lambda context: "it works now")
'''
HEALTHY = '''
def register(mv):
    mv.dashboard.action("Healthy", lambda context: "healthy")
    mv.dashboard.column("mood", "Mood", 8, lambda row: "calm")
'''
RAISING = '''
def register(mv):
    raise RuntimeError("load failure")
'''
RECOVERED = '''
def register(mv):
    mv.dashboard.action("Recovered", lambda context: "recovered")
'''
SCHEMA = {"type": "object", "properties": {"count": {"type": "integer"}}}
OWN_FIELDS = '''
from multiverse.plugins import api

def register(mv):
    mv.dashboard.settings(fields=[api.Field("count", "Count", "int")])
'''
OWN = '''
def register(mv):
    mv.dashboard.settings(screen="screens:Settings")
'''
SLOW = '''
import time

def register(mv):
    time.sleep(SECONDS)
    mv.dashboard.settings(screen="screens:Settings")
'''
OWN_SCREENS = '''
from textual.screen import ModalScreen
from textual.widgets import Static

class Settings(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen", "Close")]
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("my own settings", id="own")
'''


class RepairTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.broken = self.write_plugin(
            "badfield", {"module": "plugin", "extends": ["dashboard.settings", "dashboard.action"],
                         "settings": {"version": 1, "schema": "settings.schema.json", "defaults": "defaults.json"}},
            {"plugin.py": BROKEN.replace("KIND", "integer"), "settings.schema.json": json.dumps(SCHEMA),
             "defaults.json": json.dumps({"schema": 1, "count": 2})})
        self.write_plugin("healthy", {"module": "plugin", "extends": ["dashboard.action", "dashboard.column"]},
                          {"plugin.py": HEALTHY})
        self.enable("badfield")
        self.enable("healthy")
        self.agent("worker")

    async def test_a_plugin_system_failure_while_reading_plugins_never_stops_the_agent_list(self):
        """The agent list survives dashboard_columns, dashboard_panels and on_here all raising, and plugins that fail
        to load, one turned on while the dashboard runs included, leave refresh and the Plugins screen working, can
        still be configured and load once repaired; fragile because each plugin call added to the refresh path needs
        its own guard, and one unguarded call takes down the dashboard's core view (de26e78)."""
        (self.root / "views").mkdir(exist_ok=True)
        (self.root / "views" / "default.json").write_text(json.dumps({
            "title": "T", "columns": ["name", "mood"], "plugin_panels": "auto",
            "widgets": [{"type": "healthy:thing", "title": "Thing"},
                        {"type": "markdown", "title": "Own", "text": "my own widget"}]}))
        async with self.dashboard() as d:
            with self.subTest("the plugin system raising"):
                self.assertNotIn("plugins unreadable", d.header)
                with mock.patch("multiverse.plugins.dashboard_columns", side_effect=RuntimeError("columns")), \
                        mock.patch("multiverse.plugins.dashboard_panels", side_effect=RuntimeError("panels")), \
                        mock.patch("multiverse.plugins.on_here", side_effect=RuntimeError("on_here")):
                    await d.refresh()
                    self.assertNotIn("Could not refresh", d.status)
                    headers, rows = d.table()
                    self.assertTrue(any(row[0].strip().endswith("worker") for row in rows), rows)
                    self.assertIn("plugins unreadable", d.header)
                    self.assertIn("my own widget", " ".join(d.local_view()))
                await d.refresh()  # the plugin system answers again: the notice goes
                self.assertNotIn("plugins unreadable", d.header)
            with self.subTest("plugins that fail to load, one turned on meanwhile"):
                self.write_plugin("broken", {"module": "plugin", "extends": ["dashboard.action"]},
                                  {"plugin.py": RAISING})
                self.enable("broken")
                for _ in range(3):
                    await d.press("f5")
                    self.assertNotIn("Could not refresh", d.status)
                    headers, rows = d.table()
                    self.assertTrue(any(row[0].strip().endswith("worker") for row in rows), rows)
                    screen = await d.open_plugins()
                    states = {item["name"]: item["state"] for item in screen.items}
                    self.assertEqual(states, {"badfield": "failed", "broken": "failed", "healthy": "loaded"})
                    await d.close_screen()
            with self.subTest("configured while failed, then repaired"):
                self.assertNotIn("badfield: Repaired", d.palette())
                screen = await d.open_settings("badfield")  # the generic screen, from the schema, though it failed
                screen.widgets["count"].value = "4"
                await d.press("ctrl+enter")
                await d.until(lambda: isinstance(d.screen, PluginsScreen), "the Plugins screen after saving")
                self.assertEqual(plugins.settings_of(self.root, "badfield")[0]["count"], 4)
                await d.close_screen()
                (self.broken / "plugin.py").write_text(BROKEN.replace("KIND", "int"))
                (self.plugins_dir / "broken" / "plugin.py").write_text(RECOVERED)
                await d.press("f5")  # what a person does after fixing a plugin
                self.assertNotIn("Could not refresh", d.status)
                await d.until(lambda: "badfield: Repaired" in d.palette(), "the repaired plugin's palette entry")
                await d.until(lambda: "broken: Recovered" in d.palette(), "the other repaired plugin's palette entry")
                screen = await d.open_plugins()
                states = {item["name"]: item["state"] for item in screen.items}
                self.assertEqual(set(states), {"badfield", "broken", "healthy"})
                self.assertNotEqual(states["badfield"], "failed")
                self.assertIn("healthy: Healthy", d.palette())
                await d.close_screen()
                await d.run_action("badfield: Repaired")
                self.assertIn("it works now", d.status)
                self.assertEqual(plugins.settings_of(self.root, "badfield")[0]["count"], 4)

    async def test_enter_on_a_plugin_with_its_own_settings_view_opens_it_where_it_is_off_and_says_why_when_it_cannot(self):
        """Enter on a plugin whose settings are edited on its own screen, or in fields it declares, opens them while
        the plugin is off here, and names why when the plugin cannot load, never 'no schema'; fragile because the screen
        and the fields are known only by loading the plugin, which an off or failed one used to skip, and the person
        who pressed Enter for Space got a message about a schema the plugin never had."""
        manifest = {"module": "plugin", "settings": {"version": 1}, "extends": ["dashboard.settings"]}
        self.write_plugin("owner", manifest, {"plugin.py": OWN, "screens.py": OWN_SCREENS})  # off here: never enabled
        self.write_plugin("rotten", manifest, {"plugin.py": RAISING, "screens.py": OWN_SCREENS})
        self.write_plugin("fielder", manifest, {"plugin.py": OWN_FIELDS})  # off here, fields and no screen or schema
        self.enable("rotten")
        async with self.dashboard() as d:
            with self.subTest("a plugin that is off here"):
                screen = await d.open_settings("owner")
                self.assertIn("my own settings", str(screen.query_one("#own").render()))
                await d.close_screen()
            with self.subTest("a plugin that is off here and declares fields, not a screen"):
                screen = await d.open_settings("fielder")
                self.assertIsInstance(screen, PluginSettingsScreen)
                self.assertEqual(list(screen.widgets), ["count"])
                await d.close_screen()
            with self.subTest("a plugin that fails to load"):
                with self.assertRaises(AssertionError) as shown:
                    await d.open_settings("rotten")
                self.assertIn("load failure", str(shown.exception))
                self.assertNotIn("no schema", str(shown.exception))

    async def test_enter_while_a_plugins_settings_are_opening_opens_one_screen_and_never_the_generic_form_for_a_slow_plugin(self):
        """Enter repeated while a plugin's settings screen is still being opened (the plugin loads first) opens one
        screen, and Enter on a plugin that is only still loading says so instead of opening the form its schema
        generates; fragile because each Enter started a worker with no memory of the last, so a held key stacked
        identical screens that each save on their own, and a slow plugin with a schema lost its own screen to a form."""
        own = {"module": "plugin", "extends": ["dashboard.settings"], "settings": {"version": 1}}
        both = {**own, "settings": {"version": 1, "schema": "settings.schema.json", "defaults": "defaults.json"}}
        self.write_plugin("cold", own, {"plugin.py": SLOW.replace("SECONDS", "0.15"), "screens.py": OWN_SCREENS})
        self.write_plugin("warm", own, {"plugin.py": SLOW.replace("SECONDS", "0"), "screens.py": OWN_SCREENS})
        self.enable("warm")
        self.write_plugin("slowboth", both, {"plugin.py": SLOW.replace("SECONDS", "1.0"), "screens.py": OWN_SCREENS,
                                             "settings.schema.json": json.dumps(SCHEMA),
                                             "defaults.json": json.dumps({"schema": 1, "count": 2})})
        names = lambda d: [type(screen).__name__ for screen in d.app.screen_stack]
        async with self.dashboard() as d:
            shown = await d.open_plugins()

            def select(name):
                table = shown.query_one("#plugins-table", DataTable)
                table.move_cursor(row=[item["name"] for item in shown.items].index(name), animate=False)

            with self.subTest("Enter three times in one turn on a plugin that is not loaded yet"):
                select("cold")
                for _ in range(3):  # what a held key does: all before the first worker has loaded anything
                    shown.action_settings()
                await d.until(lambda: "Settings" in names(d), "its settings screen")
                self.assertEqual(names(d).count("Settings"), 1, names(d))
                while d.app.screen is not shown:
                    await d.close_screen()
            with self.subTest("Enter twice, one after the other, on an enabled plugin whose screen the listing knows"):
                shown.load()
                await d.until(lambda: next(i for i in shown.items if i["name"] == "warm")["settings"].get("screen"), "its listing")
                select("warm")
                for _ in range(2):
                    shown.action_settings()
                    await d.until(lambda: "Settings" in names(d), "its settings screen")
                    self.assertEqual(names(d).count("Settings"), 1, names(d))
                    await d.close_screen()  # Enter must work again once the screen is up and gone
            with self.subTest("Enter on a plugin with its own screen and a schema, while it is still loading"):
                select("slowboth")
                shown.action_settings()
                await d.until(lambda: d.app.screen is not shown or "still loading" in str(shown.query_one(".cerror", Static).render()),
                              "an answer")
                self.assertIs(d.app.screen, shown, names(d))  # not the form its schema generates
                self.assertIn("again", str(shown.query_one(".cerror", Static).render()))
                await d.until(lambda: not loader.loading(plugins.entry(self.root, "slowboth")), "slowboth to finish loading")
                shown.action_settings()
                await d.until(lambda: d.app.screen is not shown, "its settings screen")
                self.assertEqual(names(d)[-1], "Settings", names(d))  # its own screen, not PluginSettingsScreen
                self.assertEqual(names(d).count("Settings"), 1, names(d))


if __name__ == "__main__":
    unittest.main()
