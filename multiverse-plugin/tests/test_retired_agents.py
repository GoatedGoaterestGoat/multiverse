from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import compact_runner, monitor, server, sessions
from src.ask_agents import AskResult


class RetiredDashboardTests(unittest.TestCase):
    def _row(self) -> monitor.DashboardRow:
        session = monitor.Session(
            id="old-wrapper",
            cwd="/workspace",
            created_at=1,
            status="retired",
            status_ts=2,
            name="old-agent",
            successor="new-agent",
        )
        return monitor.DashboardRow(
            name="old-agent",
            cwd="/workspace",
            status="retired",
            status_ts=2,
            session=session,
            session_id="old-native-id",
            claude_uuid="old-native-id",
        )

    def test_enter_does_not_attach_or_resume_retired_agent(self) -> None:
        app = monitor.MonitorApp([])
        row = self._row()
        with (
            patch.object(app, "_current_entry", return_value=row),
            patch.object(app, "_attach_to") as attach,
            patch.object(app, "_spawn_resumed") as resume,
            patch.object(app, "notify") as notify,
        ):
            app.action_primary()

        attach.assert_not_called()
        resume.assert_not_called()
        self.assertIn("cannot be attached or resumed", notify.call_args.args[0])
        self.assertIn("new-agent", notify.call_args.args[0])

    def test_kill_is_the_only_disallowed_terminal_action_besides_resume(self) -> None:
        app = monitor.MonitorApp([])
        row = self._row()
        with (
            patch.object(app, "_current_entry", return_value=row),
            patch.object(app, "_begin_session_kill") as kill,
            patch.object(app, "notify") as notify,
        ):
            app.action_kill()

        kill.assert_not_called()
        self.assertIn("cannot be killed", notify.call_args.args[0])

    def test_retired_is_compactable(self) -> None:
        self.assertIn("retired", monitor._COMPACTABLE_STATUSES)

    def test_missing_tmux_does_not_downgrade_retired_to_ended(self) -> None:
        retired = self._row().session
        with (
            patch.object(sessions, "current_host", return_value="local"),
            patch.object(sessions, "tmux_session_exists", return_value=False),
            patch.object(sessions, "_mark_session_ended") as mark_ended,
        ):
            changed = sessions._correct_local_liveness(retired)

        self.assertFalse(changed)
        mark_ended.assert_not_called()

    def test_retired_agent_is_listed_but_not_counted_live(self) -> None:
        row = self._row()
        with (
            patch.object(
                server, "_resolve_self_identity", return_value=("caller", "/workspace")
            ),
            patch.object(server, "list_named_agents_in_cwd", return_value=[row]),
        ):
            rendered = server.list_agents()

        self.assertIn("1 agent(s) in workspace (0 live)", rendered)
        self.assertIn("old-agent", rendered)
        self.assertIn("retired", rendered)


class DashboardHandoffTests(unittest.TestCase):
    def _dormant(self, *, status: str = "dormant") -> monitor.DashboardRow:
        return monitor.DashboardRow(
            name="predecessor",
            cwd="/workspace",
            status=status,
            status_ts=1,
            runtime=monitor.RUNTIME_CLAUDE,
            session_id="native-id",
            claude_uuid="native-id",
        )

    def _name_successor(
        self, app, target, result, *, name="successor", runtime=None, cancel=False
    ):
        """Run the hand-off up to its modal, then answer it (or back out).

        The screen is where the user reads the report before anything is
        retired, so the test pushes it the way the app does and calls the
        callback with what the user would have chosen.
        """
        with (
            patch.object(app, "_move_cursor_to_key"),
            patch.object(monitor, "all_session_names_in_cwd", return_value=set()),
            patch.object(app, "push_screen") as push,
        ):
            app._handoff_context_ready(target, "native-id", result)
            if not push.call_args_list:
                return None
            screen, on_close = push.call_args.args[:2]
            on_close(
                None if cancel else (name, runtime or monitor.RUNTIME_CLAUDE)
            )
            return screen

    def test_handoff_starts_only_for_dormant_agent(self) -> None:
        app = monitor.MonitorApp([])
        active = self._dormant(status="idle")
        with (
            patch.object(app, "_current_entry", return_value=active),
            patch.object(app, "_handoff_context_worker") as worker,
            patch.object(app, "notify") as notify,
        ):
            app.action_handoff()

        worker.assert_not_called()
        self.assertIn("only available for dormant", notify.call_args.args[0])

        dormant = self._dormant()
        with (
            patch.object(app, "_current_entry", return_value=dormant),
            patch.object(app, "_handoff_context_worker") as worker,
            patch.object(app, "notify"),
        ):
            app.action_handoff()

        worker.assert_called_once_with(dormant, "native-id")
        self.assertTrue(app._handoff_in_progress)

    def test_generated_context_names_and_spawns_the_successor(self) -> None:
        app = monitor.MonitorApp([])
        target = self._dormant()
        result = AskResult(
            name=target.name,
            response="Completed A. Open task B.",
            error=None,
            fork_uuid="fork",
            elapsed=1,
        )
        retired = sessions.Session(
            id="retired-record",
            cwd=target.cwd,
            created_at=1,
            status="retired",
            name=target.name,
            successor="successor",
        )
        with (
            patch.object(app, "notify"),
            patch.object(app, "refresh_sessions"),
            patch.object(monitor, "sync_full") as sync,
            patch.object(
                monitor,
                "create_retired_session_record",
                return_value=retired,
            ) as retire,
            patch.object(monitor, "create_session") as create,
        ):
            self._name_successor(
                app, target, result, runtime=monitor.RUNTIME_CODEX
            )

        sync.assert_called_once_with("native-id", target.cwd)
        retire.assert_called_once_with(
            cwd=target.cwd,
            name=target.name,
            runtime=target.runtime,
            native_session_id="native-id",
            successor="successor",
        )
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["name"], "successor")
        self.assertEqual(kwargs["runtime"], monitor.RUNTIME_CODEX)
        self.assertEqual(create.call_args.args[:2], (target.cwd, []))
        prompt = kwargs["initial_prompt"]
        self.assertIn('successor to the retired agent "predecessor"', prompt)
        self.assertIn("Completed A. Open task B.", prompt)
        self.assertIn(
            'ask_agent(target_name="predecessor", question="...")', prompt
        )

    def test_the_successor_is_filed_into_the_predecessors_folder(self) -> None:
        # A handoff keeps the lineage together the same way a spawn does.
        app = monitor.MonitorApp([])
        target = self._dormant()
        result = AskResult(target.name, "handoff", None, "fork", 1)
        session = sessions.Session(
            id="successor-session", cwd=target.cwd, created_at=1,
            status="idle", name="successor",
        )
        with (
            patch.object(app, "notify"),
            patch.object(app, "refresh_sessions"),
            patch.object(monitor, "sync_full"),
            patch.object(monitor, "create_retired_session_record"),
            patch.object(monitor, "create_session", return_value=session),
            patch.object(
                app.group_state, "group_of", return_value="g-predecessor"
            ),
            patch.object(monitor.groups, "assign_agent") as assign,
        ):
            self._name_successor(app, target, result)

        assign.assert_called_once_with(app.monitor_cwd, "successor", "g-predecessor")

    def test_the_modal_shows_the_report_and_the_next_name_in_the_line(self) -> None:
        # The report is the successor's whole inherited memory, so it is on
        # screen before anything is retired — and the name follows the
        # lineage rather than the random pool.
        app = monitor.MonitorApp([])
        target = self._dormant()
        result = AskResult(target.name, "Completed A. Open task B.", None, "fork", 1)

        with (
            patch.object(app, "notify"),
            patch.object(monitor, "create_session"),
            patch.object(monitor, "create_retired_session_record"),
            patch.object(monitor, "sync_full"),
        ):
            screen = self._name_successor(app, target, result, cancel=True)

        self.assertIsInstance(screen, monitor.HandoffSuccessorScreen)
        self.assertEqual(screen.handoff_context, "Completed A. Open task B.")
        self.assertEqual(screen.predecessor, "predecessor")
        self.assertEqual(screen.default_name, "predecessor II")
        self.assertEqual(screen.predecessor_runtime, monitor.RUNTIME_CLAUDE)

    def test_the_default_name_walks_past_generations_already_in_use(self) -> None:
        app = monitor.MonitorApp([])
        target = self._dormant()
        target.name = "predecessor II"
        result = AskResult(target.name, "handoff", None, "fork", 1)

        with (
            patch.object(app, "notify"),
            patch.object(app, "push_screen") as push,
            patch.object(
                monitor,
                "all_session_names_in_cwd",
                return_value={"predecessor", "predecessor II", "predecessor III"},
            ),
        ):
            app._handoff_context_ready(target, "native-id", result)

        self.assertEqual(push.call_args.args[0].default_name, "predecessor IV")

    def test_handoff_ask_uses_approved_prompt_and_native_fork(self) -> None:
        app = monitor.MonitorApp([])
        target = self._dormant()
        answer = AskResult(target.name, "context", None, "fork", 1)
        app.call_from_thread = MagicMock()
        with patch.object(monitor, "ask_one", return_value=answer) as ask:
            app._handoff_context_worker.__wrapped__(app, target, "native-id")

        kwargs = ask.call_args.kwargs
        self.assertEqual(kwargs["question"], monitor._HANDOFF_PROMPT)
        self.assertEqual(kwargs["target"].native_session_id, "native-id")
        self.assertEqual(kwargs["target"].runtime, monitor.RUNTIME_CLAUDE)
        self.assertEqual(kwargs["timeout"], monitor._HANDOFF_TIMEOUT_SECONDS)

    def test_escape_on_the_successor_modal_cancels_the_handoff(self) -> None:
        app = monitor.MonitorApp([])
        target = self._dormant()
        result = AskResult(target.name, "handoff", None, "fork", 1)
        with (
            patch.object(app, "notify") as notify,
            patch.object(monitor, "sync_full") as sync,
            patch.object(monitor, "create_retired_session_record") as retire,
            patch.object(monitor, "create_session") as create,
        ):
            self._name_successor(app, target, result, cancel=True)

        sync.assert_not_called()
        retire.assert_not_called()
        create.assert_not_called()
        self.assertIn("cancelled", notify.call_args.args[0])

    def test_failed_successor_start_rolls_predecessor_back_to_dormant(self) -> None:
        app = monitor.MonitorApp([])
        target = self._dormant()
        target.runtime = monitor.RUNTIME_CODEX
        target.claude_uuid = None
        result = AskResult(target.name, "handoff", None, "fork", 1)
        retired = sessions.Session(
            id="retired-record",
            cwd=target.cwd,
            created_at=1,
            status="retired",
            name=target.name,
            runtime=target.runtime,
            successor="successor",
        )
        with (
            patch.object(app, "notify") as notify,
            patch.object(app, "refresh_sessions"),
            patch.object(monitor, "sync_full") as sync,
            patch.object(
                monitor,
                "create_retired_session_record",
                return_value=retired,
            ),
            patch.object(
                monitor, "create_session", side_effect=RuntimeError("startup failed")
            ),
            patch.object(monitor, "remove_retired_session_record") as rollback,
        ):
            self._name_successor(
                app, target, result, runtime=monitor.RUNTIME_PI
            )

        sync.assert_not_called()
        rollback.assert_called_once_with(retired)
        self.assertIn("startup failed", notify.call_args.args[0])


class RetiredRecordTests(unittest.TestCase):
    def test_dormant_native_session_becomes_non_running_retired_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            state_dir = root / "state" / "sessions"
            with (
                patch.object(sessions, "STATE_DIR", state_dir),
                patch.object(sessions, "short_id", return_value="retired-id"),
                patch.object(sessions, "current_host", return_value="host"),
            ):
                created = sessions.create_retired_session_record(
                    cwd=str(workspace),
                    name="predecessor",
                    runtime=sessions.RUNTIME_CODEX,
                    native_session_id="native-id",
                    successor="successor",
                )
                loaded = sessions.load_session(created.id)

        self.assertEqual(created.status, "retired")
        self.assertEqual(created.runtime_session_id, "native-id")
        self.assertEqual(loaded.status, "retired")
        self.assertEqual(loaded.successor, "successor")
        self.assertFalse(
            sessions.DashboardRow(
                name=loaded.name or "predecessor",
                cwd=loaded.cwd,
                status=loaded.status,
                status_ts=loaded.status_ts,
                session=loaded,
            ).is_live
        )


class RetiredCompactionTests(unittest.TestCase):
    def test_retired_compaction_never_syncs_into_frozen_full_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            full = root / "full.jsonl"
            full.write_text("frozen\n")
            runner = compact_runner.CompactRunner(
                claude_uuid="old-native-id",
                cwd="/workspace",
                live_jsonl=root / "live.jsonl",
                total_budget=0,
                summarize_only=True,
                own_lock=False,
                sync_full_before_build=False,
            )
            runner.paths = compact_runner.CompactPaths(
                cache_root=root / "trees",
                lockfile=root / "trees" / ".worker.lock",
                log=root / "trees" / "worker.log",
            )
            logs: list[str] = []
            done: list[tuple[bool, str]] = []
            with (
                patch.object(compact_runner, "full_jsonl_path", return_value=full),
                patch.object(compact_runner, "sync_full") as sync,
                patch.object(runner, "_count_total_turns", return_value=0),
                patch.object(runner, "_count_cached_trees", return_value=0),
            ):
                runner.run(
                    logs.append,
                    lambda *_: None,
                    lambda *_: None,
                    lambda ok, message: done.append((ok, message)),
                )

        sync.assert_not_called()
        self.assertTrue(any("full.jsonl is frozen" in line for line in logs))
        self.assertTrue(done[0][0])


if __name__ == "__main__":
    unittest.main()
