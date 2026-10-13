"""Refresh updates unedited definition files from the template and never overwrites local edits."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import cli
from multiverse.universe import templates
from multiverse.store import read, write
from multiverse.tools import refresh


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.template = self.work / "template"
        write(self.template / "universe.json", {"schema": 1, "name": "template", "workspace": ".."})
        self.put("bootstrap.md", "Bootstrap v1\n")
        self.put("skills/coordination/SKILL.md", "Coordinate v1\n")
        self.put("harnesses/codex/harness.json", '{"schema": 2, "adapter": "codex", "settings": {}}\n')
        write(self.template / "subverses" / "child" / "universe.json", {"schema": 1, "name": "child", "workspace": ".."})
        self.put("subverses/child/bootstrap.md", "Child v1\n")
        self.root = templates.create(self.work / "project" / ".multiverse", "project", str(self.template), self.work / "project")

    def put(self, path, text, base=None):
        target = (base or self.template) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def plan(self, **options):
        return refresh.run(self.root, **options)["universes"][0]

    def actions(self, **options):
        return {change["path"]: change["action"] for change in self.plan(**options)["changes"]}

    def test_install_records_definition_files_but_never_identity_or_state(self):
        manifest = read(self.root / templates.MANIFEST)
        self.assertEqual(manifest["template"], str(self.template))
        self.assertEqual(sorted(manifest["files"]), ["bootstrap.md", "harnesses/codex/harness.json",
                                                     "skills/coordination/SKILL.md"])
        child = read(self.root / "subverses" / "child" / templates.MANIFEST)
        self.assertEqual((child["source"], sorted(child["files"])), ("subverses/child", ["bootstrap.md"]))
        self.assertEqual(self.plan()["changes"], [])

    def test_dry_run_writes_nothing(self):
        self.put("bootstrap.md", "Bootstrap v2\n")
        before = (self.root / templates.MANIFEST).read_text()
        self.assertEqual(self.actions(), {"bootstrap.md": "update"})
        self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v1\n")
        self.assertEqual((self.root / templates.MANIFEST).read_text(), before)
        self.assertFalse((self.root / "trash").exists())

    def test_apply_updates_unedited_files_and_saves_the_replaced_copy(self):
        self.put("bootstrap.md", "Bootstrap v2\n")
        self.put("skills/review/SKILL.md", "Review v1\n")
        result = self.plan(apply=True)
        self.assertEqual(sorted(result["applied"]["written"]), ["bootstrap.md", "skills/review/SKILL.md"])
        self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v2\n")
        self.assertEqual((self.root / "skills/review/SKILL.md").read_text(), "Review v1\n")
        backup = Path(result["applied"]["trash"])
        self.assertEqual((backup / "bootstrap.md").read_text(), "Bootstrap v1\n")
        self.assertEqual(read(backup / ".removed.json")["files"], ["bootstrap.md"])
        self.assertEqual(self.plan()["changes"], [])

    def test_local_edits_are_kept_and_conflicts_need_take(self):
        self.put("bootstrap.md", "My bootstrap\n", self.root)
        self.assertEqual(self.actions(), {"bootstrap.md": "local"})
        self.put("bootstrap.md", "Bootstrap v2\n")
        self.assertEqual(self.actions(apply=True), {"bootstrap.md": "conflict"})
        self.assertEqual((self.root / "bootstrap.md").read_text(), "My bootstrap\n")
        self.assertEqual(self.actions(), {"bootstrap.md": "conflict"})
        self.assertEqual(self.actions(take=["bootstrap.md"], apply=True), {"bootstrap.md": "update"})
        self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v2\n")
        self.assertEqual(self.plan()["changes"], [])

    def test_local_deletions_stay_deleted_and_template_removals_are_only_reported(self):
        (self.root / "skills/coordination/SKILL.md").unlink()
        (self.template / "harnesses/codex/harness.json").unlink()
        self.assertEqual(self.actions(apply=True), {"skills/coordination/SKILL.md": "deleted",
                                                    "harnesses/codex/harness.json": "removed"})
        self.assertFalse((self.root / "skills/coordination/SKILL.md").exists())
        self.assertTrue((self.root / "harnesses/codex/harness.json").exists())

    def test_without_a_manifest_differences_are_unknown_until_taken(self):
        (self.root / templates.MANIFEST).unlink()
        self.put("harnesses/codex/harness.json", '{"schema": 2, "adapter": "codex", "settings": {"inbox_bridge": true}}\n', self.root)
        self.put("bootstrap.md", "Bootstrap v2\n")
        self.assertEqual(self.actions(apply=True), {"bootstrap.md": "unknown", "harnesses/codex/harness.json": "unknown"})
        self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v1\n")
        # Kept files now count as local edits of the current template.
        self.assertEqual(self.actions(), {"bootstrap.md": "local", "harnesses/codex/harness.json": "local"})
        self.put("harnesses/codex/harness.json", '{"schema": 2, "adapter": "codex", "settings": {"model": "x"}}\n')
        self.assertEqual(self.actions()["harnesses/codex/harness.json"], "conflict")
        self.assertEqual(self.actions(take=["bootstrap.md"], apply=True)["bootstrap.md"], "update")
        self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v2\n")

    def test_plugin_settings_wait_for_the_migration_of_a_legacy_file(self):
        # A universe from before compaction became a plugin: its compaction.json, and the template's new settings file.
        write(self.root / "compaction.json", {"schema": 1, "harnesses": {"claude": {"enabled": True}}})
        self.put("plugin-settings/compaction.json", '{"schema": 2}\n')
        self.assertEqual(self.actions()["plugin-settings/compaction.json"], "deferred")
        reason = {c["path"]: c["reason"] for c in self.plan()["changes"]}["plugin-settings/compaction.json"]
        self.assertIn("run mv2 plugins migrate", reason)
        refresh.run(self.root, apply=True)
        self.assertFalse((self.root / "plugin-settings/compaction.json").exists())  # nothing in front of the real settings
        # After mv2 plugins migrate wrote the universe's own settings, they are kept.
        write(self.root / "plugin-settings/compaction.json", {"schema": 2, "default": {"budget": 200000}})
        (self.root / "compaction.json").unlink()
        self.assertEqual(self.actions()["plugin-settings/compaction.json"], "local")
        self.assertEqual(self.actions(take=["plugin-settings/compaction.json"])["plugin-settings/compaction.json"], "update")

    def test_take_rejects_paths_outside_the_template(self):
        with self.assertRaises(ValueError):
            self.plan(take=["universe.json"])

    def test_recursive_refresh_uses_each_subverse_source(self):
        self.put("subverses/child/bootstrap.md", "Child v2\n")
        self.assertEqual(self.plan()["changes"], [])
        plans = refresh.run(self.root, recursive=True, apply=True)["universes"]
        self.assertEqual([plan["name"] for plan in plans], ["project", "child"])
        self.assertEqual((self.root / "subverses/child/bootstrap.md").read_text(), "Child v2\n")

    def test_cli_prints_a_readable_dry_run(self):
        self.put("bootstrap.md", "Bootstrap v2\n")
        args = cli.parser().parse_args(["-u", str(self.root), "refresh"])
        text = refresh.format_output(cli.run(args))
        self.assertIn("update   bootstrap.md", text)
        self.assertIn("Dry run: nothing written", text)


if __name__ == "__main__":
    unittest.main()
