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
    "detail": {"origin": "the release checklist"}, "asked_at": None, "answerable": True, "reason": None,
    "subagent": None, "choices": [],
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
        return str(self.app.query_one("#status", Static).content)  # an answer's outcome fits itself when drawn

    async def open(self, pilot, agent):
        await self.inspect(pilot, agent)
        await pilot.press("a")
        await self.settle(pilot)

    async def test_the_answer_screen_shows_what_the_highlight_is_and_keeps_it_in_view(self):
        """The highlighted choice's full label shows whatever is picked, a full-length display title is shown whole,
        and in a 60x20 terminal the highlighted choice and its scope stay in view; fragile because two choices can
        share a short label, and a choice scrolled out of view could be picked and sent unseen (a saved rule)."""
        def choice(n, meaning, short, label, saves=False):
            return {"id": f"choice-{n}", "label": label, "saves_rule": saves, "takes_message": False,
                    "meaning": meaning, "short": short}
        title = ("Review the generated command and its context before it runs here " + "x" * 80)[:71] + "TITLE-END"
        prompt = {"id": "long", "kind": "permission", "tool": "Bash", "summary": "make release", "detail": {},
                  "asked_at": None, "answerable": True, "reason": None, "subagent": None, "questions": [],
                  "display": {"title": title, "subject": "make release " + "VERSION=1.2.3 " * 34,  # 500 characters
                              "lines": [f"line {n}" for n in range(1, 9)]},
                  "choices": [choice(1, "allow", "Allow once", "Yes"),
                              choice(2, "allow_session", "Allow for session", "Yes, for make in this session"),
                              choice(3, "allow_session", "Allow for session", "Yes, for make release in this session"),
                              choice(4, "allow_always", "Allow always", "Yes, and always allow make in this project",
                                     saves=True),
                              choice(5, "deny", "Deny", "No")]}
        self.writer["status"] = "waiting"
        self.backend.prompts[self.writer["id"]] = [prompt]
        for size in ((120, 36), (60, 20)):
            self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)
            async with self.app.run_test(size=size) as pilot:
                await self.settle(pilot)
                await pilot.press("p")
                await self.open(pilot, self.writer)
                screen = self.app.screen
                self.assertIsInstance(screen, AnswerScreen)

                def frame():
                    return "".join(strip.text for strip in screen._compositor.render_strips()).replace(" ", "")

                long = screen.query_one("#prompt-choice-long", Static)
                if size == (120, 36):
                    with self.subTest("the whole display title"):
                        self.assertIn("TITLE-END", frame())
                    with self.subTest("the highlighted choice's full label, whatever is picked"):
                        await pilot.press("down", "enter", "down")  # pick the first session choice, then look on
                        await self.settle(pilot)
                        self.assertEqual(screen.picked, "choice-2")
                        self.assertEqual(str(long.render()), "Yes, for make release in this session")
                else:
                    with self.subTest("at 60x20 the highlighted choice and its scope stay in view"):
                        await pilot.press("down", "down", "down")  # Allow always, the fourth: it saves a rule
                        await self.settle(pilot)
                        shown = frame()
                        for text in ("Allowalways", "savesarule", "alwaysallowmakeinthisproject"):
                            self.assertIn(text, shown)
                await pilot.press("escape")
                await self.settle(pilot)
        self.assertEqual(self.backend.answers, [])

    async def test_a_long_full_label_never_hides_the_highlighted_choice_in_a_short_terminal(self):
        """At 60x20, a highlighted choice whose full label is longer than the view stays on screen with its rule
        warning, the label scrolling in its own box; fragile because scrolling the label into view pushed the choice
        out, and a hidden rule-saving choice could still be picked and sent (the reviewer's case on 0ccf880f)."""
        item = {"id": "test", "kind": "permission", "summary": "Test choices", "detail": {}, "questions": [],
                "display": {"title": "Run a command", "subject": "review-command", "lines": []},
                "choices": [{"id": "highlight-first", "short": "Allow always", "saves_rule": True,
                             "label": "Requested scope details " * 18 + "SCOPE-END"},
                            *[{"id": f"alt-{n}", "short": "Allow for session", "label": f"Alternative scope {n}"}
                              for n in range(1, 8)]]}
        async with self.app.run_test(size=(60, 20)) as pilot:
            screen = AnswerScreen("worker", item)
            await self.app.push_screen(screen)
            await self.settle(pilot)
            frame = "\n".join(strip.text for strip in screen._compositor.render_strips())
            self.assertIn("highlight-first", frame)
            self.assertIn("saves a rule", frame)
            self.assertIn("Requested scope details", frame)  # the full label starts in view, and scrolls on its own
            self.assertNotIn("SCOPE-END", frame)
        item["choices"][1] = {"id": "second", "short": "Allow always", "saves_rule": True,
                              "label": "SECOND-START " + "Other scope details " * 18}
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)
        async with self.app.run_test(size=(60, 20)) as pilot:
            screen = AnswerScreen("worker", item)
            await self.app.push_screen(screen)
            await self.settle(pilot)
            box = screen.query_one("#prompt-choice-long-box")
            await pilot.press("tab", "end")  # read the first label to its end
            await self.settle(pilot)
            self.assertGreater(box.scroll_y, 0)
            await pilot.press("shift+tab", "enter")  # pick it: the same choice keeps its place in the label
            await self.settle(pilot)
            self.assertGreater(box.scroll_y, 0)
            await pilot.press("down")  # another choice: its label from the start, where scopes differ
            await self.settle(pilot)
            self.assertEqual(box.scroll_y, 0)
            self.assertIn("SECOND-START", "\n".join(strip.text for strip in screen._compositor.render_strips()))

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
                await pilot.press("ctrl+enter")  # "Allow once" is highlighted, not picked
                await self.settle(pilot)
                self.assertIsInstance(self.app.screen, AnswerScreen)
                self.assertEqual(self.backend.answers, [])
                self.assertIn("Pick a choice", str(self.app.screen.query_one("#answer-error", Static).render()))
            with self.subTest("the prompt as core's display says it, not the harness's own detail"):
                self.assertIn("Run a command", str(self.app.screen.query_one("#dialog-title").render()))
                shown = "\n".join(str(widget.render()) for widget in self.app.screen.query(".prompt-detail"))
                self.assertIn("git push origin draft-pr\nPublish the draft branch", shown)
                self.assertNotIn("command:", shown)
            with self.subTest("choices by their short labels, the highlighted one's long label under them"):
                choices = self.app.screen.query_one("#prompt-choices")
                shown = [str(choices.get_option_at_index(i).prompt).split("  ")[0] for i in range(choices.option_count)]
                self.assertEqual(shown, ["Allow once", "Allow always", "Deny"])
                long = self.app.screen.query_one("#prompt-choice-long", Static)
                self.assertEqual(str(long.render()), "Yes")
                await pilot.press("down")
                self.assertEqual(str(long.render()), "Yes, and don't ask again for git push")
                await pilot.press("up")
            with self.subTest("Enter only picks: nothing is sent until Ctrl+Enter"):
                await pilot.press("enter")  # picks "Allow once"
                await self.settle(pilot)
                self.assertIsInstance(self.app.screen, AnswerScreen)
                self.assertEqual(self.backend.answers, [])
                self.assertIn("Picked: Allow once", str(self.app.screen.query_one("#prompt-picked", Static).render()))
            with self.subTest("a choice with its message, exactly as picked"):
                await pilot.press("down", "down", "enter")  # Allow once, Allow always, Deny: it takes a message
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
                # no display (an older adapter): the harness's own detail, as before
                self.assertIn("origin: the release checklist", "\n".join(
                    str(widget.render()) for widget in screen.query(".prompt-detail")))
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
                # no short label (an older adapter): nothing more to say under the choices
                self.assertFalse(screen.query_one("#prompt-choice-long-box").display)
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
                self.assertIn("The person answered in the terminal first. The answer: answered 'Which parts?' with UI.",
                              self.status())
                await self.until(pilot, lambda: "Last answer: The person" in self.line(), "the outcome in the inspector")
                self.assertIn("Last answer: The person answered", self.line())
            with self.subTest("a long answer is cut on the dashboard's lines, saying v shows all of it"):
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [dict(QUESTION, id="long-answer",
                                                                questions=QUESTION["questions"][:1])]
                self.app.refresh_snapshot()
                message = "keep the old build " * 40 + "MESSAGE-END"  # 771 characters, kept whole by core
                answer = self.backend.answer_prompt
                self.backend.answer_prompt = lambda root, reference, prompt_id, values: {
                    "id": prompt_id, "state": "answered", "reason": None, "cause": None, "saved_rule": False,
                    "by": {"kind": "mv2"}, "answer": {"choice": {"id": "deny", "label": "No", "short": "Deny"},
                                                      "message": message}}
                try:
                    await self.open(pilot, self.writer)
                    self.app.screen.query_one("#q0", SelectionList).select("API")
                    await pilot.press("ctrl+enter")
                    await self.settle(pilot)
                finally:
                    self.backend.answer_prompt = answer
                await self.until(pilot, lambda: "Last answer: (v shows" in self.line(), "the outcome")
                frame = [strip.text for strip in self.app.screen._compositor.render_strips()]
                status = self.app.query_one("#status", Static)
                # on screen, not just in the widgets: the pointer leads, so no width or line count clips it
                self.assertIn("(v shows the whole answer) Answered by", frame[status.region.y])
                self.assertIn("Last answer: (v shows the whole answer)", "\n".join(frame))
                self.assertNotIn("MESSAGE-END", "\n".join(frame))
                await pilot.press("v")
                await self.settle(pilot)
                details = "\n".join(str(widget.render()) for widget in self.app.screen.query(Static))
                self.assertIn("LAST ANSWER", details)
                self.assertIn("MESSAGE-END", details)  # all of it
                await pilot.press("escape")
                await self.settle(pilot)


    async def test_a_long_answers_outcome_points_to_v_on_screen_at_any_size(self):
        """At 120x36 and 60x20 a long answer's outcome shows 'v shows the whole answer' on the status line and in the
        inspector as drawn, and v has all of it; fragile because both lines are narrow and the inspector's rows are
        shared with the prompt, so a pointer at the end is clipped off screen (the reviewer's case on d69b71b)."""
        cases = [(size, message, end) for size in ((120, 36), (60, 20)) for message, end in (
            ("keep the old build " * 40 + "MESSAGE-END", "MESSAGE-END"),  # longer than any line: always cut
            ("keep the old build " * 5 + "MEDIUM-END", "MEDIUM-END"),  # fits some places, not others
            ("SHORT-END", "SHORT-END"))]  # fits a wide status line, not a narrow one
        for size, message, end in cases:
            with self.subTest(size=size, end=end):
                self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [dict(QUESTION, id="long-answer",
                                                                questions=QUESTION["questions"][:1])]
                self.backend.answer_prompt = lambda root, reference, prompt_id, values: {
                    "id": prompt_id, "state": "answered", "reason": None, "cause": None, "saved_rule": False,
                    "by": {"kind": "mv2"}, "answer": {"choice": {"id": "deny", "label": "No", "short": "Deny"},
                                                      "message": message}}
                async with self.app.run_test(size=size) as pilot:
                    await self.settle(pilot)
                    await pilot.press("p")
                    await self.open(pilot, self.writer)
                    self.app.screen.query_one("#q0", SelectionList).select("API")
                    await pilot.press("ctrl+enter")
                    await self.settle(pilot)
                    await self.until(pilot, lambda: "Answered by" in self.status(), "the outcome")
                    frame = [strip.text for strip in self.app.screen._compositor.render_strips()]
                    status = self.app.query_one("#status", Static)
                    # each line, as drawn, shows the answer to its end or says where the rest is: never cut silently
                    for where, shown in (("status", frame[status.region.y]),
                                         ("inspector", "\n".join(frame[:status.region.y]))):
                        self.assertTrue("v shows the whole answer" in shown or end in shown, (where, shown))
                    self.assertIn("Answered by", frame[status.region.y])
                    if end == "MESSAGE-END":
                        self.assertNotIn(end, "\n".join(frame))
                    await pilot.press("v")
                    await self.settle(pilot)
                    self.assertIn(end, "\n".join(str(widget.render()) for widget in self.app.screen.query(Static)))

    async def test_the_status_line_refits_after_a_resize_and_counts_wide_characters(self):
        """An answer's outcome on the status line shows its end or the pointer to v after the terminal narrows, and
        with wide characters that take two cells each; fragile because the line was fitted once, by characters, and
        clipped silently after a resize or with CJK text (the reviewer's cases on 38bc2f3)."""
        for name, message, end, sizes in (
                ("resize", "SHORT-END", "SHORT-END", ((120, 36), (60, 20))),  # fits at 120, not at 60
                ("wide", "全" * 18 + "WIDE-END", "WIDE-END", ((120, 36),))):  # fits in characters, not in cells
            with self.subTest(name):
                self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [dict(QUESTION, id=f"{name}-answer",
                                                                questions=QUESTION["questions"][:1])]
                self.backend.answer_prompt = lambda root, reference, prompt_id, values, message=message: {
                    "id": prompt_id, "state": "answered", "reason": None, "cause": None, "saved_rule": False,
                    "by": {"kind": "mv2"}, "answer": {"choice": {"id": "deny", "label": "No", "short": "Deny"},
                                                      "message": message}}
                async with self.app.run_test(size=sizes[0]) as pilot:
                    await self.settle(pilot)
                    await pilot.press("p")
                    await self.open(pilot, self.writer)
                    self.app.screen.query_one("#q0", SelectionList).select("API")
                    await pilot.press("ctrl+enter")
                    await self.settle(pilot)
                    await self.until(pilot, lambda: "Answered by" in self.status(), "the outcome")
                    for size in sizes:
                        await pilot.resize_terminal(*size)
                        await self.settle(pilot)
                        status = self.app.query_one("#status", Static)
                        shown = self.app.screen._compositor.render_strips()[status.region.y].text
                        self.assertTrue("v shows the whole answer" in shown or end in shown, (size, shown))

    async def test_a_multiline_reason_and_a_resized_inspector_show_the_end_or_the_pointer(self):
        """A reason with a line break shows on the one status line as one line, its end or the pointer to v; and after
        the terminal narrows, the inspector's Last answer is cut again for its new room; fragile because the status
        renderable skipped the cleaning a string status gets, and the inspector kept the cut made for the old width
        (the reviewer's cases on 77d411d)."""
        for case in ("multiline status", "resized inspector"):
            with self.subTest(case):
                multiline = case == "multiline status"
                self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)
                self.writer["status"] = "waiting"
                self.backend.prompts[self.writer["id"]] = [dict(QUESTION, id="another-answer",
                                                                questions=QUESTION["questions"][:1])]
                self.backend.answer_prompt = lambda root, reference, prompt_id, values, multiline=multiline: {
                    "id": prompt_id, "state": "answered", "cause": None, "saved_rule": False,
                    "reason": "First line of the outcome.\nSECOND-LINE" if multiline else None,
                    "by": {"kind": "mv2"}, "answer": {"choice": {"id": "deny", "label": "No", "short": "Deny"},
                                                      **({} if multiline else {"message": "Plain text INSPECTOR-END"})}}
                async with self.app.run_test(size=(120, 36)) as pilot:
                    await self.settle(pilot)
                    await pilot.press("p")
                    await self.open(pilot, self.writer)
                    self.app.screen.query_one("#q0", SelectionList).select("API")
                    await pilot.press("ctrl+enter")
                    await self.settle(pilot)
                    await self.until(pilot, lambda: "Last answer:" in self.line(), "the outcome")
                    status = self.app.query_one("#status", Static)
                    if multiline:
                        shown = self.app.screen._compositor.render_strips()[status.region.y].text
                        self.assertTrue("SECOND-LINE" in shown or "v shows the whole answer" in shown, shown)
                    else:
                        await pilot.resize_terminal(60, 20)
                        await self.settle(pilot)
                        frame = self.app.screen._compositor.render_strips()
                        shown = "\n".join(row.text for row in frame[:status.region.y])
                        self.assertIn("Last answer:", shown)
                        self.assertTrue("INSPECTOR-END" in shown or "v shows the whole answer" in shown, shown)

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
