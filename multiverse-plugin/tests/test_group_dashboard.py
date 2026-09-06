"""The dashboard side of folders: what gets drawn, and what the keys do.

The monitor is a Textual app, so these drive it the way the existing
monitor tests do — a mocked DataTable and patched screen/notify plumbing —
and assert on the cells and the calls rather than on pixels.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

from rich.text import Text
from textual import events

from src import groups
from src import monitor


CWD = "/workspace/project"

NAME, ID, RUNTIME, INBOX, BUILD, STATUS, TOKENS, TOTAL, UPDATED, PREVIEW = (
    range(10)
)


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
        # The real widget moves its own cursor; the mock has to be told, or
        # every redraw would restore the cursor to where it started.
        def _move_cursor(row=None, **_kw):
            if row is not None:
                self.table.cursor_row = row

        self.table.move_cursor.side_effect = _move_cursor
        notifications = patch.object(self.app, "notify")
        self.notify = notifications.start()
        self.addCleanup(notifications.stop)

    def _restore(self) -> None:
        if self._prev_root is None:
            os.environ.pop("MULTIVERSE_ROOT", None)
        else:
            os.environ["MULTIVERSE_ROOT"] = self._prev_root
        self._tmp.cleanup()

    def _reload(self) -> None:
        self.app.group_state = groups.load(CWD)

    def _refresh(self) -> None:
        """What the app's own refresh does, minus the session scan."""
        self._reload()
        self.app._redraw_table()

    def _patched(self):
        return (
            patch.object(self.app, "query_one", return_value=self.table),
            patch.object(monitor, "pending_inbox_count", return_value=0),
            patch.object(monitor, "auto_build_enabled", return_value=False),
            patch.object(self.app, "refresh_sessions", side_effect=self._refresh),
        )

    def _act(self, fn, *args, cursor: Optional[int] = None):
        """Run a dashboard action with the table and refresh plumbing in place."""
        if cursor is not None:
            self.table.cursor_row = cursor
        with self._patched()[0], self._patched()[1], self._patched()[2], \
                self._patched()[3]:
            fn(*args)
        return self.notify

    def _send_key(self, key: str, char: Optional[str] = None) -> bool:
        """Feed one key through the interception path DashboardTable uses."""
        with self._patched()[0], self._patched()[1], self._patched()[2], \
                self._patched()[3]:
            return self.app._intercept_key(events.Key(key, char))

    def _type(self, text: str) -> None:
        for ch in text:
            self._send_key(ch, ch)

    def _select_down(self, start: int, times: int) -> None:
        self.table.cursor_row = start
        for _ in range(times):
            self._act(self.app.action_extend_selection_down)

    def _cursor_on(self, index: int):
        self.table.cursor_row = index
        return patch.object(self.app, "query_one", return_value=self.table)

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

    def test_a_retired_member_never_outranks_a_usable_one(self) -> None:
        group = groups.create_group(CWD, "backend")
        for name in ("swift-otter", "brave-mole"):
            groups.assign_agent(CWD, name, group.id)
        groups.set_collapsed(CWD, group.id, True)

        drawn = self._draw([
            _row("swift-otter", "retired"),
            _row("brave-mole", "dormant"),
        ])
        self.assertIn("1 dormant", drawn[0][STATUS])

    def test_an_empty_folder_says_so_rather_than_borrowing_a_status(self) -> None:
        groups.create_group(CWD, "backend")
        drawn = self._draw([_row("quiet-fox")])
        self.assertEqual(drawn[0][STATUS], "[dim]empty[/]")
        self.assertEqual(drawn[0][UPDATED], "-")


class MarkupSafetyTests(DashboardFolderTestCase):
    """Names the user types are data, not markup.

    Every cell goes through Rich's markup parser on its way into the
    table, and an unbalanced tag there raises out of the render — which in
    a Textual app means the monitor exits. A folder name lives on disk, so
    one bad name would take the workspace down on every restart until the
    JSON was hand-edited.
    """

    def _assert_renders(self, drawn: list) -> None:
        for cells in drawn:
            for cell in cells:
                if isinstance(cell, str):
                    Text.from_markup(cell)

    def test_a_folder_named_with_markup_still_renders(self) -> None:
        group = groups.create_group(CWD, "todo [/] urgent")
        groups.assign_agent(CWD, "swift-otter", group.id)

        drawn = self._draw([_row("swift-otter")])

        self._assert_renders(drawn)
        self.assertIn("todo [/] urgent", Text.from_markup(drawn[0][NAME]).plain)

    def test_a_live_agent_row_named_with_markup_still_renders(self) -> None:
        # The draft row accepts any name, so this reaches the table for
        # real once the agent is created — and stays there.
        drawn = self._draw([_row("a[/]b")])

        self._assert_renders(drawn)
        self.assertEqual(Text.from_markup(drawn[0][NAME]).plain, "a[/]b")

    def test_an_agent_preview_containing_markup_still_renders(self) -> None:
        row = _row("swift-otter")
        row.preview = "wrote [/] to the file"

        drawn = self._draw([row])

        self._assert_renders(drawn)
        self.assertIn("wrote [/] to", Text.from_markup(drawn[0][PREVIEW]).plain)

    def test_a_collapsed_folder_preview_of_such_names_still_renders(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter [/]", group.id)
        groups.set_collapsed(CWD, group.id, True)

        self._assert_renders(self._draw([_row("swift-otter [/]")]))

    def test_typing_markup_into_a_rename_does_not_crash_the_redraw(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_rename_group, cursor=0)

        self._type("a[/]b")

        self.assertEqual(self.app._inline.buffer, "a[/]b")
        self._assert_renders(self._draw([]))

    def test_typing_markup_into_the_draft_row_does_not_crash_the_redraw(self) -> None:
        self._draw([])
        self._act(self.app.action_new_session, cursor=0)

        self._type("a[/]b")

        self.assertEqual(self.app._draft.buffer, "a[/]b")
        self._assert_renders(self._draw([]))

    def test_the_delete_prompt_renders_a_folder_named_with_markup(self) -> None:
        screen = monitor.ConfirmGroupDeleteScreen(
            "todo [/] urgent", 1, 0, "parent [/] folder"
        )

        for container in screen.compose():
            for child in container._pending_children:
                child.render()  # Static parses markup here


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
    def test_g_on_a_folder_header_makes_an_empty_subfolder(self) -> None:
        # → on a folder means "open it", so g is the only way to make a
        # folder that holds nothing yet, or one nested inside another.
        backend = groups.create_group(CWD, "backend")
        self._draw([])

        self._act(self.app.action_new_group, cursor=0)

        state = groups.load(CWD)
        children = state.children_of(backend.id)
        self.assertEqual(len(children), 1)
        # And it comes up with its name selected, like every other new folder.
        self.assertIsNotNone(self.app._inline)
        self.assertEqual(self.app._inline.group_id, children[0].id)

    def test_g_expands_the_folder_it_puts_the_new_one_in(self) -> None:
        backend = groups.create_group(CWD, "backend")
        groups.set_collapsed(CWD, backend.id, True)
        self._draw([])

        self._act(self.app.action_new_group, cursor=0)

        # A row inside a folded folder would be invisible — and so would
        # the name field the user is now typing into.
        self.assertFalse(groups.load(CWD).groups[backend.id].collapsed)

    def test_g_on_an_agent_still_wraps_it(self) -> None:
        self._draw([_row("swift-otter")])

        self._act(self.app.action_new_group, cursor=0)

        state = groups.load(CWD)
        self.assertIsNotNone(state.group_of("swift-otter"))

    def test_escape_takes_back_an_empty_folder_made_with_g(self) -> None:
        backend = groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_new_group, cursor=0)

        self._send_key("escape")

        self.assertEqual(list(groups.load(CWD).groups), [backend.id])

    def test_enter_on_a_folder_folds_it_instead_of_attaching(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        with (
            self._cursor_on(0),
            patch.object(self.app, "_reload_groups_and_redraw") as redraw,
            patch.object(self.app, "_attach_to") as attach,
        ):
            self.app.action_primary()

        attach.assert_not_called()
        # Folding is a tree change, not a session change: no rescan.
        redraw.assert_called()
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
            patch.object(self.app, "_reload_groups_and_redraw"),
            patch.object(self.app, "notify"),
        ):
            confirm(True)

        state = groups.load(CWD)
        self.assertEqual(state.groups, {})
        self.assertIsNone(state.group_of("swift-otter"))

    def test_the_delete_prompt_counts_only_agents_that_still_exist(self) -> None:
        # Membership is by name and nothing prunes it when a transcript is
        # deleted, so the raw member list can name agents that are gone.
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "long-gone", group.id)
        self._draw([])  # no live rows at all

        with (
            self._cursor_on(0),
            patch.object(self.app, "push_screen") as push,
            patch.object(self.app, "_reload_groups_and_redraw"),
            patch.object(self.app, "notify"),
        ):
            self.app.action_kill()

        # Nothing the user can see is in there, so nothing to confirm.
        push.assert_not_called()
        self.assertEqual(groups.load(CWD).groups, {})

    def test_an_empty_folder_is_removed_without_a_prompt(self) -> None:
        # Nothing to lose and nothing hidden, so the confirmation would be
        # a window for its own sake.
        group = groups.create_group(CWD, "scratch")
        self._draw([])

        with (
            self._cursor_on(0),
            patch.object(self.app, "push_screen") as push,
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
        ):
            self.app.action_kill()

        push.assert_not_called()
        self.assertNotIn(group.id, groups.load(CWD).groups)

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

    def test_rename_with_no_folder_in_reach_explains_itself(self) -> None:
        self._draw([_row("swift-otter")])
        with (
            self._cursor_on(0),
            patch.object(self.app, "notify") as notify,
        ):
            self.app.action_rename_group()
        self.assertIsNone(self.app._inline)
        self.assertIn("Highlight a folder", notify.call_args.args[0])


class ArrowStructureTests(DashboardFolderTestCase):
    """→ and ← restructure the tree; on folders they open and close it."""

    def test_right_on_an_agent_wraps_it_in_a_new_folder(self) -> None:
        self._draw([_row("swift-otter"), _row("brave-mole")])
        self._act(self.app.action_indent, cursor=0)

        state = groups.load(CWD)
        self.assertEqual(len(state.groups), 1)
        gid = state.group_of("swift-otter")
        self.assertIsNotNone(gid)
        self.assertEqual(state.groups[gid].name, groups.DEFAULT_FOLDER_NAME)
        self.assertIsNone(state.group_of("brave-mole"))

    def test_the_new_folder_opens_its_name_for_editing_straight_away(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_indent, cursor=0)

        edit = self.app._inline
        self.assertIsNotNone(edit)
        self.assertEqual(edit.kind, "create")
        self.assertEqual(edit.buffer, groups.DEFAULT_FOLDER_NAME)
        # Finder-style: the name comes up selected, so typing replaces it.
        self.assertTrue(edit.select_all)

    def test_a_folder_is_created_beside_what_it_wraps_not_at_the_root(self) -> None:
        outer = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", outer.id)
        self._draw([_row("swift-otter")])

        self._act(self.app.action_indent, cursor=1)  # the member row

        state = groups.load(CWD)
        gid = state.group_of("swift-otter")
        self.assertEqual(state.groups[gid].parent, outer.id)

    def test_right_opens_a_folded_folder_rather_than_wrapping_it(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        groups.set_collapsed(CWD, group.id, True)
        self._draw([_row("swift-otter")])

        self._act(self.app.action_indent, cursor=0)

        state = groups.load(CWD)
        self.assertEqual(len(state.groups), 1)
        self.assertFalse(state.groups[group.id].collapsed)

    def test_right_on_an_open_folder_steps_into_its_first_child(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        self._act(self.app.action_indent, cursor=0)

        self.assertEqual(len(groups.load(CWD).groups), 1)
        self.table.move_cursor.assert_called_with(row=1)

    def test_left_closes_an_open_folder(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        self._act(self.app.action_outdent, cursor=0)

        self.assertTrue(groups.load(CWD).groups[group.id].collapsed)

    def test_left_on_a_closed_nested_folder_jumps_to_its_parent(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        groups.set_collapsed(CWD, inner.id, True)
        self._draw([])

        self._act(self.app.action_outdent, cursor=1)

        # Structure untouched; the cursor moved to the parent header.
        self.assertEqual(groups.load(CWD).groups[inner.id].parent, outer.id)
        self.table.move_cursor.assert_called_with(row=0)

    def test_left_on_a_closed_top_level_folder_has_nowhere_to_go(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.set_collapsed(CWD, group.id, True)
        self._draw([])

        notify = self._act(self.app.action_outdent, cursor=0)

        # Nothing to fold, nowhere to go, and nothing to complain about:
        # lifting a nested folder out is ``u``'s job, not ←'s.
        self.assertIn(group.id, groups.load(CWD).groups)
        self.assertIsNone(groups.load(CWD).groups[group.id].parent)
        notify.assert_not_called()

    def test_left_on_a_member_lifts_it_one_level_out(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        groups.assign_agent(CWD, "swift-otter", inner.id)
        self._draw([_row("swift-otter")])

        self._act(self.app.action_outdent, cursor=2)

        self.assertEqual(groups.load(CWD).group_of("swift-otter"), outer.id)

    def test_left_on_a_loose_agent_says_there_is_nowhere_to_lift_it(self) -> None:
        self._draw([_row("swift-otter")])
        notify = self._act(self.app.action_outdent, cursor=0)
        self.assertIn("top level", notify.call_args.args[0])
        self.assertEqual(groups.load(CWD).groups, {})


class InlineNamingTests(DashboardFolderTestCase):
    """The folder name is edited in the row, not in a window."""

    def test_typing_replaces_the_preselected_name_and_enter_commits(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_indent, cursor=0)

        self._type("infra")
        self.assertEqual(self.app._inline.buffer, "infra")
        self._send_key("enter")

        self.assertIsNone(self.app._inline)
        state = groups.load(CWD)
        self.assertEqual(state.groups[state.group_of("swift-otter")].name, "infra")

    def test_delete_removes_one_character_not_the_whole_name(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_rename_group, cursor=0)
        self._type("infra")

        self._send_key("left")
        self._send_key("left")
        self._send_key("delete")

        self.assertEqual(self.app._inline.buffer, "infa")

    def test_ctrl_u_is_the_one_that_clears_the_field(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_rename_group, cursor=0)
        self._type("infra")

        self._send_key("ctrl+u")

        self.assertEqual(self.app._inline.buffer, "")

    def test_escape_aborts_the_new_folder_entirely(self) -> None:
        # Esc on a name prompt means "never mind" — leaving a stray "New
        # folder" behind is not that.
        self._draw([_row("swift-otter")])
        self._act(self.app.action_indent, cursor=0)
        self.assertEqual(len(groups.load(CWD).groups), 1)

        self._send_key("escape")

        self.assertIsNone(self.app._inline)
        state = groups.load(CWD)
        self.assertEqual(state.groups, {})
        self.assertIsNone(state.group_of("swift-otter"))

    def test_escape_puts_a_wrapped_agent_back_in_the_folder_it_came_from(self) -> None:
        outer = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", outer.id)
        self._draw([_row("swift-otter")])

        self._act(self.app.action_indent, cursor=1)  # wrap the member
        self._send_key("escape")

        state = groups.load(CWD)
        self.assertEqual(list(state.groups), [outer.id])
        self.assertEqual(state.group_of("swift-otter"), outer.id)

    def test_escape_restores_a_selection_that_spanned_two_folders(self) -> None:
        # The wrapped items came from different places, so simply deleting
        # the folder would dump them both at the top level instead of
        # putting each one back.
        a = groups.create_group(CWD, "a-team")
        b = groups.create_group(CWD, "b-team")
        groups.assign_agent(CWD, "swift-otter", a.id)
        groups.assign_agent(CWD, "brave-mole", b.id)
        self._draw([_row("swift-otter"), _row("brave-mole")])

        # rows: a-team, swift-otter, b-team, brave-mole
        self._select_down(1, 2)
        self._act(self.app.action_indent)
        self._send_key("escape")

        state = groups.load(CWD)
        self.assertEqual(sorted(state.groups), sorted([a.id, b.id]))
        self.assertEqual(state.group_of("swift-otter"), a.id)
        self.assertEqual(state.group_of("brave-mole"), b.id)
        # And the selection the user had is handed back to them.
        self.assertEqual(len(self.app._selection), 3)

    def test_escape_reverts_a_rename(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_rename_group, cursor=0)

        self._type("something-else")
        self._send_key("escape")

        self.assertEqual(groups.load(CWD).groups[group.id].name, "backend")

    def test_backspace_and_the_text_cursor_edit_the_buffer(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_rename_group, cursor=0)

        self._send_key("left")  # drops the selection, cursor at the end - 1
        self._send_key("backspace")
        self.assertEqual(self.app._inline.buffer, "backed")
        self._type("n")
        self.assertEqual(self.app._inline.buffer, "backend")

    def test_a_duplicate_name_keeps_the_field_open_to_be_fixed(self) -> None:
        groups.create_group(CWD, "backend")
        other = groups.create_group(CWD, "frontend")
        self._draw([])
        self._act(self.app.action_rename_group, cursor=1)  # frontend

        with patch.object(self.app, "notify") as notify:
            self._type("backend")
            self._send_key("enter")

        self.assertIsNotNone(self.app._inline)
        self.assertIn("already exists", notify.call_args.args[0])
        self.assertEqual(groups.load(CWD).groups[other.id].name, "frontend")

    def test_rename_reaches_the_folder_from_a_member_row(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        self._act(self.app.action_rename_group, cursor=1)
        self._type("infra")
        self._send_key("enter")

        self.assertEqual(groups.load(CWD).groups[group.id].name, "infra")

    def test_arrows_edit_the_name_instead_of_restructuring_the_tree(self) -> None:
        # The whole point of intercepting keys: → while naming must not
        # also wrap the row in another folder.
        self._draw([_row("swift-otter")])
        self._act(self.app.action_indent, cursor=0)
        before = len(groups.load(CWD).groups)

        for key in ("left", "right", "up", "down"):
            self.assertTrue(self._send_key(key))

        self.assertEqual(len(groups.load(CWD).groups), before)
        self.assertIsNotNone(self.app._inline)

    def test_the_row_renders_the_buffer_with_a_cursor(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_indent, cursor=0)
        self._type("ab")

        drawn = self._draw([_row("swift-otter")])
        self.assertIn("ab", drawn[0][NAME])
        self.assertIn("reverse", drawn[0][NAME])


class MultiSelectTests(DashboardFolderTestCase):
    def test_shift_down_grows_a_range_from_the_cursor(self) -> None:
        self._draw([_row("a"), _row("b"), _row("c")])
        self._select_down(0, 2)
        self.assertEqual(
            self.app._selection, [r.key for r in self.app.display_rows]
        )

    def test_right_wraps_the_whole_selection_into_one_folder(self) -> None:
        self._draw([_row("a"), _row("b"), _row("c")])
        self._select_down(0, 1)
        self._act(self.app.action_indent, cursor=1)

        state = groups.load(CWD)
        self.assertEqual(len(state.groups), 1)
        gid = next(iter(state.groups))
        self.assertEqual(state.group_of("a"), gid)
        self.assertEqual(state.group_of("b"), gid)
        self.assertIsNone(state.group_of("c"))
        self.assertEqual(self.app._selection, [])

    def test_left_lifts_the_whole_selection_out(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "a", group.id)
        groups.assign_agent(CWD, "b", group.id)
        self._draw([_row("a"), _row("b")])
        self._select_down(1, 1)  # both members

        self._act(self.app.action_outdent, cursor=2)

        state = groups.load(CWD)
        self.assertIsNone(state.group_of("a"))
        self.assertIsNone(state.group_of("b"))

    def test_a_selected_folder_carries_its_contents_without_double_moving(self) -> None:
        outer = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "a", outer.id)
        self._draw([_row("a")])
        self._select_down(0, 1)  # the folder header and its member

        self._act(self.app.action_indent, cursor=1)

        state = groups.load(CWD)
        # The member is still inside backend, and backend moved into the new
        # folder — the agent did not get yanked out on its own.
        self.assertEqual(state.group_of("a"), outer.id)
        self.assertIsNotNone(state.groups[outer.id].parent)

    def test_a_plain_cursor_move_drops_the_selection(self) -> None:
        self._draw([_row("a"), _row("b"), _row("c")])
        self._select_down(0, 1)
        self.assertTrue(self.app._selection)

        # Not consumed — the table still gets to move its own cursor.
        self.assertFalse(self._send_key("down"))
        self.assertEqual(self.app._selection, [])

    def test_a_shift_move_is_not_a_plain_move(self) -> None:
        self._draw([_row("a"), _row("b"), _row("c")])
        self._select_down(0, 1)
        self._send_key("shift+down")
        self.assertTrue(self.app._selection)

    def test_a_redraw_does_not_count_as_a_cursor_move(self) -> None:
        # The selection used to be wiped by the very redraw that drew it:
        # DataTable POSTS RowHighlighted (including one for its own
        # clear() mid-rebuild), so a handler on that message fired long
        # after the move it was meant to describe. Only a real terminal
        # showed it, so it is pinned here.
        self._draw([_row("a"), _row("b")])
        self._select_down(0, 1)
        self._draw([_row("a"), _row("b")])
        self.assertEqual(len(self.app._selection), 2)

    def test_escape_clears_the_selection(self) -> None:
        self._draw([_row("a"), _row("b")])
        self._select_down(0, 1)
        with patch.object(self.app, "query_one", return_value=self.table):
            self.app.action_clear_selection()
        self.assertEqual(self.app._selection, [])

    def test_selected_rows_render_highlighted(self) -> None:
        self._draw([_row("a"), _row("b")])
        self._select_down(0, 1)
        drawn = self._draw([_row("a"), _row("b")])
        self.assertIn("reverse", drawn[0][NAME])
        self.assertIn("reverse", drawn[1][NAME])


class InPlaceMoveTests(DashboardFolderTestCase):
    def test_move_walks_into_a_folder_and_drops(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        self._draw([_row("swift-otter")])

        # rows: backend, auth, swift-otter
        self._act(self.app.action_move_to_group, cursor=2)
        self.assertIsNotNone(self.app._move)
        self.assertEqual(self.app._move.highlight, outer.id)

        self._send_key("right")          # into backend
        self.assertEqual(self.app._move.dest, outer.id)
        self.assertEqual(self.app._move.highlight, inner.id)
        self._send_key("right")          # into auth
        self.assertEqual(self.app._move.dest, inner.id)

        with patch.object(self.app, "notify"):
            self._send_key("enter")

        self.assertIsNone(self.app._move)
        self.assertEqual(groups.load(CWD).group_of("swift-otter"), inner.id)

    def test_left_walks_back_out_and_enter_at_the_root_ungroups(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        self._act(self.app.action_move_to_group, cursor=1)
        self._send_key("right")
        self.assertEqual(self.app._move.dest, group.id)
        self._send_key("left")
        self.assertIsNone(self.app._move.dest)

        with patch.object(self.app, "notify"):
            self._send_key("enter")

        self.assertIsNone(groups.load(CWD).group_of("swift-otter"))

    def test_a_folder_is_never_offered_a_destination_inside_itself(self) -> None:
        outer = groups.create_group(CWD, "backend")
        groups.create_group(CWD, "auth", parent=outer.id)
        groups.create_group(CWD, "frontend")
        self._draw([])

        self._act(self.app.action_move_to_group, cursor=0)  # moving backend

        candidates = [g.name for g in self.app._move_candidates()]
        self.assertEqual(candidates, ["frontend"])

    def test_escape_cancels_without_moving_anything(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([_row("swift-otter")])

        self._act(self.app.action_move_to_group, cursor=1)
        self._send_key("right")
        with patch.object(self.app, "notify"):
            self._send_key("escape")

        self.assertIsNone(self.app._move)
        self.assertIsNone(groups.load(CWD).group_of("swift-otter"))
        self.assertIn(group.id, groups.load(CWD).groups)

    def test_the_rows_being_moved_and_the_destination_are_both_marked(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([_row("swift-otter")])
        self._act(self.app.action_move_to_group, cursor=1)
        self._send_key("right")

        drawn = self._draw([_row("swift-otter")])
        self.assertIn("drop here", drawn[0][NAME])
        self.assertIn("⇢", drawn[1][NAME])

    def test_the_header_line_says_what_is_being_moved_and_where(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([_row("swift-otter")])
        self._act(self.app.action_move_to_group, cursor=1)

        banner = self.app._move_banner()
        self.assertIn("swift-otter", banner)
        self.assertIn("top level", banner)
        self._send_key("right")
        self.assertIn("backend", self.app._move_banner())

    def test_a_move_can_carry_a_whole_selection(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([_row("a"), _row("b")])
        self._act(self.app.action_extend_selection_down, cursor=1)

        self._act(self.app.action_move_to_group)
        self._send_key("right")
        with patch.object(self.app, "notify"):
            self._send_key("enter")

        state = groups.load(CWD)
        self.assertEqual(state.group_of("a"), group.id)
        self.assertEqual(state.group_of("b"), group.id)


class DraftAgentTests(DashboardFolderTestCase):
    """`n` drafts an agent in place — nothing spawns until Enter."""

    def setUp(self) -> None:
        super().setUp()
        names = patch.object(monitor, "all_session_names_in_cwd", return_value=set())
        names.start()
        self.addCleanup(names.stop)
        suggest = patch.object(monitor, "pick_random_name", return_value="new-agent")
        suggest.start()
        self.addCleanup(suggest.stop)

    def _draft_row(self):
        return next(r for r in self.app.display_rows if r.is_draft)

    def test_n_puts_a_placeholder_row_in_the_tree_without_spawning(self) -> None:
        with patch.object(monitor, "create_session") as create:
            self._act(self.app.action_new_session, cursor=0)

        create.assert_not_called()
        draft = self.app._draft
        self.assertIsNotNone(draft)
        self.assertEqual(draft.buffer, "new-agent")
        self.assertTrue(draft.select_all)
        self.assertEqual(draft.runtime, monitor.RUNTIME_CLAUDE)
        self._draft_row()  # raises if the row isn't in the tree

    def test_the_draft_lands_in_the_folder_the_cursor_is_in(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        # Cursor on the member row, not the header — "the path I'm in".
        self._act(self.app.action_new_session, cursor=1)

        self.assertEqual(self.app._draft.group_id, group.id)
        # And it is drawn inside that folder, as its last child.
        self.assertEqual(self._draft_row().depth, 1)
        self.assertIn("└─", self._draft_row().prefix)

    def test_the_draft_lands_in_the_folder_under_the_cursor(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_new_session, cursor=0)
        self.assertEqual(self.app._draft.group_id, group.id)

    def test_a_loose_cursor_drafts_at_the_top_level(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_new_session, cursor=0)
        self.assertIsNone(self.app._draft.group_id)
        self.assertEqual(self._draft_row().depth, 0)

    def test_the_draft_shows_even_inside_a_folded_folder(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.set_collapsed(CWD, group.id, True)
        self._draw([])

        self._act(self.app.action_new_session, cursor=0)

        self.assertEqual(self.app._draft.group_id, group.id)
        self._draft_row()

    def test_typing_replaces_the_suggested_name(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_new_session, cursor=0)
        self._type("scout")
        self.assertEqual(self.app._draft.buffer, "scout")

    def test_tab_cycles_the_runtime_both_ways(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_new_session, cursor=0)

        self._send_key("tab")
        self.assertEqual(self.app._draft.runtime, monitor.RUNTIME_CODEX)
        self._send_key("tab")
        self.assertEqual(self.app._draft.runtime, monitor.RUNTIME_PI)
        self._send_key("tab")
        self.assertEqual(self.app._draft.runtime, monitor.RUNTIME_CLAUDE)
        self._send_key("shift+tab")
        self.assertEqual(self.app._draft.runtime, monitor.RUNTIME_PI)

    def test_enter_creates_the_session_with_the_chosen_name_and_runtime(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_new_session, cursor=0)
        self._type("scout")
        self._send_key("tab")  # codex

        with patch.object(monitor, "create_session") as create:
            self._send_key("enter")

        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["name"], "scout")
        self.assertEqual(create.call_args.kwargs["runtime"], monitor.RUNTIME_CODEX)
        self.assertIsNone(self.app._draft)
        self.assertEqual(groups.load(CWD).group_of("scout"), group.id)

    def test_claude_args_are_forwarded_only_to_claude(self) -> None:
        self.app.claude_args = ["--permission-mode", "plan"]
        self._draw([_row("swift-otter")])

        self._act(self.app.action_new_session, cursor=0)
        with patch.object(monitor, "create_session") as create:
            self._send_key("enter")
        self.assertEqual(create.call_args.args[1], ["--permission-mode", "plan"])

        self._act(self.app.action_new_session, cursor=0)
        self._send_key("tab")  # codex
        with patch.object(monitor, "create_session") as create:
            self._send_key("enter")
        self.assertEqual(create.call_args.args[1], [])

    def test_a_duplicate_name_keeps_the_draft_open_and_creates_nothing(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_new_session, cursor=0)
        self._type("swift-otter")

        with (
            patch.object(
                monitor, "all_session_names_in_cwd", return_value={"swift-otter"}
            ),
            patch.object(monitor, "create_session") as create,
        ):
            self._send_key("enter")

        create.assert_not_called()
        self.assertIsNotNone(self.app._draft)
        self.assertIn("already used", self.notify.call_args.args[0])

    def test_an_empty_name_keeps_the_draft_open(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_new_session, cursor=0)
        self._send_key("ctrl+u")

        with patch.object(monitor, "create_session") as create:
            self._send_key("enter")

        create.assert_not_called()
        self.assertIsNotNone(self.app._draft)

    def test_escape_discards_the_draft_and_nothing_was_started(self) -> None:
        self._draw([_row("swift-otter")])
        with patch.object(monitor, "create_session") as create:
            self._act(self.app.action_new_session, cursor=0)
            self._type("scout")
            self._send_key("escape")

        create.assert_not_called()
        self.assertIsNone(self.app._draft)
        self.assertFalse(any(r.is_draft for r in self.app.display_rows))

    def test_the_app_level_escape_binding_aborts_the_draft_too(self) -> None:
        # Two things route Esc: the key interceptor (while the table has
        # focus) and this binding. Both must abort, and neither may leave a
        # session behind.
        self._draw([_row("swift-otter")])
        with patch.object(monitor, "create_session") as create:
            self._act(self.app.action_new_session, cursor=0)
            self._act(self.app.action_clear_selection)

        create.assert_not_called()
        self.assertIsNone(self.app._draft)
        self.assertFalse(any(r.is_draft for r in self.app.display_rows))

    def test_a_failed_spawn_reports_and_files_nothing(self) -> None:
        group = groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_new_session, cursor=0)

        with patch.object(
            monitor, "create_session", side_effect=RuntimeError("boom")
        ):
            self._send_key("enter")

        self.assertIn("Spawn failed", self.notify.call_args.args[0])
        self.assertIsNone(groups.load(CWD).group_of("new-agent"))
        self.assertIn(group.id, groups.load(CWD).groups)

    def test_the_draft_row_shows_its_runtime_and_how_to_finish(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_new_session, cursor=0)
        self._send_key("tab")

        drawn = self._draw([_row("swift-otter")])
        draft = next(c for c in drawn if "new" in c[STATUS])
        self.assertIn("codex", draft[RUNTIME])
        self.assertIn("reverse", draft[RUNTIME])
        self.assertIn("esc cancel", draft[PREVIEW])

    def test_the_header_says_what_is_being_created_and_where(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_new_session, cursor=0)
        self.assertIn("backend", self.app._draft_banner())
        self.assertIn("claude", self.app._draft_banner())

    def test_a_draft_row_is_not_an_agent_for_session_actions(self) -> None:
        self._draw([_row("swift-otter")])
        self._act(self.app.action_new_session, cursor=0)
        idx = self.app.display_rows.index(self._draft_row())

        self.table.cursor_row = idx
        with patch.object(self.app, "query_one", return_value=self.table):
            self.assertIsNone(self.app._current_entry())
            self.assertIsNone(self.app._current_group())


class MembershipUpkeepTests(DashboardFolderTestCase):
    """A name that goes back in the pool must not drag a folder with it."""

    def test_deleting_a_dormant_transcript_drops_its_membership(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        self._draw([_row("swift-otter")])

        jsonl = pathlib.Path(self._tmp.name) / "swift-otter.jsonl"
        jsonl.write_text("{}\n")

        with (
            self._cursor_on(1),
            patch.object(self.app, "push_screen") as push,
        ):
            self.app.action_kill()
        confirm = push.call_args.args[1]

        with (
            patch.object(monitor, "live_jsonl_path", return_value=jsonl),
            patch.object(self.app, "refresh_sessions"),
            patch.object(self.app, "notify"),
        ):
            confirm(True)

        # pick_random_name can hand "swift-otter" straight back out now, and
        # the next agent to get it is not this one.
        self.assertFalse(jsonl.exists())
        self.assertIsNone(groups.load(CWD).group_of("swift-otter"))
        self.assertIn(group.id, groups.load(CWD).groups)


class InterruptionTests(DashboardFolderTestCase):
    """A tick must not shove a modal in front of an edit in progress.

    Before folders, ``n`` opened a ModalScreen and the spawn tick checked
    for one. The draft row, the inline rename and the move mode are edits
    in the table itself, so the check has to know about them too.
    """

    def _queue_a_spawn(self) -> None:
        self.app._active_spawn_id = None
        self.app._spawn_queue = [{"id": "req-1", "from": "swift-otter"}]

    def test_a_queued_spawn_waits_for_the_draft_row(self) -> None:
        self._draw([])
        self._act(self.app.action_new_session, cursor=0)
        self._queue_a_spawn()

        with patch.object(self.app, "_show_spawn_modal") as show:
            self.app._tick_spawn_requests()

        show.assert_not_called()
        self.assertEqual(len(self.app._spawn_queue), 1)
        self.assertIsNotNone(self.app._draft)

    def test_a_queued_spawn_waits_for_an_inline_rename(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([])
        self._act(self.app.action_rename_group, cursor=0)
        self._queue_a_spawn()

        with patch.object(self.app, "_show_spawn_modal") as show:
            self.app._tick_spawn_requests()

        show.assert_not_called()

    def test_a_queued_spawn_waits_for_a_move_in_progress(self) -> None:
        groups.create_group(CWD, "backend")
        self._draw([_row("swift-otter")])
        self._act(self.app.action_move_to_group, cursor=1)
        self._queue_a_spawn()

        with patch.object(self.app, "_show_spawn_modal") as show:
            self.app._tick_spawn_requests()

        show.assert_not_called()

    def test_the_tmux_reattach_also_waits(self) -> None:
        self._draw([])
        self._act(self.app.action_new_session, cursor=0)
        self.app._pending_reattach = "multiverse-swift-otter"
        self.app._active_spawn_id = None
        self.app._spawn_queue = []

        with patch.object(self.app, "_attach_to") as attach:
            self.app._tick_spawn_requests()

        attach.assert_not_called()
        self.assertEqual(self.app._pending_reattach, "multiverse-swift-otter")


if __name__ == "__main__":
    unittest.main()
