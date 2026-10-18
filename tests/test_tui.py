"""Real Textual widgets and events, without a model, socket or terminal."""
import asyncio
from contextlib import ExitStack, contextmanager
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import DataTable, Input, OptionList, Static, TextArea
from textual import events
from multiverse.ui.tree import agent_key, group_key
from tui_demo import DemoBackend
from multiverse.ui.screens import Form
from multiverse.ui.app import MultiverseApp
from isolation import isolate


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

    def kill(self, root, reference):
        self.calls.append(('kill', root, reference))
        return super().kill(root, reference)

    def resume(self, root, reference):
        self.calls.append(("resume", root, reference))
        return super().resume(root, reference)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate(self)
        self.backend = RecordingBackend()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)
        self.review = self.backend.root / "subverses/pr-review"
        self.cad = self.backend.root / "subverses/cad-lab"
        self.verify = self.cad / "subverses/verification"

    async def settle(self, pilot):
        await pilot.pause()
        await self.app.workers.wait_for_complete()
        await pilot.pause()

    def buffered(self, text):
        # Unlike Pilot.press, send a whole terminal read without yielding between keys.
        for char in text:
            key = {"/": "slash", " ": "space", "\r": "enter"}.get(char, char)
            self.app._driver.send_message(events.Key(key, char))

    def status(self):
        return str(self.app.query_one("#status", Static).render())

    async def test_message_keeps_original_recipient_during_refresh(self):
        """A message goes to the agent selected when the form opened, even if a refresh reorders the rows before
        submit; a break goes unnoticed because the message lands in another agent's inbox and the form closes
        normally."""
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

    async def test_a_failing_backend_operation_never_ends_the_dashboard(self):
        """Send, inspect, spawn and attach raising, attach inside Textual's suspend, leave the error on show, the rows
        listed, a spawn draft kept and input working; fragile because Textual ends the app on an unhandled worker
        exception and suspend() skips its resume when the body raises, and these errors happen only in the field
        (LAB-UI003)."""
        resumed = []

        @contextmanager
        def suspend():
            # Match Textual: code after yield is skipped if the body raises.
            yield
            resumed.append(True)

        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            agent = self.app.selected_key
            with self.subTest(operation="send"), \
                    patch.object(self.backend, "send", side_effect=OSError("Inbox is read-only")):
                await pilot.press("m")
                self.app.screen.query_one("#field-text", TextArea).load_text("test")
                await pilot.press("ctrl+enter")
                await self.settle(pilot)
                self.assertIn("read-only", self.status())
                self.assertFalse(self.app.operating)
            with self.subTest(operation="inspect"), \
                    patch.object(self.backend, "inspect", side_effect=OSError("Pane unavailable")):
                await pilot.press("p")
                await self.settle(pilot)
                self.assertIn("Pane unavailable", str(self.app.query_one("#preview", Static).render()))
                self.assertFalse(self.app.operating)
                await pilot.press("p")
            with self.subTest(operation="spawn"), \
                    patch.object(self.backend, "spawn", side_effect=OSError("Harness unavailable")):
                self.app.select_key(group_key(self.review))
                await pilot.press("n", *"queen", "tab", "tab", *"Verify")
                before = dict(self.app.draft.values)
                await pilot.press("enter")
                await self.settle(pilot)
                self.assertEqual(self.app.draft.values, before)
                self.assertIn("Harness unavailable", self.status())
                self.assertFalse(self.app.operating)
                await pilot.press("escape")
                self.assertIsNone(self.app.draft)
            with self.subTest(operation="attach"), patch.object(self.app, "suspend", side_effect=suspend), \
                    patch.object(self.backend, "attach", side_effect=RuntimeError("no current client")):
                self.app.select_key(agent)
                await pilot.press("enter")
                await self.settle(pilot)
                self.assertEqual(resumed, [True])
                self.assertIn("no current client", self.status())
                self.assertFalse(self.app.operating)
            self.assertEqual(len(self.app.screen_stack), 1)
            self.assertEqual(len(self.app.visible_agents), 7)
            self.assertEqual(len(self.backend.agents), 7)
            self.assertEqual(self.backend.calls, [])
            await pilot.press("slash", *"gearbox")
            self.assertEqual([a["name"] for a in self.app.visible_agents.values()], ["verifier"])

    async def test_typed_keys_never_trigger_global_actions(self):
        """Text typed into the filter, a spawn draft or the message editor, in one terminal read or key by key, is never
        read as single-letter bindings (s stop, q quit, m message, n new agent), and a draft survives a refresh;
        fragile because a terminal delivers a burst in one read before focus moves, and a dropped / silently corrupts a
        message."""
        self.backend.scopes[self.review]["name"] = "Testing queue"
        async with self.app.run_test(size=(120, 32)) as pilot:
            await self.settle(pilot)
            search = self.app.query_one("#filter", Input)
            with ExitStack() as stack:
                actions = {name: stack.enter_context(patch.object(self.app, "action_" + name))
                           for name in ("stop", "kill", "spawn", "subverse", "message", "resume", "attach", "quit",
                                        "preview", "details")}
                # Each row: whether the inspector is open first, the terminal reads, then the query and focus.
                for inspector, reads, query, focused in (
                        (True, ["/Testing queue\r"], "Testing queue", "#agents"),  # the live repro's exact batch
                        (True, ["/Testing", " queue names\r"], "Testing queue names", "#agents"),  # after it closed
                        (False, ["/nmsqgpvr", "/q"], "nmsqgpvr/q", "#filter"),
                        # Reopening selects the old query, so new text replaces it.
                        (False, ["/Testing\r", "/ new message stop resume"], " new message stop resume", "#filter")):
                    with self.subTest(reads=reads):
                        await pilot.press("escape")
                        if inspector:
                            MultiverseApp.action_preview(self.app)
                            self.assertIsNotNone(self.app.inspector.row)
                        for read in reads:
                            self.buffered(read)
                            await self.settle(pilot)
                        self.assertEqual(search.value, query)
                        self.assertIs(self.app.focused, self.app.query_one(focused))
                        self.assertEqual(len(self.app.screen_stack), 1)
                        self.assertIsNone(self.app.draft)
                with self.subTest("paced keys reopening the search"):
                    await pilot.press("enter", "slash", *"paced names")
                    self.assertEqual(search.value, "paced names")
                    self.assertIs(self.app.focused, search)
                for name, action in actions.items():
                    with self.subTest(action=name):
                        action.assert_not_called()
            self.assertEqual(self.backend.calls, [])
            await pilot.press("escape")
            self.assertFalse(search.display)
            with self.subTest("slashes in the filter, a draft and the message editor"):
                self.app.select_key(agent_key(self.backend.agents[0]))
                await pilot.press("slash", "a", "slash", "b")
                self.assertEqual(search.value, "a/b")
                await pilot.press("escape", "n", "tab", "tab", "a", "slash", "b")
                self.assertEqual(self.app.draft.values["model"], "a/b")
                await pilot.press("escape", "m")
                await pilot.press("a", "slash", "b")
                self.assertEqual(self.app.screen.query_one("#field-text", TextArea).text, "a/b")
                await pilot.press("escape")
            with self.subTest("action letters typed into a spawn draft, then a refresh"):
                self.app.select_key(group_key(self.review))
                await pilot.press("n", *"queen", "tab", "tab", *"Verify", "shift+enter", *"q n m s", "tab", *"model-a")
                before = dict(self.app.draft.values)
                key = self.app.draft.key
                self.backend.agents.reverse()
                self.app.refresh_snapshot()
                await self.settle(pilot)
                self.assertEqual(self.app.draft.values, before)
                self.assertIn("q n m s", "".join(before.values()))
                self.assertEqual(self.app.selected_key, key)
                self.assertEqual(self.app.draft.scope, self.review)
                await pilot.press("escape")
                self.assertIsNone(self.app.draft)
            self.assertEqual(self.backend.calls, [])
            self.assertEqual(len(self.backend.agents), 7)

    async def test_stale_async_results_are_discarded(self):
        """A delayed pane capture of the previous agent, or a delayed scan of the parent after entering a child, is
        dropped; a break goes unnoticed because it shows another agent's terminal under the selected name, or the
        parent's rows under the child's title."""
        entered, release = Event(), Event()
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            original = dict(self.app.selected)
            reviewer = next(a for a in self.backend.agents if a["name"] == "reviewer")

            def capture(root, reference):
                if reference == original["id"]:
                    entered.set()
                    release.wait(3)
                    return "OLD PANE"
                return "NEW PANE"

            with self.subTest("a pane capture of the previous agent"), \
                    patch.object(self.backend, "inspect", side_effect=capture):
                await pilot.press("p")
                await asyncio.to_thread(entered.wait, 1)
                try:
                    self.app.select_key(agent_key(reviewer))
                    await pilot.pause(0.3)
                    self.assertEqual(self.app.inspector.row["id"], reviewer["id"])
                finally:
                    release.set()
                await self.settle(pilot)
                self.assertIn("NEW PANE", str(self.app.query_one("#preview", Static).render()))
                self.assertNotIn("OLD PANE", str(self.app.query_one("#preview", Static).render()))
                await pilot.press("p")
            entered.clear()
            release.clear()
            snapshot = self.backend.snapshot

            def slow(root):
                if root == self.backend.root:
                    entered.set()
                    release.wait(3)
                return snapshot(root)

            with self.subTest("a scan of the parent after entering a child"), \
                    patch.object(self.backend, "snapshot", side_effect=slow):
                self.app.refresh_snapshot()
                await asyncio.to_thread(entered.wait, 1)
                try:
                    self.app.select_key(group_key(self.cad))
                    await pilot.press("enter")
                finally:
                    release.set()
                await self.settle(pilot)
                self.assertEqual(self.app.snapshot["root"], self.cad)
                self.assertEqual(self.app.title, "CAD Lab")

    async def test_inline_harness_selector_keyboard_mouse_and_scoping(self):
        """The inline harness selector takes keyboard and mouse but no typed text, and Enter on it creates the agent in
        the selected group; kept because Enter there once only moved on to the model, and nothing shows that the press
        did not create the agent (b1b6a6e)."""
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
            await pilot.press("down")
            self.assertEqual(self.app.draft.values["harness"], "claude")
            await pilot.press("tab")
            self.assertEqual(self.app.draft.active, "model")
            self.assertFalse(menu.display)
            self.assertIn("Model", str(self.app.query_one("#selection", Static).render()))
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertTrue(await pilot.click("#harness-menu", offset=(3, 1)))
            await pilot.pause()
            self.assertEqual(self.app.draft.values["harness"], "codex")
            self.assertEqual(self.app.draft.active, "model")
            created = len(self.backend.agents) + 1
            await pilot.press("shift+tab")
            self.assertEqual(self.app.draft.active, "harness")
            await pilot.press("enter")
            await self.settle(pilot)
            self.assertIsNone(self.app.draft)
            self.assertEqual(len(self.backend.agents), created)
            self.assertEqual(self.backend.agents[-1]["harness"], "codex")
            self.assertEqual(self.backend.agents[-1]["universe"], str(self.review))

    async def test_enter_uses_cursor_before_highlight_message_is_dispatched(self):
        """Enter acts on the row under the cursor even before Textual has dispatched the highlight message; kept for
        the lab finding, since a fast key after a move otherwise opens or attaches the previously highlighted row and
        the user types into the wrong agent (LAB-UI003)."""
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


if __name__ == "__main__":
    unittest.main()
