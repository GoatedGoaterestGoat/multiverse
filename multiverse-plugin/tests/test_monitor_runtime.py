from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Input, RadioButton

from src import monitor


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
        with (
            patch.object(
                monitor,
                "list_pending_requests",
                return_value=[
                    {"caller_tmux_session": "multiverse-caller"},
                ],
            ),
            patch.object(monitor.subprocess, "run") as run,
        ):
            guard._run()

        run.assert_called_once_with(
            ["tmux", "detach-client", "-s", "multiverse-caller"],
            stdout=monitor.subprocess.DEVNULL,
            stderr=monitor.subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        self.assertTrue(guard.forced_detach)


class RuntimeModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_modal_returns_codex_selection(self) -> None:
        results: list[tuple[str, str] | None] = []
        with tempfile.TemporaryDirectory() as cwd, patch.object(
            monitor, "all_session_names_in_cwd", return_value=set()
        ):
            app = App()
            async with app.run_test() as pilot:
                screen = monitor.NewSessionScreen(cwd)
                app.push_screen(screen, results.append)
                await pilot.pause()

                screen.query_one("#new-name", Input).value = "codex-agent"
                screen.query_one("#new-runtime-codex", RadioButton).value = True
                await pilot.press("enter")
                await pilot.pause()

        self.assertEqual(
            results, [("codex-agent", monitor.RUNTIME_CODEX)]
        )

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
            results, [("codex-child", monitor.RUNTIME_CODEX)]
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
                callbacks[0](("codex-child", monitor.RUNTIME_CODEX))

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
                patch.dict(
                    "os.environ", {"MULTIVERSE_ORIG_CWD": "/workspace"}
                ),
            ):
                app = monitor.MonitorApp([])
                async with app.run_test() as pilot:
                    await pilot.press("k")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, monitor.ConfirmDeleteScreen)
                    await pilot.press("enter")
                    await pilot.pause()

        delete_thread.assert_called_once_with("codex-thread")


if __name__ == "__main__":
    unittest.main()
