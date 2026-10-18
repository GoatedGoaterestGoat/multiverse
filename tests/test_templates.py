"""Reusable templates install cleanly and keep role details lazy."""
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


class RoleTemplatesTest(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_install_has_fresh_identity_empty_state_and_declarative_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            for name in ("testing", "development"):
                with self.subTest(template=name):
                    root = templates.create(work / name, name, name, work)
                    write(root / "state" / "private-session.json", {"secret": "never clone"})
                    clone = templates.create(work / (name + "-clone"), name, str(root), work)
                    self.assertNotEqual(read(root / "universe.json")["id"], read(clone / "universe.json")["id"])
                    self.assertEqual(list((clone / "state" / "agents").iterdir()), [])
                    self.assertFalse((clone / "state" / "private-session.json").exists())
                    self.assertEqual(universe.workspace(clone), work)
                    self.assertEqual(read(clone / "harnesses/codex/harness.json"),
                                     read(templates.TEMPLATES / name / "harnesses/codex/harness.json"))
                    self.assertFalse(any(path.is_symlink() for path in clone.rglob("*")))

    def test_a_directory_without_universe_json_is_named_as_not_a_template(self):
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
            for name, action in (("testing", "retest"), ("development", "implement")):
                with self.subTest(template=name):
                    root = templates.create(work / name, name, name, work)
                    role = root / "workflows" / (name + "-role") / "prompt.md"
                    role.write_text("ROLE_BODY_SENTINEL\n" * 1000)
                    prompt = catalog.bootstrap(root, {"id": "test", "name": "test", "universe_id": "test"})
                    self.assertLess(len(prompt.split()), 300)
                    self.assertNotIn("ROLE_BODY_SENTINEL", prompt)
                    self.assertIn("ROLE_BODY_SENTINEL", catalog.command(root, name + "-role"))
                    self.assertTrue(catalog.command(root, action))
                    self.assertIn(name + "-role", [entry["name"] for entry in catalog.catalog(root)])

    def test_local_views_link_real_blank_forms_and_claim_no_results(self):
        views = []
        for name in ("testing", "development"):
            root = templates.TEMPLATES / name
            view = read(root / "views/default.json")
            views.append(view)
            self.assertEqual({item["type"] for item in view["widgets"]}, {"markdown", "table", "artifacts"})
            for widget in view["widgets"]:
                if widget["type"] == "table":
                    self.assertEqual(widget["rows"], [])
                if widget["type"] == "artifacts":
                    for artifact in widget["items"]:
                        path = (root / artifact["path"]).resolve()
                        self.assertTrue(path.is_relative_to(root.resolve()))
                        self.assertIn("no result recorded", path.read_text())
        self.assertNotEqual(views[0]["title"], views[1]["title"])
        self.assertNotEqual(views[0]["columns"], views[1]["columns"])


if __name__ == "__main__":
    unittest.main()
