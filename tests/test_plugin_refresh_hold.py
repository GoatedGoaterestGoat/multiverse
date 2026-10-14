"""While an old settings file waits, refresh puts neither the template's settings nor its switch in front (PR #20, 6)."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase


class RefreshHoldBackTest(PluginTestCase):
    """While an old settings file waits, refresh puts neither the template's settings nor its switch in front."""

    def make(self, template):
        from multiverse.universe import templates
        root = templates.create(self.work / template / ".multiverse", template, template=template,
                                workdir=self.work / template)
        (root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)  # made before plugins
        (root / "plugins.json").unlink(missing_ok=True)
        return root

    def plan(self, root):
        from multiverse.universe import templates
        return {change["path"]: change for change in templates.refresh_plan(root)["changes"]}

    def test_development_settings_and_switch_wait_for_the_old_file(self):
        root = self.make("development")
        (root / "compaction.json").write_text(json.dumps({"enabled": False, "harnesses": {"claude": {"enabled": False}}}))
        changes = self.plan(root)
        for path in ("plugin-settings/compaction.json", "plugins.json"):
            self.assertEqual(changes[path]["action"], "deferred", changes)
            self.assertIn("compaction.json is still here and not converted", changes[path]["reason"])

    def test_without_an_old_file_nothing_is_deferred_even_without_the_plugin(self):
        root = self.make("base")
        changes = self.plan(root)
        self.assertNotIn("deferred", {change["action"] for change in changes.values()})
