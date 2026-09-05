"""Agents spawned by agents land in one folder — driven through the real tool.

``tests/test_agent_groups.py`` covers ``groups.auto_group_spawn`` on its own.
This file goes through ``server.spawn_agent`` itself, against a real group
store on disk, and finishes at the rendered dashboard tree — because the
useful claim is not "the helper works", it is "delegating twice puts the
whole lineage in one folder on screen".

Only the parts that would touch the machine are stubbed: ``create_session``
(tmux), the request/resolution files, and the tmux nudge. The identity
resolution, the cwd plumbing, the store, and the layout are the real thing.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Optional
from unittest.mock import patch

from src import groups, monitor, server, sessions
from src.sessions import DashboardRow


CWD = "/workspace/project"


def _row(name: str) -> DashboardRow:
    return DashboardRow(
        name=name,
        cwd=CWD,
        status="idle",
        status_ts=1000.0,
        session_id=f"sid-{name}",
    )


class SpawnGroupingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("MULTIVERSE_ROOT")
        os.environ["MULTIVERSE_ROOT"] = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prev is None:
            os.environ.pop("MULTIVERSE_ROOT", None)
        else:
            os.environ["MULTIVERSE_ROOT"] = self._prev
        self._tmp.cleanup()

    async def _spawn(
        self,
        parent: str,
        child: str,
        *,
        runtime: str = sessions.RUNTIME_CLAUDE,
        status: str = "accepted",
        create_error: Optional[Exception] = None,
    ) -> str:
        """One full spawn_agent round trip, as an agent named ``parent``."""
        resolution = {"status": status, "name": child, "runtime": runtime}
        env = {
            "MULTIVERSE_SESSION_CWD": CWD,
            "MULTIVERSE_SESSION_NAME": parent,
            "MULTIVERSE_SESSION_ID": f"{parent}-id",
            "MULTIVERSE_RUNTIME": sessions.RUNTIME_CLAUDE,
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(server, "all_session_names_in_cwd", return_value=set()),
            patch.object(server, "new_request_id", return_value=f"req-{child}"),
            patch.object(server, "write_spawn_request"),
            patch.object(server, "read_resolution", return_value=resolution),
            patch.object(server, "cleanup_request"),
            patch.object(server, "create_session", side_effect=create_error),
            patch.object(server.subprocess, "run"),
        ):
            return await server.spawn_agent(instruction="Do the work.", name=child)

    def _folder_of(self, agent: str) -> Optional[str]:
        return groups.load(CWD).group_of(agent)

    # -- the basic claim ------------------------------------------------

    async def test_a_spawn_puts_the_spawner_and_the_child_in_one_folder(self) -> None:
        await self._spawn("swift-otter", "brave-mole")

        state = groups.load(CWD)
        folder = state.group_of("swift-otter")
        self.assertIsNotNone(folder)
        self.assertEqual(state.group_of("brave-mole"), folder)
        # Named after whoever did the delegating, so the folder says why it
        # exists.
        self.assertEqual(state.groups[folder].name, "swift-otter")

    async def test_the_tool_tells_the_spawner_where_the_child_went(self) -> None:
        result = await self._spawn("swift-otter", "brave-mole")
        self.assertEqual(
            result,
            "Spawned agent with name 'brave-mole' in folder 'swift-otter'.",
        )

    async def test_further_spawns_reuse_the_folder_instead_of_making_more(self) -> None:
        await self._spawn("swift-otter", "brave-mole")
        await self._spawn("swift-otter", "keen-heron")

        state = groups.load(CWD)
        self.assertEqual(len(state.groups), 1)
        folder = state.group_of("swift-otter")
        self.assertEqual(state.group_of("brave-mole"), folder)
        self.assertEqual(state.group_of("keen-heron"), folder)

    async def test_a_grandchild_joins_the_same_lineage(self) -> None:
        await self._spawn("swift-otter", "brave-mole")
        await self._spawn("brave-mole", "keen-heron")

        state = groups.load(CWD)
        self.assertEqual(len(state.groups), 1)
        self.assertEqual(
            {state.group_of(n) for n in ("swift-otter", "brave-mole", "keen-heron")},
            {state.group_of("swift-otter")},
        )

    async def test_two_separate_lineages_get_separate_folders(self) -> None:
        await self._spawn("swift-otter", "brave-mole")
        await self._spawn("quiet-fox", "night-owl")

        state = groups.load(CWD)
        self.assertEqual(len(state.groups), 2)
        self.assertNotEqual(
            state.group_of("swift-otter"), state.group_of("quiet-fox")
        )
        self.assertEqual(
            state.group_of("brave-mole"), state.group_of("swift-otter")
        )

    # -- respecting what the user set up --------------------------------

    async def test_a_spawner_the_user_filed_keeps_its_folder(self) -> None:
        backend = groups.create_group(CWD, "backend")
        groups.assign_agent(CWD, "swift-otter", backend.id)

        await self._spawn("swift-otter", "brave-mole")

        state = groups.load(CWD)
        self.assertEqual(list(state.groups), [backend.id])
        self.assertEqual(state.group_of("brave-mole"), backend.id)

    async def test_a_spawner_in_a_nested_folder_keeps_its_depth(self) -> None:
        outer = groups.create_group(CWD, "backend")
        inner = groups.create_group(CWD, "auth", parent=outer.id)
        groups.assign_agent(CWD, "swift-otter", inner.id)

        await self._spawn("swift-otter", "brave-mole")

        self.assertEqual(self._folder_of("brave-mole"), inner.id)

    async def test_a_folder_name_already_taken_does_not_collide(self) -> None:
        groups.create_group(CWD, "swift-otter")  # a folder, not the agent's

        await self._spawn("swift-otter", "brave-mole")

        state = groups.load(CWD)
        folder = state.group_of("swift-otter")
        self.assertEqual(state.groups[folder].name, "swift-otter 2")

    # -- nothing spawned, nothing filed ---------------------------------

    async def test_a_rejected_spawn_groups_nothing(self) -> None:
        result = await self._spawn("swift-otter", "brave-mole", status="rejected")

        self.assertIn("rejected", result)
        self.assertEqual(groups.load(CWD).groups, {})
        self.assertIsNone(self._folder_of("swift-otter"))

    async def test_a_failed_create_session_groups_nothing(self) -> None:
        result = await self._spawn(
            "swift-otter", "brave-mole", create_error=RuntimeError("no tmux")
        )

        self.assertIn("failed to create session", result)
        self.assertEqual(groups.load(CWD).groups, {})

    async def test_grouping_never_turns_a_good_spawn_into_an_error(self) -> None:
        # The session is already running by this point; a bookkeeping
        # failure must not report the spawn as failed.
        with patch.object(
            groups, "auto_group_spawn", side_effect=OSError("read-only store")
        ):
            result = await self._spawn("swift-otter", "brave-mole")

        self.assertEqual(result, "Spawned agent with name 'brave-mole'.")

    # -- and what it looks like on the dashboard ------------------------

    async def test_the_lineage_renders_as_one_folder_on_the_dashboard(self) -> None:
        await self._spawn("swift-otter", "brave-mole")
        await self._spawn("swift-otter", "keen-heron")

        entries = [
            _row("swift-otter"),
            _row("brave-mole"),
            _row("keen-heron"),
            _row("unrelated-agent"),
        ]
        rendered = []
        for row in groups.build_display_rows(entries, groups.load(CWD)):
            label = row.group.name if row.is_group else row.entry.name
            rendered.append(f"{row.prefix}{label}")

        self.assertEqual(rendered, [
            "swift-otter",            # the folder header
            "├─ swift-otter",         # the spawner
            "├─ brave-mole",
            "└─ keen-heron",
            "unrelated-agent",
        ])

    async def test_the_folder_survives_the_agents_going_dormant(self) -> None:
        # Membership is keyed by name, so a row that comes back as a
        # dormant transcript (different session id) is still in the folder.
        await self._spawn("swift-otter", "brave-mole")

        dormant = DashboardRow(
            name="brave-mole",
            cwd=CWD,
            status="dormant",
            status_ts=1.0,
            session=None,
            session_id="a-completely-different-uuid",
            claude_uuid="a-completely-different-uuid",
        )
        state = groups.load(CWD)
        self.assertEqual(
            state.group_of(dormant.name), state.group_of("swift-otter")
        )


class MonitorSideFilingTests(unittest.TestCase):
    """The monitor files the pair too — once the child actually exists.

    The MCP server that calls ``auto_group_spawn`` is the plugin build the
    *agent* started with, so an agent that has been running since before
    folders existed would never file its children. The monitor is the
    process the user actually restarts, and it is the authoritative
    resolver, so it does the same filing — and the two agree, because
    whichever runs first puts the spawner in a folder and the other joins
    it.

    Approving is not creating, though: the agent still has to run
    ``create_session``, which can fail or never run at all. So the monitor
    remembers the approved pair and files it on the refresh that first
    sees the new agent's row.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("MULTIVERSE_ROOT")
        os.environ["MULTIVERSE_ROOT"] = self._tmp.name
        self.addCleanup(self._restore)
        self.app = monitor.MonitorApp([])
        self.app.monitor_cwd = CWD
        self.req = {
            "id": "req-1",
            "from": "swift-otter",
            "cwd": CWD,
            "instruction": "Do the work.",
            "default_name": "brave-mole",
            "caller_tmux_session": "multiverse-caller",
            "caller_runtime": sessions.RUNTIME_CLAUDE,
        }

    def _restore(self) -> None:
        if self._prev is None:
            os.environ.pop("MULTIVERSE_ROOT", None)
        else:
            os.environ["MULTIVERSE_ROOT"] = self._prev
        self._tmp.cleanup()

    def _arrive(self, *names: str) -> None:
        """The refresh that first sees the spawned agent's row."""
        self.app.entries = [_row(n) for n in names]
        self.app._file_arrived_spawns()

    def _decide(self, decision, *, won: bool = True):
        with (
            patch.object(monitor, "write_resolution", return_value=won),
            patch.object(monitor, "read_resolution", return_value=None),
            patch.object(self.app, "push_screen") as push,
            patch.object(self.app, "notify"),
        ):
            self.app._show_spawn_modal(self.req)
            on_close = push.call_args.args[1]
            on_close(decision)

    def test_the_pair_is_filed_once_the_child_exists(self) -> None:
        self._decide(
            monitor.SpawnDecision(
                accepted=True,
                name="brave-mole",
                runtime=sessions.RUNTIME_CLAUDE,
            )
        )

        # Approval alone files nothing: create_session has not run yet.
        self.assertEqual(groups.load(CWD).groups, {})

        self._arrive("swift-otter", "brave-mole")

        state = groups.load(CWD)
        folder = state.group_of("swift-otter")
        self.assertIsNotNone(folder)
        self.assertEqual(state.groups[folder].name, "swift-otter")
        self.assertEqual(state.group_of("brave-mole"), folder)

    def test_the_user_renaming_the_child_files_the_name_they_chose(self) -> None:
        self._decide(
            monitor.SpawnDecision(
                accepted=True,
                name="renamed-child",
                runtime=sessions.RUNTIME_CLAUDE,
            )
        )
        self._arrive("swift-otter", "renamed-child")
        state = groups.load(CWD)
        self.assertEqual(
            state.group_of("renamed-child"), state.group_of("swift-otter")
        )
        self.assertIsNone(state.group_of("brave-mole"))

    def test_rejecting_files_nothing(self) -> None:
        self._decide(monitor.SpawnDecision(accepted=False, reason="not now"))
        self.assertEqual(groups.load(CWD).groups, {})

    def test_an_external_dismissal_files_nothing(self) -> None:
        self._decide(None)
        self.assertEqual(groups.load(CWD).groups, {})

    def test_losing_the_resolution_race_files_nothing(self) -> None:
        # Another monitor won and is doing the filing; doing it here too
        # would be filing against a name we didn't get to choose.
        self._decide(
            monitor.SpawnDecision(
                accepted=True,
                name="brave-mole",
                runtime=sessions.RUNTIME_CLAUDE,
            ),
            won=False,
        )
        self.assertEqual(groups.load(CWD).groups, {})

    def test_a_store_failure_does_not_break_the_approval(self) -> None:
        with patch.object(
            monitor.groups, "auto_group_spawn", side_effect=OSError("nope")
        ):
            self._decide(
                monitor.SpawnDecision(
                    accepted=True,
                    name="brave-mole",
                    runtime=sessions.RUNTIME_CLAUDE,
                )
            )
            self._arrive("swift-otter", "brave-mole")
        self.assertEqual(groups.load(CWD).groups, {})

    def test_a_spawn_that_never_starts_files_nothing(self) -> None:
        # create_session failed, or the requesting agent died before it
        # ever ran: the child never appears, so neither does a folder.
        self._decide(
            monitor.SpawnDecision(
                accepted=True,
                name="brave-mole",
                runtime=sessions.RUNTIME_CLAUDE,
            )
        )
        self._arrive("swift-otter")

        self.assertEqual(groups.load(CWD).groups, {})
        self.assertEqual(groups.load(CWD).members, {})

    def test_the_monitor_and_the_agent_agree_rather_than_duplicating(self) -> None:
        # The monitor files the pair when the child turns up; the agent's
        # own MCP server then calls auto_group_spawn for the same pair. The
        # second call must find the spawner already filed and join it, not
        # create a second folder.
        self._decide(
            monitor.SpawnDecision(
                accepted=True,
                name="brave-mole",
                runtime=sessions.RUNTIME_CLAUDE,
            )
        )
        self._arrive("swift-otter", "brave-mole")
        before = groups.load(CWD)
        groups.auto_group_spawn(CWD, "swift-otter", "brave-mole")
        after = groups.load(CWD)

        self.assertEqual(list(after.groups), list(before.groups))
        self.assertEqual(after.members, before.members)


if __name__ == "__main__":
    unittest.main()
