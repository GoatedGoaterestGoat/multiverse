"""mv2 plugins trust NAME trusts this universe's copy even when another source has a plugin of that name
(PR #20 review 3, item 2)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import plugins as tool


class TrustForkTest(PluginTestCase):
    def test_trust_and_untrust_target_the_universe_copy(self):
        self.write_plugin("fork", where=self.home / ".multiverse" / "plugins")  # e.g. the bundled compaction
        directory = self.write_plugin("fork", {"description": "my fork"}, where=self.root / "plugins")
        self.enable("fork")
        self.assertEqual(plugins.entry(self.root, "fork").source, "personal")  # the untrusted copy hides nothing
        result = tool.run(self.root, plugin_action="trust", name="fork")
        self.assertEqual(Path(result["directory"]), directory.resolve())
        self.assertEqual(trust.state(directory), "trusted")
        text = tool.text("trust", result)
        self.assertIn(f"this universe's copy of fork ({directory.resolve()})", text)
        self.assertIn("It now overrides the personal fork.", text)
        self.assertEqual(plugins.entry(self.root, "fork").source, "universe")
        forgotten = tool.run(self.root, plugin_action="untrust", name="fork")
        self.assertTrue(forgotten["forgotten"])
        self.assertEqual(plugins.entry(self.root, "fork").source, "personal")

    def test_a_name_with_no_universe_copy_says_so(self):
        self.write_plugin("solo", where=self.home / ".multiverse" / "plugins")
        with self.assertRaisesRegex(ValueError, "solo is a personal plugin; only plugins in a universe's plugins/"):
            tool.run(self.root, plugin_action="trust", name="solo")
        with self.assertRaisesRegex(ValueError, "No plugin named ghost here"):
            tool.run(self.root, plugin_action="trust", name="ghost")
