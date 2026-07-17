from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src import ask_agents, sb_connector, server, sessions

sb_protocol = sb_connector.p


_FAKE_RUNTIME = r"""
#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ.get("ASK_TEST_LOG")


def log(value):
    if not log_path:
        return
    with open(log_path, "a") as fh:
        fh.write(json.dumps(value) + "\n")


log({"argv": sys.argv[1:], "caller_status": os.environ.get("MULTIVERSE_STATUS")})

if "app-server" not in sys.argv:
    print("Claude side answer")
    raise SystemExit(0)

mode = os.environ.get("ASK_TEST_MODE", "success")
fork_id = "ephemeral-fork-id"
answer_item = {
    "id": "answer-item",
    "type": "agentMessage",
    "phase": "final_answer",
    "text": "Codex side answer",
}

for raw in sys.stdin:
    message = json.loads(raw)
    log({"request": message})
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"id": message["id"], "result": {"userAgent": "fake"}}), flush=True)
    elif method == "thread/fork":
        if mode == "timeout":
            continue
        if mode == "fork_error":
            print(json.dumps({"id": message["id"], "error": {"message": "fork failed"}}), flush=True)
            continue
        print(json.dumps({
            "id": message["id"],
            "result": {"thread": {
                "id": fork_id,
                "ephemeral": mode != "persistent",
            }},
        }), flush=True)
    elif method == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-id"}}}), flush=True)
        if mode == "tool":
            print(json.dumps({
                "method": "item/started",
                "params": {
                    "threadId": fork_id,
                    "turnId": "turn-id",
                    "item": {"id": "tool-id", "type": "commandExecution"},
                },
            }), flush=True)
            continue
        print(json.dumps({
            "method": "item/completed",
            "params": {
                "threadId": fork_id,
                "turnId": "turn-id",
                "item": answer_item,
            },
        }), flush=True)
        print(json.dumps({
            "method": "turn/completed",
            "params": {
                "threadId": fork_id,
                "turn": {
                    "id": "turn-id",
                    "status": "completed",
                    "items": [answer_item],
                },
            },
        }), flush=True)
"""


class CodexSideQuestionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cwd = str((self.root / "workspace").resolve())
        Path(self.cwd).mkdir()
        self.log_path = self.root / "requests.jsonl"
        self.fake_runtime = self.root / "fake-runtime"
        self.fake_runtime.write_text(textwrap.dedent(_FAKE_RUNTIME).lstrip())
        self.fake_runtime.chmod(0o755)
        self.env_patch = patch.dict(
            os.environ,
            {
                "MULTIVERSE_CODEX_BIN": str(self.fake_runtime),
                "MULTIVERSE_CLAUDE_BIN": str(self.fake_runtime),
                "MULTIVERSE_STATUS": str(self.root / "caller-status.json"),
                "MULTIVERSE_SESSION_NAME": "caller",
                "ASK_TEST_LOG": str(self.log_path),
                "ASK_TEST_MODE": "success",
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.projects_patch = patch.object(
            ask_agents, "CLAUDE_PROJECTS_DIR", self.root / "claude-projects"
        )
        self.projects_patch.start()
        self.addCleanup(self.projects_patch.stop)

    def _codex_target(self) -> ask_agents.AskTarget:
        return ask_agents.AskTarget(
            name="codex-agent",
            cwd=self.cwd,
            runtime=sessions.RUNTIME_CODEX,
            session_id="source-thread-id",
        )

    def _records(self) -> list[dict]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def test_ephemeral_fork_returns_final_answer_with_no_runtime_leak(self) -> None:
        result = ask_agents.ask_one("What did you finish?", self._codex_target())

        self.assertEqual(result.response, "Codex side answer")
        self.assertIsNone(result.error)
        self.assertEqual(result.fork_uuid, "ephemeral-fork-id")

        records = self._records()
        startup = records[0]
        self.assertIsNone(startup["caller_status"])
        argv = startup["argv"]
        for feature in ask_agents._CODEX_DISABLED_FEATURES:
            self.assertIn(feature, argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertIn("mcp_servers={}", argv)
        self.assertIn("apps._default.enabled=false", argv)

        requests = [record["request"] for record in records if "request" in record]
        initialize = next(req for req in requests if req.get("method") == "initialize")
        self.assertTrue(initialize["params"]["capabilities"]["experimentalApi"])
        fork = next(req for req in requests if req.get("method") == "thread/fork")
        params = fork["params"]
        self.assertEqual(params["threadId"], "source-thread-id")
        self.assertTrue(params["ephemeral"])
        self.assertTrue(params["excludeTurns"])
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["runtimeWorkspaceRoots"], [])
        profile = params["config"]["permissions"]["multiverse-side-question"]
        self.assertEqual(profile["filesystem"], {":root": "deny"})
        self.assertFalse(profile["network"]["enabled"])
        self.assertEqual(params["config"]["mcp_servers"], {})
        self.assertFalse(params["config"]["apps"]["_default"]["enabled"])

        turn = next(req for req in requests if req.get("method") == "turn/start")
        prompt = turn["params"]["input"][0]["text"]
        self.assertIn("You have NO tools available", prompt)
        self.assertTrue(prompt.endswith("What did you finish?"))

    def test_tool_attempt_fails_closed(self) -> None:
        with patch.dict(os.environ, {"ASK_TEST_MODE": "tool"}, clear=False):
            result = ask_agents.ask_one("Inspect a file", self._codex_target())

        self.assertIsNone(result.response)
        self.assertIn("disallowed action 'commandExecution'", result.error or "")

    def test_non_ephemeral_fork_is_rejected(self) -> None:
        with patch.dict(os.environ, {"ASK_TEST_MODE": "persistent"}, clear=False):
            result = ask_agents.ask_one("Answer this", self._codex_target())

        self.assertIsNone(result.response)
        self.assertEqual(result.error, "Codex thread/fork was not ephemeral")

    def test_timeout_terminates_the_app_server(self) -> None:
        with patch.dict(os.environ, {"ASK_TEST_MODE": "timeout"}, clear=False):
            result = ask_agents.ask_one(
                "Answer this", self._codex_target(), timeout=0.1
            )

        self.assertIsNone(result.response)
        self.assertIn("timed out after", result.error or "")
        self.assertLess(result.elapsed, 1.5)

    def test_mixed_runtime_ask_all_preserves_target_order(self) -> None:
        callbacks: list[tuple[str, bool]] = []
        targets = [
            ask_agents.AskTarget(
                name="claude-agent",
                cwd=self.cwd,
                claude_uuid="claude-source-id",
            ),
            self._codex_target(),
        ]

        results = ask_agents.ask_all(
            "Status?",
            targets,
            max_parallel=2,
            register_fork=lambda fork_id, active: callbacks.append((fork_id, active)),
        )

        self.assertEqual(
            [result.name for result in results], ["claude-agent", "codex-agent"]
        )
        self.assertEqual(
            [result.response for result in results],
            ["Claude side answer", "Codex side answer"],
        )
        self.assertEqual([active for _, active in callbacks], [True, False])


class AskAgentRuntimeRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cwd = str(Path(self.temp_dir.name).resolve())

    def test_mcp_ask_routes_codex_target_by_native_session_id(self) -> None:
        target = sessions.DashboardRow(
            name="codex-target",
            cwd=self.cwd,
            status="idle",
            status_ts=1,
            runtime=sessions.RUNTIME_CODEX,
            session_id="codex-thread-id",
        )
        completed = ask_agents.AskResult(
            name=target.name,
            response="The migration is complete.",
            error=None,
            fork_uuid="ephemeral-fork",
            elapsed=0.1,
        )
        with (
            patch.dict(
                os.environ,
                {
                    "MULTIVERSE_SESSION_CWD": self.cwd,
                    "MULTIVERSE_SESSION_NAME": "caller",
                    "MULTIVERSE_RUNTIME": sessions.RUNTIME_CLAUDE,
                },
                clear=False,
            ),
            patch.object(server, "find_agent_by_name", return_value=target),
            patch.object(server, "ask_one", return_value=completed) as ask,
        ):
            result = server.ask_agent("codex-target", "Did you finish?")

        self.assertEqual(result, "The migration is complete.")
        routed = ask.call_args.kwargs["target"]
        self.assertEqual(routed.runtime, sessions.RUNTIME_CODEX)
        self.assertEqual(routed.native_session_id, "codex-thread-id")

    def test_mcp_ask_supports_the_mixed_runtime_matrix(self) -> None:
        matrix = [
            (sessions.RUNTIME_CODEX, sessions.RUNTIME_CODEX),
            (sessions.RUNTIME_CODEX, sessions.RUNTIME_CLAUDE),
            (sessions.RUNTIME_CLAUDE, sessions.RUNTIME_CODEX),
        ]
        for caller_runtime, target_runtime in matrix:
            with self.subTest(
                caller_runtime=caller_runtime, target_runtime=target_runtime
            ):
                native_id = f"{target_runtime}-thread-id"
                target = sessions.DashboardRow(
                    name="target",
                    cwd=self.cwd,
                    status="idle",
                    status_ts=1,
                    runtime=target_runtime,
                    session_id=native_id,
                    claude_uuid=(
                        native_id if target_runtime == sessions.RUNTIME_CLAUDE else None
                    ),
                )
                completed = ask_agents.AskResult(
                    name="target",
                    response="answer",
                    error=None,
                    fork_uuid="fork",
                    elapsed=0.1,
                )
                with (
                    patch.dict(
                        os.environ,
                        {
                            "MULTIVERSE_SESSION_CWD": self.cwd,
                            "MULTIVERSE_SESSION_NAME": "caller",
                            "MULTIVERSE_RUNTIME": caller_runtime,
                        },
                        clear=False,
                    ),
                    patch.object(server, "find_agent_by_name", return_value=target),
                    patch.object(server, "ask_one", return_value=completed) as ask,
                ):
                    self.assertEqual(server.ask_agent("target", "Question?"), "answer")

                routed = ask.call_args.kwargs["target"]
                self.assertEqual(routed.runtime, target_runtime)
                self.assertEqual(routed.native_session_id, native_id)

    def test_cross_workspace_codex_question_uses_remote_cwd(self) -> None:
        remote = str((Path(self.temp_dir.name) / "remote").resolve())
        Path(remote).mkdir()
        target = sessions.DashboardRow(
            name="remote-codex",
            cwd=remote,
            status="dormant",
            status_ts=1,
            runtime=sessions.RUNTIME_CODEX,
            session_id="remote-thread-id",
        )
        completed = ask_agents.AskResult(
            name=target.name,
            response="remote answer",
            error=None,
            fork_uuid="fork",
            elapsed=0.1,
        )
        with (
            patch.dict(
                os.environ,
                {
                    "MULTIVERSE_SESSION_CWD": self.cwd,
                    "MULTIVERSE_SESSION_NAME": "caller",
                },
                clear=False,
            ),
            patch.object(server, "find_agent_by_name", return_value=target) as find,
            patch.object(server, "ask_one", return_value=completed) as ask,
        ):
            result = server.ask_agent(
                "remote-codex", "Question?", working_directory=remote
            )

        self.assertEqual(result, "remote answer")
        find.assert_called_once_with(remote, "remote-codex")
        self.assertEqual(ask.call_args.kwargs["target"].cwd, remote)


class SecondBrainCodexAskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cwd = str(Path(self.temp_dir.name).resolve())
        self.connector = object.__new__(sb_connector.SBConnector)
        self.connector.identity = SimpleNamespace(
            device_id="device-id", granted_workspaces=[self.cwd]
        )
        self.connector.channel = None
        self.connector._active_asks = 0
        self.connector._ask_max = 4

    def _codex_row(self) -> sessions.DashboardRow:
        return sessions.DashboardRow(
            name="codex-target",
            cwd=self.cwd,
            status="idle",
            status_ts=1,
            runtime=sessions.RUNTIME_CODEX,
            session_id="codex-thread-id",
        )

    async def test_first_connect_summary_includes_codex_targets(self) -> None:
        completed = ask_agents.AskResult(
            name="codex-target",
            response="Implemented the parser.",
            error=None,
            fork_uuid="ephemeral-fork",
            elapsed=0.1,
        )
        captured: list[ask_agents.AskTarget] = []

        def fake_ask_all(question, targets, timeout=60.0):
            captured.extend(targets)
            return [completed]

        with (
            patch.object(
                sessions, "list_named_agents_in_cwd", return_value=[self._codex_row()]
            ),
            patch.object(ask_agents, "ask_all", side_effect=fake_ask_all),
        ):
            summary = await self.connector._gather_workspace_summary(self.cwd)

        self.assertEqual(summary, {"codex-target": "Implemented the parser."})
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].runtime, sessions.RUNTIME_CODEX)
        self.assertEqual(captured[0].native_session_id, "codex-thread-id")

    async def test_remote_ask_query_routes_to_codex_side_fork(self) -> None:
        completed = ask_agents.AskResult(
            name="codex-target",
            response="The parser is complete.",
            error=None,
            fork_uuid="ephemeral-fork",
            elapsed=0.1,
        )
        ws = SimpleNamespace(send=AsyncMock())
        query = sb_protocol.Query(
            query_id="query-id",
            kind=sb_protocol.QueryKind.ASK_AGENT,
            workspace_id=f"device-id:{self.cwd}",
            params={"agent": "codex-target", "question": "Is it done?"},
        )
        with (
            patch.object(
                sessions, "find_agent_by_name", return_value=self._codex_row()
            ),
            patch.object(ask_agents, "ask_one", return_value=completed) as ask,
        ):
            await self.connector._handle_ask_query(ws, query)

        routed = ask.call_args.kwargs["target"]
        self.assertEqual(routed.runtime, sessions.RUNTIME_CODEX)
        self.assertEqual(routed.native_session_id, "codex-thread-id")
        wire = sb_protocol.decode(ws.send.await_args.args[0])
        self.assertEqual(wire.status, sb_protocol.ResultStatus.OK)
        self.assertEqual(wire.result, {"answer": "The parser is complete."})


class CodexAskAgentMcpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_server_asks_a_discovered_codex_thread(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        wrapper_root = repo_root / "multiverse-plugin"
        mcp_server = (
            repo_root / "plugins" / "multiverse-codex" / "scripts" / "mcp-server"
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            cwd = str((root / "workspace").resolve())
            Path(cwd).mkdir()
            rollout_dir = codex_home / "sessions" / "2026" / "07" / "16"
            rollout_dir.mkdir(parents=True)
            (codex_home / "session_index.jsonl").write_text(
                json.dumps({"id": "target-thread", "thread_name": "codex-target"})
                + "\n"
            )
            (rollout_dir / "rollout-target-thread.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "target-thread", "cwd": cwd},
                    }
                )
                + "\n"
            )
            fake_runtime = root / "fake-runtime"
            fake_runtime.write_text(textwrap.dedent(_FAKE_RUNTIME).lstrip())
            fake_runtime.chmod(0o755)
            ask_log = root / "ask.jsonl"
            env = {
                **os.environ,
                "CODEX_HOME": str(codex_home),
                "MULTIVERSE_ROOT": str(root / "state"),
                "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
                "MULTIVERSE_SESSION_CWD": cwd,
                "MULTIVERSE_SESSION_NAME": "codex-caller",
                "MULTIVERSE_PLUGIN_ROOT": str(wrapper_root),
                "MULTIVERSE_CODEX_BIN": str(fake_runtime),
                "ASK_TEST_LOG": str(ask_log),
                "ASK_TEST_MODE": "success",
                "UV_CACHE_DIR": "/private/tmp/multiverse-uv-cache",
            }
            params = StdioServerParameters(
                command=str(mcp_server), env=env, cwd=str(repo_root)
            )

            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    response = await session.call_tool(
                        "ask_agent",
                        {
                            "target_name": "codex-target",
                            "question": "What did you finish?",
                        },
                    )

            self.assertFalse(response.isError)
            self.assertEqual(response.content[0].text, "Codex side answer")
            requests = [
                record["request"]
                for record in map(json.loads, ask_log.read_text().splitlines())
                if "request" in record
            ]
            fork = next(
                request
                for request in requests
                if request.get("method") == "thread/fork"
            )
            self.assertEqual(fork["params"]["threadId"], "target-thread")
            self.assertTrue(fork["params"]["ephemeral"])


if __name__ == "__main__":
    unittest.main()
