from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import sb_connector, sb_notify, sb_transcript, sessions
from secondbrain import protocol as sb_protocol


class _Rollout:
    def __init__(self, root: Path, thread_id: str, cwd: str) -> None:
        self.thread_id = thread_id
        self.codex_home = root / "codex-home"
        directory = self.codex_home / "sessions" / "2026" / "07" / "19"
        directory.mkdir(parents=True)
        self.path = directory / f"rollout-2026-07-19T00-00-00-{thread_id}.jsonl"
        self.append(
            {
                "timestamp": "2026-07-19T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": thread_id, "cwd": cwd},
            }
        )

    def append(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def start(self, turn_id: str, prompt: str, *, minute: int = 1) -> None:
        timestamp = f"2026-07-19T00:{minute:02d}:00Z"
        self.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn_id},
            }
        )
        self.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            }
        )

    def answer(
        self,
        turn_id: str,
        answer: str,
        *,
        minute: int = 1,
        status: str = "completed",
    ) -> None:
        timestamp = f"2026-07-19T00:{minute:02d}:30Z"
        self.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": f"message-{turn_id}",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": answer}],
                },
            }
        )
        if status in ("completed", "aborted"):
            self.finish(turn_id, status=status, minute=minute)

    def finish(
        self, turn_id: str, *, status: str = "completed", minute: int = 1
    ) -> None:
        event_type = "task_complete" if status == "completed" else "turn_aborted"
        self.append(
            {
                "timestamp": f"2026-07-19T00:{minute:02d}:31Z",
                "type": "event_msg",
                "payload": {"type": event_type, "turn_id": turn_id},
            }
        )


class CodexSecondBrainHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_root = self.root / "state"
        self.cwd = str((self.root / "workspace").resolve())
        Path(self.cwd).mkdir()
        sb_dir = self.state_root / "secondbrain"
        sb_dir.mkdir(parents=True)
        # Hooks only record for a workspace the device has been granted AND the user
        # is currently observing, so the fixture has to carry both — the same shape
        # enroll() and expose_workspace() write in production.
        (sb_dir / "device.key.json").write_text(
            json.dumps({"device_id": "device-id", "granted_workspaces": [self.cwd]}),
            encoding="utf-8",
        )
        (sb_dir / "state.json").write_text(
            json.dumps(
                {"seeded": [f"device-id:{self.cwd}"], "exposed": [self.cwd]}
            ),
            encoding="utf-8",
        )
        self.hook = (
            Path(__file__).resolve().parents[2]
            / "plugins"
            / "multiverse-codex"
            / "scripts"
            / "session-hook.py"
        )
        self.env = {
            **os.environ,
            "MULTIVERSE_ROOT": str(self.state_root),
            "MULTIVERSE_STATUS": str(self.state_root / "sessions" / "one" / "status.json"),
            "MULTIVERSE_SESSION_CWD": self.cwd,
            "MULTIVERSE_SESSION_NAME": "codex-agent",
            "MULTIVERSE_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        }
        self.env_patch = patch.dict(
            os.environ, {"MULTIVERSE_ROOT": str(self.state_root)}
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _run(self, event: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.hook)],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

    def test_hooks_record_idempotent_cleaned_turn_signals(self) -> None:
        prompt = (
            "Implement the parser\n\nYou have 2 pending inbox messages from other "
            "agents in your workspace. Call the multiverse check_inbox tool to read "
            "and clear them, then respond or act as appropriate."
        )
        prompt_event = {
            "hook_event_name": "UserPromptSubmit",
            "turn_id": "turn-real",
            "session_id": "thread-main",
            "prompt": prompt,
            "cwd": self.cwd,
        }
        self.assertEqual(self._run(prompt_event).returncode, 0)
        self.assertEqual(self._run(prompt_event).returncode, 0)

        prompt_files = list(
            (self.state_root / "secondbrain" / "user_prompt_signals").glob("*.json")
        )
        self.assertEqual(len(prompt_files), 1)
        prompt_signal = json.loads(prompt_files[0].read_text(encoding="utf-8"))
        self.assertEqual(prompt_signal["text"], "Implement the parser")
        self.assertEqual(prompt_signal["turn_id"], "turn-real")

        synthetic = {
            **prompt_event,
            "turn_id": "turn-inbox",
            "prompt": (
                "Please check your inbox — one or more new inter-agent messages have "
                "arrived. Use the multiverse check_inbox tool to read and clear them, "
                "then respond or act as appropriate."
            ),
        }
        self.assertEqual(self._run(synthetic).returncode, 0)
        self.assertEqual(len(list(prompt_files[0].parent.glob("*.json"))), 1)

        empty_stop = {
            "hook_event_name": "Stop",
            "turn_id": "turn-empty",
            "session_id": "thread-main",
            "last_assistant_message": None,
            "cwd": self.cwd,
        }
        self.assertEqual(self._run(empty_stop).returncode, 0)
        response_dir = self.state_root / "secondbrain" / "response_signals"
        self.assertFalse(response_dir.exists())

        stop = {
            **empty_stop,
            "turn_id": "turn-real",
            "last_assistant_message": "Parser implemented.",
        }
        self.assertEqual(self._run(stop).returncode, 0)
        self.assertEqual(self._run(stop).returncode, 0)
        response_files = list(response_dir.glob("*.json"))
        self.assertEqual(len(response_files), 1)
        self.assertEqual(
            json.loads(response_files[0].read_text(encoding="utf-8"))["turn_id"],
            "turn-real",
        )

    def _tool_event(self, event_name: str, tool_use_id: str) -> dict:
        return {
            "hook_event_name": event_name,
            "session_id": "thread-main",
            "turn_id": "turn-permission",
            "cwd": self.cwd,
            "tool_use_id": tool_use_id,
            "tool_name": "Bash",
            "tool_input": {"command": "touch /tmp/probe"},
        }

    def test_permission_request_correlates_to_pretool_and_allow_closes_on_posttool(
        self,
    ) -> None:
        pre = self._tool_event("PreToolUse", "call-allow")
        self.assertEqual(self._run(pre).returncode, 0)

        permission = {
            key: value for key, value in pre.items() if key != "tool_use_id"
        }
        permission["hook_event_name"] = "PermissionRequest"
        permission["tool_input"] = {
            "command": "touch /tmp/probe",
            "description": "Create the requested probe file?",
        }
        self.assertEqual(self._run(permission).returncode, 0)

        requests = sb_notify.list_permission_request_signals()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["tool_use_id"], "call-allow")
        self.assertEqual(requests[0]["tool_input"], permission["tool_input"])
        self.assertTrue(sb_notify.had_permission_prompt("call-allow"))

        post = self._tool_event("PostToolUse", "call-allow")
        post["tool_response"] = {"exit_code": 0}
        self.assertEqual(self._run(post).returncode, 0)

        decisions = sb_notify.list_permission_decision_signals()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["tool_use_id"], "call-allow")
        self.assertEqual(decisions[0]["decision"], "allow")
        self.assertIsNone(sb_notify.pending_tool_data("call-allow"))
        self.assertFalse(sb_notify.had_permission_prompt("call-allow"))

    def test_cancelled_permission_closes_as_deny_on_stop_without_response(self) -> None:
        pre = self._tool_event("PreToolUse", "call-deny")
        permission = {
            key: value for key, value in pre.items() if key != "tool_use_id"
        }
        permission["hook_event_name"] = "PermissionRequest"
        self.assertEqual(self._run(pre).returncode, 0)
        self.assertEqual(self._run(permission).returncode, 0)

        stop = {
            "hook_event_name": "Stop",
            "session_id": "thread-main",
            "turn_id": "turn-permission",
            "last_assistant_message": None,
            "cwd": self.cwd,
        }
        self.assertEqual(self._run(stop).returncode, 0)

        decisions = sb_notify.list_permission_decision_signals()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["tool_use_id"], "call-deny")
        self.assertEqual(decisions[0]["decision"], "deny")
        self.assertFalse(
            (self.state_root / "secondbrain" / "response_signals").exists()
        )

    def test_permission_request_refuses_ambiguous_concurrent_candidates(self) -> None:
        first = self._tool_event("PreToolUse", "call-one")
        second = self._tool_event("PreToolUse", "call-two")
        self.assertEqual(self._run(first).returncode, 0)
        self.assertEqual(self._run(second).returncode, 0)

        permission = {
            key: value for key, value in first.items() if key != "tool_use_id"
        }
        permission["hook_event_name"] = "PermissionRequest"
        self.assertEqual(self._run(permission).returncode, 0)

        self.assertEqual(sb_notify.list_permission_request_signals(), [])
        self.assertIsNotNone(sb_notify.pending_tool_data("call-one"))
        self.assertIsNotNone(sb_notify.pending_tool_data("call-two"))

    def test_permission_request_selects_exact_concurrent_candidate(self) -> None:
        first = self._tool_event("PreToolUse", "call-one")
        second = self._tool_event("PreToolUse", "call-two")
        second["tool_input"] = {"command": "touch /tmp/other"}
        self.assertEqual(self._run(first).returncode, 0)
        self.assertEqual(self._run(second).returncode, 0)

        permission = {
            key: value for key, value in second.items() if key != "tool_use_id"
        }
        permission["hook_event_name"] = "PermissionRequest"
        self.assertEqual(self._run(permission).returncode, 0)

        requests = sb_notify.list_permission_request_signals()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["tool_use_id"], "call-two")
        self.assertFalse(sb_notify.had_permission_prompt("call-one"))
        self.assertTrue(sb_notify.had_permission_prompt("call-two"))


class CodexSecondBrainEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_root = self.root / "state"
        self.cwd = str((self.root / "workspace").resolve())
        Path(self.cwd).mkdir()
        self.agent = "codex-agent"
        self.workspace_id = f"device-id:{self.cwd}"
        sb_dir = self.state_root / "secondbrain"
        sb_dir.mkdir(parents=True)
        # Granted + exposed, matching what enroll()/expose_workspace() write — the
        # recording gate needs both, not just the legacy seeded set.
        (sb_dir / "device.key.json").write_text(
            json.dumps({"device_id": "device-id", "granted_workspaces": [self.cwd]}),
            encoding="utf-8",
        )
        (sb_dir / "state.json").write_text(
            json.dumps({"seeded": [self.workspace_id], "exposed": [self.cwd]}),
            encoding="utf-8",
        )
        self.env_patch = patch.dict(
            os.environ, {"MULTIVERSE_ROOT": str(self.state_root)}
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.rollout = _Rollout(self.root, "thread-main", self.cwd)
        self.row = SimpleNamespace(
            runtime=sessions.RUNTIME_CODEX,
            session_id=self.rollout.thread_id,
            session=None,
        )
        self.row_patch = patch.object(
            sb_transcript, "_resolve_agent_row", return_value=self.row
        )
        self.home_patch = patch.object(
            sessions, "CODEX_HOME", self.rollout.codex_home
        )
        self.row_patch.start()
        self.home_patch.start()
        self.addCleanup(self.row_patch.stop)
        self.addCleanup(self.home_patch.stop)
        self.assertTrue(sb_transcript.initialize_marker(self.cwd, self.agent))

        self.connector = object.__new__(sb_connector.SBConnector)
        self.connector.buffer = sb_connector.OutboundBuffer(start_seq=0)
        self.connector._new_event = asyncio.Event()

    def _events(self):  # noqa: ANN202
        return self.connector.buffer.replay_from(0)

    def test_prompt_waits_for_rollout_then_response_keeps_wire_contract(self) -> None:
        sb_notify.record_user_prompt_signal(
            self.cwd, self.agent, "Implement the parser", turn_id="turn-one"
        )
        self.connector._consume_user_prompt_signals()
        self.assertEqual(self._events(), [])
        self.assertEqual(len(sb_notify.list_user_prompt_signals()), 1)

        self.rollout.start("turn-one", "Implement the parser")
        self.connector._consume_user_prompt_signals()
        prompt_event = self._events()[0]
        self.assertEqual(prompt_event.kind, sb_protocol.EventKind.USER_PROMPT)
        self.assertEqual(prompt_event.agent, self.agent)
        self.assertEqual(
            prompt_event.payload,
            {
                "text": "Implement the parser",
                "overview_since_last": "",
                "full": "[turn 2026-07-19 00:01]\n[user] Implement the parser",
            },
        )
        self.assertNotIn("runtime", json.dumps(prompt_event.to_json()))
        self.assertEqual(sb_notify.list_user_prompt_signals(), [])

        self.rollout.answer("turn-one", "Parser implemented.")
        sb_notify.record_response_signal(
            self.cwd, self.agent, turn_id="turn-one"
        )
        self.connector._consume_response_signals()
        response_event = self._events()[1]
        self.assertEqual(response_event.kind, "agent.response")
        self.assertEqual(response_event.agent, self.agent)
        self.assertEqual(
            response_event.payload,
            {
                "state": "idle",
                "summary": (
                    "agent 'codex-agent' finished a turn: Parser implemented."
                ),
                "full": "[turn 2026-07-19 00:01]\n[assistant] Parser implemented.",
            },
        )
        self.assertNotIn("runtime", json.dumps(response_event.to_json()))
        self.assertEqual(sb_notify.list_response_signals(), [])

    def test_response_waits_for_same_turn_prompt_event(self) -> None:
        self.rollout.start("turn-fast", "Fast prompt")
        self.rollout.answer("turn-fast", "Fast answer")
        sb_notify.record_user_prompt_signal(
            self.cwd, self.agent, "Fast prompt", turn_id="turn-fast"
        )
        sb_notify.record_response_signal(
            self.cwd, self.agent, turn_id="turn-fast"
        )

        self.connector._consume_response_signals()
        self.assertEqual(self._events(), [])
        self.connector._consume_user_prompt_signals()
        self.connector._consume_response_signals()

        self.assertEqual(
            [event.kind for event in self._events()],
            [sb_protocol.EventKind.USER_PROMPT, "agent.response"],
        )
        self.assertIn("[user] Fast prompt", self._events()[0].payload["full"])
        self.assertIn("[assistant] Fast answer", self._events()[1].payload["full"])

    def test_response_defers_in_progress_and_drops_aborted_turn(self) -> None:
        self.rollout.start("turn-waiting", "Waiting prompt")
        self.rollout.answer(
            "turn-waiting", "Partial answer", status="in_progress"
        )
        sb_notify.record_response_signal(
            self.cwd, self.agent, turn_id="turn-waiting"
        )
        self.connector._consume_response_signals()
        self.assertEqual(self._events(), [])
        self.assertEqual(len(sb_notify.list_response_signals()), 1)

        self.rollout.finish("turn-waiting", status="aborted")
        self.connector._consume_response_signals()
        self.assertEqual(self._events(), [])
        self.assertEqual(sb_notify.list_response_signals(), [])

    def test_permission_notification_waits_for_rollout_and_keeps_wire_contract(
        self,
    ) -> None:
        tool_input = {
            "command": "rm generated.txt",
            "description": "Remove generated output?",
        }
        sb_notify.record_permission_request_signal(
            self.cwd,
            self.agent,
            "call-delete",
            "Bash",
            tool_input,
            codex_turn_id="turn-permission",
        )
        sb_notify.record_permission_decision_signal(
            self.cwd,
            self.agent,
            "call-delete",
            "allow",
            "Bash",
            tool_input,
            {"exit_code": 0},
        )

        self.connector._consume_permission_request_signals()
        self.connector._consume_permission_decision_signals()
        self.assertEqual(self._events(), [])
        self.assertEqual(len(sb_notify.list_permission_request_signals()), 1)
        self.assertEqual(len(sb_notify.list_permission_decision_signals()), 1)

        self.rollout.start("turn-permission", "Delete the generated file")
        self.connector._consume_permission_request_signals()
        self.connector._consume_permission_decision_signals()

        events = self._events()
        self.assertEqual(
            [event.kind for event in events],
            ["agent.permission_request", "user.permission_decision"],
        )
        event = events[0]
        self.assertEqual(event.kind, "agent.permission_request")
        self.assertEqual(event.agent, self.agent)
        self.assertEqual(event.payload["tool_use_id"], "call-delete")
        self.assertEqual(event.payload["tool_name"], "Bash")
        self.assertEqual(event.payload["tool_input"], tool_input)
        self.assertIn("[user] Delete the generated file", event.payload["full"])
        self.assertNotIn("runtime", json.dumps(event.to_json()))
        self.assertEqual(events[1].payload["tool_use_id"], "call-delete")
        self.assertNotIn("runtime", json.dumps(events[1].to_json()))
        self.assertEqual(sb_notify.list_permission_request_signals(), [])
        self.assertEqual(sb_notify.list_permission_decision_signals(), [])


if __name__ == "__main__":
    unittest.main()
