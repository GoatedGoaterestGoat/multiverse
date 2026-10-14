"""First-use conversion of a plugin's pre-plugin settings file: once, where mv2 starts work in a universe."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase

SRC = Path(__file__).resolve().parents[1] / "src"
NOTES = """
import json


def register(mv):
    def migrate(old, source):
        import os
        if os.environ.get("SEEN"):
            with open(os.environ["SEEN"], "a") as out:
                out.write(str(mv.universe().path) + "\\n")
        if source == "legacy":
            if not isinstance(old.get("colour"), str):
                raise ValueError("colour must be text")
            return {"schema": 2, "colour": old["colour"]}, bool(old.get("enabled", True))
        return old
    mv.settings.migrate(migrate)

    def show(arguments):
        print(json.dumps(mv.settings.read()[0]))
    mv.helper.action("show", show)
"""


class FirstUseTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("notes", {"module": "plugin", "extends": ["helper"],
                                    "settings": {"version": 2, "legacy": "notes.json"}}, {"plugin.py": NOTES})

    def legacy(self, data, root=None):
        path = Path(root or self.root) / "notes.json"
        path.write_text(json.dumps(data))
        return path

    def mv2(self, *arguments, module="multiverse.cli", env=None):
        environment = {**os.environ, "PYTHONPATH": str(SRC), **(env or {})}
        return subprocess.run([sys.executable, "-m", module, "-u", str(self.root), *arguments], env=environment,
                              capture_output=True, text=True, timeout=60)

    def converted(self, root=None):
        path = Path(root or self.root) / "plugin-settings" / "notes.json"
        return json.loads(path.read_text()) if path.exists() else None

    def backups(self, root=None):
        return sorted((Path(root or self.root) / "trash").glob("*-plugins-migrate/notes.json"))

    def test_the_first_command_converts_once_and_says_so_on_stderr(self):
        self.legacy({"colour": "teal"})
        first = self.mv2("list", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        json.loads(first.stdout)  # --json output is untouched
        [line] = [line for line in first.stderr.splitlines() if "notes" in line]
        self.assertRegex(line, r"^mv2: notes: converted notes\.json to plugin-settings/notes\.json, turned it on in "
                               r"plugins\.json \(the old file is in trash/\S+-plugins-migrate/\)$")
        self.assertEqual(self.converted(), {"schema": 2, "colour": "teal"})
        self.assertFalse((self.root / "notes.json").exists())
        self.assertEqual(len(self.backups()), 1)
        self.assertTrue(json.loads((self.root / "plugins.json").read_text())["enabled"]["notes"])
        second = self.mv2("list")
        self.assertNotIn("notes", second.stderr)
        self.assertEqual(len(self.backups()), 1)

    def test_a_switch_plugins_json_already_has_is_kept_and_not_claimed(self):
        self.enable("notes", on=False)
        self.legacy({"colour": "teal"})
        line = self.mv2("list").stderr
        self.assertIn("mv2: notes: converted notes.json to plugin-settings/notes.json (the old file", line)
        self.assertFalse(json.loads((self.root / "plugins.json").read_text())["enabled"]["notes"])

    def test_a_subverse_is_converted_by_a_command_in_its_parent(self):
        child = self.subverse("child")
        self.legacy({"colour": "red"}, child)
        result = self.mv2("list")
        self.assertIn("mv2: notes in child: converted notes.json", result.stderr)
        self.assertEqual(self.converted(child), {"schema": 2, "colour": "red"})

    def test_two_processes_at_once_convert_once(self):
        self.legacy({"colour": "teal"})
        environment = {**os.environ, "PYTHONPATH": str(SRC)}
        runs = [subprocess.Popen([sys.executable, "-m", "multiverse.cli", "-u", str(self.root), "list"], env=environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = [run.communicate(timeout=60) for run in runs]
        self.assertEqual([run.returncode for run in runs], [0, 0])
        self.assertEqual(sum("converted notes.json" in stderr for _, stderr in outputs), 1, outputs)
        self.assertEqual(len(self.backups()), 1)

    def test_a_failed_conversion_is_said_once_reported_by_doctor_and_retried_when_the_file_changes(self):
        path = self.legacy({"colour": 3})
        first = self.mv2("list")
        self.assertEqual(first.returncode, 0)
        self.assertIn("mv2: notes: could not convert notes.json (notes's migration failed: ValueError: colour must be "
                      "text); it is unchanged: fix it and run mv2 plugins migrate --apply", first.stderr)
        self.assertTrue(path.exists())
        self.assertIsNone(self.converted())
        self.assertNotIn("notes", self.mv2("list").stderr)  # said once
        loader.reset()
        self.assertTrue(any("notes.json could not be converted" in problem for problem in plugins.problems(self.root)))
        self.legacy({"colour": "blue"})
        self.assertIn("converted notes.json", self.mv2("list").stderr)
        self.assertEqual(self.converted(), {"schema": 2, "colour": "blue"})
        loader.reset()
        self.assertFalse(any("could not be converted" in problem for problem in plugins.problems(self.root)))

    def test_where_it_never_runs(self):
        self.legacy({"colour": "teal"})
        self.assertNotIn("converted", self.mv2("plugins", "list").stderr)  # managed by hand there
        self.assertNotIn("converted", self.mv2("list", env={"MV2_PLUGINS": "off"}).stderr)
        self.assertIsNone(self.converted())
        planned = json.loads(self.mv2("plugins", "migrate", "--json").stdout)["planned"]
        self.assertEqual([item["plugin"] for item in planned], ["notes"])

    def test_a_plugin_helper_converts_before_its_action(self):
        self.legacy({"colour": "teal"})
        result = self.mv2("run", "notes", "show", module="multiverse.plugins")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("converted notes.json", result.stderr)
        self.assertEqual(json.loads(result.stdout), {"schema": 2, "colour": "teal"})

    def test_an_untrusted_universe_plugin_is_never_converted_or_imported(self):
        self.write_plugin("local", {"module": "plugin", "settings": {"version": 2, "legacy": "local.json"}},
                          {"plugin.py": "raise SystemExit('imported')\n"}, where=self.root / "plugins")
        (self.root / "local.json").write_text("{}")
        self.assertEqual(plugins.migrate_on_first_use(self.root), [])
        self.assertTrue((self.root / "local.json").exists())
        self.assertFalse((self.root / "state" / "plugins" / plugins.MIGRATE_LOCK).exists())

    def test_nothing_pending_costs_no_lock_and_no_plugin_import(self):
        self.assertEqual(plugins.migrate_on_first_use(self.root), [])
        self.assertFalse((self.root / "state" / "plugins" / plugins.MIGRATE_LOCK).exists())
        self.assertFalse(loader._loaded)


class ConversionSafetyTest(FirstUseTest):
    """A conversion either happens or leaves every file as it was, and says which."""

    def test_a_broken_switch_file_stops_the_conversion_before_anything_changes(self):
        self.legacy({"colour": "teal"})
        (self.root / "plugins.json").write_text("{bad-json")
        [line] = plugins.migrate_on_first_use(self.root)
        self.assertIn("could not convert notes.json (plugins.json cannot be used, so the switch for notes cannot be "
                      "set", line)
        self.assertTrue(line.endswith("it is unchanged: fix it and run mv2 plugins migrate --apply"))
        self.assertTrue((self.root / "notes.json").exists())
        self.assertIsNone(self.converted())
        self.assertEqual(self.backups(), [])
        self.assertEqual((self.root / "plugins.json").read_text(), "{bad-json")

    def test_a_failure_after_writing_puts_everything_back(self):
        from unittest import mock
        self.legacy({"colour": "teal"})
        with mock.patch.object(plugins.config, "set_enabled", side_effect=OSError("disk full")):
            [line] = plugins.migrate_on_first_use(self.root)
        self.assertIn("could not convert notes.json (the switch in plugins.json could not be set: disk full); "
                      "it is unchanged", line)
        self.assertTrue((self.root / "notes.json").exists())
        self.assertIsNone(self.converted())
        self.assertEqual(self.backups(), [])

    def test_a_file_changed_meanwhile_is_left_alone_and_said_so(self):
        from unittest import mock
        self.legacy({"colour": "teal"})
        target = self.root / "plugin-settings" / "notes.json"

        def someone_edits(*arguments):
            target.write_text('{"schema": 2, "colour": "edited"}')
            raise OSError("disk full")
        with mock.patch.object(plugins.config, "set_enabled", side_effect=someone_edits):
            [line] = plugins.migrate_on_first_use(self.root)
        self.assertIn("check plugin-settings/notes.json and plugins.json by hand", line)
        self.assertNotIn("unchanged", line)
        self.assertEqual(self.converted(), {"schema": 2, "colour": "edited"})
        self.assertTrue((self.root / "notes.json").exists())

    def test_a_target_that_appears_while_writing_is_never_replaced(self):
        from unittest import mock
        self.legacy({"colour": "teal"})
        target = self.root / "plugin-settings" / "notes.json"
        real = os.link

        def someone_writes_first(source, destination, *arguments, **options):
            Path(destination).write_text('{"schema": 2, "colour": "theirs"}')
            return real(source, destination, *arguments, **options)  # fails: a file is there now
        with mock.patch.object(plugins.os, "link", someone_writes_first):
            [line] = plugins.migrate_on_first_use(self.root)
        self.assertIn("could not convert notes.json (plugin-settings/notes.json appeared meanwhile); it is unchanged",
                      line)
        self.assertEqual(self.converted(), {"schema": 2, "colour": "theirs"})
        self.assertTrue((self.root / "notes.json").exists())
        self.assertEqual(list(target.parent.glob(".migrate-*")), [])

    def test_a_write_that_fails_publishes_nothing_and_touches_no_other_file(self):
        from unittest import mock
        self.legacy({"colour": "teal"})
        with mock.patch.object(plugins.os, "link", side_effect=OSError("I/O error")):  # publishing fails
            [line] = plugins.migrate_on_first_use(self.root)
        self.assertIn("could not convert notes.json (notes.json could not be written: I/O error); it is unchanged", line)
        self.assertIsNone(self.converted())
        self.assertTrue((self.root / "notes.json").exists())
        self.assertEqual(list((self.root / "plugin-settings").glob(".migrate-*")), [])

    def test_a_file_replaced_before_the_undo_is_left_alone(self):
        from unittest import mock
        self.legacy({"colour": "teal"})
        target = self.root / "plugin-settings" / "notes.json"

        def someone_replaces(*arguments):
            other = target.with_name("other.json")
            other.write_text('{"schema": 2, "colour": "replaced"}')
            os.replace(other, target)
            raise OSError("disk full")
        with mock.patch.object(plugins.config, "set_enabled", side_effect=someone_replaces):
            [line] = plugins.migrate_on_first_use(self.root)
        self.assertIn("check plugin-settings/notes.json and plugins.json by hand", line)
        self.assertEqual(self.converted(), {"schema": 2, "colour": "replaced"})

    def test_an_old_file_that_cannot_be_removed_is_said(self):
        from unittest import mock
        self.legacy({"colour": "teal"})
        real = Path.unlink

        def refuse(path, *arguments, **options):
            if path.name == "notes.json" and path.parent == self.root:
                raise PermissionError("read-only")
            return real(path, *arguments, **options)
        with mock.patch.object(Path, "unlink", refuse):
            [line] = plugins.migrate_on_first_use(self.root)
        self.assertIn("converted notes.json to plugin-settings/notes.json", line)
        self.assertIn("but notes.json could not be moved to trash (read-only): remove it yourself", line)
        self.assertEqual(self.converted(), {"schema": 2, "colour": "teal"})
        self.assertEqual(plugins.migrate_on_first_use(self.root), [])  # converted: not pending again

    # The FirstUseTest tests are not run again here.
    test_the_first_command_converts_once_and_says_so_on_stderr = None
    test_a_switch_plugins_json_already_has_is_kept_and_not_claimed = None
    test_a_subverse_is_converted_by_a_command_in_its_parent = None
    test_two_processes_at_once_convert_once = None
    test_a_failed_conversion_is_said_once_reported_by_doctor_and_retried_when_the_file_changes = None
    test_where_it_never_runs = None
    test_a_plugin_helper_converts_before_its_action = None
    test_an_untrusted_universe_plugin_is_never_converted_or_imported = None
    test_nothing_pending_costs_no_lock_and_no_plugin_import = None


class UpdateConversionTest(FirstUseTest):
    """mv2 update converts with the code it just updated to, in a process of its own."""

    def test_update_converts_the_callers_universe_with_the_checkouts_code(self):
        from multiverse.tools import update
        checkout = SRC.parent
        self.assertEqual(update._convert_settings(checkout, self.root)["state"], "none")
        self.legacy({"colour": "teal"})
        result = update._convert_settings(checkout, self.root)
        self.assertEqual(result["state"], "converted", result)
        self.assertEqual(result["lines"], ["notes: converted notes.json to notes.json"])
        self.assertEqual(self.converted(), {"schema": 2, "colour": "teal"})
        text = update.format_output({"ok": True, "check": False, "json": False, "commits": 0, "after": "a" * 40,
                                     "environment": {"state": "synced"}, "notes": [], "settings": result})
        self.assertIn(f"Plugin settings converted in {self.root}:", text)

    def test_failed_and_partial_conversions_are_reported_as_such(self):
        from multiverse.tools import update
        self.write_plugin("memo", {"module": "plugin", "settings": {"version": 2, "legacy": "memo.json"}}, {
            "plugin.py": "def register(mv):\n    mv.settings.migrate(lambda old, source: ({'schema': 2}, True))\n"})
        self.legacy({"colour": 3})
        failed = update._convert_settings(SRC.parent, self.root)
        self.assertEqual(failed["state"], "failed", failed)
        self.assertIn("notes: not converted: notes's migration failed: ValueError: colour must be text", failed["lines"])
        self.assertTrue((self.root / "notes.json").exists())
        (self.root / "memo.json").write_text("{}")
        partial = update._convert_settings(SRC.parent, self.root)
        self.assertEqual(partial["state"], "partial", partial)
        self.assertIn("memo: converted memo.json to memo.json", partial["lines"])
        text = update.format_output({"ok": True, "check": False, "json": False, "commits": 0, "after": "a" * 40,
                                     "environment": {"state": "synced"}, "notes": [], "settings": partial})
        self.assertIn("Plugin settings partly converted in", text)
        self.assertIn("run mv2 plugins migrate --apply --recursive there", text)

    def test_noise_and_unreadable_output_never_count_as_converted(self):
        from unittest import mock
        from multiverse.tools import update
        empty = json.dumps({"universe": str(self.root), "planned": [], "applied": [], "apply": True})
        failing = json.dumps({"universe": str(self.root), "planned": [], "applied": [], "apply": True, "ok": False})
        for stdout, stderr, code, state in ((empty, "some warning\n", 0, "none"),
                                            ("not json \ufffd", "", 0, "failed"),
                                            ("", "Traceback ...", 1, "failed"),
                                            ('{"planned": 3, "applied": []}', "", 0, "failed"),
                                            ('{"planned": [3], "applied": []}', "", 0, "failed"),
                                            ('{"planned": [], "applied": [3]}', "", 0, "failed"),
                                            (failing, "conversion failed", 1, "failed"),
                                            (empty, "", 1, "failed")):
            with self.subTest(stdout=stdout):
                done = subprocess.CompletedProcess([], code, stdout, stderr)
                with mock.patch.object(update.subprocess, "run", return_value=done):
                    self.assertEqual(update._convert_settings(SRC.parent, self.root)["state"], state)

    def test_a_failed_apply_exits_one_in_both_output_modes(self):
        self.legacy({"colour": 3})
        readable = self.mv2("plugins", "migrate", "--apply")
        self.assertEqual(readable.returncode, 1, readable.stdout)
        self.assertIn("! notes:", readable.stdout)
        machine = self.mv2("plugins", "migrate", "--apply", "--json")
        self.assertEqual(machine.returncode, 1)
        self.assertFalse(json.loads(machine.stdout)["ok"])
        self.legacy({"colour": "teal"})
        self.assertEqual(self.mv2("plugins", "migrate", "--apply").returncode, 0)

    def test_migrations_see_the_universe_being_converted(self):
        from multiverse.tools import update
        other = self.subverse("other")
        child = self.subverse("child")
        seen = self.work / "seen.txt"
        self.legacy({"colour": "teal"})
        self.legacy({"colour": "red"}, child)
        os.environ.update(SEEN=str(seen), MV2_UNIVERSE=str(other))  # an ambient universe that is not the target
        self.assertEqual(update._convert_settings(SRC.parent, self.root)["state"], "converted")
        self.assertEqual(sorted(seen.read_text().split()), sorted([str(self.root), str(child)]))
        seen.unlink()
        self.legacy({"colour": "blue"}, other)
        os.environ["MV2_UNIVERSE"] = str(self.root)
        self.assertIn("notes:", " ".join(plugins.migrate_on_first_use(other)))  # in process, too
        self.assertEqual(seen.read_text().split(), [str(other)])
        self.assertEqual(os.environ["MV2_UNIVERSE"], str(self.root))  # put back afterwards

    def test_no_universe_is_skipped(self):
        from multiverse.tools import update
        nowhere = self.work / "nowhere"
        nowhere.mkdir()
        self.assertEqual(update._convert_settings(SRC.parent, nowhere)["state"], "skipped")

    # The FirstUseTest tests are not run again here.
    test_the_first_command_converts_once_and_says_so_on_stderr = None
    test_a_switch_plugins_json_already_has_is_kept_and_not_claimed = None
    test_a_subverse_is_converted_by_a_command_in_its_parent = None
    test_two_processes_at_once_convert_once = None
    test_a_failed_conversion_is_said_once_reported_by_doctor_and_retried_when_the_file_changes = None
    test_where_it_never_runs = None
    test_a_plugin_helper_converts_before_its_action = None
    test_an_untrusted_universe_plugin_is_never_converted_or_imported = None
    test_nothing_pending_costs_no_lock_and_no_plugin_import = None
    test_a_broken_switch_file_stops_the_conversion_before_anything_changes = None
    test_a_failure_after_writing_puts_everything_back = None
    test_a_file_changed_meanwhile_is_left_alone_and_said_so = None
    test_an_old_file_that_cannot_be_removed_is_said = None
    test_a_target_that_appears_while_writing_is_never_replaced = None
    test_a_write_that_fails_publishes_nothing_and_touches_no_other_file = None
    test_a_file_replaced_before_the_undo_is_left_alone = None
