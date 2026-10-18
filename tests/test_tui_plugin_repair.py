"""A plugin that failed and is then repaired loads, and nothing about it takes the Plugins screen or a refresh down."""
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

    def repair(self):
        (self.broken / "plugin.py").write_text(BROKEN.replace("KIND", "int"))

    async def test_configuring_a_failed_plugin_then_repairing_it_loads_it_and_keeps_everything_else(self):
        async with self.dashboard() as d:
            self.assertNotIn("badfield: Repaired", d.palette())
            screen = await d.open_settings("badfield")  # the generic screen, from the schema, though the plugin failed
            screen.widgets["count"].value = "4"
            await d.press("ctrl+enter")
            await d.until(lambda: isinstance(d.screen, PluginsScreen), "the Plugins screen after saving")
            self.assertEqual(plugins.settings_of(self.root, "badfield")[0]["count"], 4)
            await d.close_screen()
            self.repair()
            await d.press("f5")  # what a person does after fixing a plugin
            self.assertNotIn("Could not refresh", d.status)
            await d.until(lambda: "badfield: Repaired" in d.palette(), "the repaired plugin's palette entry")
            screen = await d.open_plugins()
            states = {item["name"]: item["state"] for item in screen.items}
            self.assertEqual(set(states), {"badfield", "healthy"})
            self.assertNotEqual(states["badfield"], "failed")
            self.assertIn("healthy: Healthy", d.palette())
            await d.close_screen()
            await d.run_action("badfield: Repaired")
            self.assertIn("it works now", d.status)

    async def test_a_plugin_system_failure_while_reading_plugins_never_stops_the_agent_list(self):
        (self.root / "views").mkdir(exist_ok=True)
        (self.root / "views" / "default.json").write_text(json.dumps({
            "title": "T", "columns": ["name", "mood"], "plugin_panels": "auto",
            "widgets": [{"type": "healthy:thing", "title": "Thing"}, {"type": "markdown", "title": "Own", "text": "my own widget"}]}))
        async with self.dashboard() as d:
            self.assertNotIn("plugins unreadable", d.header)
            with mock.patch("multiverse.plugins.dashboard_columns", side_effect=RuntimeError("columns exploded")), \
                    mock.patch("multiverse.plugins.dashboard_panels", side_effect=RuntimeError("panels exploded")), \
                    mock.patch("multiverse.plugins.on_here", side_effect=RuntimeError("on_here exploded")):
                await d.refresh()
                self.assertNotIn("Could not refresh", d.status)
                headers, rows = d.table()
                self.assertTrue(any(row[0].strip().endswith("worker") for row in rows), rows)
                self.assertIn("plugins unreadable", d.header)
                self.assertIn("my own widget", " ".join(d.local_view()))
            await d.refresh()  # the plugin system answers again: the notice goes
            self.assertNotIn("plugins unreadable", d.header)


if __name__ == "__main__":
    unittest.main()
