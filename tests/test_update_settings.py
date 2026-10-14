"""Update consumes new migration notices in its old process, without touching a real universe."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from multiverse import plugins, universe
from multiverse.tools import update
from test_update import UpdateTest


class ConversionNoticeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def conversion(self, *, applied=(), missing=(), planned=(), code=0):
        record = {"planned": list(planned), "applied": list(applied), "not_installed": missing}
        process = subprocess.CompletedProcess([], code, json.dumps(record), "")
        with patch.object(universe, "resolve", return_value=self.root), \
                patch.object(update.subprocess, "run", return_value=process):
            return update._convert_settings(self.root, self.root)

    def render(self, notice):
        return update.format_output({"json": False, "ok": True, "check": False, "commits": 0,
                                     "after": "a" * 40, "environment": {"state": "synced"}, "notes": [],
                                     "settings": notice})

    def test_missing_plugins_are_said_even_when_nothing_is_converted(self):
        missing = [{"plugin": "compaction", "file": "compaction.json", "universe": str(self.root)},
                   {"plugin": "memo", "file": "memo.json", "universe": str(self.root / "subverses" / "child")}]
        # The updater can already have the old plugins module loaded, without the new sentence helper.
        with patch.object(plugins, "not_installed_message", None, create=True):
            notice = self.conversion(missing=missing)
        self.assertEqual(notice["state"], "not_installed")
        self.assertEqual(len(notice["lines"]), 2)
        self.assertEqual(notice["lines"][0], "compaction.json is here but the compaction plugin isn't installed: "
                         "run mv2 plugins install compaction, or move compaction.json away to stop using its settings")
        self.assertIn("in child", notice["lines"][1])
        text = self.render(notice)
        self.assertIn("environment synced", text)
        self.assertIn("await plugin installation:", text)
        self.assertIn(notice["lines"][0], text)
        self.assertNotIn("run mv2 plugins migrate --apply", text)  # installation advice comes first

    def test_successful_and_failed_conversions_keep_the_missing_plugin_notice(self):
        missing = [{"plugin": "compaction", "file": "compaction.json"}]
        done = {"plugin": "notes", "from": "notes.json", "to": "plugin-settings/notes.json"}
        notice = self.conversion(applied=[done], missing=missing)
        self.assertEqual(notice["state"], "partial")
        self.assertIn("notes: converted notes.json to notes.json", notice["lines"])
        self.assertIn("partly converted", self.render(notice))
        self.assertIn("run mv2 plugins install compaction", self.render(notice))
        failed = self.conversion(planned=[{"plugin": "notes", "problem": "bad settings"}],
                                 missing=missing, code=1)
        self.assertEqual(failed["state"], "failed")
        self.assertIn("notes: not converted: bad settings", failed["lines"])
        self.assertIn("run mv2 plugins install compaction", self.render(failed))

    def test_malformed_missing_notices_stay_inside_the_optional_conversion_boundary(self):
        for missing in (None, 3, [3], [{"plugin": "compaction"}],
                        [{"plugin": "compaction", "file": None}],
                        [{"plugin": "compaction", "file": "compaction.json", "universe": 3}]):
            with self.subTest(missing=missing):
                self.assertEqual(self.conversion(missing=missing)["state"], "failed")
        self.assertEqual(self.conversion(missing=[])["state"], "none")


class UpdateNoticeTest(UpdateTest):
    def test_update_retains_the_notice_without_failing_or_hiding_it_from_json(self):
        notice = {"state": "not_installed", "universe": str(self.root), "lines": [
            "compaction.json is here but the compaction plugin isn't installed: run mv2 plugins install compaction"]}
        self.convert_settings.return_value = notice
        result = update.run(json=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["environment"]["state"], "synced")
        self.assertEqual(update.format_output(result)["settings"], notice)
        self.assertIn(notice["lines"][0], update.format_output({**result, "json": False}))


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for case in (ConversionNoticeTest, UpdateNoticeTest):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite
