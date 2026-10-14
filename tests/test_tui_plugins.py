"""Plugins in the dashboard: keys, inspector lines, the error count, the Plugins screen and generated settings.

The dashboard talks to plugins through one PluginService; these tests replace it
with a fake, so they need no plugin code, no filesystem and no tmux.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import DataTable, Input, Static
from multiverse.plugins.settings import DELETE
from multiverse.store import write
from multiverse.ui.app import MultiverseApp
from multiverse.ui.plugins_screen import PluginSettingsScreen, PluginsScreen, declared_fields, describe, schema_fields
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
        self.actions, self.screens = {}, {}
        self.errors = 0
        self.fact_lines = []
        self.fact_delay = 0.0
        self.gate = None
        self.calls = []
        self.fail = None
        self.key_result = "alpha did it"
        self.off = set()
        self.host = {"needed": False, "running": False, "stopped": {}, "pid": None, "workers": []}
        self.host_start_error = None

    def host_state(self, root):
        self.calls.append(("host_state", Path(root)))
        return dict(self.host) if self.host is not None else None

    def start_host(self, root):
        self.calls.append(("start_host", Path(root)))
        if self.host_start_error:
            raise RuntimeError(self.host_start_error)
        self.host["running"] = True

    def stop_host(self, root):
        self.calls.append(("stop_host", Path(root)))
        self.host["running"] = False

    def listing(self, root):
        return [dict(entry) for entry in self.items]

    def problems(self, root):
        return []

    def error_count(self):
        return self.errors

    def registry(self, root):
        self.calls.append(("registry", Path(root)))
        return {"actions": dict(self.actions), "screens": dict(self.screens)}

    def on_here(self, root):
        return {entry["name"] for entry in self.items if entry["on"]} - self.off

    def facts(self, root, row):
        self.calls.append(("facts", Path(root), row["id"]))
        time.sleep(self.fact_delay)
        if self.gate:
            self.gate.wait(5)
        return list(self.fact_lines)

    def run_action(self, scope, entry, row):
        self.calls.append(("action", Path(scope), entry[0], entry[1], row and row["id"], bool(row) and dict(row) is not row))
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

    def screen_class(self, loaded, reference):
        self.calls.append(("screen_class", reference))
        if reference == "broken:Screen":
            raise ImportError("no module named broken")
        return DismissingScreen

    def record(self, loaded, what, error, root):
        self.calls.append(("record", what, str(error)))


from textual.screen import ModalScreen  # noqa: E402


class OwnScreen(ModalScreen):
    def __init__(self, root, backend):
        super().__init__()
        self.opened_with = (root, backend)

    def compose(self):
        yield Static("own settings", id="own")


class DismissingScreen(ModalScreen):
    """A plugin screen opened by a key; Enter closes it with a status line."""
    BINDINGS = [("enter", "done", "Done"), ("escape", "nothing", "Nothing")]

    def __init__(self, root, backend):
        super().__init__()
        self.scope = root

    def compose(self):
        yield Static("screen of " + self.scope.name, id="keyscreen")

    def action_done(self):
        self.dismiss("saved by the plugin")

    def action_nothing(self):
        self.dismiss(None)


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


class LiveDemo(DemoBackend):
    """The demo agents, but not in demo mode, which never starts anything."""
    demo = False


class HostTests(DashboardCase):
    def setUp(self):
        super().setUp()
        self.backend = LiveDemo()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0, plugin_service=self.service)

    def called(self, name):
        return [call for call in self.service.calls if call[0] == name]

    async def test_nothing_is_started_or_shown_when_no_plugin_needs_the_host(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.called("host_state"), "the host check")
            self.assertEqual(self.called("start_host"), [])
            self.assertNotIn("host off", self.text("#totals"))

    async def test_the_host_starts_on_open_when_needed_and_is_left_running_on_quit(self):
        self.service.host = {"needed": True, "running": False, "stopped": {}, "pid": None, "workers": []}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.called("start_host"), "the host to start")
            await self.until(pilot, lambda: not self.app.query_one("#status", Static).display or "Starting" not in self.text("#status"),
                             "the status line to clear")
            self.assertNotIn("host off", self.text("#totals"))
            self.assertEqual(len(self.called("start_host")), 1)
            await pilot.press("q")
        self.assertEqual(self.called("stop_host"), [])

    async def test_a_host_that_will_not_start_is_reported_once_and_shown_in_the_header(self):
        self.service.host = {"needed": True, "running": False, "stopped": {}, "pid": None, "workers": []}
        self.service.host_start_error = "no tmux"
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: "could not start: no tmux" in self.text("#status"), "the failure")
            await self.until(pilot, lambda: "host off" in self.text("#totals"), "host off in the header")
            self.app.check_host()  # a later check does not try again until something changes
            await self.app.workers.wait_for_complete()
            self.assertEqual(len(self.called("start_host")), 1)
            self.assertTrue(self.app.is_running)

    async def test_stopped_workers_count_as_plugin_errors(self):
        self.service.host = {"needed": True, "running": True, "stopped": {"loud": "its worker exited 4 times in 5 minutes"},
                             "pid": 7, "workers": []}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: "1 plugin error" in self.text("#totals"), "the stopped worker")
            self.assertNotIn("host off", self.text("#totals"))

    async def test_the_plugins_screen_starts_and_stops_the_host_and_the_dashboard_rechecks(self):
        self.service.host = {"needed": True, "running": True, "stopped": {}, "pid": 7, "workers": ["loud"]}
        async with self.app.run_test(size=(100, 32)) as pilot:
            await self.start(pilot)
            self.app.action_plugins()
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen) and self.app.screen.host, "the screen")
            screen = self.app.screen
            self.assertIn("Plugin host: running", str(screen.query_one("#plugin-details", Static).render()))
            await pilot.press("h")
            await self.until(pilot, lambda: self.called("stop_host") and not screen.busy, "the host to stop")
            self.assertIn("Plugin host: off (h starts it)", str(screen.query_one("#plugin-details", Static).render()))
            await pilot.press("h")
            await self.until(pilot, lambda: self.called("start_host") and not screen.busy, "the host to start again")
            self.assertTrue(screen.changed)

    async def test_a_host_stopped_with_h_is_not_started_again_when_the_screen_closes(self):
        self.service.host = {"needed": True, "running": True, "stopped": {}, "pid": 7, "workers": ["loud"]}
        async with self.app.run_test(size=(100, 32)) as pilot:
            await self.start(pilot)
            self.app.action_plugins()
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen) and self.app.screen.host, "the screen")
            screen = self.app.screen
            await pilot.press("h")
            await self.until(pilot, lambda: self.called("stop_host") and not screen.busy, "the host to stop")
            await pilot.press("escape")
            await self.until(pilot, lambda: self.app.screen is not screen, "the screen to close")
            await pilot.pause(0.3)
            await self.app.workers.wait_for_complete()
            self.assertEqual(self.called("start_host"), [])
            self.assertIn("host off", self.text("#totals"))
            # h again starts it, and a later check may keep it up.
            self.app.action_plugins()
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen) and self.app.screen.host, "the screen")
            await pilot.press("h")
            await self.until(pilot, lambda: self.called("start_host"), "the host to start")

    async def test_start_failure_on_the_screen_shows_the_reason(self):
        self.service.host = {"needed": True, "running": False, "stopped": {}, "pid": None, "workers": []}
        self.service.host_start_error = "no tmux"
        async with self.app.run_test(size=(100, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: "host off" in self.text("#totals"), "the failed start")
            self.app.action_plugins()
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen) and self.app.screen.host, "the screen")
            screen = self.app.screen
            await pilot.press("h")
            await self.until(pilot, lambda: "could not start: no tmux" in str(screen.query_one(".cerror", Static).render()), "the reason")


class PlainServiceTests(unittest.TestCase):
    def test_a_host_that_does_not_come_up_points_at_its_real_log(self):
        from multiverse.plugins import host
        service = PluginService()
        with mock.patch.object(host, "start", return_value={"running": False}):
            with self.assertRaises(RuntimeError) as raised:
                service.start_host(Path("/demo/fastvideo/.multiverse"))
        self.assertIn(str(host.host_directory(Path("/demo/fastvideo/.multiverse")) / "host.log"), str(raised.exception))
        self.assertIn("state/plugins/host/host.log", str(raised.exception))


class PluginDashboardTests(DashboardCase):
    def command(self, title):
        return next((SimpleNamespace(callback=callback) for name, _, callback in self.app.plugin_palette() if name == title), None)

    async def test_without_plugins_nothing_changes(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: any(call[0] == "registry" for call in self.service.calls), "the registry")
            self.assertEqual((self.app.plugin_actions, self.app.plugin_screens), ({}, {}))
            self.assertEqual(self.app.plugin_help(), "")
            self.assertNotIn("plugin error", self.text("#totals"))
            self.assertFalse(self.app.query_one("#inspect-plugins").display)
            self.app.action_help()
            await pilot.pause(0.1)
            self.assertEqual(self.app.screen.content, KEYS)
            self.assertEqual([name for name, _, _ in self.app.plugin_palette()], [])
            # Plugins have no keys: nothing binds to a plugin.
            self.assertFalse([b for items in self.app._bindings.key_to_bindings.values() for b in items
                              if "plugin" in b.action and b.action != "plugins"])

    async def test_palette_entries_come_from_the_opened_root_and_stay_while_moving_between_subverses(self):
        self.service.actions = {("alpha", "Say hello"): ("alpha", "Say hello", None, None)}
        self.service.screens = {("alpha", "board"): (None, "x:Board")}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_actions, "the registry")
            review = self.backend.root / "subverses/pr-review"
            self.app.enter_scope(review)
            await self.until(pilot, lambda: self.app.root == review and self.app.snapshot and self.app.snapshot["name"], "the portal")
            await pilot.pause(0.3)
            await self.app.workers.wait_for_complete()
            self.assertEqual([call[1] for call in self.service.calls if call[0] == "registry"], [self.backend.root])
            self.assertEqual(sorted(self.app.plugin_actions), [("alpha", "Say hello")])
            self.assertEqual(sorted(self.app.plugin_screens), [("alpha", "board")])
            self.app.action_back()
            await self.until(pilot, lambda: self.app.root == self.backend.root, "back at the opened root")
            self.assertEqual(self.app.home, self.backend.root)

    async def test_shift_p_opens_the_plugins_screen_and_help_says_so(self):
        self.service.items[0]["provides"] = {"columns": ["shout"], "panels": ["counter"], "screens": [], "commands": ["hello"], "services": 2, "events": None}
        async with self.app.run_test(size=(140, 40)) as pilot:
            await self.start(pilot)
            self.assertIn("P              Plugins", KEYS)
            await pilot.press("P")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen)
                             and self.app.screen.query_one("#plugins-table", DataTable).row_count == 2, "the Plugins screen")
            text = str(self.app.screen.query_one("#plugin-details", Static).render())
            self.assertIn("Adds here: columns shout · widgets counter · mv2 commands hello · 2 services", text)
            self.assertIn("Configure", [b.description for b in self.app.screen.BINDINGS])

    async def test_a_row_highlighted_while_the_dashboard_shuts_down_is_not_an_error(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            key = next(iter(self.app.visible_agents))
        # The screen is gone, as when a highlight event is still queued at shutdown.
        self.app.show_selection(key)
        self.assertEqual(self.app.selected_key, key)

    async def test_plugin_error_count_shows_in_the_header(self):
        self.service.errors = 1
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            self.assertTrue(self.text("#totals").endswith("· 1 plugin error"), self.text("#totals"))
            self.service.errors = 3
            self.app.refresh_snapshot()
            await self.until(pilot, lambda: self.text("#totals").endswith("· 3 plugin errors"), "the new count")

    async def test_a_palette_action_runs_with_a_copy_of_the_row_and_its_scope_and_reports_back(self):
        self.service.actions = {("alpha", "Say hello"): ("alpha", "Say hello", None, None)}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_actions, "the registry")
            agent = next(a for a in self.backend.agents if a["name"] == "kernel-worker")
            self.app.select_key(agent_key(agent))
            self.command("alpha: Say hello").callback()
            await self.until(pilot, lambda: any(call[0] == "action" for call in self.service.calls), "the action")
            call = next(call for call in self.service.calls if call[0] == "action")
            self.assertEqual(call[1:5], (Path(agent["universe"]), "alpha", "Say hello", agent["id"]))
            self.assertTrue(call[5])
            await self.until(pilot, lambda: "alpha did it" in self.text("#status"), "the action's status line")
            self.assertFalse(self.app.operating)

    async def test_a_palette_action_that_fails_or_is_off_says_so_and_never_stops_the_dashboard(self):
        self.service.actions = {("alpha", "Say hello"): ("alpha", "Say hello", None, None)}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_actions, "the registry")
            self.service.off = {"alpha"}
            self.command("alpha: Say hello").callback()
            await self.until(pilot, lambda: "alpha is off in" in self.text("#status"), "the off message")
            self.service.off = set()
            self.service.fail = "alpha: Say hello failed; see mv2 plugins"
            self.command("alpha: Say hello").callback()
            await self.until(pilot, lambda: "failed; see mv2 plugins" in self.text("#status"), "the failure message")
            self.assertFalse(self.app.operating)
            await pilot.press("j")
            self.assertTrue(self.app.is_running)

    async def test_a_plugin_screen_is_built_from_the_scope_and_its_result_is_the_status(self):
        self.service.screens = {("alpha", "board"): (None, "screens:Board")}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_screens, "the registry")
            self.command("alpha: board").callback()
            await self.until(pilot, lambda: isinstance(self.app.screen, DismissingScreen), "the plugin's screen")
            self.assertEqual(self.app.screen.scope, self.app.target_scope())
            self.assertFalse(self.app.operating)
            await pilot.press("enter")
            await self.until(pilot, lambda: not isinstance(self.app.screen, DismissingScreen), "the screen to close")
            self.assertIn("saved by the plugin", self.text("#status"))

    async def test_a_plugin_screen_says_when_the_plugin_is_off_and_reports_a_broken_one(self):
        self.service.screens = {("alpha", "board"): (None, "screens:Board"), ("alpha", "broken"): (None, "broken:Screen")}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_screens, "the registry")
            self.service.off = {"alpha"}
            self.command("alpha: board").callback()
            await self.until(pilot, lambda: "alpha is off in" in self.text("#status"), "the off message")
            self.assertNotIsInstance(self.app.screen, DismissingScreen)
            self.service.off = set()
            self.command("alpha: broken").callback()
            await self.until(pilot, lambda: "screen failed: no module named broken" in self.text("#status"), "the failure")
            self.assertIn(("record", "screen broken:Screen", "no module named broken"), self.service.calls)
            self.assertTrue(self.app.is_running)

    async def test_help_lists_what_plugins_add_to_the_palette(self):
        self.service.actions = {("alpha", "Say hello"): ("alpha", "Say hello", None, None)}
        self.service.screens = {("alpha", "board"): (None, "x:Board")}
        async with self.app.run_test(size=(140, 40)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_actions, "the registry")
            self.app.action_help()
            await pilot.pause(0.1)
            content = self.app.screen.content
            self.assertTrue(content.startswith(KEYS))
            self.assertIn("PLUGINS (command palette)", content)
            self.assertIn("Ctrl+p         alpha: Say hello", content)
            self.assertIn("alpha: board (screen)", content)

    async def test_the_command_palette_offers_plugins_actions_and_plugin_screens(self):
        self.service.actions = {("alpha", "Say hello"): ("alpha", "Say hello", None, None)}
        self.service.screens = {("alpha", "board"): (None, "x:Board")}
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.start(pilot)
            await self.until(pilot, lambda: self.app.plugin_screens, "the registry")
            titles = [command.title for command in self.app.get_system_commands(self.app.screen)] + [name for name, _, _ in self.app.plugin_palette()]
            self.assertIn("Plugins", titles)
            self.assertIn("alpha: board", titles)
            self.assertIn("alpha: Say hello", titles)

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
            self.assertEqual([call[:2] for call in self.service.calls if call[0] == "facts"][0], ("facts", Path(agent["universe"])))

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
            self.assertEqual([[str(cell) for cell in table.get_row_at(i)[:3]] for i in range(2)], [["alpha", "on", "on"], ["beta", "off", "off"]])
            self.assertIn("alpha plugin", str(screen.query_one("#plugin-details", Static).render()))
            await pilot.press("down")
            await self.until(pilot, lambda: "beta plugin" in str(screen.query_one("#plugin-details", Static).render()), "beta's details")

    async def test_space_turns_a_plugin_on_in_the_scope_and_the_dashboard_refreshes(self):
        async with self.app.run_test(size=(100, 32)) as pilot:
            screen = await self.open_screen(pilot)
            scans = self.app.scan_count
            await pilot.press("down", "space")
            await self.until(pilot, lambda: screen.query_one("#plugins-table", DataTable).get_row_at(1)[1].plain == "on", "beta to be on")
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
            self.assertEqual(screen.query_one("#plugins-table", DataTable).get_row_at(1)[1].plain, "off")
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

    async def test_error_lines_on_the_plugins_screen_are_red(self):
        async with self.app.run_test(size=(100, 32)) as pilot:
            screen = await self.open_screen(pilot)
            self.assertEqual(str(screen.query_one(".cerror").styles.color.hex), "#F38BA8")

    async def test_the_settings_title_names_the_universe_not_the_hidden_directory(self):
        self.service.items[0]["settings"] = {"path": "p", "source": "file", "problem": None, "schema": SCHEMA, "values": {},
                                             "screen": None}
        async with self.app.run_test(size=(100, 40)) as pilot:
            await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets, "settings")
            title = str(self.app.screen.query_one(".detail-title").render())
            self.assertIn("Settings · alpha · Fastvideo", title)
            self.assertNotIn(".multiverse", title)

    async def test_an_enum_with_null_still_has_a_default_option_and_writes_null_when_chosen(self):
        schema = {"type": "object", "properties": {"mode": {"type": ["string", "null"], "enum": ["a", "b", None]}}}
        self.service.items[0]["settings"] = {"path": "p", "source": "file", "problem": None, "schema": schema,
                                             "values": {"mode": "b"}, "screen": None}
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets, "settings")
            edit = self.app.screen
            choice = edit.widgets["mode"]
            self.assertEqual([label for _, label in choice.options], ["default", "a", "b", "null"])
            self.assertEqual(choice.value, "b")
            choice.action_step(1)  # null
            await pilot.press("ctrl+enter")
            await self.until(pilot, lambda: self.app.screen is screen, "the save to close")
            call = next(call for call in self.service.calls if call[0] == "edit")
            self.assertEqual(call[3], {("mode",): None})
            self.service.calls.clear()
            self.service.items[0]["settings"]["values"] = {"mode": None}
            screen.load()
            await pilot.pause(0.3)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets, "settings")
            self.assertEqual(self.app.screen.widgets["mode"].options[self.app.screen.widgets["mode"].index][1], "null")
            await pilot.press("ctrl+enter")
            await self.until(pilot, lambda: self.app.screen is screen, "an unchanged save to close")
            self.assertEqual([call for call in self.service.calls if call[0] == "edit"], [])

    async def test_declared_fields_build_the_settings_screen_and_win_over_the_schema(self):
        fields = [{"path": "default.budget", "label": "Budget", "kind": "int", "placeholder": "0", "help": "tokens kept"},
                  {"path": "loud", "label": "Loud", "kind": "bool", "options": (), "placeholder": "", "help": ""},
                  {"path": "mode", "label": "Mode", "kind": "choice", "options": ("fast", "slow"), "placeholder": "fast", "help": ""},
                  {"path": "note", "label": "Note", "kind": "text", "options": (), "placeholder": "", "help": ""}]
        self.service.items[0]["settings"] = {"path": "p", "source": "file", "problem": None, "schema": SCHEMA, "screen": None,
                                             "values": {"default": {"budget": 5}, "mode": "slow"}, "fields": fields}
        async with self.app.run_test(size=(100, 40)) as pilot:
            screen = await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets, "settings")
            edit = self.app.screen
            self.assertEqual(list(edit.widgets), ["default.budget", "loud", "mode", "note"])
            self.assertEqual((edit.widgets["default.budget"].value, edit.widgets["mode"].value), ("5", "slow"))
            self.assertEqual(edit.widgets["default.budget"].placeholder, "0")
            self.assertIn("tokens kept", " ".join(str(w.render()) for w in edit.query(".fhelp")))
            edit.widgets["default.budget"].value = "9"
            edit.widgets["loud"].action_step(1)
            edit.widgets["mode"].action_step(-2)  # back to the default option
            await pilot.press("ctrl+enter")
            await self.until(pilot, lambda: self.app.screen is screen, "the save to close")
            call = next(call for call in self.service.calls if call[0] == "edit")
            self.assertEqual(call[3], {("default", "budget"): 9, ("loud",): True, ("mode",): DELETE})

    async def test_a_field_screen_brings_its_own_look_not_the_dashboards_stylesheet(self):
        """A plugin's field screen must look right with nothing in app.tcss for its rows, error line or help."""
        fields = [{"path": "budget", "label": "Budget", "kind": "int", "placeholder": "0", "help": "tokens kept"},
                  {"path": "mode", "label": "Mode", "kind": "choice", "options": ("fast", "slow"), "placeholder": "", "help": ""}]
        self.service.items[0]["settings"] = {"path": "p", "source": "file", "problem": None, "schema": None, "values": {}, "screen": None,
                                             "fields": fields}
        async with self.app.run_test(size=(100, 40)) as pilot:
            await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets, "settings")
            screen = self.app.screen
            await pilot.pause(0.1)
            row, label = screen.query(".frow").first(), screen.query(".flabel").first()
            self.assertEqual((row.styles.height.value, label.styles.width.value), (1, 22))
            self.assertEqual(screen.query(".frow Input").first().styles.height.value, 1)
            self.assertEqual(str(screen.query_one(".cerror").styles.color.hex), "#F38BA8")
            self.assertEqual(screen.query(".fhelp").first().styles.padding.left, 22)
            self.assertEqual(screen.styles.align_horizontal, "center")
            sheet = (Path(__file__).resolve().parents[1] / "src/multiverse/ui/app.tcss").read_text()
            self.assertFalse([line for line in sheet.splitlines() if line.startswith((".frow", ".flabel", ".cerror", ".csection", ".cwarn", ".fhelp"))])

    async def test_a_status_line_a_settings_screen_leaves_reaches_the_dashboard_when_the_plugins_screen_closes(self):
        self.service.items[0]["settings"] = {"path": "p", "source": "file", "problem": None, "schema": None, "values": {},
                                             "screen": "ui:Settings"}
        async with self.app.run_test(size=(100, 32)) as pilot:
            screen = await self.open_screen(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(self.app.screen, OwnScreen), "the plugin's screen")
            self.app.screen.dismiss("Settings saved. Restart agents.")
            await self.until(pilot, lambda: self.app.screen is screen, "back on the Plugins screen")
            await pilot.press("escape")
            await self.until(pilot, lambda: "Settings saved. Restart agents." in self.text("#status"), "the status line")

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
    mv.dashboard.action("Say hello", lambda context: f"hello {context.row['name'] if context.row else '-'} in {context.scope.name}")
    mv.dashboard.action("Break", lambda context: 1 / 0)
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

    async def test_actions_screens_and_the_error_count_of_a_real_plugin(self):
        self.write_plugin("greeter", {"module": "plugin", "extends": ["dashboard.action", "dashboard.screen", "dashboard.inspector"],
                                      "settings": {"version": 1, "schema": "schema.json"}},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS,
                           "schema.json": '{"type": "object", "properties": {"greeting": {"type": "string", "default": "hi"}}}'})
        self.enable("greeter")
        app = MultiverseApp(self.root, LocalBackend(), refresh_interval=0, plugin_service=PluginService())

        async def until(check, what):
            for _ in range(300):
                await app.workers.wait_for_complete()
                try:
                    if check():
                        return
                except Exception:
                    pass
                await pilot.pause(0.02)
            self.fail(f"timed out waiting for {what}")

        def status():
            return str(app.query_one("#status", Static).render())

        def run(title):
            next(callback for name, _, callback in app.plugin_palette() if name == title)()

        async with app.run_test(size=(140, 36)) as pilot:
            await until(lambda: app.snapshot is not None and app.plugin_actions, "the registry")
            self.assertEqual(sorted(name for _, name in app.plugin_actions), ["Break", "Say hello"])
            self.assertIn(("greeter", "board"), app.plugin_screens)
            run("greeter: Say hello")
            await until(lambda: "hello" in status(), "the action's status line")
            run("greeter: Break")
            await until(lambda: "failed" in status(), "the failure")
            self.assertTrue(app.is_running)
            self.assertGreaterEqual(app.plugin_service.error_count(), 1)
            app.refresh_snapshot()
            await until(lambda: "plugin error" in str(app.query_one("#totals", Static).render()), "the error count")
            await pilot.press("P")
            await until(lambda: isinstance(app.screen, PluginsScreen) and app.screen.items, "the Plugins screen")
            mine = next(entry for entry in app.screen.items if entry["name"] == "greeter")
            self.assertTrue(mine["on"])
            self.assertIn("actions Break, Say hello", describe(mine))
            self.assertIn("screens board", describe(mine))

    async def test_service_facts_and_a_plugin_screen_from_the_palette(self):
        self.write_plugin("greeter", {"module": "plugin", "extends": ["dashboard.action", "dashboard.screen", "dashboard.inspector"]},
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
            next(callback for name, _, callback in app.plugin_palette() if name == "greeter: board")()
            for _ in range(200):
                await app.workers.wait_for_complete()
                if app.screen.query("#board"):
                    break
                await pilot.pause(0.02)
            self.assertIn("board of", str(app.screen.query_one("#board", Static).render()))


PANELS_PLUGIN = """
from multiverse.plugins import api

def register(mv):
    @mv.dashboard.panel("tally", title="Tally")
    def tally(context):
        return [api.Markdown(f"{context.universe.path.parent.name} n={context.widget.get('n')} on={sorted(context.plugins)}"),
                api.Table(("a", "b"), (("1", "2"),))]

    @mv.dashboard.panel("note", auto=True)
    def note(context):
        return api.Markdown("auto note")

    @mv.dashboard.panel("boom")
    def boom(context):
        raise RuntimeError("kaput")

    @mv.dashboard.panel("slow")
    def slow(context):
        import time
        time.sleep(3)
        return api.Markdown("late")

    @mv.dashboard.panel("bad")
    def bad(context):
        return "not a block"
"""


class PortalPanelTests(PluginTestCase):
    """Plugin panels in a scope's local view: named by the view or automatic, rendered in the snapshot."""

    def setUp(self):
        super().setUp()
        self.write_plugin("panelsy", {"module": "plugin", "extends": ["dashboard.panel"]}, {"plugin.py": PANELS_PLUGIN})
        self.sub = self.subverse("ops")

    def view(self, root, **config):
        write(Path(root) / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name"], **config})

    def widgets(self, root):
        return LocalBackend().snapshot(root)["widgets"]

    def test_a_view_names_a_data_panel_and_gets_its_blocks_with_the_plugin_marked(self):
        self.enable("panelsy")
        self.view(self.root, plugin_panels="none", widgets=[{"type": "tally", "title": "Counts", "n": 3}])
        shown = self.widgets(self.root)
        self.assertEqual([w["type"] for w in shown], ["markdown", "table"])
        self.assertEqual((shown[0]["title"], shown[1]["title"]), ("Counts", ""))
        self.assertIn("n=3 on=['panelsy']", shown[0]["text"])
        self.assertEqual({w["plugin"] for w in shown}, {"panelsy"})
        self.assertEqual(shown[1]["rows"], [["1", "2"]])

    def test_auto_panels_follow_the_views_own_widgets_and_can_be_left_out(self):
        self.enable("panelsy")
        own = {"type": "markdown", "title": "Own", "text": "mine"}
        self.view(self.root, widgets=[own])
        shown = self.widgets(self.root)
        self.assertEqual([(w["type"], w["title"]) for w in shown], [("markdown", "Own"), ("markdown", "panelsy")])
        self.assertEqual(shown[1]["text"], "auto note")
        self.view(self.root, widgets=[own], plugin_panels="none")
        self.assertEqual([w["title"] for w in self.widgets(self.root)], ["Own"])
        self.view(self.root, widgets=[own], plugin_panels=["other"])
        self.assertEqual([w["title"] for w in self.widgets(self.root)], ["Own"])
        self.view(self.root, widgets=[own], plugin_panels=["note"])
        self.assertEqual([w["title"] for w in self.widgets(self.root)], ["Own", "panelsy"])
        self.view(self.root, widgets=[own], plugin_panels=["panelsy:note"])
        self.assertEqual(len(self.widgets(self.root)), 2)

    def test_an_auto_panel_the_view_already_names_is_not_shown_twice(self):
        self.enable("panelsy")
        self.view(self.root, widgets=[{"type": "panelsy:note", "title": "Note"}])
        self.assertEqual([w["title"] for w in self.widgets(self.root)], ["Note"])

    def test_a_plugin_on_only_in_a_subverse_shows_its_panels_there_and_a_hint_elsewhere(self):
        self.enable("panelsy", root=self.sub)
        self.view(self.root, widgets=[{"type": "panelsy:tally", "title": "Counts"}])
        self.view(self.sub, widgets=[{"type": "tally", "title": "Counts", "n": 1}])
        top = self.widgets(self.root)
        self.assertEqual(top[0]["text"], "widget type panelsy:tally needs plugin panelsy, which is off here")
        portal = self.widgets(self.sub)
        self.assertEqual([w["type"] for w in portal], ["markdown", "table", "markdown"])  # its panel, then the auto one
        self.assertIn("n=1", portal[0]["text"])
        self.assertEqual(portal[2]["text"], "auto note")

    def test_no_plugin_on_means_the_view_is_exactly_what_it_says(self):
        self.view(self.root, widgets=[{"type": "markdown", "title": "Own", "text": "mine"}])
        self.assertEqual(self.widgets(self.root), [{"type": "markdown", "title": "Own", "text": "mine"}])

    def test_a_failing_panel_is_one_line_and_counts_against_its_plugin(self):
        from multiverse.plugins import loader
        self.enable("panelsy")
        self.view(self.root, plugin_panels="none", widgets=[{"type": "boom", "title": "Bad"}, {"type": "bad", "title": "Worse"},
                                                           {"type": "markdown", "title": "Fine", "text": "x"}])
        shown = self.widgets(self.root)
        self.assertEqual([w["title"] for w in shown], ["Bad", "Worse", "Fine"])
        self.assertIn("panel boom could not be shown", shown[0]["text"])
        self.assertIn("not str", shown[1]["text"])
        self.assertEqual(sum(len(texts) for texts in loader.errors().values()), 2)

    def test_a_slow_panel_is_cut_off_and_never_holds_up_the_snapshot(self):
        import time
        from multiverse.plugins import loader
        self.enable("panelsy")
        self.view(self.root, plugin_panels="none", widgets=[{"type": "slow", "title": "Slow"}])
        with mock.patch.object(loader, "SYNCHRONOUS_BUDGET", 0.2):
            started = time.monotonic()
            first = self.widgets(self.root)
            again = self.widgets(self.root)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertIn("could not be shown", first[0]["text"])
        self.assertIn("could not be shown", again[0]["text"])

    def test_a_bad_plugin_panels_option_says_so_and_changes_nothing_else(self):
        self.enable("panelsy")
        self.view(self.root, plugin_panels=7, widgets=[{"type": "markdown", "title": "Own", "text": "mine"}])
        shown = self.widgets(self.root)
        self.assertEqual(shown[0]["title"], "Own")
        self.assertIn('plugin_panels must be "auto", "none" or a list', next(w["text"] for w in shown if w["title"] == "View configuration error"))

    def test_two_plugins_with_one_panel_type_need_the_qualified_name(self):
        self.write_plugin("other", {"module": "plugin", "extends": ["dashboard.panel"]},
                          {"plugin.py": "from multiverse.plugins import api\n\ndef register(mv):\n"
                                        "    mv.dashboard.panel('tally', lambda context: api.Markdown('other tally'))\n"})
        self.enable("panelsy")
        self.enable("other")
        self.view(self.root, plugin_panels="none", widgets=[{"type": "tally", "title": "A"}, {"type": "other:tally", "title": "B"}])
        shown = self.widgets(self.root)
        self.assertIn("is offered by other and panelsy; name it as PLUGIN:tally", shown[0]["text"])
        self.assertEqual(shown[1]["text"], "other tally")


class PortalPanelScreenTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_a_portal_draws_its_plugin_panels_with_their_plugin_and_refreshes_them_with_the_fleet(self):
        self.write_plugin("panelsy", {"module": "plugin", "extends": ["dashboard.panel"]},
                          {"plugin.py": "from multiverse.plugins import api\nfrom pathlib import Path\n\ndef register(mv):\n"
                                        "    @mv.dashboard.panel('live')\n    def live(context):\n"
                                        "        return api.Markdown(Path(context.universe.path / 'live.txt').read_text())\n"})
        sub = self.subverse("ops")
        self.enable("panelsy", root=sub)
        (sub / "live.txt").write_text("first")
        write(sub / "views" / "default.json", {"title": "Ops", "columns": ["name"], "plugin_panels": "none",
                                               "widgets": [{"type": "live", "title": "Live"}]})
        app = MultiverseApp(self.root, LocalBackend(), refresh_interval=0, plugin_service=PluginService())
        async with app.run_test(size=(140, 32)) as pilot:
            async def until(check, what):
                for _ in range(300):
                    await app.workers.wait_for_complete()
                    try:
                        if check():
                            return
                    except Exception:
                        pass
                    await pilot.pause(0.02)
                self.fail(f"timed out waiting for {what}")

            def texts():
                return [str(label.render()) for label in app.query_one("#local-view").query(Static)]

            await until(lambda: app.snapshot is not None, "the dashboard")
            app.enter_scope(sub)
            await until(lambda: app.root == sub and any("first" in text for text in texts()), "the plugin panel")
            self.assertIn("Live · panelsy", [str(label.render()) for label in app.query_one("#local-view").query(".view-title")])
            (sub / "live.txt").write_text("second")
            app.refresh_snapshot()
            await until(lambda: any("second" in text for text in texts()), "the refreshed panel")
            self.assertFalse(any("first" in text for text in texts()))


if __name__ == "__main__":
    unittest.main()
