"""A plugin's own settings check runs wherever its settings are edited or shown, not only in doctor
(COMPACTION-2), and an old settings file that reappears after its conversion is said (qa-compaction's spec gap 2).

The compaction cases are qa-compaction's black-box tests (20d8dff), run on this checkout's mv2 and its
plugins/compaction checkout; they are skipped where that submodule is not initialized."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import loader, settings as settings_files
from multiverse.plugins.testing import PluginTestCase

PROJECT = Path(__file__).resolve().parents[1]
CHECKED = """
def register(mv):
    def check(values):
        if values.get("level", 1) not in (1, 2):
            raise ValueError("level must be 1 or 2")
    mv.settings.check(check)
"""
SCHEMA = {"type": "object", "properties": {"schema": {"type": "integer"}, "level": {"type": "integer"}}}
SETTINGS = {"version": 1, "schema": "settings.schema.json", "defaults": "defaults/settings.json"}


class CliCase(PluginTestCase):
    def mv2(self, *args):
        environment = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}
        return subprocess.run([sys.executable, str(PROJECT / "bin" / "mv2"), "-u", str(self.root), *args],
                              env=environment, text=True, capture_output=True, timeout=120, cwd=self.work)

    def state(self, name):
        line = next(line for line in self.mv2("plugins").stdout.splitlines() if line.split()[:1] == [name])
        return line.split()[1]


class CheckedSettingsTest(CliCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(loader.reset)
        self.write_plugin("picky", {"module": "plugin", "settings": SETTINGS}, {
            "plugin.py": CHECKED, "settings.schema.json": json.dumps(SCHEMA),
            "defaults/settings.json": json.dumps({"schema": 1, "level": 1})})
        self.enable("picky")
        self.file = settings_files.path(self.root, "picky")

    def test_an_edit_its_own_check_refuses_is_not_written(self):
        self.assertEqual(self.mv2("plugins", "settings", "picky", "--set", "level=2").returncode, 0)
        before = self.file.read_text()
        refused = self.mv2("plugins", "settings", "picky", "--set", "level=3")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("level must be 1 or 2", refused.stderr)
        self.assertEqual(self.file.read_text(), before)
        self.assertEqual(self.state("picky"), "on")

    def test_an_edit_is_checked_where_the_plugin_is_off_too(self):
        self.enable("picky", on=False)
        refused = self.mv2("plugins", "settings", "picky", "--set", "level=3")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("level must be 1 or 2", refused.stderr)
        self.assertFalse(self.file.exists())

    def test_a_file_its_check_refuses_reads_unusable_everywhere(self):
        self.file.parent.mkdir(exist_ok=True)
        self.file.write_text(json.dumps({"schema": 1, "level": 9}))
        self.assertEqual(self.state("picky"), "unusable")
        self.assertIn("unusable", self.mv2("plugins", "info", "picky").stdout)
        shown = self.mv2("plugins", "settings", "picky")
        self.assertIn("Cannot be used", shown.stdout)
        self.assertIn("level must be 1 or 2", shown.stdout)
        self.assertIn("its settings cannot be used", self.mv2("doctor").stdout)
        # and it can be fixed from the command line
        self.assertEqual(self.mv2("plugins", "settings", "picky", "--set", "level=1").returncode, 0)
        self.assertEqual(self.state("picky"), "on")

    def test_a_plugin_whose_code_cannot_run_is_checked_against_its_schema_alone(self):
        self.write_plugin("broken", {"module": "plugin", "settings": SETTINGS}, {
            "plugin.py": "raise RuntimeError('cannot load')\n", "settings.schema.json": json.dumps(SCHEMA),
            "defaults/settings.json": json.dumps({"schema": 1, "level": 1})})
        self.enable("broken")
        written = self.mv2("plugins", "settings", "broken", "--set", "level=3")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertIn("broken's own settings check did not run", written.stdout)
        self.assertIn("failed to load: RuntimeError: cannot load", written.stdout)
        self.assertEqual(json.loads(settings_files.path(self.root, "broken").read_text())["level"], 3)
        self.assertEqual(self.mv2("plugins", "settings", "broken", "--set", "level=x").returncode, 1)


class ReappearedOldFileTest(CliCase):
    def test_an_old_settings_file_found_after_its_conversion_is_said_to_be_ignored(self):
        self.write_plugin("veteran", {"module": "plugin", "settings": {**SETTINGS, "legacy": "veteran.json"}}, {
            "plugin.py": "def register(mv):\n    pass\n", "settings.schema.json": json.dumps(SCHEMA),
            "defaults/settings.json": json.dumps({"schema": 1, "level": 1})})
        self.enable("veteran")
        settings_files.path(self.root, "veteran").parent.mkdir(exist_ok=True)
        settings_files.path(self.root, "veteran").write_text(json.dumps({"schema": 1, "level": 2}))
        self.assertEqual(plugins.notes(self.root), [])
        (self.root / "veteran.json").write_text(json.dumps({"level": 1}))  # back from an old branch, say
        note = ("veteran: veteran.json is here but ignored: its settings were converted to "
                "plugin-settings/veteran.json already; remove veteran.json")
        self.assertEqual(plugins.notes(self.root), [note])
        doctor = self.mv2("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout)
        self.assertIn(note, doctor.stdout)


class WaitingOldFileTest(CliCase):
    """While an old settings file waits to be converted, the plugin is off here (its defaults never stand in for
    the real settings), and mv2 plugins, plugins info and plugins settings say so, as doctor does (COMPACTION-7)."""

    def setUp(self):
        super().setUp()
        self.addCleanup(loader.reset)
        self.write_plugin("veteran", {"module": "plugin", "settings": {**SETTINGS, "legacy": "veteran.json"}}, {
            "plugin.py": "def register(mv):\n    pass\n", "settings.schema.json": json.dumps(SCHEMA),
            "defaults/settings.json": json.dumps({"schema": 1, "level": 1})})
        self.enable("veteran")
        (self.root / "veteran.json").write_text('{"level": tru')  # cannot be converted as it is

    def test_every_view_says_it_is_off_until_converted(self):
        for when in ("before its conversion is tried", "after it failed"):
            with self.subTest(when):
                self.assertEqual(self.state("veteran"), "unusable")
                info = self.mv2("plugins", "info", "veteran").stdout
                self.assertIn("unusable", info)
                self.assertIn("off until veteran.json is converted: run mv2 plugins migrate --apply", info)
                shown = self.mv2("plugins", "settings", "veteran").stdout
                self.assertIn("Cannot be used", shown)
                self.assertIn("veteran.json is not converted yet: run mv2 plugins migrate --apply", shown)
                self.assertFalse(settings_files.path(self.root, "veteran").exists())  # no defaults written
                doctor = self.mv2("doctor")
                self.assertEqual(doctor.returncode, 1)
                self.assertEqual(doctor.stdout.count("veteran.json"), 1, doctor.stdout)  # said once
        self.assertTrue((self.root / "veteran.json").is_file())

    def test_once_converted_it_is_on(self):
        (self.root / "veteran.json").write_text(json.dumps({"level": 2}))
        loader.reset()
        self.write_plugin("veteran", {"module": "plugin", "settings": {**SETTINGS, "legacy": "veteran.json"}}, {
            "plugin.py": ("def register(mv):\n    @mv.settings.migrate\n    def migrate(data, source):\n"
                          "        return {'schema': 1, 'level': data['level']}\n")})
        self.assertEqual(self.mv2("plugins", "migrate", "--apply").returncode, 0)
        self.assertEqual(self.state("veteran"), "on")


@unittest.skipUnless((PROJECT / "plugins" / "compaction" / "plugin.json").is_file(),
                     "needs plugins/compaction: mv2 plugins install compaction")
class CompactionSettingsTest(CliCase):
    """qa-compaction's cases, with GOAT's own schema and check."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.mv2("plugins", "enable", "compaction").returncode, 0)
        self.file = self.root / "plugin-settings" / "compaction.json"
        if not self.file.is_file():
            self.assertEqual(self.mv2("plugins", "settings", "compaction", "--set", "default.k=2").returncode, 0)

    def test_settings_edits_the_plugin_refuses_are_not_written(self):
        before = self.file.read_text()
        for edit in ("harnesses.claude.builder.effort=ultra", "default.k=-1", "default.scheduler=bogus",
                     "enabled=true", "harnesses.claude.builder.claude_max_retries=99",
                     'default.builder={"model": "haiku"}'):
            with self.subTest(edit=edit):
                result = self.mv2("plugins", "settings", "compaction", "--set", edit)
                written = self.file.read_text()
                self.file.write_text(before)
                self.assertNotEqual(result.returncode, 0, f"accepted {edit}: {result.stdout}{result.stderr}")
                self.assertEqual(written, before, f"{edit} was written")

    def test_an_unconverted_compaction_json_reads_unusable_not_on(self):
        self.file.unlink(missing_ok=True)
        (self.root / "compaction.json").write_text('{"schema": 1, "enabled": tru')
        self.assertEqual(self.mv2("list").returncode, 0)  # the conversion runs and fails, leaving the file
        self.assertTrue((self.root / "compaction.json").is_file())
        self.assertIn("compaction.json", self.mv2("doctor").stdout)
        self.assertEqual(self.state("compaction"), "unusable")
        shown = self.mv2("plugins", "settings", "compaction")
        self.assertIn("compaction.json", shown.stdout + shown.stderr)

    def test_plugins_says_unusable_whenever_doctor_says_the_settings_cannot_be_used(self):
        self.file.write_text('{"schema": 2, "default": {')
        self.assertEqual(self.state("compaction"), "unusable")
        for content in ({"schema": 2, "enabled": True},
                        {"schema": 2, "harnesses": {"claude": {"builder": {"effort": "ultra"}}}},
                        {"schema": 2, "default": {"k": -1}}):
            with self.subTest(content=content):
                self.file.write_text(json.dumps(content))
                self.assertIn("its settings cannot be used", self.mv2("doctor").stdout)
                self.assertEqual(self.state("compaction"), "unusable")
