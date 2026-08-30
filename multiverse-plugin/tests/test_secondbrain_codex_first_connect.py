from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ed25519

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

        # Built through the REAL constructor, deliberately. A fixture using
        # object.__new__ has to re-list every attribute __init__ sets, which makes it
        # a snapshot that breaks whenever the class gains a field -- it cost three
        # rounds of this exact churn while the connector was growing. Constructing for
        # real keeps the dependency local: new attributes simply exist.
        #
        # The constructor reads its state from disk, so point the state root at the
        # temp dir and write the state this test wants it to load.
        os.environ["MULTIVERSE_ROOT"] = self.temp_dir.name
        self.addCleanup(os.environ.pop, "MULTIVERSE_ROOT", None)
        sb_dir = Path(self.temp_dir.name) / "secondbrain"
        sb_dir.mkdir(parents=True, exist_ok=True)
        (sb_dir / "state.json").write_text(
            json.dumps({"seq_hw": 0, "seeded": [], "exposed": [self.cwd]}),
            encoding="utf-8",
        )
        identity = sb_connector.DeviceIdentity(
            device_id="device-id",
            private_key=ed25519.Ed25519PrivateKey.generate(),
            pinned_server_fingerprint="FINGERPRINT",
            granted_workspaces=[self.cwd],
            device_name="test-device",
        )
        self.connector = sb_connector.SBConnector("ws://test", identity)

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
        ):
            await self.connector._emit_workspace_connected(from_seq=0)
            await self.connector._emit_workspace_connected(from_seq=1)

        events = self.connector.buffer.replay_from(0)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.kind, sb_protocol.EventKind.WORKSPACE_CONNECTED)
        self.assertEqual(event.workspace_id, self.workspace_id)
        self.assertEqual(event.workspace_label, "mixed-workspace")
        # workspace_id alone is unique per SUBJECT, not per event, and a workspace
        # can be re-onboarded (state wiped) -- SB dedups on event_id alone and
        # would drop the second one silently. The epoch suffix separates them.
        self.assertTrue(
            event.event_id.startswith(f"wsconn-first:{self.workspace_id}:"),
            event.event_id,
        )
        epoch = event.event_id.rsplit(":", 1)[1]
        self.assertTrue(epoch, "expected an onboarding epoch in the event_id")
        self.assertEqual(self.connector._seed_epochs[self.workspace_id], epoch)
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
        # Assert what reached disk rather than how many times the writer was called:
        # a restarted daemon reads this back, so persistence is the real requirement.
        persisted = sb_connector.load_conn_state()
        self.assertEqual(persisted["seeded"], [self.workspace_id])
        self.assertEqual(persisted["seed_epochs"][self.workspace_id], epoch)

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
        self.assertEqual(
            sb_connector.load_conn_state()["seeded"], [self.workspace_id]
        )


if __name__ == "__main__":
    unittest.main()
