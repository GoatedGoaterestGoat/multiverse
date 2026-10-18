"""Refresh updates unedited definition files from the template and never overwrites local edits."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.universe import templates
from multiverse.store import read, write
from multiverse.tools import refresh
from isolation import isolate


class RefreshTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
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

    def test_refresh_never_overwrites_or_resurrects_local_state(self):
        """refresh decides each file from manifest, template and local hashes: unedited files update with a backup,
        edits and deletions are kept, a missing manifest means unknown, a file new to the template is added, take
        refuses paths outside it, and a template that is gone is refused naming the universe and how to compare;
        fragile because a missing manifest flips the default, and a break silently overwrites a user's edited bootstrap
        or harness.json (LC-7, LC-13, LC-14)."""
        manifest = (self.root / templates.MANIFEST).read_text()
        with self.subTest('a dry run writes nothing'):
            self.put("bootstrap.md", "Bootstrap v2\n")
            self.put("skills/review/SKILL.md", "Review v1\n")
            self.assertEqual(self.actions(), {"bootstrap.md": "update", "skills/review/SKILL.md": "add"})
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v1\n")
            self.assertFalse((self.root / "skills/review/SKILL.md").exists())
            self.assertEqual((self.root / templates.MANIFEST).read_text(), manifest)
            self.assertFalse((self.root / "trash").exists())
        with self.subTest('apply updates unedited files, adds new ones and saves the replaced copy'):
            result = self.plan(apply=True)
            self.assertEqual(sorted(result["applied"]["written"]), ["bootstrap.md", "skills/review/SKILL.md"])
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v2\n")
            self.assertEqual((self.root / "skills/review/SKILL.md").read_text(), "Review v1\n")
            backup = Path(result["applied"]["trash"])
            self.assertEqual((backup / "bootstrap.md").read_text(), "Bootstrap v1\n")
            self.assertEqual(read(backup / ".removed.json")["files"], ["bootstrap.md"])
            self.assertEqual(self.plan()["changes"], [])
        with self.subTest('local edits are kept, and conflicts need take'):
            self.put("bootstrap.md", "My bootstrap\n", self.root)
            self.assertEqual(self.actions(), {"bootstrap.md": "local"})
            self.put("bootstrap.md", "Bootstrap v3\n")
            self.assertEqual(self.actions(apply=True), {"bootstrap.md": "conflict"})
            self.assertEqual((self.root / "bootstrap.md").read_text(), "My bootstrap\n")
            self.assertEqual(self.actions(), {"bootstrap.md": "conflict"})
            self.assertEqual(self.actions(take=["bootstrap.md"], apply=True), {"bootstrap.md": "update"})
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v3\n")
            self.assertEqual(self.plan()["changes"], [])
        with self.subTest('local deletions stay deleted, and template removals are only reported'):
            (self.root / "skills/coordination/SKILL.md").unlink()
            (self.template / "harnesses/codex/harness.json").unlink()
            self.assertEqual(self.actions(apply=True), {"skills/coordination/SKILL.md": "deleted",
                                                        "harnesses/codex/harness.json": "removed"})
            self.assertFalse((self.root / "skills/coordination/SKILL.md").exists())
            self.assertTrue((self.root / "harnesses/codex/harness.json").exists())
            # Back as they were installed, so that only the next case's changes differ.
            self.put("skills/coordination/SKILL.md", "Coordinate v1\n", self.root)
            self.put("harnesses/codex/harness.json", '{"schema": 2, "adapter": "codex", "settings": {}}\n')
        with self.subTest('without a manifest, differences are unknown until taken'):
            (self.root / templates.MANIFEST).unlink()
            self.put("harnesses/codex/harness.json",
                     '{"schema": 2, "adapter": "codex", "settings": {"inbox_bridge": true}}\n', self.root)
            self.put("bootstrap.md", "Bootstrap v4\n")
            self.assertEqual(self.actions(apply=True),
                             {"bootstrap.md": "unknown", "harnesses/codex/harness.json": "unknown"})
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v3\n")
            # Kept files now count as local edits of the current template.
            self.assertEqual(self.actions(), {"bootstrap.md": "local", "harnesses/codex/harness.json": "local"})
            self.put("harnesses/codex/harness.json", '{"schema": 2, "adapter": "codex", "settings": {"model": "x"}}\n')
            self.assertEqual(self.actions()["harnesses/codex/harness.json"], "conflict")
            self.assertEqual(self.actions(take=["bootstrap.md"], apply=True)["bootstrap.md"], "update")
            self.assertEqual((self.root / "bootstrap.md").read_text(), "Bootstrap v4\n")
        with self.subTest('take refuses paths outside the template'), self.assertRaises(ValueError):
            self.plan(take=["universe.json"])
        with self.subTest('a template that is gone names the universe, the template and how to compare'):
            shutil.rmtree(self.template)
            with self.assertRaises(ValueError) as caught:
                self.plan()
            self.assertIn(str(self.root), str(caught.exception))
            self.assertIn(str(self.template), str(caught.exception))
            self.assertIn(f"mv2 -u {self.root} refresh --template", str(caught.exception))
            with self.assertRaisesRegex(ValueError, "elsewhere"):
                self.plan(template="elsewhere")


if __name__ == "__main__":
    unittest.main()
