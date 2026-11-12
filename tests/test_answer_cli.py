"""mv2 answer: what it sends to core for each way of giving an answer, and how it reports each outcome."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import cli
from multiverse.harnesses.types import PromptChoice, PromptList, PromptQuestion, PromptResult, WaitingPrompt
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import answer as tool

PROMPTS = PromptList(state="ok", coverage="complete", prompts=(
    WaitingPrompt(id="perm1", kind="permission", tool="Bash", summary="rm -rf build", choices=(
        PromptChoice(id="allow", label="Yes"), PromptChoice(id="allow_always", label="Always", saves_rule=True),
        PromptChoice(id="deny", label="No", takes_message=True))),
    WaitingPrompt(id="ask1", kind="question", tool="AskUserQuestion", summary="Which database?", questions=(
        PromptQuestion(id="Which database should I use?", header="Database", question="Which database should I use?",
                       options=(("Postgres", ""), ("SQLite", ""))),
        PromptQuestion(id="Which = parts?", header="Parts", question="Which = parts?", multiple=True,
                       options=(("API", ""), ("UI", ""))),
        PromptQuestion(id="token", header="Token", question="Your token?", secret=True))),
    WaitingPrompt(id="mcp1", kind="question", tool="user_input", summary="Run the tool?", questions=(
        PromptQuestion(id="run", header="Run", question="Run the tool?", options=(("Allow", ""), ("Other", "")),
                       free_text=False, takes_note=True),
        PromptQuestion(id="key", header="Key", question="Its key?", secret=True, takes_note=True, required=False))),
    WaitingPrompt(id="async1", kind="question", tool="request_user_input_async", summary="Which branch?",
                  requires_waiting=False, questions=(
        PromptQuestion(id="branch", header="Branch", question="Which branch?", options=(("main", ""), ("dev", ""))),)),
))


class AnswerCommandTests(PluginTestCase):
    def mv2(self, *args, stdin=""):
        """What `mv2 answer ARGS` prints and its exit status, run through mv2's own entry point."""
        out, err = io.StringIO(), io.StringIO()
        status = 0
        with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "answer", "worker", *args]), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit:
                status = exit.code or 0
        return status, out.getvalue(), err.getvalue()

    def test_each_way_of_answering_reaches_core_as_one_answer_and_each_outcome_is_reported_as_it_is(self):
        """--answer by number, header or text with labels repeated for a multi-select question, a secret answer only
        through --stdin, --result that never sends, and an exit status that is 0 only when the answer took or may
        still; fragile because a wrong question key or a dropped label sends a different answer than meant, which
        then stands (a prompt takes one answer), a secret on the command line or quoted in an error stays in shell
        history or the terminal, and a script reading the exit status would take 'the person answered first' or 'not
        sent' for success."""
        sent, outcome = [], [PromptResult(id="ask1", state="answered")]

        def answer(root, agent, id, **given):
            sent.append((id, given))
            return outcome[0]

        reads = []
        with mock.patch.object(tool.prompts, "waiting", return_value=PROMPTS), \
                mock.patch.object(tool.prompts, "answer", side_effect=answer), \
                mock.patch.object(tool.prompts, "result", side_effect=lambda root, agent, id: reads.append(id) or
                                  PromptResult(id=id, state="person", person_answer={
                                      "choice": "deny", "answers": {"Database": ["SQLite"]}, "typed": {"Why?": ["no"]}})):
            with self.subTest("the list shows choices, rule-saving ones and questions by number"):
                status, out, _ = self.mv2()
                self.assertEqual(status, 0)
                self.assertIn("perm1  permission  Bash: rm -rf build", out)
                self.assertIn("allow_always (Always) [saves a rule; person only]", out)
                self.assertIn("1. Database: Which database should I use?  [Postgres | SQLite]", out)
                self.assertIn("secret: answer with --stdin", out)
                self.assertIn("async1  question  request_user_input_async: Which branch?  (doesn't need the agent "
                              "waiting)", out)
                self.assertNotIn("rm -rf build  (doesn't need", out)
            with self.subTest("a choice with its message"):
                sent.clear()
                status, out, _ = self.mv2("perm1", "--choice", "deny", "--message", "use staging", "--timeout", "5")
                self.assertEqual((status, out.strip()), (0, "Answered."))
                self.assertEqual(sent, [("perm1", {"choice": "deny", "message": "use staging", "answers": None,
                                                   "notes": None, "timeout": 5.0, "wait": True})])
            with self.subTest("--answer by number, header or text; a label with '=', several for one question"):
                sent.clear()
                status, _, _ = self.mv2("ask1", "--answer", "1=Postgres", "--answer", "Parts=API",
                                        "--answer", "Which = parts?=UI")
                self.assertEqual(status, 0)
                self.assertEqual(sent[0][1]["answers"], {"Which database should I use?": ("Postgres",),
                                                         "Which = parts?": ("API", "UI")})
            with self.subTest("--note goes with its question's answer, one per question, a secret one only on stdin"):
                sent.clear()
                status, _, _ = self.mv2("mcp1", "--answer", "Run=Other", "--note", "1=only in the sandbox")
                self.assertEqual(status, 0)
                self.assertEqual((sent[0][1]["answers"], sent[0][1]["notes"]),
                                 ({"run": ("Other",)}, {"run": "only in the sandbox"}))
                status, _, err = self.mv2("mcp1", "--answer", "1=Allow", "--note", "1=a", "--note", "Run=b")
                self.assertEqual(status, 1)
                self.assertIn("one --note", err)
                status, _, err = self.mv2("mcp1", "--answer", "1=Allow", "--note", "Key=k9")
                self.assertEqual((status, len(sent)), (1, 1))
                self.assertIn("give its note with --stdin", err)
                status, _, _ = self.mv2("mcp1", "--stdin", stdin=json.dumps(
                    {"answers": {"1": "Allow"}, "notes": {"Key": "k9"}}))
                self.assertEqual((status, sent[-1][1]["notes"]), (0, {"key": "k9"}))
            with self.subTest("a secret answer is refused on the command line and taken from --stdin"):
                sent.clear()
                status, _, err = self.mv2("ask1", "--answer", "Token=hunter2")
                self.assertEqual((status, sent), (1, []))
                self.assertIn("--stdin", err)
                status, _, _ = self.mv2("ask1", "--stdin", stdin=json.dumps({"answers": {"3": "hunter2", "1": "SQLite"}}))
                self.assertEqual(status, 0)
                self.assertEqual(sent[0][1]["answers"], {"token": ("hunter2",), "Which database should I use?": ("SQLite",)})
            with self.subTest("a malformed --answer or --note never echoes what was typed"):
                sent.clear()
                for flag in ("--answer", "--note"):
                    status, out, err = self.mv2("mcp1", flag, "SENTINEL-s3cret")
                    self.assertEqual((status, sent), (1, []))
                    self.assertIn("QUESTION=", err)
                    self.assertNotIn("SENTINEL", out + err)
            with self.subTest("--stdin takes notes and answers only as objects; null or left out means none"):
                for key, value in (("notes", []), ("notes", 0), ("notes", ""), ("notes", False), ("answers", [])):
                    status, _, err = self.mv2("mcp1", "--stdin", stdin=json.dumps({"answers": {"1": "Allow"}, key: value}))
                    self.assertEqual((status, sent), (1, []))
                    self.assertIn(f"{key} must be an object", err)
                status, _, _ = self.mv2("mcp1", "--stdin", stdin=json.dumps({"answers": {"1": "Allow"}, "notes": None}))
                self.assertEqual((status, sent[-1][1]["notes"]), (0, None))
            for listed in (PromptList(state="unavailable", reason="The agent is not running"),
                           PromptList(state="ok", coverage="complete", prompts=PROMPTS.prompts[:1])):
                for read, says in (
                        (PromptResult(id="mcp1", state="person", cause="person",
                                      reason="The person answered it in the terminal first. Nothing was sent.",
                                      person_answer={"answers": {"Run": ["Allow"]}}),
                         "The person answered it in the terminal first. Nothing was sent. They answered 'Run' with Allow."),
                        (PromptResult(id="mcp1", state="refused", cause="not_answered",
                                      reason="mv2 sent no answer to prompt mcp1, and it is still open."),
                         "mv2 sent no answer to prompt mcp1, and it is still open."),
                        (PromptResult(id="mcp1", state="answered"), "mv2 already sent an answer to prompt mcp1"),
                        (PromptResult(id="mcp1", state="refused", cause="unconfirmed", reason="Can't tell."),
                         "or give the answer with --stdin"),
                        (ValueError("The prompt result is damaged"), "The prompt result is damaged")):
                    with self.subTest("an answer on the command line to a prompt mv2 can't list says what became of "
                                      "it, never sends, and suggests --stdin only when it may be open",
                                      state=listed.state, read=getattr(read, "cause", None) or repr(read)):
                        sent.clear()
                        with mock.patch.object(tool.prompts, "waiting", return_value=listed), \
                                mock.patch.object(tool.prompts, "result",
                                                  **({"side_effect": read} if isinstance(read, Exception) else
                                                     {"return_value": read})):
                            for flag, given in (("--answer", "token=hunter2"), ("--note", "key=k9")):
                                status, _, err = self.mv2("mcp1", flag, given)
                                self.assertEqual((status, sent), (1, []))  # core is never asked to send
                                self.assertIn(says, err)
                                self.assertNotIn("hunter2", err)
                                if "--stdin" not in says:
                                    self.assertNotIn("--stdin", err)
                            status, _, _ = self.mv2("ask1", "--stdin", stdin=json.dumps({"answers": {"token": "hunter2"}}))
                            self.assertEqual((status, sent[0][1]["answers"]), (0, {"token": ("hunter2",)}))
            with self.subTest("sent first, where the harness doesn't confirm more, counts as success"):
                outcome[0] = PromptResult(id="perm1", state="sent_first", reason="Sent first; Codex doesn't confirm it.")
                status, out, _ = self.mv2("perm1", "--choice", "allow")
                self.assertEqual((status, out.strip()), (0, "Sent first; Codex doesn't confirm it."))
            with self.subTest("answered, with core's reason saying what it proves"):
                said = "Delivered to the conversation as the answer; the card may still show."
                outcome[0] = PromptResult(id="perm1", state="answered", reason=said)
                status, out, _ = self.mv2("perm1", "--choice", "allow")
                self.assertEqual((status, out.strip()), (0, f"Answered. {said}"))
            with self.subTest("--timeout 0 returns once sent, and a pending outcome says how to read it"):
                sent.clear()
                outcome[0] = PromptResult(id="perm1", state="submitted")
                status, out, _ = self.mv2("perm1", "--choice", "allow", "--timeout", "0")
                self.assertEqual((status, sent[0][1]["wait"]), (0, False))
                self.assertIn("mv2 answer worker --result perm1", out)
            for state, reason, cause, says in (
                    ("person", None, "person", "The person answered first"),
                    ("gone", "The turn ended before the answer arrived.", "turn_ended",
                     "The turn ended before the answer arrived."),
                    ("gone", "The agent now waits on another prompt, ask2.", "replaced",
                     "another prompt, ask2. Read it with mv2 answer worker."),
                    ("unknown", "Sent; mv2 can't confirm it took.\nPossibly the person answered first.", None,
                     "Sent; mv2 can't confirm it took.\nPossibly the person answered first."),
                    ("refused", None, "not_waiting", "Not sent")):
                with self.subTest(state=state, cause=cause):  # the reason as core gives it; ours only when it gives none
                    outcome[0] = PromptResult(id="perm1", state=state, reason=reason, cause=cause)
                    status, out, err = self.mv2("perm1", "--choice", "allow")
                    self.assertEqual(status, 1)
                    self.assertIn(says, err)
                    status, out, _ = self.mv2("perm1", "--choice", "allow", "--json")
                    self.assertEqual((status, json.loads(out)["state"], json.loads(out)["ok"]), (1, state, False))
            with self.subTest("--result reads the outcome and never sends"):
                sent.clear()
                status, _, err = self.mv2("--result", "perm1")
                self.assertEqual((status, sent, reads), (1, [], ["perm1"]))
                self.assertIn("The person answered first. They chose deny; answered 'Database' with SQLite; typed 'no' "
                              "for 'Why?'.", err)


if __name__ == "__main__":
    unittest.main()
