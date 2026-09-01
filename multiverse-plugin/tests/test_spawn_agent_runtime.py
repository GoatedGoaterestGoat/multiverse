from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src import server, sessions, spawn_requests


class SpawnRequestRuntimeProtocolTests(unittest.TestCase):
    def test_request_and_resolution_preserve_runtime_choices(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            spawn_requests, "SPAWN_REQUESTS_DIR", Path(raw)
        ), patch.object(
            spawn_requests.sb_notify, "record_spawn_request"
        ), patch.object(
            spawn_requests.sb_notify, "record_spawn_decision"
        ):
            request_path = spawn_requests.write_spawn_request(
                req_id="request-id",
                from_name="caller",
                cwd="/workspace",
                instruction="Do the work.",
                default_name="child",
                caller_tmux_session="multiverse-caller",
                caller_runtime=sessions.RUNTIME_CLAUDE,
            )
            self.assertEqual(
                json.loads(request_path.read_text())["caller_runtime"],
                sessions.RUNTIME_CLAUDE,
            )

            self.assertTrue(
                spawn_requests.write_resolution(
                    "request-id",
                    status="accepted",
                    name="child",
                    runtime=sessions.RUNTIME_CODEX,
                )
            )
            self.assertEqual(
                spawn_requests.read_resolution("request-id")["runtime"],
                sessions.RUNTIME_CODEX,
            )

    def test_pi_spawn_activity_emits_secondbrain_events(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            spawn_requests, "SPAWN_REQUESTS_DIR", Path(raw)
        ), patch.object(
            spawn_requests.sb_notify, "record_spawn_request"
        ) as record_request, patch.object(
            spawn_requests.sb_notify, "record_spawn_decision"
        ) as record_decision:
            spawn_requests.write_spawn_request(
                req_id="pi-request",
                from_name="pi-caller",
                cwd="/workspace",
                instruction="Do the work.",
                default_name="child",
                caller_tmux_session="multiverse-pi",
                caller_runtime=sessions.RUNTIME_PI,
            )
            self.assertTrue(
                spawn_requests.write_resolution(
                    "pi-request", status="accepted", name="child",
                    runtime=sessions.RUNTIME_PI,
                )
            )

        record_request.assert_called_once()
        record_decision.assert_called_once()

    def test_dead_caller_request_is_deleted_as_an_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            spawn_requests, "SPAWN_REQUESTS_DIR", Path(raw)
        ), patch.object(
            spawn_requests.sb_notify, "record_spawn_request"
        ), patch.object(
            spawn_requests.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1),
        ):
            request_path = spawn_requests.write_spawn_request(
                req_id="orphan-id",
                from_name="caller",
                cwd="/workspace",
                instruction="No caller remains.",
                default_name="orphan-child",
                caller_tmux_session="multiverse-dead-caller",
                caller_runtime=sessions.RUNTIME_CODEX,
            )

            self.assertEqual(
                spawn_requests.list_pending_requests("/workspace"), []
            )
            self.assertFalse(request_path.exists())


class SpawnAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_selected_codex_runtime_starts_named_prompted_child(
        self,
    ) -> None:
        instruction = "Inspect the failure.\nReturn a concise diagnosis."
        resolution = {
            "status": "accepted",
            "name": "codex-child",
            "runtime": sessions.RUNTIME_CODEX,
        }
        env = {
            "MULTIVERSE_SESSION_CWD": "/workspace",
            "MULTIVERSE_SESSION_NAME": "claude-caller",
            "MULTIVERSE_SESSION_ID": "caller-id",
            "MULTIVERSE_RUNTIME": sessions.RUNTIME_CLAUDE,
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(server, "all_session_names_in_cwd", return_value=set()),
            patch.object(server, "new_request_id", return_value="request-id"),
            patch.object(server, "write_spawn_request") as write_request,
            patch.object(server, "read_resolution", return_value=resolution),
            patch.object(server, "cleanup_request") as cleanup,
            patch.object(server, "create_session") as create,
            patch.object(server.subprocess, "run"),
        ):
            result = await server.spawn_agent(
                instruction=instruction,
                name="codex-child",
            )

        self.assertEqual(result, "Spawned agent with name 'codex-child'.")
        write_request.assert_called_once_with(
            req_id="request-id",
            from_name="claude-caller",
            cwd="/workspace",
            instruction=instruction,
            default_name="codex-child",
            caller_tmux_session="multiverse-caller-id",
            caller_runtime=sessions.RUNTIME_CLAUDE,
        )
        create.assert_called_once_with(
            cwd="/workspace",
            claude_args=[],
            name="codex-child",
            runtime=sessions.RUNTIME_CODEX,
            initial_prompt=instruction,
        )
        cleanup.assert_called_once_with("request-id")

    async def test_user_selected_claude_runtime_overrides_codex_caller(
        self,
    ) -> None:
        instruction = "Handle this in a fresh Claude session."
        resolution = {
            "status": "accepted",
            "name": "claude-child",
            "runtime": sessions.RUNTIME_CLAUDE,
        }
        env = {
            "MULTIVERSE_SESSION_CWD": "/workspace",
            "MULTIVERSE_SESSION_NAME": "codex-caller",
            "MULTIVERSE_SESSION_ID": "caller-id",
            "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(server, "all_session_names_in_cwd", return_value=set()),
            patch.object(server, "new_request_id", return_value="request-id"),
            patch.object(server, "write_spawn_request"),
            patch.object(server, "read_resolution", return_value=resolution),
            patch.object(server, "cleanup_request"),
            patch.object(server, "create_session") as create,
            patch.object(server.subprocess, "run"),
        ):
            result = await server.spawn_agent(
                instruction=instruction,
                name="claude-child",
            )

        self.assertEqual(result, "Spawned agent with name 'claude-child'.")
        create.assert_called_once_with(
            cwd="/workspace",
            claude_args=[],
            name="claude-child",
            runtime=sessions.RUNTIME_CLAUDE,
            initial_prompt=instruction,
        )

    async def test_rejected_spawn_cleans_up_without_creating_session(
        self,
    ) -> None:
        env = {
            "MULTIVERSE_SESSION_CWD": "/workspace",
            "MULTIVERSE_SESSION_NAME": "codex-caller",
            "MULTIVERSE_SESSION_ID": "caller-id",
            "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(server, "all_session_names_in_cwd", return_value=set()),
            patch.object(server, "new_request_id", return_value="request-id"),
            patch.object(server, "write_spawn_request"),
            patch.object(
                server,
                "read_resolution",
                return_value={"status": "rejected", "reason": "cancelled"},
            ),
            patch.object(server, "cleanup_request") as cleanup,
            patch.object(server, "create_session") as create,
            patch.object(server.subprocess, "run"),
        ):
            result = await server.spawn_agent(
                instruction="Do not create this child.",
                name="rejected-child",
            )

        # "cancelled" is the internal marker for a bare Esc, not something a
        # person typed, so no quoted-message clause is added.
        self.assertEqual(
            result,
            "Spawn request rejected by the user\nNo subagent was created.",
        )
        create.assert_not_called()
        cleanup.assert_called_once_with("request-id")

    async def test_cancelled_tool_call_cleans_up_pending_request(self) -> None:
        env = {
            "MULTIVERSE_SESSION_CWD": "/workspace",
            "MULTIVERSE_SESSION_NAME": "codex-caller",
            "MULTIVERSE_SESSION_ID": "caller-id",
            "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(server, "all_session_names_in_cwd", return_value=set()),
            patch.object(server, "new_request_id", return_value="request-id"),
            patch.object(server, "write_spawn_request"),
            patch.object(server, "read_resolution", return_value=None),
            patch.object(
                server.asyncio,
                "sleep",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch.object(server, "cleanup_request") as cleanup,
            patch.object(server, "create_session") as create,
            patch.object(server.subprocess, "run"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await server.spawn_agent(
                    instruction="Cancel while awaiting approval.",
                    name="cancelled-child",
                )

        create.assert_not_called()
        cleanup.assert_called_once_with("request-id")

    async def test_spawn_agent_schema_has_no_runtime_argument(self) -> None:
        tools = await server.mcp.list_tools()
        spawn_tool = next(tool for tool in tools if tool.name == "spawn_agent")

        self.assertNotIn("runtime", spawn_tool.inputSchema["properties"])


if __name__ == "__main__":
    unittest.main()
