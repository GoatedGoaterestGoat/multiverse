"""The bundled template installs cleanly and keeps prompt bodies out of the bootstrap."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import universe
from multiverse.tools import init
from multiverse.ui.backend import LocalBackend
from multiverse.universe import catalog
from multiverse.universe import templates
from multiverse.store import read, write
from isolation import isolate


class BaseTemplateTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()

    def test_install_has_fresh_identity_empty_state_and_declarative_adapter(self):
        """Installing or cloning a universe or subverse never copies state/ and gives each a fresh id; a break goes
        unnoticed because the clone works while carrying another universe's sessions, and duplicate ids break pane-tag
        ownership."""
        work = self.work
        root = templates.create(work / "base", "base", "base", work)
        with self.subTest("a clone of an installed universe"):
            write(root / "state" / "private-session.json", {"secret": "never clone"})
            clone = templates.create(work / "base-clone", "base", str(root), work)
            self.assertNotEqual(read(root / "universe.json")["id"], read(clone / "universe.json")["id"])
            self.assertEqual(list((clone / "state" / "agents").iterdir()), [])
            self.assertFalse((clone / "state" / "private-session.json").exists())
            self.assertEqual(universe.workspace(clone), work)
            for harness in ("claude", "codex", "pi"):
                self.assertEqual(read(clone / "harnesses" / harness / "harness.json"),
                                 read(templates.TEMPLATES / "base" / "harnesses" / harness / "harness.json"))
            self.assertFalse(any(path.is_symlink() for path in clone.rglob("*")))
        with self.subTest("a recursive clone, its subverses included"):
            child = templates.create(root / "subverses" / "review", "review", workdir=work)
            templates.create(child / "subverses" / "verify", "verify", workdir=work)
            (child / "state" / "secret").write_text("never clone live state")
            clone = templates.create(work / "copy", "copy", str(root), work)
            old = list(universe.tree(root))
            new = list(universe.tree(clone))
            self.assertEqual(len(new), 3)
            self.assertFalse((new[1] / "state" / "secret").exists())
            for a, b in zip(old, new):
                self.assertNotEqual(read(a / "universe.json")["id"], read(b / "universe.json")["id"])
                self.assertEqual(universe.workspace(b), work)
            self.assertEqual(universe.resolve(child), child)
            self.assertEqual(universe.parent(child), root)
            with self.assertRaises(ValueError):
                templates.create(root, "overwrite")
        with self.subTest("init and the dashboard's new subverse"):
            installed = init.run(directory=str(work / "fresh"), name="fresh")
            self.assertEqual(installed["name"], "fresh")
            subverse = LocalBackend().subverse(root, {"name": "child"})
            self.assertEqual(subverse, root / "subverses/child")
            for made in (Path(installed["universe"]), subverse):
                self.assertNotEqual(read(made / "universe.json")["id"], read(root / "universe.json")["id"])
                self.assertEqual(list((made / "state/agents").iterdir()), [])

    def test_bootstrap_is_short_and_workflows_load_only_on_request(self):
        """The bootstrap stays within its word budget and skill and workflow bodies load only on request; a break goes
        unnoticed because a longer bootstrap still works and only costs every agent context on every launch."""
        root = templates.create(self.work / "base", "base", "base", self.work)
        (root / "workflows" / "review" / "prompt.md").write_text("REVIEW_BODY_SENTINEL\n" * 1000)
        (root / "skills" / "coordination" / "SKILL.md").write_text("SENTINEL_LONG_CAPABILITY\n" * 1000)
        prompt = catalog.bootstrap(root, {"id": "test", "name": "test", "universe_id": "test"})
        self.assertLess(len(prompt.split()), 260)
        self.assertNotIn("REVIEW_BODY_SENTINEL", prompt)
        self.assertNotIn("SENTINEL_LONG_CAPABILITY", prompt)
        self.assertIn("REVIEW_BODY_SENTINEL", catalog.command(root, "review"))
        self.assertIn("SENTINEL_LONG_CAPABILITY", catalog.command(root, "coordination"))
        self.assertIn("review", [entry["name"] for entry in catalog.catalog(root)])


if __name__ == "__main__":
    unittest.main()
