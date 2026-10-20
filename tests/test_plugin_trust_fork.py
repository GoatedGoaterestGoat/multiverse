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
        """mv2 plugins trust NAME and untrust act on the universe copy that would run, not another copy of the same
        name, and say which copy they trusted; fragile because several copies share one name, and trusting the
        wrong one runs code the user never reviewed (PR #20 review 3, item 2)."""
        self.write_plugin("fork", where=self.home / ".multiverse" / "plugins")  # e.g. the bundled compaction
        directory = self.write_plugin("fork", {"description": "my fork"}, where=self.root / "plugins")
        self.enable("fork")
        self.assertEqual(plugins.entry(self.root, "fork").source, "personal")  # the untrusted copy hides nothing
        result = tool.run(self.root, plugin_action="trust", name="fork")
        self.assertEqual(Path(result["directory"]), directory.resolve())
        self.assertEqual(trust.state(directory), "trusted")
        self.assertEqual(result["overrides"], ["personal"])
        self.assertIn(str(directory.resolve()), tool.text("trust", result))
        self.assertEqual(plugins.entry(self.root, "fork").source, "universe")
        forgotten = tool.run(self.root, plugin_action="untrust", name="fork")
        self.assertTrue(forgotten["forgotten"])
        self.assertEqual(plugins.entry(self.root, "fork").source, "personal")
