"""Plugin columns, view panels and the theme in the dashboard."""
import json
from pathlib import Path
import sys
import unittest
import unittest.mock
from unittest import IsolatedAsyncioTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write
from multiverse.ui import columns, panels, theme
from multiverse.ui.backend import LocalBackend
from multiverse.ui.views import DEFAULT_COLUMNS, view

COLUMN = """
def register(mv):
    mv.dashboard.column("shout", "Shout", 10, lambda row: row["name"].upper(), group=lambda row: "group")
    mv.dashboard.column("broken", "Broken", 6, lambda row: 1 / 0)
"""
TWIN = """
def register(mv):
    mv.dashboard.column("shout", "Again", 10, lambda row: "again")
"""
PANEL = """
from multiverse.plugins import api

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


class DashboardPluginTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, columns, "PLUGIN_COLUMNS", {})
        self.addCleanup(panels._plugin_panels.clear)
        self.write_plugin("loud", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": COLUMN})
        self.write_plugin("twin", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": TWIN})
        self.write_plugin("counters", {"module": "plugin", "extends": ["dashboard.panel"]},
                          {"plugin.py": PANEL, "widgets.py": WIDGETS})

    def set_view(self, root, **fields):
        write(root / "views" / "default.json", {"title": "", "accent": "#89b4fa", **fields})

    def test_views_keep_plugin_columns_only_as_plugins_offer_them(self):
        self.set_view(self.root, columns=["name", "shout", "loud:shout", "nonsense", "Bad:key", "phase"])
        self.assertEqual(view(self.root)["columns"], ["name", "loud:shout", "phase"])
        self.enable("loud")
        self.assertEqual(view(self.root)["columns"], ["name", "shout", "loud:shout", "phase"])
        self.set_view(self.root)
        self.assertEqual(view(self.root)["columns"], DEFAULT_COLUMNS)

    def test_column_values_follow_the_rows_universe(self):
        child = self.subverse("child")
        self.enable("loud", root=child)
        self.set_view(self.root, columns=["name", "loud:shout", "loud:broken"])
        found = columns.resolve_plugin_columns([self.root, child], ["loud:shout", "loud:broken", "gone:x"])
        shout, broken = found["loud:shout"], found["loud:broken"]
        self.assertEqual((shout.title, shout.width, shout.plugin), ("Shout", 10, "loud"))
        self.assertIsInstance(found["gone:x"], columns.MissingColumn)
        row = {"kind": "agent", "name": "worker", "universe": str(child), "plugins": ["loud"]}
        self.assertEqual(shout.cell(row), "WORKER")
        self.assertIsNone(shout.cell({**row, "plugins": []}))
        self.assertEqual(shout.cell({"name": "child", "path": str(child)}, group=True), "group")
        self.assertIsNone(broken.cell(row))
        self.assertIsNone(broken.cell(row))
        # Rendering only reads the cells a row carries.
        self.assertEqual(shout.render({**row, "plugin_cells": {"loud:shout": "WORKER"}}).plain, "WORKER")
        self.assertEqual(shout.render(row).plain, "—")
        self.assertEqual(shout.render({"kind": "group", "accent": "#ffffff", "plugin_cells": {"loud:shout": "g"}}).plain, "g")
        log = (child / "state" / "plugins" / "loud" / "errors.log").read_text()
        self.assertEqual(log.count("column broken: ZeroDivisionError: division by zero"), 1)

    def test_a_short_key_two_plugins_use_is_left_blank(self):
        self.enable("loud")
        self.enable("twin")
        self.set_view(self.root, columns=["name", "shout", "twin:shout"])
        self.assertEqual(view(self.root)["columns"], ["name", "shout", "twin:shout"])
        found = columns.resolve_plugin_columns([self.root], ["shout", "twin:shout"])
        self.assertIsInstance(found["shout"], columns.MissingColumn)
        self.assertEqual(found["twin:shout"].title, "Again")

    def test_snapshot_marks_which_plugins_each_agents_universe_has_on(self):
        self.enable("loud")
        self.set_view(self.root, columns=["name", "loud:shout"])
        agents = self.root / "state" / "agents"
        write(agents / "a" / "properties.json", {"id": "a", "name": "worker", "harness": "codex", "task": "",
                                                 "universe_id": json.loads((self.root / "universe.json").read_text())["id"],
                                                 "workspace": str(self.work)})
        snapshot = LocalBackend().snapshot(self.root)
        self.assertEqual(snapshot["agents"][0]["plugins"], ["loud"])
        self.assertEqual(snapshot["agents"][0]["plugin_cells"], {"loud:shout": "WORKER"})
        self.assertEqual(snapshot["groups"][0]["plugin_cells"], {"loud:shout": "group"})
        self.assertEqual(columns.lookup("loud:shout").title, "Shout")
        self.set_view(self.root, columns=["name", "phase"])
        self.assertNotIn("plugins", LocalBackend().snapshot(self.root)["agents"][0])
        self.assertEqual(columns.PLUGIN_COLUMNS, {})

    def test_plugin_panels_and_what_they_need(self):
        widgets = [{"type": "counter", "title": "Count", "count": 3}, {"type": "counters:counter", "title": "Q"},
                   {"type": "ghost:thing", "title": "G"}, {"type": "nothing", "title": "N"}]
        self.set_view(self.root, widgets=widgets)
        off = view(self.root)["widgets"]
        self.assertEqual(off[0]["text"], "unknown widget type: counter")
        self.assertEqual(off[1]["text"], "widget type counters:counter needs plugin counters, which is off here")
        self.assertEqual(off[2]["text"], "widget type ghost:thing needs plugin ghost, which is not installed")
        self.enable("counters")
        on = view(self.root)["widgets"]
        self.assertEqual(on[0], {"type": "counter", "title": "Count", "count": 3})
        self.assertEqual(on[1], {"type": "counters:counter", "title": "Q", "count": 0})
        self.assertEqual(on[3]["text"], "unknown widget type: nothing")

    def test_theme_apply_takes_only_palette_colors(self):
        before = {name: getattr(theme, name) for name in ("BLUE", "TEAL", "ACCENTS")}
        self.addCleanup(lambda: [setattr(theme, name, value) for name, value in before.items()])
        self.assertEqual(theme.apply({"BLUE": "#010203", "TEAL": "#ff0000", "NOPE": "#000000", "RED": "red"}),
                         ["NOPE", "RED"])
        self.assertEqual(theme.BLUE, "#010203")
        self.assertEqual(theme.css_variables()["mv-blue"], "#010203")
        self.assertEqual(theme.ACCENTS[:3], ("#ff0000", before["ACCENTS"][1], "#010203"))

    def test_a_slow_column_is_cut_off_and_not_called_again_while_it_runs(self):
        import threading
        from multiverse.plugins import loader
        release = threading.Event()
        self.addCleanup(release.set)
        self.write_plugin("slow", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": (
            "import threading\nGATE = threading.Event()\nCALLS = []\n"
            "def value(row):\n    CALLS.append(1)\n    GATE.wait(10)\n    return 'late'\n"
            "def register(mv): mv.dashboard.column('slow', 'Slow', 6, value)\n")})
        self.enable("slow")
        column = columns.resolve_plugin_columns([self.root], ["slow:slow"])["slow:slow"]
        module = column.loaded.module
        self.addCleanup(module.GATE.set)
        with unittest.mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.2):
            row = {"name": "w", "universe": str(self.root), "plugins": ["slow"]}
            self.assertIsNone(column.cell(row))
            self.assertIsNone(column.cell(row))
        self.assertEqual(len(module.CALLS), 1)
        self.assertEqual(loader.errors()["slow"], ["column slow: TimeoutError: took longer than 0.2 s"])
        module.GATE.set()

    def test_caches_reset_after_plugin_changes(self):
        self.set_view(self.root, widgets=[{"type": "counters:counter", "title": "Q"}])
        self.assertIn("off here", view(self.root)["widgets"][0]["text"])
        panels._plugin_panels[("counters:counter", str(self.root), None)] = ValueError("cached")
        self.assertIn("cached", view(self.root)["widgets"][0]["text"])  # kept while plugins.json is unchanged
        self.enable("counters")
        self.assertEqual(view(self.root)["widgets"][0]["count"], 0)  # plugins.json changed
        panels._plugin_panels[next(iter(panels._plugin_panels))] = ValueError("cached")
        columns.PLUGIN_COLUMNS = {"x:y": columns.MissingColumn("x:y", "y", 8)}
        columns.reset_plugin_caches()
        self.assertEqual(columns.PLUGIN_COLUMNS, {})
        self.assertEqual(view(self.root)["widgets"][0]["count"], 0)


class PluginColumnScreenTest(PluginTestCase, IsolatedAsyncioTestCase):
    async def test_the_table_shows_a_plugin_column(self):
        from multiverse.ui.app import MultiverseApp
        from textual.widgets import DataTable
        self.addCleanup(setattr, columns, "PLUGIN_COLUMNS", {})
        self.write_plugin("loud", {"module": "plugin", "extends": ["dashboard.column"]}, {"plugin.py": COLUMN})
        self.enable("loud")
        write(self.root / "views" / "default.json", {"title": "", "accent": "#89b4fa", "columns": ["name", "loud:shout"]})
        write(self.root / "state" / "agents" / "a" / "properties.json", {
            "id": "a", "name": "worker", "harness": "codex", "task": "",
            "universe_id": json.loads((self.root / "universe.json").read_text())["id"], "workspace": str(self.work)})
        app = MultiverseApp(self.root, refresh_interval=0)
        async with app.run_test(size=(140, 30)) as pilot:
            for _ in range(50):
                await pilot.pause(0.05)
                if app.scan_count:
                    break
            table = app.query_one("#agents", DataTable)
            titles = [str(column.label) for column in table.ordered_columns]
            self.assertEqual(titles[1], "Shout")
            self.assertIn("WORKER", [str(cell) for cell in table.get_row("agent:a")] if "agent:a" in table.rows else
                          [str(table.get_row_at(index)[1]) for index in range(table.row_count)])


DATA_PANEL = """
from multiverse.plugins import api

def register(mv):
    @mv.dashboard.panel("board", title="Board", auto=True)
    def board(context):
        names = ", ".join(sorted(agent.name for agent in context.agents)) or "nobody"
        return [api.Markdown(f"**{context.widget.get('heading', 'Agents')}**: {names}"),
                api.Table(("plugin",), tuple((name,) for name in sorted(context.plugins))),
                api.Artifacts((api.Artifact("Report", "artifacts/r.md"), {"label": "Log", "path": "log.txt"}))]
    mv.dashboard.panel("legacy", "widgets:Old")
"""


class DataPanelTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("boards", {"module": "plugin", "extends": ["dashboard.panel"], "permissions": ["agents.read"]},
                          {"plugin.py": DATA_PANEL})
        self.write_plugin("blind", {"module": "plugin", "extends": ["dashboard.panel"]}, {"plugin.py": DATA_PANEL})
        self.enable("boards")
        self.enable("blind")

    def test_data_panels_render_plain_blocks(self):
        from multiverse import plugins
        from multiverse.plugins import api
        found = {(loaded.name, kind): spec for loaded, kind, spec in plugins.dashboard_panels(self.root)}
        self.assertEqual(found[("boards", "legacy")], "widgets:Old")
        spec = found[("boards", "board")]
        self.assertEqual((spec.title, spec.auto), ("Board", True))
        rows = [{"id": "a1", "name": "worker", "universe": str(self.root), "live": "alive"}]
        boards = next(loaded for loaded, kind, _ in plugins.dashboard_panels(self.root) if loaded.name == "boards")
        context = api.panel_context(boards, self.root, {"heading": "Crew"}, rows, plugins.on_here(self.root))
        self.assertEqual([agent.name for agent in context.agents], ["worker"])
        widgets = api.blocks(spec.render(context), spec.title)
        self.assertEqual(widgets[0], {"type": "markdown", "title": "Board", "text": "**Crew**: worker"})
        self.assertEqual(widgets[1]["rows"], [["blind"], ["boards"]])
        self.assertEqual([item["label"] for item in widgets[2]["items"]], ["Report", "Log"])
        blind = next(loaded for loaded, _, _ in plugins.dashboard_panels(self.root) if loaded.name == "blind")
        self.assertEqual(api.panel_context(blind, self.root, {}, rows, set()).agents, ())  # no agents.read

    def test_unusable_results_are_refused(self):
        from multiverse.plugins import api
        for result, expected in ((api.Table(("a", "b"), (("1",),)), "same length"), ("text", "not str"),
                                 (api.Artifacts(("x",)), "Artifact")):
            with self.subTest(result=result):
                with self.assertRaisesRegex(api.Error, expected):
                    api.blocks(result, "T")

    def test_listing_says_what_a_plugin_provides(self):
        from multiverse import plugins
        from multiverse.plugins import loader
        item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "boards")
        self.assertIsNone(item["provides"]["panels"])  # not loaded in this process yet
        loader.load(self.entry("boards"))
        item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "boards")
        self.assertEqual(item["provides"]["panels"], ["board", "legacy"])
        self.assertEqual(item["provides"]["commands"], [])


SETTINGS_ENTRY = """
from multiverse.plugins import api

def register(mv):
    mv.dashboard.settings([api.Field("greeting", "Greeting", placeholder="hello"),
                           api.Field("mode", "Mode", "choice", options=("fast", "slow"))])

    @mv.dashboard.action("Say hello")
    def hello(context):
        return f"hello from {context.scope.name}"
"""


class SettingsEntryTest(PluginTestCase):
    def test_declared_fields_and_palette_actions(self):
        from multiverse import plugins
        from multiverse.plugins import loader
        self.write_plugin("greeter", {"module": "plugin", "extends": ["dashboard.settings", "dashboard.action"],
                                      "settings": {"version": 1}}, {"plugin.py": SETTINGS_ENTRY})
        self.enable("greeter")
        loader.load(self.entry("greeter"))
        item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "greeter")
        self.assertEqual([field["path"] for field in item["settings"]["fields"]], ["greeting", "mode"])
        self.assertIsNone(item["settings"]["screen"])
        self.assertEqual(item["provides"]["actions"], ["Say hello"])
        [(loaded, name, function)] = plugins.dashboard_actions(self.root)
        from types import SimpleNamespace
        self.assertEqual(function(SimpleNamespace(row=None, scope=self.root)), "hello from .multiverse")

    def test_a_screen_entry_and_what_is_refused(self):
        from multiverse.plugins import api, loader
        self.write_plugin("own", {"module": "plugin", "extends": ["dashboard.settings"]}, {
            "plugin.py": "def register(mv):\n    mv.dashboard.settings(screen='screens:Settings')\n"})
        self.enable("own")
        self.assertEqual(loader.load(self.entry("own")).registrations.settings_screen, "screens:Settings")
        with self.assertRaisesRegex(api.Error, "choice field needs its options"):
            api.Field("mode", "Mode", "choice")
        cases = (("mv.dashboard.settings()", "either fields or screen"),
                 ("mv.dashboard.settings([api.Field('a', 'A')])", 'declares no "settings"'),
                 ("mv.dashboard.action('x', lambda c: 1)", 'does not list it in "extends"'))
        for body, expected in cases:
            with self.subTest(body):
                loader.reset()
                self.write_plugin("bad", {"module": "plugin", "extends": ["dashboard.settings"]},
                                  {"plugin.py": f"from multiverse.plugins import api\ndef register(mv):\n    {body}\n"})
                self.enable("bad")
                try:
                    loader.load(self.entry("bad"))
                    problem = ""
                except loader.PluginError as error:
                    problem = str(error)
                self.assertIn(expected, problem)
