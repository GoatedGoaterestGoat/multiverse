"""Plugin columns: one guarded call per column per refresh, a slow one skipped for a few refreshes, and no work off its scope."""
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write
from multiverse.ui import columns
from multiverse.ui.backend import LocalBackend

PLUGIN = '''
import threading, time
from pathlib import Path

CALLS = []
GATE = threading.Event()
DELAY = 0

BLOCK_AT = 0  # the Nth call (in whatever order the rows come) waits on GATE
PERMITS = None  # a semaphore: each answer needs a permit

def shout(row):
    CALLS.append(("agent", row["name"]))
    if row["name"] == "bad":
        raise RuntimeError("no value for " + row["name"])
    if BLOCK_AT and len([c for c in CALLS if c[0] == "agent"]) == BLOCK_AT:
        GATE.wait(30)
    if PERMITS is not None:
        PERMITS.acquire(timeout=30)
    return row["name"].upper()

def group(row):
    CALLS.append(("group", Path(row["path"]).name))
    return "G:" + Path(row["path"]).name

def register(mv):
    mv.dashboard.column("shout", "Shout", 10, shout, compact=True, group=group)
'''


class ColumnTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("loud", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": PLUGIN})
        self.enable("loud")
        self.sub = self.subverse("ops")  # the plugin is off there
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name", "loud:shout"]})
        write(self.sub / "views" / "default.json", {"title": "Ops", "accent": "#89b4fa", "columns": ["name"]})
        self.addCleanup(self.release)

    def mod(self):
        import importlib
        if not hasattr(self, "_module"):
            self._module = importlib.import_module(f"{self.loaded('loud').package}.plugin")
        return self._module

    def release(self):
        self.mod().GATE.set()

    def cells(self, snapshot):
        return {a["name"]: a.get("plugin_cells", {}).get("loud:shout") for a in snapshot["agents"]}

    def test_one_call_serves_every_row_and_a_failing_row_costs_one_error_not_one_per_row(self):
        for name in ("a", "b", "bad", "c"):
            self.agent(name)
        errors_before = sum(len(texts) for texts in loader.errors().values())
        snapshot = LocalBackend().snapshot(self.root)
        self.assertEqual(self.cells(snapshot), {"a": "A", "b": "B", "bad": None, "c": "C"})
        self.assertEqual(sum(len(texts) for texts in loader.errors().values()) - errors_before, 1)
        calls = [call for call in self.mod().CALLS if call[0] == "agent"]
        self.assertEqual(sorted(call[1] for call in calls), ["a", "b", "bad", "c"])

    def answered(self, snapshot):
        return {name for name, value in self.cells(snapshot).items() if value not in ("…", None)}

    def until(self, check, what, timeout=6.0):
        deadline = time.monotonic() + timeout
        while not check():
            self.assertLess(time.monotonic(), deadline, f"timed out waiting for {what}")
            time.sleep(0.03)

    def test_a_column_that_runs_long_shows_the_rows_it_answered_and_fills_in_as_its_pass_goes_on(self):
        names = ["a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8"]
        for name in names:
            self.agent(name)
        module = self.mod()
        module.BLOCK_AT = 4  # the fourth row asked, whichever it is, takes its time
        agent_calls = lambda: [c[1] for c in module.CALLS if c[0] == "agent"]
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 1.0):
            first = self.cells(LocalBackend().snapshot(self.root))
            answered = {n for n, v in first.items() if v not in ("…", None)}
            self.assertEqual(len(answered), 3, first)  # the three before the slow one are in
            self.assertEqual(sorted(v for v in first.values() if v == "…"), ["…"] * 5)
            seen = len(agent_calls())
            again = self.cells(LocalBackend().snapshot(self.root))
            self.assertEqual(len(agent_calls()), seen)  # the pass is still parked: no second pass
            self.assertEqual({n for n, v in again.items() if v not in ("…", None)}, answered)
        module.GATE.set()
        self.until(lambda: len(agent_calls()) >= 8, "the parked pass to finish")
        self.until(lambda: self.answered(LocalBackend().snapshot(self.root)) == set(names), "every row to have its value")
        self.assertEqual(self.cells(LocalBackend().snapshot(self.root)), {n: n.upper() for n in names})

    def test_a_column_slow_on_every_row_still_shows_values_that_arrive_over_several_refreshes(self):
        import threading
        names = [f"s{index:02d}" for index in range(12)]
        for name in names:
            self.agent(name)
        module = self.mod()
        module.PERMITS = threading.Semaphore(0)
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.5):
            LocalBackend().snapshot(self.root)  # the pass starts and parks on its first row
            counts = []
            for permits in (4, 4, 40):
                for _ in range(permits if permits < 40 else 12):
                    module.PERMITS.release()
                self.until(lambda: len(self.answered(LocalBackend().snapshot(self.root))) >= min(sum(counts) + permits, 12),
                           f"{sum(counts) + permits} rows")
                counts.append(permits)
                shown = self.answered(LocalBackend().snapshot(self.root))
                self.assertGreaterEqual(len(shown), min(sum(counts), 12))
        self.until(lambda: self.answered(LocalBackend().snapshot(self.root)) == set(names), "every row")

    def test_a_subverse_row_where_the_plugin_is_off_gets_no_call_and_no_cell(self):
        self.agent("a")
        snapshot = LocalBackend().snapshot(self.root)
        groups = {Path(g["path"]).name: g.get("plugin_cells", {}).get("loud:shout") for g in snapshot["groups"]}
        self.assertEqual(groups[self.root.name], "G:" + self.root.name)
        self.assertIsNone(groups["ops"])
        self.assertEqual([call for call in self.mod().CALLS if call[0] == "group" and call[1] == "ops"], [])

    def test_turning_a_plugin_off_and_on_starts_its_column_empty_not_with_the_old_values(self):
        import threading
        names = ["a1", "a2", "a3"]
        for name in names:
            self.agent(name)
        self.assertEqual(self.cells(LocalBackend().snapshot(self.root)), {n: n.upper() for n in names})
        self.enable("loud", on=False)
        self.assertEqual(self.cells(LocalBackend().snapshot(self.root)), {n: None for n in names})
        self.enable("loud")
        module = self.mod()
        module.PERMITS = threading.Semaphore(0)  # the column is slow to answer this time
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.3):
            again = self.cells(LocalBackend().snapshot(self.root))
        self.assertEqual(again, {n: "…" for n in names}, "stale values from before it was turned off")
        for _ in names:
            module.PERMITS.release()
        self.until(lambda: self.answered(LocalBackend().snapshot(self.root)) == set(names), "the new values")

    def test_a_universe_with_the_plugin_off_everywhere_makes_no_call(self):
        self.agent("a")
        module = self.mod()
        self.enable("loud", on=False)
        LocalBackend().snapshot(self.root)
        self.assertEqual(module.CALLS, [])


if __name__ == "__main__":
    unittest.main()
