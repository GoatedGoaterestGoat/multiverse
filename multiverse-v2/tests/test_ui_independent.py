"""Independent lab acceptance checks; no provider or agent process is launched."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Static
from multiverse.dashboard import group_key, agent_key
from multiverse.demo import DemoBackend
from multiverse.tui import MultiverseApp


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


if __name__ == "__main__":
    unittest.main()
