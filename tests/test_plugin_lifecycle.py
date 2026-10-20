"""Plugins at launch: the identity a launch's first preparation sees, and the exit the runner records for them."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from multiverse.harnesses.types import CommandSpec
from multiverse.plugins import config, loader
from multiverse.runtime import lifecycle, rpc, runner
from multiverse.store import read, write
from multiverse.universe import agents
from test_native_backends import NativeBackendTest


class PluginLaunchTest(NativeBackendTest):
    """The native backends fixture, with a plugin that subscribes to agent events."""

    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="mv2-plugin-home-", dir="/tmp")
        self.addCleanup(home.cleanup)
        plugins = Path(home.name) / "plugins" / "watcher"
        plugins.mkdir(parents=True)
        (plugins / "plugin.json").write_text(json.dumps({
            "schema": 1, "name": "watcher", "version": "0.1.0", "api": "1.1", "module": "plugin",
            "extends": ["events", "capabilities"], "events": ["agent.*"]}))
        (plugins / "plugin.py").write_text(  # one capability of its own, and one that claims a core name
            "from multiverse.plugins import api\n"
            "def register(mv):\n"
            "    mv.capabilities.add('watching', lambda info: api.Capability('supported', mechanism='watch'))\n"
            "    mv.capabilities.add('spawn', lambda info: api.Capability('unavailable', 'a plugin may not answer'))\n")
        environment = {key: value for key, value in os.environ.items() if key != "MV2_PLUGINS"}
        environment.update(HOME=home.name, MV2_PLUGIN_PATH=str(plugins.parent))
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        loader.reset()
        self.addCleanup(loader.reset)
        super().setUp()
        config.set_enabled(self.root, "watcher", True)
        # The base template's compaction.json turns the bundled compaction plugin on; these tests are about watcher.
        config.set_enabled(self.root, "compaction", False)

    def contexts(self, **start):
        self.adapter.calls.clear()
        parent = {"MV2_AGENT_ID": "parent-agent", "MV2_LAUNCH_ID": "parent-launch", "MV2_TMUX_SOCKET": "/parent.sock",
                  "MV2_UNIVERSE": "/parent", "AMBIENT_SETTING": "kept"}
        with patch.dict(os.environ, parent), patch.object(lifecycle, "_cli_path", return_value="/opt/mv2/bin/mv2"):
            result = lifecycle.start(self.root, socket_path="/parent.sock", **start)
        self.addCleanup(shutil.rmtree, Path("/tmp") / ("mv2-native-" + result["runtime"]["launch_id"]), True)
        return result, [call[-1] for call in self.adapter.calls if call[0] in ("spawn", "resume")]

    def expected(self, result):
        directory = agents.directory(self.root, result["id"])
        return {"MV2_UNIVERSE": str(self.root), "MV2_AGENT_ID": result["id"], "MV2_AGENT_DIR": str(directory),
                "MV2_LAUNCH_ID": result["runtime"]["launch_id"], "MV2_CLI": "/opt/mv2/bin/mv2", "MV2_PYTHON": sys.executable}

    def exits(self):
        from multiverse.plugins import events
        return [event for event in events.read(self.root, 0)[0]
                if (event.get("agent") or {}).get("id") == self.result["id"] and event["type"] == "agent.exited"]

    def run_once(self, popen, event=None):
        """The runner's exit code and the agent.exited events it recorded, run once on the agent's launch."""
        write(self.directory / "runtime.json", self.facts)
        write(self.directory / "launches" / self.launch / "request.json", self.request)
        launch, proxy = self.fallback_fixture(False, False)
        before = len(self.exits())
        with patch.object(runner.sys, "argv", ["runner", str(launch)]), \
             patch.object(rpc, "LaunchProxy", return_value=proxy), \
             patch.object(runner, "process_started", return_value=None), \
             patch.object(runner.subprocess, "Popen", **popen), \
             patch("multiverse.terminal.tmux.owned", return_value=(self.backend, "%1")), \
             patch.object(runner, "launch_diagnostic", return_value={}), \
             (patch.object(lifecycle, "_event", event) if event else contextlib.nullcontext()):
            code = runner.main()
        return code, self.exits()[before:]

    def test_first_preparation_sees_the_target_agents_identity(self):
        """The new agent's MV2_AGENT_ID, MV2_LAUNCH_ID, MV2_UNIVERSE and MV2_AGENT_DIR reach the adapter's first
        preparation on spawn, resume and a plain command, not the parent agent's; fragile because that preparation
        runs in the parent's process, which carries the parent's identity."""
        for start in ({"name": "target-spawn", "harness": "service-test"}, {"resume": self.result["id"]}):
            with self.subTest(start=start):
                if "resume" in start:
                    write(self.directory / "runtime.json", {**agents.runtime(self.directory), "state": "stopped"})
                result, contexts = self.contexts(**start)
                first, final = contexts[0], contexts[-1]
                self.assertEqual(len(contexts), 2)  # a service launch is prepared twice, as before
                for context in (first, final):
                    for key, value in self.expected(result).items():
                        self.assertEqual(context.environment[key], value, key)
                    self.assertTrue(context.environment["PATH"].startswith("/opt/mv2/bin" + os.pathsep))
                # The first preparation also sees the ambient environment; the final one, as before, the child's.
                self.assertEqual(first.environment["AMBIENT_SETTING"], "kept")
                self.assertEqual(final.environment["TEST_NATIVE_HOME"], str(self.work / "storage"))
                self.assertNotIn("MV2_TMUX_SOCKET", first.environment)
                self.assertEqual(final.environment["MV2_TMUX_SOCKET"], self.terminal["socket"])
                request = read(agents.directory(self.root, result["id"]) / "launches" / result["runtime"]["launch_id"]
                               / "request.json")
                for key, value in self.expected(result).items():
                    self.assertEqual(request["env"][key], value, key)
                self.assertEqual(request["env"]["MV2_TMUX_SOCKET"], self.terminal["socket"])
        with self.subTest("a plain command is prepared once"):
            def plain(context):
                return CommandSpec((sys.executable, "-c", "pass"))
            with patch.object(self.adapter, "command", side_effect=plain):
                result, contexts = self.contexts(name="target-plain", harness="service-test")
            [only] = contexts
            for key, value in self.expected(result).items():
                self.assertEqual(only.environment[key], value, key)
            self.assertNotIn("MV2_TMUX_SOCKET", only.environment)

    def test_core_capability_keys_win_over_a_plugins(self):
        """A plugin adds capabilities of its own, but one that claims a core name (spawn) never replaces core's
        answer; a break goes unnoticed: a plugin then quietly tells agents and the dashboard that mv2 can't spawn."""
        from multiverse.runtime import native
        found = native.capabilities(self.root, self.result["id"])
        self.assertEqual(found["watching"]["state"], "supported")
        self.assertEqual(found["watching"]["mechanism"], "watch")
        self.assertEqual(found["spawn"]["state"], "supported")  # core's, not the plugin's "unavailable"

    def test_inspect_says_why_a_plugin_was_left_out_of_the_launch(self):
        """The launch's recorded plugin problems (a plugin left out for unusable settings) reach mv2 inspect, once even
        where the fallback reason already carries them; a break goes unnoticed: a plain Claude launch has no fallback
        reason, so the agent runs without the plugin and only runtime.json says why."""
        from dataclasses import replace
        from multiverse.tools import inspect
        why = "plugin picky was left out: its settings cannot be used: level must be 1 or 2"
        original = self.adapter.command
        for name, fallback in (("plain", None), ("bridged", "bridge note; " + why)):
            with self.subTest(name):
                def command(context):
                    made = original(context)
                    mode = {**(made.native_mode or {}), "fallback_reason": fallback} if fallback else made.native_mode
                    return replace(made, native_mode=mode, plugins={
                        "plugins": {"picky": {"version": "1.0.0", "state": "skipped", "reason": why[29:]}},
                        "status_items": [], "problems": [why]})
                with patch.object(self.adapter, "command", side_effect=command):
                    result = lifecycle.start(self.root, name="picky-" + name, harness="service-test")
                self.addCleanup(shutil.rmtree, Path("/tmp") / ("mv2-native-" + result["runtime"]["launch_id"]), True)
                text = inspect.cli_run(self.root, agent=result["id"])
                self.assertEqual(text.count(why), 1, text)
                self.assertIn("Note: " + (fallback or why), text.splitlines())  # plain: its own line
                found = inspect.cli_run(self.root, agent=result["id"], json=True)
                self.assertEqual(found.get("notices", []).count(why), 0 if fallback else 1)

    def test_each_launch_records_one_exit(self):
        """Each launch records exactly one agent.exited with the right state however the runner ends; a break goes
        unnoticed because core never reads the journal, while plugins that react to exits fire twice or never."""
        self.request = read(self.directory / "launches" / self.launch / "request.json")
        process = Mock(pid=123)
        process.poll.return_value = process.wait.return_value = 0
        with self.subTest("the child exits"):
            code, exits = self.run_once({"return_value": process})
            self.assertEqual(code, 0)
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["data"], {"state": "stopped", "exit_code": 0, "reason": None})
            self.assertEqual(exits[0]["launch"], self.launch)
        with self.subTest("the harness never starts"):
            code, exits = self.run_once({"side_effect": OSError("no such harness")})
            self.assertEqual(code, 1)
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["data"]["state"], "failed")
            self.assertIsNone(exits[0]["data"]["exit_code"])
            self.assertIn("no such harness", exits[0]["data"]["reason"])
            self.assertEqual(exits[0]["launch"], self.launch)
        with self.subTest("an exception after the exit was recorded"):
            real_event = lifecycle._event

            def then_fail(agent_dir, kind, *args, **kwargs):
                real_event(agent_dir, kind, *args, **kwargs)
                if kind == "agent.exited":
                    raise RuntimeError("cleanup failed after the exit was recorded")
            _, exits = self.run_once({"return_value": process}, then_fail)
            self.assertEqual(len(exits), 1)


def load_tests(test_loader, tests, pattern):
    """Only this module's own tests: the imported fixture's tests run in test_native_backends."""
    suite = unittest.TestSuite()
    for name in test_loader.getTestCaseNames(PluginLaunchTest):
        if name in PluginLaunchTest.__dict__:
            suite.addTest(PluginLaunchTest(name))
    return suite
