"""Codex never treats shared metadata as permission to operate remote tmux/PIDs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import (
    ask_agents, codex_control, codex_startup_input, codex_usage, hosts,
    inbox, monitor, server, sessions, wake_relay,
)


class CodexMultiHostTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        for context in (
            patch.object(sessions, "STATE_DIR", self.root / "sessions"),
            patch.object(inbox, "WAKE_PENDING_DIR", self.root / "wake_pending"),
            patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "host-a"}),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.directory = sessions.STATE_DIR / "target"
        self.directory.mkdir(parents=True)
        self.tmux = "multiverse-target"
        self.set_owner("host-a")
        (self.directory / "runtime_session_id").write_text("native-thread\n")

    def set_owner(self, host: str | None, status: str = "idle") -> None:
        (self.directory / "meta.json").write_text(json.dumps({
            "runtime": "codex", "name": "target", "cwd": str(self.root),
            "created_at": 123, "host": host,
        }))
        (self.directory / "status.json").write_text(json.dumps({"status": status}))

    def test_ownership_is_fresh_and_uses_stable_host_override(self) -> None:
        with patch.object(hosts.socket, "gethostname", return_value="raw-hostname"):
            self.assertIsNotNone(codex_control.local_session(self.tmux))
            self.set_owner("host-b")
            self.assertIsNone(codex_control.local_session(self.tmux))
            self.set_owner(None)
            self.assertIsNotNone(codex_control.local_session(self.tmux))
        (self.directory / "meta.json").unlink()
        self.assertIsNone(codex_control.local_session(self.tmux))

    def test_noninteractive_local_rows_are_not_wake_targets(self) -> None:
        for status in ("starting", "waiting", "ending", "ended", "retiring", "retired", "unknown"):
            self.set_owner("host-a", status)
            self.assertIsNone(codex_control.local_session(self.tmux), status)
        self.set_owner("host-a", "starting")
        self.assertIsNotNone(codex_control.local_session(self.tmux, startup=True))
        (self.directory / "ending").touch()
        self.assertIsNone(codex_control.local_session(self.tmux, startup=True))

    def test_foreign_wake_never_captures_pastes_clears_or_sends_keys(self) -> None:
        self.set_owner("host-b")
        with (
            patch.object(server, "capture_pane") as pane,
            patch.object(server, "capture_focused_codex_composer_draft") as capture,
            patch.object(server, "clear_composer_text_via_tmux") as clear,
            patch.object(server, "paste_text_via_tmux") as paste,
            patch.object(server.subprocess, "run") as run,
        ):
            self.assertFalse(server._wake_via_tmux(self.tmux, runtime="codex"))
            self.assertFalse(server._submit_codex_wake_prompt(self.tmux))
            self.assertFalse(server._replace_codex_composer_text(self.tmux, "a", "b"))
            self.assertFalse(server._restore_codex_composer_text(self.tmux, "saved"))
            self.assertFalse(server._wait_for_codex_wake_prompt_to_clear(self.tmux))
            for mocked in (pane, capture, clear, paste, run):
                mocked.assert_not_called()

    def test_codex_submit_rechecks_ownership_between_enter_retries(self) -> None:
        def unacknowledged(*args):
            self.set_owner("host-b")
            return False

        pane = f"› {server._WAKE_PROMPT}\n  gpt-6-astra · Ready · 100% left\n"
        with (
            patch.object(server, "capture_pane", return_value=(True, pane)),
            patch.object(server, "_wait_for_codex_wake_prompt_to_clear", side_effect=unacknowledged),
            patch.object(server.subprocess, "run") as run,
        ):
            self.assertFalse(server._submit_codex_wake_prompt(self.tmux))
            self.assertEqual(run.call_count, 1)

    def test_codex_submit_never_sends_enter_for_changed_draft(self) -> None:
        with (
            patch.object(server, "capture_pane", return_value=(True, "› user typed this\n")),
            patch.object(server.subprocess, "run") as run,
        ):
            self.assertFalse(server._submit_codex_wake_prompt(self.tmux))
            run.assert_not_called()

    def test_codex_submit_rechecks_ownership_after_prompt_validation(self) -> None:
        def capture(*args, **kwargs):
            self.set_owner("host-b")
            return True, f"› {server._WAKE_PROMPT}\n  gpt-6-astra · Ready · 100% left\n"

        with (
            patch.object(server, "capture_pane", side_effect=capture),
            patch.object(server.subprocess, "run") as run,
        ):
            self.assertFalse(server._submit_codex_wake_prompt(self.tmux))
            run.assert_not_called()

    def test_restore_does_not_overwrite_newly_typed_text(self) -> None:
        with (
            patch.object(server, "capture_focused_codex_composer_draft", return_value=(True, "new input", "")),
            patch.object(server, "_replace_codex_composer_text") as replace,
        ):
            self.assertFalse(server._restore_codex_composer_text(self.tmux, "saved"))
            replace.assert_not_called()

    def test_failed_restore_retains_draft_and_retry_does_not_resubmit(self) -> None:
        with (
            patch.object(server, "capture_focused_codex_composer_draft", return_value=(True, "saved draft", "")),
            patch.object(server, "_replace_codex_composer_text", return_value=True),
            patch.object(server, "_submit_codex_wake_prompt", return_value=True) as submit,
            patch.object(server, "_restore_codex_composer_text", return_value=False),
        ):
            self.assertFalse(server._wake_codex_via_tmux(self.tmux))
            self.assertEqual(submit.call_count, 1)
        record = self.directory / "codex_wake_draft.json"
        self.assertEqual(json.loads(record.read_text())["text"], "saved draft")
        self.assertTrue(json.loads(record.read_text())["submitted"])
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        with (
            patch.object(server, "_submit_codex_wake_prompt") as submit,
            patch.object(server, "_restore_codex_composer_text", return_value=True),
        ):
            self.assertTrue(server._wake_codex_via_tmux(self.tmux))
            submit.assert_not_called()
        self.assertFalse(record.exists())

    def test_failed_draft_backup_does_not_clear_the_composer(self) -> None:
        with (
            patch.object(server, "capture_focused_codex_composer_draft", return_value=(True, "saved", "")),
            patch.object(codex_control, "save_draft", side_effect=OSError("read-only")),
            patch.object(server, "_replace_codex_composer_text") as replace,
        ):
            self.assertFalse(server._wake_codex_via_tmux(self.tmux))
            replace.assert_not_called()

    def test_foreign_startup_preserves_prompt_and_native_id(self) -> None:
        self.set_owner("host-b", "starting")
        prompt = self.directory / "initial_prompt"
        prompt.write_text("User task")
        native = self.directory / "runtime_session_id"
        with (
            patch.object(codex_startup_input, "capture_focused_codex_composer_draft") as capture,
            patch.object(codex_startup_input, "paste_text_via_tmux") as paste,
            patch.object(codex_startup_input, "_run_tmux") as run,
        ):
            errors = codex_startup_input.deliver_startup_input(
                self.tmux, "target", prompt, native, self.root / "codex",
            )
            self.assertIn("locally owned", errors[0])
            capture.assert_not_called()
            paste.assert_not_called()
            run.assert_not_called()
        self.assertEqual(prompt.read_text(), "User task")
        self.assertEqual(native.read_text(), "native-thread\n")

    def test_startup_rechecks_ownership_after_waiting_for_composer(self) -> None:
        def ready(*args):
            self.set_owner("host-b")
            return True, ""

        with (
            patch.object(codex_startup_input, "_wait_for_empty_composer", side_effect=ready),
            patch.object(codex_startup_input, "paste_text_via_tmux") as paste,
        ):
            ok, error = codex_startup_input._send_startup_input(self.tmux, "task")
            self.assertFalse(ok)
            self.assertIn("locally owned", error)
            paste.assert_not_called()

    def test_foreign_runtime_pid_is_never_probed_or_signalled(self) -> None:
        self.set_owner("host-b")
        session = sessions.load_session("target")
        with (
            patch.object(sessions, "_pid_alive") as alive,
            patch.object(sessions, "_proc_cmdline") as command,
            patch.object(sessions.os, "getpgid") as pgid,
            patch.object(sessions.os, "killpg") as kill,
        ):
            self.assertFalse(sessions._codex_runtime_pid_matches(session, 1234))
            self.assertFalse(sessions._signal_codex_runtime_group(session, 1234, 1234, signal.SIGTERM))
            for mocked in (alive, command, pgid, kill):
                mocked.assert_not_called()

    def test_foreign_active_thread_cannot_start_local_deletion_cli(self) -> None:
        for status in ("starting", "idle", "working", "waiting", "ending", "retiring", "unknown"):
            self.set_owner("host-b", status)
            with patch.object(ask_agents, "_JsonLineProcess") as client:
                with self.assertRaisesRegex(RuntimeError, "host-b.*may still be running"):
                    sessions.delete_codex_thread("native-thread")
                client.assert_not_called()

    def test_foreign_ended_thread_needs_no_local_pid_or_tmux_probe(self) -> None:
        self.set_owner("host-b", "ended")
        with (
            patch.object(sessions, "tmux_session_exists") as tmux,
            patch.object(sessions, "_runtime_pid") as pid,
        ):
            sessions._require_dormant_codex_thread("native-thread")
            tmux.assert_not_called()
            pid.assert_not_called()

    def test_local_ended_row_with_live_runtime_cannot_be_deleted(self) -> None:
        self.set_owner("host-a", "ended")
        with (
            patch.object(sessions, "tmux_session_exists", return_value=False),
            patch.object(sessions, "_runtime_pid", return_value=1234),
            patch.object(sessions, "_codex_runtime_pid_matches", return_value=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "may still be running"):
                sessions._require_dormant_codex_thread("native-thread")

    def test_delete_rechecks_after_app_server_initialization(self) -> None:
        self.set_owner("host-b", "ended")
        client = MagicMock()
        def initialized(*args):
            self.set_owner("host-b", "working")
            return {}

        with (
            patch.object(ask_agents, "_JsonLineProcess", return_value=client),
            patch.object(ask_agents, "_wait_for_response", side_effect=initialized),
        ):
            with self.assertRaisesRegex(RuntimeError, "host-b.*may still be running"):
                sessions.delete_codex_thread("native-thread")
        self.assertNotIn("thread/delete", [call.args[0].get("method") for call in client.send.call_args_list])
        client.close.assert_called_once()

    def test_shared_rollout_tokens_remain_readable_for_foreign_thread(self) -> None:
        self.set_owner("host-b")
        home = self.root / "codex-home"
        rollout = home / "sessions" / "2026" / "rollout-native-thread.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(json.dumps({
            "type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 12345},
                "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 100},
                "model_context_window": 258400,
            }},
        }) + "\n")
        usage = codex_usage.token_usage_cached("native-thread", home)
        self.assertEqual((usage.live_tokens, usage.total_tokens, usage.context_window), (12345, 900, 258400))

    def test_hook_publishes_local_heartbeat_but_never_writes_foreign_state(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        hook = repo / "plugins" / "multiverse-codex" / "scripts" / "session-hook.py"
        env = {key: value for key, value in os.environ.items() if not key.startswith("MULTIVERSE_")}
        env.update({
            "MULTIVERSE_STATUS": str(self.directory / "status.json"),
            "MULTIVERSE_RUNTIME_SESSION_ID_PATH": str(self.directory / "runtime_session_id"),
            "MULTIVERSE_HOST_ID": "host-a",
            "MULTIVERSE_PLUGIN_ROOT": str(repo / "multiverse-plugin"),
        })
        self.set_owner("host-b", "working")
        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "new-native"})
        result = subprocess.run([sys.executable, str(hook)], input=payload, text=True,
                                env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.directory / "status.json").read_text())["status"], "working")
        self.assertEqual((self.directory / "runtime_session_id").read_text(), "native-thread\n")
        self.assertFalse((self.directory / "heartbeat").exists())
        self.set_owner("host-a", "starting")
        result = subprocess.run([sys.executable, str(hook)], input=payload, text=True,
                                env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / "runtime_session_id").read_text(), "new-native\n")
        self.assertTrue((self.directory / "heartbeat").exists())

    def test_wrapper_refuses_foreign_owner_before_launch_or_pid_claim(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        runner = repo / "multiverse-plugin" / "bin" / "multiverse-codex-session"
        self.set_owner("host-b", "working")
        runtime_pid = self.directory / "runtime_pid"
        runtime_pid.write_text("12345\n")
        fake = self.root / "codex"
        launched = self.root / "launched"
        fake.write_text('#!/usr/bin/env bash\ntouch "$LAUNCHED"\n')
        fake.chmod(0o755)
        env = {key: value for key, value in os.environ.items() if not key.startswith("MULTIVERSE_")}
        env.update({
            "MULTIVERSE_STATUS": str(self.directory / "status.json"),
            "MULTIVERSE_RUNTIME_PID_PATH": str(runtime_pid),
            "MULTIVERSE_CODEX_BIN": str(fake), "MULTIVERSE_HOST_ID": "host-a",
            "MULTIVERSE_CODEX_PLUGIN_ROOT": str(repo / "plugins" / "multiverse-codex"),
            "LAUNCHED": str(launched),
        })
        result = subprocess.run([str(runner)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("another host", result.stderr)
        self.assertFalse(launched.exists())
        self.assertEqual(runtime_pid.read_text(), "12345\n")
        self.assertEqual(json.loads((self.directory / "status.json").read_text())["status"], "working")

    def test_cross_host_message_is_only_woken_by_owning_monitor(self) -> None:
        self._cross_host_message("idle")

    def test_cross_host_starting_message_is_relayed_after_startup(self) -> None:
        self._cross_host_message("starting")

    def test_cross_host_waiting_message_is_relayed_after_modal_closes(self) -> None:
        self._cross_host_message("waiting")

    def _cross_host_message(self, initial_status: str) -> None:
        self.set_owner("host-b", initial_status)
        session = sessions.load_session("target")
        row = sessions.DashboardRow(
            name="target", cwd=str(self.root), status=initial_status, status_ts=0, runtime="codex",
            session=session, session_id="native-thread",
        )
        app = monitor.MonitorApp([])
        with (
            patch.dict(os.environ, {
                "MULTIVERSE_ROOT": str(self.root),
                "MULTIVERSE_SESSION_NAME": "sender", "MULTIVERSE_SESSION_CWD": str(self.root),
            }),
            patch.object(inbox, "INBOX_DIR", self.root / "inbox"),
            patch.object(wake_relay, "WAKE_REQUESTS_DIR", self.root / "wakes"),
            patch.object(server, "find_agent_by_name", return_value=row),
            patch.object(server, "is_locked", return_value=False),
            patch.object(server, "log_message_sent"),
            patch.object(server, "log_notification_routed"),
            patch.object(sessions, "tmux_session_exists", return_value=True),
            patch.object(server, "capture_focused_codex_composer_draft", return_value=(True, "", "")) as capture,
            patch.object(server, "_replace_codex_composer_text", return_value=True),
            patch.object(server, "_submit_codex_wake_prompt", return_value=True) as submit,
            patch.object(server, "_restore_codex_composer_text", return_value=True),
        ):
            result = server.message_agent("target", "Please inspect the remote work.")
            self.assertIn("host-b", result)
            self.assertIn("Codex calls check_inbox", result)
            self.assertIn("no terminal ping was sent", result)
            capture.assert_not_called()
            self.assertEqual(inbox.pending_inbox_count(str(self.root), "target"), 1)
            self.assertEqual(wake_relay.relay_pending_wakes(app._deliver_relayed_wake), 0)
            capture.assert_not_called()
            self.assertTrue(wake_relay.request_path("target").exists())
            with patch.dict(os.environ, {"MULTIVERSE_HOST_ID": "host-b"}):
                if initial_status != "idle":
                    self.assertEqual(wake_relay.relay_pending_wakes(app._deliver_relayed_wake), 0)
                    capture.assert_not_called()
                    self.assertTrue(wake_relay.request_path("target").exists())
                    self.set_owner("host-b", "idle")
                self.assertEqual(wake_relay.relay_pending_wakes(app._deliver_relayed_wake), 1)
            submit.assert_called_once_with(self.tmux)
            self.assertFalse(wake_relay.request_path("target").exists())
            self.assertTrue(inbox.wake_is_outstanding(str(self.root), "target"))


if __name__ == "__main__":
    unittest.main()
