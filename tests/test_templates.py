"""The bundled template installs cleanly and keeps prompt bodies out of the bootstrap."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import universe
from multiverse.universe import catalog
from multiverse.universe import templates
from multiverse.store import read, write
from isolation import isolate


class BaseTemplateTest(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_install_has_fresh_identity_empty_state_and_declarative_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            root = templates.create(work / "base", "base", "base", work)
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

    def test_a_directory_without_universe_json_is_named_as_not_a_template(self):
        """Template lookup: a copied folder missing universe.json must say why, not "Unknown template"."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve() / "mytpl"
            (folder / "skills").mkdir(parents=True)
            (folder / "bootstrap.md").write_text("hello\n")
            with self.assertRaisesRegex(ValueError, "is not a template: it has no universe.json"):
                templates.template_source(str(folder))
            with self.assertRaisesRegex(ValueError, "Unknown template: no-such-template"):
                templates.template_source("no-such-template")

    def test_bootstrap_is_short_and_workflows_load_only_on_request(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            root = templates.create(work / "base", "base", "base", work)
            (root / "workflows" / "review" / "prompt.md").write_text("REVIEW_BODY_SENTINEL\n" * 1000)
            prompt = catalog.bootstrap(root, {"id": "test", "name": "test", "universe_id": "test"})
            self.assertLess(len(prompt.split()), 300)
            self.assertNotIn("REVIEW_BODY_SENTINEL", prompt)
            self.assertIn("REVIEW_BODY_SENTINEL", catalog.command(root, "review"))
            self.assertIn("review", [entry["name"] for entry in catalog.catalog(root)])


if __name__ == "__main__":
    unittest.main()
