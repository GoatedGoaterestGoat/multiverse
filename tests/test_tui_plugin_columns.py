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

def shout(row):
    CALLS.append(("agent", row["name"]))
    if row["name"] == "bad":
        raise RuntimeError("no value for " + row["name"])
    if row["name"].startswith("slow"):
        GATE.wait(30)
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

    def test_a_slow_column_costs_one_budget_per_refresh_not_one_per_row_and_is_skipped_for_a_few_refreshes(self):
        for name in ("slow1", "slow2", "slow3", "slow4", "slow5"):
            self.agent(name)
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.3):
            started = time.monotonic()
            first = LocalBackend().snapshot(self.root)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)  # not 5 x 0.3
            self.assertEqual(set(self.cells(first).values()), {"…"})
            calls = len([c for c in self.mod().CALLS if c[0] == "agent"])
            for _ in range(columns.SKIP_REFRESHES):
                started = time.monotonic()
                again = LocalBackend().snapshot(self.root)
                self.assertLess(time.monotonic() - started, 0.25)
                self.assertEqual(set(self.cells(again).values()), {"…"})
            self.assertEqual(len([c for c in self.mod().CALLS if c[0] == "agent"]), calls)  # no call while skipped
        self.mod().GATE.set()
        time.sleep(0.3)
        self.mod().CALLS.clear()
        later = LocalBackend().snapshot(self.root)
        self.assertEqual(set(self.cells(later).values()), {"SLOW1", "SLOW2", "SLOW3", "SLOW4", "SLOW5"})

    def test_a_subverse_row_where_the_plugin_is_off_gets_no_call_and_no_cell(self):
        self.agent("a")
        snapshot = LocalBackend().snapshot(self.root)
        groups = {Path(g["path"]).name: g.get("plugin_cells", {}).get("loud:shout") for g in snapshot["groups"]}
        self.assertEqual(groups[self.root.name], "G:" + self.root.name)
        self.assertIsNone(groups["ops"])
        self.assertEqual([call for call in self.mod().CALLS if call[0] == "group" and call[1] == "ops"], [])

    def test_a_universe_with_the_plugin_off_everywhere_makes_no_call(self):
        self.agent("a")
        module = self.mod()
        self.enable("loud", on=False)
        LocalBackend().snapshot(self.root)
        self.assertEqual(module.CALLS, [])


if __name__ == "__main__":
    unittest.main()
