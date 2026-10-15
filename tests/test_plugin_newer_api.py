"""A plugin written for a newer plugin API says so, even when it uses names this mv2 does not know yet."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase


class NewerApiTest(PluginTestCase):
    def test_new_permissions_and_extension_points_report_the_api_it_needs(self):
        self.write_plugin("future", {"api": "1.999", "permissions": ["transcripts.read", "models.claude"],
                                     "extends": ["transcripts"], "transcripts": {"rows": 10}})
        self.enable("future")
        found = loader.entry(self.root, "future")
        self.assertEqual(found.state, "incompatible")
        self.assertIn("needs plugin API 1.999", found.problem)
        self.assertIn("update mv2", found.problem)
        self.assertIn("transcripts.read", found.manifest.permission_texts())

    def test_names_unknown_for_the_api_it_asks_for_are_still_refused(self):
        self.write_plugin("typo", {"permissions": ["transcript.read"]})
        found = loader.entry(self.root, "typo")
        self.assertEqual(found.state, "invalid")
        self.assertIn("unknown permissions: transcript.read", found.problem)
