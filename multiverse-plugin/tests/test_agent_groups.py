"""Folder grouping for the dashboard: the store, and the tree it lays out.

Everything here runs against a real ``MULTIVERSE_ROOT`` in a temp dir, so
the round-trips are through actual files — that's where the interesting
failures live (a mutation that doesn't persist, a repair pass that eats a
valid tree).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from src import groups
from src.sessions import DashboardRow


CWD = "/workspace/project"


def _row(name: str, status: str = "idle", uuid: Optional[str] = None) -> DashboardRow:
    return DashboardRow(
        name=name,
        cwd=CWD,
        status=status,
        status_ts=100.0,
        session_id=uuid or name,
        claude_uuid=uuid or name,
    )


class GroupStoreTestCase(unittest.TestCase):
    """Base: an isolated multiverse root per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_root = os.environ.get("MULTIVERSE_ROOT")
        os.environ["MULTIVERSE_ROOT"] = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prev_root is None:
            os.environ.pop("MULTIVERSE_ROOT", None)
        else:
            os.environ["MULTIVERSE_ROOT"] = self._prev_root
        self._tmp.cleanup()


class StorePersistenceTests(GroupStoreTestCase):
    def test_empty_workspace_has_no_groups(self) -> None:
        state = groups.load(CWD)
        self.assertEqual(state.groups, {})
        self.assertEqual(state.members, {})
        self.assertFalse(groups.state_path(CWD).exists())

    def test_create_assign_and_reload_round_trip(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)

        state = groups.load(CWD)
        self.assertEqual([g.name for g in state.groups.values()], ["backend"])
        self.assertEqual(state.group_of("swift-otter"), group.id)
        # Written where the module says it writes it.
        on_disk = json.loads(groups.state_path(CWD).read_text())
        self.assertEqual(on_disk["version"], groups.STATE_VERSION)

    def test_groups_are_scoped_per_workspace(self) -> None:
        groups.create_group(CWD, "backend")
        self.assertEqual(groups.load("/other/place").groups, {})

    def test_duplicate_sibling_name_is_refused_but_allowed_when_nested(self) -> None:
        parent = groups.create_group(CWD, "backend")
        with self.assertRaises(ValueError):
            groups.create_group(CWD, "Backend")  # case-insensitive clash
        # Same name is fine in a different parent.
        nested = groups.create_group(CWD, "backend", parent=parent.id)
        self.assertEqual(nested.parent, parent.id)

    def test_empty_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            groups.create_group(CWD, "   ")

    def test_rename_rejects_a_sibling_collision_but_allows_a_no_op(self) -> None:
        a = groups.create_group(CWD, "backend")
        groups.create_group(CWD, "frontend")
        with self.assertRaises(ValueError):
            groups.rename_group(CWD, a.id, "frontend")
        groups.rename_group(CWD, a.id, "Backend")  # itself, recased
        self.assertEqual(groups.load(CWD).groups[a.id].name, "Backend")

    def test_collapsed_state_survives_a_reload(self) -> None:
        group = groups.create_group(CWD, "backend")
        self.assertTrue(groups.toggle_collapsed(CWD, group.id))
        self.assertTrue(groups.load(CWD).groups[group.id].collapsed)
        self.assertFalse(groups.toggle_collapsed(CWD, group.id))
        self.assertFalse(groups.load(CWD).groups[group.id].collapsed)

    def test_set_all_collapsed_covers_every_group(self) -> None:
        a = groups.create_group(CWD, "backend")
        groups.create_group(CWD, "auth", parent=a.id)
        groups.set_all_collapsed(CWD, True)
        self.assertTrue(all(g.collapsed for g in groups.load(CWD).groups.values()))
        groups.set_all_collapsed(CWD, False)
        self.assertFalse(any(g.collapsed for g in groups.load(CWD).groups.values()))

    def test_assigning_to_a_missing_group_raises_and_changes_nothing(self) -> None:
        with self.assertRaises(ValueError):
            groups.assign_agent(CWD, "swift-otter", "g-nope")
        self.assertIsNone(groups.load(CWD).group_of("swift-otter"))

    def test_a_failed_mutation_does_not_write_a_partial_state(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        before = groups.state_path(CWD).read_text()
        with self.assertRaises(ValueError):
            groups.create_group(CWD, "backend")
        self.assertEqual(groups.state_path(CWD).read_text(), before)


class MemberKeyTests(unittest.TestCase):
    def test_display_suffixes_are_stripped_so_ghosts_file_with_the_agent(self) -> None:
        self.assertEqual(groups.member_key("swift-otter (folded 14:03)"), "swift-otter")
        self.assertEqual(groups.member_key("swift-otter (folded Mar 04)"), "swift-otter")
        self.assertEqual(groups.member_key("swift-otter (bg fork)"), "swift-otter")

    def test_stacked_suffixes_and_idempotence(self) -> None:
        key = groups.member_key("swift-otter (folded 14:03) (folded 09:12)")
        self.assertEqual(key, "swift-otter")
        self.assertEqual(groups.member_key(key), key)

    def test_none_and_blank_are_safe(self) -> None:
        self.assertEqual(groups.member_key(None), "")
        self.assertEqual(groups.member_key("   "), "")

    def test_a_parenthetical_that_is_not_a_suffix_is_left_alone(self) -> None:
        self.assertEqual(groups.member_key("agent (v2)"), "agent (v2)")

    def test_a_folded_background_fork_strips_both_suffixes(self) -> None:
        # build_dashboard_rows wraps the fork title in the fold suffix, so
        # the two land in this order and stripping has to survive it.
        self.assertEqual(
            groups.member_key("swift-otter (bg fork) (folded 02:33)"),
            "swift-otter",
        )
        self.assertEqual(
            groups.member_key("swift-otter (folded 02:33) (bg fork)"),
            "swift-otter",
        )


class GhostMembershipTests(GroupStoreTestCase):
    def test_a_folded_ghost_row_resolves_to_the_live_agent_folder(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        state = groups.load(CWD)
        self.assertEqual(state.group_of("swift-otter (folded 14:03)"), group.id)


class WrapTests(GroupStoreTestCase):
    def test_wrapping_same_named_folders_keeps_their_names_unique(self) -> None:
        one = groups.create_group(CWD, "frontend-work")
        two = groups.create_group(CWD, "api-work")
        a = groups.create_group(CWD, "backend", parent=one.id)
        b = groups.create_group(CWD, "backend", parent=two.id)

        wrap = groups.wrap_in_new_folder(CWD, group_ids=[a.id, b.id])

        state = groups.load(CWD)
        names = sorted(g.name for g in state.children_of(wrap.group.id))
        self.assertEqual(names, ["backend", "backend 2"])

    def test_undoing_a_wrap_restores_a_name_it_had_to_change(self) -> None:
        one = groups.create_group(CWD, "frontend-work")
        two = groups.create_group(CWD, "api-work")
        a = groups.create_group(CWD, "backend", parent=one.id)
        b = groups.create_group(CWD, "backend", parent=two.id)

        wrap = groups.wrap_in_new_folder(CWD, group_ids=[a.id, b.id])
        groups.undo_wrap(CWD, wrap)

        state = groups.load(CWD)
        self.assertEqual(state.groups[a.id].name, "backend")
        self.assertEqual(state.groups[b.id].name, "backend")
        self.assertEqual(state.groups[a.id].parent, one.id)
        self.assertEqual(state.groups[b.id].parent, two.id)

    def test_wrapping_nothing_makes_an_empty_folder_where_asked(self) -> None:
        backend = groups.create_group(CWD, "backend")

        wrap = groups.wrap_in_new_folder(CWD, parent=backend.id)

        state = groups.load(CWD)
        self.assertEqual(state.groups[wrap.group.id].parent, backend.id)
        self.assertEqual(
            [g.id for g in state.children_of(backend.id)], [wrap.group.id]
        )

    def test_wrapping_nothing_with_a_dead_parent_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            groups.wrap_in_new_folder(CWD, parent="g-gone")

    def test_a_batch_move_renames_the_same_loser_every_run(self) -> None:
        # _prune_contained used to iterate a set, so which of two identically
        # named folders got renamed depended on hash randomization.
        dest = groups.create_group(CWD, "dest")
        one = groups.create_group(CWD, "frontend-work")
        two = groups.create_group(CWD, "api-work")
        first = groups.create_group(CWD, "backend", parent=one.id)
        second = groups.create_group(CWD, "backend", parent=two.id)

        groups.move_into(CWD, dest.id, group_ids=[first.id, second.id])

        state = groups.load(CWD)
        self.assertEqual(state.groups[first.id].name, "backend")
        self.assertEqual(state.groups[second.id].name, "backend 2")


class HierarchyTests(GroupStoreTestCase):
    def test_delete_promotes_members_and_subgroups_to_the_parent(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        deepest = groups.create_group(CWD, "tokens", parent=inner.id)
        groups.assign_agent(CWD, "swift-otter", inner.id)

        groups.delete_group(CWD, inner.id)

        state = groups.load(CWD)
        self.assertNotIn(inner.id, state.groups)
        self.assertEqual(state.groups[deepest.id].parent, outer.id)
        self.assertEqual(state.group_of("swift-otter"), outer.id)

    def test_deleting_a_root_folder_drops_members_to_the_top_level(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        groups.delete_group(CWD, group.id)
        state = groups.load(CWD)
        self.assertEqual(state.groups, {})
        self.assertIsNone(state.group_of("swift-otter"))

    def test_deleting_an_unknown_group_is_a_no_op(self) -> None:
        groups.create_group(CWD, "backend")
        groups.delete_group(CWD, "g-nope")
        self.assertEqual(len(groups.load(CWD).groups), 1)

    def test_a_folder_cannot_be_moved_inside_itself_or_a_descendant(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)

        # Skipped rather than raised: a batch move the user can see beats
        # an error they can't act on.
        self.assertEqual(groups.move_into(CWD, outer.id, group_ids=[outer.id]), 0)
        self.assertEqual(groups.move_into(CWD, inner.id, group_ids=[outer.id]), 0)

        self.assertIsNone(groups.load(CWD).groups[outer.id].parent)

    def test_moving_into_a_name_clash_uniquifies_rather_than_failing(self) -> None:
        outer = groups.create_group(CWD, "backend")
        groups.create_group(CWD, "auth", parent=outer.id)
        loose = groups.create_group(CWD, "auth")

        groups.move_into(CWD, outer.id, group_ids=[loose.id])

        state = groups.load(CWD)
        self.assertEqual(state.groups[loose.id].parent, outer.id)
        self.assertEqual(state.groups[loose.id].name, "auth 2")

    def test_path_names_reads_root_first(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        self.assertEqual(groups.load(CWD).path_names(inner.id), ["backend", "auth"])

    def test_members_of_can_include_the_whole_subtree(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        groups.assign_agent(CWD, "swift-otter", outer.id)
        groups.assign_agent(CWD, "brave-mole", inner.id)
        state = groups.load(CWD)
        self.assertEqual(state.members_of(outer.id), {"swift-otter"})
        self.assertEqual(
            state.members_of(outer.id, recursive=True),
            {"swift-otter", "brave-mole"},
        )


class CorruptStateTests(GroupStoreTestCase):
    def _write_raw(self, payload: str) -> None:
        path = groups.state_path(CWD)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)

    def test_unparseable_file_reads_as_no_groups(self) -> None:
        self._write_raw("{not json")
        self.assertEqual(groups.load(CWD).groups, {})

    def test_non_object_payload_reads_as_no_groups(self) -> None:
        self._write_raw("[1, 2, 3]")
        self.assertEqual(groups.load(CWD).groups, {})

    def test_a_dangling_parent_becomes_a_top_level_folder(self) -> None:
        self._write_raw(json.dumps({
            "version": 1,
            "groups": {"g-1": {"id": "g-1", "name": "orphan", "parent": "g-gone"}},
            "members": {},
        }))
        self.assertIsNone(groups.load(CWD).groups["g-1"].parent)

    def test_a_parent_cycle_is_broken_instead_of_hanging(self) -> None:
        self._write_raw(json.dumps({
            "version": 1,
            "groups": {
                "g-1": {"id": "g-1", "name": "a", "parent": "g-2"},
                "g-2": {"id": "g-2", "name": "b", "parent": "g-1"},
            },
            "members": {},
        }))
        state = groups.load(CWD)
        parents = {g.id: g.parent for g in state.groups.values()}
        self.assertIn(None, parents.values())
        # And the tree still lays out, which is the thing a cycle breaks.
        rows = groups.build_display_rows([], state)
        self.assertEqual(len(rows), 2)

    def test_membership_pointing_at_a_deleted_group_reads_as_ungrouped(self) -> None:
        self._write_raw(json.dumps({
            "version": 1,
            "groups": {},
            "members": {"swift-otter": "g-gone"},
        }))
        self.assertIsNone(groups.load(CWD).group_of("swift-otter"))

    def test_malformed_entries_are_dropped_not_fatal(self) -> None:
        self._write_raw(json.dumps({
            "version": 1,
            "groups": {
                "g-1": {"id": "g-1", "name": "", "parent": None},
                "g-2": {"id": "g-2", "name": "keep", "created_at": "nonsense"},
                "g-3": "not-a-dict",
            },
            # JSON keys are always strings, so only the values can be
            # the wrong type here.
            "members": {"a": 5, "b": ["g-2"], "c": "g-gone"},
        }))
        state = groups.load(CWD)
        self.assertEqual(list(state.groups), ["g-2"])
        self.assertEqual(state.groups["g-2"].created_at, 0.0)
        self.assertEqual(state.members, {})


class AutoGroupOnSpawnTests(GroupStoreTestCase):
    def test_a_loose_spawner_gets_a_folder_holding_both_agents(self) -> None:
        gid = groups.auto_group_spawn(CWD, "swift-otter", "brave-mole")
        state = groups.load(CWD)
        self.assertEqual(state.groups[gid].name, "swift-otter")
        self.assertEqual(state.group_of("swift-otter"), gid)
        self.assertEqual(state.group_of("brave-mole"), gid)

    def test_a_second_spawn_joins_the_folder_that_already_exists(self) -> None:
        first = groups.auto_group_spawn(CWD, "swift-otter", "brave-mole")
        second = groups.auto_group_spawn(CWD, "swift-otter", "keen-heron")
        self.assertEqual(first, second)
        self.assertEqual(len(groups.load(CWD).groups), 1)

    def test_a_grandchild_lands_in_its_own_parents_folder(self) -> None:
        gid = groups.auto_group_spawn(CWD, "swift-otter", "brave-mole")
        again = groups.auto_group_spawn(CWD, "brave-mole", "keen-heron")
        self.assertEqual(again, gid)
        self.assertEqual(groups.load(CWD).group_of("keen-heron"), gid)

    def test_a_hand_made_folder_is_respected_rather_than_duplicated(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        gid = groups.auto_group_spawn(CWD, "swift-otter", "brave-mole")
        self.assertEqual(gid, group.id)
        self.assertEqual(len(groups.load(CWD).groups), 1)

    def test_a_folder_name_already_taken_gets_a_suffix(self) -> None:
        groups.create_group(CWD, "swift-otter")
        gid = groups.auto_group_spawn(CWD, "swift-otter", "brave-mole")
        self.assertEqual(groups.load(CWD).groups[gid].name, "swift-otter 2")

    def test_degenerate_inputs_do_nothing(self) -> None:
        self.assertIsNone(groups.auto_group_spawn(CWD, "", "brave-mole"))
        self.assertIsNone(groups.auto_group_spawn(CWD, "swift-otter", ""))
        self.assertIsNone(groups.auto_group_spawn(CWD, "same", "same"))
        self.assertEqual(groups.load(CWD).groups, {})

    def test_the_spawner_keeps_its_place_when_it_is_nested(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        groups.assign_agent(CWD, "swift-otter", inner.id)
        gid = groups.auto_group_spawn(CWD, "swift-otter", "brave-mole")
        self.assertEqual(gid, inner.id)


class DisplayTreeTests(GroupStoreTestCase):
    def _layout(self, entries: list[DashboardRow]) -> list[str]:
        state = groups.load(CWD)
        out = []
        for row in groups.build_display_rows(entries, state):
            if row.is_group:
                chevron = (
                    groups.CHEVRON_CLOSED
                    if row.group.collapsed
                    else groups.CHEVRON_OPEN
                )
                out.append(f"{row.prefix}{chevron} {row.group.name} ({row.agent_count})")
            else:
                out.append(f"{row.prefix}{row.entry.name}")
        return out

    def test_a_workspace_with_no_folders_renders_exactly_as_before(self) -> None:
        entries = [_row("swift-otter"), _row("brave-mole")]
        rows = groups.build_display_rows(entries, groups.load(CWD))
        self.assertEqual([r.entry.name for r in rows], ["swift-otter", "brave-mole"])
        self.assertEqual({r.prefix for r in rows}, {""})
        self.assertEqual({r.depth for r in rows}, {0})

    def test_nested_folders_draw_connectors_that_close_correctly(self) -> None:
        backend = groups.create_group(CWD, "backend")
        auth = groups.create_group(CWD, "auth-work", parent=backend.id)
        groups.assign_agent(CWD, "swift-otter", auth.id)
        groups.assign_agent(CWD, "brave-mole", auth.id)
        groups.assign_agent(CWD, "dev-agent", backend.id)

        layout = self._layout([
            _row("swift-otter"),
            _row("brave-mole"),
            _row("dev-agent"),
            _row("quiet-fox"),
        ])
        self.assertEqual(layout, [
            "▼ backend (3)",
            "├─ ▼ auth-work (2)",
            "│  ├─ swift-otter",
            "│  └─ brave-mole",
            "└─ dev-agent",
            "quiet-fox",
        ])

    def test_folders_sort_before_loose_agents_at_every_level(self) -> None:
        group = groups.create_group(CWD, "zeta")
        groups.assign_agent(CWD, "swift-otter", group.id)
        layout = self._layout([_row("alpha-agent"), _row("swift-otter")])
        self.assertEqual(layout, [
            "▼ zeta (1)",
            "└─ swift-otter",
            "alpha-agent",
        ])

    def test_sibling_folders_are_ordered_by_name(self) -> None:
        groups.create_group(CWD, "zeta")
        groups.create_group(CWD, "Alpha")
        layout = self._layout([])
        self.assertEqual(layout, ["▼ Alpha (0)", "▼ zeta (0)"])

    def test_the_existing_agent_sort_is_preserved_inside_a_folder(self) -> None:
        group = groups.create_group(CWD, "backend")
        for name in ("c-agent", "a-agent", "b-agent"):
            groups.assign_agent(CWD, name, group.id)
        # Incoming order is the dashboard's own sort (status, recency) — the
        # tree must not re-sort it.
        layout = self._layout([_row("c-agent"), _row("a-agent"), _row("b-agent")])
        self.assertEqual(layout[1:], ["├─ c-agent", "├─ a-agent", "└─ b-agent"])

    def test_a_collapsed_folder_hides_its_subtree_but_still_counts_it(self) -> None:
        backend = groups.create_group(CWD, "backend")
        auth = groups.create_group(CWD, "auth-work", parent=backend.id)
        groups.assign_agent(CWD, "swift-otter", auth.id)
        groups.assign_agent(CWD, "brave-mole", backend.id)
        groups.set_collapsed(CWD, backend.id, True)

        entries = [_row("swift-otter"), _row("brave-mole"), _row("quiet-fox")]
        self.assertEqual(self._layout(entries), ["▶ backend (2)", "quiet-fox"])

        rows = groups.build_display_rows(entries, groups.load(CWD))
        self.assertEqual(
            {a.name for a in rows[0].agents}, {"swift-otter", "brave-mole"}
        )

    def test_an_empty_folder_still_shows_as_a_row(self) -> None:
        groups.create_group(CWD, "backend")
        self.assertEqual(self._layout([]), ["▼ backend (0)"])

    def test_an_agent_whose_folder_vanished_shows_at_the_top_level(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        groups.delete_group(CWD, group.id)
        self.assertEqual(self._layout([_row("swift-otter")]), ["swift-otter"])

    def test_a_ghost_row_is_drawn_inside_its_agents_folder(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        layout = self._layout([
            _row("swift-otter"),
            _row("swift-otter (folded 14:03)"),
        ])
        self.assertEqual(layout, [
            "▼ backend (2)",
            "├─ swift-otter",
            "└─ swift-otter (folded 14:03)",
        ])

    def test_row_keys_distinguish_folders_from_agents(self) -> None:
        group = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", group.id)
        rows = groups.build_display_rows([_row("swift-otter")], groups.load(CWD))
        self.assertEqual(rows[0].key, f"grp:{group.id}")
        self.assertEqual(rows[1].key, rows[1].entry.row_key)
        self.assertNotEqual(rows[0].key, rows[1].key)


if __name__ == "__main__":
    unittest.main()
