"""The dashboard side of folders: what gets drawn, and what the keys do.

The monitor is a Textual app, so these drive it the way the existing
monitor tests do — a mocked DataTable and patched screen/notify plumbing —
and assert on the cells and the calls rather than on pixels.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

from src import groups
from src import monitor


CWD = "/workspace/project"

NAME, ID, RUNTIME, INBOX, BUILD, STATUS, TOKENS, UPDATED, PREVIEW = range(9)


def _row(name: str, status: str = "idle") -> monitor.DashboardRow:
    """A dormant, Claude-less row: keeps token lookups off the disk so the
    rendering assertions don't need half the transcript stack patched."""
    return monitor.DashboardRow(
        name=name,
        cwd=CWD,
        status=status,
        status_ts=1000.0,
        session_id=f"sid-{name}",
    )


class DashboardFolderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_root = os.environ.get("MULTIVERSE_ROOT")
        os.environ["MULTIVERSE_ROOT"] = self._tmp.name
        self.addCleanup(self._restore)

        self.app = monitor.MonitorApp([])
        self.app.monitor_cwd = CWD
        self.table = MagicMock()
        self.table.cursor_row = 0

    def _restore(self) -> None:
        if self._prev_root is None:
            os.environ.pop("MULTIVERSE_ROOT", None)
        else:
            os.environ["MULTIVERSE_ROOT"] = self._prev_root
        self._tmp.cleanup()

    def _reload(self) -> None:
        self.app.group_state = groups.load(CWD)

    def _draw(self, entries: list[monitor.DashboardRow], pending: int = 0) -> list:
        """Run one redraw and hand back the rendered rows as arg tuples."""
        self.app.entries = entries
        self._reload()
        self.table.add_row.reset_mock()
        with (
            patch.object(self.app, "query_one", return_value=self.table),
            patch.object(monitor, "pending_inbox_count", return_value=pending),
            patch.object(monitor, "auto_build_enabled", return_value=False),
        ):
            self.app._redraw_table()
        return [c.args for c in self.table.add_row.call_args_list]


class FolderRenderingTests(DashboardFolderTestCase):
    def test_an_ungrouped_workspace_draws_a_flat_list_with_no_connectors(self) -> None:
        drawn = self._draw([_row("swift-otter"), _row("brave-mole")])
        self.assertEqual([c[NAME] for c in drawn], ["swift-otter", "brave-mole"])

    def test_a_folder_header_precedes_its_indented_members(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        groups.assign_agent(CWD, "brave-mole", group.id)

        drawn = self._draw(
            [_row("swift-otter"), _row("brave-mole"), _row("quiet-fox")]
        )

        self.assertIn("▼ backend", drawn[0][NAME])
        self.assertIn("(2)", drawn[0][NAME])
        self.assertIn("├─ ", drawn[1][NAME])
        self.assertTrue(drawn[1][NAME].endswith("swift-otter"))
        self.assertIn("└─ ", drawn[2][NAME])
        self.assertEqual(drawn[3][NAME], "quiet-fox")

    def test_a_folder_header_has_no_session_id_runtime_or_build_cell(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        header = self._draw([_row("swift-otter")])[0]
        self.assertEqual(header[ID], "")
        self.assertEqual(header[RUNTIME], "[dim]-[/]")
        self.assertEqual(header[BUILD], "[dim]-[/]")

    def test_the_build_column_still_lands_where_the_dashboard_expects_it(self) -> None:
        # The name column moved to the front to carry the tree; the other
        # columns must not have shifted with it.
        drawn = self._draw([_row("swift-otter")])
        self.assertEqual(drawn[0][BUILD], "[dim]off[/]")
        self.assertEqual(drawn[0][STATUS], "[dim]idle[/]")

    def test_a_collapsed_folder_hides_members_and_names_them_in_the_preview(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        groups.assign_agent(CWD, "brave-mole", group.id)
        groups.set_collapsed(CWD, group.id, True)

        drawn = self._draw([_row("swift-otter"), _row("brave-mole")])

        self.assertEqual(len(drawn), 1)
        self.assertIn("▶ backend", drawn[0][NAME])
        self.assertIn("swift-otter", drawn[0][PREVIEW])
        self.assertIn("brave-mole", drawn[0][PREVIEW])

    def test_a_collapsed_folder_rolls_up_the_inbox_count(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        groups.assign_agent(CWD, "brave-mole", group.id)
        groups.set_collapsed(CWD, group.id, True)

        drawn = self._draw([_row("swift-otter"), _row("brave-mole")], pending=2)
        self.assertIn("📩 4", drawn[0][INBOX])

    def test_a_folder_reports_the_status_that_most_wants_attention(self) -> None:
        group = groups.create_group(CWD, "backend")
        for name in ("swift-otter", "brave-mole", "keen-heron"):
            groups.assign_agent(CWD, name, group.id)
        groups.set_collapsed(CWD, group.id, True)

        drawn = self._draw([
            _row("swift-otter", "idle"),
            _row("brave-mole", "waiting"),
            _row("keen-heron", "working"),
        ])
        self.assertIn("1 waiting", drawn[0][STATUS])

    def test_an_empty_folder_says_so_rather_than_borrowing_a_status(self) -> None:
        groups.create_group(CWD, "backend")
        drawn = self._draw([_row("quiet-fox")])
        self.assertEqual(drawn[0][STATUS], "[dim]empty[/]")
        self.assertEqual(drawn[0][UPDATED], "-")


class CursorTests(DashboardFolderTestCase):
    def test_a_folder_row_is_not_an_agent_for_session_actions(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        self.table.cursor_row = 0
        with patch.object(self.app, "query_one", return_value=self.table):
            self.assertIsNone(self.app._current_entry())
            current = self.app._current_display()
            self.assertTrue(current.is_group)
            self.assertEqual(current.group.id, group.id)

            self.table.cursor_row = 1
            entry = self.app._current_entry()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "swift-otter")

    def test_the_cursor_follows_its_row_when_a_fold_shifts_the_rows(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        groups.assign_agent(CWD, "brave-mole", group.id)
        entries = [_row("swift-otter"), _row("brave-mole"), _row("quiet-fox")]

        self._draw(entries)
        # Cursor parked on the loose agent, last of four rows.
        self.assertEqual(self.app.display_rows[3].entry.name, "quiet-fox")
        self.table.cursor_row = 3

        groups.set_collapsed(CWD, group.id, True)
        self.table.move_cursor.reset_mock()
        self._draw(entries)

        # Two rows vanished above it; the cursor tracked the row, not the
        # index it happened to be sitting at.
        self.assertEqual(self.app.display_rows[1].entry.name, "quiet-fox")
        self.table.move_cursor.assert_called_with(row=1)

    def test_the_cursor_falls_back_to_the_old_index_when_its_row_is_gone(self) -> None:
        entries = [_row("swift-otter"), _row("brave-mole"), _row("quiet-fox")]
        self._draw(entries)
        self.table.cursor_row = 2

        self.table.move_cursor.reset_mock()
        self._draw(entries[:2])
        self.table.move_cursor.assert_called_with(row=1)


class FolderKeyActionTests(DashboardFolderTestCase):
    def _cursor_on(self, index: int):
        self.table.cursor_row = index
        return patch.object(self.app, "query_one", return_value=self.table)

    def test_enter_on_a_folder_folds_it_instead_of_attaching(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        with (
            self._cursor_on(0),
            patch.object(self.app, "refresh_sessions") as refresh,
            patch.object(self.app, "_attach_to") as attach,
        ):
            self.app.action_primary()

        attach.assert_not_called()
        refresh.assert_called()
        self.assertTrue(groups.load(CWD).groups[group.id].collapsed)

    def test_kill_on_a_folder_asks_about_the_folder_not_the_sessions(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        with (
            self._cursor_on(0),
            patch.object(self.app, "push_screen") as push,
            patch.object(monitor, "kill_session") as kill,
        ):
            self.app.action_kill()

        kill.assert_not_called()
        screen = push.call_args.args[0]
        self.assertIsInstance(screen, monitor.ConfirmGroupDeleteScreen)
        self.assertEqual(screen.group_name, "backend")
        self.assertEqual(screen.agent_count, 1)
        # And the folder survives until the confirmation comes back.
        self.assertIn(group.id, groups.load(CWD).groups)

    def test_confirming_a_folder_delete_keeps_the_agent(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        with (
            self._cursor_on(0),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_kill()
        confirm = push.call_args.args[1]

        with (
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
        ):
            confirm(True)

        state = groups.load(CWD)
        self.assertEqual(state.groups, {})
        self.assertIsNone(state.group_of("swift-otter"))

    def test_new_folder_on_an_agent_row_takes_that_agent_with_it(self) -> None:
        self._draw([_row("swift-otter"), _row("brave-mole")])

        with (
            self._cursor_on(0),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_new_group()
        on_name = push.call_args.args[1]

        with (
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
            patch.object(self.app, "query_one", return_value=self.table),
        ):
            on_name("backend")

        state = groups.load(CWD)
        self.assertEqual([g.name for g in state.groups.values()], ["backend"])
        self.assertIsNotNone(state.group_of("swift-otter"))
        self.assertIsNone(state.group_of("brave-mole"))

    def test_new_folder_on_a_folder_row_nests_inside_it(self) -> None:
        outer = groups.create_group(CWD, "backend")
        self._draw([])

        with (
            self._cursor_on(0),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_new_group()

        with (
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
            patch.object(self.app, "query_one", return_value=self.table),
        ):
            push.call_args.args[1]("auth")

        state = groups.load(CWD)
        nested = next(g for g in state.groups.values() if g.name == "auth")
        self.assertEqual(nested.parent, outer.id)

    def test_a_rejected_folder_name_is_reported_and_changes_nothing(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([_row("swift-otter")])

        # Row 1 is the loose agent, so this asks for a *top-level* folder
        # named "backend" — where one already exists.
        with (
            self._cursor_on(1),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_new_group()

        with (
            patch.object(self.app, "refresh_sessions") as refresh,
            patch.object(self.app, "notify") as notify,
        ):
            push.call_args.args[1]("backend")

        refresh.assert_not_called()
        self.assertIn("already exists", notify.call_args.args[0])
        self.assertEqual(len(groups.load(CWD).groups), 1)

    def test_ungroup_lifts_an_agent_one_level_up(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        groups.assign_agent(CWD, "swift-otter", inner.id)
        self._draw([_row("swift-otter")])

        # rows: backend, auth, swift-otter
        with (
            self._cursor_on(2),
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
        ):
            self.app.action_ungroup()

        self.assertEqual(groups.load(CWD).group_of("swift-otter"), outer.id)

    def test_ungroup_on_a_loose_agent_says_so_and_does_nothing(self) -> None:
        self._draw([_row("swift-otter")])
        with (
            self._cursor_on(0),
            patch.object(self.app, "refresh_sessions") as refresh,
            patch.object(self.app, "notify") as notify,
        ):
            self.app.action_ungroup()
        refresh.assert_not_called()
        self.assertIn("isn't in a folder", notify.call_args.args[0])

    def test_space_on_a_member_folds_the_folder_around_it(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        with (
            self._cursor_on(1),
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
        ):
            self.app.action_toggle_collapse()

        self.assertTrue(groups.load(CWD).groups[group.id].collapsed)

    def test_fold_all_toggles_back_to_unfold_all(self) -> None:
        a = groups.create_group(CWD, "backend")
        groups.create_group(CWD, "auth", parent=a.id)
        self._draw([])

        with (
            patch.object(self.app, "query_one", return_value=self.table),
            patch.object(self.app, "refresh_sessions", side_effect=self._reload),
            patch.object(self.app, "notify"),
        ):
            self.app.action_toggle_collapse_all()
            self.assertTrue(
                all(g.collapsed for g in groups.load(CWD).groups.values())
            )
            self.app.action_toggle_collapse_all()

        self.assertFalse(any(g.collapsed for g in groups.load(CWD).groups.values()))

    def test_moving_an_agent_into_a_folder_via_the_picker(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([_row("swift-otter")])

        # rows: backend, swift-otter
        with (
            self._cursor_on(1),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_move_to_group()
        picker, on_pick = push.call_args.args[0], push.call_args.args[1]
        self.assertIsInstance(picker, monitor.GroupPickerScreen)

        with (
            patch.object(self.app, "query_one", return_value=self.table),
            patch.object(self.app, "refresh_sessions", side_effect=self._reload),
            patch.object(self.app, "notify"),
        ):
            on_pick(("group", group.id))

        self.assertEqual(groups.load(CWD).group_of("swift-otter"), group.id)

    def test_the_picker_will_not_offer_a_folders_own_subtree(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        groups.create_group(CWD, "frontend")
        self._draw([])

        with (
            self._cursor_on(0),  # the backend header
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_move_to_group()

        picker = push.call_args.args[0]
        self.assertEqual(picker.exclude, {outer.id, inner.id})

    def test_moving_to_the_top_level_clears_membership(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        with (
            self._cursor_on(1),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_move_to_group()

        with (
            patch.object(self.app, "query_one", return_value=self.table),
            patch.object(self.app, "refresh_sessions", side_effect=self._reload),
            patch.object(self.app, "notify"),
        ):
            push.call_args.args[1](("root", None))

        self.assertIsNone(groups.load(CWD).group_of("swift-otter"))

    def test_rename_reaches_the_folder_from_a_member_row(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        with (
            self._cursor_on(1),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_rename_group()

        with (
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
        ):
            push.call_args.args[1]("infra")

        self.assertEqual(groups.load(CWD).groups[group.id].name, "infra")

    def test_rename_with_no_folder_in_reach_explains_itself(self) -> None:
        self._draw([_row("swift-otter")])
        with (
            self._cursor_on(0),
            patch.object(self.app, "push_screen") as push,
            patch.object(self.app, "notify") as notify,
        ):
            self.app.action_rename_group()
        push.assert_not_called()
        self.assertIn("Highlight a folder", notify.call_args.args[0])


class PickerEventTests(DashboardFolderTestCase):
    def test_the_picker_stops_the_selection_event_from_bubbling(self) -> None:
        # MonitorApp.on_data_table_row_selected calls action_primary(), so a
        # selection that escapes the modal attaches or resumes whatever the
        # dashboard cursor happens to be on. Observed for real: picking a
        # destination folder also resumed a dormant agent behind the modal.
        picker = monitor.GroupPickerScreen("move…", groups.load(CWD))
        picker._choices = [("root", None)]
        event = MagicMock()
        event.cursor_row = 0
        with patch.object(monitor.GroupPickerScreen, "dismiss") as dismiss:
            picker.on_data_table_row_selected(event)
        event.stop.assert_called_once()
        dismiss.assert_called_once_with(("root", None))


class NewSessionFolderTests(DashboardFolderTestCase):
    def test_a_session_started_on_a_folder_header_lands_in_that_folder(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([])

        self.table.cursor_row = 0
        with (
            patch.object(self.app, "query_one", return_value=self.table),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_new_session()
        cb = push.call_args.args[1]

        with (
            patch.object(monitor, "create_session") as create,
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify") as notify,
        ):
            cb(("swift-otter", monitor.RUNTIME_CLAUDE))

        create.assert_called_once()
        self.assertEqual(groups.load(CWD).group_of("swift-otter"), group.id)
        self.assertIn("backend", notify.call_args.args[0])

    def test_a_session_started_on_an_agent_row_is_left_at_the_top_level(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "brave-mole", group.id)
        self._draw([_row("brave-mole")])

        self.table.cursor_row = 1  # the member row, not the header
        with (
            patch.object(self.app, "query_one", return_value=self.table),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_new_session()

        with (
            patch.object(monitor, "create_session"),
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
        ):
            push.call_args.args[1](("swift-otter", monitor.RUNTIME_CLAUDE))

        self.assertIsNone(groups.load(CWD).group_of("swift-otter"))

    def test_a_failed_spawn_does_not_file_a_session_that_does_not_exist(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([])

        self.table.cursor_row = 0
        with (
            patch.object(self.app, "query_one", return_value=self.table),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_new_session()

        with (
            patch.object(
                monitor, "create_session", side_effect=RuntimeError("boom")
            ),
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
        ):
            push.call_args.args[1](("swift-otter", monitor.RUNTIME_CLAUDE))

        self.assertIsNone(groups.load(CWD).group_of("swift-otter"))
        self.assertIn(group.id, groups.load(CWD).groups)


if __name__ == "__main__":
    unittest.main()
