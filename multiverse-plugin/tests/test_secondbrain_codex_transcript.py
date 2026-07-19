from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import codex_transcript, sb_transcript, sessions


class RolloutFixture:
    def __init__(self, root: Path, thread_id: str) -> None:
        self.thread_id = thread_id
        self.codex_home = root / "codex-home"
        directory = self.codex_home / "sessions" / "2026" / "07" / "19"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"rollout-2026-07-19T00-00-00-{thread_id}.jsonl"
        self.records: list[dict] = []
        self.append(
            {
                "timestamp": "2026-07-19T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": thread_id, "cwd": str(root)},
            }
        )

    def append(self, record: dict) -> None:
        self.records.append(record)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def append_records(self, records: list[dict]) -> None:
        for record in records:
            self.append(record)

    def turn_records(
        self,
        turn_id: str,
        user: str,
        assistant: str,
        *,
        minute: int,
        tool: tuple[str, object] | None = None,
        status: str = "complete",
    ) -> list[dict]:
        timestamp = f"2026-07-19T00:{minute:02d}:00Z"
        records = [
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn_id},
            },
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": user},
            },
        ]
        if tool is not None:
            name, arguments = tool
            records.append(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "id": f"tool-{turn_id}",
                        "call_id": f"call-{turn_id}",
                        "name": name,
                        "input": arguments,
                        "status": "completed",
                    },
                }
            )
        if assistant:
            records.append(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": f"message-{turn_id}",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": assistant}],
                    },
                }
            )
        end_type = "task_complete" if status == "complete" else "turn_aborted"
        records.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": end_type, "turn_id": turn_id},
            }
        )
        return records

    def append_turn(self, *args, **kwargs) -> list[dict]:  # noqa: ANN002, ANN003
        records = self.turn_records(*args, **kwargs)
        self.append_records(records)
        return records


class CodexRolloutTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.rollout = RolloutFixture(self.root, "thread-main")

    def test_incremental_cursor_can_advance_within_a_turn(self) -> None:
        self.rollout.append_turn("turn-a", "First prompt", "First answer", minute=1)
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )

        timestamp = "2026-07-19T00:02:00Z"
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            }
        )
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Second prompt"},
            }
        )
        first = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert first is not None
        first_text, first_cursor = first
        self.assertEqual(first_text, "[turn 2026-07-19 00:02]\n[user] Second prompt")

        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "id": "tool-b",
                    "call_id": "call-b",
                    "name": "functions.exec",
                    "input": {"command": "git status --short"},
                    "status": "completed",
                },
            }
        )
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "message-b",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Second answer"}],
                },
            }
        )
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-b"},
            }
        )

        second = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, first_cursor
        )
        assert second is not None
        second_text, _ = second
        self.assertNotIn("Second prompt", second_text)
        self.assertEqual(
            second_text,
            "[turn 2026-07-19 00:02]\n"
            "[tool:functions.exec] git status --short\n"
            "[assistant] Second answer",
        )

    def test_rollback_finds_common_turn_and_delivers_only_new_branch(self) -> None:
        self.rollout.append_turn("turn-a", "A prompt", "A answer", minute=1)
        self.rollout.append_turn("turn-b", "B prompt", "B answer", minute=2)
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )
        self.rollout.append(
            {
                "timestamp": "2026-07-19T00:03:00Z",
                "type": "event_msg",
                "payload": {"type": "thread_rolled_back", "num_turns": 1},
            }
        )
        self.rollout.append_turn("turn-c", "C prompt", "C answer", minute=4)

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert peeked is not None
        text, cursor = peeked

        self.assertIn("C prompt", text)
        self.assertIn("C answer", text)
        self.assertNotIn("A prompt", text)
        self.assertNotIn("B prompt", text)
        self.assertEqual(cursor["turn_id"], "turn-c")

    def test_rollback_without_new_turn_reanchors_before_later_branch(self) -> None:
        self.rollout.append_turn("turn-a", "A prompt", "A answer", minute=1)
        self.rollout.append_turn("turn-b", "B prompt", "B answer", minute=2)
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )
        self.rollout.append(
            {
                "timestamp": "2026-07-19T00:03:00Z",
                "type": "event_msg",
                "payload": {"type": "thread_rolled_back", "num_turns": 1},
            }
        )

        rolled_back = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert rolled_back is not None
        text, reanchored_cursor = rolled_back
        self.assertEqual(text, "")
        self.assertEqual(reanchored_cursor["turn_id"], "turn-a")

        self.rollout.append_turn("turn-c", "C prompt", "C answer", minute=4)
        branched = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, reanchored_cursor
        )
        assert branched is not None
        self.assertIn("C prompt", branched[0])
        self.assertNotIn("B prompt", branched[0])

    def test_compaction_records_are_not_rendered_or_treated_as_new_turns(self) -> None:
        self.rollout.append_turn("turn-a", "A prompt", "A answer", minute=1)
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )
        self.rollout.append(
            {
                "timestamp": "2026-07-19T00:02:00Z",
                "type": "compacted",
                "payload": {
                    "message": "DO NOT RENDER COMPACTION SUMMARY",
                    "replacement_history": [
                        {"role": "user", "content": "DO NOT RENDER OLD HISTORY"}
                    ],
                    "window_id": "window-1",
                },
            }
        )
        self.rollout.append(
            {
                "timestamp": "2026-07-19T00:02:00Z",
                "type": "event_msg",
                "payload": {"type": "context_compacted"},
            }
        )
        self.rollout.append_turn("turn-b", "B prompt", "B answer", minute=3)

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert peeked is not None
        self.assertIn("B prompt", peeked[0])
        self.assertNotIn("DO NOT RENDER", peeked[0])

    def test_incomplete_final_jsonl_record_is_excluded_from_cursor(self) -> None:
        self.rollout.append_turn("turn-a", "A prompt", "A answer", minute=1)
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )
        self.rollout.append_turn("turn-b", "B prompt", "B answer", minute=2)
        complete_size = self.rollout.path.stat().st_size
        with self.rollout.path.open("ab") as fh:
            fh.write(b'{"type":"event_msg","payload":')

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert peeked is not None
        self.assertIn("B answer", peeked[0])
        self.assertEqual(peeked[1]["record_offset"], complete_size)

    def test_forked_thread_uses_preserved_turn_ids_to_find_divergence(self) -> None:
        turn_a = self.rollout.append_turn("turn-a", "A prompt", "A answer", minute=1)
        self.rollout.append_turn("turn-b", "B prompt", "B answer", minute=2)
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )

        fork = RolloutFixture(self.root, "thread-fork")
        fork.append_records(turn_a)
        fork.append_turn("turn-c", "C prompt", "C answer", minute=3)
        peeked = codex_transcript.peek_partial_transcript(
            fork.codex_home, fork.thread_id, marker
        )
        assert peeked is not None

        self.assertIn("C prompt", peeked[0])
        self.assertNotIn("A prompt", peeked[0])
        self.assertNotIn("B prompt", peeked[0])
        self.assertEqual(peeked[1]["thread_id"], "thread-fork")

    def test_pending_user_is_not_repeated_when_task_start_assigns_turn_id(self) -> None:
        timestamp = "2026-07-19T00:01:00Z"
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Pending prompt"},
            }
        )
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-a"},
            }
        )
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "message-a",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Later answer"}],
                },
            }
        )

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert peeked is not None
        self.assertEqual(peeked[0], "[turn 2026-07-19 00:01]\n[assistant] Later answer")

    def test_invalid_byte_cursor_recovers_from_stable_item_identity(self) -> None:
        timestamp = "2026-07-19T00:01:00Z"
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-a"},
            }
        )
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Already delivered"},
            }
        )
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )
        assert marker is not None
        marker["record_hash"] = "stale-hash"
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "message-a",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "New answer"}],
                },
            }
        )

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert peeked is not None
        self.assertIn("New answer", peeked[0])
        self.assertNotIn("Already delivered", peeked[0])

    def test_invalid_empty_turn_cursor_replays_turn_instead_of_skipping(self) -> None:
        timestamp = "2026-07-19T00:01:00Z"
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-a"},
            }
        )
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )
        assert marker is not None
        marker["record_hash"] = "stale-hash"
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "New prompt"},
            }
        )
        self.rollout.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "message-a",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "New answer"}],
                },
            }
        )

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert peeked is not None
        self.assertIn("New prompt", peeked[0])
        self.assertIn("New answer", peeked[0])

    def test_multiple_rollbacks_count_aborted_turns(self) -> None:
        self.rollout.append_turn("turn-a", "A prompt", "A answer", minute=1)
        self.rollout.append_turn("turn-b", "B prompt", "B answer", minute=2)
        self.rollout.append_turn(
            "turn-c", "C prompt", "C interrupted", minute=3, status="aborted"
        )
        marker = codex_transcript.initialize_cursor(
            self.rollout.codex_home, self.rollout.thread_id
        )
        self.rollout.append(
            {
                "timestamp": "2026-07-19T00:04:00Z",
                "type": "event_msg",
                "payload": {"type": "thread_rolled_back", "num_turns": 2},
            }
        )
        self.rollout.append_turn("turn-d", "D prompt", "D answer", minute=5)
        self.rollout.append(
            {
                "timestamp": "2026-07-19T00:06:00Z",
                "type": "event_msg",
                "payload": {"type": "thread_rolled_back", "num_turns": 1},
            }
        )
        self.rollout.append_turn("turn-e", "E prompt", "E answer", minute=7)

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, marker
        )
        assert peeked is not None
        self.assertIn("E prompt", peeked[0])
        self.assertNotIn("B prompt", peeked[0])
        self.assertNotIn("C prompt", peeked[0])
        self.assertNotIn("D prompt", peeked[0])

    def test_multiverse_inbox_prompts_are_filtered_from_user_text(self) -> None:
        self.rollout.append_turn(
            "turn-a",
            "Please check your inbox — a new inter-agent message just arrived.",
            "Inbox handled",
            minute=1,
        )
        self.rollout.append_turn(
            "turn-b",
            "Real initial task\n\nYou have 2 pending inbox messages from other agents "
            "in your workspace. Call the multiverse check_inbox tool to read and "
            "clear them, then respond or act as appropriate.",
            "Task handled",
            minute=2,
        )

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, None
        )
        assert peeked is not None
        self.assertNotIn("check your inbox", peeked[0])
        self.assertNotIn("pending inbox", peeked[0])
        self.assertIn("Real initial task", peeked[0])
        self.assertIn("Inbox handled", peeked[0])

    def test_scaffolding_reasoning_and_tool_results_are_not_rendered(self) -> None:
        timestamp = "2026-07-19T00:01:00Z"
        self.rollout.append_records(
            [
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-a"},
                },
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Visible prompt"},
                },
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "reasoning",
                        "id": "reasoning-a",
                        "summary": [{"type": "summary_text", "text": "HIDDEN"}],
                    },
                },
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "id": "tool-a",
                        "call_id": "call-a",
                        "name": "exec",
                        "arguments": '{"command":"git status"}',
                    },
                },
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-a",
                        "output": "HIDDEN TOOL RESULT",
                    },
                },
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "developer-a",
                        "role": "developer",
                        "content": [{"type": "text", "text": "HIDDEN DEVELOPER"}],
                    },
                },
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "message-a",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Visible answer"}],
                    },
                },
            ]
        )

        peeked = codex_transcript.peek_partial_transcript(
            self.rollout.codex_home, self.rollout.thread_id, None
        )
        assert peeked is not None
        self.assertIn("[user] Visible prompt", peeked[0])
        self.assertIn("[tool:exec] git status", peeked[0])
        self.assertIn("[assistant] Visible answer", peeked[0])
        self.assertNotIn("HIDDEN", peeked[0])


class CodexMarkerDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cwd = str((self.root / "workspace").resolve())
        Path(self.cwd).mkdir()
        self.rollout = RolloutFixture(self.root, "thread-main")
        self.rollout.append_turn(
            "turn-a", "Existing prompt", "Existing answer", minute=1
        )
        self.row = sessions.DashboardRow(
            name="codex-agent",
            cwd=self.cwd,
            status="idle",
            status_ts=1,
            runtime=sessions.RUNTIME_CODEX,
            session_id=self.rollout.thread_id,
        )
        self.markers = self.root / "markers"

    def _patches(self):
        return (
            patch.object(sb_transcript, "_markers_dir", return_value=self.markers),
            patch.object(sb_transcript, "_resolve_agent_row", return_value=self.row),
            patch.object(sessions, "CODEX_HOME", self.rollout.codex_home),
        )

    def test_first_connect_initializes_at_current_codex_tip_without_backlog(
        self,
    ) -> None:
        marker_dir, resolve, codex_home = self._patches()
        with marker_dir, resolve, codex_home:
            self.assertTrue(sb_transcript.initialize_marker(self.cwd, "codex-agent"))
            self.assertFalse(sb_transcript.initialize_marker(self.cwd, "codex-agent"))
            marker = sb_transcript.read_marker(self.cwd, "codex-agent")
            peeked = sb_transcript.peek_partial_transcript(self.cwd, "codex-agent")

        assert marker is not None
        assert peeked is not None
        self.assertEqual(marker["runtime"], "codex")
        self.assertEqual(marker["thread_id"], self.rollout.thread_id)
        self.assertEqual(marker["turn_id"], "turn-a")
        self.assertEqual(peeked[0], "")

    def test_commit_advances_shared_marker_only_to_returned_cursor(self) -> None:
        marker_dir, resolve, codex_home = self._patches()
        with marker_dir, resolve, codex_home:
            sb_transcript.initialize_marker(self.cwd, "codex-agent")
            original = sb_transcript.read_marker(self.cwd, "codex-agent")
            self.rollout.append_turn("turn-b", "New prompt", "New answer", minute=2)
            peeked = sb_transcript.peek_partial_transcript(self.cwd, "codex-agent")
            assert peeked is not None
            text, cursor = peeked
            before_commit = sb_transcript.read_marker(self.cwd, "codex-agent")
            sb_transcript.commit_marker(self.cwd, "codex-agent", cursor)
            committed = sb_transcript.read_marker(self.cwd, "codex-agent")

        self.assertIn("New answer", text)
        self.assertEqual(before_commit["record_offset"], original["record_offset"])
        self.assertEqual(committed["record_offset"], cursor["record_offset"])
        self.assertEqual(committed["turn_id"], "turn-b")


if __name__ == "__main__":
    unittest.main()
