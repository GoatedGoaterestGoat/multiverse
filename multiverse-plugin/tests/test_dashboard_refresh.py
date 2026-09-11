"""Slow discovery, transcript parsing and wake relay must not own the UI loop."""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from xml.etree import ElementTree
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from textual.widgets import DataTable

from src import groups, monitor


def row(name="agent", native_id="thread"):
    return monitor.DashboardRow(name=name, cwd="/workspace", status="idle",
                                status_ts=1, runtime="codex", session_id=native_id)


class RefreshStateTests(unittest.TestCase):
    def setUp(self):
        self.app = monitor.MonitorApp([])

    def test_explicit_refresh_coalesces_but_timer_does_not_starve_slow_scan(self):
        app = self.app
        with patch.object(app, "_refresh_worker") as worker:
            app.refresh_sessions()
            for _ in range(5):
                app._poll_sessions()
            worker.assert_called_once()
            self.assertEqual(app._refresh_generation, 1)
            app.refresh_sessions()
            app.refresh_sessions()
            worker.assert_called_once()
            app._finish_refresh(1, [], [row("stale")], False, None)
            self.assertEqual(app.entries, [])
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(worker.call_args.args[0], 3)

    def test_refresh_errors_keep_last_view_and_retry_without_notification_spam(self):
        app = self.app
        app.entries = [row()]
        with patch.object(app, "notify") as notify, patch.object(app, "_refresh_worker") as worker:
            for _ in range(2):
                app.refresh_sessions()
                app._finish_refresh(app._refresh_generation, [], [], False, "read failed")
                self.assertEqual(app.entries[0].name, "agent")
            self.assertEqual(worker.call_count, 2)
            notify.assert_called_once()

    def test_usage_is_single_flight_and_copies_thread_identity(self):
        app = self.app
        original = row()
        app.entries = [original]
        with patch.object(app, "_usage_worker") as worker, patch.object(app, "_redraw_table"):
            app._request_usage()
            old_snapshot = worker.call_args.args[0][0]
            original.session_id = "resumed"
            app._request_usage()
            app._request_usage()
            worker.assert_called_once()
            self.assertEqual(old_snapshot.session_id, "thread")
            app._finish_usage({monitor._usage_key(old_snapshot): monitor._DashboardUsage(10, 20, 100)})
            self.assertIsNone(app._tokens_of(original))
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(worker.call_args.args[0][0].session_id, "resumed")

    def test_late_usage_uses_current_rows_and_current_folder_state(self):
        app = self.app
        old = row()
        current = row("new", "new-thread")
        app.entries = [current]
        folder = groups.Group("folder", "Work", collapsed=True)
        app.group_state = groups.GroupState(groups={folder.id: folder})
        with patch.object(app, "_redraw_table") as redraw:
            app._finish_usage({monitor._usage_key(old): monitor._DashboardUsage(10, 20, 100)})
        self.assertEqual(app.entries, [current])
        self.assertNotIn(monitor._usage_key(old), app._usage)
        self.assertTrue(app.group_state.groups[folder.id].collapsed)
        redraw.assert_called_once()

    def test_bad_transcript_does_not_block_other_rows_or_clear_previous_sample(self):
        app = self.app
        bad, good = row("bad"), row("good", "other")
        app.entries = [bad, good]
        app._usage[monitor._usage_key(bad)] = monitor._DashboardUsage(1, 2, 3)
        def tokens(entry):
            if entry.name == "bad":
                raise OSError("rewritten")
            return 10
        with (
            patch.object(app, "_agent_tokens", side_effect=tokens),
            patch.object(app, "_agent_total", return_value=20),
            patch.object(app, "_agent_context_window", return_value=100),
            patch.object(app, "_redraw_table"),
        ):
            app._finish_usage(app._collect_usage(app.entries))
        self.assertEqual(app._tokens_of(bad), 1)
        self.assertEqual(app._tokens_of(good), 10)

    def test_unmount_makes_all_late_results_and_followups_inert(self):
        app = self.app
        app._usage_requested = True
        app.on_unmount()
        with (
            patch.object(app, "_redraw_table") as redraw,
            patch.object(app, "_refresh_worker") as discovery,
            patch.object(app, "_usage_worker") as usage,
            patch.object(app, "call_from_thread") as publish,
        ):
            app._finish_refresh(0, [], [row()], False, None)
            app._finish_usage({})
            app._publish_from_worker(app._finish_usage, {})
            app.refresh_sessions()
            app._request_usage()
        for mock in (redraw, discovery, usage, publish):
            mock.assert_not_called()

    def test_new_row_focus_waits_for_snapshot_and_respects_later_navigation(self):
        for current in ("old", "navigated-away"):
            app = monitor.MonitorApp([])
            app._refresh_in_flight = True
            app._ending_recovery_done = True
            app._relay_in_flight = True
            with (
                patch.object(app, "_move_cursor_to_key") as move,
                patch.object(app, "_cursor_row_key", return_value="old") as cursor,
                patch.object(app, "_user_is_busy", return_value=False),
                patch.object(app, "_file_arrived_spawns"),
                patch.object(monitor.groups, "load", return_value=groups.GroupState()),
                patch.object(app, "_redraw_table"),
                patch.object(app, "_tick_spawn_requests"),
                patch.object(app, "_request_usage"),
            ):
                app._focus_refreshed_row("new")
                move.reset_mock()
                cursor.return_value = current
                app._finish_refresh(0, [], [row()], False, None)
                if current == "old":
                    move.assert_called_once_with("new")
                else:
                    move.assert_not_called()
                self.assertIsNone(app._refresh_focus)

    def test_kill_gates_input_immediately_without_waiting_for_discovery(self):
        app = self.app
        session = monitor.Session(id="wrapper", cwd="/workspace", created_at=1)
        target = row()
        target.session = session
        app.entries = [target]
        with (
            patch.object(monitor, "mark_session_ending"),
            patch.object(app, "_start_session_teardown"),
            patch.object(app, "_redraw_table"),
            patch.object(app, "_current_entry", return_value=target),
            patch.object(app, "_attach_to") as attach,
            patch.object(app, "notify") as notify,
        ):
            app._begin_session_kill(session, "stopped")
            self.assertEqual(target.status, "ending")
            app.action_primary()
            attach.assert_not_called()
            self.assertIn("attach is disabled", notify.call_args.args[0])


class RefreshResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=3)

    def isolated_app(self, stack, entries):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {"MULTIVERSE_ROOT": str(root)}))
        app = monitor.MonitorApp([])
        app.monitor_cwd = "/workspace"
        stack.enter_context(patch.object(monitor, "STATE_DIR", root / "sessions"))
        stack.enter_context(patch.object(monitor, "recover_abandoned_handoffs"))
        stack.enter_context(patch.object(monitor, "list_sessions", return_value=[]))
        stack.enter_context(patch.object(monitor, "build_dashboard_rows", return_value=entries))
        stack.enter_context(patch.object(monitor.web_share, "status", return_value=None))
        stack.enter_context(patch.object(app, "_vouch_for_visible"))
        stack.enter_context(patch.object(app, "_recover_ending_sessions"))
        stack.enter_context(patch.object(app, "_tick_spawn_requests"))
        stack.enter_context(patch.object(app, "_tick_host_liveness"))
        stack.enter_context(patch.object(app, "_pending_inbox", return_value=0))
        stack.enter_context(patch.object(app, "_collect_usage", return_value={}))
        return app

    async def test_slow_refresh_does_not_block_navigation_or_restore_loading_screen(self):
        started, release, timed_out = (threading.Event() for _ in range(3))
        scans = 0
        def slow_scan():
            nonlocal scans
            scans += 1
            if scans == 1:
                return []
            started.set()
            if not release.wait(timeout=8):
                timed_out.set()
            return []
        with ExitStack() as stack:
            entries = [row("first"), row("second", "second-thread")]
            app = self.isolated_app(stack, entries)
            stack.enter_context(patch.object(monitor, "list_sessions", side_effect=slow_scan))
            try:
                async with app.run_test() as pilot:
                    await self.wait_until(lambda: app._initial_scan_complete)
                    app.refresh_sessions()
                    await self.wait_until(started.is_set)
                    table = app.query_one(DataTable)
                    self.assertFalse(table.loading)
                    table.focus()
                    await asyncio.wait_for(pilot.press("down"), 2)
                    self.assertEqual(table.cursor_row, 1)
                    self.assertFalse(timed_out.is_set())
                    release.set()
                    await self.wait_until(lambda: not app._refresh_in_flight)
            finally:
                release.set()

    async def test_first_scan_shows_loading_then_reveals_rows_or_empty_workspace(self):
        for entries in ([row()], []):
            with self.subTest(empty=not entries), ExitStack() as stack:
                app = self.isolated_app(stack, entries)
                started, release = threading.Event(), threading.Event()
                def slow_scan():
                    started.set()
                    release.wait(timeout=8)
                    return []
                stack.enter_context(patch.object(monitor, "list_sessions", side_effect=slow_scan))
                try:
                    async with app.run_test() as pilot:
                        await self.wait_until(started.is_set)
                        await pilot.pause()
                        table = app.query_one(DataTable)
                        self.assertTrue(table.loading)
                        rendered = "".join(ElementTree.fromstring(app.export_screenshot()).itertext()).replace("\xa0", " ")
                        self.assertIn("Loading agents…", rendered)
                        self.assertNotIn("Tokens", rendered)
                        self.assertFalse(app._initial_scan_complete)
                        release.set()
                        await self.wait_until(lambda: app._initial_scan_complete)
                        await pilot.pause()
                        self.assertFalse(table.loading)
                        self.assertEqual(table.row_count, len(entries))
                        rendered = "".join(ElementTree.fromstring(app.export_screenshot()).itertext()).replace("\xa0", " ")
                        self.assertNotIn("Loading agents…", rendered)
                        self.assertIn("Tokens", rendered)
                        self.assertIs(app.focused, table)
                finally:
                    release.set()

    async def test_failed_first_scan_keeps_loading_until_successful_retry(self):
        with ExitStack() as stack:
            app = self.isolated_app(stack, [row()])
            stack.enter_context(patch.object(monitor, "list_sessions", side_effect=OSError("offline")))
            stack.enter_context(patch.object(app, "notify"))
            async with app.run_test():
                await self.wait_until(lambda: app._refresh_error == "offline")
                self.assertTrue(app.query_one(DataTable).loading)
                self.assertFalse(app._initial_scan_complete)
                with patch.object(monitor, "list_sessions", return_value=[]):
                    app.refresh_sessions()
                    await self.wait_until(lambda: app._initial_scan_complete)
                self.assertFalse(app.query_one(DataTable).loading)

    async def test_quit_is_available_while_first_scan_is_pending(self):
        with ExitStack() as stack:
            app = self.isolated_app(stack, [])
            stack.enter_context(patch.object(app, "_refresh_worker"))
            async with app.run_test() as pilot:
                self.assertTrue(app.query_one(DataTable).loading)
                await pilot.press("q")
                self.assertFalse(app.is_running)

    async def test_slow_tokens_and_relay_allow_rows_navigation_and_folder_edits(self):
        usage_started, relay_started, release, timed_out = (threading.Event() for _ in range(4))
        entries = [row("first"), row("second", "second-thread")]
        def wait(started):
            started.set()
            if not release.wait(timeout=8):
                timed_out.set()
        def usage(snapshot):
            wait(usage_started)
            return {monitor._usage_key(e): monitor._DashboardUsage(10, 20, 100)
                    for e in snapshot}
        with ExitStack() as stack:
            app = self.isolated_app(stack, entries)
            folder = groups.create_group(app.monitor_cwd, "Work")
            for entry in entries:
                groups.assign_agent(app.monitor_cwd, entry.name, folder.id)
            stack.enter_context(patch.object(app, "_collect_usage", side_effect=usage))
            stack.enter_context(patch.object(app, "_tick_host_liveness",
                                             side_effect=lambda: wait(relay_started)))
            try:
                async with app.run_test() as pilot:
                    await self.wait_until(lambda: usage_started.is_set() and relay_started.is_set())
                    table = app.query_one(DataTable)
                    self.assertEqual(table.row_count, 3)
                    self.assertFalse(table.loading)
                    self.assertIsNone(app._tokens_of(entries[0]))
                    table.focus()
                    # Collapse and reopen the folder while BOTH workers wait.
                    await asyncio.wait_for(pilot.press("left"), 2)
                    self.assertEqual(table.row_count, 1)
                    await asyncio.wait_for(pilot.press("right", "down"), 2)
                    self.assertEqual(table.row_count, 3)
                    self.assertEqual(table.cursor_row, 1)
                    self.assertFalse(timed_out.is_set())
                    release.set()
                    await self.wait_until(lambda: app._tokens_of(entries[0]) == 10)
                    self.assertEqual(table.get_row_at(0)[6:8], ["", ""])
            finally:
                release.set()

    async def test_startup_recovery_error_is_best_effort(self):
        with ExitStack() as stack:
            app = self.isolated_app(stack, [row()])
            recovery = stack.enter_context(patch.object(monitor, "recover_abandoned_handoffs",
                                                        side_effect=OSError("unwritable")))
            notify = stack.enter_context(patch.object(app, "notify"))
            async with app.run_test():
                await self.wait_until(lambda: bool(app.entries))
                self.assertTrue(app._startup_recovery_done)
                self.assertIn("unwritable", notify.call_args.args[0])
                app.refresh_sessions()
                await self.wait_until(lambda: not app._refresh_in_flight)
                recovery.assert_called_once()


if __name__ == "__main__":
    unittest.main()
