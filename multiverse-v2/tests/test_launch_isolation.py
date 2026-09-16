"""Offline regressions for launch environment and first-inbox checkpoints."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import agents, runner, universe
from multiverse.cli import parser, run
from multiverse.store import ack, append_message, inbox, read, write


class LaunchIsolationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.root = universe.create(self.work / "universe", "test", workdir=self.work)

    def test_empty_inbox_ack_preserves_future_messages(self):
        agent = self.work / "empty-agent"
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    (agent / "inbox.jsonl").touch()
                batch = inbox(agent)
                self.assertEqual(batch, {"messages": [], "cursor": 0})
                self.assertEqual(ack(agent, batch["cursor"]), {"cursor": 0})
                self.assertEqual(ack(agent, 0), {"cursor": 0})
                for invalid in (-1, 1):
                    with self.assertRaises(ValueError):
                        ack(agent, invalid)
        message = append_message(agent, "after empty checkpoint", {})
        self.assertEqual(inbox(agent)["messages"], [message])
        ack(agent, inbox(agent)["cursor"])
        self.assertEqual(inbox(agent)["messages"], [])

    def test_ack_unknown_agent_does_not_create_state(self):
        before = list((self.root / "state" / "agents").iterdir())
        args = parser().parse_args(["-u", str(self.root), "ack", "--agent", "missing", "--cursor", "0"])
        with self.assertRaisesRegex(ValueError, "Expected one agent"):
            run(args)
        self.assertEqual(list((self.root / "state" / "agents").iterdir()), before)

    def test_spawn_and_resume_publish_actual_socket_before_ready(self):
        terminal = {"socket": "/actual/tmux.sock", "session": "$1", "pane": "%1"}
        backend = Mock()
        backend.create.return_value = terminal
        with patch.object(agents, "Tmux", return_value=backend) as tmux, \
                patch.object(agents.shutil, "which", return_value="/fake/executable"), \
                patch.object(agents, "observe", return_value={"live": "stopped"}), \
                patch.dict(os.environ, {"MV2_TMUX_SOCKET": "/inherited/tmux.sock"}):
            first = agents.start(self.root, "test", "test", "pi", socket_path="/explicit/tmux.sock")
            directory = agents.directory(self.root, first["id"])
            for result in (first, agents.start(self.root, resume=first["id"])):
                launch = directory / "launches" / result["runtime"]["launch_id"]
                self.assertTrue((launch / "ready").exists())
                request = read(launch / "request.json")
                self.assertEqual(request["env"]["MV2_TMUX_SOCKET"], terminal["socket"])
            self.assertIn(unittest.mock.call("/explicit/tmux.sock"), tmux.call_args_list)
            self.assertIn(unittest.mock.call("/inherited/tmux.sock"), tmux.call_args_list)

    def test_runner_uses_request_identity_and_socket(self):
        agent = self.work / "agent"
        launch = agent / "launches" / "launch"
        request_env = {"MV2_CLI": "/launch/bin/mv2", "MV2_UNIVERSE": "/child",
                       "MV2_AGENT_ID": "child", "MV2_TMUX_SOCKET": "/actual/tmux.sock"}
        write(launch / "request.json", {"argv": ["fake"], "env": request_env,
                                        "unset_env": ["CODEX_THREAD_ID"]})
        (launch / "ready").touch()
        process = Mock(pid=123)
        process.wait.return_value = 0
        with patch.object(sys, "argv", ["runner", str(launch)]), \
                patch.dict(os.environ, {"MV2_AGENT_ID": "parent", "MV2_TMUX_SOCKET": "/stale",
                                        "MV2_STALE": "parent", "CODEX_THREAD_ID": "parent-session"}), \
                patch.object(runner, "update_runtime"), \
                patch.object(runner.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(runner.main(), 0)
        env = popen.call_args.kwargs["env"]
        self.assertEqual({key: value for key, value in env.items() if key.startswith("MV2_")}, request_env)
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertTrue(env["PATH"].startswith("/launch/bin" + os.pathsep))

    def test_failed_spawn_retry_keeps_records_and_explains_exact_addressing(self):
        backend = Mock()
        backend.create.side_effect = [RuntimeError("socket access denied"),
                                      {"socket": "/actual/tmux.sock", "session": "$1", "pane": "%1"}]
        with patch.object(agents, "Tmux", return_value=backend), \
                patch.object(agents.shutil, "which", return_value="/fake/executable"):
            with self.assertRaises(RuntimeError) as failure:
                agents.start(self.root, "same-name", "test", socket_path="/actual/tmux.sock")
            failed = agents.directory(self.root, "same-name")
            self.assertEqual(agents.runtime(failed)["state"], "failed")
            successful = agents.start(self.root, "same-name", "test", socket_path="/actual/tmux.sock")
        self.assertIn(failed.name, str(failure.exception))
        self.assertIn("unique name", str(failure.exception))
        self.assertEqual(agents.directory(self.root, failed.name), failed)
        self.assertEqual(agents.directory(self.root, successful["id"]).name, successful["id"])
        with self.assertRaises(ValueError) as ambiguous:
            agents.directory(self.root, "same-name")
        self.assertIn(failed.name, str(ambiguous.exception))
        self.assertIn(successful["id"], str(ambiguous.exception))
        self.assertIn("full UUID", str(ambiguous.exception))

    def test_stale_cli_callbacks_cannot_read_ack_bind_or_change_current_launch(self):
        agent = self.root / "state/agents" / str(uuid4())
        properties = {"id": agent.name, "name": "callback", "phase": "working"}
        runtime = {"launch_id": "current", "activity": "working"}
        write(agent / "properties.json", properties)
        write(agent / "runtime.json", runtime)
        message = append_message(agent, "for current launch", {})
        cursor = inbox(agent)["cursor"]
        actions = (["inbox"], ["ack", "--cursor", str(cursor)],
                   ["status", "--phase", "done"], ["runtime-update", "--activity", "idle"],
                   ["session-bind", "--session-id", str(uuid4())])
        for action in actions:
            with self.subTest(action=action[0]), patch.dict(os.environ, {"MV2_LAUNCH_ID": "old"}):
                args = parser().parse_args(["-u", str(self.root), *action, "--agent", agent.name])
                with self.assertRaisesRegex(ValueError, "Outdated launch"):
                    run(args)
        self.assertEqual(read(agent / "properties.json"), properties)
        self.assertEqual(read(agent / "runtime.json"), runtime)
        self.assertEqual(inbox(agent)["messages"], [message])
        self.assertFalse((agent / "inbox.cursor").exists())

    def test_all_bundled_adapters_clear_cross_harness_native_markers(self):
        for manifest in sorted(universe.TEMPLATES.glob("*/harnesses/*/harness.json")):
            with self.subTest(manifest=manifest.relative_to(universe.TEMPLATES)):
                spec = read(manifest)
                launch = self.work / "marker-test" / "launches" / "launch"
                identity = {"MV2_CLI": "/child/bin/mv2", "MV2_AGENT_ID": "child",
                            "MV2_LAUNCH_ID": "launch", "MV2_TMUX_SOCKET": "/actual/tmux.sock"}
                write(launch / "request.json", {"argv": ["fake"], "env": identity,
                                                "unset_env": spec.get("unset_env", [])})
                (launch / "ready").touch()
                process = Mock(pid=123)
                process.wait.return_value = 0
                with patch.object(sys, "argv", ["runner", str(launch)]), \
                        patch.dict(os.environ, {"CODEX_THREAD_ID": "parent-codex", "CLAUDECODE": "parent-claude"}, clear=True), \
                        patch.object(runner, "update_runtime"), \
                        patch.object(runner.subprocess, "Popen", return_value=process) as popen:
                    self.assertEqual(runner.main(), 0)
                env = popen.call_args.kwargs["env"]
                self.assertNotIn("CODEX_THREAD_ID", env)
                self.assertNotIn("CLAUDECODE", env)
                self.assertEqual({key: value for key, value in env.items() if key.startswith("MV2_")}, identity)


if __name__ == "__main__":
    unittest.main()
