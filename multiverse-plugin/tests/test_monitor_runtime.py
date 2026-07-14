from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import DataTable, Input, RadioButton

from src import monitor


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


if __name__ == "__main__":
    unittest.main()
