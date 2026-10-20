"""Plugin columns: one guarded call per column per refresh, a slow one parked across refreshes, and no work, no cells
and no old values where the plugin is off."""
import importlib
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write
from multiverse.ui import columns, panels
from multiverse.ui.backend import LocalBackend
from multiverse.ui.views import view
from multiverse.universe import templates

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
STEADY = """
def register(mv):
    mv.dashboard.column("shout", "Shout", 10, lambda row: row["name"].upper(), group=lambda row: "group")
    mv.dashboard.column("broken", "Broken", 6, lambda row: 1 / 0)
"""
SLOW = """
import threading
GATE = threading.Event()
CALLS = []

def value(row):
    CALLS.append(1)
    GATE.wait(10)
    return 'late'

def register(mv):
    mv.dashboard.column('slow', 'Slow', 6, value)
"""
PANEL = """
def register(mv):
    mv.dashboard.panel("counter", "widgets:Counter")
"""
WIDGETS = """
from textual.widgets import Static

class Counter:
    def parse(self, widget, title):
        return {"type": widget["type"], "title": title, "count": int(widget.get("count", 0))}

    def compose(self, view, root, widget, index):
        return [Static(f"count {widget['count']}")]
"""


class ColumnTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("loud", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": PLUGIN})
        self.write_plugin("steady", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": STEADY})
        self.write_plugin("slow", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": SLOW})
        self.write_plugin("counters", {"module": "plugin", "extends": ["dashboard.panel"]},
                          {"plugin.py": PANEL, "widgets.py": WIDGETS})
        self.gates = []
        self.addCleanup(self.release)
        self.addCleanup(columns.reset_plugin_caches)

    def release(self):
        for gate in self.gates:
            gate.set()

    def universe(self, name, loud=True):
        """A universe of its own for one row, with loud on and its column in the view, and the plugins imported
        afresh."""
        loader.reset()
        columns.reset_plugin_caches()
        root = templates.create(self.work / name / ".multiverse", name, workdir=self.work / "project")
        if loud:
            self.enable("loud", root=root)
            write(root / "views" / "default.json",
                  {"title": "T", "accent": "#89b4fa", "columns": ["name", "loud:shout"]})
        return root

    def mod(self, root, name="loud"):
        module = importlib.import_module(f"{self.loaded(name, root).package}.plugin")
        self.gates.append(module.GATE)
        return module

    def cells(self, snapshot):
        return {a["name"]: a.get("plugin_cells", {}).get("loud:shout") for a in snapshot["agents"]}

    def answered(self, snapshot):
        return {name for name, value in self.cells(snapshot).items() if value not in ("…", None)}

    def until(self, check, what, timeout=6.0):
        deadline = time.monotonic() + timeout
        while not check():
            self.assertLess(time.monotonic(), deadline, f"timed out waiting for {what}")
            time.sleep(0.03)

    def test_a_slow_column_is_parked_and_fills_in(self):
        """One pass per plugin column is parked across refreshes, answered rows show and the rest show …, no second
        pass or call starts while it runs, and a failing row costs one error; fragile because of threading across
        refreshes, and a break shows only with a slow column such as GOAT's, as stuck cells or a thread pile-up."""
        rows = (("one call serves every row", self.one_call_serves_every_row),
                ("a pass that runs long fills in", self.a_long_pass_fills_in),
                ("slow on every row, over several refreshes", self.slow_on_every_row),
                ("a column past its budget is not called again", self.cut_off_and_not_called_again))
        for index, (name, row) in enumerate(rows):
            with self.subTest(name):
                row(self.universe(f"slow{index}"))

    def one_call_serves_every_row(self, root):
        for name in ("a", "b", "bad", "c"):
            self.agent(name, root=root)
        errors_before = sum(len(texts) for texts in loader.errors().values())
        snapshot = LocalBackend().snapshot(root)
        self.assertEqual(self.cells(snapshot), {"a": "A", "b": "B", "bad": None, "c": "C"})
        self.assertEqual(sum(len(texts) for texts in loader.errors().values()) - errors_before, 1)
        calls = [call for call in self.mod(root).CALLS if call[0] == "agent"]
        self.assertEqual(sorted(call[1] for call in calls), ["a", "b", "bad", "c"])

    def a_long_pass_fills_in(self, root):
        names = ["a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8"]
        for name in names:
            self.agent(name, root=root)
        module = self.mod(root)
        module.BLOCK_AT = 4  # the fourth row asked, whichever it is, takes its time
        agent_calls = lambda: [c[1] for c in module.CALLS if c[0] == "agent"]
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 1.0):
            first = self.cells(LocalBackend().snapshot(root))
            answered = {n for n, v in first.items() if v not in ("…", None)}
            self.assertEqual(len(answered), 3, first)  # the three before the slow one are in
            self.assertEqual(sorted(v for v in first.values() if v == "…"), ["…"] * 5)
            seen = len(agent_calls())
            again = self.cells(LocalBackend().snapshot(root))
            self.assertEqual(len(agent_calls()), seen)  # the pass is still parked: no second pass
            self.assertEqual({n for n, v in again.items() if v not in ("…", None)}, answered)
        module.GATE.set()
        self.until(lambda: len(agent_calls()) >= 8, "the parked pass to finish")
        self.until(lambda: self.answered(LocalBackend().snapshot(root)) == set(names), "every row to have its value")
        self.assertEqual(self.cells(LocalBackend().snapshot(root)), {n: n.upper() for n in names})

    def slow_on_every_row(self, root):
        names = [f"s{index:02d}" for index in range(12)]
        for name in names:
            self.agent(name, root=root)
        module = self.mod(root)
        module.PERMITS = threading.Semaphore(0)
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.5):
            LocalBackend().snapshot(root)  # the pass starts and parks on its first row
            counts = []
            for permits in (4, 4, 40):
                for _ in range(permits if permits < 40 else 12):
                    module.PERMITS.release()
                self.until(lambda: len(self.answered(LocalBackend().snapshot(root))) >= min(sum(counts) + permits, 12),
                           f"{sum(counts) + permits} rows")
                counts.append(permits)
                shown = self.answered(LocalBackend().snapshot(root))
                self.assertGreaterEqual(len(shown), min(sum(counts), 12))
        self.until(lambda: self.answered(LocalBackend().snapshot(root)) == set(names), "every row")

    def cut_off_and_not_called_again(self, root):
        self.enable("slow", root=root)
        column = columns.resolve_plugin_columns([root], ["slow:slow"])["slow:slow"]
        module = column.loaded.module
        self.gates.append(module.GATE)
        columns.PLUGIN_COLUMNS = {"slow:slow": column}
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.2):
            row = {"name": "w", "universe": str(root), "plugins": ["slow"]}
            columns.fill_cells([row])
            columns.fill_cells([row])
        self.assertEqual(row["plugin_cells"], {"slow:slow": "…"})
        self.assertEqual(len(module.CALLS), 1)
        self.assertEqual(len(loader.errors()["slow"]), 1)
        self.assertIn("TimeoutError", loader.errors()["slow"][0])
        module.GATE.set()

    def test_a_column_turned_off_and_on_starts_empty_with_no_calls_where_off(self):
        """While a plugin is off its column gets no calls or cells, subverse rows included, and turned back on it starts
        at … (also while its old pass still runs), never old values, with cached columns and panels dropped; a break
        goes unnoticed because stale values look plausible and calls made where the plugin is off are hidden."""
        rows = (("turned off and on", self.turned_off_and_on, True),
                ("turned back on while its old pass runs", self.back_on_while_the_old_pass_runs, True),
                ("a subverse where it is off", self.a_subverse_where_it_is_off, True),
                ("off everywhere", self.off_everywhere, True),
                ("values follow the row's universe", self.values_follow_the_rows_universe, True),
                ("caches reset after plugin changes", self.caches_reset_after_plugin_changes, False))
        for index, (name, row, loud) in enumerate(rows):
            with self.subTest(name):
                row(self.universe(f"off{index}", loud=loud))

    def turned_off_and_on(self, root):
        names = ["a1", "a2", "a3"]
        for name in names:
            self.agent(name, root=root)
        self.assertEqual(self.cells(LocalBackend().snapshot(root)), {n: n.upper() for n in names})
        self.enable("loud", on=False, root=root)
        self.assertEqual(self.cells(LocalBackend().snapshot(root)), {n: None for n in names})
        self.enable("loud", root=root)
        module = self.mod(root)
        module.PERMITS = threading.Semaphore(0)  # the column is slow to answer this time
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.3):
            again = self.cells(LocalBackend().snapshot(root))
        self.assertEqual(again, {n: "…" for n in names}, "stale values from before it was turned off")
        for _ in names:
            module.PERMITS.release()
        self.until(lambda: self.answered(LocalBackend().snapshot(root)) == set(names), "the new values")

    def back_on_while_the_old_pass_runs(self, root):
        names = ["a1", "a2", "a3"]
        for name in names:
            self.agent(name, root=root)
        module = self.mod(root)
        module.PERMITS = threading.Semaphore(0)  # the pass parks on its first row and stays parked
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.3):
            self.assertEqual(self.cells(LocalBackend().snapshot(root)), {n: "…" for n in names})
            self.enable("loud", on=False, root=root)
            LocalBackend().snapshot(root)
            self.enable("loud", root=root)
            again = self.cells(LocalBackend().snapshot(root))  # the old pass still holds the plugin's call
        self.assertEqual(again, {n: "…" for n in names}, "a column waiting on its old pass showed dashes")
        for _ in range(2 * len(names)):
            module.PERMITS.release()
        self.until(lambda: self.answered(LocalBackend().snapshot(root)) == set(names), "the new values")

    def a_subverse_where_it_is_off(self, root):
        sub = templates.create(root / "subverses" / "ops", "ops", workdir=self.work / "project")
        write(sub / "views" / "default.json", {"title": "Ops", "accent": "#89b4fa", "columns": ["name"]})
        self.agent("a", root=root)
        snapshot = LocalBackend().snapshot(root)
        groups = {Path(g["path"]).name: g.get("plugin_cells", {}).get("loud:shout") for g in snapshot["groups"]}
        self.assertEqual(groups[root.name], "G:" + root.name)
        self.assertIsNone(groups["ops"])
        self.assertEqual([call for call in self.mod(root).CALLS if call[0] == "group" and call[1] == "ops"], [])

    def off_everywhere(self, root):
        self.agent("a", root=root)
        module = self.mod(root)
        self.enable("loud", on=False, root=root)
        LocalBackend().snapshot(root)
        self.assertEqual(module.CALLS, [])

    def values_follow_the_rows_universe(self, root):
        child = templates.create(root / "subverses" / "child", "child", workdir=self.work / "project")
        self.enable("steady", root=child)
        found = columns.resolve_plugin_columns([root, child], ["steady:shout", "steady:broken", "gone:x"])
        shout, broken = found["steady:shout"], found["steady:broken"]
        self.assertEqual((shout.title, shout.width, shout.plugin), ("Shout", 10, "steady"))
        self.assertIsInstance(found["gone:x"], columns.MissingColumn)
        columns.PLUGIN_COLUMNS = found
        row = {"kind": "agent", "name": "worker", "universe": str(child), "plugins": ["steady"]}
        off = {"kind": "agent", "name": "idle", "universe": str(root), "plugins": []}
        columns.fill_cells([row, off])
        self.assertEqual(row["plugin_cells"], {"steady:shout": "WORKER"})  # broken has no value: shown as a dash
        self.assertNotIn("plugin_cells", off)
        subverse = {"name": "child", "path": str(child), "plugins": ["steady"]}
        columns.fill_cells([subverse], group=True)
        self.assertEqual(subverse["plugin_cells"]["steady:shout"], "group")
        columns.fill_cells([row])
        # Rendering only reads the cells a row carries.
        self.assertEqual(shout.render({**row, "plugin_cells": {"steady:shout": "WORKER"}}).plain, "WORKER")
        self.assertEqual(shout.render({key: value for key, value in row.items() if key != "plugin_cells"}).plain, "—")
        group = {"kind": "group", "accent": "#ffffff", "plugin_cells": {"steady:shout": "g"}}
        self.assertEqual(shout.render(group).plain, "g")
        log = (child / "state" / "plugins" / "steady" / "errors.log").read_text()
        self.assertEqual(log.count("column broken: ZeroDivisionError: division by zero"), 1)

    def caches_reset_after_plugin_changes(self, root):
        write(root / "views" / "default.json", {"title": "", "accent": "#89b4fa",
                                                "widgets": [{"type": "counters:counter", "title": "Q"}]})
        self.assertIn("off here", view(root)["widgets"][0]["text"])
        panels._plugin_panels[("counters:counter", str(root), None)] = ValueError("cached")
        self.assertIn("cached", view(root)["widgets"][0]["text"])  # kept while plugins.json is unchanged
        self.enable("counters", root=root)
        self.assertEqual(view(root)["widgets"][0]["count"], 0)  # plugins.json changed
        panels._plugin_panels[next(iter(panels._plugin_panels))] = ValueError("cached")
        columns.PLUGIN_COLUMNS = {"x:y": columns.MissingColumn("x:y", "y", 8)}
        columns.reset_plugin_caches()
        self.assertEqual(columns.PLUGIN_COLUMNS, {})
        self.assertEqual(view(root)["widgets"][0]["count"], 0)


if __name__ == "__main__":
    unittest.main()
