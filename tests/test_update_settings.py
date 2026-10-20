"""Update converts plugin settings with the checkout's new code and reads its records in the old process."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from multiverse import plugins
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import update

CHECKOUT = Path(__file__).resolve().parents[1]
NOTES = """
def register(mv):
    def migrate(old, source):
        import os
        if os.environ.get("SEEN"):
            with open(os.environ["SEEN"], "a") as out:
                out.write(str(mv.universe().path) + "\\n")
        if source == "legacy":
            return {"schema": 2, "colour": old["colour"]}, bool(old.get("enabled", True))
        return old
    mv.settings.migrate(migrate)
"""


class ConversionTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("notes", {"module": "plugin", "settings": {"version": 2, "legacy": "notes.json"}},
                          {"plugin.py": NOTES})

    def legacy(self, data, root=None):
        (Path(root or self.root) / "notes.json").write_text(json.dumps(data))

    def converted(self, root=None):
        path = Path(root or self.root) / "plugin-settings" / "notes.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_the_old_updater_reads_the_new_converters_records(self):
        """mv2 update's old process runs the new code's conversion in a child for the caller's universe (whatever
        MV2_UNIVERSE says) and parses its records, counting noise or malformed output as not converted and still
        naming a missing plugin; fragile because the two sides are different versions by design, and a crash
        happens only during a real upgrade."""
        with self.subTest("the checkout's converter, in a child"):
            self.assertEqual(update._convert_settings(CHECKOUT, self.root)["state"], "none")
            other, child = self.subverse("other"), self.subverse("child")
            seen = self.work / "seen.txt"
            self.legacy({"colour": "teal"})
            self.legacy({"colour": "red"}, child)
            os.environ.update(SEEN=str(seen), MV2_UNIVERSE=str(other))  # an ambient universe that is not the target
            result = update._convert_settings(CHECKOUT, self.root)
            self.assertEqual((result["state"], result["universe"]), ("converted", str(self.root)), result)
            self.assertEqual(sorted(result["lines"]), ["notes in child: converted notes.json to notes.json",
                                                       "notes: converted notes.json to notes.json"])
            self.assertEqual((self.converted(), self.converted(child)),
                             ({"schema": 2, "colour": "teal"}, {"schema": 2, "colour": "red"}))
            self.assertEqual(sorted(seen.read_text().split()), sorted([str(self.root), str(child)]))
            seen.unlink()
            self.legacy({"colour": "blue"}, other)
            os.environ["MV2_UNIVERSE"] = str(self.root)
            self.assertIn("notes:", " ".join(plugins.migrate_on_first_use(other)))  # in process, too
            self.assertEqual(seen.read_text().split(), [str(other)])
            self.assertEqual(os.environ["MV2_UNIVERSE"], str(self.root))  # put back afterwards

        def record(**fields):
            return json.dumps({"universe": str(self.root), "planned": [], "applied": [], "apply": True, **fields})

        malformed_missing = (None, 3, [3], [{"plugin": "compaction"}], [{"plugin": "compaction", "file": None}],
                             [{"plugin": "compaction", "file": "compaction.json", "universe": 3}])
        rows = ((record(), "some warning\n", 0, "none"),
                ("not json �", "", 0, "failed"),
                ("", "Traceback ...", 1, "failed"),
                ('{"planned": 3, "applied": []}', "", 0, "failed"),
                ('{"planned": [3], "applied": []}', "", 0, "failed"),
                ('{"planned": [], "applied": [3]}', "", 0, "failed"),
                (record(ok=False), "conversion failed", 1, "failed"),
                (record(), "", 1, "failed"),
                *((record(not_installed=missing), "", 0, "failed") for missing in malformed_missing),
                (record(not_installed=[]), "", 0, "none"))
        for stdout, stderr, code, state in rows:
            with self.subTest(stdout=stdout, stderr=stderr, code=code):
                done = subprocess.CompletedProcess([], code, stdout, stderr)
                with patch.object(update.subprocess, "run", return_value=done):
                    self.assertEqual(update._convert_settings(CHECKOUT, self.root)["state"], state)
        with self.subTest("plugins not installed, nothing converted"):
            missing = [{"plugin": "compaction", "file": "compaction.json", "universe": str(self.root)},
                       {"plugin": "memo", "file": "memo.json", "universe": str(self.root / "subverses" / "child")}]
            done = subprocess.CompletedProcess([], 0, record(not_installed=missing), "")
            # The updater can already have the old plugins module loaded, without the new sentence helper.
            with patch.object(plugins, "not_installed_message", None, create=True), \
                    patch.object(update.subprocess, "run", return_value=done):
                notice = update._convert_settings(CHECKOUT, self.root)
            self.assertEqual(notice["state"], "not_installed")
            first, second = notice["lines"]
            self.assertIn("compaction.json", first)
            self.assertIn("run mv2 plugins install compaction", first)
            self.assertIn("in child", second)
            self.assertIn("run mv2 plugins install memo", second)
            text = update.format_output({"json": False, "ok": True, "check": False, "commits": 0, "after": "a" * 40,
                                         "environment": {"state": "synced"}, "notes": [], "settings": notice})
            self.assertIn("environment synced", text)
            self.assertIn(first, text)
            self.assertNotIn("run mv2 plugins migrate --apply", text)  # installation advice comes first


if __name__ == "__main__":
    unittest.main()
