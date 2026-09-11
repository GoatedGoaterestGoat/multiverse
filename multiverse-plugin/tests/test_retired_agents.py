from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from textual.app import App
from textual.widgets import Static

from src import compact_runner, groups, monitor, server, sessions
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
    def setUp(self) -> None:
        # A successor is filed into the workspace's group store, so keep
        # that off the real ~/.multiverse when this file runs on its own.
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_root = os.environ.get("MULTIVERSE_ROOT")
        os.environ["MULTIVERSE_ROOT"] = self._tmp.name
        self.addCleanup(self._restore_root)
        self.enterContext(patch.object(sessions, "STATE_DIR", Path(self._tmp.name) / "sessions"))
        self.enterContext(patch.object(monitor.MonitorApp, "refresh_sessions"))
        self.enterContext(patch.object(monitor.MonitorApp, "_move_cursor_to_key"))

    def _restore_root(self) -> None:
        if self._prev_root is None:
            os.environ.pop("MULTIVERSE_ROOT", None)
        else:
            os.environ["MULTIVERSE_ROOT"] = self._prev_root
        self._tmp.cleanup()

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
        if app._handoff_session is None:
            self._reserve(app, target)
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

    def _reserve(self, app, target):
        app._handoff_session = sessions.create_retiring_session_record(
            target.cwd, target.name, target.runtime, "native-id"
        )
        app._handoff_in_progress = True
        return app._handoff_session

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
        self.assertEqual(sessions.load_session(app._handoff_session.id).status, "retiring")

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
        retired = self._reserve(app, target)
        with (
            patch.object(app, "notify"),
            patch.object(app, "refresh_sessions"),
            patch.object(monitor, "sync_full") as sync,
            patch.object(
                monitor,
                "mark_session_retired",
                wraps=sessions.mark_session_retired,
            ) as retire,
            patch.object(monitor, "create_session") as create,
        ):
            self._name_successor(
                app, target, result, runtime=monitor.RUNTIME_CODEX
            )

        sync.assert_called_once_with("native-id", target.cwd)
        retire.assert_called_once_with(retired, "successor")
        self.assertEqual(sessions.load_session(retired.id).status, "retired")
        self.assertIsNone(app._handoff_session)
        self.assertFalse(app._handoff_in_progress)
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

    def _hand_off(self, app, target):
        """Run a hand-off through to a created successor."""
        result = AskResult(target.name, "handoff", None, "fork", 1)
        session = sessions.Session(
            id="successor-session", cwd=target.cwd, created_at=1,
            status="idle", name="successor",
        )
        with (
            patch.object(app, "notify"),
            patch.object(app, "refresh_sessions"),
            patch.object(monitor, "sync_full"),
            patch.object(monitor, "create_session", return_value=session),
        ):
            self._name_successor(app, target, result)

    def test_a_successor_joins_the_folder_its_predecessor_was_in(self) -> None:
        # Filed exactly like a spawn: the folder already exists, so the
        # successor joins it rather than starting a second one.
        app = monitor.MonitorApp([])
        app.monitor_cwd = "/workspace"
        target = self._dormant()
        folder = groups.create_group(app.monitor_cwd, "review-crew")
        groups.assign_agent(app.monitor_cwd, target.name, folder.id)
        app.group_state = groups.load(app.monitor_cwd)

        self._hand_off(app, target)

        state = groups.load(app.monitor_cwd)
        self.assertEqual(list(state.groups), [folder.id])
        self.assertEqual(state.group_of("successor"), folder.id)
        self.assertEqual(state.group_of(target.name), folder.id)

    def test_a_loose_predecessor_gets_a_folder_holding_the_pair(self) -> None:
        # No folder yet, so the succession makes one — named after the
        # predecessor, with the retired agent and its successor inside.
        app = monitor.MonitorApp([])
        app.monitor_cwd = "/workspace"
        target = self._dormant()

        self._hand_off(app, target)

        state = groups.load(app.monitor_cwd)
        self.assertEqual(len(state.groups), 1)
        gid = next(iter(state.groups))
        self.assertEqual(state.groups[gid].name, "predecessor")
        self.assertEqual(state.group_of("predecessor"), gid)
        self.assertEqual(state.group_of("successor"), gid)

    def test_a_cancelled_handoff_files_nothing(self) -> None:
        app = monitor.MonitorApp([])
        app.monitor_cwd = "/workspace"
        target = self._dormant()
        result = AskResult(target.name, "handoff", None, "fork", 1)

        with (
            patch.object(app, "notify"),
            patch.object(monitor, "create_session"),
            patch.object(monitor, "sync_full"),
        ):
            self._name_successor(app, target, result, cancel=True)

        self.assertEqual(groups.load(app.monitor_cwd).groups, {})

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
        self._reserve(app, target)

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
            patch.object(monitor, "mark_session_retired") as retire,
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
        retired = self._reserve(app, target)
        with (
            patch.object(app, "notify") as notify,
            patch.object(app, "refresh_sessions"),
            patch.object(monitor, "sync_full") as sync,
            patch.object(
                monitor, "create_session", side_effect=RuntimeError("startup failed")
            ),
            patch.object(
                monitor, "remove_retired_session_record",
                wraps=sessions.remove_retired_session_record,
            ) as rollback,
        ):
            self._name_successor(
                app, target, result, runtime=monitor.RUNTIME_PI
            )

        sync.assert_not_called()
        rollback.assert_called_once_with(retired)
        self.assertIsNone(sessions.load_session(retired.id))
        self.assertIn("startup failed", notify.call_args.args[0])

    def test_generation_failure_releases_the_reservation(self) -> None:
        for response, error in ((None, "fork failed"), ("   ", None)):
            with self.subTest(response=response, error=error):
                app = monitor.MonitorApp([])
                target = self._dormant()
                reservation = self._reserve(app, target)
                with patch.object(app, "notify"), patch.object(app, "push_screen") as push:
                    app._handoff_context_ready(
                        target, "native-id", AskResult(target.name, response, error, "fork", 1)
                    )
                push.assert_not_called()
                self.assertIsNone(sessions.load_session(reservation.id))
                self.assertFalse(app._handoff_in_progress)

    def test_modal_keeps_retiring_until_cancelled(self) -> None:
        app = monitor.MonitorApp([])
        target = self._dormant()
        reservation = self._reserve(app, target)
        with (
            patch.object(app, "notify"), patch.object(app, "push_screen") as push,
            patch.object(monitor, "all_session_names_in_cwd", return_value=set()),
        ):
            app._handoff_context_ready(
                target, "native-id", AskResult(target.name, "context", None, "fork", 1)
            )
            self.assertEqual(sessions.load_session(reservation.id).status, "retiring")
            self.assertTrue(app._handoff_in_progress)
            push.call_args.args[1](None)
        self.assertIsNone(sessions.load_session(reservation.id))
        self.assertFalse(app._handoff_in_progress)

    def test_dashboard_shutdown_releases_reservation_and_ignores_late_result(self) -> None:
        app = monitor.MonitorApp([])
        target = self._dormant()
        reservation = self._reserve(app, target)
        app.on_unmount()
        self.assertIsNone(sessions.load_session(reservation.id))
        with patch.object(app, "push_screen") as push:
            app._handoff_context_ready(
                target, "native-id", AskResult(target.name, "context", None, "fork", 1)
            )
        push.assert_not_called()

    def test_late_worker_result_after_dashboard_exit_is_harmless(self) -> None:
        app = monitor.MonitorApp([])
        with (
            patch.object(monitor, "ask_one", return_value=AskResult("old", "context", None, "fork", 1)),
            patch.object(app, "call_from_thread", side_effect=RuntimeError("App is not running")),
        ):
            app._handoff_context_worker.__wrapped__(app, self._dormant(), "native-id")

    def test_retiring_blocks_attach_handoff_kill_and_compaction(self) -> None:
        app = monitor.MonitorApp([])
        target = self._dormant()
        reservation = self._reserve(app, target)
        target.status = "retiring"
        target.session = reservation
        with (
            patch.object(app, "_current_entry", return_value=target),
            patch.object(app, "notify"), patch.object(app, "_attach_to") as attach,
            patch.object(app, "_spawn_resumed") as resume,
            patch.object(app, "_handoff_context_worker") as worker,
            patch.object(app, "_begin_session_kill") as kill,
            patch.object(app, "push_screen") as push,
        ):
            app.action_primary()
            app.action_handoff()
            app.action_kill()
            app.action_compact()
        for blocked in (attach, resume, worker, kill, push):
            blocked.assert_not_called()


class HandoffSuccessorScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_renders_literally_and_escape_still_cancels(self) -> None:
        reports = (
            "Earlier bug: `[opacity=0 / 0.1)` as markup and crashed the entire TUI on `on_mount`\n",
            "[bold]literal tags[/bold]\n[1, 2] and [unfinished\nC:\\work\\[notes]\\",
            "",
        )
        for report in reports:
            with self.subTest(report=report):
                app = App()
                results = []
                async with app.run_test(size=(213, 51)) as pilot:
                    screen = monitor.HandoffSuccessorScreen(
                        "/workspace", "SecondBrain", "claude", report, "SecondBrain II"
                    )
                    app.push_screen(screen, results.append)
                    await pilot.pause()
                    displayed = screen.query_one("#handoff-context", Static)
                    self.assertEqual(displayed.render().plain, report or "(empty report)")
                    self.assertEqual(screen.handoff_context, report)
                    await pilot.press("escape")
                    await pilot.pause()
                self.assertEqual(results, [None])


class HandoffShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_q_during_generation_cleans_up_without_opening_successor_modal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            started, finish, delivered = threading.Event(), threading.Event(), threading.Event()
            app = monitor.MonitorApp([])
            target = monitor.DashboardRow(
                name="old", cwd=str(root), status="dormant", status_ts=1,
                runtime="codex", session_id="native-id",
            )

            def ask(**_kwargs):
                started.set()
                finish.wait(5)
                return AskResult("old", "context", None, "fork", 1)

            original_call = app.call_from_thread

            def deliver(*args):
                try:
                    return original_call(*args)
                finally:
                    delivered.set()

            with (
                patch.object(sessions, "STATE_DIR", root / "sessions"),
                patch.object(monitor.MonitorApp, "on_mount"),
                patch.object(app, "refresh_sessions"),
                patch.object(app, "_move_cursor_to_key"),
                patch.object(app, "_current_entry", return_value=target),
                patch.object(monitor, "ask_one", side_effect=ask),
                patch.object(app, "call_from_thread", side_effect=deliver),
            ):
                try:
                    async with app.run_test() as pilot:
                        app.action_handoff()
                        self.assertTrue(await asyncio.to_thread(started.wait, 2))
                        reservation = app._handoff_session
                        self.assertEqual(sessions.load_session(reservation.id).status, "retiring")
                        await pilot.press("q")
                    self.assertIsNone(sessions.load_session(reservation.id))
                    with patch.object(app, "push_screen") as push:
                        finish.set()
                        self.assertTrue(await asyncio.to_thread(delivered.wait, 2))
                    push.assert_not_called()
                finally:
                    finish.set()


class RetiringRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.object(sessions, "STATE_DIR", self.root / "sessions"))
        self.enterContext(patch.object(sessions, "current_host", return_value="local"))
        self.enterContext(patch.dict(os.environ, {"MULTIVERSE_PLUGIN_ROOT": str(self.root)}))

    def _create(self, runtime="claude"):
        return sessions.create_retiring_session_record(str(self.root), "old", runtime, "native-id")

    def test_all_runtimes_reserve_name_and_native_id_without_starting_runtime(self) -> None:
        for runtime in sessions.RUNTIMES:
            with self.subTest(runtime=runtime), patch.object(sessions.subprocess, "run") as run:
                record = self._create(runtime)
                loaded = sessions.load_session(record.id)
                self.assertEqual(loaded.runtime_session_id, "native-id")
                self.assertEqual(loaded.status, "retiring")
                self.assertFalse(sessions._correct_local_liveness(loaded))
                self.assertIsNone(loaded.successor)
                with self.assertRaisesRegex(RuntimeError, "claimed"):
                    self._create(runtime)
                with self.assertRaisesRegex(RuntimeError, "claimed"):
                    sessions.create_session(
                        str(self.root), [], name="renamed", runtime=runtime, resumed_from="native-id"
                    )
                run.assert_not_called()
                sessions.remove_retiring_session_record(record)
                # Cancellation also releases claims: another handoff can start.
                sessions.remove_retiring_session_record(self._create(runtime))

    def test_retiring_is_listed_but_not_live(self) -> None:
        record = self._create()
        row = sessions.DashboardRow(
            name="old", cwd=str(self.root), status="retiring", status_ts=1, session=record
        )
        self.assertFalse(row.is_live)
        with (
            patch.object(server, "_resolve_self_identity", return_value=("caller", str(self.root))),
            patch.object(server, "list_named_agents_in_cwd", return_value=[row]),
        ):
            rendered = server.list_agents()
        self.assertIn("(0 live)", rendered)
        self.assertIn("retiring", rendered)
        row.status = "compacting"
        self.assertFalse(row.is_live)
        with (
            patch.object(server, "_resolve_self_identity", return_value=("caller", str(self.root))),
            patch.object(server, "list_named_agents_in_cwd", return_value=[row]),
        ):
            self.assertIn("(0 live)", server.list_agents())

    def test_late_status_writes_cannot_revive_retiring_agent(self) -> None:
        record = self._create()
        script = Path(__file__).resolve().parents[1] / "scripts" / "set-status.sh"
        for status in ("idle", "working", "waiting", "ended", "ending"):
            sessions.write_status_path(record.status_path, status)
            subprocess.run(
                [str(script), status], check=True,
                env={**os.environ, "MULTIVERSE_STATUS": str(record.status_path)},
            )
            self.assertEqual(sessions.load_session(record.id).status, "retiring")
        sessions.mark_session_retired(record, "successor")
        self.assertEqual(sessions.load_session(record.id).status, "retired")
        self.assertEqual(sessions.load_session(record.id).successor, "successor")
        with self.assertRaises(ValueError):
            sessions.remove_retiring_session_record(record)

    def test_recovery_only_releases_dead_local_owners(self) -> None:
        record = self._create()
        with patch.object(sessions, "_pid_alive", return_value=True):
            sessions.recover_abandoned_handoffs(str(self.root))
        self.assertIsNotNone(sessions.load_session(record.id))
        with (
            patch.object(sessions, "current_host", return_value="other-host"),
            patch.object(sessions, "_pid_alive", return_value=False),
        ):
            sessions.recover_abandoned_handoffs(str(self.root))
        self.assertIsNotNone(sessions.load_session(record.id))
        with patch.object(sessions, "_pid_alive", return_value=False):
            sessions.recover_abandoned_handoffs("/another-workspace")
            self.assertIsNotNone(sessions.load_session(record.id))
            sessions.recover_abandoned_handoffs(str(self.root))
        self.assertIsNone(sessions.load_session(record.id))


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
