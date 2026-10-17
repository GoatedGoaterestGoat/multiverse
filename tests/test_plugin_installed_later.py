"""The "not installed" notice never stops the conversion once the plugin is installed (PR #20 re-check, item 2)."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase

CONVERT = """
def register(mv):
    @mv.settings.migrate
    def convert(data, source):
        if source == "legacy":
            return {"schema": 2, "budget": data.get("budget", 1)}, bool(data.get("enabled"))
        return data
"""


class InstalledLaterTest(PluginTestCase):
    def test_installing_the_plugin_after_the_notice_converts_on_the_next_command(self):
        (self.root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)
        (self.root / "compaction.json").write_text(json.dumps({"enabled": True, "budget": 7}))
        notice = plugins.migrate_on_first_use(self.root)
        self.assertEqual(len(notice), 1)
        self.assertIn("isn't installed", notice[0])
        # Installed now (a checkout's plugins/compaction, here on the plugin path).
        self.write_plugin("compaction", {"module": "plugin", "settings": {"version": 2, "legacy": "compaction.json"}},
                          {"plugin.py": CONVERT})
        loader.reset()
        self.assertIn("compaction: compaction.json is still here and not converted yet: it is converted the next "
                      "time mv2 runs here, or now with mv2 plugins migrate --apply.", plugins.problems(self.root))
        lines = plugins.migrate_on_first_use(self.root)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("converted compaction.json to plugin-settings/compaction.json, turned it on", lines[0])
        self.assertEqual(json.loads((self.root / "plugin-settings" / "compaction.json").read_text()),
                         {"schema": 2, "budget": 7})
        self.assertFalse((self.root / "compaction.json").exists())
        self.assertEqual([problem for problem in plugins.problems(self.root) if "compaction" in problem], [])

    def test_a_record_left_by_an_older_mv2_does_not_hold_it_back_either(self):
        (self.root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)
        legacy = self.root / "compaction.json"
        legacy.write_text(json.dumps({"enabled": False}))
        status = legacy.stat()
        state = self.root / "state" / "plugins"
        state.mkdir(parents=True, exist_ok=True)
        (state / plugins.MIGRATE_FAILED).write_text(json.dumps({"compaction": {
            "file": "compaction.json", "fingerprint": [status.st_mtime_ns, status.st_size], "reason": "not installed",
            "not_installed": True}}))
        self.write_plugin("compaction", {"module": "plugin", "settings": {"version": 2, "legacy": "compaction.json"}},
                          {"plugin.py": CONVERT})
        lines = plugins.migrate_on_first_use(self.root)
        self.assertIn("converted compaction.json", " ".join(lines))
