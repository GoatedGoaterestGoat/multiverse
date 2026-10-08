"""Independent lab acceptance checks; no provider or agent process is launched."""
from pathlib import Path
import sys
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import DataTable, Static
from textual import events
from multiverse.ui.backend import LocalBackend
from multiverse.ui.tree import group_key, agent_key
from tui_demo import DemoBackend
from multiverse.ui.app import MultiverseApp
from multiverse.universe.templates import create


class IndependentPortalTests(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def watch_global_actions(self, app):
        with ExitStack() as stack:
            yield [stack.enter_context(patch.object(app, name, wraps=getattr(app, name)))
                   for name in ("action_stop", "action_spawn", "action_message", "action_resume",
                                "action_attach", "action_subverse", "action_quit")]

    def buffered_keys(self, app, text, enter=False):
        # Unlike Pilot.press, send an entire terminal read without per-key waits.
        for char in text:
            key = {"/": "slash", " ": "space"}.get(char, char)
            app._driver.send_message(events.Key(key, char))
        if enter:
            app._driver.send_message(events.Key("enter", "\r"))

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

    async def test_inspector_opens_only_with_p_and_draft_cancel_survives_resize(self):
        backend = DemoBackend()
        app = MultiverseApp(backend.root, backend, refresh_interval=0)
        review = backend.root / "subverses/pr-review"
        async with app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot, app)
            original = app.selected_key
            reviewer = next(a for a in backend.agents if a["name"] == "reviewer")
            index = list(app.visible_rows).index(agent_key(reviewer))
            # Hovering a row neither selects it nor opens the inspector.
            await pilot.hover("#agents", offset=(6, index + 1))
            await pilot.pause(0.4)
            await self.settle(pilot, app)
            self.assertEqual(app.selected_key, original)
            self.assertIsNone(app.inspector.row)
            app.select_key(agent_key(reviewer))
            await pilot.press("p")
            await self.settle(pilot, app)
            self.assertEqual(app.inspector.row["id"], reviewer["id"])
            self.assertIn("Context loaded", str(app.query_one("#preview", Static).render()))
            await pilot.press("p")
            self.assertIsNone(app.inspector.row)
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

    async def test_attach_failure_exits_suspend_normally_and_ui_remains_usable(self):
        backend = DemoBackend()
        backend.demo = False
        backend.attach = lambda root, reference: (_ for _ in ()).throw(OSError("independent attach failure"))
        app = MultiverseApp(backend.root, backend, refresh_interval=0)
        resumed = []

        @contextmanager
        def suspend_like_installed_textual():
            # Installed Textual resumes after yield, not in finally. The actual
            # terminal reproduction separately confirms why normal exit matters.
            yield
            resumed.append(True)

        async with app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot, app)
            with patch.object(app, "suspend", suspend_like_installed_textual):
                await pilot.press("enter")
                await self.settle(pilot, app)
            self.assertEqual(resumed, [True], "LAB-UI003: attach errors must not escape suspend")
            self.assertIn("independent attach failure", str(app.query_one("#status").render()))
            self.assertFalse(app.operating)
            await pilot.press("n", "escape")
            self.assertIsNone(app.draft)

    async def test_activation_reads_current_cursor_before_highlight_dispatch(self):
        backend = DemoBackend()
        app = MultiverseApp(backend.root, backend, refresh_interval=0)
        cad = backend.root / "subverses/cad-lab"
        async with app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot, app)
            # A rapid activation can occur before queued RowHighlighted updates
            # app.selected; the visible table cursor is authoritative.
            target = list(app.visible_rows).index(group_key(cad))
            app.query_one("#agents", DataTable).move_cursor(row=target, animate=False)
            app.action_open()
            await self.settle(pilot, app)
            self.assertEqual(app.root, cad, "LAB-UI003: activation must use current table cursor")

    async def test_buffered_search_never_dispatches_global_actions(self):
        backend = DemoBackend()
        app = MultiverseApp(backend.root, backend, refresh_interval=0)
        async with app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot, app)
            with patch.object(backend, "stop") as stop, patch.object(backend, "spawn") as spawn, \
                    patch.object(backend, "send") as send, patch.object(backend, "resume") as resume, \
                    self.watch_global_actions(app) as actions:
                self.buffered_keys(app, "/Testing queue", enter=True)
                await self.settle(pilot, app)
                self.assertEqual(app.query_one("#filter").value, "Testing queue")
                self.assertEqual(len(app.screen_stack), 1)
                self.assertIsNone(app.draft)
                self.assertIs(app.focused, app.query_one("#agents"))
                # Reopening search after submission must also accept buffered text.
                self.buffered_keys(app, "/ new message stop resume")
                await self.settle(pilot, app)
                # Input selects the existing query on refocus, so new text replaces it.
                self.assertEqual(app.query_one("#filter").value, " new message stop resume")
                self.assertIs(app.focused, app.query_one("#filter"))
                self.assertEqual(len(app.screen_stack), 1)
                self.assertIsNone(app.draft)
                for action in (stop, spawn, send, resume):
                    action.assert_not_called()
                for action in actions:
                    action.assert_not_called()
                # The ordinary paced editor has the same select-on-refocus behavior.
                await pilot.press("enter", "slash", *"paced names")
                self.assertEqual(app.query_one("#filter").value, "paced names")
                self.assertIs(app.focused, app.query_one("#filter"))
                self.assertEqual(len(app.screen_stack), 1)
                for action in actions:
                    action.assert_not_called()

    async def test_search_retains_focus_after_hiding_inspector_and_more_typing(self):
        backend = DemoBackend()
        app = MultiverseApp(backend.root, backend, refresh_interval=0)
        async with app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot, app)
            await pilot.press("p")
            await self.settle(pilot, app)
            self.assertIsNotNone(app.inspector.row)
            with patch.object(backend, "stop") as stop, patch.object(backend, "spawn") as spawn, \
                    self.watch_global_actions(app) as actions:
                self.buffered_keys(app, "/Testing")
                await self.settle(pilot, app)
                self.assertEqual(app.query_one("#filter").value, "Testing")
                self.assertIs(app.focused, app.query_one("#filter"))
                self.buffered_keys(app, " queue names", enter=True)
                await self.settle(pilot, app)
                self.assertEqual(app.query_one("#filter").value, "Testing queue names")
                self.assertEqual(len(app.screen_stack), 1)
                self.assertIsNone(app.draft)
                stop.assert_not_called()
                spawn.assert_not_called()
                for action in actions:
                    action.assert_not_called()
                await pilot.press("escape", "slash", *"CAD", "enter")
                self.assertEqual(app.query_one("#filter").value, "CAD")
                self.assertIn(group_key(backend.root / "subverses/cad-lab"), app.visible_rows)


if __name__ == "__main__":
    unittest.main()
