from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import ask_agents, sb_connector, sb_transcript, sessions
from secondbrain import protocol as sb_protocol


SUMMARY_QUESTION = (
    "Please give a concise summary of what you have achieved so far "
    "in this workspace."
)


class SecondBrainCodexFirstConnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cwd = str((Path(self.temp_dir.name) / "mixed-workspace").resolve())
        Path(self.cwd).mkdir()
        self.workspace_id = f"device-id:{self.cwd}"

        self.connector = object.__new__(sb_connector.SBConnector)
        self.connector.identity = SimpleNamespace(
            device_id="device-id", granted_workspaces=[self.cwd]
        )
        self.connector._state = {"seq_hw": 0, "seeded": []}
        self.connector._seeded = set()
        self.connector._marker_inited = set()
        self.connector.buffer = sb_connector.OutboundBuffer(start_seq=0)
        self.connector._new_event = asyncio.Event()

    def _rows(self) -> list[sessions.DashboardRow]:
        return [
            sessions.DashboardRow(
                name="claude-agent",
                cwd=self.cwd,
                status="idle",
                status_ts=1,
                runtime=sessions.RUNTIME_CLAUDE,
                claude_uuid="claude-thread-id",
            ),
            sessions.DashboardRow(
                name="codex-agent",
                cwd=self.cwd,
                status="idle",
                status_ts=2,
                runtime=sessions.RUNTIME_CODEX,
                session_id="codex-thread-id",
            ),
        ]

    async def test_first_connect_asks_both_runtimes_and_emits_one_event(
        self,
    ) -> None:
        rows = self._rows()
        timeline: list[tuple[str, str]] = []
        targets: dict[str, ask_agents.AskTarget] = {}
        questions: list[str] = []

        def initialize_marker(cwd: str, name: str) -> bool:
            self.assertEqual(cwd, self.cwd)
            timeline.append(("marker", name))
            return True

        def ask_one(target, question, plugin_root, timeout, cancel_event):
            self.assertIsNone(cancel_event)
            self.assertGreater(timeout, 0)
            targets[target.name] = target
            questions.append(question)
            timeline.append(("ask", target.name))
            response = {
                "claude-agent": "Claude completed the data model.",
                "codex-agent": "Codex completed the transcript adapter.",
            }[target.name]
            return ask_agents.AskResult(
                name=target.name,
                response=response,
                error=None,
                fork_uuid=f"ephemeral-{target.name}",
                elapsed=0.1,
            )

        with (
            patch.object(
                sessions, "list_named_agents_in_cwd", return_value=rows
            ) as list_agents,
            patch.object(
                sb_transcript,
                "initialize_marker",
                side_effect=initialize_marker,
            ) as init_marker,
            patch.object(ask_agents, "_ask_one", side_effect=ask_one) as ask,
            patch.object(sb_connector, "save_conn_state") as save_state,
        ):
            await self.connector._emit_workspace_connected(from_seq=0)
            await self.connector._emit_workspace_connected(from_seq=1)

        events = self.connector.buffer.replay_from(0)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.kind, sb_protocol.EventKind.WORKSPACE_CONNECTED)
        self.assertEqual(event.workspace_id, self.workspace_id)
        self.assertEqual(event.workspace_label, "mixed-workspace")
        self.assertEqual(event.event_id, f"wsconn-first:{self.workspace_id}")
        self.assertIsNone(event.agent)
        self.assertEqual(
            event.payload,
            {
                "first_connect": True,
                "workspace_summary": {
                    "claude-agent": "Claude completed the data model.",
                    "codex-agent": "Codex completed the transcript adapter.",
                },
            },
        )
        self.assertNotIn("runtime", json.dumps(event.to_json()))

        self.assertEqual(set(targets), {"claude-agent", "codex-agent"})
        self.assertEqual(targets["claude-agent"].runtime, sessions.RUNTIME_CLAUDE)
        self.assertEqual(
            targets["claude-agent"].native_session_id, "claude-thread-id"
        )
        self.assertEqual(targets["codex-agent"].runtime, sessions.RUNTIME_CODEX)
        self.assertEqual(
            targets["codex-agent"].native_session_id, "codex-thread-id"
        )
        self.assertEqual(questions, [SUMMARY_QUESTION, SUMMARY_QUESTION])

        first_ask = next(index for index, item in enumerate(timeline) if item[0] == "ask")
        self.assertEqual(
            set(timeline[:first_ask]),
            {("marker", "claude-agent"), ("marker", "codex-agent")},
        )
        self.assertEqual(init_marker.call_count, 2)
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(list_agents.call_count, 2)

        self.assertEqual(self.connector._seeded, {self.workspace_id})
        self.assertEqual(self.connector._marker_inited, {self.workspace_id})
        self.assertEqual(self.connector._state["seeded"], [self.workspace_id])
        save_state.assert_called_once_with(self.connector._state)

    async def test_codex_ask_failure_is_kept_in_first_connect_payload(self) -> None:
        codex = self._rows()[1]
        failed = ask_agents.AskResult(
            name="codex-agent",
            response=None,
            error="app-server timed out",
            fork_uuid="ephemeral-codex-agent",
            elapsed=90.0,
        )

        with (
            patch.object(
                sessions, "list_named_agents_in_cwd", return_value=[codex]
            ),
            patch.object(sb_transcript, "initialize_marker", return_value=True),
            patch.object(ask_agents, "_ask_one", return_value=failed),
            patch.object(sb_connector, "save_conn_state"),
        ):
            await self.connector._emit_workspace_connected(from_seq=0)

        event = self.connector.buffer.replay_from(0)[0]
        self.assertEqual(
            event.payload,
            {
                "first_connect": True,
                "workspace_summary": {
                    "codex-agent": "[ask failed: app-server timed out]"
                },
            },
        )

    async def test_empty_workspace_emits_empty_summary_once(self) -> None:
        with (
            patch.object(
                sessions, "list_named_agents_in_cwd", return_value=[]
            ) as list_agents,
            patch.object(sb_transcript, "initialize_marker") as init_marker,
            patch.object(ask_agents, "_ask_one") as ask,
            patch.object(sb_connector, "save_conn_state") as save_state,
        ):
            await self.connector._emit_workspace_connected(from_seq=0)
            await self.connector._emit_workspace_connected(from_seq=1)

        events = self.connector.buffer.replay_from(0)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].payload,
            {"first_connect": True, "workspace_summary": {}},
        )
        self.assertEqual(list_agents.call_count, 2)
        init_marker.assert_not_called()
        ask.assert_not_called()
        save_state.assert_called_once_with(self.connector._state)


if __name__ == "__main__":
    unittest.main()
