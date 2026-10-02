"""The compaction screens: pure edit logic, then the dashboard flow against a fake backend."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import DataTable, Input, Static
from multiverse.compaction_screens import AgentScreen, Choice, CompactionScreen, Field, apply_changes, sections
from multiverse.dashboard import group_key
from multiverse.tui import KEYS, MultiverseApp
from tui_demo import DemoBackend

FILE = {"schema": 1, "enabled": False,
        "default": {"scheduler": "last_k_raw", "k": 2, "budget": 0, "image_mode": "budget_fit"},
        "harnesses": {"claude": {"builder": {"model": "sonnet", "effort": "medium", "claude_max_retries": 3,
                                             "rate_limit": {"retries": 10, "wait_seconds": {"first": 20, "multiplier": 2, "max": 300}}}}}}
SCHEDULERS = {'last_k_raw': ['k'], 'proportional': [], 'uniform': [], 'recency_boosted': ['recency_ratio', 'recency_half_life']}


def overview(file, problem=None, agents=()):
    return {"universe": "/demo", "path": "/demo/compaction.json", "exists": True, "problem": problem, "file": copy.deepcopy(file),
            "enabled": bool(file.get("enabled")), "default": {"scheduler": "last_k_raw", "budget": 0, "k": 2},
            "harnesses": [{"name": "claude", "supported": True, "enabled": False,
                           "default": {"scheduler": "last_k_raw", "budget": 0, "k": 2}, "builder": {},
                           "defaults": {"model": "sonnet", "fallback_model": "claude-sonnet-4-6", "effort": "medium",
                                        "claude_max_retries": 3},
                           "options": ["claude_max_retries"], "problem": None}],
            "agents": list(agents), "schedulers": SCHEDULERS, "image_modes": ["budget_fit", "keep_all", "drop_all"]}


class FakeBackend(DemoBackend):
    demo = False

    def __init__(self):
        super().__init__()
        self.file = copy.deepcopy(FILE)
        self.problem = None
        self.saved, self.previews, self.schedulers = [], [], []
        self.error = None
        self.agent = {"id": "a1", "name": "reviewer", "harness": "claude", "live": "alive",
                      "scheduler": {"scheduler": "last_k_raw", "budget": 0, "k": 2}, "scheduler_from": "universe",
                      "trees_cached": 3, "activity": "building", "failed_turns": 1, "last_error": "refused"}

    def compaction_overview(self, root):
        return overview(self.file, self.problem, [self.agent])

    def save_compaction(self, root, file):
        if self.error:
            raise ValueError(self.error)
        self.saved.append(copy.deepcopy(file))
        self.file = copy.deepcopy(file)
        return overview(self.file)

    def compaction_agent(self, root, agent_id, **kwargs):
        self.previews.append(kwargs)
        if "scheduler" in kwargs:
            scheduler = kwargs["scheduler"]
            own, source = scheduler or {"scheduler": "last_k_raw", "budget": 0, "k": 2}, "agent" if scheduler else "universe"
        else:
            scheduler, own, source = self.agent["scheduler"], self.agent["scheduler"], self.agent["scheduler_from"]
        return {**self.agent, "enabled": True, "unavailable": None, "scheduler": own,
                "scheduler_from": source, "default": {"scheduler": "last_k_raw", "budget": 0, "k": 2},
                "recent_errors": ["refused"],
                "plan": [{"turn": "5d1c3a4e-1111-4222-8333-444455556666", "prompt": "Fix the bug", "leaves": 12, "tokens": 82000, "omitted": 0, "share": 40000,
                          "raw": False, "needs_tree": True, "has_tree": False, "prompt_images": 0},
                         {"turn": "9e2b7c1f-aaaa-4bbb-8ccc-ddddeeeeffff", "prompt": "Now ship it", "leaves": 3, "tokens": 900, "omitted": 0, "share": 900,
                          "raw": True, "needs_tree": False, "has_tree": False, "prompt_images": 0}],
                "summary": {"turns": 2, "summarized": 1, "trees_missing": 1, "dropped_turns": 0, "tokens_before": 82900,
                            "tokens_after": 20000 + (scheduler or {}).get("budget", 0), "after_partial": True}}

    def set_compaction_scheduler(self, root, agent_id, scheduler):
        if self.error:
            raise ValueError(self.error)
        self.schedulers.append(scheduler)
        return self.agent


class EditLogicTests(unittest.TestCase):
    def edit(self, file=FILE, **changes):
        fields = [item for _, items in sections(overview(file)) for item in items]
        values = {item.key: item.initial for item in fields}
        for key, value in changes.items():
            self.assertIn(key.replace("__", "."), values)
            values[key.replace("__", ".")] = value
        return apply_changes(file, fields, values)

    def test_untouched_form_writes_the_file_exactly_as_it_was(self):
        self.assertEqual(self.edit(), FILE)
        self.assertEqual(self.edit({}), {})
        self.assertEqual(self.edit({"enabled": True}), {"enabled": True})

    def test_only_edited_fields_change_and_clearing_deletes_the_key(self):
        new = self.edit(shared__k="5", claude__builder__effort="")
        self.assertEqual(new["default"]["k"], 5)
        self.assertNotIn("effort", new["harnesses"]["claude"]["builder"])
        self.assertEqual(new["harnesses"]["claude"]["builder"]["model"], "sonnet")
        expected = copy.deepcopy(FILE)
        expected["default"]["k"] = 5
        del expected["harnesses"]["claude"]["builder"]["effort"]
        self.assertEqual(new, expected)

    def test_new_keys_are_created_only_for_edited_fields_and_empty_objects_are_pruned(self):
        new = self.edit({}, claude__builder__claude_max_retries="7")
        self.assertEqual(new, {"harnesses": {"claude": {"builder": {"claude_max_retries": 7}}}})
        self.assertEqual(self.edit(new, claude__builder__claude_max_retries=""), {})
        wait = self.edit({}, claude__enabled=True, **{"claude.builder.rate_limit.wait_seconds.max": "90.5"})
        self.assertEqual(wait, {"harnesses": {"claude": {"enabled": True, "builder": {"rate_limit": {"wait_seconds": {"max": 90.5}}}}}})

    def test_changing_the_scheduler_drops_parameters_it_does_not_take(self):
        new = self.edit(shared__scheduler="proportional")
        self.assertEqual(new["default"], {"scheduler": "proportional", "budget": 0, "image_mode": "budget_fit"})
        both = self.edit(shared__scheduler="recency_boosted", shared__recency_ratio="4")
        self.assertEqual(both["default"], {"scheduler": "recency_boosted", "budget": 0, "image_mode": "budget_fit", "recency_ratio": 4})

    def test_a_harness_scheduler_of_its_own_or_following_the_universe(self):
        own = self.edit(claude__default__mode="own", claude__default__budget="500")
        self.assertEqual(own["harnesses"]["claude"]["default"], {"budget": 500})
        file = {"harnesses": {"claude": {"default": {"scheduler": "uniform", "budget": 9}, "enabled": False}}}
        back = self.edit(file, claude__default__mode=None, claude__default__budget="1")
        self.assertEqual(back, {"harnesses": {"claude": {"enabled": False}}})
        untouched_own = self.edit(file, claude__default__budget="10")
        self.assertEqual(untouched_own["harnesses"]["claude"]["default"], {"scheduler": "uniform", "budget": 10})

    def test_bad_numbers_are_reported_by_label(self):
        for key, text in (("shared__k", "two"), ("shared__k", "2.5"), ("shared__budget", "-"), ("shared__recency_ratio", "nan")):
            file = {"default": {"scheduler": "recency_boosted"}} if "ratio" in key else FILE
            with self.assertRaises(ValueError, msg=(key, text)):
                self.edit(file, **{key: text})

    def test_null_fallback_and_boolean_choices(self):
        new = self.edit(claude__builder__fallback_model="null", claude__builder__background=False, shared__enabled=True)
        self.assertIsNone(new["harnesses"]["claude"]["builder"]["fallback_model"])
        self.assertIs(new["harnesses"]["claude"]["builder"]["background"], False)
        self.assertIs(new["enabled"], True)


class CompactionScreenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)

    async def settle(self, pilot):
        for _ in range(3):
            await pilot.pause(0.05)
            await self.app.workers.wait_for_complete()
        await pilot.pause(0.05)

    async def open(self, pilot):
        await self.settle(pilot)
        await pilot.press("c")
        await self.settle(pilot)
        self.assertIsInstance(self.app.screen, CompactionScreen)
        return self.app.screen

    def widget(self, screen, key):
        return screen.widgets[key]

    async def test_help_lists_the_key_and_the_footer_stays_unchanged(self):
        self.assertIn("Compaction settings", KEYS)
        self.assertNotIn("c", [b.key for b in self.app.BINDINGS if b.show])

    async def test_sections_fill_from_the_file_and_defaults_are_placeholders(self):
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open(pilot)
            self.assertEqual(self.widget(screen, "shared.k").value, "2")
            self.assertEqual(self.widget(screen, "claude.builder.model").value, "sonnet")
            self.assertEqual(self.widget(screen, "claude.builder.fallback_model").value, "")
            self.assertEqual(self.widget(screen, "claude.builder.fallback_model").placeholder, "claude-sonnet-4-6")
            self.assertEqual(self.widget(screen, "claude.builder.concurrency").placeholder, "1")
            self.assertEqual(self.widget(screen, "claude.default.mode").value, None)
            # k is hidden for schedulers that do not take it; a harness following the universe hides its own fields.
            self.assertTrue(screen.rows["shared.k"].display)
            self.assertFalse(screen.rows["shared.recency_ratio"].display)
            self.assertFalse(screen.rows["claude.default.budget"].display)
            self.widget(screen, "shared.scheduler").action_step(1)
            await pilot.pause()
            self.assertEqual(self.widget(screen, "shared.scheduler").value, "proportional")
            self.assertFalse(screen.rows["shared.k"].display)

    async def test_save_sends_the_file_with_only_the_edits_and_closes(self):
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open(pilot)
            self.widget(screen, "shared.k").value = "4"
            self.widget(screen, "claude.builder.effort").value = ""
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            expected = copy.deepcopy(FILE)
            expected["default"]["k"] = 4
            del expected["harnesses"]["claude"]["builder"]["effort"]
            self.assertEqual(self.backend.saved, [expected])
            self.assertNotIsInstance(self.app.screen, CompactionScreen)
            self.assertIn("saved", str(self.app.query_one("#status", Static).render()))

    async def test_escape_discards_and_a_rejected_save_stays_open_with_the_error(self):
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open(pilot)
            self.widget(screen, "shared.k").value = "9"
            self.backend.error = "effort must be one of low, medium"
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            self.assertIs(self.app.screen, screen)
            self.assertIn("effort must be one of", str(screen.query_one(".cerror", Static).render()))
            self.assertEqual(self.backend.saved, [])
            self.widget(screen, "shared.k").value = "x"
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            self.assertIn("k (last turns raw) must be a whole number", str(screen.query_one(".cerror", Static).render()))
            await pilot.press("escape")
            await self.settle(pilot)
            self.assertNotIsInstance(self.app.screen, CompactionScreen)
            self.assertEqual(self.backend.saved, [])

    async def test_an_unusable_file_is_shown_at_the_top(self):
        self.backend.problem = "Unknown compaction.json fields: ['bogus']"
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open(pilot)
            banner = screen.query_one("#compaction-problem", Static)
            self.assertTrue(banner.display)
            self.assertIn("bogus", str(banner.render()))

    async def test_targets_the_selected_scope(self):
        calls = []
        original = self.backend.compaction_overview
        self.backend.compaction_overview = lambda root: (calls.append(root), original(root))[1]
        async with self.app.run_test(size=(100, 40)) as pilot:
            await self.settle(pilot)
            review = self.backend.root / "subverses/pr-review"
            self.app.select_key(group_key(review))
            await pilot.press("c")
            await self.settle(pilot)
            self.assertEqual(calls, [review])

    async def test_compact_terminals_keep_every_field_reachable(self):
        async with self.app.run_test(size=(60, 20)) as pilot:
            screen = await self.open(pilot)
            body = screen.query_one("#compaction-body")
            self.assertGreaterEqual(body.region.height, 6)
            self.assertLessEqual(screen.query_one("#compaction-dialog").region.bottom, 20)
            last = self.widget(screen, "claude.builder.rate_limit.wait_seconds.max")
            last.focus()
            await pilot.pause(0.1)
            self.assertTrue(last.region.height > 0 and last.region.bottom <= 20)
            table = screen.query_one("#compaction-agents", DataTable)
            self.assertEqual(table.row_count, 1)

    async def test_agent_screen_previews_while_editing_and_saves_its_own_scheduler(self):
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open(pilot)
            screen.query_one("#compaction-agents", DataTable).focus()
            await pilot.press("enter")
            await self.settle(pilot)
            agent = self.app.screen
            self.assertIsInstance(agent, AgentScreen)
            self.assertIn("3 trees · building · 1 failed turns", str(agent.query_one("#agent-status", Static).render()))
            self.assertIn("refused", str(agent.query_one("#agent-status", Static).render()))
            plan = agent.query_one("#plan", DataTable)
            self.assertEqual(plan.row_count, 2)
            self.assertEqual([plan.get_row_at(i)[0] for i in range(2)], ["1", "2"])
            self.assertEqual(plan.get_row_at(0)[4], "40k")
            summary = str(agent.query_one("#plan-summary", Static).render())
            self.assertIn("2 turns · 1 summarized · 1 trees missing", summary)
            self.assertIn("partial", summary)
            self.assertTrue(agent.query_one("#agent-follows").display)
            self.assertFalse(agent.rows["agent.budget"].display)
            first = len(self.backend.previews)
            self.widget(agent, "agent.mode").action_step(1)
            self.widget(agent, "agent.budget").value = "5000"
            await pilot.pause(0.7)
            await self.settle(pilot)
            self.assertEqual(self.backend.previews[-1]["scheduler"], {"scheduler": "last_k_raw", "budget": 5000, "k": 2})
            self.assertEqual(self.backend.saved, [])
            self.assertIn("25k", str(agent.query_one("#plan-summary", Static).render()))
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            self.assertEqual(self.backend.schedulers, [{"scheduler": "last_k_raw", "budget": 5000, "k": 2}])
            self.assertIs(self.app.screen, screen)

    async def test_agent_returns_to_the_default_and_bad_input_keeps_the_last_plan(self):
        self.backend.agent = {**self.backend.agent, "scheduler": {"scheduler": "uniform", "budget": 100}, "scheduler_from": "agent"}
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open(pilot)
            screen.query_one("#compaction-agents", DataTable).focus()
            await pilot.press("enter")
            await self.settle(pilot)
            agent = self.app.screen
            self.assertEqual(self.widget(agent, "agent.mode").value, "own")
            self.widget(agent, "agent.budget").value = "lots"
            await pilot.pause(0.7)
            self.assertIn("must be a whole number", str(agent.query_one(".cerror", Static).render()))
            self.assertEqual(agent.query_one("#plan", DataTable).row_count, 2)
            self.widget(agent, "agent.mode").action_step(1)
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            self.assertEqual(self.backend.schedulers, [None])

    async def test_agent_save_error_stays_on_the_screen(self):
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open(pilot)
            screen.query_one("#compaction-agents", DataTable).focus()
            await pilot.press("enter")
            await self.settle(pilot)
            agent = self.app.screen
            self.widget(agent, "agent.mode").action_step(1)
            self.backend.error = "budget must be a whole number of at least 0"
            await pilot.press("ctrl+enter")
            await self.settle(pilot)
            self.assertIs(self.app.screen, agent)
            self.assertIn("budget must be", str(agent.query_one(".cerror", Static).render()))


if __name__ == "__main__":
    unittest.main()
