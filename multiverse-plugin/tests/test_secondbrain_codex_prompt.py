from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from src import compact_state, sb_actions, sb_connector, sessions
from secondbrain import protocol as sb_protocol


CODEX_FOOTER = (
    "  gpt-5.6 · Multiverse · main · Ready · Context 90% left · codex-agent"
)


class CodexPromptTmuxTests(unittest.TestCase):
    def test_bracketed_paste_loads_text_on_stdin_and_deletes_buffer(self) -> None:
        calls: list[tuple[list[str], str | None]] = []

        def fake_tmux(args, *, capture=False, input_text=None):
            self.assertFalse(capture)
            calls.append((args, input_text))
            return True, ""

        with (
            patch.object(sb_actions, "_run_tmux", side_effect=fake_tmux),
            patch.object(
                sb_actions.uuid,
                "uuid4",
                return_value=SimpleNamespace(hex="fixed"),
            ),
            patch.object(sb_actions.os, "getpid", return_value=123),
        ):
            result = sb_actions.paste_text_via_tmux(
                "multiverse-target", "First\nSecond"
            )

        self.assertEqual(result, (True, ""))
        self.assertEqual(
            calls,
            [
                (
                    [
                        "load-buffer",
                        "-b",
                        "multiverse-input-123-fixed",
                        "-",
                    ],
                    "First\nSecond",
                ),
                (
                    [
                        "paste-buffer",
                        "-p",
                        "-r",
                        "-d",
                        "-b",
                        "multiverse-input-123-fixed",
                        "-t",
                        "multiverse-target",
                    ],
                    None,
                ),
            ],
        )

    def test_codex_composer_parser_preserves_logical_multiline_draft(self) -> None:
        pane = (
            "• Earlier output\n"
            "  › this is not the composer\n\n"
            "› First draft line\n"
            "  Second draft line\n"
            "  \n"
            "  Fourth draft line\n\n"
            f"{CODEX_FOOTER}\n"
        )

        self.assertEqual(
            sb_actions.parse_input_box_text(pane, runtime=sessions.RUNTIME_CODEX),
            "First draft line\nSecond draft line\n\nFourth draft line",
        )

    def test_indented_transcript_quote_is_not_treated_as_codex_composer(self) -> None:
        pane = f"• Output\n  › quoted output\n\n{CODEX_FOOTER}\n"

        self.assertEqual(
            sb_actions.parse_input_box_text(pane, runtime=sessions.RUNTIME_CODEX), ""
        )
        styled_draft = (
            "\x1b[1m›\x1b[0m Real draft\n\n"
            f"{CODEX_FOOTER}\n"
        )
        self.assertEqual(
            sb_actions.parse_input_box_text(
                styled_draft, runtime=sessions.RUNTIME_CODEX
            ),
            "Real draft",
        )
        self.assertFalse(sb_actions._codex_composer(pane)[0])

    def test_codex_composer_parser_recognizes_thinking_footer(self) -> None:
        pane = (
            "• Earlier output\n"
            "› Draft while active\n\n"
            "  gpt-5.6 · Multiverse · main · Thinking · Context 90% left\n"
        )

        self.assertEqual(
            sb_actions.parse_input_box_text(pane, runtime=sessions.RUNTIME_CODEX),
            "Draft while active",
        )

    def test_codex_delivery_saves_submits_and_restores_draft(self) -> None:
        draft = "Draft first\nDraft second"
        pane = f"› Draft first\n  Draft second\n\n{CODEX_FOOTER}\n"
        calls: list[list[str]] = []

        def fake_tmux(args, *, capture=False):
            self.assertFalse(capture)
            calls.append(args)
            return True, ""

        with (
            patch.object(sb_actions, "capture_pane", return_value=(True, pane)) as cap,
            patch.object(sb_actions, "_run_tmux", side_effect=fake_tmux),
            patch.object(
                sb_actions, "paste_text_via_tmux", return_value=(True, "")
            ) as paste,
        ):
            result = sb_actions.send_prompt_via_tmux(
                "multiverse-target",
                "Remote first\nRemote second",
                runtime=sessions.RUNTIME_CODEX,
            )

        self.assertEqual(result, (True, draft, ""))
        cap.assert_called_once_with(
            "multiverse-target", join_wrapped=True, include_styles=True
        )
        self.assertEqual(
            paste.call_args_list,
            [
                call("multiverse-target", "Remote first\nRemote second"),
                call("multiverse-target", draft),
            ],
        )
        self.assertEqual(
            calls,
            [
                [
                    "send-keys",
                    "-t",
                    "multiverse-target",
                    *(["BSpace"] * (len(draft) + sb_actions._CLEAR_MARGIN)),
                ],
                ["send-keys", "-t", "multiverse-target", "Enter"],
            ],
        )

    def test_codex_delivery_does_not_restore_dim_ghost_suggestion(self) -> None:
        pane = (
            "\x1b[1m›\x1b[0m \x1b[2mImplement changes in @filename\x1b[0m\n\n"
            f"{CODEX_FOOTER}\n"
        )
        calls: list[list[str]] = []

        def fake_tmux(args, *, capture=False):
            self.assertFalse(capture)
            calls.append(args)
            return True, ""

        with (
            patch.object(sb_actions, "capture_pane", return_value=(True, pane)),
            patch.object(sb_actions, "_run_tmux", side_effect=fake_tmux),
            patch.object(
                sb_actions, "paste_text_via_tmux", return_value=(True, "")
            ) as paste,
        ):
            result = sb_actions.send_prompt_via_tmux(
                "multiverse-target", "Remote prompt", runtime=sessions.RUNTIME_CODEX
            )

        self.assertEqual(result, (True, "", ""))
        self.assertEqual(
            calls,
            [
                ["send-keys", "-t", "multiverse-target", "Enter"],
            ],
        )
        paste.assert_called_once_with("multiverse-target", "Remote prompt")
        self.assertEqual(
            sb_actions.parse_input_box_text(pane, runtime=sessions.RUNTIME_CODEX), ""
        )

    def test_codex_delivery_requires_visible_composer(self) -> None:
        with (
            patch.object(
                sb_actions,
                "capture_pane",
                return_value=(True, f"Starting Codex...\n{CODEX_FOOTER}\n"),
            ),
            patch.object(sb_actions, "_run_tmux") as run,
        ):
            ok, saved, error = sb_actions.send_prompt_via_tmux(
                "multiverse-target", "Remote prompt", runtime=sessions.RUNTIME_CODEX
            )

        self.assertFalse(ok)
        self.assertEqual(saved, "")
        self.assertIn("composer is not visible", error)
        run.assert_not_called()

    def test_codex_delivery_refuses_opaque_paste_without_typing(self) -> None:
        pane = f"› [Pasted Content 2500 chars]\n\n{CODEX_FOOTER}\n"
        with (
            patch.object(sb_actions, "capture_pane", return_value=(True, pane)),
            patch.object(sb_actions, "_run_tmux") as run,
        ):
            ok, saved, error = sb_actions.send_prompt_via_tmux(
                "multiverse-target", "Remote prompt", runtime=sessions.RUNTIME_CODEX
            )

        self.assertFalse(ok)
        self.assertEqual(saved, "[Pasted Content 2500 chars]")
        self.assertIn("cannot be safely restored", error)
        run.assert_not_called()

    def test_claude_delivery_keeps_existing_multiline_sequence(self) -> None:
        pane = "╭────────╮\n│ > draft │\n╰────────╯\n"
        calls: list[list[str]] = []

        def fake_tmux(args, *, capture=False):
            self.assertFalse(capture)
            calls.append(args)
            return True, ""

        with (
            patch.object(sb_actions, "capture_pane", return_value=(True, pane)) as cap,
            patch.object(sb_actions, "_run_tmux", side_effect=fake_tmux),
        ):
            result = sb_actions.send_prompt_via_tmux(
                "multiverse-target", "First\nSecond"
            )

        self.assertEqual(result, (True, "draft", ""))
        cap.assert_called_once_with(
            "multiverse-target", join_wrapped=False, include_styles=False
        )
        self.assertIn(
            ["send-keys", "-t", "multiverse-target", "\\", "Enter"], calls
        )
        self.assertNotIn(["send-keys", "-t", "multiverse-target", "C-j"], calls)

    def test_codex_ready_wait_uses_composer_or_footer(self) -> None:
        pane = f"› \n\n{CODEX_FOOTER}\n"
        with patch.object(
            sb_actions, "capture_pane", return_value=(True, pane)
        ) as cap:
            result = sb_actions.wait_for_ready(
                "multiverse-target", runtime=sessions.RUNTIME_CODEX
            )

        self.assertEqual(result, (True, ""))
        cap.assert_called_once_with("multiverse-target", join_wrapped=True)


class CodexPromptConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cwd = str(Path("/workspace").resolve())
        self.connector = object.__new__(sb_connector.SBConnector)

    def _target(
        self,
        *,
        status: str = "idle",
        wrapped: bool = True,
        host: str = "local-host",
        session_id: str | None = "codex-thread-id",
    ) -> sessions.DashboardRow:
        session = None
        if wrapped:
            session = sessions.Session(
                id="wrapper-id",
                cwd=self.cwd,
                created_at=1,
                status=status,
                status_ts=2,
                name="codex-target",
                runtime=sessions.RUNTIME_CODEX,
                codex_session_id=session_id,
                host=host,
            )
        return sessions.DashboardRow(
            name="codex-target",
            cwd=self.cwd,
            status=status,
            status_ts=2,
            runtime=sessions.RUNTIME_CODEX,
            session=session,
            session_id=session_id,
        )

    def _deliver(self, target: sessions.DashboardRow):
        with (
            patch.object(sessions, "find_agent_by_name", return_value=target),
            patch.object(sessions, "current_host", return_value="local-host"),
            patch.object(sessions, "tmux_session_exists", return_value=True),
            patch.object(compact_state, "is_locked", return_value=False),
            patch.object(
                sb_actions,
                "send_prompt_via_tmux",
                return_value=(True, "", ""),
            ) as send,
            patch.object(sb_actions, "wait_for_ready", return_value=(True, "")) as wait,
        ):
            result = self.connector._deliver_prompt_blocking(
                self.cwd, target.name, "Remote prompt"
            )
        return result, send, wait

    def test_live_codex_statuses_route_through_runtime_tmux_delivery(self) -> None:
        for status in ("idle", "working", "listening"):
            with self.subTest(status=status):
                result, send, wait = self._deliver(self._target(status=status))

                self.assertEqual(result[0], "ok")
                send.assert_called_once_with(
                    "multiverse-wrapper-id",
                    "Remote prompt",
                    runtime=sessions.RUNTIME_CODEX,
                )
                wait.assert_not_called()

    def test_starting_codex_waits_for_runtime_ready_signal(self) -> None:
        result, send, wait = self._deliver(self._target(status="starting"))

        self.assertEqual(result[0], "ok")
        wait.assert_called_once_with(
            "multiverse-wrapper-id", runtime=sessions.RUNTIME_CODEX
        )
        send.assert_called_once()

    def test_starting_timeout_preserves_deliver_failed_semantics(self) -> None:
        target = self._target(status="starting")
        with (
            patch.object(sessions, "find_agent_by_name", return_value=target),
            patch.object(sessions, "current_host", return_value="local-host"),
            patch.object(sessions, "tmux_session_exists", return_value=True),
            patch.object(compact_state, "is_locked", return_value=False),
            patch.object(
                sb_actions, "wait_for_ready", return_value=(False, "timed out")
            ),
            patch.object(sb_actions, "send_prompt_via_tmux") as send,
        ):
            result = self.connector._deliver_prompt_blocking(
                self.cwd, target.name, "Remote prompt"
            )

        self.assertEqual(result[0], "deliver_failed")
        self.assertIn("timed out", result[2])
        send.assert_not_called()

    def test_waiting_and_foreign_codex_sessions_are_refused(self) -> None:
        waiting, send, _ = self._deliver(self._target(status="waiting"))
        self.assertEqual(waiting[0], "waiting")
        send.assert_not_called()

        foreign, send, _ = self._deliver(self._target(host="remote-host"))
        self.assertEqual(foreign[0], "deliver_failed")
        self.assertIn("remote-host", foreign[2])
        send.assert_not_called()

    def test_missing_tmux_and_injection_failure_keep_delivery_error(self) -> None:
        target = self._target()
        with (
            patch.object(sessions, "find_agent_by_name", return_value=target),
            patch.object(sessions, "current_host", return_value="local-host"),
            patch.object(sessions, "tmux_session_exists", return_value=False),
            patch.object(compact_state, "is_locked", return_value=False),
            patch.object(sb_actions, "send_prompt_via_tmux") as send,
        ):
            missing = self.connector._deliver_prompt_blocking(
                self.cwd, target.name, "Remote prompt"
            )
        self.assertEqual(missing[0], "deliver_failed")
        self.assertIn("tmux session", missing[2])
        send.assert_not_called()

        with (
            patch.object(sessions, "find_agent_by_name", return_value=target),
            patch.object(sessions, "current_host", return_value="local-host"),
            patch.object(sessions, "tmux_session_exists", return_value=True),
            patch.object(compact_state, "is_locked", return_value=False),
            patch.object(
                sb_actions,
                "send_prompt_via_tmux",
                return_value=(False, "draft", "composer is not visible"),
            ),
        ):
            failed = self.connector._deliver_prompt_blocking(
                self.cwd, target.name, "Remote prompt"
            )
        self.assertEqual(failed, ("deliver_failed", "", "composer is not visible"))

    def test_dormant_codex_routes_to_resume_with_initial_prompt(self) -> None:
        target = self._target(status="dormant", wrapped=False)
        with (
            patch.object(sessions, "find_agent_by_name", return_value=target),
            patch.object(compact_state, "is_locked", return_value=False),
            patch.object(self.connector, "_auto_resume_with_prompt") as resume,
        ):
            result = self.connector._deliver_prompt_blocking(
                self.cwd, target.name, "Remote prompt"
            )

        self.assertEqual(result[0], "ok")
        resume.assert_called_once_with(target, self.cwd, "Remote prompt")

    def test_dormant_codex_without_native_id_is_not_resumed(self) -> None:
        target = self._target(
            status="dormant", wrapped=False, session_id=None
        )
        with (
            patch.object(sessions, "find_agent_by_name", return_value=target),
            patch.object(compact_state, "is_locked", return_value=False),
            patch.object(self.connector, "_auto_resume_with_prompt") as resume,
        ):
            result = self.connector._deliver_prompt_blocking(
                self.cwd, target.name, "Remote prompt"
            )

        self.assertEqual(result[0], "not_found")
        self.assertIn("codex session ID", result[2])
        resume.assert_not_called()

    def test_codex_resume_uses_native_command_runtime_and_initial_prompt(self) -> None:
        target = self._target(status="dormant", wrapped=False)
        with (
            patch.object(sessions, "create_session") as create,
            patch.object(sessions, "kill_session") as kill,
        ):
            self.connector._auto_resume_with_prompt(
                target, self.cwd, "Remote prompt"
            )

        create.assert_called_once_with(
            cwd=self.cwd,
            claude_args=["resume", "codex-thread-id"],
            resumed_from="codex-thread-id",
            name="codex-target",
            initial_prompt="Remote prompt",
            runtime=sessions.RUNTIME_CODEX,
        )
        kill.assert_not_called()

    def test_ended_codex_wrapper_is_reaped_before_resume(self) -> None:
        target = self._target(status="ended")
        with (
            patch.object(
                sessions, "foreign_host_still_reported_live", return_value=False
            ),
            patch.object(sessions, "kill_session") as kill,
            patch.object(sessions, "create_session") as create,
        ):
            self.connector._auto_resume_with_prompt(
                target, self.cwd, "Remote prompt"
            )

        kill.assert_called_once_with(target.session)
        self.assertEqual(
            create.call_args.kwargs["claude_args"],
            ["resume", "codex-thread-id"],
        )

    def test_foreign_live_ended_wrapper_is_not_reaped_for_resume(self) -> None:
        target = self._target(status="ended", host="remote-host")
        with (
            patch.object(
                sessions, "foreign_host_still_reported_live", return_value=True
            ),
            patch.object(sessions, "kill_session") as kill,
            patch.object(sessions, "create_session") as create,
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing to auto-resume"):
                self.connector._auto_resume_with_prompt(
                    target, self.cwd, "Remote prompt"
                )

        kill.assert_not_called()
        create.assert_not_called()

    def test_claude_resume_command_remains_unchanged(self) -> None:
        target = sessions.DashboardRow(
            name="claude-target",
            cwd=self.cwd,
            status="dormant",
            status_ts=2,
            runtime=sessions.RUNTIME_CLAUDE,
            session_id="claude-session-id",
            claude_uuid="claude-session-id",
        )
        with patch.object(sessions, "create_session") as create:
            self.connector._auto_resume_with_prompt(
                target, self.cwd, "Remote prompt"
            )

        create.assert_called_once_with(
            cwd=self.cwd,
            claude_args=["--resume", "claude-session-id"],
            resumed_from="claude-session-id",
            name="claude-target",
            initial_prompt="Remote prompt",
            runtime=sessions.RUNTIME_CLAUDE,
        )


class PromptActionDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_duplicate_action_is_delivered_once(self) -> None:
        connector = object.__new__(sb_connector.SBConnector)
        connector.identity = SimpleNamespace(
            device_id="device-id", granted_workspaces=["/workspace"]
        )
        connector.channel = None
        connector.dedup = sb_connector.ActionDedup()
        ws = SimpleNamespace(send=AsyncMock())
        action = sb_protocol.Action(
            action_id="action-id",
            ts="2026-07-19T00:00:00Z",
            workspace_id="device-id:/workspace",
            kind=sb_protocol.ActionKind.PROMPT_AGENT,
            agent="codex-target",
            payload={"text": "Remote prompt"},
        )
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def deliver(*_args):
            nonlocal calls
            calls += 1
            started.set()
            release.wait(timeout=2)
            return "ok", "Prompt delivered.", ""

        with patch.object(connector, "_deliver_prompt_blocking", side_effect=deliver):
            await connector._on_action(ws, action)
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started.is_set())

            await connector._on_action(ws, action)
            release.set()
            for _ in range(100):
                if ws.send.await_count:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(calls, 1)
            self.assertEqual(ws.send.await_count, 1)
            delivered = sb_protocol.decode(ws.send.await_args_list[0].args[0])
            self.assertEqual(
                delivered.result, {"status": "Prompt delivered."}
            )
            self.assertNotIn("runtime", delivered.result)

            # A later retry receives the cached result without another delivery.
            await connector._on_action(ws, action)
            self.assertEqual(calls, 1)
            self.assertEqual(ws.send.await_count, 2)


if __name__ == "__main__":
    unittest.main()
