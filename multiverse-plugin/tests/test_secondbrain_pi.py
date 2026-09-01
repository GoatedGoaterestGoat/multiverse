from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import (
    pi_prompt_queue,
    pi_transcript,
    sb_connector,
    sb_notify,
    sb_transcript,
    sessions,
)
from secondbrain import protocol as sb_protocol


class _PiSession:
    def __init__(self, path: Path, cwd: str, session_id: str = "pi-session") -> None:
        self.path = path
        self.session_id = session_id
        self._last: str | None = None
        self.append(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": "2026-08-30T00:00:00Z",
                "cwd": cwd,
            },
            parent=False,
        )

    def append(self, row: dict, *, parent: bool = True) -> str | None:
        row = dict(row)
        if row.get("type") != "session":
            row.setdefault("id", f"entry-{len(self.path.read_text().splitlines()) if self.path.exists() else 0}")
            row.setdefault("parentId", self._last if parent else None)
            row.setdefault("timestamp", "2026-08-30T00:01:00Z")
            self._last = row["id"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        return row.get("id")

    def user(self, text: str, *, entry_id: str, parent_id: str | None = None) -> str:
        row = {
            "type": "message",
            "id": entry_id,
            "timestamp": "2026-08-30T00:02:00Z",
            "message": {"role": "user", "content": text, "timestamp": 1},
        }
        if parent_id is not None:
            row["parentId"] = parent_id
            result = self.append(row, parent=False)
            self._last = entry_id
        else:
            result = self.append(row)
        return str(result)

    def assistant(self, text: str, *, entry_id: str) -> str:
        return str(
            self.append(
                {
                    "type": "message",
                    "id": entry_id,
                    "timestamp": "2026-08-30T00:02:30Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                        "timestamp": 2,
                    },
                }
            )
        )


class PiSecondBrainTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = str((self.root / "workspace").resolve())
        Path(self.cwd).mkdir()
        self.path = self.root / "pi-session.jsonl"
        self.transcript = _PiSession(self.path, self.cwd)
        self.old_user = self.transcript.user("Old task", entry_id="old-user")
        self.transcript.assistant("Old answer", entry_id="old-assistant")
        self.agent = "pi-agent"
        self.row = SimpleNamespace(
            runtime=sessions.RUNTIME_PI,
            session_id=self.transcript.session_id,
            session=None,
        )
        self.env = patch.dict(os.environ, {"MULTIVERSE_ROOT": str(self.root / "state")})
        self.row_patch = patch.object(
            sb_transcript, "_resolve_agent_row", return_value=self.row
        )
        self.path_patch = patch.object(
            pi_transcript, "pi_session_path", return_value=self.path
        )
        self.env.start()
        self.row_patch.start()
        self.path_patch.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.row_patch.stop)
        self.addCleanup(self.path_patch.stop)

    def test_incremental_pi_transcript_and_marker(self) -> None:
        self.assertTrue(sb_transcript.initialize_marker(self.cwd, self.agent))
        self.transcript.user("Implement pi support", entry_id="new-user")

        first = sb_transcript.peek_partial_transcript(self.cwd, self.agent)
        self.assertIsNotNone(first)
        text, cursor = first or ("", {})
        self.assertIn("[user] Implement pi support", text)
        self.assertEqual(cursor["runtime"], "pi")
        sb_transcript.commit_marker(self.cwd, self.agent, cursor)

        self.transcript.append(
            {
                "type": "message",
                "id": "new-assistant",
                "timestamp": "2026-08-30T00:03:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Implemented."},
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {"command": "python -m unittest"},
                        },
                    ],
                    "timestamp": 3,
                },
            }
        )
        self.transcript.append(
            {
                "type": "message",
                "id": "tool-result",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "large output"}],
                    "isError": False,
                    "timestamp": 4,
                },
            }
        )

        second = sb_transcript.peek_partial_transcript(self.cwd, self.agent)
        self.assertIsNotNone(second)
        text, cursor = second or ("", {})
        self.assertIn("[assistant] Implemented.", text)
        self.assertIn("[tool:bash] python -m unittest", text)
        self.assertNotIn("large output", text)
        sb_transcript.commit_marker(self.cwd, self.agent, cursor)
        self.assertEqual(
            sb_transcript.read_marker(self.cwd, self.agent)["entry_id"],
            "tool-result",
        )

    def test_branch_away_from_marker_emits_new_suffix(self) -> None:
        self.assertTrue(sb_transcript.initialize_marker(self.cwd, self.agent))
        self.transcript.user(
            "Try another approach",
            entry_id="branch-user",
            parent_id=self.old_user,
        )

        peeked = sb_transcript.peek_partial_transcript(self.cwd, self.agent)
        self.assertIsNotNone(peeked)
        text, _ = peeked or ("", {})
        self.assertIn("[user] Try another approach", text)
        self.assertNotIn("Old answer", text)


class PiSecondBrainEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.cwd = str((self.root / "workspace").resolve())
        Path(self.cwd).mkdir()
        sb_dir = self.state / "secondbrain"
        sb_dir.mkdir(parents=True)
        (sb_dir / "device.key.json").write_text(
            json.dumps({"device_id": "device-id", "granted_workspaces": [self.cwd]})
        )
        (sb_dir / "state.json").write_text(
            json.dumps(
                {
                    "seeded": [f"device-id:{self.cwd}"],
                    "exposed": [self.cwd],
                }
            )
        )
        self.path = self.root / "pi-session.jsonl"
        self.transcript = _PiSession(self.path, self.cwd)
        self.transcript.user("Old task", entry_id="old-user")
        self.transcript.assistant("Old answer", entry_id="old-assistant")
        self.agent = "pi-agent"
        self.row = SimpleNamespace(
            runtime=sessions.RUNTIME_PI,
            session_id=self.transcript.session_id,
            session=None,
        )
        self.env = patch.dict(os.environ, {"MULTIVERSE_ROOT": str(self.state)})
        self.row_patch = patch.object(
            sb_transcript, "_resolve_agent_row", return_value=self.row
        )
        self.path_patch = patch.object(
            pi_transcript, "pi_session_path", return_value=self.path
        )
        self.env.start()
        self.row_patch.start()
        self.path_patch.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.row_patch.stop)
        self.addCleanup(self.path_patch.stop)
        self.assertTrue(sb_transcript.initialize_marker(self.cwd, self.agent))

        self.connector = object.__new__(sb_connector.SBConnector)
        self.connector.buffer = sb_connector.OutboundBuffer(start_seq=0)
        self.connector._new_event = asyncio.Event()

    def test_pi_prompt_and_response_events_use_incremental_native_transcript(self) -> None:
        self.transcript.user("Implement the parser", entry_id="new-user")
        sb_notify.record_user_prompt_signal(self.cwd, self.agent, "Implement the parser")
        self.connector._consume_user_prompt_signals()

        self.transcript.assistant("Parser implemented.", entry_id="new-assistant")
        sb_notify.record_response_signal(self.cwd, self.agent)
        self.connector._consume_response_signals()

        events = self.connector.buffer.replay_from(0)
        self.assertEqual(
            [event.kind for event in events],
            [sb_protocol.EventKind.USER_PROMPT, "agent.response"],
        )
        self.assertIn("[user] Implement the parser", events[0].payload["full"])
        self.assertIn("[assistant] Parser implemented.", events[1].payload["full"])
        for event in events:
            self.assertNotIn("runtime", json.dumps(event.to_json()))


class PiPromptQueueTests(unittest.TestCase):
    def test_prompt_consumer_capability_is_versioned_and_clearable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session_dir = Path(raw)
            self.assertFalse(
                pi_prompt_queue.has_prompt_consumer_capability(session_dir)
            )

            pi_prompt_queue.record_prompt_consumer_capability(session_dir)
            self.assertTrue(
                pi_prompt_queue.has_prompt_consumer_capability(session_dir)
            )
            marker = pi_prompt_queue.capability_path(session_dir)
            marker.write_text(
                json.dumps(
                    {
                        "secondbrain_prompt_consumer": (
                            pi_prompt_queue.PROMPT_CONSUMER_VERSION + 1
                        )
                    }
                )
            )
            self.assertFalse(
                pi_prompt_queue.has_prompt_consumer_capability(session_dir)
            )

            pi_prompt_queue.clear_prompt_consumer_capability(session_dir)
            self.assertFalse(marker.exists())

    def test_claim_release_and_commit_preserve_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(
            os.environ, {"MULTIVERSE_ROOT": raw}
        ):
            cwd = str((Path(raw) / "workspace").resolve())
            pi_prompt_queue.write_prompt(cwd, "pi-agent", "Remote prompt")
            first = pi_prompt_queue.claim_prompts(cwd, "pi-agent")
            self.assertEqual([item["text"] for item in first], ["Remote prompt"])
            claim = Path(first[0]["__claim_path"]).name

            self.assertEqual(
                pi_prompt_queue.release_prompts(cwd, "pi-agent", [claim]), 1
            )
            second = pi_prompt_queue.claim_prompts(cwd, "pi-agent")
            self.assertEqual([item["text"] for item in second], ["Remote prompt"])
            self.assertEqual(pi_prompt_queue.commit_prompts(second), 1)
            self.assertEqual(
                list(pi_prompt_queue.prompt_dir(cwd, "pi-agent").iterdir()), []
            )


if __name__ == "__main__":
    unittest.main()
