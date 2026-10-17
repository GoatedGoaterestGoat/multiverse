"""Plugins at launch, in capabilities and in usage: what lifecycle, the runner and native record and report."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from multiverse import harnesses
from multiverse.harnesses.types import BridgeUnavailable, CommandSpec, NativeResult
from multiverse.plugins import config, loader
from multiverse.runtime import lifecycle, native, rpc, runner
from multiverse.store import read, write
from multiverse.universe import agents
from test_native_backends import NativeBackendTest

PLUGIN = """
from multiverse.plugins import api

SEEN = []  # the SessionInfo each usage adjustment was given

def register(mv):
    mv.capabilities.add("guarding", lambda info: api.Capability("supported", mechanism="guard-" + info.harness))
    mv.capabilities.add("spawn", lambda info: api.Capability("unavailable", "a plugin may not answer this"))
    @mv.usage.adjust
    def adjust(info, usage):
        SEEN.append(info)
        if usage.get("total_tokens") == 13:
            raise RuntimeError("unlucky")
        return {**usage, "adjusted_by": "guard"}
"""
RECORD = {"plugins": {"guard": {"version": "0.1.0", "state": "loaded", "added": ["environment"]}},
          "status_items": [{"name": "trees", "plugin": "guard", "claude": True, "tmux": False}]}
ITEMS_ONLY = {"plugins": {}, "status_items": [{"name": "trees", "plugin": "guard", "claude": True, "tmux": False}]}
EMPTY = {"plugins": {}, "status_items": []}


class PluginLaunchTest(NativeBackendTest):
    """The native backends fixture, with a plugin that declares a status item and adds capabilities and usage."""

    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="mv2-plugin-home-", dir="/tmp")
        self.addCleanup(home.cleanup)
        plugins = Path(home.name) / "plugins" / "guard"
        plugins.mkdir(parents=True)
        (plugins / "plugin.json").write_text(json.dumps({
            "schema": 1, "name": "guard", "version": "0.1.0", "api": "1.1", "module": "plugin",
            "extends": ["statusline", "capabilities", "usage", "events"], "statusline": {"trees": {"claude": True}},
            "events": ["agent.*"]}))
        (plugins / "plugin.py").write_text(PLUGIN)
        environment = {key: value for key, value in os.environ.items() if key != "MV2_PLUGINS"}
        environment.update(HOME=home.name, MV2_PLUGIN_PATH=str(plugins.parent))
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        loader.reset()
        self.addCleanup(loader.reset)
        super().setUp()
        config.set_enabled(self.root, "guard", True)
        # The base template's compaction.json turns the bundled compaction plugin on; these tests are about guard alone.
        config.set_enabled(self.root, "compaction", False)

    def launched(self, name, command=None, fallback=None):
        original = self.adapter.command
        def prepared(context):
            result = original(context)
            if command is not None:
                result = replace(result, plugins=command)
            if fallback is not None:
                result = replace(result, fallback=fallback)
            return result
        with patch.object(self.adapter, "command", side_effect=prepared):
            result = lifecycle.start(self.root, name=name, harness="service-test")
        self.addCleanup(shutil.rmtree, Path("/tmp") / ("mv2-native-" + result["runtime"]["launch_id"]), True)
        directory = agents.directory(self.root, result["id"])
        request = read(directory / "launches" / result["runtime"]["launch_id"] / "request.json")
        return agents.runtime(directory), request

    def test_an_adapter_that_applies_no_plugins_still_records_their_status_items(self):
        facts, request = self.launched("plain-record", fallback=CommandSpec((sys.executable, "-c", "pass")))
        self.assertEqual(facts["plugins"], ITEMS_ONLY)
        self.assertEqual(request["fallback"]["plugins"], ITEMS_ONLY)

    def test_an_adapters_plugin_record_is_kept_and_its_fallback_has_its_own(self):
        fallback = CommandSpec((sys.executable, "-c", "pass"), plugins=EMPTY)
        facts, request = self.launched("applied-record", RECORD, fallback)
        self.assertEqual(facts["plugins"], RECORD)
        self.assertEqual(request["fallback"]["plugins"], EMPTY)

    def test_preparation_fallback_records_the_fallback_plan(self):
        fallback = CommandSpec((sys.executable, "-c", "pass"), plugins={"plugins": {"guard": {"state": "skipped"}},
                                                                       "status_items": []})
        with patch.object(native, "start_shared_service", side_effect=BridgeUnavailable("no daemon")):
            facts, _ = self.launched("prepared-fallback", RECORD, fallback)
        self.assertEqual(facts["plugins"], {"plugins": {"guard": {"state": "skipped"}}, "status_items": []})
        self.assertEqual(facts["native_mode"]["fallback_reason"], "no daemon")

    def test_runner_fallback_records_the_fallback_plan(self):
        original = read(self.directory / "launches" / self.launch / "request.json")
        for recorded, expected in ((ITEMS_ONLY, ITEMS_ONLY), (None, EMPTY)):
            with self.subTest(recorded=recorded):
                write(self.directory / "runtime.json", {**self.facts, "plugins": RECORD})
                write(self.directory / "launches" / self.launch / "request.json", original)
                launch, proxy = self.fallback_fixture(False, False)
                request = read(launch / "request.json")
                request["fallback"]["plugins"] = recorded
                write(launch / "request.json", request)
                process = Mock(pid=123)
                process.poll.return_value = process.wait.return_value = 0
                with patch.object(runner.sys, "argv", ["runner", str(launch)]), \
                     patch.object(rpc, "LaunchProxy", return_value=proxy), \
                     patch.object(runner, "process_started", return_value=None), \
                     patch.object(runner.subprocess, "Popen", return_value=process), \
                     patch("multiverse.terminal.tmux.owned", return_value=(self.backend, "%1")), \
                     patch.object(runner, "launch_diagnostic", return_value={}):
                    self.assertEqual(runner.main(), 0)
                self.assertEqual(agents.runtime(self.directory)["plugins"], expected)

    def test_capabilities_add_plugin_keys_and_core_keys_win(self):
        found = native.capabilities(self.root, self.result["id"])
        self.assertEqual(found["guarding"], {"state": "supported", "reason": "", "mechanism": "guard-service-test",
                                             "required": False})
        self.assertEqual(found["spawn"]["state"], "supported")
        with patch.object(loader, "active", side_effect=RuntimeError("plugins broke")):
            self.assertNotIn("guarding", native.capabilities(self.root, self.result["id"]))

    def test_usage_is_adjusted_only_when_ok_and_never_lost(self):
        measured = {"native_session_id": "n", "total_tokens": 5}
        with patch.object(self.adapter, "get_usage", create=True, return_value=NativeResult("ok", value=measured)):
            self.assertEqual(native.operation(self.root, self.result["id"], "get_usage")["value"],
                             {**measured, "adjusted_by": "guard"})
            self.assertEqual(native.usage_snapshot(self.root, self.properties, self.facts)["value"],
                             {**measured, "adjusted_by": "guard"})
        unlucky = {"native_session_id": "n", "total_tokens": 13}
        with patch.object(self.adapter, "get_usage", create=True, return_value=NativeResult("ok", value=unlucky)):
            self.assertEqual(native.operation(self.root, self.result["id"], "get_usage")["value"], unlucky)
        for state in ("unavailable", "error"):
            result = NativeResult(state, "not measured", value=measured)
            with patch.object(self.adapter, "get_usage", create=True, return_value=result):
                self.assertEqual(native.operation(self.root, self.result["id"], "get_usage"), result.to_dict())
        with patch.object(self.adapter, "get_usage", create=True, return_value=NativeResult("ok", value=measured)), \
             patch.object(loader, "active", side_effect=RuntimeError("plugins broke")):
            self.assertEqual(native.operation(self.root, self.result["id"], "get_usage")["value"], measured)


class TargetEnvironmentTest(PluginLaunchTest):
    """The launch's own identity reaches the adapter's first preparation, never the parent's."""

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

    def test_first_and_final_preparation_see_the_target_identity(self):
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

    def test_a_plain_command_is_prepared_once_with_the_target_identity(self):
        def plain(context):
            return CommandSpec((sys.executable, "-c", "pass"))
        with patch.object(self.adapter, "command", side_effect=plain):
            result, contexts = self.contexts(name="target-plain", harness="service-test")
        [only] = contexts
        for key, value in self.expected(result).items():
            self.assertEqual(only.environment[key], value, key)
        self.assertNotIn("MV2_TMUX_SOCKET", only.environment)


class PluginRecordTest(PluginLaunchTest):
    def test_malformed_records_become_the_empty_record(self):
        for record in ({"bad": object()}, ["not", "a", "record"], {"plugins": [], "status_items": []}, {"plugins": {}}):
            with self.subTest(record=record):
                fallback = CommandSpec((sys.executable, "-c", "pass"), plugins=record)
                facts, request = self.launched(f"malformed-{len(str(record))}", record, fallback)
                self.assertEqual(facts["plugins"], EMPTY)
                self.assertEqual(request["fallback"]["plugins"], EMPTY)

    def agent_events(self, agent_id):
        from multiverse.plugins import events
        return [event for event in events.read(self.root, 0)[0] if (event.get("agent") or {}).get("id") == agent_id]

    def test_created_then_launched_before_the_runner_is_released(self):
        facts, _ = self.launched("evented")
        directory = next(path.parent for path in (self.root / "state/agents").glob("*/properties.json")
                         if read(path)["name"] == "evented")
        found = self.agent_events(directory.name)
        self.assertEqual([event["type"] for event in found], ["agent.created", "agent.launched"])
        self.assertEqual(found[1]["launch"], facts["launch_id"])
        self.assertEqual(found[1]["data"], {"mode": "spawn", "native_mode": None, "plugins": []})
        write(directory / "runtime.json", {**agents.runtime(directory), "state": "stopped"})
        with patch.object(lifecycle, "observe", return_value={"live": "dormant"}):
            resumed = lifecycle.start(self.root, resume=directory.name)
        self.addCleanup(shutil.rmtree, Path("/tmp") / ("mv2-native-" + resumed["runtime"]["launch_id"]), True)
        latest = self.agent_events(directory.name)[-1]
        self.assertEqual((latest["type"], latest["data"]["mode"], latest["launch"]),
                         ("agent.launched", "resume", resumed["runtime"]["launch_id"]))

    def test_the_runner_records_one_exit(self):
        launch, proxy = self.fallback_fixture(False, False)
        process = Mock(pid=123)
        process.poll.return_value = process.wait.return_value = 0
        with patch.object(runner.sys, "argv", ["runner", str(launch)]), \
             patch.object(rpc, "LaunchProxy", return_value=proxy), \
             patch.object(runner, "process_started", return_value=None), \
             patch.object(runner.subprocess, "Popen", return_value=process), \
             patch("multiverse.terminal.tmux.owned", return_value=(self.backend, "%1")), \
             patch.object(runner, "launch_diagnostic", return_value={}):
            self.assertEqual(runner.main(), 0)
        exits = [event for event in self.agent_events(self.result["id"]) if event["type"] == "agent.exited"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["data"], {"state": "stopped", "exit_code": 0, "reason": None})
        self.assertEqual(exits[0]["launch"], self.launch)

    def test_a_launch_that_fails_with_an_exception_records_its_exit_once(self):
        launch, proxy = self.fallback_fixture(False, False)
        with patch.object(runner.sys, "argv", ["runner", str(launch)]), \
             patch.object(rpc, "LaunchProxy", return_value=proxy), \
             patch.object(runner.subprocess, "Popen", side_effect=OSError("no such harness")), \
             patch("multiverse.terminal.tmux.owned", return_value=(self.backend, "%1")), \
             patch.object(runner, "launch_diagnostic", return_value={}):
            self.assertEqual(runner.main(), 1)
        exits = [event for event in self.agent_events(self.result["id"]) if event["type"] == "agent.exited"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["data"]["state"], "failed")
        self.assertIsNone(exits[0]["data"]["exit_code"])
        self.assertIn("no such harness", exits[0]["data"]["reason"])
        self.assertEqual(exits[0]["launch"], self.launch)

    def test_an_exception_after_the_exit_was_recorded_records_no_second_one(self):
        launch, proxy = self.fallback_fixture(False, False)
        process = Mock(pid=123)
        process.poll.return_value = process.wait.return_value = 0
        real_event = lifecycle._event

        def then_fail(agent_dir, kind, *args, **kwargs):
            real_event(agent_dir, kind, *args, **kwargs)
            if kind == "agent.exited":
                raise RuntimeError("cleanup failed after the exit was recorded")
        with patch.object(runner.sys, "argv", ["runner", str(launch)]), \
             patch.object(rpc, "LaunchProxy", return_value=proxy), \
             patch.object(runner, "process_started", return_value=None), \
             patch.object(runner.subprocess, "Popen", return_value=process), \
             patch("multiverse.terminal.tmux.owned", return_value=(self.backend, "%1")), \
             patch.object(runner, "launch_diagnostic", return_value={}), \
             patch.object(lifecycle, "_event", then_fail):
            runner.main()
        exits = [event for event in self.agent_events(self.result["id"]) if event["type"] == "agent.exited"]
        self.assertEqual(len(exits), 1)

    def test_usage_is_adjusted_with_the_launch_it_was_measured_in(self):
        measured = {"native_session_id": "n", "total_tokens": 5}
        old_launch = agents.runtime(self.directory)["launch_id"]
        write(self.directory / "runtime.json", {**agents.runtime(self.directory), "plugins": RECORD})

        def relaunched_meanwhile(session, executor=None):
            # A resume lands right after the measurement: a new launch with another plugins record.
            write(self.directory / "runtime.json", {**agents.runtime(self.directory), "launch_id": "newer",
                                                    "plugins": EMPTY})
            return NativeResult("ok", value=measured)
        with patch.object(self.adapter, "get_usage", create=True, side_effect=relaunched_meanwhile):
            native.operation(self.root, self.result["id"], "get_usage")
        seen = next(loaded for loaded in loader._loaded.values() if getattr(loaded, "name", None) == "guard").module.SEEN
        self.assertEqual((seen[-1].launch_id, seen[-1].plugins), (old_launch, RECORD))


def load_tests(test_loader, tests, pattern):
    """Only this module's own tests: the imported fixture's tests run in test_native_backends."""
    suite = unittest.TestSuite()
    for case in (PluginLaunchTest, PluginRecordTest, TargetEnvironmentTest):
        for name in test_loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite
