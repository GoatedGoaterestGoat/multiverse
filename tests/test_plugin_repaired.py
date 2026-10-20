"""A plugin that failed to load and is then repaired on disk, and one whose entry cannot be built (BUG-DASHBOARD-003)."""
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase

PLUGIN = '''
from multiverse.plugins import api

def register(mv):
    mv.dashboard.settings([api.Field("count", "Count", "{kind}")])
'''
SETTINGS = {"version": 1, "schema": "settings.schema.json", "defaults": "defaults/settings.json"}
SCHEMA = {"type": "object", "properties": {"schema": {"type": "integer"}, "count": {"type": "integer"}}}
PLAIN = {"plugin.py": "def register(mv):\n    pass\n"}


class OneBadEntryTest(PluginTestCase):
    def test_one_bad_plugin_entry_never_hides_the_others(self):
        """entries(), listing() and problems() list every plugin with its own state when one failed and was
        repaired, one raises in compatible(), one has properties: 3 and one a bad pattern, and the repaired one
        loads; fragile because every new per-entry field can raise and the failure cache is shared
        (BUG-DASHBOARD-003)."""
        for case in (self.failed_then_repaired, self.compatible_raises, self.malformed_schema):
            # Each case alone: its own plugins, its own universe, nothing loaded.
            where = self.work / case.__name__
            with self.subTest(case.__name__), mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(where)}):
                loader.reset()
                case(where, self.subverse(case.__name__))

    def failed_then_repaired(self, where, root):
        directory = self.write_plugin("badfield", {"module": "plugin", "extends": ["dashboard.settings"],
                                                   "settings": SETTINGS}, {
            "plugin.py": PLUGIN.format(kind="integer"),  # not a field kind: register() raises
            "settings.schema.json": json.dumps(SCHEMA),
            "defaults/settings.json": json.dumps({"schema": 1, "count": 1})}, where=where)
        self.write_plugin("other", {"module": "plugin"}, PLAIN, where=where)
        self.enable("badfield", root=root)
        self.enable("other", root=root)
        with self.assertRaises(loader.PluginError):
            loader.load(self.entry("badfield", root))  # as the dashboard's first load would
        self.assertEqual(self.entry("badfield", root).state, "failed")
        plugins.edit_settings(root, "badfield", {"count": 4})  # Configure, then save
        (directory / "plugin.py").write_text(PLUGIN.format(kind="int"))  # repaired on disk
        listed = {item["name"]: item for item in plugins.listing(root)}
        self.assertEqual(set(listed), {"badfield", "other"})
        self.assertIn(listed["badfield"]["state"], ("on", "loaded"))
        self.assertNotIsInstance(self.entry("badfield", root).loaded, loader.Failed)  # never the old failure
        self.assertEqual(plugins.problems(root), [])
        loaded = loader.load(self.entry("badfield", root))
        self.assertEqual([field.path for field in loaded.registrations.settings_fields], ["count"])
        self.assertEqual(self.entry("badfield", root).state, "loaded")
        self.assertEqual(plugins.edit_settings(root, "badfield", {"count": 5})["count"], 5)

    def compatible_raises(self, where, root):
        for name in ("alpha", "breaks", "gamma"):
            self.write_plugin(name, {"module": "plugin"}, PLAIN, where=where)
            self.enable(name, root=root)
        original = loader.compatible

        def compatible(found):
            if found.name == "breaks":
                raise RuntimeError("something unforeseen\nwith a second line")
            return original(found)
        with mock.patch.object(loader, "compatible", compatible):
            entries = {found.name: found for found in loader.entries(root)}
            listed = {item["name"] for item in plugins.listing(root)}
            problems = plugins.problems(root)
        self.assertEqual(set(entries), {"alpha", "breaks", "gamma"})
        self.assertEqual(listed, {"alpha", "breaks", "gamma"})
        self.assertEqual(entries["alpha"].state, "on")
        self.assertEqual(entries["breaks"].state, "failed")
        self.assertIn("breaks could not be read: RuntimeError: something unforeseen", entries["breaks"].problem)
        self.assertNotIn("second line", entries["breaks"].problem)
        self.assertEqual(problems, [entries["breaks"].problem])

    def malformed_schema(self, where, root):
        self.write_plugin("odd", {"settings": {"version": 1, "schema": "schema.json"}},
                          {"schema.json": json.dumps({"type": "object", "properties": 3}), "defaults.json": "{}"},
                          where=where)
        self.write_plugin("rex", {"settings": {"version": 1, "schema": "schema.json"}},
                          {"schema.json": json.dumps({"type": "object", "properties": {
                              "name": {"type": "string", "pattern": "(["}}})}, where=where)
        self.write_plugin("fine", {}, where=where)
        for name in ("odd", "rex", "fine"):
            self.enable(name, root=root)
        states = {entry["name"]: entry for entry in plugins.listing(root)}  # never raises
        self.assertEqual(set(states), {"odd", "rex", "fine"})
        self.assertIn("schema", states["odd"]["problem"])
        self.assertIn("pattern", states["rex"]["problem"])
        self.assertIsNone(states["fine"]["problem"])
        with self.assertRaisesRegex(ValueError, "pattern"):
            plugins.edit_settings(root, "rex", {"name": "x"})
