from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src import inbox, interagent_log, server, sessions


class MixedRuntimeMessageAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cwd = str((self.root / "workspace").resolve())
        Path(self.cwd).mkdir()
        self.state_root = self.root / "state"
        self.env_patch = patch.dict(
            os.environ,
            {
                "MULTIVERSE_ROOT": str(self.state_root),
                "MULTIVERSE_SESSION_CWD": self.cwd,
                "MULTIVERSE_SESSION_NAME": "sender",
                "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.inbox_patch = patch.object(
            inbox, "INBOX_DIR", self.state_root / "inbox"
        )
        self.inbox_patch.start()
        self.addCleanup(self.inbox_patch.stop)
        state_patch = patch.object(sessions, "STATE_DIR", self.state_root / "sessions")
        state_patch.start()
        self.addCleanup(state_patch.stop)
        # Real local ownership for the isolated Codex TUI helper tests below.
        target_dir = sessions.STATE_DIR / "target"
        target_dir.mkdir(parents=True)
        (target_dir / "meta.json").write_text(json.dumps({
            "cwd": self.cwd, "name": "receiver", "runtime": "codex",
            "created_at": 1, "host": sessions.current_host(),
        }))
        (target_dir / "status.json").write_text('{"status":"idle"}')

    def _target(
        self,
        *,
        name: str = "receiver",
        runtime: str = sessions.RUNTIME_CODEX,
        status: str = "idle",
        cwd: str | None = None,
        wrapped: bool = True,
    ) -> sessions.DashboardRow:
        target_cwd = cwd or self.cwd
        native_id = f"{runtime}-thread-{name}"
        wrapped_session = None
        if wrapped:
            wrapped_session = sessions.Session(
                id=f"wrapper-{name}",
                cwd=target_cwd,
                created_at=1,
                status=status,
                status_ts=2,
                name=name,
                runtime=runtime,
                claude_uuid=(
                    native_id if runtime == sessions.RUNTIME_CLAUDE else None
                ),
                codex_session_id=(
                    native_id if runtime == sessions.RUNTIME_CODEX else None
                ),
            )
        return sessions.DashboardRow(
            name=name,
            cwd=target_cwd,
            status=status,
            status_ts=2,
            runtime=runtime,
            session=wrapped_session,
            session_id=native_id,
            claude_uuid=(
                native_id if runtime == sessions.RUNTIME_CLAUDE else None
            ),
        )

    def _message(
        self,
        target: sessions.DashboardRow,
        *,
        caller_runtime: str = sessions.RUNTIME_CODEX,
        working_directory: str = "",
    ):
        with (
            patch.dict(
                os.environ,
                {"MULTIVERSE_RUNTIME": caller_runtime},
                clear=False,
            ),
            patch.object(server, "find_agent_by_name", return_value=target),
            patch.object(server, "is_locked", return_value=False),
            patch.object(server, "log_message_sent") as sent,
            patch.object(server, "log_notification_routed") as routed,
        ):
            result = server.message_agent(
                target.name,
                "Please inspect the failing test.",
                working_directory=working_directory,
            )
        return result, sent, routed

    def test_communication_matrix_persists_messages(self) -> None:
        matrix = [
            (sessions.RUNTIME_CLAUDE, sessions.RUNTIME_CLAUDE),
            (sessions.RUNTIME_CODEX, sessions.RUNTIME_CODEX),
            (sessions.RUNTIME_CODEX, sessions.RUNTIME_CLAUDE),
            (sessions.RUNTIME_CLAUDE, sessions.RUNTIME_CODEX),
        ]
        for index, (caller_runtime, target_runtime) in enumerate(matrix):
            with self.subTest(
                caller_runtime=caller_runtime, target_runtime=target_runtime
            ):
                target = self._target(
                    name=f"receiver-{index}", runtime=target_runtime
                )
                # Keep every runtime pair on a failed-delivery path so this stays a
                # pure persistence/metadata test.
                with (
                    patch.object(
                        server, "_wake_via_tmux", return_value=False
                    ) as wake,
                    patch.object(
                        server,
                        "send_prompt_via_tmux",
                        return_value=(
                            False,
                            "",
                            "Claude input composer is not visible",
                        ),
                    ),
                ):
                    result, sent, routed = self._message(
                        target, caller_runtime=caller_runtime
                    )

                pending = inbox.list_pending_messages(self.cwd, target.name)
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["from"], "sender")
                self.assertEqual(
                    pending[0]["body"], "Please inspect the failing test."
                )
                sent.assert_called_once()
                self.assertEqual(
                    sent.call_args.kwargs["sender_runtime"], caller_runtime
                )
                self.assertEqual(
                    sent.call_args.kwargs["recipient_runtime"], target_runtime
                )
                if target_runtime == sessions.RUNTIME_CODEX:
                    wake.assert_called_once_with(
                        target.session.tmux_name,
                        runtime=sessions.RUNTIME_CODEX,
                    )
                    self.assertEqual(
                        routed.call_args.kwargs["route"],
                        "wake_tmux",
                    )
                    self.assertFalse(routed.call_args.kwargs["success"])
                    self.assertIn(
                        "remain queued until Codex calls check_inbox", result
                    )
                else:
                    wake.assert_not_called()
                    self.assertEqual(
                        routed.call_args.kwargs["route"],
                        "passive_fallback_no_composer",
                    )
                    self.assertIn("recipient's next turn", result)

    def test_cross_workspace_message_records_reply_workspace(self) -> None:
        other_cwd = str((self.root / "other-workspace").resolve())
        Path(other_cwd).mkdir()
        target = self._target(
            runtime=sessions.RUNTIME_CODEX, cwd=other_cwd
        )

        # Failed wake keeps this test focused on cross-workspace reply metadata.
        with patch.object(server, "_wake_via_tmux", return_value=False):
            result, _, _ = self._message(
                target,
                caller_runtime=sessions.RUNTIME_CLAUDE,
                working_directory=other_cwd,
            )

        pending = inbox.list_pending_messages(other_cwd, target.name)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["from_cwd"], self.cwd)
        rendered = inbox.render_messages_as_tool_result(pending)
        self.assertIn(f'in workspace: {self.cwd}', rendered)
        self.assertIn(f'working_directory="{self.cwd}"', rendered)
        self.assertIn("remain queued until Codex calls check_inbox", result)

    def test_live_codex_is_pinged_for_idle_and_working(self) -> None:
        for status in ("idle", "working"):
            with self.subTest(status=status):
                target = self._target(name=f"target-{status}", status=status)
                with patch.object(
                    server, "_wake_via_tmux", return_value=True
                ) as wake:
                    result, _, routed = self._message(target)

                wake.assert_called_once_with(
                    target.session.tmux_name,
                    runtime=sessions.RUNTIME_CODEX,
                )
                self.assertEqual(
                    inbox.pending_inbox_count(self.cwd, target.name), 1
                )
                self.assertIn("pinged to call check_inbox now", result)
                self.assertEqual(routed.call_args.kwargs["route"], "wake_tmux")
                self.assertTrue(routed.call_args.kwargs["success"])
                self.assertEqual(
                    routed.call_args.kwargs["recipient_runtime"],
                    sessions.RUNTIME_CODEX,
                )

    def test_codex_delivery_is_not_attachment_gated(self) -> None:
        target = self._target()
        with patch.object(
            server, "_wake_via_tmux", return_value=True
        ) as wake:
            result, _, routed = self._message(target)

        wake.assert_called_once_with(
            target.session.tmux_name,
            runtime=sessions.RUNTIME_CODEX,
        )
        self.assertIn("pinged to call check_inbox now", result)
        self.assertEqual(routed.call_args.kwargs["route"], "wake_tmux")
        self.assertTrue(routed.call_args.kwargs["success"])

    def test_listening_codex_relies_on_monitor_without_terminal_input(self) -> None:
        target = self._target(status="listening")
        with patch.object(server, "_wake_via_tmux") as wake:
            result, _, routed = self._message(target)

        wake.assert_not_called()
        self.assertIn("already listening", result)
        self.assertIn("detect it within a second", result)
        self.assertEqual(
            routed.call_args.kwargs["route"], "already_listening"
        )

    def test_retired_agent_rejects_message_and_names_successor(self) -> None:
        target = self._target(status="retired")
        target.session.successor = "replacement-agent"
        with (
            patch.object(server, "_wake_via_tmux") as wake,
            patch.object(server, "_auto_resume") as resume,
        ):
            result, sent, routed = self._message(target)

        self.assertEqual(
            result,
            "'receiver' cannot be messaged because it has retired. "
            "Consider messaging its successor 'replacement-agent' instead.",
        )
        self.assertEqual(inbox.pending_inbox_count(self.cwd, target.name), 0)
        wake.assert_not_called()
        resume.assert_not_called()
        sent.assert_not_called()
        routed.assert_not_called()

    def test_retired_agent_stays_rejected_under_compaction_overlay(self) -> None:
        target = self._target(status="compacting")
        target.session.status = "retired"
        target.session.successor = "replacement-agent"

        result, sent, routed = self._message(target)

        self.assertIn("successor 'replacement-agent'", result)
        self.assertEqual(inbox.pending_inbox_count(self.cwd, target.name), 0)
        sent.assert_not_called()
        routed.assert_not_called()

    def test_retiring_agent_rejects_messages_without_queueing_or_waking(self) -> None:
        for runtime in ("claude", "codex", "pi"):
            for status in ("retiring", "compacting"):
                with self.subTest(runtime=runtime, status=status):
                    target = self._target(status=status)
                    target.runtime = runtime
                    target.session.runtime = runtime
                    target.session.status = "retiring"
                    with (
                        patch.object(server, "_wake_via_tmux") as wake,
                        patch.object(server, "_auto_resume") as resume,
                    ):
                        result, sent, routed = self._message(target)
                    self.assertEqual(
                        result, "This agent is being retired, please try again later"
                    )
                    self.assertEqual(inbox.pending_inbox_count(self.cwd, target.name), 0)
                    wake.assert_not_called()
                    resume.assert_not_called()
                    sent.assert_not_called()
                    routed.assert_not_called()

    def test_starting_and_waiting_codex_are_always_left_queued(self) -> None:
        for status in ("starting", "waiting"):
            with self.subTest(status=status):
                target = self._target(name=f"target-{status}", status=status)
                with patch.object(server, "_wake_via_tmux") as wake:
                    result, _, routed = self._message(target)

                wake.assert_not_called()
                self.assertIn("no terminal ping was sent", result)
                self.assertIn(
                    "remain queued until Codex calls check_inbox", result
                )
                self.assertEqual(
                    routed.call_args.kwargs["route"],
                    f"passive_fallback_{status}",
                )

    def test_ending_and_status_only_compacting_never_receive_terminal_input(
        self,
    ) -> None:
        for status in ("ending", "compacting"):
            with self.subTest(status=status):
                target = self._target(name=f"target-{status}", status=status)
                with (
                    patch.object(server, "_wake_via_tmux") as wake,
                    patch.object(server, "send_prompt_via_tmux") as send,
                    patch.object(server, "_auto_resume") as resume,
                ):
                    result, _, routed = self._message(target)

                wake.assert_not_called()
                send.assert_not_called()
                resume.assert_not_called()
                self.assertEqual(
                    inbox.pending_inbox_count(self.cwd, target.name), 1
                )
                self.assertIn(f"currently {status}", result)
                self.assertEqual(
                    routed.call_args.kwargs["route"],
                    f"passive_fallback_{status}",
                )

    def test_durable_ending_marker_overrides_stale_idle_snapshot(self) -> None:
        target = self._target(status="idle")
        state_dir = self.root / "sessions"
        target_dir = state_dir / target.session.id
        target_dir.mkdir(parents=True)
        (target_dir / "ending").touch()
        with (
            patch.object(sessions, "STATE_DIR", state_dir),
            patch.object(server, "_wake_via_tmux") as wake,
            patch.object(server, "send_prompt_via_tmux") as send,
        ):
            result, _, routed = self._message(target)

        wake.assert_not_called()
        send.assert_not_called()
        self.assertIn("currently ending", result)
        self.assertEqual(
            routed.call_args.kwargs["route"], "passive_fallback_ending"
        )

    def test_failed_terminal_ping_keeps_codex_message_queued(self) -> None:
        target = self._target()
        with patch.object(server, "_wake_via_tmux", return_value=False):
            result, _, routed = self._message(target)

        self.assertIn("terminal ping failed", result)
        self.assertIn("remain queued until Codex calls check_inbox", result)
        self.assertEqual(routed.call_args.kwargs["route"], "wake_tmux")
        self.assertFalse(routed.call_args.kwargs["success"])
        self.assertEqual(inbox.pending_inbox_count(self.cwd, target.name), 1)

    def test_claude_delivery_is_composer_gated(self) -> None:
        target = self._target(runtime=sessions.RUNTIME_CLAUDE)
        with (
            patch.object(server, "_wake_via_tmux") as wake,
            patch.object(
                server, "send_prompt_via_tmux", return_value=(True, "", "")
            ) as send,
        ):
            result, _, routed = self._message(
                target, caller_runtime=sessions.RUNTIME_CLAUDE
            )

        wake.assert_not_called()
        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs["require_composer"])
        self.assertEqual(routed.call_args.kwargs["route"], "wake_tmux")
        self.assertIn("pinged its terminal", result)
        self.assertNotIn("Codex", result)

    def test_claude_dormant_is_resumed_with_claude_wording(self) -> None:
        target = self._target(
            runtime=sessions.RUNTIME_CLAUDE,
            status="dormant",
            wrapped=False,
        )

        with patch.object(server, "_auto_resume") as resume:
            result, _, routed = self._message(
                target, caller_runtime=sessions.RUNTIME_CLAUDE
            )

        resume.assert_called_once()
        self.assertEqual(routed.call_args.kwargs["route"], "auto_resume")
        self.assertTrue(routed.call_args.kwargs["success"])
        self.assertIn(
            "Woke 'receiver' (it was dormant) and delivered your message",
            result,
        )
        # Claude wording must not borrow the Codex check_inbox phrasing.
        self.assertNotIn("check_inbox", result.split(" If you're expecting")[0])

    def test_dormant_codex_is_resumed_after_inbox_write(self) -> None:
        target = self._target(status="dormant", wrapped=False)

        def assert_inbox_precedes_resume(row, cwd):
            self.assertIs(row, target)
            self.assertEqual(cwd, self.cwd)
            self.assertEqual(inbox.pending_inbox_count(cwd, target.name), 1)

        with patch.object(
            server, "_auto_resume", side_effect=assert_inbox_precedes_resume
        ) as resume:
            result, _, routed = self._message(target)

        resume.assert_called_once_with(target, self.cwd)
        self.assertIn("prompted Codex to call check_inbox on startup", result)
        self.assertEqual(routed.call_args.kwargs["route"], "auto_resume")
        self.assertTrue(routed.call_args.kwargs["success"])

    def test_failed_codex_auto_resume_leaves_message_durable(self) -> None:
        target = self._target(status="dormant", wrapped=False)
        with patch.object(
            server, "_auto_resume", side_effect=RuntimeError("spawn failed")
        ):
            result, _, routed = self._message(target)

        self.assertIn("auto-resume failed (spawn failed)", result)
        self.assertIn("Codex wrapper will then prompt it", result)
        self.assertEqual(inbox.pending_inbox_count(self.cwd, target.name), 1)
        self.assertEqual(routed.call_args.kwargs["route"], "auto_resume")
        self.assertFalse(routed.call_args.kwargs["success"])


class CodexMessageRoutingHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        state = patch.object(sessions, "STATE_DIR", Path(root.name))
        state.start()
        self.addCleanup(state.stop)
        target = sessions.STATE_DIR / "target"
        target.mkdir()
        (target / "meta.json").write_text(json.dumps({
            "runtime": "codex", "created_at": 1, "host": sessions.current_host(),
        }))
        (target / "status.json").write_text('{"status":"idle"}')

    def test_auto_resume_uses_codex_resume_command(self) -> None:
        target = sessions.DashboardRow(
            name="receiver",
            cwd="/workspace",
            status="dormant",
            status_ts=1,
            runtime=sessions.RUNTIME_CODEX,
            session=None,
            session_id="codex-thread",
        )
        with patch.object(server, "create_session") as create:
            server._auto_resume(target, "/workspace")

        create.assert_called_once_with(
            cwd="/workspace",
            claude_args=["resume", "codex-thread"],
            resumed_from="codex-thread",
            name="receiver",
            runtime=sessions.RUNTIME_CODEX,
        )

    def test_ended_codex_wrapper_is_reaped_before_resume(self) -> None:
        wrapped = sessions.Session(
            id="old-wrapper",
            cwd="/workspace",
            created_at=1,
            status="ended",
            name="receiver",
            runtime=sessions.RUNTIME_CODEX,
            codex_session_id="codex-thread",
        )
        target = sessions.DashboardRow(
            name="receiver",
            cwd="/workspace",
            status="ended",
            status_ts=2,
            runtime=sessions.RUNTIME_CODEX,
            session=wrapped,
            session_id="codex-thread",
        )
        calls: list[str] = []
        with (
            patch.object(
                server, "foreign_host_still_reported_live", return_value=False
            ),
            patch.object(
                server, "kill_session", side_effect=lambda _: calls.append("kill")
            ),
            patch.object(
                server, "create_session", side_effect=lambda **_: calls.append("create")
            ),
        ):
            server._auto_resume(target, "/workspace")

        self.assertEqual(calls, ["kill", "create"])

    def test_codex_tmux_wake_uses_empty_focused_composer(self) -> None:
        with (
            patch.object(
                server,
                "capture_focused_codex_composer_draft",
                return_value=(True, "", ""),
            ) as capture,
            patch.object(
                server, "_replace_codex_composer_text", return_value=True
            ) as replace,
            patch.object(
                server, "_submit_codex_wake_prompt", return_value=True
            ) as submit,
            patch.object(
                server, "_restore_codex_composer_text", return_value=True
            ) as restore,
        ):
            woke = server._wake_via_tmux(
                "multiverse-target", runtime=sessions.RUNTIME_CODEX
            )

        self.assertTrue(woke)
        capture.assert_called_once_with("multiverse-target")
        replace.assert_called_once_with(
            "multiverse-target", "", server._WAKE_PROMPT
        )
        submit.assert_called_once_with("multiverse-target")
        restore.assert_called_once_with("multiverse-target", "")

    def test_codex_tmux_wake_saves_clears_and_restores_partial_draft(self) -> None:
        draft = "partially typed\nprompt"
        with (
            patch.object(
                server,
                "capture_focused_codex_composer_draft",
                return_value=(True, draft, ""),
            ),
            patch.object(
                server, "_replace_codex_composer_text", return_value=True
            ) as replace,
            patch.object(
                server, "_submit_codex_wake_prompt", return_value=True
            ) as submit,
            patch.object(
                server, "_restore_codex_composer_text", return_value=True
            ) as restore,
        ):
            woke = server._wake_via_tmux(
                "multiverse-target", runtime=sessions.RUNTIME_CODEX
            )

        self.assertTrue(woke)
        self.assertEqual(
            replace.call_args_list,
            [
                call("multiverse-target", draft, ""),
                call("multiverse-target", "", server._WAKE_PROMPT),
            ],
        )
        submit.assert_called_once_with("multiverse-target")
        restore.assert_called_once_with("multiverse-target", draft)

    def test_codex_tmux_wake_requires_focused_composer(self) -> None:
        with (
            patch.object(
                server,
                "capture_focused_codex_composer_draft",
                return_value=(False, "", "Codex input composer is not focused"),
            ),
            patch.object(server, "_replace_codex_composer_text") as replace,
            patch.object(server, "_submit_codex_wake_prompt") as submit,
        ):
            woke = server._wake_via_tmux(
                "multiverse-target", runtime=sessions.RUNTIME_CODEX
            )

        self.assertFalse(woke)
        replace.assert_not_called()
        submit.assert_not_called()

    def test_codex_tmux_wake_rolls_back_when_notification_insert_fails(self) -> None:
        draft = "saved draft"
        with (
            patch.object(
                server,
                "capture_focused_codex_composer_draft",
                return_value=(True, draft, ""),
            ),
            patch.object(
                server,
                "_replace_codex_composer_text",
                side_effect=[True, False],
            ),
            patch.object(server, "_submit_codex_wake_prompt") as submit,
            patch.object(
                server, "_restore_codex_composer_text", return_value=True
            ) as restore,
        ):
            woke = server._wake_via_tmux(
                "multiverse-target", runtime=sessions.RUNTIME_CODEX
            )

        self.assertFalse(woke)
        submit.assert_not_called()
        restore.assert_called_once_with("multiverse-target", draft)

    def test_codex_tmux_wake_retries_enter_until_composer_clears(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(server, "_codex_wake_prompt_ready", return_value=True),
            patch.object(server.subprocess, "run", side_effect=fake_run),
            patch.object(
                server,
                "_wait_for_codex_wake_prompt_to_clear",
                side_effect=[False, True],
            ) as cleared,
        ):
            submitted = server._submit_codex_wake_prompt("multiverse-target")

        self.assertTrue(submitted)
        self.assertEqual(cleared.call_count, 2)
        self.assertEqual(
            calls,
            [
                ["tmux", "send-keys", "-t", "multiverse-target", "Enter"],
                ["tmux", "send-keys", "-t", "multiverse-target", "Enter"],
            ],
        )

    def test_codex_tmux_wake_fails_if_enter_never_clears_composer(self) -> None:
        with (
            patch.object(server, "_codex_wake_prompt_ready", return_value=True),
            patch.object(
                server.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
            patch.object(
                server, "_wait_for_codex_wake_prompt_to_clear", return_value=False
            ),
        ):
            submitted = server._submit_codex_wake_prompt("multiverse-target")

        self.assertFalse(submitted)
        self.assertEqual(run.call_count, server._CODEX_WAKE_SUBMIT_ATTEMPTS)

    def test_codex_wake_prompt_validation_rejects_xterm_response_leak(self) -> None:
        pane = (
            "transcript\n"
            f"› {server._WAKE_PROMPT}0;276;0c>|xterm.js(6.1.0-beta.288)\n"
            "  gpt-5.4 · Ready · 100% left\n"
        )
        with patch.object(server, "capture_pane", return_value=(True, pane)):
            ready = server._codex_wake_prompt_ready("multiverse-target")

        self.assertFalse(ready)

    def test_codex_wake_prompt_validation_accepts_exact_prompt(self) -> None:
        pane = (
            "transcript\n"
            f"› {server._WAKE_PROMPT}\n"
            "  gpt-5.4 · Ready · 100% left\n"
        )
        with patch.object(server, "capture_pane", return_value=(True, pane)):
            ready = server._codex_wake_prompt_ready("multiverse-target")

        self.assertTrue(ready)

    def test_codex_wake_prompt_validation_accepts_visual_row_wraps(self) -> None:
        first, rest = server._WAKE_PROMPT.split(" read and clear ", 1)
        pane = (
            "transcript\n"
            f"› {first} read and clear\n"
            f"  {rest}\n"
            "  gpt-5.6-sol · Ready · 100% left\n"
        )
        with patch.object(server, "capture_pane", return_value=(True, pane)):
            ready = server._codex_wake_prompt_ready("multiverse-target")

        self.assertTrue(ready)

    def test_codex_wake_insert_accepts_visual_row_wraps(self) -> None:
        rendered = server._WAKE_PROMPT.replace(
            " read and clear ", " read and clear   \n"
        )
        with patch.object(
            server,
            "capture_focused_codex_composer_draft",
            return_value=(True, rendered, ""),
        ):
            inserted = server._wait_for_codex_composer_text(
                "multiverse-target", server._WAKE_PROMPT
            )

        self.assertTrue(inserted)

    def test_codex_wake_prompt_validation_does_not_depend_on_footer_state(self) -> None:
        pane = (
            "transcript\n"
            f"› {server._WAKE_PROMPT}\n"
            "  gpt-5.6 · Thinking · 100% left\n"
        )
        with patch.object(server, "capture_pane", return_value=(True, pane)):
            ready = server._codex_wake_prompt_ready("multiverse-target")

        self.assertTrue(ready)

    def test_claude_tmux_wake_does_not_add_codex_delay(self) -> None:
        with (
            patch.object(
                server.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ),
            patch.object(server.time, "sleep") as sleep,
        ):
            woke = server._wake_via_tmux(
                "multiverse-target", runtime=sessions.RUNTIME_CLAUDE
            )

        self.assertTrue(woke)
        sleep.assert_not_called()

    def test_interagent_logs_record_mixed_runtime_metadata(self) -> None:
        with patch.object(interagent_log, "_append") as append:
            interagent_log.log_message_sent(
                sender="claude-agent",
                recipient="codex-agent",
                cwd="/workspace",
                push_notification=True,
                message_id="message-id",
                message="hello",
                sender_runtime=sessions.RUNTIME_CLAUDE,
                recipient_runtime=sessions.RUNTIME_CODEX,
            )
            sent = append.call_args.args[0]
            self.assertEqual(sent["from_runtime"], sessions.RUNTIME_CLAUDE)
            self.assertEqual(sent["to_runtime"], sessions.RUNTIME_CODEX)

            interagent_log.log_notification_routed(
                sender="claude-agent",
                recipient="codex-agent",
                cwd="/workspace",
                route="wake_tmux",
                success=True,
                sender_runtime=sessions.RUNTIME_CLAUDE,
                recipient_runtime=sessions.RUNTIME_CODEX,
            )
            routed = append.call_args.args[0]
            self.assertEqual(routed["from_runtime"], sessions.RUNTIME_CLAUDE)
            self.assertEqual(routed["to_runtime"], sessions.RUNTIME_CODEX)

            interagent_log.log_inbox_delivered(
                recipient="codex-agent",
                cwd="/workspace",
                hook_event="check_inbox_tool",
                messages=[],
                recipient_runtime=sessions.RUNTIME_CODEX,
            )
            delivered = append.call_args.args[0]
            self.assertEqual(delivered["to_runtime"], sessions.RUNTIME_CODEX)


class CodexMessageAgentMcpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_server_ignores_retired_push_notification_arg(
        self,
    ) -> None:
        """A caller passing the retired ``push_notification`` argument must
        not error, and must not suppress the (now unconditional) wake — the
        MCP layer drops unknown kwargs, so the flag is silently ignored."""
        repo_root = Path(__file__).resolve().parents[2]
        wrapper_root = repo_root / "multiverse-plugin"
        mcp_server = (
            repo_root
            / "plugins"
            / "multiverse-codex"
            / "scripts"
            / "mcp-server"
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_root = root / "state"
            codex_home = root / "codex-home"
            cwd = str((root / "workspace").resolve())
            Path(cwd).mkdir()
            rollout_dir = codex_home / "sessions" / "2026" / "07" / "16"
            rollout_dir.mkdir(parents=True)
            (codex_home / "session_index.jsonl").write_text(
                json.dumps(
                    {"id": "target-thread", "thread_name": "codex-target"}
                )
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
            env = {
                **os.environ,
                "CODEX_HOME": str(codex_home),
                "MULTIVERSE_ROOT": str(state_root),
                "MULTIVERSE_RUNTIME": sessions.RUNTIME_CODEX,
                "MULTIVERSE_SESSION_CWD": cwd,
                "MULTIVERSE_SESSION_NAME": "codex-sender",
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
                    response = await session.call_tool(
                        "message_agent",
                        {
                            "target_name": "codex-target",
                            "message": "Check the integration failure.",
                            "push_notification": False,
                        },
                    )

            self.assertFalse(response.isError)
            # Retired arg ignored → the dormant thread is woken anyway.
            self.assertIn(
                "prompted Codex to call check_inbox on startup",
                response.content[0].text,
            )
            target_inbox = (
                state_root
                / "inbox"
                / inbox._encode_cwd(cwd)
                / "codex-target"
            )
            message_files = list(target_inbox.glob("*.json"))
            self.assertEqual(len(message_files), 1)
            payload = json.loads(message_files[0].read_text())
            self.assertEqual(payload["from"], "codex-sender")
            self.assertEqual(payload["body"], "Check the integration failure.")

            log_files = list(
                (state_root / "logs" / "inter-agent").glob("*.jsonl")
            )
            self.assertEqual(len(log_files), 1)
            records = [
                json.loads(line)
                for line in log_files[0].read_text().splitlines()
                if line.strip()
            ]
            sent = next(record for record in records if record["event"] == "message_sent")
            self.assertEqual(sent["from_runtime"], sessions.RUNTIME_CODEX)
            self.assertEqual(sent["to_runtime"], sessions.RUNTIME_CODEX)


if __name__ == "__main__":
    unittest.main()
