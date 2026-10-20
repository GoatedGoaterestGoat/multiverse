"""Neither the plugin system failing nor a plugin failing to load takes the agent list, a refresh or the Plugins screen
down, and a failed plugin repaired on disk loads without a restart."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins.testing import PluginTestCase
from multiverse.ui.plugins_screen import PluginsScreen

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

if __name__ == "__main__":
    unittest.main()
