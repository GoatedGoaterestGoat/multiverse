"""A plugin's one-shot run through the plugin API (1.14): the API's checks, its environment and its records, with
core's run replaced so nothing is spawned."""
import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.harnesses import types
from multiverse.plugins import api, config, loader, run_records
from multiverse.plugins.testing import ClaudeRows, PluginTestCase
from multiverse.runtime import runs
from multiverse.tools import plugins as plugins_tool
from multiverse.universe import templates

RUNNER = {"module": "plugin", "api": "1.14", "permissions": ["agents.read", "transcripts.read", "models.claude"]}
OPTIONS = types.RunOptions(supported=True, images=True, formats=("claude-jsonl",), transport_retries=(0, 15))


class RunApiTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("runner", RUNNER, {"plugin.py": "def register(mv): pass\n"})
        self.enable("runner")
        self.mv = api.Plugin(loader.load(self.entry("runner")))
        self.claude = self.mv.universe(self.root).harness("claude")
        self.calls = []

    def core(self, answer=None, options=OPTIONS):
        """Core's run replaced: it records each call and answers with `answer` (a RunReply, or a RunFailure it
        raises)."""
        def run_once(adapter, request, **keywords):
            self.calls.append((request, keywords))
            if isinstance(answer, Exception):
                raise answer
            return answer or types.RunReply(text="Only the plugin sees this", model="claude-haiku-4-5", usage={"input": 3,
                                            "output": 1, "cache_read": 40, "cache_write": 606}, cost_usd=0.0004, stop="end_turn", duration=1.5)
        return mock.patch.multiple(runs, run_once=run_once, options=lambda adapter, *, environment, root=None: options)

    def run_once(self, messages=(), **keywords):
        arguments = {"prompt": "Summarise", "system": "You summarise.", "model": "haiku", **keywords}
        return self.claude.run_once(list(messages), **arguments)

    def records(self):
        path = run_records.path(self.root, "runner")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    def test_nothing_is_sent_for_a_request_the_api_refuses(self):
        """Every argument, message, permission, access level and agent is checked before core is called, so a
        refused run spends nothing and leaves no record; fragile because a check run after the spawn still raises
        the same error, so a wrong order goes unnoticed while it costs the person money."""
        rows = ClaudeRows()
        rows.prompt("Hello")
        reply = rows.response("Hi")
        fake = self.transcript(self.agent("worker", native_storage_environment={"CLAUDE_CONFIG_DIR": str(
            self.claude_config)}), rows.rows)
        worker = self.mv.universe(self.root).agent("worker")
        bodiless = worker.transcript_rows(bodies=False).rows[1]
        stored = worker.transcript_rows().rows[1]
        other = self.subverse("other")
        self.enable("runner", root=other)
        stranger = self.agent("stranger", root=other)
        codex_agent = self.agent("coder", harness="codex")
        image = api.ImageBlock("image/png", b"\x89PNG")
        refused = {  # name: (arguments, the error and its text)
            "a row without its body": ({"messages": [bodiless]}, api.Error, "needs its body"),
            "a row of another format": ({"messages": [api.Row(**{**stored.__dict__, "format": "codex-rollout"})]},
                                        api.Error, "build a Message"),
            "an image from the model": ({"messages": [api.Message("assistant", [image])]}, api.Error,
                                        "ImageBlocks in user messages"),
            "an image too large": ({"messages": [api.Message("user", [api.ImageBlock("image/png",
                                   b"x" * (api.IMAGE_BYTES + 1))])]}, api.Error, "at most 5 MB"),
            "no blocks": ({"messages": [api.Message("user", [])]}, api.Error, "at least one block"),
            "another role": ({"messages": [api.Message("system", [api.TextBlock("x")])]}, api.Error, "role"),
            "no prompt": ({"prompt": " "}, api.Error, "prompt must be text"),
            "a timeout out of range": ({"timeout": 0}, api.Error, "timeout must be seconds"),
            "retries out of the harness's range": ({"transport_retries": 99}, api.Error, "0 to 15"),
            "another universe's agent": ({"agent": self.mv.universe(other).agent("stranger")}, api.Error,
                                         "another universe"),
            "an agent of another harness": ({"agent": self.mv.universe(self.root).agent("coder")}, api.Error,
                                            "runs codex"),
        }
        self.agent("unproven", native_session_id="s1")
        refused["an agent whose storage is unproven"] = ({"agent": self.mv.universe(self.root).agent("unproven")},
                                                         api.RunError, "resume it once")
        with self.core():
            for name, (arguments, error, text) in refused.items():
                with self.subTest(name):
                    with self.assertRaisesRegex(error, text):
                        self.run_once(**arguments)
            with self.subTest("a harness that can't run once"):
                with self.core(options=types.RunOptions(supported=False, reason="not logged in")):
                    with self.assertRaisesRegex(api.RunError, "not logged in") as caught:
                        self.run_once()
                self.assertEqual(caught.exception.kind, "unavailable")
            with self.subTest("without the model permission"):
                self.write_plugin("loose", {**RUNNER, "permissions": ["agents.read"]},
                                  {"plugin.py": "def register(mv): pass\n"})
                self.enable("loose")
                loose = api.Plugin(loader.load(self.entry("loose"))).universe(self.root).harness("claude")
                with self.assertRaisesRegex(api.Error, "models.claude"):
                    loose.run_once([], prompt="x", system="", model="haiku")
            with self.subTest("granted observe only"):
                config.set_grant(self.root, "runner", "observe")
                with self.assertRaises(api.AccessDenied):
                    self.run_once()
                config.set_grant(self.root, "runner", "act")
        self.assertEqual((self.calls, self.records()), ([], []))  # nothing reached core, nothing recorded
        del fake, reply, stranger, codex_agent

    def test_what_a_run_sends_answers_and_records(self):
        """The request core gets (messages converted, rows' bodies copied, an agent's recorded storage over this
        process's environment, inside its own session the inherited one), the Reply or RunError the plugin gets,
        and one metadata-only record per run that mv2 plugins info sums; fragile because a wrong environment runs
        under another login, and a lost record or one with content in it goes unnoticed until someone asks what a
        plugin spent."""
        rows = ClaudeRows()
        rows.prompt("Hello there")
        rows.response("Hi")
        home = self.work / "agent-claude"
        self.transcript(self.agent("worker", native_storage_environment={"CLAUDE_CONFIG_DIR": str(home)}), rows.rows)
        worker = self.mv.universe(self.root).agent("worker")
        history = worker.transcript_rows(history=True).rows
        cancel = threading.Event()
        with self.subTest("a reply"), self.core():
            reply = self.run_once([*history, api.Message("user", [api.TextBlock("Look"), api.ImageBlock("image/png", b"png")]),
                              api.Message("assistant", [api.TextBlock("Seen")])], agent=worker, effort="low",
                             transport_retries=2, timeout=30, cancel=cancel)
            request, keywords = self.calls[-1]
            self.assertEqual((reply.text, reply.model, reply.usage, reply.cost_usd, reply.stop),
                             ("Only the plugin sees this", "claude-haiku-4-5",
                              {"input": 3, "output": 1, "cache_read": 40, "cache_write": 606}, 0.0004,
                              "end_turn"))
            self.assertIsInstance(reply.usage, dict)
            self.assertEqual([type(item).__name__ for item in request.messages], ["RunRow", "RunRow", "RunMessage",
                                                                                   "RunMessage"])
            self.assertEqual(request.messages[0].data, history[0].data)
            self.assertIsNot(request.messages[0].data, history[0].data)  # a copy: the plugin's row stays its own
            self.assertEqual([type(block).__name__ for block in request.messages[2].blocks], ["RunText", "RunImage"])
            self.assertEqual((request.prompt, request.system, request.model, request.effort, request.transport_retries),
                             ("Summarise", "You summarise.", "haiku", "low", 2))
            self.assertEqual((keywords["plugin"], keywords["agent_directory"], keywords["timeout"],
                              keywords["cancel"]), ("runner", worker.directory, 30, cancel))
            self.assertEqual(keywords["environment"]["CLAUDE_CONFIG_DIR"], str(home))  # the agent's own login
            self.assertFalse(any(key.startswith("MV2_") for key in keywords["environment"]))
        with self.subTest("inside the agent's own session, without a record"):
            self.agent("unproven", native_session_id="s1")
            unproven = self.mv.universe(self.root).agent("unproven")
            with self.core(), mock.patch.dict(os.environ, {"MV2_AGENT_DIR": str(unproven.directory)}):
                self.run_once(agent=unproven)
            self.assertEqual(self.calls[-1][1]["environment"].get("CLAUDE_CONFIG_DIR"), os.environ["CLAUDE_CONFIG_DIR"])
        with self.subTest("a relative storage variable is made absolute where it was set"):
            self.agent("sessioned", native_session_id="s2", workspace=str(self.work / "agent-work"))
            sessioned = self.mv.universe(self.root).agent("sessioned")
            with self.core(), mock.patch.dict(os.environ, {"MV2_AGENT_DIR": str(sessioned.directory),
                                                           "CLAUDE_CONFIG_DIR": "relative-claude"}):
                self.run_once(agent=sessioned)  # inside its own session: against its recorded workspace
                self.assertEqual(self.calls[-1][1]["environment"]["CLAUDE_CONFIG_DIR"],
                                 str(self.work / "agent-work" / "relative-claude"))
                self.run_once()  # no agent: against this process's directory
                self.assertEqual(self.calls[-1][1]["environment"]["CLAUDE_CONFIG_DIR"],
                                 os.path.join(os.getcwd(), "relative-claude"))
            self.agent("unplaced", native_session_id="s3", workspace="not-absolute")
            unplaced = self.mv.universe(self.root).agent("unplaced")
            with self.core(), mock.patch.dict(os.environ, {"MV2_AGENT_DIR": str(unplaced.directory),
                                                           "CLAUDE_CONFIG_DIR": "relative-claude"}):
                calls = len(self.calls)
                with self.assertRaisesRegex(api.RunError, "relative") as caught:
                    self.run_once(agent=unplaced)
                self.assertEqual((caught.exception.kind, len(self.calls)), ("unavailable", calls))
        failure = types.RunFailure("rate_limit", "API Error: 529 overloaded", retry_after=12, usage={"input": 9},
                                   cost_usd=0.001, model="claude-haiku-4-5", duration=0.25)
        with self.subTest("a failure"), self.core(failure):
            with self.assertRaisesRegex(api.RunError, "529 overloaded") as caught:
                self.run_once()
            self.assertEqual((caught.exception.kind, caught.exception.retry_after, caught.exception.confirmed),
                             ("rate_limit", 12, True))
            self.assertNotIsInstance(caught.exception, OSError)
        with self.subTest("an unconfirmed cancel"), self.core(types.RunFailure("cancelled", "stopped", confirmed=False)):
            with self.assertRaises(api.RunError) as caught:
                self.run_once()
            self.assertEqual((caught.exception.kind, caught.exception.confirmed), ("cancelled", False))
        records = self.records()
        self.assertEqual([item["outcome"] for item in records], ["ok", "ok", "ok", "ok", "rate_limit", "cancelled"])
        self.assertEqual((records[0]["agent"], records[0]["input_tokens"], records[0]["cost_usd"], records[0]["duration"]),
                         (worker.id, 3, 0.0004, 1.5))
        self.assertEqual((records[0]["cache_read_tokens"], records[0]["cache_write_tokens"]), (40, 606))
        written = run_records.path(self.root, "runner").read_text()
        for content in ("Hello there", "Summarise", "You summarise", "Look", "Seen", "Only the plugin sees"):
            self.assertNotIn(content, written)  # metadata only, never what was sent or answered
        with self.subTest("mv2 plugins info sums the last day"):
            with run_records.path(self.root, "runner").open("a") as stream:
                old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
                stream.write(json.dumps({"at": old, "outcome": "ok", "cost_usd": 5.0}) + "\n{damaged\n")
            result = plugins_tool.run(self.root, plugin_action="info", name="runner")
            self.assertEqual(result["runs"], {"runs": 6, "failed": 2, "cost_usd": 0.0026,
                                              "outcomes": {"ok": 4, "rate_limit": 1, "cancelled": 1}})
            self.assertIn("Runs in the last day: 6 (2 failed: cancelled 1, rate_limit 1), $0.0026.",
                          plugins_tool.text("info", result))
        # An agent replaced while its run is prepared never runs under the removed agent's login: through the real
        # core, with only the run itself faked.
        fake = self.fake_runs(api.Reply(text="never sent"), api.Reply(text="never sent"))
        context = [api.Message("user", [api.TextBlock("Earlier")]), api.Message("assistant", [api.TextBlock("Noted")])]
        old, new = self.work / "old-store", self.work / "new-store"

        def replace(directory):
            properties = json.loads((directory / "properties.json").read_text())
            directory.rename(directory.with_name(directory.name + ".removed"))
            directory.mkdir()
            (directory / "properties.json").write_text(json.dumps(
                {**properties, "native_storage_environment": {"CLAUDE_CONFIG_DIR": str(new)}}))
        moments = (("during the storage read", "storage"), ("during the options call", "options"),
                   ("during an unsupported options call", "options-unsupported"),
                   ("as core's run is entered", "core"))
        for name, moment in moments:
            with self.subTest(f"an agent replaced {name}"):
                directory = self.agent(f"swapped-{moment}", native_session_id="11111111-1111-4111-8111-111111111111",
                                       native_storage_environment={"CLAUDE_CONFIG_DIR": str(old)})
                swapped = self.mv.universe(self.root).agent(f"swapped-{moment}")
                reading, options, entering = api.Plugin.harness_info, runs.options, runs.run_once
                asked = []

                def read_then_replace(plugin, *arguments, **keywords):
                    info = reading(plugin, *arguments, **keywords)
                    if keywords.get("agent") is not None and directory.exists():
                        replace(directory)  # after its storage was read
                    return info

                def options_then_replace(adapter, *, environment, root=None):  # the harness may take a moment here
                    asked.append(True)
                    if moment.startswith("options") and directory.exists():
                        replace(directory)
                    if moment == "options-unsupported":
                        return types.RunOptions(supported=False, reason="no one-shot support")
                    return options(adapter, environment=environment, root=root)

                entered = []

                def enter_then_replace(adapter, request, **keywords):  # after both of the API's checks
                    entered.append(True)
                    if moment == "core" and directory.exists():
                        replace(directory)
                    return entering(adapter, request, **keywords)
                patches = [mock.patch.object(runs, "options", options_then_replace),
                           mock.patch.object(runs, "run_once", enter_then_replace)]
                if moment == "storage":
                    patches.append(mock.patch.object(api.Plugin, "harness_info", read_then_replace))
                with contextlib.ExitStack() as stack, self.assertRaises(api.AgentGone):
                    for patch in patches:
                        stack.enter_context(patch)
                    self.run_once(context, agent=swapped)
                if moment == "storage":
                    self.assertEqual(asked, [])  # refused right after the read, before the options call
                if moment != "core":
                    self.assertEqual(entered, [])  # refused by the API itself, before core's run is entered
        self.assertEqual(fake.requests, [])  # none reached the run

    def test_fake_runs_answer_a_plugins_runs_behind_the_real_checks(self):
        """PluginTestCase.fake_runs answers runs from a script and records each request, while the API's, the
        harness's and core's checks still run; fragile because a fake that skipped them would let a plugin's
        tests pass with requests the real harness refuses, found only when the person's run fails. Also where runs
        may live: a universe in the home folder runs, and run_options() refuses what run_once would, since a
        harness reported as able to run while every run fails leaves a plugin like compaction on and broken."""
        fake = self.fake_runs(api.Reply(text="A summary", model="claude-haiku-4-5", usage={"input": 5}),
                              api.RunError("overloaded", kind="rate_limit", retry_after=3),
                              api.Reply(text="A summary", model="claude-haiku-4-5", usage={"input": 5}))
        reply = self.claude.run_once([api.Message("user", [api.TextBlock("Earlier")]),
                                      api.Message("assistant", [api.TextBlock("Noted")])],
                                     prompt="Summarise", system="You summarise.", model="haiku", timeout=60)
        self.assertEqual((reply.text, reply.usage), ("A summary", {"input": 5}))
        self.assertEqual(fake.requests[0]["messages"], [api.Message("user", [api.TextBlock("Earlier")]),
                                                        api.Message("assistant", [api.TextBlock("Noted")])])
        self.assertEqual((fake.requests[0]["prompt"], fake.requests[0]["model"], fake.requests[0]["timeout"]),
                         ("Summarise", "haiku", 60))
        context = [api.Message("user", [api.TextBlock("Earlier")]), api.Message("assistant", [api.TextBlock("Noted")])]
        with self.assertRaises(api.RunError) as caught:
            self.run_once(context)
        self.assertEqual((caught.exception.kind, caught.exception.retry_after), ("rate_limit", 3))
        with self.subTest("the harness's own checks still run"):
            with self.assertRaises(api.RunError) as caught:  # Claude's: the messages end with the model's
                self.run_once([api.Message("user", [api.TextBlock("Hello")])])
            self.assertEqual(caught.exception.kind, "invalid_request")
            with self.assertRaises(api.RunError) as caught:  # core's: a model the harness doesn't take
                self.run_once(context, model="haiku", effort="ultra")
            self.assertEqual(caught.exception.kind, "invalid_request")
        with self.subTest("a universe made in the home folder runs, and one under a universe's state/ is refused"):
            home = templates.create(self.home / ".multiverse", "home", workdir=self.home)  # mv2 init in ~
            self.enable("runner", root=home)
            here = self.mv.universe(home).harness("claude")
            self.assertTrue(here.run_options().supported)
            self.assertEqual(here.run_once(context, prompt="Summarise", system="", model="haiku").text, "A summary")
            buried = self.root / "state" / "home"  # a HOME that puts the run base under this universe's state/
            buried.mkdir()
            with mock.patch.dict(os.environ, HOME=str(buried)):
                options = self.claude.run_options()
                with self.assertRaises(api.RunError) as caught:
                    self.run_once(context)
            self.assertFalse(options.supported)  # run_options() says what every run would hit
            self.assertEqual((caught.exception.kind, str(caught.exception)), ("unavailable", options.reason))
        self.assertEqual(len(fake.requests), 3)  # the refused ones never reached the run
        self.assertEqual([item["outcome"] for item in self.records()], ["ok", "rate_limit", "invalid_request",
                                                                         "invalid_request"])  # run_options() refused that one before it ran
