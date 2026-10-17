"""A plugins.json written by a newer mv2 keeps working here (its switches count); fields this mv2 does not know
are a note, never a reason to drop the whole file."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import config
from multiverse.plugins.testing import PluginTestCase


class NewerPluginsJsonTest(PluginTestCase):
    def test_unknown_fields_are_kept_and_noted(self):
        self.write_plugin("tool")
        config.set_enabled(self.root, "tool", True)
        path = config.path(self.root)
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, "access": {"tool": "spend"}, "access_ceiling": "observe"}))
        loaded = config.load(self.root)
        self.assertIsNone(loaded.problem)
        self.assertTrue(loaded.is_on("tool"))
        self.assertEqual(plugins.entry(self.root, "tool").state, "on")
        self.assertIn("plugins.json has fields this mv2 does not know: access, access_ceiling; update mv2 to use them",
                      plugins.notes(self.root))
        config.set_enabled(self.root, "tool", False)  # writing it keeps what a newer mv2 put there
        self.assertEqual(json.loads(path.read_text())["access"], {"tool": "spend"})

    def test_a_malformed_known_field_is_still_a_problem(self):
        config.path(self.root).write_text(json.dumps({"schema": 1, "enabled": {"tool": "yes"}}))
        self.assertIn("enabled.tool must be true or false", config.load(self.root).problem)
