"""Moving and deleting from the dashboard, against real universes on disk."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import DataTable, Static
from multiverse import universe
from multiverse.runtime import lifecycle
from multiverse.universe import relocate, templates, trash
from multiverse.ui.backend import LocalBackend
from multiverse.ui.tree import agent_key, group_key
from multiverse.store import read, write
from multiverse.ui.app import Form, MultiverseApp


class TreeEditingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.root = templates.create(self.work / ".multiverse", "top", workdir=self.work)
        self.lab = templates.create(self.root / "subverses/lab", "lab", workdir=self.work)
        self.deep = templates.create(self.lab / "subverses/deep", "deep", workdir=self.work)
        self.review = templates.create(self.root / "subverses/review", "review", workdir=self.work)
        self.ids = {}
        for scope, name in ((self.root, "idle"), (self.lab, "worker"), (self.deep, "inner"), (self.review, "busy")):
            agent_id = f"00000000-0000-4000-8000-{len(self.ids) + 1:012d}"
            self.ids[name] = agent_id
            write(scope / "state/agents" / agent_id / "properties.json",
                  {"id": agent_id, "name": name, "harness": "claude", "universe_id": read(scope / "universe.json")["id"],
                   "workspace": str(self.work), "task": "", "phase": "done", "summary": ""})
            write(scope / "state/agents" / agent_id / "runtime.json", {"launch_id": "launch", "state": "stopped"})
        # Only "busy" has a live terminal; the listing and relocate agree on that.
        observed = lambda properties, *_args: {"live": "alive" if properties["name"] == "busy" else "stopped",
                                               "reason": ""}
        for module in (lifecycle, relocate):
            patcher = patch.object(module, "observe", observed)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = MultiverseApp(self.root, LocalBackend(), refresh_interval=0)

    def tearDown(self):
        self.temp.cleanup()

    async def settle(self, pilot):
        await pilot.pause()
        await self.app.workers.wait_for_complete()
        await pilot.pause()

    def key(self, scope, name):
        return agent_key({"universe": str(scope), "id": self.ids[name]})

    def status(self):
        return str(self.app.query_one("#status", Static).render())

    async def test_move_agent_by_keyboard_with_navigation_and_refusals(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            footer = {active.binding.action for active in self.app.active_bindings.values() if active.binding.show}
            self.assertTrue({"cut", "kill"} <= footer)
            self.assertNotIn("remove", footer)
            self.app.select_key(self.key(self.review, "busy"))
            await pilot.press("x")
            self.assertIsNone(self.app.moving)
            self.assertIn("Stop it first", self.status())
            self.app.select_key(self.key(self.root, "idle"))
            await pilot.press("x")
            self.assertIn("moving", str(self.app.query_one("#agents", DataTable).get_cell(self.key(self.root, "idle"), "name")))
            await pilot.press("n", "g", "d")
            self.assertIsNone(self.app.draft)
            self.assertEqual(len(self.app.screen_stack), 1)
            await pilot.press("escape")
            self.assertIsNone(self.app.moving)
            await pilot.press("x")
            # Tree keys still work while a destination is chosen.
            self.app.select_key(group_key(self.lab))
            await pilot.press("left")
            self.assertNotIn(group_key(self.deep), self.app.visible_rows)
            await pilot.press("right")
            self.assertIn(group_key(self.deep), self.app.visible_rows)
            await pilot.press("enter")
            await self.settle(pilot)
            self.assertIsNone(self.app.moving)
            moved = self.lab / "state/agents" / self.ids["idle"]
            self.assertEqual(read(moved / "properties.json")["universe_id"], read(self.lab / "universe.json")["id"])
            self.assertEqual(self.app.selected_key, self.key(self.lab, "idle"))
            await pilot.press("x")
            self.app.select_key(group_key(self.lab))
            await pilot.press("enter")
            await self.settle(pilot)
            self.assertIn("already in that universe", self.status())
            self.assertIsNotNone(self.app.moving, "A refused destination keeps the move pending")
            await pilot.press("q")
            self.assertIsNone(self.app.moving)
            self.assertTrue(self.app.is_running)

    async def test_move_subverse_with_x_and_group_refusals(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            self.app.select_key(group_key(self.root))
            await pilot.press("x")
            self.assertIn("top-level universe cannot be moved", self.status())
            self.app.select_key(group_key(self.review))
            await pilot.press("x")
            self.assertIn("Stop 1 running agent inside first", self.status())
            self.app.select_key(group_key(self.lab))
            await pilot.press("x")
            self.app.select_key(group_key(self.deep))
            await pilot.press("enter")
            await self.settle(pilot)
            self.assertIn("into itself", self.status())
            await pilot.press("escape")
            self.app.select_key(group_key(self.deep))
            await pilot.press("x")
            self.app.select_key(group_key(self.review))
            await pilot.press("x")
            await self.settle(pilot)
            moved = self.review / "subverses/deep"
            self.assertIsNone(self.app.moving)
            self.assertFalse(self.deep.exists())
            self.assertEqual(universe.workspace(moved), self.work.resolve())
            self.assertEqual(self.app.selected_key, group_key(moved))
            self.assertEqual(self.app.visible_rows[group_key(moved)]["parent_key"], group_key(self.review))

    async def test_delete_agent_and_subverse_after_confirmation(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            self.app.select_key(self.key(self.root, "idle"))
            await pilot.press("d")
            self.assertIsInstance(self.app.screen, Form)
            await pilot.press("escape")
            self.assertTrue((self.root / "state/agents" / self.ids["idle"]).is_dir())
            await pilot.press("d", "ctrl+enter")
            await self.settle(pilot)
            self.assertFalse((self.root / "state/agents" / self.ids["idle"]).exists())
            self.assertEqual([(e["kind"], e["name"]) for e in trash.contents(self.root)["entries"]], [("agent", "idle")])
            self.assertIn(self.app.selected_key, self.app.visible_rows)
            self.app.select_key(group_key(self.root))
            await pilot.press("d")
            self.assertEqual(len(self.app.screen_stack), 1)
            self.assertIn("cannot be deleted", self.status())
            self.app.select_key(group_key(self.review))
            await pilot.press("d")
            self.assertEqual(len(self.app.screen_stack), 1)
            self.assertIn("Stop 1 running agent inside first", self.status())
            self.app.select_key(group_key(self.lab))
            await pilot.press("d")
            self.assertIn("2 agents", str(self.app.screen.query_one("#dialog-description", Static).render()))
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            self.assertFalse(self.lab.exists())
            self.assertNotIn(group_key(self.lab), self.app.visible_rows)
            self.assertEqual({e["name"] for e in trash.contents(self.root)["entries"]}, {"idle", "lab"})

    async def test_the_universe_being_viewed_cannot_be_deleted_from_inside(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            self.app.enter_scope(self.lab)
            await self.settle(pilot)
            self.app.select_key(group_key(self.lab))
            await pilot.press("d")
            self.assertEqual(len(self.app.screen_stack), 1)
            self.assertIn("Go back to the parent universe", self.status())
            self.assertTrue(self.lab.is_dir())


if __name__ == "__main__":
    unittest.main()
