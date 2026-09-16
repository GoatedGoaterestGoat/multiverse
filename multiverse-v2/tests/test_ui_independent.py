"""Independent lab acceptance checks; no provider or agent process is launched."""
from pathlib import Path
import sys
import json
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Static
from multiverse.dashboard import LocalBackend, group_key, agent_key
from multiverse.demo import DemoBackend
from multiverse.tui import MultiverseApp
from multiverse.universe import create


class IndependentPortalTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, pilot, app):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_enter_nested_scope_and_back_restores_selection_and_folds(self):
        backend = DemoBackend()
        app = MultiverseApp(backend.root, backend, refresh_interval=0)
        cad = backend.root / "subverses/cad-lab"
        verify = cad / "subverses/verification"
        review = backend.root / "subverses/pr-review"
        async with app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot, app)
            app.select_key(group_key(review))
            await pilot.press("left")
            app.select_key(group_key(cad))
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(app.root, cad, "LAB-UI001: Enter must open the selected local portal")
            self.assertEqual(app.snapshot["name"], "CAD Lab")
            self.assertEqual(app.snapshot["columns"], ["name", "live", "phase", "summary"])
            app.select_key(group_key(verify))
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(app.root, verify)
            await pilot.press("n", "ctrl+u", *"temporary")
            self.assertEqual(app.draft.scope, verify)
            await pilot.press("backspace")
            self.assertEqual(app.root, verify, "Editing a draft must not navigate back")
            await pilot.press("escape")
            self.assertIsNone(app.draft)
            self.assertEqual(len(backend.agents), 7)
            await pilot.press("backspace")
            await self.settle(pilot, app)
            self.assertEqual(app.root, cad)
            self.assertEqual(app.selected_key, group_key(verify))
            await pilot.press("backspace")
            await self.settle(pilot, app)
            self.assertEqual(app.root, backend.root)
            self.assertEqual(app.selected_key, group_key(cad))
            self.assertIn(str(review), app.collapsed)

    async def test_hover_keeps_selection_and_draft_cancel_survives_resize(self):
        backend = DemoBackend()
        app = MultiverseApp(backend.root, backend, refresh_interval=0, hover_delay=0.02)
        review = backend.root / "subverses/pr-review"
        async with app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot, app)
            original = app.selected_key
            reviewer = next(a for a in backend.agents if a["name"] == "reviewer")
            index = list(app.visible_rows).index(agent_key(reviewer))
            await pilot.hover("#agents", offset=(6, index + 1))
            await pilot.pause(0.08)
            await self.settle(pilot, app)
            self.assertEqual(app.selected_key, original)
            self.assertEqual(app.inspector.row["id"], reviewer["id"])
            self.assertIn("Context loaded", str(app.query_one("#preview", Static).render()))
            await pilot.hover("#masthead")
            await pilot.pause(0.25)
            app.select_key(group_key(review))
            await pilot.press("n", "ctrl+u", *"independent-draft", "tab", "tab", *"check scope")
            before = dict(app.draft.values)
            await pilot.resize_terminal(60, 20)
            app.refresh_snapshot()
            await self.settle(pilot, app)
            self.assertEqual(app.draft.values, before)
            self.assertEqual(app.draft.scope, review)
            await pilot.press("escape")
            self.assertIsNone(app.draft)
            self.assertEqual(len(backend.agents), 7)

    async def test_mouse_portal_local_artifact_and_search_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create(Path(temporary) / "root", "Independent root")
            child = create(root / "subverses/fixture", "Independent child")
            (child / "artifacts").mkdir()
            (child / "artifacts/proof.md").write_text("# Independent evidence\nfixture payload\n")
            (child / "views/default.json").write_text(json.dumps({
                "title": "Child evidence portal", "columns": ["name", "phase"],
                "widgets": [{"type": "artifacts", "title": "Evidence", "items": [
                    {"label": "Read proof", "path": "artifacts/proof.md"}]}]}))
            app = MultiverseApp(root, LocalBackend(), refresh_interval=0)
            async with app.run_test(size=(120, 32)) as pilot:
                await self.settle(pilot, app)
                await pilot.press("slash", *"Child evidence", "enter")
                index = list(app.visible_rows).index(group_key(child))
                self.assertTrue(await pilot.double_click("#agents", offset=(6, index + 1)))
                await self.settle(pilot, app)
                self.assertEqual(app.root, child)
                self.assertEqual(app.snapshot["name"], "Child evidence portal")
                self.assertEqual(app.snapshot["columns"], ["name", "phase"])
                self.assertTrue(await pilot.click("#artifact-0-0"))
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "ArtifactPreview")
                self.assertEqual(app.screen.query_one("#artifact-link").url,
                                 (child / "artifacts/proof.md").as_uri())
                await pilot.press("escape", "backspace")
                await self.settle(pilot, app)
                self.assertEqual(app.root, root)
                self.assertEqual(app.query_one("#filter").value, "Child evidence")
                self.assertEqual(app.selected_key, group_key(child))


if __name__ == "__main__":
    unittest.main()
