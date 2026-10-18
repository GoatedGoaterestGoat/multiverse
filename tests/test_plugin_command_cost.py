"""Built-in commands and core's hot paths never pay for plugins: no trust check, no plugin lookup or import."""
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from multiverse import cli, plugins
from multiverse.plugins import events, harness, loader, trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write
from multiverse.universe import agents, mailbox

HELLO = """
from pathlib import Path
import os
Path(os.environ["HELLO_MARKER"]).write_text("imported")

def register(mv):
    mv.cli.command("hello", lambda parser: None, lambda root=None: {"greeting": "hello"})
"""


class HotPathTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        # A trusted universe plugin that subscribes to messages: the kind whose trust check walks every file.
        self.directory = self.write_plugin("big", {"extends": ["events"], "events": ["message.*"]},
                                           {"data/a.txt": "a"}, where=self.root / "plugins")
        trust.trust(self.directory)
        # A plugin with code, a command, agent events and a pre-plugin settings file it would convert: none of
        # which may import its code on a hot path.
        self.marker = self.work / "imported"
        os.environ["HELLO_MARKER"] = str(self.marker)
        self.write_plugin("hello", {"module": "plugin", "extends": ["cli", "events"], "events": ["agent.*"],
                                    "commands": {"hello": "Say hello"}, "settings": {"version": 1,
                                                                                     "legacy": "hello.json"}},
                          {"plugin.py": HELLO})
        loader.reset()
        self.walks = mock.patch.object(trust, "signature", side_effect=AssertionError("trust check on a built-in path"))
        identity = str(uuid4())
        self.agent_dir = self.root / "state" / "agents" / identity
        write(self.agent_dir / "properties.json", {"id": identity, "name": "worker", "harness": "codex", "task": "",
                                                   "universe_id": read(self.root / "universe.json")["id"],
                                                   "workspace": str(self.work)})
        write(self.agent_dir / "runtime.json", {"launch_id": "L1", "state": "running", "status": "idle"})

    def recorded(self, kind):
        return [event for event in events.read(self.root, 0)[0] if event["type"] == kind]

    def test_hot_paths_never_touch_plugins(self):
        """Built-in commands look up no plugin commands, and runtime updates, message appends, event recording, the
        first-use check and usage do no trust walk and import no plugin code, nothing writes a journal nobody
        reads, and command help lists plugin commands while importing neither their code nor textual; a break goes
        unnoticed because output is identical while every agent call slows or untrusted code is imported."""
        with self.subTest("nobody subscribes: no journal"):
            self.assertIsNone(events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": "idle"}))
            agents.update_runtime(self.agent_dir, "L1", status="working")
            mailbox.append_message(self.agent_dir, "hi", {}, "inbox-only")
            self.assertFalse(events.directory(self.root).exists())
        self.enable("big")
        self.enable("hello")
        loader.reset()
        with self.subTest("built-in commands"):
            with mock.patch.object(cli, "plugin_commands", side_effect=AssertionError("plugin lookup")):
                for argv in (["-u", str(self.root), "list"], ["send", "x", "--text", "hi"],
                             ["status", "--phase", "done"], []):
                    cli.parser(argv).parse_known_args(argv)
            with mock.patch.object(cli, "plugin_commands", return_value=(self.root, {})) as looked:
                cli.parser(["-h"])  # the top-level help lists plugin commands
                cli.parser(["no-such-command"])  # a name mv2 does not have may be a plugin's
            self.assertEqual(looked.call_count, 2)
        with self.subTest("recording events, the first-use check and usage"), self.walks:
            self.assertIsNotNone(events.emit(self.root, "message.saved", {"id": "a", "name": "w"}, data={"length": 3}))
            self.assertIsNone(events.emit(self.root, "universe.created"))  # nobody subscribes
            self.assertEqual(plugins.migrate_on_first_use(self.root), [])  # nothing to convert
            self.assertFalse((self.root / "state" / "plugins" / plugins.MIGRATE_LOCK).exists())
            info = harness.SessionInfo(self.root, "a", "claude", "claude")
            value = {"state": "ok", "value": {"context": {"tokens": 5}}}
            with mock.patch.object(loader, "active", side_effect=AssertionError("loaded plugins")):
                self.assertIs(harness.adjust_usage(info, value), value)
        with self.subTest("a status change is recorded once, without the subscriber's code"):
            agents.update_runtime(self.agent_dir, "L1", status="waiting")
            agents.update_runtime(self.agent_dir, "L1", status="waiting")  # no change
            agents.update_runtime(self.agent_dir, "L1", usage={"x": 1})  # not a status
            [event] = self.recorded("agent.status")
            self.assertEqual((event["data"], event["launch"], event["agent"]["name"]),
                             ({"from": "working", "to": "waiting"}, "L1", "worker"))
        with self.subTest("command help"):
            self.assertIn("Say hello (plugin hello)", cli.parser(["-u", str(self.root), "--help"]).format_help())
            code = ("import sys, multiverse.cli, multiverse.plugins, multiverse.plugins.api; "
                    "multiverse.cli.parser(['-u', sys.argv[1], '--help']); "
                    "print('multiverse.plugins.ui' in sys.modules, 'textual' in sys.modules, "
                    "any(m.startswith('mv2_plugins') for m in sys.modules))")
            probe = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, text=True,
                                   env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                                   timeout=60)
            self.assertEqual(probe.stdout.split(), ["False", "False", "False"], probe.stderr)
        self.assertFalse(loader._loaded)
        self.assertFalse(self.marker.exists())
        loader.load(self.entry("hello"))  # what the marker would show: the plugin's code imported
        self.assertTrue(self.marker.exists())
