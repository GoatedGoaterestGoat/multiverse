"""Real Textual widgets and events, without a model, socket or terminal."""
import asyncio
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Static, TextArea
from textual import events
from multiverse.dashboard import agent_key, group_key
from tui_demo import DemoBackend
from multiverse.draft import SubverseDraft
from multiverse.tui import Details, Form, MultiverseApp


class RecordingBackend(DemoBackend):
    demo = False

    def __init__(self):
        super().__init__()
        self.calls = []

    def attach(self, root, reference):
        self.calls.append(("attach", root, reference))

    def stop(self, root, reference):
        self.calls.append(("stop", root, reference))
        return super().stop(root, reference)

    def resume(self, root, reference):
        self.calls.append(("resume", root, reference))
        return super().resume(root, reference)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0, hover_delay=0.03)
        self.review = self.backend.root / "subverses/pr-review"
        self.cad = self.backend.root / "subverses/cad-lab"
        self.verify = self.cad / "subverses/verification"

    async def settle(self, pilot):
        await pilot.pause()
        await self.app.workers.wait_for_complete()
        await pilot.pause()

    async def click_row(self, pilot, key, double=False):
        index = list(self.app.visible_rows).index(key)
        # Actual table hit testing: one header line, then one line per row.
        method = pilot.double_click if double else pilot.click
        self.assertTrue(await method("#agents", offset=(5, index + 1)))
        await pilot.pause()

    async def test_all_groups_are_inline_colored_and_keyboard_foldable(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            table = self.app.query_one("#agents", DataTable)
            self.assertEqual(table.row_count, 11)  # four groups, seven agents
            self.assertEqual(len(self.app.visible_agents), 7)
            self.assertEqual(len(self.app.query(Button)), 0)
            self.app.select_key(group_key(self.cad))
            self.assertIn("#89b4fa", str(table.get_cell(group_key(self.cad), "name").style))
            await pilot.press("left")
            self.assertEqual(table.row_count, 8)
            self.assertNotIn(group_key(self.verify), self.app.visible_rows)
            await pilot.press("right", "right")
            self.assertEqual(self.app.selected["name"], "harness-builder")
            await pilot.press("j", "right")
            self.assertEqual(self.app.selected["name"], "verifier")
            await pilot.press("h")
            self.assertEqual(self.app.selected_key, group_key(self.verify))
            await pilot.press("space")
            self.assertFalse(self.app.visible_rows[group_key(self.verify)]["expanded"])
            self.assertEqual(self.app.root, self.backend.root)

    async def test_mouse_folds_group_selects_agent_and_double_click_attaches(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            await self.click_row(pilot, group_key(self.review))
            self.assertEqual(len(self.app.visible_agents), 5)
            await self.click_row(pilot, group_key(self.review))
            self.assertEqual(len(self.app.visible_agents), 7)
            reviewer = next(a for a in self.backend.agents if a["name"] == "reviewer")
            await self.click_row(pilot, agent_key(reviewer))
            self.assertEqual(self.app.selected["id"], reviewer["id"])
            self.assertEqual(self.backend.calls, [])
            with patch.object(self.app, "suspend", return_value=nullcontext()):
                await self.click_row(pilot, agent_key(reviewer), double=True)
                await self.settle(pilot)
            self.assertEqual(self.backend.calls, [("attach", self.review, reviewer["id"])])

    async def test_search_reveals_folded_descendants_and_preserves_folds(self):
        async with self.app.run_test(size=(100, 28)) as pilot:
            await self.settle(pilot)
            self.app.select_key(group_key(self.cad))
            await pilot.press("left", "slash", *"gearbox")
            self.assertEqual([a["name"] for a in self.app.visible_agents.values()], ["verifier"])
            self.assertIn(group_key(self.cad), self.app.visible_rows)
            self.assertIn(group_key(self.verify), self.app.visible_rows)
            await pilot.press("enter")
            self.app.select_key(next(iter(self.app.visible_agents)))
            selected = self.app.selected_key
            self.backend.agents.reverse()
            self.app.refresh_snapshot()
            await self.settle(pilot)
            self.assertEqual(self.app.selected_key, selected)
            await pilot.press("escape")
            self.assertFalse(self.app.query_one("#filter").display)
            self.assertIn(str(self.cad), self.app.collapsed)
            self.assertNotIn(group_key(self.verify), self.app.visible_rows)
            await pilot.press("slash", *"no-such-agent")
            self.assertTrue(self.app.query_one("#empty").display)
            await pilot.press("escape")
            self.assertEqual(len(self.app.snapshot["agents"]), 7)

    async def test_spawn_in_selected_subverse_validation_typing_and_cancel(self):
        async with self.app.run_test(size=(110, 35)) as pilot:
            await self.settle(pilot)
            self.app.select_key(group_key(self.review))
            original = len(self.backend.agents)
            await pilot.press("n", "ctrl+u", "enter")
            self.assertEqual(len(self.app.screen_stack), 1)
            self.assertIn("required", str(self.app.query_one("#status", Static).render()))
            await pilot.press(*"queen")
            self.assertEqual(self.app.draft.values["name"], "queen")
            await pilot.press("tab", "tab")
            self.assertEqual(self.app.draft.active, "model")
            self.app.query_one("#agents").post_message(events.Paste("opus"))
            await pilot.pause()
            await pilot.press("tab")
            self.assertEqual(self.app.draft.active, "name")
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            self.assertEqual(len(self.backend.agents), original + 1)
            self.assertEqual(self.backend.agents[-1]["universe"], str(self.review))
            self.assertEqual(self.backend.agents[-1]["model"], "opus")
            self.assertEqual(self.backend.agents[-1]["task"], "")
            self.assertIsNone(self.app.draft)
            await pilot.press("n", "escape")
            self.assertEqual(len(self.backend.agents), original + 1)

    async def test_message_keeps_original_recipient_during_refresh(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            original = self.app.selected["id"]
            await pilot.press("m")
            self.app.screen.query_one("#field-text", TextArea).load_text("What did you verify?\nInclude evidence.")
            self.backend.agents.reverse()
            self.app.refresh_snapshot()
            await self.settle(pilot)
            self.assertIsInstance(self.app.screen, Form)
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            self.assertEqual(len(self.backend.messages), 1)
            self.assertEqual(self.backend.messages[0]["to"], original)
            self.assertIn("\n", self.backend.messages[0]["text"])

    async def test_empty_initial_prompt_can_spawn_and_failed_delivery_is_visible(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            original = len(self.backend.agents)
            await pilot.press('n', 'ctrl+enter')
            await self.settle(pilot)
            self.assertEqual(len(self.backend.agents), original + 1)
            self.assertEqual(self.backend.agents[-1]['task'], '')
            failed = {'saved': True, 'state': 'unavailable', 'ok': False,
                      'native_delivery': {'state': 'unavailable', 'reason': 'Native bridge is unavailable'}}
            with patch.object(self.backend, 'send', return_value=failed):
                await pilot.press('m')
                self.app.screen.query_one('#field-text', TextArea).load_text('Keep this saved')
                await pilot.press('ctrl+enter')
                await self.settle(pilot)
            status = str(self.app.query_one('#status', Static).render())
            self.assertIn('Message saved', status)
            self.assertIn('Native bridge is unavailable', status)

    async def test_inline_harness_selector_keyboard_mouse_and_scoping(self):
        self.backend.scopes[self.review]["harnesses"] = ["codex", "claude"]
        async with self.app.run_test(size=(110, 28)) as pilot:
            await self.settle(pilot)
            self.app.select_key(group_key(self.review))
            await pilot.press("n", "tab")
            await pilot.pause()
            menu = self.app.query_one("#harness-menu", OptionList)
            self.assertTrue(menu.display)
            self.assertEqual(menu.option_count, 2)
            self.assertEqual(self.app.draft.values["harness"], "codex")
            await pilot.press(*"garbage", "backspace")
            self.app.query_one("#agents").post_message(events.Paste("pi"))
            await pilot.pause()
            self.assertEqual(self.app.draft.values["harness"], "codex")
            await pilot.press("down", "enter")
            self.assertEqual(self.app.draft.values["harness"], "claude")
            self.assertEqual(self.app.draft.active, "model")
            self.assertFalse(menu.display)
            self.assertIn("Model", str(self.app.query_one("#selection", Static).render()))
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertTrue(await pilot.click("#harness-menu", offset=(3, 1)))
            await pilot.pause()
            self.assertEqual(self.app.draft.values["harness"], "codex")
            self.assertEqual(self.app.draft.active, "model")
            await pilot.press("enter")
            await self.settle(pilot)
            self.assertEqual(self.backend.agents[-1]["harness"], "codex")
            self.assertEqual(self.backend.agents[-1]["universe"], str(self.review))

    async def test_removed_harness_requires_a_new_selection(self):
        async with self.app.run_test(size=(60, 20)) as pilot:
            await self.settle(pilot)
            self.app.select_key(group_key(self.review))
            await pilot.press("n", "tab", "tab", *"Review")
            self.backend.scopes[self.review]["harnesses"] = ["claude"]
            self.app.refresh_snapshot()
            await self.settle(pilot)
            await pilot.press("enter")
            self.assertIsNotNone(self.app.draft)
            self.assertEqual(self.app.draft.active, "harness")
            menu = self.app.query_one("#harness-menu", OptionList)
            self.assertTrue(menu.display)
            self.assertLessEqual(menu.region.right, 60)
            await pilot.press("down", "enter", "enter")
            await self.settle(pilot)
            self.assertEqual(self.backend.agents[-1]["harness"], "claude")

    async def test_stop_confirmation_resume_and_keyboard_attach(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            reference = self.app.selected["id"]
            await pilot.press("s", "escape")
            self.assertEqual(self.backend.calls, [])
            await pilot.press("s", "ctrl+enter")
            await self.settle(pilot)
            self.assertEqual(self.app.selected["live"], "stopped")
            await pilot.press("enter")
            self.assertEqual(self.backend.calls, [("stop", self.backend.root, reference)])
            await pilot.press("r")
            await self.settle(pilot)
            with patch.object(self.app, "suspend", return_value=nullcontext()):
                await pilot.press("enter")
                await self.settle(pilot)
            self.assertEqual([call[0] for call in self.backend.calls], ["stop", "resume", "attach"])

    async def test_create_nested_subverse_and_quit_do_not_stop_agents(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            footer = {active.binding.action for active in self.app.active_bindings.values() if active.binding.show}
            self.assertIn("subverse", footer)
            self.app.select_key(group_key(self.verify))
            await pilot.press("g")
            self.assertEqual(len(self.app.screen_stack), 1, "Subverse creation is inline, not a form")
            self.assertIsInstance(self.app.draft, SubverseDraft)
            self.assertEqual(self.app.draft.values["name"], "subverse")
            self.assertIn(self.app.draft.key, self.app.visible_rows)
            await pilot.press("tab")
            self.assertEqual(self.app.draft.active, "name")
            await pilot.press("escape")
            self.assertIsNone(self.app.draft)
            self.assertEqual(len(self.backend.scopes), 4)
            # The backend validates names; a refusal keeps the draft for correction.
            await pilot.press("g", "ctrl+u", "enter")
            await self.settle(pilot)
            self.assertIn("letters", str(self.app.query_one("#status", Static).render()))
            self.assertIsNotNone(self.app.draft)
            await pilot.press(*"bad name", "enter")
            await self.settle(pilot)
            self.assertIsNotNone(self.app.draft)
            await pilot.press("ctrl+u")
            self.app.query_one("#agents").post_message(events.Paste("experiments\nsecond line"))
            await pilot.pause()
            self.assertEqual(self.app.draft.values["name"], "experiments")
            await pilot.press("enter")
            await self.settle(pilot)
            target = self.verify / "subverses/experiments"
            self.assertIsNone(self.app.draft)
            self.assertEqual(self.app.selected_key, group_key(target))
            self.app.select_key(group_key(self.verify))
            await pilot.press("g", "ctrl+u", *"experiments", "enter")
            await self.settle(pilot)
            self.assertIn("already exists", str(self.app.query_one("#status", Static).render()))
            self.assertIsNotNone(self.app.draft)
            await pilot.press("escape")
            self.assertEqual(self.app.root, self.backend.root)
            self.assertEqual(len(self.app.visible_agents), 7)
            await pilot.press("q")
        self.assertEqual(self.backend.calls, [])

    async def test_resize_dense_layout_forms_and_on_demand_preview(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            with patch.object(self.backend, "inspect", wraps=self.backend.inspect) as inspect:
                self.app.refresh_snapshot()
                await self.settle(pilot)
                inspect.assert_not_called()
                await pilot.press("p")
                await self.settle(pilot)
                self.assertIn("Context loaded", str(self.app.query_one("#preview", Static).render()))
                await pilot.press("p")
            for width, height in ((100, 28), (80, 24), (60, 20)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                table = self.app.query_one("#agents", DataTable)
                self.assertGreaterEqual(table.region.height, height - 4)
                self.assertLessEqual(table.region.right, width)
                self.assertFalse(table.show_horizontal_scrollbar)
                self.assertEqual(table.row_count, 11)
                self.assertEqual(len(self.app.query(Button)), 0)
            await pilot.press("n")
            self.assertEqual(len(self.app.screen.query(Button)), 0)
            for selector in ("#selection", "#status"):
                region = self.app.query_one(selector).region
                self.assertLessEqual(region.bottom, 20)
                self.assertGreater(region.height, 0)
            self.assertEqual(len(self.app.screen_stack), 1)
            await pilot.press("tab", "tab", *"sonnet")
            self.assertEqual(self.app.draft.values["model"], "sonnet")
            await pilot.press("escape", "question_mark")
            self.assertIsInstance(self.app.screen, Details)
            await pilot.press("escape")

    async def test_hover_inspects_without_retargeting_and_can_be_read_with_mouse(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            original = self.app.selected_key
            reviewer = next(a for a in self.backend.agents if a["name"] == "reviewer")
            index = list(self.app.visible_rows).index(agent_key(reviewer))
            await pilot.hover("#agents", offset=(6, index + 1))
            await pilot.pause(0.08)
            await self.settle(pilot)
            self.assertEqual(self.app.inspector.row["id"], reviewer["id"])
            self.assertEqual(self.app.selected_key, original)
            self.assertIn("Context loaded", str(self.app.query_one("#preview", Static).render()))
            self.assertGreaterEqual(self.app.query_one("#inspector").region.x, self.app.query_one("#agents").region.right)
            await pilot.hover("#inspect-work")
            await pilot.pause(0.22)
            self.assertIsNotNone(self.app.inspector.row)
            await pilot.hover("#masthead")
            await pilot.pause(0.22)
            self.assertIsNone(self.app.inspector.row)
            self.assertEqual(self.backend.calls, [])

    async def test_hover_delay_keyboard_inspection_and_compact_drawer(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            self.app.hover_delay = 0.25
            with patch.object(self.backend, "inspect", wraps=self.backend.inspect) as inspect:
                await pilot.hover("#agents", offset=(6, 3))
                await pilot.hover("#masthead")
                await pilot.pause(0.3)
                inspect.assert_not_called()
            await pilot.press("j")
            await pilot.pause(0.16)
            await self.settle(pilot)
            self.assertEqual(self.app.inspector.row["key"], self.app.selected_key)
            for width, height in ((80, 24), (60, 20)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                inspector = self.app.query_one("#inspector").region
                table = self.app.query_one("#agents", DataTable)
                self.assertGreaterEqual(inspector.y, table.region.bottom)
                self.assertLessEqual(inspector.bottom, height - 2)
                self.assertGreaterEqual(table.region.height, 6)
                self.assertFalse(table.show_horizontal_scrollbar)
                self.assertGreaterEqual(self.app.query_one("#pane-scroll").region.height, 2)
            await pilot.press("escape")
            self.assertIsNone(self.app.inspector.row)

    async def test_preview_refresh_discards_delayed_result_and_does_not_read_stopped_agent(self):
        entered, release = Event(), Event()
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            original = dict(self.app.selected)
            reviewer = next(a for a in self.backend.agents if a["name"] == "reviewer")
            calls = []

            def capture(root, reference):
                calls.append(reference)
                if reference == original["id"]:
                    entered.set()
                    release.wait(3)
                    return "OLD PANE"
                return "NEW PANE " + str(len(calls))

            with patch.object(self.backend, "inspect", side_effect=capture):
                await pilot.press("p")
                await asyncio.to_thread(entered.wait, 1)
                try:
                    index = list(self.app.visible_rows).index(agent_key(reviewer))
                    await pilot.hover("#agents", offset=(6, index + 1))
                    await pilot.pause(0.08)
                    self.assertEqual(self.app.inspector.row["id"], reviewer["id"])
                finally:
                    release.set()
                await self.settle(pilot)
                self.assertIn("NEW PANE", str(self.app.query_one("#preview", Static).render()))
                self.assertNotIn("OLD PANE", str(self.app.query_one("#preview", Static).render()))
                before = len(calls)
                self.app.refresh_snapshot()
                await self.settle(pilot)
                self.assertEqual(len(calls), before + 1)
                reviewer["live"], reviewer["activity"] = "stopped", "—"
                self.app.refresh_snapshot()
                await self.settle(pilot)
                self.assertEqual(len(calls), before + 1)
                self.assertIn("No live pane", str(self.app.query_one("#preview", Static).render()))

    async def test_group_inspector_does_not_capture_and_preview_errors_stay_local(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            with patch.object(self.backend, "inspect", side_effect=OSError("Pane unavailable")) as inspect:
                index = list(self.app.visible_rows).index(group_key(self.review))
                await pilot.hover("#agents", offset=(6, index + 1))
                await pilot.pause(0.08)
                self.assertEqual(self.app.inspector.row["kind"], "group")
                self.assertIn("2/2 live", str(self.app.query_one("#inspect-state", Static).render()))
                inspect.assert_not_called()
                await pilot.hover("#agents", offset=(6, index + 2))
                await pilot.pause(0.08)
                await self.settle(pilot)
                self.assertIn("Pane unavailable", str(self.app.query_one("#preview", Static).render()))
                self.assertFalse(self.app.operating)

    async def test_inline_draft_survives_refresh_failure_and_literal_action_keys(self):
        async with self.app.run_test(size=(100, 28)) as pilot:
            await self.settle(pilot)
            self.app.select_key(group_key(self.review))
            await pilot.press("n", *"queen", "tab", "tab", *"Verify", "shift+enter", *"q n m s", "tab", *"model-a")
            before = dict(self.app.draft.values)
            key = self.app.draft.key
            self.backend.agents.reverse()
            self.app.refresh_snapshot()
            await self.settle(pilot)
            self.assertEqual(self.app.draft.values, before)
            self.assertEqual(self.app.selected_key, key)
            self.assertEqual(self.app.draft.scope, self.review)
            with patch.object(self.backend, "spawn", side_effect=OSError("Harness unavailable")):
                await pilot.press("enter")
                await self.settle(pilot)
            self.assertEqual(self.app.draft.values, before)
            self.assertIn("Harness unavailable", str(self.app.query_one("#status", Static).render()))
            self.assertEqual(self.backend.calls, [])
            self.assertEqual(len(self.backend.agents), 7)
            await pilot.press("escape")
            self.assertIsNone(self.app.draft)

    async def test_slow_refresh_leaves_input_responsive_and_preserves_current_filter(self):
        entered, release = Event(), Event()
        original = self.backend.snapshot
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)

            def slow(root):
                entered.set()
                release.wait(4)
                return original(root)

            with patch.object(self.backend, "snapshot", side_effect=slow):
                self.app.refresh_snapshot()
                await asyncio.to_thread(entered.wait, 1)
                try:
                    await pilot.press("slash", *"blocked")
                    self.assertIs(self.app.focused, self.app.query_one("#filter"))
                    self.assertEqual([a["name"] for a in self.app.visible_agents.values()], ["pr-writer"])
                finally:
                    release.set()
                await self.settle(pilot)
            self.assertEqual([a["name"] for a in self.app.visible_agents.values()], ["pr-writer"])

    async def test_backend_failure_is_visible_and_preserves_dashboard(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            with patch.object(self.backend, "send", side_effect=OSError("Inbox is read-only")):
                await pilot.press("m")
                self.app.screen.query_one("#field-text", TextArea).load_text("test")
                await pilot.press("ctrl+enter")
                await self.settle(pilot)
            self.assertIn("read-only", str(self.app.query_one("#status", Static).render()))
            self.assertFalse(self.app.operating)
            self.assertEqual(len(self.app.visible_agents), 7)


    async def test_enter_uses_cursor_before_highlight_message_is_dispatched(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            old_agent = self.app.selected
            table = self.app.query_one("#agents", DataTable)
            index = list(self.app.visible_rows).index(group_key(self.cad))
            # Move and activate in one event-loop turn, as with batched keys.
            table.move_cursor(row=index, animate=False)
            self.assertIs(self.app.selected, old_agent)
            self.app.action_open()
            await self.settle(pilot)
            self.assertEqual(self.app.root, self.cad)
            self.assertEqual(self.backend.calls, [])

    async def test_attach_failure_exits_suspend_normally_and_restores_input(self):
        resumed = []

        @contextmanager
        def suspend():
            # Match Textual: code after yield is skipped if the body raises.
            yield
            resumed.append(True)

        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            with patch.object(self.app, "suspend", side_effect=suspend), \
                    patch.object(self.backend, "attach", side_effect=RuntimeError("no current client")):
                await pilot.press("enter")
                await self.settle(pilot)
            self.assertEqual(resumed, [True])
            self.assertFalse(self.app.operating)
            self.assertIn("no current client", str(self.app.query_one("#status", Static).render()))
            await pilot.press("slash", *"gearbox")
            self.assertEqual([a["name"] for a in self.app.visible_agents.values()], ["verifier"])

    async def test_buffered_search_routes_query_to_input_without_global_actions(self):
        self.backend.scopes[self.review]["name"] = "Testing queue"
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            await pilot.press("p")
            self.assertIsNotNone(self.app.inspector.row)
            with ExitStack() as stack:
                actions = {name: stack.enter_context(patch.object(self.app, "action_" + name))
                           for name in ("stop", "spawn", "subverse", "message", "resume", "quit", "preview", "details")}
                # Unlike Pilot.press, enqueue the whole terminal read without
                # yielding between keys. The live repro sends this exact batch.
                for key, character in [("slash", "/"), *[(c, c) for c in "Testing queue"], ("enter", None)]:
                    self.app.post_message(events.Key(key, character))
                await self.settle(pilot)
                for action in actions.values():
                    action.assert_not_called()
            self.assertEqual(self.app.query_one("#filter", Input).value, "Testing queue")
            self.assertIs(self.app.focused, self.app.query_one("#agents"))
            self.assertEqual(len(self.app.screen_stack), 1)
            self.assertIsNone(self.app.draft)
            self.assertEqual(self.backend.calls, [])
            await pilot.press("escape")
            self.assertFalse(self.app.query_one("#filter").display)

    async def test_buffered_search_keeps_focus_for_subsequent_action_letters(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            with ExitStack() as stack:
                actions = [stack.enter_context(patch.object(self.app, "action_" + name))
                           for name in ("stop", "spawn", "subverse", "message", "resume", "quit", "preview", "details")]
                for key, character in [("slash", "/"), *[(c, c) for c in "nmsqgpvr"]]:
                    self.app.post_message(events.Key(key, character))
                await self.settle(pilot)
                self.assertIs(self.app.focused, self.app.query_one("#filter"))
                for key, character in [("slash", "/"), ("q", "q")]:
                    self.app.post_message(events.Key(key, character))
                await self.settle(pilot)
                self.assertEqual(self.app.query_one("#filter", Input).value, "nmsqgpvr/q")
                for action in actions:
                    action.assert_not_called()

    async def test_search_shortcut_preserves_slashes_in_editors_and_drafts(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            await pilot.press("slash", "a", "slash", "b")
            self.assertEqual(self.app.query_one("#filter", Input).value, "a/b")
            await pilot.press("escape", "n", "tab", "tab", "a", "slash", "b")
            self.assertEqual(self.app.draft.values["model"], "a/b")
            await pilot.press("escape", "m")
            await pilot.press("a", "slash", "b")
            self.assertEqual(self.app.screen.query_one("#field-text", TextArea).text, "a/b")
            await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
