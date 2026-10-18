"""An old settings file whose plugin is not installed is never silently ignored (PR #20 review, item 6)."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import harness, loader, settings
from multiverse.plugins.testing import PluginTestCase

NOT_INSTALLED = ("compaction.json is here but the compaction plugin isn't installed: run mv2 plugins install "
                 "compaction, or move compaction.json away to stop using its settings")


class KnownPluginTest(PluginTestCase):
    # PluginTestCase hides the checkout's plugins: compaction is not installed here.

    def old_file(self, data=None):
        """A universe made before plugins: the old file, and none of the template's plugin settings."""
        (self.root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)
        (self.root / "compaction.json").write_text(json.dumps(data or {"enabled": True}))

    def test_doctor_first_use_and_launches_say_so(self):
        self.assertNotIn("compaction", [found.name for found in loader.entries(self.root) if found.manifest])
        self.old_file()
        self.assertIn(NOT_INSTALLED, plugins.problems(self.root))
        self.assertEqual(plugins.migrate_on_first_use(self.root), [f"mv2: {NOT_INSTALLED}"])
        self.assertEqual(plugins.migrate_on_first_use(self.root), [])  # said once for this file
        plan = harness.launch_plan(harness.LaunchInfo(self.root, "a", "w", "spawn", "claude", "claude",
                                                      (2, 1, 299), True))
        self.assertIn(NOT_INSTALLED, plan.problems)
        codex = harness.launch_plan(harness.LaunchInfo(self.root, "a", "w", "spawn", "codex", "codex", None, True))
        self.assertNotIn(NOT_INSTALLED, codex.problems)  # compaction only ever took part in Claude launches

    def test_migrate_says_it_too(self):
        from multiverse.tools import plugins as tool
        self.old_file()
        result = tool.run(self.root, plugin_action="migrate", apply=True)
        self.assertEqual(result["not_installed"], [{"plugin": "compaction", "file": "compaction.json",
                                                    "universe": str(self.root)}])
        self.assertEqual(tool.text("migrate", result), f"! {NOT_INSTALLED}")

    def test_a_waiting_old_file_counts_whatever_the_plugins_state(self):
        self.old_file()
        self.assertEqual(plugins.waiting_settings(self.root), {"plugin-settings/compaction.json": "compaction.json"})
        self.enable("compaction")  # named in plugins.json, still not installed: a "missing" entry
        self.assertEqual(plugins.waiting_settings(self.root), {"plugin-settings/compaction.json": "compaction.json"})
        (self.root / "plugin-settings").mkdir(exist_ok=True)
        (self.root / "plugin-settings" / "compaction.json").write_text('{"schema": 2}')
        self.assertEqual(plugins.waiting_settings(self.root), {})

    def test_settings_are_never_built_in_front_of_a_waiting_old_file(self):
        directory = self.write_plugin("tree", {"settings": {"version": 2, "legacy": "tree.json"}})
        (self.root / "tree.json").write_text('{"budget": 5}')
        from multiverse.plugins import manifest
        with self.assertRaisesRegex(ValueError, "tree.json is still here and not converted"):
            settings.edit(self.root, manifest.load(directory), {"budget": 9})
        self.assertFalse(settings.path(self.root, "tree").exists())



class NotInstalledNoteTest(PluginTestCase):
    """A known optional plugin that is on but not installed, with no old file waiting, is a note, not a problem."""

    def test_on_but_not_installed_is_a_note(self):
        (self.root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)
        self.enable("compaction")  # as a template's plugins.json can
        self.assertFalse([problem for problem in plugins.problems(self.root) if "compaction" in problem])
        self.assertEqual(plugins.notes(self.root), [
            "compaction is on here but not installed (an optional plugin: mv2 plugins install compaction, which "
            "needs access to its repository)"])
        (self.root / "compaction.json").write_text("{}")  # with an old file waiting it is a problem again
        self.assertIn(NOT_INSTALLED, plugins.problems(self.root))
        self.assertEqual(plugins.notes(self.root), [])

    def test_doctor_says_it_as_a_note_and_finds_no_problem(self):
        import contextlib
        import io
        from multiverse.tools import doctor
        (self.root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)
        self.enable("compaction")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.cli_run(self.root)  # exits only when it finds a problem
        self.assertIn("No problems.", out.getvalue())
        self.assertIn("Note: Plugin compaction is on here but not installed", out.getvalue())
        self.assertTrue(doctor.run(self.root)["ok"])


class NoUniverseLaunchTest(PluginTestCase):
    def test_a_launch_without_a_universe_still_gets_its_plugins(self):
        """Some callers plan a launch with no universe (an adapter's own tests): a known plugin that is not
        installed must not turn that into an empty plan."""
        plan = harness.launch_plan(harness.LaunchInfo(None, None, None, "spawn", "claude", "claude", (2, 1, 299), True))
        self.assertIsNone(getattr(plan, "error", None))
        self.assertEqual(plugins.not_installed(None), [])
