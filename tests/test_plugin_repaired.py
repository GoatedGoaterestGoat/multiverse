"""A plugin that failed to load and is then repaired on disk, and one whose entry cannot be built (BUG-DASHBOARD-003)."""
import json
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


class RepairedPluginTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(loader.reset)
        self.directory = self.write_plugin("badfield", {"module": "plugin", "extends": ["dashboard.settings"],
                                                        "settings": SETTINGS}, {
            "plugin.py": PLUGIN.format(kind="integer"),  # not a field kind: register() raises
            "settings.schema.json": json.dumps(SCHEMA),
            "defaults/settings.json": json.dumps({"schema": 1, "count": 1})})
        self.write_plugin("other", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
        self.enable("badfield")
        self.enable("other")

    def test_a_repaired_plugin_loads_and_never_breaks_the_list(self):
        with self.assertRaises(loader.PluginError):
            loader.load(self.entry("badfield"))  # as the dashboard's first load would
        self.assertEqual(self.entry("badfield").state, "failed")
        plugins.edit_settings(self.root, "badfield", {"count": 4})  # Configure, then save
        (self.directory / "plugin.py").write_text(PLUGIN.format(kind="int"))  # repaired on disk
        listed = {item["name"]: item for item in plugins.listing(self.root)}
        self.assertEqual(set(listed), {"badfield", "other"})
        self.assertIn(listed["badfield"]["state"], ("on", "loaded"))
        self.assertNotIsInstance(self.entry("badfield").loaded, loader.Failed)  # never the old failure as loaded
        self.assertEqual(plugins.problems(self.root), [])
        loaded = loader.load(self.entry("badfield"))
        self.assertEqual([field.path for field in loaded.registrations.settings_fields], ["count"])
        self.assertEqual(self.entry("badfield").state, "loaded")
        self.assertEqual(plugins.edit_settings(self.root, "badfield", {"count": 5})["count"], 5)


class OneEntryCannotBeBuiltTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(loader.reset)
        for name in ("alpha", "breaks", "gamma"):
            self.write_plugin(name, {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
            self.enable(name)

    def test_the_other_plugins_are_still_listed(self):
        original = loader.compatible

        def compatible(found):
            if found.name == "breaks":
                raise RuntimeError("something unforeseen\nwith a second line")
            return original(found)
        with mock.patch.object(loader, "compatible", compatible):
            entries = {found.name: found for found in loader.entries(self.root)}
            listed = {item["name"] for item in plugins.listing(self.root)}
            problems = plugins.problems(self.root)
        self.assertEqual(set(entries), {"alpha", "breaks", "gamma"})
        self.assertEqual(listed, {"alpha", "breaks", "gamma"})
        self.assertEqual(entries["alpha"].state, "on")
        self.assertEqual(entries["breaks"].state, "failed")
        self.assertEqual(entries["breaks"].problem,
                         "breaks could not be read: RuntimeError: something unforeseen")
        self.assertEqual(problems, ["breaks could not be read: RuntimeError: something unforeseen"])
