from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rich.console import Console
from rich.text import Text
from textual.widgets import DataTable

from src import groups, monitor


class TokenColorTests(unittest.TestCase):
    def test_context_boundaries_and_unknown_capacity(self) -> None:
        for tokens, expected in ((0, "green"), (599, "green"), (600, "yellow"),
                                 (799, "yellow"), (800, "red"), (1001, "red")):
            self.assertEqual(monitor._context_token_color(tokens, 1000), expected)
        for tokens, capacity in ((None, 1000), (100, None), (100, 0), (100, -1)):
            self.assertEqual(monitor._context_token_color(tokens, capacity), "")

    def test_total_boundaries_and_unrounded_values(self) -> None:
        for tokens, expected in ((0, "green"), (1_499_999, "green"),
                                 (1_500_000, "yellow"), (2_499_999, "yellow"),
                                 (2_500_000, "red"), (10_000_000, "red")):
            self.assertEqual(monitor._total_token_color(tokens), expected)
        self.assertEqual(monitor._total_token_color(None), "")
        self.assertEqual(monitor._token_cell(None, "red"), "[dim]-[/]")
        self.assertEqual(monitor._token_cell(123, ""), "123")

    def test_folder_token_cells_are_blank_without_reading_member_usage(self) -> None:
        app = monitor.MonitorApp([])
        members = [monitor.DashboardRow(name=name, cwd="/test", status="idle", status_ts=0)
                   for name in ("near-limit", "mostly-empty", "unknown")]
        row = groups.DisplayRow(
            kind="group", key="grp:test", group=groups.Group("test", "Work", collapsed=True),
            agents=members, agent_count=3,
        )
        with (
            patch.object(app, "_pending_inbox", return_value=0),
            patch.object(app, "_tokens_of") as live,
            patch.object(app, "_context_window_of") as capacity,
            patch.object(app, "_total_of") as total,
        ):
            for collapsed in (False, True):
                for agents in (members, []):
                    row.group.collapsed = collapsed
                    row.agents = agents
                    row.agent_count = len(agents)
                    self.assertEqual(app._group_cells(row, 0)[6:8], ("", ""))
            live.assert_not_called()
            capacity.assert_not_called()
            total.assert_not_called()


class RenderedTokenColorTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_dashboard_cells_render_the_requested_colors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = monitor.MonitorApp([])
            app.monitor_cwd = tmp
            rows = [monitor.DashboardRow(
                name=color, cwd=tmp, status="idle", status_ts=0,
                runtime="codex", session_id=color,
            ) for color in ("green", "yellow", "red")]
            folder = groups.Group("usage", "Agents")
            app.group_state = groups.GroupState(
                groups={folder.id: folder}, members={row.name: folder.id for row in rows},
            )
            live = {"green": 599, "yellow": 600, "red": 800}
            totals = {"green": 1_499_999, "yellow": 1_500_000, "red": 2_500_000}

            def refresh():
                app.entries = rows
                app._redraw_table()

            with (
                patch.object(monitor, "STATE_DIR", Path(tmp) / "sessions"),
                patch.object(app, "refresh_sessions", side_effect=refresh),
                patch.object(app, "_recover_ending_sessions"),
                patch.object(app, "_pending_inbox", return_value=0),
                patch.object(app, "_tokens_of", side_effect=lambda e: live[e.name]),
                patch.object(app, "_total_of", side_effect=lambda e: totals[e.name]),
                patch.object(app, "_context_window_of", return_value=1000),
            ):
                async with app.run_test(size=(200, 16)) as pilot:
                    table = app.query_one(DataTable)
                    self.assertEqual(table.get_row_at(0)[6:8], ["", ""])
                    for index, row in enumerate(rows, start=1):
                        for cell in table.get_row_at(index)[6:8]:
                            text = Text.from_markup(cell)
                            style = text.get_style_at_offset(Console(), 0)
                            self.assertEqual(style.color.name, row.name)
                            self.assertFalse(style.dim)
                        self.assertEqual(next(iter(table.rows.values())).height, 1)
                    folder.collapsed = True
                    refresh()
                    await pilot.pause()
                    self.assertEqual(table.row_count, 1)
                    self.assertEqual(table.get_row_at(0)[6:8], ["", ""])


class ClaudeCapturedTokenColorTests(unittest.TestCase):
    def test_statusline_capacity_replaces_assumed_claude_window(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "claude-statusline.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            cwd = str(workspace.resolve())
            state = root / "state"
            row = monitor.DashboardRow(
                name="claude", cwd=cwd, status="idle", status_ts=0,
                runtime="claude", claude_uuid="12345678-1234-4234-9234-123456789abc",
                session_id="12345678-1234-4234-9234-123456789abc",
            )
            app = monitor.MonitorApp([])
            app.entries = [row]
            table = MagicMock()
            table.cursor_row = 0
            env = {k: v for k, v in os.environ.items() if not k.startswith("MULTIVERSE_")}
            env.update({"MULTIVERSE_ROOT": str(state), "MULTIVERSE_SESSION_CWD": cwd})
            with (
                patch.dict(os.environ, {
                    "MULTIVERSE_ROOT": str(state), "MULTIVERSE_CLAUDE_CONTEXT_WINDOW": "",
                }),
                patch.object(app, "query_one", return_value=table),
                patch.object(app, "_pending_inbox", return_value=0),
                patch.object(app, "_agent_tokens", return_value=600_000) as live,
                patch.object(app, "_agent_total", return_value=1_500_000),
                patch.object(monitor, "auto_build_enabled", return_value=False),
            ):
                for tokens, color in ((599_999, "green"), (600_000, "yellow"), (800_000, "red")):
                    live.return_value = tokens
                    app._usage = app._collect_usage(app.entries)
                    app._redraw_table()
                    self.assertEqual(table.add_row.call_args.args[6:8], (
                        f"[{color}]{tokens:,}[/]", "[yellow]1.50M[/]",
                    ))
                live.return_value = 600_000
                for capacity, color in ((1_000_000, "yellow"), (2_000_000, "green"), (750_000, "red")):
                    result = subprocess.run(
                        [sys.executable, str(script), "render"], cwd=workspace, env=env,
                        input=json.dumps({
                            "session_id": row.claude_uuid, "cwd": cwd,
                            "workspace": {"project_dir": cwd, "current_dir": cwd},
                            "context_window": {"context_window_size": capacity},
                        }), capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    app._usage = app._collect_usage(app.entries)
                    app._redraw_table()
                    self.assertEqual(table.add_row.call_args.args[6], f"[{color}]600,000[/]")
                    self.assertEqual(table.add_row.call_args.args[7], "[yellow]1.50M[/]")


if __name__ == "__main__":
    unittest.main()
