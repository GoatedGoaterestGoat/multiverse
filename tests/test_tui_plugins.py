"""Plugins in the dashboard: keys, inspector lines, the error count, the Plugins screen and generated settings.

The dashboard talks to plugins through one PluginService; these tests replace it
with a fake, so they need no plugin code, no filesystem and no tmux.
"""
import asyncio
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import DataTable, Input, Static
from multiverse.plugins.settings import DELETE
from multiverse.ui.app import MultiverseApp
from multiverse.ui.plugins_screen import PluginSettingsScreen, PluginsScreen, schema_fields
from multiverse.ui.screens import KEYS
from multiverse.ui.tree import agent_key
from multiverse.plugins.testing import PluginTestCase
from multiverse.ui.plugin_service import PluginService
from multiverse.ui.backend import LocalBackend
from tui_demo import DemoBackend

SCHEMA = {"type": "object", "properties": {
    "greeting": {"type": "string", "title": "Greeting", "default": "hello"},
    "retries": {"type": "integer", "default": 3},
    "ratio": {"type": "number"},
    "loud": {"type": "boolean"},
    "mode": {"type": "string", "enum": ["fast", "slow"], "default": "fast"},
    "note": {"type": ["string", "null"]},
    "limits": {"type": "object", "properties": {"max": {"type": "integer"}}},
    "tags": {"type": "array", "items": {"type": "string"}}}}


def item(name, on=True, state="loaded", settings=None, **extra):
    return {"name": name, "on": on, "state": state, "source": "bundled", "version": "0.1.0", "description": f"{name} plugin",
            "problem": None, "errors": [], "permissions": [], "commands": {}, "shadowed": [], "settings": settings, **extra}


class FakeService:
    """The PluginService surface the dashboard uses, recording what it is asked."""

    def __init__(self):
        self.items = [item("alpha"), item("beta", on=False, state="off")]
        self.keys, self.screens, self.clashes = {}, {}, []
        self.errors = 0
        self.fact_lines = []
        self.fact_delay = 0.0
        self.gate = None
        self.calls = []
        self.fail = None
        self.core_keys_seen = None
        self.key_result = "alpha did it"
        self.off = set()

    def listing(self, root):
        return [dict(entry) for entry in self.items]

    def problems(self, root):
        return []

    def error_count(self):
        return self.errors

    def registry(self, root, core_keys):
        self.core_keys_seen = set(core_keys)
        return {"keys": dict(self.keys), "screens": dict(self.screens), "clashes": list(self.clashes)}

    def on_here(self, root):
        return {entry["name"] for entry in self.items if entry["on"]} - self.off

    def facts(self, root, row):
        self.calls.append(("facts", Path(root), row["id"]))
        time.sleep(self.fact_delay)
        if self.gate:
            self.gate.wait(5)
        return list(self.fact_lines)

    def run_key(self, scope, entry, row):
        self.calls.append(("key", Path(scope), entry[0], row and row["id"], row and dict(row) is not row))
        if self.fail:
            raise RuntimeError(self.fail)
        if entry[0] in self.off:
            raise ValueError(f"{entry[0]} is off in {Path(scope).name}")
        return self.key_result

    def set_enabled(self, root, name, on):
        self.calls.append(("enable", Path(root), name, on))
        if self.fail:
            raise ValueError(self.fail)
        for entry in self.items:
            if entry["name"] == name:
                entry["on"], entry["state"] = on, "loaded" if on else "off"
        return {"name": name, "on": on, "installed": []}

    def edit_settings(self, root, name, changes):
        self.calls.append(("edit", Path(root), name, dict(changes)))
        if self.fail:
            raise ValueError(self.fail)
        return {}

    def settings_screen(self, root, name, reference):
        return OwnScreen


from textual.screen import ModalScreen  # noqa: E402


class OwnScreen(ModalScreen):
    def __init__(self, root, backend):
        super().__init__()
        self.opened_with = (root, backend)

    def compose(self):
        yield Static("own settings", id="own")


class DashboardCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = DemoBackend()
        self.service = FakeService()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0, plugin_service=self.service)

    async def until(self, pilot, check, what, timeout=8.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            await self.app.workers.wait_for_complete()
            try:
                if check():
                    return
            except Exception:
                pass
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"timed out waiting for {what}")
            await pilot.pause(0.02)

    async def start(self, pilot):
        await self.until(pilot, lambda: self.app.snapshot is not None and self.app.visible_rows, "the dashboard")
        await pilot.pause(0.1)
        await self.app.workers.wait_for_complete()

    def text(self, selector):
        return str(self.app.query_one(selector, Static).render())


class PluginDashboardTests(DashboardCase):
    async def test_without_plugins_nothing_changes(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            self.assertEqual(self.app.plugin_keys, {})
            self.assertEqual(self.app.plugin_help(), "")
            self.assertNotIn("plugin error", self.text("#totals"))
            self.assertFalse(self.app.query_one("#inspect-plugins").display)
            self.app.action_help()
            await pilot.pause(0.1)
            self.assertEqual(self.app.screen.content, KEYS)
            self.assertNotIn("plugin_key", " ".join(b.action for items in self.app._bindings.key_to_bindings.values() for b in items))
            # The dashboard told the registry which keys are its own, so a plugin cannot take them.
            self.assertTrue({"c", "p", "n", "enter", "question_mark", "j"} <= self.service.core_keys_seen)

    async def test_plugin_error_count_shows_in_the_header(self):
        self.service.errors = 1
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            self.assertTrue(self.text("#totals").endswith("· 1 plugin error"), self.text("#totals"))
            self.service.errors = 3
            self.app.refresh_snapshot()
            await self.until(pilot, lambda: self.text("#totals").endswith("· 3 plugin errors"), "the new count")

    async def test_a_plugin_key_runs_with_a_copy_of_the_row_and_its_scope_and_reports_back(self):
        self.service.keys = {"z": ("alpha", "Say hello", None, None)}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: "z" in self.app.plugin_keys, "the plugin key")
            agent = next(a for a in self.backend.agents if a["name"] == "kernel-worker")
            self.app.select_key(agent_key(agent))
            await pilot.press("z")
            await self.until(pilot, lambda: any(call[0] == "key" for call in self.service.calls), "the key action")
            call = next(call for call in self.service.calls if call[0] == "key")
            self.assertEqual(call[1:4], (Path(agent["universe"]), "alpha", agent["id"]))
            self.assertTrue(call[4])
            await self.until(pilot, lambda: "alpha did it" in self.text("#status"), "the action's status line")
            self.assertFalse(self.app.operating)

    async def test_a_plugin_key_that_fails_or_is_off_says_so_and_never_stops_the_dashboard(self):
        self.service.keys = {"z": ("alpha", "Say hello", None, None)}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: "z" in self.app.plugin_keys, "the plugin key")
            self.service.off = {"alpha"}
            await pilot.press("z")
            await self.until(pilot, lambda: "alpha is off in" in self.text("#status"), "the off message")
            self.service.off = set()
            self.service.fail = "alpha: Say hello failed; see mv2 plugins"
            await pilot.press("z")
            await self.until(pilot, lambda: "failed; see mv2 plugins" in self.text("#status"), "the failure message")
            self.assertFalse(self.app.operating)
            await pilot.press("j")
            self.assertTrue(self.app.is_running)

    async def test_plugin_keys_are_ignored_while_typing_or_in_a_dialog(self):
        self.service.keys = {"z": ("alpha", "Say hello", None, None)}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: "z" in self.app.plugin_keys, "the plugin key")
            await pilot.press("slash", "z")
            await pilot.pause(0.2)
            self.assertEqual(self.app.query_one("#filter", Input).value, "z")
            await pilot.press("escape")
            self.app.action_help()
            await pilot.pause(0.1)
            await pilot.press("z")
            await pilot.pause(0.2)
            self.assertEqual([call for call in self.service.calls if call[0] == "key"], [])

    async def test_help_lists_plugin_keys_screens_and_clashes(self):
        self.service.keys = {"z": ("alpha", "Say hello", None, None)}
        self.service.screens = {("alpha", "board"): (None, "x:Board")}
        self.service.clashes = ["alpha and beta both want the y key; set it in plugins.json keys"]
        async with self.app.run_test(size=(140, 40)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_keys, "the registry")
            self.app.action_help()
            await pilot.pause(0.1)
            content = self.app.screen.content
            self.assertTrue(content.startswith(KEYS))
            self.assertIn("z              alpha: Say hello", content)
            self.assertIn("alpha: board (command palette)", content)
            self.assertIn("Not bound: alpha and beta both want the y key", content)

    async def test_the_command_palette_offers_plugins_and_plugin_screens(self):
        self.service.screens = {("alpha", "board"): (None, "x:Board")}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_screens, "the registry")
            titles = [command.title for command in self.app.get_system_commands(self.app.screen)]
            self.assertIn("Plugins", titles)
            self.assertIn("alpha: board", titles)

    async def test_inspector_shows_plugin_lines_after_the_core_facts_without_blocking(self):
        self.service.fact_lines = [("alpha", "3 open"), ("beta", "unavailable")]
        self.service.gate = threading.Event()
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            agent = next(a for a in self.backend.agents if a["name"] == "kernel-worker")
            self.app.select_key(agent_key(agent))
            await pilot.press("p")
            for _ in range(100):
                if self.app.inspector.row and any(call[0] == "facts" for call in self.service.calls):
                    break
                await pilot.pause(0.02)
            # The plugin is still working: the core facts are there and the dashboard answers keys.
            self.assertIn("Status", self.text("#inspect-state"))
            self.assertFalse(self.app.query_one("#inspect-plugins").display)
            before = self.app.selected_key
            await pilot.press("j")
            await pilot.pause(0.1)
            self.assertNotEqual(self.app.selected_key, before)
            self.service.gate.set()
            await self.until(pilot, lambda: self.app.query_one("#inspect-plugins").display, "the plugin lines")
            self.assertEqual(self.text("#inspect-plugins"), "alpha: 3 open\nbeta: unavailable")
            self.assertEqual(self.service.calls[0][:2], ("facts", Path(agent["universe"])))

    async def test_a_stuck_plugin_fact_times_out_into_one_line(self):
        self.service.fact_delay = 2.0
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            from multiverse.ui import inspector
            original = inspector.PLUGIN_FACTS_TIMEOUT
            inspector.PLUGIN_FACTS_TIMEOUT = 0.2
            try:
                await pilot.press("p")
                await self.until(pilot, lambda: self.app.query_one("#inspect-plugins").display, "the timeout line", timeout=4)
            finally:
                inspector.PLUGIN_FACTS_TIMEOUT = original
            self.assertEqual(self.text("#inspect-plugins"), "plugins: unavailable")

    async def test_group_rows_have_no_plugin_lines(self):
        self.service.fact_lines = [("alpha", "3 open")]
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            self.app.select_key(next(key for key, row in self.app.visible_rows.items() if row["kind"] == "group"))
            await pilot.press("p")
            await self.until(pilot, lambda: self.app.inspector.row, "the inspector")
            await pilot.pause(0.3)
            self.assertFalse(self.app.query_one("#inspect-plugins").display)
            self.assertEqual([call for call in self.service.calls if call[0] == "facts"], [])


class PluginsScreenTests(DashboardCase):
    async def open_screen(self, pilot):
        await self.start(pilot)
        self.app.action_plugins()
        await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen)
                         and self.app.screen.query_one("#plugins-table", DataTable).row_count == 2, "the Plugins screen")
        return self.app.screen

    async def test_lists_plugins_with_their_state_here(self):
        async with self.app.run_test(size=(100, 32)) as pilot:
            screen = await self.open_screen(pilot)
            table = screen.query_one("#plugins-table", DataTable)
            self.assertEqual([table.get_row_at(i)[:3] for i in range(2)], [["alpha", "on", "on"], ["beta", "off", "off"]])
            self.assertIn("alpha plugin", str(screen.query_one("#plugin-details", Static).render()))
            await pilot.press("down")
            await self.until(pilot, lambda: "beta plugin" in str(screen.query_one("#plugin-details", Static).render()), "beta's details")

    async def test_space_turns_a_plugin_on_in_the_scope_and_the_dashboard_refreshes(self):
        async with self.app.run_test(size=(100, 32)) as pilot:
            screen = await self.open_screen(pilot)
            scans = self.app.scan_count
            await pilot.press("down", "space")
            await self.until(pilot, lambda: screen.query_one("#plugins-table", DataTable).get_row_at(1)[1] == "on", "beta to be on")
            self.assertIn(("enable", self.app.target_scope(), "beta", True), self.service.calls)
            with mock.patch("multiverse.ui.columns.reset_plugin_caches") as reset:
                await pilot.press("escape")
                await self.until(pilot, lambda: self.app.screen is not screen, "the screen to close")
                reset.assert_called_once_with()
            await self.until(pilot, lambda: self.app.scan_count > scans, "a refresh after the change")

    async def test_a_refused_toggle_shows_the_reason_and_changes_nothing(self):
        self.service.fail = "beta is untrusted: run mv2 plugins trust beta"
        async with self.app.run_test(size=(100, 32)) as pilot:
            screen = await self.open_screen(pilot)
            await pilot.press("down", "space")
            await self.until(pilot, lambda: "untrusted" in str(screen.query_one(".cerror", Static).render()), "the reason")
            self.assertEqual(screen.query_one("#plugins-table", DataTable).get_row_at(1)[1], "off")
            self.assertFalse(screen.changed)

    async def test_plugin_without_settings_says_so(self):
        async with self.app.run_test(size=(100, 32)) as pilot:
            screen = await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: "has no settings" in str(screen.query_one(".cerror", Static).render()), "the message")

    async def test_settings_screen_is_generated_from_the_schema_and_saves_only_edits(self):
        settings = {"path": "/u/plugin-settings/alpha.json", "source": "file", "problem": None, "schema": SCHEMA,
                    "values": {"greeting": "hello", "retries": 3, "mode": "fast", "limits": {"max": 9}}, "screen": None}
        self.service.items[0]["settings"] = settings
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets, "settings")
            edit = self.app.screen
            self.assertEqual(list(edit.widgets), ["greeting", "retries", "ratio", "loud", "mode", "note", "limits.max"])
            self.assertEqual((edit.widgets["greeting"].value, edit.widgets["retries"].value, edit.widgets["limits.max"].value),
                             ("hello", "3", "9"))
            self.assertEqual(edit.widgets["greeting"].placeholder, "hello")
            self.assertIn("tags", str(edit.query(".cwarn").first().render()))
            await pilot.press("ctrl+enter")
            await self.until(pilot, lambda: self.app.screen is screen, "an unchanged save to close")
            self.assertEqual([call for call in self.service.calls if call[0] == "edit"], [])
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets, "settings")
            edit = self.app.screen
            edit.widgets["greeting"].value = ""
            edit.widgets["retries"].value = "5"
            edit.widgets["loud"].action_step(1)
            edit.widgets["limits.max"].value = "12"
            await pilot.press("ctrl+enter")
            await self.until(pilot, lambda: self.app.screen is screen, "the save to close")
            call = next(call for call in self.service.calls if call[0] == "edit")
            self.assertEqual(call[2], "alpha")
            self.assertEqual(call[3], {("greeting",): DELETE, ("retries",): 5, ("loud",): True, ("limits", "max"): 12})

    async def test_a_rejected_settings_save_stays_open_with_the_reason(self):
        settings = {"path": "p", "source": "file", "problem": None, "schema": SCHEMA, "values": {}, "screen": None}
        self.service.items[0]["settings"] = settings
        async with self.app.run_test(size=(100, 40)) as pilot:
            await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets, "settings")
            edit = self.app.screen
            edit.widgets["retries"].value = "lots"
            await pilot.press("ctrl+enter")
            await self.until(pilot, lambda: "whole number" in str(edit.query_one(".cerror", Static).render()), "the number error")
            edit.widgets["retries"].value = "-1"
            self.service.fail = "retries must be at least 0"
            await pilot.press("ctrl+enter")
            await self.until(pilot, lambda: "at least 0" in str(edit.query_one(".cerror", Static).render()), "the schema error")
            self.assertIs(self.app.screen, edit)
            await pilot.press("escape")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen), "the list again")

    async def test_a_plugin_with_its_own_settings_screen_gets_it(self):
        self.service.items[0]["settings"] = {"path": "p", "source": "file", "problem": None, "schema": None, "values": {},
                                             "screen": "ui:Settings"}
        async with self.app.run_test(size=(100, 32)) as pilot:
            await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, OwnScreen), "the plugin's screen")
            self.assertEqual(self.app.screen.opened_with[1], self.backend)

    async def test_compact_terminals_keep_the_list_and_error_line_visible(self):
        async with self.app.run_test(size=(60, 20)) as pilot:
            screen = await self.open_screen(pilot)
            dialog = screen.query_one("#plugins-dialog").region
            self.assertLessEqual(dialog.bottom, 20)
            self.assertGreaterEqual(screen.query_one("#plugins-table", DataTable).region.height, 3)
            self.assertGreaterEqual(screen.query_one(".cerror", Static).region.y, 0)


class SchemaFieldTests(unittest.TestCase):
    def test_each_scalar_property_gets_a_field_and_others_are_listed(self):
        fields, skipped = schema_fields(SCHEMA, {"retries": 3, "limits": {"max": 9}})
        self.assertEqual([(f.key, f.kind) for f in fields],
                         [("greeting", "text"), ("retries", "int"), ("ratio", "float"), ("loud", "choice"),
                          ("mode", "choice"), ("note", "nullable"), ("limits.max", "int")])
        self.assertEqual(skipped, ["tags"])
        by_key = {f.key: f for f in fields}
        self.assertEqual(by_key["retries"].initial, "3")
        self.assertEqual(by_key["limits.max"].initial, "9")
        self.assertEqual(by_key["ratio"].initial, "")
        self.assertEqual(by_key["greeting"].label, "Greeting")
        self.assertEqual([value for value, _ in by_key["mode"].options], [None, "fast", "slow"])
        self.assertEqual(by_key["limits.max"].path, ("limits", "max"))


PLUGIN = """
from multiverse.plugins import api

def register(mv):
    mv.dashboard.key("z", "Say hello", lambda context: f"hello {context.row['name'] if context.row else '-'} in {context.scope.name}")
    mv.dashboard.key("b", "Break", lambda context: 1 / 0)
    mv.dashboard.screen("board", "screens:Board")

    @mv.dashboard.inspector
    def fact(row):
        return "fact for " + row["name"]

    @mv.dashboard.inspector
    def broken(row):
        raise RuntimeError("boom")
"""
SCREENS = """
from textual.screen import ModalScreen
from textual.widgets import Static

class Board(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen", "Close")]
    def __init__(self, root, backend):
        super().__init__()
        self.scope = root
    def compose(self):
        yield Static("board of " + self.scope.name, id="board")
"""


class RealPluginTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    """The dashboard against real plugin code: registry, key actions, inspector lines and the error count."""

    async def test_keys_screens_and_inspector_lines_of_a_real_plugin(self):
        from multiverse.plugins import loader
        self.write_plugin("greeter", {"module": "plugin", "extends": ["dashboard.key", "dashboard.screen", "dashboard.inspector"],
                                      "settings": {"version": 1, "schema": "schema.json"}},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS,
                           "schema.json": '{"type": "object", "properties": {"greeting": {"type": "string", "default": "hi"}}}'})
        self.enable("greeter")
        app = MultiverseApp(self.root, LocalBackend(), refresh_interval=0, plugin_service=PluginService())
        async with app.run_test(size=(140, 36)) as pilot:
            for _ in range(200):
                await app.workers.wait_for_complete()
                if app.snapshot is not None and app.plugin_keys:
                    break
                await pilot.pause(0.02)
            self.assertEqual(sorted(app.plugin_keys), ["b", "z"])
            self.assertIn(("greeter", "board"), app.plugin_screens)
            await pilot.press("z")
            for _ in range(200):
                await app.workers.wait_for_complete()
                if "hello" in str(app.query_one("#status", Static).render()):
                    break
                await pilot.pause(0.02)
            self.assertIn("hello", str(app.query_one("#status", Static).render()))
            await pilot.press("b")
            for _ in range(200):
                await app.workers.wait_for_complete()
                if "failed" in str(app.query_one("#status", Static).render()):
                    break
                await pilot.pause(0.02)
            self.assertIn("failed", str(app.query_one("#status", Static).render()))
            self.assertTrue(app.is_running)
            self.assertGreaterEqual(app.plugin_service.error_count(), 1)
            app.refresh_snapshot()
            for _ in range(200):
                await app.workers.wait_for_complete()
                if "plugin error" in str(app.query_one("#totals", Static).render()):
                    break
                await pilot.pause(0.02)
            self.assertIn("plugin error", str(app.query_one("#totals", Static).render()))
            app.action_plugins()
            for _ in range(200):
                await app.workers.wait_for_complete()
                if isinstance(app.screen, PluginsScreen) and app.screen.items:
                    break
                await pilot.pause(0.02)
            names = [entry["name"] for entry in app.screen.items]
            self.assertIn("greeter", names)
            self.assertTrue(next(entry for entry in app.screen.items if entry["name"] == "greeter")["on"])

    async def test_service_facts_and_a_plugin_screen_from_the_palette(self):
        self.write_plugin("greeter", {"module": "plugin", "extends": ["dashboard.key", "dashboard.screen", "dashboard.inspector"]},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS})
        self.enable("greeter")
        service = PluginService()
        self.assertEqual(service.facts(self.root, {"name": "w"}), [("greeter", "fact for w"), ("greeter", "unavailable")])
        self.assertGreaterEqual(service.error_count(), 1)
        app = MultiverseApp(self.root, LocalBackend(), refresh_interval=0, plugin_service=service)
        async with app.run_test(size=(120, 30)) as pilot:
            for _ in range(200):
                await app.workers.wait_for_complete()
                if app.plugin_screens:
                    break
                await pilot.pause(0.02)
            command = next(c for c in app.get_system_commands(app.screen) if c.title == "greeter: board")
            command.callback()
            for _ in range(200):
                await app.workers.wait_for_complete()
                if app.screen.query("#board"):
                    break
                await pilot.pause(0.02)
            self.assertIn("board of", str(app.screen.query_one("#board", Static).render()))


if __name__ == "__main__":
    unittest.main()
