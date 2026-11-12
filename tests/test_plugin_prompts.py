"""A plugin reading and answering the prompts an agent waits on through the plugin API (1.17), with only the
harness's side scripted (PluginTestCase.fake_prompts): the API's checks and core's caller rule, status gate,
validation, exclusive answer, record and wait are real."""
import json
import os
from pathlib import Path
import sys
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, config, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write

ANSWERER = {"module": "plugin", "api": "1.17", "permissions": ["agents.read", "prompts.answer"],
            "access": {"request": "act", "minimum": "observe"}}
BASH = api.WaitingPrompt(id="perm-1", kind="permission", tool="Bash", summary="rm -rf build", detail={"command": "x"},
                         choices=(api.PromptChoice(id="allow", label="Allow once"),
                                  api.PromptChoice(id="allow_always", label="Allow always", saves_rule=True),
                                  api.PromptChoice(id="deny", label="Deny", takes_message=True)))
ASK = api.WaitingPrompt(id="ask-1", kind="question", tool="AskUserQuestion", summary="Which colour?",
                        questions=(api.PromptQuestion(id="colour", question="Which colour?", free_text=False,
                                                      options=(("red", ""), ("blue", "")), takes_note=True),
                                   api.PromptQuestion(id="token", question="Your token?", secret=True),
                                   api.PromptQuestion(id="vault", question="Open the vault?", secret=True,
                                                      free_text=False, options=(("open", ""), ("shut", "")),
                                                      takes_note=True)))


LATER = api.WaitingPrompt(id="later-1", kind="question", tool="request_user_input_async", summary="Which branch?",
                          requires_waiting=False, questions=(api.PromptQuestion(id="0", question="Which branch?",
                                                                                options=(("main", ""), ("next", "")),
                                                                                free_text=False),))
NOW = api.WaitingPrompt(id="now-1", kind="question", tool="requestUserInput", summary="Proceed?",
                        questions=(api.PromptQuestion(id="0", question="Proceed?", options=(("yes", ""), ("no", "")),
                                                      free_text=False),))


class PromptApiTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("answerer", ANSWERER, {"plugin.py": "def register(mv): pass\n"})
        self.enable("answerer")
        # A bound generation: core answers only a prompt of the agent's recorded conversation and launch.
        self.directory = self.agent("worker", harness="claude", native_id=str(uuid4()), runtime={"status": "waiting"})
        self.worker = self.loaded_plugin("answerer").universe(self.root).agent("worker")

    def loaded_plugin(self, name):
        loader.reset()
        return api.Plugin(loader.load(self.entry(name)))

    def test_a_plugin_answers_an_open_prompt_once_and_never_as_the_person(self):
        """A plugin lists a waiting agent's prompts without the choices that save the person's rules, answers one
        once, reads a submitted answer later without resending, and every refusal (a rule-saving choice, a second
        answer, an answer from an agent's shell, an agent not waiting, observe access, a missing or access-only
        prompts.answer) sends nothing; fragile because answering approves commands with the person's authority,
        so a lost caller or permission check lets a plugin or an agent grant itself standing rules, unseen."""
        fake = self.fake_prompts([BASH, ASK], {}, "pending")
        listed = self.worker.waiting_prompts()
        self.assertEqual((listed.state, listed.coverage), ("ok", "complete"))
        self.assertIsInstance(listed.prompts[0], api.WaitingPrompt)
        self.assertIsInstance(listed.prompts[0].choices[0], api.PromptChoice)
        self.assertEqual([choice.id for choice in listed.prompts[0].choices], ["allow", "deny"])  # no rule-saving one
        self.assertEqual(listed.prompts[1].questions[0].options, (("red", ""), ("blue", "")))
        refusals = (  # (what, call, cause): each comes back refused and sends nothing
            ("a choice that saves the person's rule", lambda: self.worker.answer_prompt("perm-1", choice="allow_always"),
             "person_only"),
            ("a choice it doesn't offer", lambda: self.worker.answer_prompt("perm-1", choice="maybe"), "not_offered"),
            ("an answer missing a required question", lambda: self.worker.answer_prompt("ask-1",
                                                                                        answers={"colour": "red"}),
             "missing_answer"),
            ("an answer from an agent's shell", lambda: self.from_agent(lambda: self.worker.answer_prompt(
                "perm-1", choice="allow")), "agent_caller"),
        )
        for what, call, cause in refusals:
            with self.subTest(what):
                refused = call()
                self.assertIsInstance(refused, api.PromptResult)
                self.assertEqual((refused.state, refused.cause), ("refused", cause))
                self.assertTrue(refused.reason)
        self.assertEqual(fake.answers, [])
        answered = self.worker.answer_prompt("perm-1", choice="allow")
        self.assertEqual((answered.state, answered.saved_rule), ("answered", False))
        record = read(self.directory / "prompts" / "perm-1.json")
        self.assertEqual((record["caller"], record["choice"]), ({"kind": "plugin", "name": "answerer"}, "allow"))
        again = self.worker.answer_prompt("perm-1", choice="allow")
        self.assertEqual((again.state, again.cause), ("refused", "already_answered"))
        with self.subTest("a secret answer, submitted and read later"):
            with self.assertRaisesRegex(api.Error, "notes"):
                self.worker.answer_prompt("ask-1", answers={"colour": "blue", "token": "s3cret"}, notes=["why"])
            pending = self.worker.answer_prompt("ask-1", answers={"colour": "blue", "token": "s3cret", "vault": "open"},
                                                notes={"colour": "a calm one", "vault": "n0te-secret"}, wait=False)
            self.assertEqual(pending.state, "submitted")
            self.assertEqual(self.worker.prompt_result("ask-1").state, "submitted")
            fake.settle("ask-1")
            self.assertEqual(self.worker.prompt_result("ask-1").state, "answered")
            self.assertEqual(fake.answers[-1]["answers"], {"colour": ("blue",), "token": ("***",), "vault": ("***",)})
            self.assertEqual(fake.answers[-1]["notes"], {"colour": "a calm one", "vault": "***"})
            stored = (self.directory / "prompts" / "ask-1.json").read_text()
            for kept_out in ("s3cret", "n0te-secret", "a calm one"):  # no note, nor a secret, is kept
                self.assertNotIn(kept_out, stored)
        self.assertEqual([item["id"] for item in fake.answers], ["perm-1", "ask-1"])  # once each, never resent
        with self.subTest("an agent that isn't waiting"):
            runtime = read(self.directory / "runtime.json")
            write(self.directory / "runtime.json", {**runtime, "status": "idle"})
            listed = self.worker.waiting_prompts()  # only prompts that don't need it waiting: none here
            self.assertEqual((listed.state, listed.prompts), ("ok", ()))
            self.assertNotEqual(self.worker.answer_prompt("perm-2", choice="allow").state, "answered")
            self.assertEqual(len(fake.answers), 2)
        with self.subTest("observe access reads but never answers"):
            config.set_grant(self.root, "answerer", "observe")
            watcher = self.loaded_plugin("answerer").universe(self.root).agent("worker")
            self.assertEqual(watcher.prompt_result("perm-1").state, "answered")
            with self.assertRaises(api.AccessDenied):
                watcher.answer_prompt("perm-1", choice="allow")
        for name, manifest in (("reader", {**ANSWERER, "permissions": ["agents.read"]}),
                               ("granted", {k: v for k, v in ANSWERER.items() if k != "permissions"})):
            with self.subTest("without prompts.answer", plugin=name):
                self.write_plugin(name, manifest, {"plugin.py": "def register(mv): pass\n"})
                self.enable(name)
                other = self.loaded_plugin(name).universe(self.root).agent("worker")
                with self.assertRaisesRegex(api.Error, "prompts.answer"):
                    other.answer_prompt("perm-1", choice="allow")
        self.assertEqual(len(fake.answers), 2)

    def from_agent(self, call):
        with mock.patch.dict(os.environ, {"MV2_AGENT_ID": "someone"}):
            return call()


    def test_a_question_that_does_not_stop_the_agent_is_answered_once_as_its_input(self):
        """Async questions are answered while the agent runs, sent once, text kept nowhere; a lost gate is unseen."""
        write(self.directory / "runtime.json", {**read(self.directory / "runtime.json"), "status": "running"})
        later2 = api.WaitingPrompt(**{**LATER.__dict__, "id": "later-2"})
        later3 = api.WaitingPrompt(**{**LATER.__dict__, "id": "later-3"})
        later4 = api.WaitingPrompt(**{**LATER.__dict__, "id": "later-4"})
        fake = self.fake_prompts([LATER, NOW, later2, later3, later4], {"state": "answered"}, "pending",
                                 {"state": "unknown"}, {"state": "refused"})
        listed = self.worker.waiting_prompts()
        self.assertEqual((listed.state, [prompt.id for prompt in listed.prompts]),
                         ("ok", ["later-1", "later-2", "later-3", "later-4"]))  # not now-1: it needs waiting
        self.assertIs(listed.prompts[0].requires_waiting, False)
        refused = self.worker.answer_prompt("now-1", answers={"0": "yes"})
        self.assertEqual((refused.state, refused.cause, len(fake.answers)), ("refused", "not_waiting", 0))
        cases = (("later-1", "answered", "answered"), ("later-2", "submitted", "answered"),
                 ("later-3", "unknown", "unknown"), ("later-4", "refused", "refused"))
        for prompt, first, settled in cases:
            with self.subTest(prompt):
                result = self.worker.answer_prompt(prompt, answers={"0": "next"}, timeout=0)
                self.assertEqual(result.state, first, result)
                self.assertEqual(result.cause, "not_delivered" if first == "refused" else result.cause)
                self.assertEqual(fake.answers[-1]["input"], "0: next")
                if first == "submitted":
                    self.assertEqual(self.worker.prompt_result(prompt).state, "submitted")
                    fake.settle(prompt, {"state": "answered"})
                self.assertEqual(self.worker.prompt_result(prompt).state, settled)
                self.assertNotIn(fake.answers[-1]["input"], (self.directory / "prompts" / f"{prompt}.json").read_text())
                again = self.worker.answer_prompt(prompt, answers={"0": "main"})
                self.assertEqual(again.state, "refused")
        self.assertEqual(len(fake.answers), 4)  # one delivery each, never a second
        with self.subTest("a secret answer is delivered, never shown back or kept"):
            token = api.WaitingPrompt(id="later-5", kind="question", tool="request_user_input_async",
                                      summary="Token?", requires_waiting=False,
                                      questions=(api.PromptQuestion(id="0", question="Token?", secret=True),))
            fake = self.fake_prompts([token], {"state": "answered"})
            self.assertEqual(self.worker.answer_prompt("later-5", answers={"0": "s3cret"}).state, "answered")
            self.assertEqual(fake.answers[-1]["input"], "0: ***")
            self.assertNotIn("s3cret", (self.directory / "prompts" / "later-5.json").read_text())


if __name__ == "__main__":
    import unittest
    unittest.main()
