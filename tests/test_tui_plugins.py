"""Plugins in the dashboard: starting the plugin host, and the settings screen generated for a plugin.

The dashboard talks to plugins through one PluginService; these tests replace it
with a fake, so they need no plugin code, no filesystem and no tmux. FakeService
is also the base of the fakes in GOAT's dashboard tests (plugins/compaction).
"""
import asyncio
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import DataTable, Static
from multiverse.plugins.settings import DELETE
from multiverse.ui.app import MultiverseApp
from multiverse.ui.plugins_screen import PluginSettingsScreen, PluginsScreen, schema_fields
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
    def called(self, name):
        return [call for call in self.service.calls if call[0] == name]

    def open(self, host, start_error=None):
        """A dashboard on the demo agents (not in demo mode, which never starts anything) with this host state."""
        self.backend = LiveDemo()
        self.service = FakeService()
        self.service.host = {"stopped": {}, "workers": [], **host}
        self.service.host_start_error = start_error
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0, plugin_service=self.service)
        return self.app.run_test(size=(100, 32))

    async def open_plugins_screen(self, pilot):
        self.app.action_plugins()
        await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen) and self.app.screen.host,
                         "the Plugins screen")
        return self.app.screen

    async def test_dashboard_host_auto_start_state_machine(self):
        """The dashboard starts the host once when needed, leaves it running on quit, never retries a failed start
        each refresh, never undoes the user's h-stop when a screen closes, and starts nothing when no plugin needs it;
        fragile because check_host runs on every refresh and screen close, and a slip respawns the host in a loop
        nobody watches."""
        with self.subTest("no plugin needs the host"):
            async with self.open({"needed": False, "running": False, "pid": None}) as pilot:
                await self.start(pilot)
                await self.until(pilot, lambda: self.called("host_state"), "the host check")
                self.assertEqual(self.called("start_host"), [])
                self.assertNotIn("host off", self.text("#totals"))
        with self.subTest("started once on open, left running on quit"):
            async with self.open({"needed": True, "running": False, "pid": None}) as pilot:
                await self.start(pilot)
                await self.until(pilot, lambda: self.called("start_host"), "the host to start")
                await self.until(pilot, lambda: not self.app.query_one("#status", Static).display
                                 or "Starting" not in self.text("#status"), "the status line to clear")
                self.assertNotIn("host off", self.text("#totals"))
                self.assertEqual(len(self.called("start_host")), 1)
                await pilot.press("q")
            self.assertEqual(self.called("stop_host"), [])
        with self.subTest("a start that fails is reported once, not retried"):
            async with self.open({"needed": True, "running": False, "pid": None}, start_error="no tmux") as pilot:
                await self.start(pilot)
                await self.until(pilot, lambda: "could not start: no tmux" in self.text("#status"), "the failure")
                await self.until(pilot, lambda: "host off" in self.text("#totals"), "host off in the header")
                self.app.check_host()  # a later check does not try again until something changes
                await self.app.workers.wait_for_complete()
                self.assertEqual(len(self.called("start_host")), 1)
                self.assertTrue(self.app.is_running)
        with self.subTest("stopped with h, not started again when the screen closes"):
            async with self.open({"needed": True, "running": True, "pid": 7, "workers": ["loud"]}) as pilot:
                await self.start(pilot)
                screen = await self.open_plugins_screen(pilot)
                await pilot.press("h")
                await self.until(pilot, lambda: self.called("stop_host") and not screen.busy, "the host to stop")
                await pilot.press("escape")
                await self.until(pilot, lambda: self.app.screen is not screen, "the screen to close")
                await pilot.pause(0.3)
                await self.app.workers.wait_for_complete()
                self.assertEqual(self.called("start_host"), [])
                self.assertIn("host off", self.text("#totals"))
                # h again starts it, and a later check may keep it up.
                await self.open_plugins_screen(pilot)
                await pilot.press("h")
                await self.until(pilot, lambda: self.called("start_host"), "the host to start")


class SettingsScreenTests(DashboardCase):
    def open(self, settings):
        """A dashboard whose alpha plugin has these settings."""
        self.service = FakeService()
        self.service.items[0]["settings"] = settings
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0, plugin_service=self.service)
        return self.app.run_test(size=(100, 40))

    async def open_settings(self, pilot):
        """The alpha plugin's settings screen, opened with Enter on the Plugins screen, and that Plugins screen."""
        if not isinstance(self.app.screen, PluginsScreen):
            await self.start(pilot)
            self.app.action_plugins()
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen)
                             and self.app.screen.query_one("#plugins-table", DataTable).row_count == 2,
                             "the Plugins screen")
        screen = self.app.screen
        await pilot.press("enter")
        await self.until(pilot, lambda: isinstance(self.app.screen, PluginSettingsScreen) and self.app.screen.widgets,
                         "settings")
        return screen, self.app.screen

    async def save(self, pilot, screen):
        """Save with ctrl+enter; the (plugin, changes) passed to edit_settings, or None when nothing was."""
        self.service.calls.clear()
        await pilot.press("ctrl+enter")
        await self.until(pilot, lambda: self.app.screen is screen, "the save to close")
        return next(((call[2], call[3]) for call in self.service.calls if call[0] == "edit"), None)

    async def test_generated_settings_screen_saves_only_edits(self):
        """The generated settings screen passes only real edits to edit_settings: nothing for an unchanged save,
        DELETE for a cleared field or the default option, None for a chosen null, declared fields over the schema;
        a break goes unnoticed because a wrong diff silently rewrites a plugin's settings file."""
        with self.subTest("a field for each scalar property of the schema"):
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
        with self.subTest("edits of a screen from the schema"):
            async with self.open({"path": "/u/plugin-settings/alpha.json", "source": "file", "problem": None,
                                  "schema": SCHEMA, "values": {"greeting": "hello", "retries": 3, "mode": "fast",
                                                              "limits": {"max": 9}}, "screen": None}) as pilot:
                screen, edit = await self.open_settings(pilot)
                self.assertEqual(list(edit.widgets),
                                 ["greeting", "retries", "ratio", "loud", "mode", "note", "limits.max"])
                self.assertEqual((edit.widgets["greeting"].value, edit.widgets["retries"].value,
                                  edit.widgets["limits.max"].value), ("hello", "3", "9"))
                self.assertEqual(edit.widgets["greeting"].placeholder, "hello")
                self.assertIn("tags", str(edit.query(".cwarn").first().render()))
                self.assertIsNone(await self.save(pilot, screen), "an unchanged save edited the settings")
                screen, edit = await self.open_settings(pilot)
                edit.widgets["greeting"].value = ""
                edit.widgets["retries"].value = "5"
                edit.widgets["loud"].action_step(1)
                edit.widgets["limits.max"].value = "12"
                self.assertEqual(await self.save(pilot, screen), ("alpha", {
                    ("greeting",): DELETE, ("retries",): 5, ("loud",): True, ("limits", "max"): 12}))
        with self.subTest("an enum with null"):
            schema = {"type": "object", "properties": {"mode": {"type": ["string", "null"], "enum": ["a", "b", None]}}}
            settings = {"path": "p", "source": "file", "problem": None, "schema": schema, "values": {"mode": "b"},
                        "screen": None}
            async with self.open(settings) as pilot:
                screen, edit = await self.open_settings(pilot)
                choice = edit.widgets["mode"]
                self.assertEqual([label for _, label in choice.options], ["default", "a", "b", "null"])
                self.assertEqual(choice.value, "b")
                choice.action_step(1)  # null
                self.assertEqual(await self.save(pilot, screen), ("alpha", {("mode",): None}))
                settings["values"] = {"mode": None}
                screen.load()
                await pilot.pause(0.3)
                screen, edit = await self.open_settings(pilot)
                self.assertEqual(edit.widgets["mode"].options[edit.widgets["mode"].index][1], "null")
                self.assertIsNone(await self.save(pilot, screen), "an unchanged null was saved again")
        with self.subTest("declared fields over the schema"):
            fields = [{"path": "default.budget", "label": "Budget", "kind": "int", "placeholder": "0",
                       "help": "tokens kept"},
                      {"path": "loud", "label": "Loud", "kind": "bool", "options": (), "placeholder": "", "help": ""},
                      {"path": "mode", "label": "Mode", "kind": "choice", "options": ("fast", "slow"),
                       "placeholder": "fast", "help": ""},
                      {"path": "note", "label": "Note", "kind": "text", "options": (), "placeholder": "", "help": ""}]
            async with self.open({"path": "p", "source": "file", "problem": None, "schema": SCHEMA, "screen": None,
                                  "values": {"default": {"budget": 5}, "mode": "slow"}, "fields": fields}) as pilot:
                screen, edit = await self.open_settings(pilot)
                self.assertEqual(list(edit.widgets), ["default.budget", "loud", "mode", "note"])
                self.assertEqual((edit.widgets["default.budget"].value, edit.widgets["mode"].value), ("5", "slow"))
                self.assertEqual(edit.widgets["default.budget"].placeholder, "0")
                self.assertIn("tokens kept", " ".join(str(w.render()) for w in edit.query(".fhelp")))
                edit.widgets["default.budget"].value = "9"
                edit.widgets["loud"].action_step(1)
                edit.widgets["mode"].action_step(-2)  # back to the default option
                self.assertEqual(await self.save(pilot, screen), ("alpha", {
                    ("default", "budget"): 9, ("loud",): True, ("mode",): DELETE}))


if __name__ == "__main__":
    unittest.main()
