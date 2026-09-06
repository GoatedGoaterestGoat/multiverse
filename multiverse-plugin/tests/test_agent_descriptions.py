from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

from rich.console import Console
from rich.text import Text
from textual.widgets import DataTable

from src import agent_descriptions as descriptions
from src import groups, monitor, mvctl, reminders, server, sessions


class DescriptionFixture:
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.cwd = str(self.workspace.resolve())
        self.name = "agent"
        env = patch.dict(os.environ, {
            "MULTIVERSE_ROOT": str(self.state),
            "MULTIVERSE_SESSION_NAME": self.name,
            "MULTIVERSE_SESSION_CWD": self.cwd,
            "MULTIVERSE_RUNTIME": "codex",
            "MULTIVERSE_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        })
        env.start()
        self.addCleanup(env.stop)


class DescriptionStorageTests(DescriptionFixture, unittest.TestCase):
    def test_missing_update_clear_and_persistence(self) -> None:
        self.assertIsNone(descriptions.read_description(self.cwd, self.name))
        descriptions.write_description(self.cwd, self.name, "  Codex tokens and MCP tools  ")
        self.assertEqual(descriptions.read_description(self.cwd, self.name), "Codex tokens and MCP tools")
        descriptions.write_description(self.cwd, self.name, "Dashboard descriptions")
        self.assertEqual(descriptions.read_description(self.cwd, self.name), "Dashboard descriptions")
        descriptions.write_description(self.cwd, self.name, "")
        self.assertIsNone(descriptions.read_description(self.cwd, self.name))

    def test_exact_limit_and_invalid_update_preserves_description(self) -> None:
        descriptions.write_description(self.cwd, self.name, "a" * 60)
        for invalid in ("b" * 61, "x" + " " * 60, "line\nbreak", "tab\ttext",
                        "cr\rtext", "escape\x1b[31m", "bad\u202etext", "line\u2028break"):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(ValueError):
                    descriptions.write_description(self.cwd, self.name, invalid)
                self.assertEqual(descriptions.read_description(self.cwd, self.name), "a" * 60)

    def test_unicode_is_counted_as_characters_not_bytes(self) -> None:
        descriptions.write_description(self.cwd, self.name, "界" * 60)
        self.assertEqual(len(descriptions.read_description(self.cwd, self.name)), 60)
        with self.assertRaisesRegex(ValueError, "61 characters"):
            descriptions.write_description(self.cwd, self.name, "界" * 61)

    def test_workspace_name_and_state_root_are_isolated(self) -> None:
        descriptions.write_description(self.cwd, self.name, "one")
        self.assertIsNone(descriptions.read_description(self.cwd, "other"))
        self.assertIsNone(descriptions.read_description(self.cwd + "-other", self.name))
        with patch.dict(os.environ, {"MULTIVERSE_ROOT": str(self.root / "other-state")}):
            self.assertIsNone(descriptions.read_description(self.cwd, self.name))
        # Path punctuation must not alias, and names cannot escape the store.
        self.assertNotEqual(descriptions.description_path("/a/b", "x"),
                            descriptions.description_path("/a-b", "x"))
        self.assertTrue(descriptions.description_path(self.cwd, "../../escape").is_relative_to(self.state))

    def test_canonical_workspace_paths_share_description(self) -> None:
        descriptions.write_description(self.cwd + "/./", self.name, "same workspace")
        self.assertEqual(descriptions.read_description(self.cwd, self.name), "same workspace")

    def test_corrupt_or_invalid_persisted_descriptions_read_as_unset(self) -> None:
        path = descriptions.description_path(self.cwd, self.name)
        path.parent.mkdir(parents=True)
        for data in (b"{", b"[]", b'"text"', b'{"description":12}', b"\xff",
                     json.dumps({"description": "a" * 61}).encode()):
            with self.subTest(data=data[:20]):
                path.write_bytes(data)
                self.assertIsNone(descriptions.read_description(self.cwd, self.name))

    def test_atomic_parallel_writes_do_not_lose_other_agents(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: descriptions.write_description(self.cwd, f"agent-{n}", f"Task {n}"), range(24)))
        for n in range(24):
            self.assertEqual(descriptions.read_description(self.cwd, f"agent-{n}"), f"Task {n}")


class DescriptionToolTests(DescriptionFixture, unittest.TestCase):
    def test_sets_only_resolved_self_for_every_runtime(self) -> None:
        for runtime in sessions.RUNTIMES:
            with self.subTest(runtime=runtime), patch.dict(os.environ, {"MULTIVERSE_RUNTIME": runtime}):
                with patch.object(server, "_resolve_self_identity", return_value=("resolved-agent", self.cwd)):
                    result = server.set_description("MCP schemas; shipped token counters")
                self.assertIn("description is now", result)
                self.assertEqual(descriptions.read_description(self.cwd, "resolved-agent"), "MCP schemas; shipped token counters")
                self.assertIsNone(descriptions.read_description(self.cwd, self.name))

    def test_explicit_over_limit_failure_and_no_write(self) -> None:
        server.set_description("original")
        result = server.set_description("x" * 61)
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("61 characters", result)
        self.assertIn("maximum is 60", result)
        self.assertIn("not changed", result)
        self.assertEqual(descriptions.read_description(self.cwd, self.name), "original")

    def test_identity_and_storage_failures_are_explicit(self) -> None:
        with patch.object(server, "_resolve_self_identity", return_value=(None, None)):
            self.assertTrue(server.set_description("work").startswith("Error:"))
        with patch.object(server, "write_description", side_effect=PermissionError("denied")):
            self.assertIn("could not save", server.set_description("work"))

    def test_raw_internal_calls_do_not_get_agent_reminders(self) -> None:
        # SecondBrain consumes the Python API, not the agent-facing transport.
        with patch.object(server, "claim_pending_messages", return_value=[]), patch.object(server, "clear_wake_sent"):
            self.assertEqual(server.check_inbox(), "You have no pending inbox messages.")

    def test_pi_bridge_appends_reminders_and_clears_them_after_setting(self) -> None:
        with patch.object(server, "list_named_agents_in_cwd", return_value=[]):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(mvctl.main(["--json", "list-agents"]), 0)
            self.assertIn("[Multiverse reminder]", json.loads(out.getvalue())["text"])
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(mvctl.main(["--json", "set-description", "--description=Pi lifecycle support"]), 0)
        self.assertNotIn("[Multiverse reminder]", json.loads(out.getvalue())["text"])
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(mvctl.main(["--json", "set-description", "--description=" + "x" * 61]), 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_lifecycle_bridge_results_stay_machine_readable(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as out, patch("src.inbox.pending_inbox_count", return_value=0):
            self.assertEqual(mvctl.main(["--json", "inbox-count"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "text": "0"})

    def test_pi_bridge_execution_errors_get_reminders(self) -> None:
        with (
            patch.object(server, "list_agents", side_effect=OSError("test failure")),
            patch("sys.stdout", new_callable=io.StringIO) as out,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(mvctl.main(["--json", "list-agents"]), 1)
        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("test failure", result["text"])
        self.assertIn("[Multiverse reminder]", result["text"])


def _result_text(result) -> str:
    # FastMCP returns (content, structuredContent) for tools with a str schema.
    content = result[0] if isinstance(result, tuple) else result
    return "\n".join(block.text for block in content if hasattr(block, "text"))


class DescriptionMcpTests(DescriptionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_set_description_schema_is_self_only_and_shared(self) -> None:
        schemas = []
        for runtime in sessions.RUNTIMES:
            with patch.dict(os.environ, {"MULTIVERSE_RUNTIME": runtime}):
                tool = next(t for t in await server.mcp.list_tools() if t.name == "set_description")
            schemas.append(tool.inputSchema)
            self.assertEqual(set(tool.inputSchema["properties"]), {"description"})
            self.assertEqual(tool.inputSchema["required"], ["description"])
            for phrase in ("60", "completed tasks", "ongoing implementations", "discretion"):
                self.assertIn(phrase, tool.description)
        self.assertTrue(all(s == schemas[0] for s in schemas))

    async def test_every_mcp_tool_gets_reminder_when_description_missing(self) -> None:
        calls = {
            "list_agents": {}, "check_inbox": {}, "monitor_inbox": {},
            "message_agent": {"target_name": self.name, "message": "hello"},
            "ask_agent": {"target_name": self.name, "question": "hello"},
            "spawn_agent": {"instruction": ""},
            "set_description": {"description": "x" * 61},
        }
        self.assertEqual(set(calls), {t.name for t in await server.mcp.list_tools()})
        with (
            patch.object(server, "list_named_agents_in_cwd", return_value=[]),
            patch.object(server, "claim_pending_messages", return_value=[]),
            patch.object(server, "clear_wake_sent"),
            patch.object(server, "pending_inbox_count", return_value=1),
        ):
            for name, args in calls.items():
                with self.subTest(tool=name):
                    text = _result_text(await server.mcp.call_tool(name, args))
                    self.assertEqual(text.count("[Multiverse reminder]"), 1)
                    self.assertIn("set_description", text.split("[Multiverse reminder]")[1])

    async def test_setting_stops_reminders_and_clearing_restores_them(self) -> None:
        text = _result_text(await server.mcp.call_tool("set_description", {"description": "MCP tool implementation"}))
        self.assertNotIn("[Multiverse reminder]", text)
        with patch.object(server, "list_named_agents_in_cwd", return_value=[]):
            text = _result_text(await server.mcp.call_tool("list_agents", {}))
        self.assertNotIn("[Multiverse reminder]", text)
        text = _result_text(await server.mcp.call_tool("set_description", {"description": ""}))
        self.assertIn("description was cleared", text)
        self.assertIn("[Multiverse reminder]", text)

    async def test_cross_workspace_list_reminds_caller_not_target(self) -> None:
        descriptions.write_description(self.cwd + "-other", self.name, "Other workspace")
        with patch.object(server, "list_named_agents_in_cwd", return_value=[]):
            text = _result_text(await server.mcp.call_tool("list_agents", {"working_directory": self.cwd + "-other"}))
        self.assertIn("[Multiverse reminder]", text)

    async def test_unmanaged_caller_does_not_get_an_unactionable_reminder(self) -> None:
        with patch.object(server, "_resolve_self_identity", return_value=(None, None)):
            text = _result_text(await server.mcp.call_tool("set_description", {"description": "work"}))
        self.assertIn("Error:", text)
        self.assertNotIn("[Multiverse reminder]", text)

    async def test_argument_validation_and_execution_errors_include_reminder(self) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError) as caught:
            await server.mcp.call_tool("set_description", {})
        self.assertIn("description", str(caught.exception))
        self.assertEqual(str(caught.exception).count("[Multiverse reminder]"), 1)
        with patch.object(server, "pending_inbox_count", side_effect=OSError("test failure")):
            with self.assertRaises(ToolError) as caught:
                await server.mcp.call_tool("monitor_inbox", {})
        self.assertEqual(str(caught.exception).count("[Multiverse reminder]"), 1)
        self.assertIn("test failure", str(caught.exception))


class DescriptionLifecycleTests(DescriptionFixture, unittest.TestCase):
    def test_new_agents_start_unset_and_resumes_keep_description(self) -> None:
        with (
            patch.object(sessions, "STATE_DIR", self.state / "sessions"),
            patch.object(sessions, "MULTIVERSE_DIR", self.state),
            patch.object(sessions, "_assert_no_existing_wrapper_claim"),
            patch.object(sessions, "_acquire_claim"),
            patch("src.sb_notify.record_agent_created"),
        ):
            for runtime in sessions.RUNTIMES:
                for resume in (None, "native-thread"):
                    name = f"{runtime}-{resume or 'fresh'}"
                    descriptions.write_description(self.cwd, name, "previous description")
                    observed = []

                    def run(cmd, **kwargs):
                        observed.append(descriptions.read_description(self.cwd, name))
                        return CompletedProcess(cmd, 0)

                    with patch.object(sessions.subprocess, "run", side_effect=run):
                        sessions.create_session(self.cwd, [], resumed_from=resume, name=name, runtime=runtime)
                    self.assertTrue(observed)
                    self.assertEqual(observed[0], "previous description" if resume else None)

    def test_registry_shares_descriptions_for_wrapped_and_dormant_agents(self) -> None:
        descriptions.write_description(self.cwd, "wrapped", "Runtime implementation")
        descriptions.write_description(self.cwd, "dormant", "Completed protocol work")
        wrapped = sessions.Session(id="wrapper", cwd=self.cwd, created_at=1,
                                   name="wrapped", runtime="codex", codex_session_id="thread-w")
        with (
            patch.object(sessions, "STATE_DIR", self.state / "sessions"),
            patch.object(sessions, "CLAUDE_PROJECTS_DIR", self.root / "no-claude"),
            patch.object(sessions, "list_codex_sessions_in", return_value=[{
                "id": "thread-d", "name": "dormant", "mtime": 1,
            }]),
            patch.object(sessions, "list_pi_sessions_in", return_value=[]),
            patch.object(sessions, "list_locked", return_value=set()),
            patch.object(sessions.forks, "gc"),
            patch.object(sessions.forks, "active_uuids", return_value=set()),
        ):
            rows = sessions.build_dashboard_rows([wrapped], self.cwd)
        self.assertEqual({r.name: r.description for r in rows}, {
            "wrapped": "Runtime implementation", "dormant": "Completed protocol work",
        })
        with patch.object(server, "list_named_agents_in_cwd", return_value=rows):
            text = server.list_agents()
        self.assertIn("Description", text)
        self.assertIn("Completed protocol work", text)
        self.assertNotIn("codex", text)


class DescriptionDashboardTests(DescriptionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_description_column_fits_current_longest_row_without_truncation(self) -> None:
        row = monitor.DashboardRow(name="agent", cwd=self.cwd, status="idle", status_ts=1,
                                   session_id="thread-agent",
                                   description="[red]" + "界" * 45)
        other = monitor.DashboardRow(name="other", cwd=self.cwd, status="idle", status_ts=1,
                                     session_id="thread-other",
                                     description="Protocol implementation")
        entries = [row, other]

        app = monitor.MonitorApp([])
        app.monitor_cwd = self.cwd

        def refresh():
            app.entries = entries
            app._redraw_table()

        with (
            patch.object(monitor, "pending_inbox_count", return_value=0),
            patch.object(monitor, "STATE_DIR", self.state / "sessions"),
            patch.object(app, "refresh_sessions", side_effect=refresh),
            patch.object(app, "_recover_ending_sessions"),
        ):
            async with app.run_test(size=(110, 16)) as pilot:
                table = app.query_one(DataTable)
                self.assertEqual([str(c.label) for c in table.ordered_columns], [
                    "Name", "Description", "Runtime", "Inbox", "Build", "Status",
                    "Tokens", "Total", "Updated", "Preview",
                ])
                column = table.columns["description"]
                self.assertEqual(str(column.label), "Description")
                self.assertTrue(column.auto_width)
                self.assertEqual(column.content_width, 95)
                cell = table.get_row_at(0)[1]
                self.assertIsInstance(cell, Text)
                self.assertEqual(cell.plain, row.description)
                self.assertEqual(cell.cell_len, 95)
                self.assertTrue(cell.no_wrap)
                self.assertEqual(next(iter(table.rows.values())).height, 1)
                console = Console(width=column.content_width, record=True, file=io.StringIO())
                console.print(cell)
                rendered = console.export_text().rstrip("\n")
                self.assertEqual(rendered, row.description)
                # Clearing the longest leaves the other row in charge, not
                # the historical width retained by DataTable.clear().
                row.description = None
                refresh()
                await pilot.pause()
                self.assertEqual(column.content_width, len(other.description))
                self.assertEqual(table.get_row_at(0)[1].plain, "None")
                entries.remove(other)
                refresh()
                await pilot.pause()
                self.assertEqual(column.content_width, len("Description"))
                # Grow and shrink again; wide characters need more terminal
                # cells than code points, even beyond the viewport width.
                for description in ("x" * 60, "界" * 60, "MCP tools", None):
                    row.description = description
                    refresh()
                    await pilot.pause()
                    cell = table.get_row_at(0)[1]
                    self.assertEqual(cell.plain, description or "None")
                    self.assertEqual(column.content_width, max(len("Description"), cell.cell_len))
                    self.assertEqual(next(iter(table.rows.values())).height, 1)


if __name__ == "__main__":
    unittest.main()
