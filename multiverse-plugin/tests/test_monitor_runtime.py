from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, PropertyMock, patch

from textual import events
from textual.app import App
from textual.widgets import DataTable, Input, RadioButton

from src import monitor


class DashboardRuntimeRenderingTests(unittest.TestCase):
    def test_codex_build_column_displays_not_applicable(self) -> None:
        row = monitor.DashboardRow(
            name="codex-agent",
            cwd="/workspace",
            status="idle",
            status_ts=1,
            runtime=monitor.RUNTIME_CODEX,
            session_id="codex-thread",
        )
        app = monitor.MonitorApp([])
        app.entries = [row]
        table = MagicMock()
        table.cursor_row = 0

        with (
            patch.object(app, "query_one", return_value=table),
            patch.object(monitor, "pending_inbox_count", return_value=0),
            patch.object(monitor, "auto_build_enabled") as build_enabled,
        ):
            app._redraw_table()

        build_enabled.assert_not_called()
        self.assertEqual(table.add_row.call_args.args[4], "[dim]-[/]")


class DashboardEndingTests(unittest.TestCase):
    def _session(self) -> monitor.Session:
        return monitor.Session(
            id="wrapper-id",
            cwd="/workspace",
            created_at=1,
            status="ending",
            status_ts=2,
            name="agent",
            runtime=monitor.RUNTIME_CODEX,
        )

    def test_enter_refuses_to_attach_to_ending_session(self) -> None:
        session = self._session()
        row = monitor.DashboardRow(
            name="agent",
            cwd="/workspace",
            status="ending",
            status_ts=2,
            runtime=monitor.RUNTIME_CODEX,
            session=session,
        )
        app = monitor.MonitorApp([])
        with (
            patch.object(app, "_current_entry", return_value=row),
            patch.object(app, "_attach_to") as attach,
            patch.object(app, "notify") as notify,
        ):
            app.action_primary()

        attach.assert_not_called()
        self.assertIn("attach is disabled", notify.call_args.args[0])

    def test_internal_reattach_path_also_refuses_ending_session(self) -> None:
        session = self._session()
        app = monitor.MonitorApp([])
        app.sessions = [session]
        with (
            patch.object(monitor, "_SpawnAttachGuard") as guard,
            patch.object(monitor, "attach_tmux_session") as attach,
            patch.object(app, "notify") as notify,
        ):
            app._attach_to(session.tmux_name)

        guard.assert_not_called()
        attach.assert_not_called()
        self.assertIn("attach is disabled", notify.call_args.args[0])

    def test_kill_launches_detached_teardown_before_ending_refresh(self) -> None:
        session = self._session()
        session.status = "idle"
        events: list[str] = []
        callbacks = []
        proc = MagicMock()
        app = monitor.MonitorApp([])

        def mark(_session, *, force=False):
            events.append("mark")

        def refresh():
            events.append("refresh")

        def launch(_session, *, delay):
            events.append(f"launch:{delay}")
            return proc

        def schedule(callback, *args):
            events.append("schedule")
            callbacks.append(lambda: callback(*args))

        with (
            patch.object(monitor, "mark_session_ending", side_effect=mark),
            patch.object(app, "_launch_session_teardown", side_effect=launch),
            patch.object(app, "refresh_sessions", side_effect=refresh),
            patch.object(app, "call_after_refresh", side_effect=schedule),
            patch.object(
                app,
                "_watch_session_teardown",
                side_effect=lambda *_args: events.append("watcher"),
            ) as watcher,
            patch.object(monitor, "_ENDING_MIN_VISIBLE_SECONDS", 0.25),
        ):
            app._begin_session_kill(session, "Killed wrapper-id")

            self.assertEqual(
                events, ["mark", "launch:0.25", "refresh", "schedule"]
            )
            watcher.assert_not_called()

            callbacks[0]()

        self.assertEqual(
            events,
            ["mark", "launch:0.25", "refresh", "schedule", "watcher"],
        )

    def test_recovery_relaunches_local_ending_session_without_delay(self) -> None:
        session = self._session()
        app = monitor.MonitorApp([])
        app.sessions = [session]
        with (
            patch.object(
                type(session),
                "ending_marker_path",
                new_callable=PropertyMock,
                return_value=MagicMock(is_file=MagicMock(return_value=True)),
            ),
            patch.object(monitor, "current_host", return_value=session.host),
            patch.object(app, "_launch_session_teardown") as launch,
        ):
            app._recover_ending_sessions()

        launch.assert_called_once_with(session, delay=0.0)

    def test_detached_teardown_survives_dashboard_process(self) -> None:
        session = self._session()
        app = monitor.MonitorApp([])
        with (
            patch.object(monitor, "STATE_DIR", Path("/state/sessions")),
            patch.object(monitor.subprocess, "Popen") as popen,
        ):
            app._launch_session_teardown(session, delay=0.25)

        command = popen.call_args.args[0]
        self.assertEqual(
            command[:3],
            [monitor.sys.executable, "-m", "src.session_teardown"],
        )
        self.assertIn("wrapper-id", command)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIs(popen.call_args.kwargs["stdin"], monitor.subprocess.DEVNULL)
        self.assertEqual(
            popen.call_args.kwargs["env"]["MULTIVERSE_ROOT"], "/state"
        )

    def test_k_retries_local_ending_session(self) -> None:
        session = self._session()
        row = monitor.DashboardRow(
            name="agent",
            cwd="/workspace",
            status="ending",
            status_ts=2,
            runtime=monitor.RUNTIME_CODEX,
            session=session,
        )
        app = monitor.MonitorApp([])
        with (
            patch.object(app, "_current_entry", return_value=row),
            patch.object(app, "_start_session_teardown") as start,
        ):
            app.action_kill()

        start.assert_called_once_with(
            session, "Finished teardown for wrapper-id", delay=0.0
        )

    def test_kill_worker_completion_refreshes_success_and_failure(self) -> None:
        app = monitor.MonitorApp([])
        with (
            patch.object(app, "notify") as notify,
            patch.object(app, "refresh_sessions") as refresh,
        ):
            app._finish_session_kill("Killed wrapper-id", None)
            app._finish_session_kill("Killed wrapper-id", "signal failed")

        self.assertEqual(notify.call_count, 2)
        self.assertEqual(notify.call_args_list[0].args[0], "Killed wrapper-id")
        self.assertIn("signal failed", notify.call_args_list[1].args[0])
        self.assertEqual(notify.call_args_list[1].kwargs["severity"], "error")
        self.assertEqual(refresh.call_count, 2)


class DashboardEndingAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_teardown_keeps_ui_live_in_ending_state(self) -> None:
        session = monitor.Session(
            id="claude-wrapper",
            cwd="/workspace",
            created_at=1,
            status="idle",
            status_ts=1,
            name="claude-agent",
            runtime=monitor.RUNTIME_CLAUDE,
        )
        kill_started = threading.Event()
        release_kill = threading.Event()

        def rows(items, cwd=None, **_kwargs):
            current = items[0]
            return [
                monitor.DashboardRow(
                    name=current.name or "claude-agent",
                    cwd=current.cwd,
                    status=current.status,
                    status_ts=current.status_ts,
                    runtime=current.runtime,
                    session=current,
                )
            ]

        def mark_ending(current, *, force=False):
            current.status = "ending"
            current.status_ts = 2

        class FakeProcess:
            def wait(self):
                kill_started.set()
                release_kill.wait(timeout=2)
                session.status = "ended"
                session.status_ts = 3
                return 0

        def launch(_current, *, delay):
            self.assertEqual(delay, 0.01)
            kill_started.set()
            return FakeProcess()

        with patch.dict("os.environ", {"MULTIVERSE_ORIG_CWD": "/workspace"}):
            app = monitor.MonitorApp([])
            with (
                patch.object(monitor, "list_sessions", return_value=[session]),
                patch.object(monitor, "build_dashboard_rows", side_effect=rows),
                patch.object(monitor, "list_pending_requests", return_value=[]),
                patch.object(monitor.web_share, "status", return_value=None),
                patch.object(monitor, "pending_inbox_count", return_value=0),
                patch.object(
                    monitor, "foreign_host_still_reported_live", return_value=False
                ),
                patch.object(
                    monitor, "mark_session_ending", side_effect=mark_ending
                ),
                patch.object(
                    app, "_launch_session_teardown", side_effect=launch
                ),
                patch.object(monitor, "_ENDING_MIN_VISIBLE_SECONDS", 0.01),
                patch.object(monitor, "recover_abandoned_handoffs"),
                patch.object(app, "_vouch_for_visible"),
                patch.object(app, "_tick_host_liveness"),
            ):
                try:
                    async with app.run_test() as pilot:
                        for _ in range(100):
                            if app.entries:
                                break
                            await asyncio.sleep(0.01)
                        self.assertTrue(app.entries)
                        await pilot.press("k")
                        for _ in range(20):
                            if kill_started.is_set():
                                break
                            await asyncio.sleep(0.01)

                        self.assertTrue(kill_started.is_set())
                        self.assertEqual(app.entries[0].status, "ending")
                        self.assertTrue(app.is_running)
                        release_kill.set()
                        for _ in range(20):
                            if app.entries[0].status == "ended":
                                break
                            await asyncio.sleep(0.01)
                        self.assertEqual(app.entries[0].status, "ended")
                finally:
                    release_kill.set()


class SpawnAttachGuardTests(unittest.TestCase):
    def test_ignores_request_from_another_attached_session(self) -> None:
        guard = monitor._SpawnAttachGuard("/workspace", "multiverse-caller")

        class StopAfterOnePoll:
            def is_set(self) -> bool:
                return False

            def wait(self, _timeout: float) -> bool:
                return True

        guard._stop = StopAfterOnePoll()
        with (
            patch.object(
                monitor,
                "list_pending_requests",
                return_value=[
                    {"caller_tmux_session": "multiverse-other"},
                ],
            ),
            patch.object(monitor.subprocess, "run") as run,
        ):
            guard._run()

        run.assert_not_called()
        self.assertFalse(guard.forced_detach)

    def test_detaches_for_request_from_guarded_session(self) -> None:
        guard = monitor._SpawnAttachGuard("/workspace", "multiverse-caller")
        guard.set_client_pid(4242)
        with (
            patch.object(
                monitor,
                "list_pending_requests",
                return_value=[
                    {"caller_tmux_session": "multiverse-caller"},
                ],
            ),
            patch.object(
                monitor.subprocess,
                "run",
                side_effect=[
                    CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=(
                            "4242\t/dev/pts/9\tmultiverse-caller\n"
                            "5000\t/dev/pts/10\tmultiverse-other\n"
                        ),
                    ),
                    CompletedProcess(args=[], returncode=0),
                ],
            ) as run,
        ):
            guard._run()

        self.assertEqual(run.call_count, 2)
        run.assert_any_call(
            [
                "tmux",
                "list-clients",
                "-F",
                "#{client_pid}\t#{client_name}\t#{session_name}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        run.assert_any_call(
            ["tmux", "detach-client", "-t", "/dev/pts/9"],
            stdout=monitor.subprocess.DEVNULL,
            stderr=monitor.subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        self.assertTrue(guard.forced_detach)

    def test_detach_retries_when_attach_client_is_not_registered_yet(self) -> None:
        guard = monitor._SpawnAttachGuard("/workspace", "multiverse-caller")
        guard.set_client_pid(4242)
        listed = CompletedProcess(
            args=[],
            returncode=0,
            stdout="5000\t/dev/pts/10\tmultiverse-caller\n",
        )

        with patch.object(monitor.subprocess, "run", return_value=listed) as run:
            self.assertFalse(guard._detach_client())

        run.assert_called_once()
        self.assertFalse(guard.forced_detach)


class RuntimeModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_agent_draft_cycles_runtime_with_tab(self) -> None:
        # The new-session modal is gone: the runtime is picked on the draft
        # row itself, so the coverage moves with it.
        app = monitor.MonitorApp([])
        app._draft = monitor._DraftAgent(
            buffer="agent",
            cursor=5,
            select_all=False,
            runtime=monitor.RUNTIME_CLAUDE,
            group_id=None,
        )
        with patch.object(app, "_redraw_table"):
            app._draft_key(events.Key("tab", None))
            self.assertEqual(app._draft.runtime, monitor.RUNTIME_CODEX)
            app._draft_key(events.Key("tab", None))
            self.assertEqual(app._draft.runtime, monitor.RUNTIME_PI)

    async def test_new_agent_draft_creates_the_selected_runtime(self) -> None:
        app = monitor.MonitorApp([])
        app.monitor_cwd = "/workspace"
        app._draft = monitor._DraftAgent(
            buffer="pi-agent",
            cursor=8,
            select_all=False,
            runtime=monitor.RUNTIME_PI,
            group_id=None,
        )
        with (
            patch.object(monitor, "all_session_names_in_cwd", return_value=set()),
            patch.object(monitor, "create_session") as create,
            patch.object(app, "refresh_sessions"),
            patch.object(app, "_move_cursor_to_key"),
            patch.object(app, "notify"),
        ):
            app._commit_draft()

        self.assertEqual(create.call_args.kwargs["name"], "pi-agent")
        self.assertEqual(create.call_args.kwargs["runtime"], monitor.RUNTIME_PI)
        self.assertIsNone(app._draft)

    async def test_spawn_modal_shows_caller_runtime_and_returns_codex(self) -> None:
        results: list[tuple[str, str] | None] = []
        request = {
            "from": "claude-caller",
            "instruction": "Investigate the failing build.",
            "default_name": "build-agent",
            "caller_runtime": monitor.RUNTIME_CLAUDE,
        }
        with tempfile.TemporaryDirectory() as cwd, patch.object(
            monitor, "all_session_names_in_cwd", return_value=set()
        ):
            app = App()
            async with app.run_test() as pilot:
                screen = monitor.SpawnRequestScreen(cwd, request)
                app.push_screen(screen, results.append)
                await pilot.pause()

                header = screen.query_one("#spawn-header")
                self.assertIn("(claude)", str(header.content))
                self.assertTrue(
                    screen.query_one(
                        "#spawn-runtime-claude", RadioButton
                    ).value
                )
                screen.query_one("#spawn-name", Input).value = "codex-child"
                screen.query_one(
                    "#spawn-runtime-codex", RadioButton
                ).value = True
                await pilot.press("enter")
                await pilot.pause()

        self.assertEqual(
            results,
            [
                monitor.SpawnDecision(
                    accepted=True,
                    name="codex-child",
                    runtime=monitor.RUNTIME_CODEX,
                )
            ],
        )

    async def test_spawn_modal_resolution_includes_selected_runtime(self) -> None:
        callbacks = []
        request = {
            "id": "request-id",
            "from": "caller",
            "instruction": "Do the work.",
            "default_name": "child",
            "caller_runtime": monitor.RUNTIME_CLAUDE,
        }
        with tempfile.TemporaryDirectory() as cwd, patch.dict(
            "os.environ", {"MULTIVERSE_ORIG_CWD": cwd}
        ):
            app = monitor.MonitorApp([])
            with (
                patch.object(
                    app,
                    "push_screen",
                    side_effect=lambda screen, callback: callbacks.append(callback),
                ),
                patch.object(app, "notify"),
                patch.object(
                    monitor, "write_resolution", return_value=True
                ) as write_resolution,
            ):
                app._show_spawn_modal(request)
                callbacks[0](
                    monitor.SpawnDecision(
                        accepted=True,
                        name="codex-child",
                        runtime=monitor.RUNTIME_CODEX,
                    )
                )

        write_resolution.assert_called_once_with(
            "request-id",
            status="accepted",
            name="codex-child",
            runtime=monitor.RUNTIME_CODEX,
        )

    async def test_resume_modal_merges_both_runtimes(self) -> None:
        results: list[tuple[str, str, str | None] | None] = []
        claude_info = {
            "uuid": "claude-thread",
            "name": "claude-agent",
            "preview": "Claude prompt",
            "mtime": 1,
            "size": 10,
        }
        codex_info = {
            "id": "codex-thread",
            "name": "codex-agent",
            "preview": "Codex prompt",
            "mtime": 2,
            "size": 10,
        }
        with (
            patch.object(
                monitor, "list_claude_sessions_in", return_value=[claude_info]
            ),
            patch.object(
                monitor, "list_codex_sessions_in", return_value=[codex_info]
            ),
            patch.object(monitor, "list_pi_sessions_in", return_value=[]),
        ):
            app = App()
            async with app.run_test() as pilot:
                screen = monitor.ResumeSessionScreen("/workspace", set())
                app.push_screen(screen, results.append)
                await pilot.pause()

                table = screen.query_one("#resume-table", DataTable)
                self.assertEqual(len(table.columns), 5)
                self.assertEqual(
                    [info["runtime"] for info in screen.sessions_info],
                    [monitor.RUNTIME_CODEX, monitor.RUNTIME_CLAUDE],
                )
                table.focus()
                await pilot.press("enter")
                await pilot.pause()

        self.assertEqual(
            results,
            [(monitor.RUNTIME_CODEX, "codex-thread", "codex-agent")],
        )

    async def test_resume_modal_includes_pi_sessions(self) -> None:
        results: list[tuple[str, str, str | None] | None] = []
        pi_info = {
            "id": "pi-thread",
            "name": "pi-agent",
            "preview": "pi prompt",
            "mtime": 3,
            "size": 10,
        }
        with (
            patch.object(monitor, "list_claude_sessions_in", return_value=[]),
            patch.object(monitor, "list_codex_sessions_in", return_value=[]),
            patch.object(monitor, "list_pi_sessions_in", return_value=[pi_info]),
        ):
            app = App()
            async with app.run_test() as pilot:
                screen = monitor.ResumeSessionScreen("/workspace", set())
                app.push_screen(screen, results.append)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()

        self.assertEqual(results, [(monitor.RUNTIME_PI, "pi-thread", "pi-agent")])

    async def test_k_confirms_and_deletes_dormant_codex_thread(self) -> None:
        row = monitor.DashboardRow(
            name="codex-agent",
            cwd="/workspace",
            status="dormant",
            status_ts=1,
            runtime=monitor.RUNTIME_CODEX,
            session_id="codex-thread",
        )
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "sessions"
            with (
                patch.object(monitor, "STATE_DIR", state_dir),
                patch.object(monitor, "list_sessions", return_value=[]),
                patch.object(monitor, "build_dashboard_rows", return_value=[row]),
                patch.object(monitor, "list_pending_requests", return_value=[]),
                patch.object(monitor.web_share, "status", return_value=None),
                patch.object(monitor, "pending_inbox_count", return_value=0),
                patch.object(monitor, "delete_codex_thread") as delete_thread,
                patch.object(monitor, "recover_abandoned_handoffs"),
                patch.object(monitor.MonitorApp, "_tick_host_liveness"),
                patch.dict(
                    "os.environ", {"MULTIVERSE_ORIG_CWD": "/workspace"}
                ),
            ):
                app = monitor.MonitorApp([])
                async with app.run_test() as pilot:
                    for _ in range(100):
                        if app.entries:
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(app.entries)
                    await pilot.press("k")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, monitor.ConfirmDeleteScreen)
                    await pilot.press("enter")
                    await pilot.pause()

        delete_thread.assert_called_once_with("codex-thread")


if __name__ == "__main__":
    unittest.main()
