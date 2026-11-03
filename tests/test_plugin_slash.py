"""A plugin running an agent's harness commands through the plugin API (1.16), with only the harness's side
scripted (PluginTestCase.fake_slash): the API's checks and core's refusals, records and wait are real."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, config, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import now, write

SLASHER = {"module": "plugin", "api": "1.16", "permissions": ["agents.read", "agents.control"],
           "access": {"request": "act", "minimum": "observe"}}
LISTED = [api.SlashCommand(name="compact", description="Compact the conversation", source="builtin"),
          api.SlashCommand(name="clear", source="builtin"),
          api.SlashCommand(name="model", source="builtin", needs_args=True, changes_settings=True),
          api.SlashCommand(name="exit", source="builtin"),
          api.SlashCommand(name="summarise", source="user")]


class SlashApiTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("slasher", SLASHER, {"plugin.py": "def register(mv): pass\n"})
        self.enable("slasher")
        self.directory = self.agent("worker", harness="claude")
        self.worker = api.Plugin(loader.load(self.entry("slasher"))).universe(self.root).agent("worker")

    def test_a_plugins_command_runs_once_through_mv2s_checks_and_reports_back(self):
        """A plugin lists and runs an agent's harness commands and reads a queued one later, while each refusal
        (a command mv2 manages, a bare picker, a settings-changing one, a name not listed, a focused agent, an
        access level below act, a missing permission) sends nothing; fragile because a plugin isn't the person,
        so a lost caller or permission check lets a plugin save the person's Claude defaults or run a command into
        a question the agent is focused on, and nothing shows it."""
        fake = self.fake_slash(LISTED, {"output": "Compacted", "prompted": False}, "queued", {"prompted": True})
        listed = self.worker.slash_commands()
        self.assertEqual(listed.state, "ok")
        self.assertIsInstance(listed.commands[0], api.SlashCommand)
        self.assertEqual({item.name: item.changes_settings for item in listed.commands}["model"], True)
        ran = self.worker.slash("compact")
        self.assertIsInstance(ran, api.CommandResult)
        self.assertEqual((ran.state, ran.output, ran.command, ran.saved_default), ("ran", "Compacted", "compact",
                                                                                    False))
        queued = self.worker.slash("/compact", wait=False)  # a leading slash is dropped
        self.assertEqual(queued.state, "queued")
        self.assertEqual(self.worker.slash_result(queued.id).state, "queued")
        fake.finish(queued.id, {"output": "Compacted later"})
        later = self.worker.slash_result(queued.id)
        self.assertEqual((later.id, later.state, later.output), (queued.id, "ran", "Compacted later"))
        prompted = self.worker.slash("summarise", "the last hour")
        self.assertEqual((prompted.state, prompted.prompted, prompted.args), ("ran", True, "the last hour"))
        self.assertEqual([(item["command"], item["args"]) for item in fake.requests],
                         [("compact", ""), ("compact", ""), ("summarise", "the last hour")])
        refusals = {"a command mv2 manages": ("exit", ""), "a bare picker": ("model", ""),
                    "a settings-changing command, from a plugin": ("model", "sonnet"),
                    "a name the harness doesn't list": ("nonesuch", "")}
        for label, (command, args) in refusals.items():
            with self.subTest(label):
                refused = self.worker.slash(command, args)
                self.assertEqual(refused.state, "refused")
                self.assertTrue(refused.reason)
        self.assertIn("mv2 resume worker --model X --effort Y", self.worker.slash("model", "sonnet").reason)
        with self.subTest("a focused agent: a plugin isn't the person"):
            write(self.directory / "focus.json", {"since": now(), "by": "user"})  # the person focused it
            self.assertEqual(self.worker.slash("compact").state, "refused")
            (self.directory / "focus.json").unlink()
        self.assertEqual(len(fake.requests), 3)  # none of the refused ones reached the harness
        with self.subTest("observe access lists but never runs"):
            config.set_grant(self.root, "slasher", "observe")
            loader.reset()
            watcher = api.Plugin(loader.load(self.entry("slasher"))).universe(self.root).agent("worker")
            self.assertEqual(watcher.slash_commands().state, "ok")
            with self.assertRaises(api.AccessDenied):
                watcher.slash("compact")
        with self.subTest("without agents.control"):
            self.write_plugin("lister", {**SLASHER, "permissions": ["agents.read"]},
                              {"plugin.py": "def register(mv): pass\n"})
            self.enable("lister")
            lister = api.Plugin(loader.load(self.entry("lister"))).universe(self.root).agent("worker")
            with self.assertRaisesRegex(api.Error, "agents.control"):
                lister.slash("compact")
        with self.subTest("a call that can't be one"):
            for bad in ({"command": ""}, {"command": "compact", "args": 3}, {"command": "compact", "timeout": -1},
                        {"command": "compact", "wait": "yes"}):
                with self.assertRaises(api.Error):
                    self.worker.slash(**bad)
        self.assertEqual(len(fake.requests), 3)
        with self.subTest("a harness that doesn't run its commands for mv2"):
            codex = self.agent("coder", harness="codex")
            self.assertTrue(codex.is_dir())
            other = api.Plugin(loader.load(self.entry("lister"))).universe(self.root).agent("coder")
            self.assertEqual(other.slash_commands().state, "unavailable")
