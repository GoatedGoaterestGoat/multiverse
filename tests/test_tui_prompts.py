"""The dashboard shows the prompt a waiting agent is stopped at, and answers it as the person."""
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.geometry import Region
from textual.widgets import Input, SelectionList, Static

from multiverse.ui.app import MultiverseApp
from multiverse.ui.prompt_screen import AnswerScreen
from multiverse.ui.tree import agent_key
from isolation import isolate
from tui_demo import DemoBackend

QUESTION = {
    "id": "demo-question", "kind": "question", "tool": "AskUserQuestion", "summary": "Which parts and how?",
    "detail": {}, "asked_at": None, "answerable": True, "reason": None, "subagent": None, "choices": [],
    "questions": [
        {"id": "Which parts?", "header": "Parts", "question": "Which parts?", "options": [["API", ""], ["UI", ""]],
         "multiple": True, "free_text": False, "secret": False, "required": True},
        {"id": "Token?", "header": "Token", "question": "Token?", "options": [], "multiple": False,
         "free_text": True, "secret": True, "required": True},
        {"id": "Why?", "header": "Why", "question": "Why?", "options": [["Other", ""]], "multiple": False,
         "free_text": False, "secret": False, "required": False, "takes_note": True},
        {"id": "Key?", "header": "Key", "question": "Key?", "options": [], "multiple": False, "free_text": False,
         "secret": True, "required": False, "takes_note": True}]}

FORM = dict(QUESTION, id="demo-form", kind="form", summary="Connect the database?",
            choices=[{"id": "decline", "label": "Decline", "saves_rule": False, "takes_message": False},
                     {"id": "cancel", "label": "Cancel", "saves_rule": False, "takes_message": False}])


class AnswerPromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate(self)
        self.backend = DemoBackend()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)
        self.writer = next(agent for agent in self.backend.agents if agent["name"] == "pr-writer")
        self.other = next(agent for agent in self.backend.agents if agent["name"] == "reviewer")

    async def settle(self, pilot):
        for _ in range(3):
            await pilot.pause()
            await self.app.workers.wait_for_complete()
        await pilot.pause()

    async def until(self, pilot, check, what, timeout=10.0):
        """Wait for `check` on the dashboard's own loop, bounded: a slow runner delays its timers, not their order."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not check():
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"timed out waiting for {what}")
            await pilot.pause(0.02)
            await self.app.workers.wait_for_complete()

    async def inspect(self, pilot, agent):
        """Select `agent` and wait until the inspector shows it: a selection reaches the inspector through a timer,
        which settle() does not wait for, and then its prompt read runs in a worker."""
        self.app.select_key(agent_key(agent))
        await self.until(pilot, lambda: bool(self.app.inspector.row) and self.app.inspector.row["id"] == agent["id"],
                         f"the inspector to show {agent['name']}")
        await self.settle(pilot)

    def line(self):
        widget = self.app.query_one("#inspect-prompt", Static)
        return str(widget.render()) if widget.display else ""

    def status(self):
        return str(self.app.query_one("#status", Static).render())

    async def open(self, pilot, agent):
        await self.inspect(pilot, agent)
        await pilot.press("a")
        await self.settle(pilot)

    async def test_the_inspector_shows_a_waiting_prompt_and_a_answers_it_as_picked_and_nothing_else(self):
        """A waiting agent's prompt shows in the inspector and a opens it; what is sent is exactly the choice, message
        or labels picked, nothing is sent on Esc or with a required question empty, a secret box is masked, and the
        outcome shows; fragile because a prompt takes one answer, so a default the person didn't pick (the first choice
        is Allow), a stray key or a lost label is sent and stands, and a secret typed in plain view is on the person's
        screen."""
        async with self.app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            await pilot.press("p")
            with self.subTest("the line shows only for a waiting agent"):
                await self.inspect(pilot, self.writer)
                await self.until(pilot, lambda: "Waiting on" in self.line(), "the waiting prompt's line")
                self.assertIn("⏸ Waiting on: permission · Bash: git push origin draft-pr · a answers it", self.line())
                await self.inspect(pilot, self.other)  # from a waiting agent to one that isn't: the line goes
                self.assertEqual(self.line(), "")
            with self.subTest("a on an agent that isn't waiting says so and opens nothing"):
                await self.open(pilot, self.other)
                self.assertNotIsInstance(self.app.screen, AnswerScreen)
                self.assertIn("isn't waiting", self.status())
            with self.subTest("Esc closes it without sending"):
                await self.open(pilot, self.writer)
                self.assertIsInstance(self.app.screen, AnswerScreen)
                await pilot.press("escape")
                await self.settle(pilot)
                self.assertEqual(self.backend.answers, [])
            with self.subTest("the highlighted choice alone is never sent"):
                await self.open(pilot, self.writer)
                await pilot.press("ctrl+enter")  # "Yes" is highlighted, not picked
                await self.settle(pilot)
                self.assertIsInstance(self.app.screen, AnswerScreen)
                self.assertEqual(self.backend.answers, [])
                self.assertIn("Pick a choice", str(self.app.screen.query_one("#answer-error", Static).render()))
            with self.subTest("Enter only picks: nothing is sent until Ctrl+Enter"):
                await pilot.press("enter")  # picks "Yes"
                await self.settle(pilot)
                self.assertIsInstance(self.app.screen, AnswerScreen)
                self.assertEqual(self.backend.answers, [])
                self.assertIn("Picked: Yes", str(self.app.screen.query_one("#prompt-picked", Static).render()))
            with self.subTest("a choice with its message, exactly as picked"):
                await pilot.press("down", "down", "enter")  # Yes, Yes always, No: the one that takes a message
                self.app.screen.query_one("#prompt-message", Input).value = "push to a fork instead"
                await pilot.press("ctrl+enter")
                await self.settle(pilot)
                self.assertEqual(self.backend.answers, [(self.writer["id"], "demo-permission", {
                    "choice": "deny", "message": "push to a fork instead", "answers": {}, "notes": {}})])
                self.assertIn("Answered.", self.status())
                await self.until(pilot, lambda: "Last answer" in self.line(), "the outcome in the inspector")
                self.assertIn("Last answer: Answered.", self.line())
                self.assertNotIn("Waiting on", self.line())  # its only prompt is answered: it works again
            with self.subTest("questions: labels picked, a masked secret, nothing sent while one is empty"):
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [QUESTION]
                self.backend.answers.clear()
                self.app.refresh_snapshot()
                await self.open(pilot, self.writer)
                screen = self.app.screen
                secret = screen.query_one("#q1-text", Input)
                self.assertTrue(secret.password)
                screen.query_one("#q0", SelectionList).select("UI")
                await pilot.press("ctrl+enter")  # the token is required and empty
                await self.settle(pilot)
                self.assertIs(self.app.screen, screen)
                self.assertEqual(self.backend.answers, [])
                self.assertIn("Answer Token", str(screen.query_one("#answer-error", Static).render()))
                screen.query_one("#q0", SelectionList).select("API")
                secret.value = "s3cret"
                await pilot.press("ctrl+enter")
                await self.settle(pilot)
                self.assertEqual(self.backend.answers[0][2]["answers"],
                                 {"Which parts?": ["UI", "API"], "Token?": ["s3cret"]})
                self.assertNotIn("s3cret", self.status() + self.line())
            with self.subTest("a note goes with its question, masked for a secret one"):
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [QUESTION]
                self.backend.answers.clear()
                self.app.refresh_snapshot()
                await self.open(pilot, self.writer)
                screen = self.app.screen
                self.assertEqual((screen.query_one("#q2-note", Input).password, screen.query_one("#q3-note", Input).password),
                                 (False, True))
                screen.query_one("#q0", SelectionList).select("API")
                screen.query_one("#q1-text", Input).value = "t0k"
                screen.query_one("#q2", SelectionList).select("Other")
                screen.query_one("#q2-note", Input).value = "only in the sandbox"
                screen.query_one("#q3-note", Input).value = "k3y"
                await pilot.press("ctrl+enter")
                await self.settle(pilot)
                sent = self.backend.answers[0][2]
                self.assertEqual((sent["answers"], sent["notes"]),
                                 ({"Which parts?": ["API"], "Token?": ["t0k"], "Why?": ["Other"]},
                                  {"Why?": "only in the sandbox", "Key?": "k3y"}))
                self.assertNotIn("k3y", self.status() + self.line())
            with self.subTest("a form: a picked choice goes alone, typed answers and notes left out"):
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [FORM]
                self.backend.answers.clear()
                self.app.refresh_snapshot()
                await self.open(pilot, self.writer)
                screen = self.app.screen
                screen.query_one("#q2-note", Input).value = "typed before declining"
                screen.query_one("#q3-note", Input).value = "k3y"
                screen.query_one("#prompt-choices").focus()
                await pilot.press("enter")  # Decline: picked, nothing sent yet, the required questions left empty
                await self.settle(pilot)
                self.assertEqual(self.backend.answers, [])
                await pilot.press("ctrl+enter")
                await self.settle(pilot)
                self.assertEqual([sent for _, _, sent in self.backend.answers],
                                 [{"choice": "decline", "message": None, "answers": {}, "notes": {}}])
            with self.subTest("a form with no choice picked is checked, then sent as its answers"):
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [FORM]
                self.backend.answers.clear()
                self.app.refresh_snapshot()
                await self.open(pilot, self.writer)
                screen = self.app.screen
                screen.query_one("#prompt-choices").focus()
                await pilot.press("enter", "enter")  # picked, then taken back
                screen.query_one("#q2-note", Input).value = "only in the sandbox"
                await pilot.press("ctrl+enter")  # Parts and Token are required and empty
                await self.settle(pilot)
                self.assertEqual(self.backend.answers, [])
                self.assertIs(self.app.screen, screen)
                screen.query_one("#q0", SelectionList).select("UI")
                screen.query_one("#q1-text", Input).value = "t0k"
                await pilot.press("ctrl+enter")
                await self.settle(pilot)
                self.assertEqual([sent for _, _, sent in self.backend.answers],
                                 [{"choice": None, "message": None, "answers": {"Which parts?": ["UI"], "Token?": ["t0k"]},
                                   "notes": {"Why?": "only in the sandbox"}}])
            with self.subTest("an answer that didn't take says why"):
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [dict(QUESTION, id="gone-already",
                                                                questions=QUESTION["questions"][:1])]  # one question
                self.app.refresh_snapshot()
                answer = self.backend.answer_prompt
                self.backend.answer_prompt = lambda root, reference, prompt_id, values: {
                    "id": prompt_id, "state": "person", "reason": "The person answered in the terminal first.",
                    "cause": "person", "saved_rule": False, "person_answer": {"answers": {"Which parts?": ["UI"]}}}
                try:
                    await self.open(pilot, self.writer)
                    self.app.screen.query_one("#q0", SelectionList).select("API")
                    await pilot.press("ctrl+enter")
                    await self.settle(pilot)
                finally:
                    self.backend.answer_prompt = answer
                self.assertIn("The person answered in the terminal first. They answered 'Which parts?' with UI.",
                              self.status())
                await self.until(pilot, lambda: "Last answer: The person" in self.line(), "the outcome in the inspector")
                self.assertIn("Last answer: The person answered", self.line())


    async def test_a_working_agents_open_questions_show_and_a_answers_them(self):
        """A gate kept, or a failed read taken as "none", hides a question the person can see in the terminal."""
        asked = dict(QUESTION, id="open-question", kind="question", tool="request_user_input_async",
                     summary="Which branch should the PR target?", requires_waiting=False,
                     questions=[QUESTION["questions"][0]])
        blocking = dict(asked, id="blocking-question", requires_waiting=True)  # a callback question: waiting-only
        permission = dict(self.backend.prompts[self.writer["id"]][0], id="later-permission")
        self.assertEqual(self.other["status"], "working")
        async with self.app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            await pilot.press("p")
            with self.subTest("a working agent with nothing open: no line, and a says so"):
                await self.inspect(pilot, self.other)
                self.assertEqual(self.line(), "")
                await pilot.press("a")
                await self.settle(pilot)
                self.assertNotIsInstance(self.app.screen, AnswerScreen)
                self.assertIn("isn't waiting on a prompt and has no open questions", self.status())
            with self.subTest("a permission or a question that requires waiting, while it works, stays hidden"):
                self.backend.prompts[self.other["id"]] = [permission, blocking]
                self.app.refresh_snapshot()
                await self.settle(pilot)
                self.assertEqual(self.line(), "")
            with self.subTest("an open question shows, and a answers it"):
                self.backend.prompts[self.other["id"]] = [permission, blocking, asked]
                self.app.refresh_snapshot()
                await self.until(pilot, lambda: "Open questions" in self.line(), "the open question in the inspector")
                self.assertIn("Open questions: ", self.line())
                self.assertNotIn("Waiting on", self.line())
                await pilot.press("a")
                await self.settle(pilot)
                self.assertIsInstance(self.app.screen, AnswerScreen)
                self.app.screen.query_one("#q0", SelectionList).select("UI")
                await pilot.press("ctrl+enter")
                await self.settle(pilot)
                self.assertEqual(self.backend.answers, [(self.other["id"], "open-question", {
                    "choice": None, "message": None, "answers": {"Which parts?": ["UI"]}, "notes": {}})])
                self.assertEqual(self.other["status"], "working")  # answering a question doesn't stop or start it
            with self.subTest("once answered it isn't offered again"):
                self.app.refresh_snapshot()
                await self.until(pilot, lambda: "Open questions" not in self.line(), "the answered question to go")
                await pilot.press("a")
                await self.settle(pilot)
                self.assertNotIsInstance(self.app.screen, AnswerScreen)
                self.assertIn("has no open questions", self.status())
                self.assertEqual(len(self.backend.answers), 1)
            real = self.backend.waiting_prompts
            with self.subTest("a failed read: the inspector stays quiet, and a says core's reason, not 'none'"):
                self.backend.waiting_prompts = lambda root, reference: {
                    "state": "unavailable", "reason": "The relay's question index can't be read", "prompts": []}
                try:
                    self.app.refresh_snapshot()
                    await self.settle(pilot)
                    self.assertNotIn("Open questions", self.line())
                    self.assertNotIn("index", self.line())
                    await pilot.press("a")
                    await self.settle(pilot)
                    self.assertIn("The relay's question index can't be read", self.status())
                    self.assertNotIn("no open questions", self.status())
                finally:
                    self.backend.waiting_prompts = real
            partial = {"state": "ok", "coverage": "unknown", "reason": "more than the relay keeps"}
            with self.subTest("a partial list shows what it has and says it may be partial"):
                self.backend.waiting_prompts = lambda root, reference: {**partial, "prompts": [asked]}
                try:
                    self.app.refresh_snapshot()
                    await self.until(pilot, lambda: "Open questions" in self.line(), "the partial listing")
                    self.assertIn("mv2 may not see every open question: more than the relay keeps", self.line())
                    widget = self.app.query_one("#inspect-prompt", Static)
                    await pilot.pause()
                    drawn = " ".join(strip.text for strip in widget.render_lines(
                        Region(0, 0, widget.size.width, widget.size.height)))
                    self.assertIn("relay keeps", " ".join(drawn.split()))  # on screen, not cut off below its rows
                finally:
                    self.backend.waiting_prompts = real
            with self.subTest("an empty partial list: a says none it can see, not none at all"):
                self.backend.waiting_prompts = lambda root, reference: {**partial, "prompts": []}
                try:
                    self.app.refresh_snapshot()
                    await self.settle(pilot)
                    self.assertNotIn("Open questions", self.line())
                    self.assertNotIn("may not see", self.line())  # an empty listing is background noise here
                    await pilot.press("a")
                    await self.settle(pilot)
                    self.assertIn("no open questions mv2 can see; it may not see every one: more than the relay keeps",
                                  self.status())
                finally:
                    self.backend.waiting_prompts = real


if __name__ == "__main__":
    unittest.main()
