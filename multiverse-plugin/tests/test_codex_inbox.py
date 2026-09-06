from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src import inbox, server


class CodexCheckInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cwd = str(self.root / "workspace")
        Path(self.cwd).mkdir()
        self.env = patch.dict(
            os.environ,
            {
                "MULTIVERSE_ROOT": str(self.root / "state"),
                "MULTIVERSE_RUNTIME": "codex",
                "MULTIVERSE_SESSION_CWD": self.cwd,
                "MULTIVERSE_SESSION_NAME": "codex-agent",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.inbox_dir_patch = patch.object(
            inbox, "INBOX_DIR", self.root / "state" / "inbox"
        )
        self.inbox_dir_patch.start()
        self.addCleanup(self.inbox_dir_patch.stop)

    def test_check_inbox_returns_the_shared_empty_response(self) -> None:
        self.assertEqual(
            server.check_inbox(), "You have no pending inbox messages."
        )

    def test_check_inbox_claims_and_logs_pending_messages(self) -> None:
        inbox.write_message_to_inbox(
            sender_name="claude-agent",
            recipient_name="codex-agent",
            recipient_cwd=self.cwd,
            message="Review the parser changes.",
        )
        inbox.write_message_to_inbox(
            sender_name="other-codex",
            recipient_name="codex-agent",
            recipient_cwd=self.cwd,
            message="Tests are green.",
        )

        with patch.object(server, "log_inbox_delivered") as log_delivery:
            result = server.check_inbox()

        self.assertIn("2 pending inbox messages:", result)
        self.assertIn("From agent: claude-agent", result)
        self.assertIn("> Review the parser changes.", result)
        self.assertIn("From agent: other-codex", result)
        self.assertIn("> Tests are green.", result)
        self.assertEqual(inbox.pending_inbox_count(self.cwd, "codex-agent"), 0)
        log_delivery.assert_called_once()
        self.assertEqual(
            log_delivery.call_args.kwargs["hook_event"], "check_inbox_tool"
        )
        self.assertEqual(
            log_delivery.call_args.kwargs["recipient_runtime"], "codex"
        )

    def test_racing_check_inbox_calls_deliver_each_message_once(self) -> None:
        bodies = [f"message-{index}" for index in range(24)]
        for body in bodies:
            inbox.write_message_to_inbox(
                sender_name="sender",
                recipient_name="codex-agent",
                recipient_cwd=self.cwd,
                message=body,
            )

        with (
            patch.object(server, "log_inbox_delivered"),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(pool.map(lambda _: server.check_inbox(), range(8)))

        combined = "\n".join(results)
        for body in bodies:
            self.assertEqual(combined.count(f"> {body}\n"), 1, body)
        self.assertEqual(inbox.pending_inbox_count(self.cwd, "codex-agent"), 0)


class CodexMonitorInboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_immediately_without_status_change_when_pending(self) -> None:
        with (
            patch.object(server, "pending_inbox_count", return_value=1),
            patch.object(server, "_write_session_status") as write_status,
        ):
            result = await server.monitor_inbox()

        self.assertEqual(
            result,
            "You already have pending inbox messages. Call check_inbox to read them.",
        )
        write_status.assert_not_called()

    async def test_waits_asynchronously_and_restores_working_status(self) -> None:
        statuses: list[str] = []
        with (
            patch.object(server, "pending_inbox_count", side_effect=[0, 0, 1]),
            patch.object(server, "_MONITOR_INBOX_POLL_INTERVAL", 0),
            patch.object(
                server, "_write_session_status", side_effect=statuses.append
            ),
        ):
            result = await server.monitor_inbox(timeout_seconds=30)

        self.assertEqual(
            result, "You have a new inbox message. Call check_inbox to read it."
        )
        self.assertEqual(statuses, ["listening", "working"])

    async def test_timeout_restores_working_status(self) -> None:
        statuses: list[str] = []
        with (
            patch.object(server, "pending_inbox_count", return_value=0),
            # Pin identity resolution: patching the time module globally means
            # ANY time.time() consumes one of the two values below, and the
            # Claude branch of _resolve_self_identity calls it while scanning
            # transcripts. That made this test pass or fail on the ambient
            # MULTIVERSE_RUNTIME of whoever ran it — green from a Codex
            # session (early return, no clock read), StopIteration from a
            # Claude one.
            patch.object(
                server, "_resolve_self_identity", return_value=("agent", "/tmp/ws")
            ),
            patch.object(server.time, "time", side_effect=[0, 2]),
            patch.object(
                server, "_write_session_status", side_effect=statuses.append
            ),
        ):
            result = await server.monitor_inbox(timeout_seconds=1)

        self.assertEqual(
            result,
            "No new inbox messages arrived within 1s. Call monitor_inbox again "
            "to keep waiting, or proceed without a reply.",
        )
        self.assertEqual(statuses, ["listening", "working"])

    async def test_cancellation_restores_working_status(self) -> None:
        statuses: list[str] = []
        with (
            patch.object(server, "pending_inbox_count", return_value=0),
            patch.object(
                server.asyncio,
                "sleep",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch.object(
                server, "_write_session_status", side_effect=statuses.append
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await server.monitor_inbox(timeout_seconds=30)

        self.assertEqual(statuses, ["listening", "working"])


class CodexInboxToolRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_mcp_server_exposes_both_inbox_tools(self) -> None:
        tools = await server.mcp.list_tools()
        tool_names = {tool.name for tool in tools}
        self.assertIn("check_inbox", tool_names)
        self.assertIn("monitor_inbox", tool_names)


class CodexPluginMcpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_server_waits_then_claims_a_real_inbox_message(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        wrapper_root = repo_root / "multiverse-plugin"
        mcp_server = repo_root / "plugins" / "multiverse-codex" / "scripts" / "mcp-server"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_root = root / "state"
            cwd = str(root / "workspace")
            Path(cwd).mkdir()
            name = "codex-agent"
            inbox_dir = (
                state_root / "inbox" / inbox._encode_cwd(cwd) / name
            )
            inbox_dir.mkdir(parents=True)
            (inbox_dir / "message.json").write_text(
                json.dumps(
                    {
                        "message_id": "message",
                        "from": "claude-agent",
                        "body": "Integration message",
                        "ts": 1,
                    }
                )
            )
            env = {
                **os.environ,
                "MULTIVERSE_ROOT": str(state_root),
                "MULTIVERSE_RUNTIME": "codex",
                "MULTIVERSE_SESSION_CWD": cwd,
                "MULTIVERSE_SESSION_NAME": name,
                "MULTIVERSE_PLUGIN_ROOT": str(wrapper_root),
                "UV_CACHE_DIR": "/tmp/multiverse-uv-cache",
            }
            params = StdioServerParameters(
                command=str(mcp_server),
                env=env,
                cwd=str(repo_root),
            )

            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = {tool.name for tool in listed.tools}
                    self.assertIn("check_inbox", names)
                    self.assertIn("monitor_inbox", names)

                    monitored = await session.call_tool("monitor_inbox", {})
                    self.assertFalse(monitored.isError)
                    self.assertTrue(monitored.content[0].text.startswith(
                        "You already have pending inbox messages. Call "
                        "check_inbox to read them.",
                    ))
                    self.assertIn("[Multiverse reminder]", monitored.content[0].text)
                    self.assertTrue((inbox_dir / "message.json").exists())

                    checked = await session.call_tool("check_inbox", {})
                    self.assertFalse(checked.isError)
                    self.assertIn("Integration message", checked.content[0].text)
                    self.assertFalse((inbox_dir / "message.json").exists())


if __name__ == "__main__":
    unittest.main()
