"""While an old settings file waits, refresh puts neither the template's settings nor its switch in front (PR #20, 6)."""
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase


class RefreshHoldBackTest(PluginTestCase):
    """While an old settings file waits, refresh puts neither the template's settings nor its switch in front."""

    def make(self, name, template="base"):
        from multiverse.universe import templates
        root = templates.create(self.work / name / ".multiverse", name, template=template, workdir=self.work / name)
        (root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)  # made before plugins
        (root / "plugins.json").unlink(missing_ok=True)
        return root

    def plan(self, root):
        from multiverse.universe import templates
        return {change["path"]: change for change in templates.refresh_plan(root)["changes"]}

    def test_template_settings_and_switch_wait_for_the_old_file(self):
        from multiverse.universe import templates
        template = self.work / "compaction-on"
        shutil.copytree(templates.TEMPLATES / "base", template)
        (template / "plugins.json").write_text(json.dumps({"schema": 1, "enabled": {"compaction": True}}))
        root = self.make("compaction-on-universe", str(template))
        (root / "compaction.json").write_text(json.dumps({"enabled": False, "harnesses": {"claude": {"enabled": False}}}))
        changes = self.plan(root)
        for path in ("plugin-settings/compaction.json", "plugins.json"):
            self.assertEqual(changes[path]["action"], "deferred", changes)
            self.assertIn("compaction.json is still here and not converted", changes[path]["reason"])

    def test_without_an_old_file_nothing_is_deferred_even_without_the_plugin(self):
        root = self.make("base")
        changes = self.plan(root)
        self.assertNotIn("deferred", {change["action"] for change in changes.values()})
