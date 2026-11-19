"""Plugins in the dashboard: starting the plugin host, and the settings screen generated for a plugin.

The dashboard talks to plugins through one PluginService; these tests replace it
with a fake, so they need no plugin code, no filesystem and no tmux. FakeService
is also the base of the fakes in GOAT's dashboard tests (plugins/compaction).
"""
import asyncio
import contextlib
import os
import signal
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import DataTable, OptionList, Static
from multiverse import plugins
from multiverse.plugins.testing import PluginTestCase
from multiverse.plugins.settings import DELETE
from multiverse.ui.app import MultiverseApp
from multiverse.ui.plugins_screen import PluginSettingsScreen, PluginsScreen, schema_fields
from multiverse.ui.screens import ChoiceScreen
from multiverse.ui.secrets_screen import SecretsScreen
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
        self.packages = {}  # name -> packages_state's answer
        self.secrets = {}  # name -> secrets_view's answer
        self.secret_error, self.secret_gate = None, None
        self.listing_gate, self.listing_held = None, False
        self.install_error, self.install_gate = None, None

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
        found = [dict(entry) for entry in self.items]  # what this read saw, before it is held
        if self.listing_gate and not self.listing_held:  # one read is held, the first once the gate is set up
            self.listing_held = True
            self.listing_gate.wait(10)
        return found

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

    def secrets_stamp(self, root):
        return None

    def secrets_view(self, root, name):
        self.calls.append(("secrets_view", Path(root), name))
        return [dict(secret) for secret in self.secrets.get(name, ())]

    def set_secret(self, root, name, key, value):
        self.calls.append(("set_secret", Path(root), name, key, value))
        if self.secret_gate:
            self.secret_gate.wait(10)
        if self.secret_error:
            raise plugins.PluginError(self.secret_error)
        for secret in self.secrets[name]:
            if secret["key"] == key:
                secret.update({"set": True, "inherited": False, "from": str(root)})
        return "keychain"

    def clear_secret(self, root, name, key):
        self.calls.append(("clear_secret", Path(root), name, key))
        return True

    def packages_state(self, root, name):
        found = self.packages.get(name)
        return dict(found) if found else None

    def install_packages(self, root, name, approved=None):
        self.calls.append(("install", Path(root), name, approved))
        if self.install_gate:
            self.install_gate.wait(10)
        if self.install_error:
            raise plugins.PluginError(self.install_error)
        if approved != Path(self.packages[name]["folder"]).name:  # as core refuses a list changed since it was shown
            raise plugins.PluginError(
                f"{name}'s requirements changed after you were shown them, so nothing was installed")
        self.packages[name] = {**self.packages[name], "installed": True, "problem": None}

    def packages_prompt(self, name, state):
        from multiverse.tools.plugins import packages_prompt
        return packages_prompt(name, state)

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


class PackagesTests(DashboardCase):
    def setUp(self):
        super().setUp()
        missing = {"packages": ["fastrelay[bold red]==1.2.3 --hash=sha256:" + "a" * 64], "installed": False,
                   "folder": "/home/x/.multiverse/plugin-packages/beta-1", "problem": "beta needs Python packages"}
        self.service.packages = {"beta": dict(missing), "alpha": {**missing, "folder": "/f/alpha"}}

    def called(self, name):
        return [call[2] for call in self.service.calls if call[0] in name]

    async def open_screen(self, pilot, name):
        self.app.action_plugins()
        await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen) and self.app.screen.items,
                         "the Plugins screen")
        screen = self.app.screen
        table = screen.query_one("#plugins-table", DataTable)
        table.move_cursor(row=[item["name"] for item in screen.items].index(name))
        await pilot.pause()
        return screen

    async def asked(self, pilot):
        await self.until(pilot, lambda: isinstance(self.app.screen, ChoiceScreen), "the packages question")
        return str(self.app.screen.query_one("#dialog-description", Static).render())

    async def test_a_plugin_with_python_packages_is_asked_about_before_they_install(self):
        """Turning on a plugin whose Python packages aren't installed, or Enter on one that is on without them, shows
        the CLI's words with Install or Cancel, installs only on Install, turns it on only once installed, and holds
        Esc while installing; fragile because packages are code the person must approve, and a slip installs them
        unasked, turns on a plugin that can't load, or loses an install that is still running."""
        async with self.app.run_test(size=(120, 40)) as pilot:
            await self.start(pilot)
            with self.subTest("Cancel installs nothing and leaves it off"):
                screen = await self.open_screen(pilot, "beta")
                self.assertIn("Python packages: 1, not installed (Space asks", str(
                    screen.query_one("#plugin-details", Static).render()))
                await pilot.press("space")
                text = await self.asked(pilot)
                self.assertIn("beta needs 1 Python package, pinned to one version and hashed:", text)
                self.assertIn("fastrelay[bold red]==1.2.3", text)  # the plugin's own words, never markup
                await pilot.press("escape")
                await self.until(pilot, lambda: self.app.screen is screen and not screen.busy, "the question to close")
                self.assertEqual((self.called("install"), self.called("enable")), ([], []))
                self.assertIn("weren't installed; it stays off", str(screen.query_one(".cerror", Static).render()))

            with self.subTest("a failed install says why and leaves it off"):
                self.service.install_error = "beta's packages could not be installed: no index reachable"
                await pilot.press("space")
                await self.asked(pilot)
                await pilot.press("enter")
                await self.until(pilot, lambda: self.app.screen is screen and not screen.busy, "the install to fail")
                self.assertEqual((self.called("install"), self.called("enable")), (["beta"], []))
                note = str(screen.query_one(".cerror", Static).render())
                self.assertIn("could not be installed: no index reachable", note)
                self.assertEqual(note.count("could not be installed"), 1, note)  # core's reason, not said twice
                self.service.install_error = None

            with self.subTest("a row moved to while the list is read again stays selected"):
                table = screen.query_one("#plugins-table", DataTable)
                self.service.install_error = "beta's packages could not be installed: no index reachable"
                await pilot.press("space")
                await self.asked(pilot)
                self.service.listing_gate = gate = threading.Event()
                self.addCleanup(gate.set)
                await pilot.press("enter")  # Install: it fails, and the list is read again, held here
                for _ in range(100):
                    if "could not be installed" in str(screen.query_one(".cerror", Static).render()):
                        break
                    await pilot.pause(0.02)
                await pilot.press("up")  # from beta to alpha, while the list is being read
                gate.set()
                self.service.listing_gate = None
                await self.until(pilot, lambda: not screen.busy, "the list to be read again")
                self.assertEqual(screen.selected()["name"], "alpha")
                self.service.install_error = None
                await pilot.press("down")

            with self.subTest("the question shows the list as it is now, not as the screen first read it"):
                original = dict(self.service.packages["beta"])
                self.service.packages["beta"] = {**original, "packages": ["newrelay==2.0.0 --hash=sha256:" + "b" * 64],
                                                 "folder": "/home/x/.multiverse/plugin-packages/beta-2"}
                await pilot.press("space")
                text = await self.asked(pilot)
                self.assertIn("newrelay==2.0.0", text)
                self.assertNotIn("fastrelay", text)
                await pilot.press("escape")
                await self.until(pilot, lambda: self.app.screen is screen and not screen.busy, "the question to close")

            with self.subTest("a list that changes after the question installs nothing and says so"):
                self.service.calls.clear()
                await pilot.press("space")
                await self.asked(pilot)
                self.service.packages["beta"] = original  # changed between the question and Install
                await pilot.press("enter")
                await self.until(pilot, lambda: self.app.screen is screen and not screen.busy, "the refusal")
                self.assertEqual(self.called("enable"), [])
                self.assertEqual([call[3] for call in self.service.calls if call[0] == "install"], ["beta-2"])
                self.assertIn("changed after you were shown them", str(screen.query_one(".cerror", Static).render()))

            with self.subTest("Install installs, then turns it on"):
                self.service.calls.clear()
                await pilot.press("space")
                await self.asked(pilot)
                await pilot.press("enter")
                await self.until(pilot, lambda: self.called("enable"), "beta to be turned on")
                self.assertEqual([call[0] for call in self.service.calls if call[0] in ("install", "enable")],
                                 ["install", "enable"])
                self.assertEqual([call[3] for call in self.service.calls if call[0] == "install"], ["beta-1"])

            with self.subTest("once installed, Esc at the access question and Space again asks only for access"):
                self.service.calls.clear()
                self.service.packages["gamma"] = {**self.service.packages["alpha"], "folder": "/f/gamma-1"}
                self.service.items.append(item("gamma", on=False, state="off",
                                               access={"request": "act", "minimum": "observe"}))
                screen.load()
                await self.until(pilot, lambda: any(entry["name"] == "gamma" for entry in screen.items), "gamma")
                table = screen.query_one("#plugins-table", DataTable)
                table.move_cursor(row=[entry["name"] for entry in screen.items].index("gamma"))
                await pilot.pause()
                await pilot.press("space")
                await self.asked(pilot)
                await pilot.press("enter")  # Install
                await self.until(pilot, lambda: isinstance(self.app.screen, ChoiceScreen)
                                 and "Turn on gamma" in str(self.app.screen.query_one("#dialog-title").render()),
                                 "the access question")
                await pilot.press("escape")
                await self.until(pilot, lambda: self.app.screen is screen and not screen.busy,
                                 "the access question to close")
                await pilot.press("space")
                await self.until(pilot, lambda: isinstance(self.app.screen, ChoiceScreen), "a question")
                self.assertIn("Turn on gamma", str(self.app.screen.query_one("#dialog-title").render()))
                self.assertEqual(self.called("install"), ["gamma"])  # installed once, never asked again
                await pilot.press("escape")
                await self.until(pilot, lambda: self.app.screen is screen and not screen.busy, "the question to close")

            with self.subTest("Enter on a plugin that is on without its packages installs them, and Esc waits"):
                self.service.calls.clear()
                table = screen.query_one("#plugins-table", DataTable)
                table.move_cursor(row=[item["name"] for item in screen.items].index("alpha"))
                await pilot.pause()
                self.service.install_gate = gate = threading.Event()
                self.addCleanup(gate.set)
                await pilot.press("enter")
                await self.asked(pilot)
                await pilot.press("enter")
                for _ in range(100):  # until() would wait for the held install itself
                    if self.called("install"):
                        break
                    await pilot.pause(0.02)
                await pilot.press("escape")
                self.assertIs(self.app.screen, screen)
                self.assertIn("wait for it to finish", str(screen.query_one(".cerror", Static).render()))
                gate.set()
                await self.until(pilot, lambda: not screen.busy and "Installed alpha" in str(
                    screen.query_one(".cerror", Static).render()), "the install to finish")
                self.assertEqual((self.called("install"), self.called("enable")), (["alpha"], []))  # already on


class SecretsTests(DashboardCase):
    VALUE = "sk-live-0123456789abcdef"

    def setUp(self):
        super().setUp()
        parent = self.backend.root / "parent-universe"
        self.service.items[1] = item("beta", on=False, state="unusable", problem="beta needs its API key")
        self.service.items[0]["settings"] = {"path": "plugin-settings/alpha.json", "values": {}, "schema": {
            "type": "object", "properties": {"greeting": {"type": "string"}}}}
        self.service.secrets = {
            "beta": [{"key": "api_key", "label": "API key", "help": "From the provider's console.", "required": True,
                      "set": False, "from": None, "inherited": False},
                     {"key": "token", "label": "Webhook token", "help": "", "required": False, "set": True,
                      "from": str(parent), "inherited": True},
                     {"key": "region", "label": "Region key", "help": "", "required": False, "set": True,
                      "from": str(self.backend.root), "inherited": False}],
            "alpha": [{"key": "api_key", "label": "API key", "help": "", "required": True, "set": False,
                       "from": None, "inherited": False}]}
        # Textual's own log of every event and message, on for the whole test: no value may reach it.
        import tempfile
        handle, self.log_path = tempfile.mkstemp(prefix="textual-log-", suffix=".txt")
        os.close(handle)
        self.addCleanup(os.unlink, self.log_path)
        import textual.constants
        patcher = mock.patch.object(textual.constants, "LOG_FILE", self.log_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.asked = []
        self.answers = []

        def ask(prompt=""):
            self.asked.append(prompt)
            answer = self.answers.pop(0) if self.answers else ""
            if isinstance(answer, BaseException):
                raise answer
            return answer

        patcher = mock.patch("multiverse.ui.secrets_screen.getpass.getpass", ask)
        patcher.start()
        self.addCleanup(patcher.stop)

    def calls(self, name):
        return [call[2:] for call in self.service.calls if call[0] == name]

    def shown(self):
        """Every piece of text on show, on every screen of the stack."""
        texts = []
        for screen in self.app.screen_stack:
            for widget in screen.query("*"):
                try:
                    texts.append(str(widget.render()))
                except Exception:
                    pass
            for table in screen.query(DataTable):
                texts.extend(str(cell) for row in range(table.row_count) for cell in table.get_row_at(row))
        return "\n".join(texts)

    def logged(self):
        return Path(self.log_path).read_text(errors="replace")

    async def open_secrets(self, pilot, name):
        self.app.action_plugins()
        await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen) and self.app.screen.items,
                         "the Plugins screen")
        plugins_screen = self.app.screen
        table = plugins_screen.query_one("#plugins-table", DataTable)
        table.move_cursor(row=[entry["name"] for entry in plugins_screen.items].index(name))
        await pilot.pause()
        await pilot.press("enter")
        return plugins_screen

    async def pick(self, pilot, screen, key):
        screen.query_one("#secrets-table", DataTable).move_cursor(
            row=[secret["key"] for secret in screen.secrets].index(key))
        await pilot.pause()

    async def test_secrets_are_set_replaced_and_cleared_but_never_pass_through_the_dashboard(self):
        """Enter on a plugin with secrets opens their screen, which says set, inherited or missing, asks for a value
        in the terminal (suspended, hidden typing) and sets it from there, clears after asking, and refuses what it
        can't; fragile because a value typed into the dashboard becomes Textual events its logs record, and a slip
        shows an API key, logs it, or sends it through the settings file."""
        self.app.suspend = contextlib.nullcontext  # the terminal the prompt runs in, as attach does
        async with self.app.run_test(size=(120, 40)) as pilot:
            await self.start(pilot)
            plugins_screen = await self.open_secrets(pilot, "beta")
            await self.until(pilot, lambda: isinstance(self.app.screen, SecretsScreen), "the Secrets screen")
            screen = self.app.screen
            with self.subTest("each secret says whether it is set, and where"):
                text = self.shown()
                self.assertIn("Not set · required", text)
                self.assertIn("Set in parent-universe, used here too", text)
                self.assertIn("From the provider's console.", text)

            with self.subTest("Enter asks in the terminal, once, and sets the value from there"):
                self.answers = [self.VALUE]
                await self.pick(pilot, screen, "api_key")
                await pilot.press("enter")
                await self.until(pilot, lambda: self.calls("set_secret"), "the secret to be set")
                self.assertEqual(self.asked, ["API key: "])  # once, and the prompt holds no value
                self.assertEqual(self.calls("set_secret"), [("beta", "api_key", self.VALUE)])
                self.assertEqual(self.calls("edit"), [])  # never through the settings file
                await self.until(pilot, lambda: "Set here" in self.shown(), "the row to say set")
                self.assertNotIn(self.VALUE, self.shown())  # right away, before anything else is drawn over it
                self.assertNotIn(self.VALUE, self.logged())

            with self.subTest("a terminal that can't hide typing is refused before anything is read"):
                self.service.calls.clear()
                read = []

                def echoing(prompt=""):  # what getpass does where it can't turn echo off: warn, then read echoed
                    import getpass
                    import warnings
                    warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
                    read.append(prompt)
                    return self.VALUE

                with mock.patch("multiverse.ui.secrets_screen.getpass.getpass", echoing):
                    await pilot.press("enter")
                    await self.until(pilot, lambda: "can't hide typing" in str(
                        screen.query_one(".cerror", Static).render()), "the refusal")
                self.assertEqual((read, self.calls("set_secret")), ([], []))
                self.assertIn("run mv2 plugins secret beta api_key", str(screen.query_one(".cerror", Static).render()))

            with self.subTest("Ctrl+C at the prompt comes back, setting nothing"):
                self.service.calls.clear()
                swallowed = []
                # As asyncio's runner does: its handler only cancels the main task, so a read goes on.
                def swallow(*args):
                    swallowed.append(args[0])

                previous = signal.signal(signal.SIGINT, swallow)
                self.addCleanup(signal.signal, signal.SIGINT, previous)

                def interrupted(prompt=""):
                    os.kill(os.getpid(), signal.SIGINT)  # the person presses Ctrl+C while it reads
                    time.sleep(0.05)  # the handler runs here
                    return self.VALUE

                with mock.patch("multiverse.ui.secrets_screen.getpass.getpass", interrupted):
                    await pilot.press("enter")
                    await self.until(pilot, lambda: "nothing set" in str(screen.query_one(".cerror", Static).render()),
                                     "the screen to come back")
                self.assertIs(self.app.screen, screen)
                self.assertEqual((self.calls("set_secret"), swallowed), ([], []))
                self.assertIs(signal.getsignal(signal.SIGINT), swallow)  # the handler before the prompt, restored
                signal.signal(signal.SIGINT, previous)

            with self.subTest("an empty answer sets nothing"):
                self.service.calls.clear()
                self.answers = [""]
                await pilot.press("enter")
                await self.until(pilot, lambda: "nothing set" in str(screen.query_one(".cerror", Static).render()),
                                 "the note")
                self.assertEqual(self.calls("set_secret"), [])

            with self.subTest("a failed set says why"):
                self.service.secret_error = "beta's API key was not set: the keychain is locked"
                self.answers = [self.VALUE]
                await pilot.press("enter")
                await self.until(pilot, lambda: "the keychain is locked" in str(
                    screen.query_one(".cerror", Static).render()), "the reason")
                self.service.secret_error = None

            with self.subTest("an inherited secret can't be cleared here; one set here is, after asking"):
                await self.pick(pilot, screen, "token")
                await pilot.press("ctrl+d")
                self.assertIn("comes from parent-universe: clear it there",
                              str(screen.query_one(".cerror", Static).render()))
                await self.pick(pilot, screen, "region")
                await pilot.press("ctrl+d")
                await self.until(pilot, lambda: isinstance(self.app.screen, ChoiceScreen), "the question")
                await pilot.press("enter")
                await self.until(pilot, lambda: self.calls("clear_secret"), "the clear")
                self.assertEqual(self.calls("clear_secret"), [("beta", "region")])

            with self.subTest("the value is nowhere on screen or in Textual's log"):
                await self.until(pilot, lambda: self.app.screen is screen and not screen.busy, "the clear to end")
                self.assertTrue(self.logged())  # the log was on
                self.assertNotIn(self.VALUE, self.logged())
                self.assertNotIn(self.VALUE, self.shown())

            with self.subTest("Esc closes, and the status line says what changed"):
                await pilot.press("escape")
                await self.until(pilot, lambda: self.app.screen is plugins_screen, "the screen to close")
                await pilot.press("escape")
                await self.until(pilot, lambda: not isinstance(self.app.screen, PluginsScreen), "the Plugins screen")
                self.assertIn("beta: set api_key", str(self.app.query_one("#status", Static).render()))
                self.assertIn("cleared region", str(self.app.query_one("#status", Static).render()))
                self.assertNotIn(self.VALUE, self.logged())

            with self.subTest("a terminal that can't suspend names the command instead"):
                from textual.app import SuspendNotSupported

                def refuse():
                    raise SuspendNotSupported("no terminal")

                self.app.suspend = refuse
                await self.open_secrets(pilot, "beta")
                await self.until(pilot, lambda: isinstance(self.app.screen, SecretsScreen), "the Secrets screen")
                screen = self.app.screen
                self.asked.clear()
                await pilot.press("enter")
                self.assertIn("run mv2 plugins secret beta api_key", str(screen.query_one(".cerror", Static).render()))
                self.assertEqual(self.asked, [])
                self.app.suspend = contextlib.nullcontext
                await pilot.press("escape")
                await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen), "the Plugins screen")
                await pilot.press("escape")

            with self.subTest("a plugin with settings and secrets asks which to open"):
                await self.open_secrets(pilot, "alpha")
                await self.until(pilot, lambda: isinstance(self.app.screen, ChoiceScreen), "the choice")
                options = self.app.screen.query_one("#choices", OptionList)
                self.assertEqual([str(options.get_option_at_index(i).prompt) for i in range(options.option_count)],
                                 ["Settings", "Secrets (1, 1 required not set)"])
                await pilot.press("down", "enter")
                await self.until(pilot, lambda: isinstance(self.app.screen, SecretsScreen), "the Secrets screen")


KEEPER = {"api": "1.20", "module": "plugin", "access": {"request": "observe"},
          "secrets": {"api_key": {"label": "API key", "required": True}}}


class OverlappingReadsTests(DashboardCase):
    async def test_an_older_list_read_finishing_last_never_draws_over_a_newer_one(self):
        """When list reads overlap, only the latest started shows what it read; fragile because a slow read that
        finishes last would put back a state already gone (a plugin shown usable after its key was cleared), and the
        follow tick, having seen the change, would not read again."""
        async with self.app.run_test(size=(120, 40)) as pilot:
            await self.start(pilot)
            self.app.action_plugins()
            await self.until(pilot, lambda: isinstance(self.app.screen, PluginsScreen) and self.app.screen.items,
                             "the Plugins screen")
            screen = self.app.screen
            self.service.listing_gate = threading.Event()
            self.addCleanup(self.service.listing_gate.set)
            screen.load()  # the older read: it saw alpha on, and is held
            for _ in range(50):
                if self.service.listing_held:
                    break
                await pilot.pause(0.02)
            self.service.items[0] = {**self.service.items[0], "state": "unusable"}  # changed meanwhile
            screen.load()  # the newer read sees it
            for _ in range(100):
                if screen.items and screen.items[0]["state"] == "unusable":
                    break
                await pilot.pause(0.02)
            self.service.listing_gate.set()  # the older read now finishes, last
            await self.until(pilot, lambda: True, "the reads to finish")
            self.assertEqual([item["state"] for item in screen.items][0], "unusable")
            table = screen.query_one("#plugins-table", DataTable)
            self.assertEqual(str(table.get_row("alpha")[2]), "unusable")


class OutsideSecretTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_the_plugins_screen_follows_a_secret_set_or_cleared_by_another_terminal(self):
        """The Plugins screen shows a plugin unusable as soon as another terminal clears its required secret, in a
        subverse reading its parent's too, and usable again once it is set; fragile because the screen's list is
        cached in this process, which never hears of another's change, and it would show a plugin working that
        can't (BUG-DASHBOARD-016)."""
        from multiverse.plugins import secrets
        self.write_plugin("keeper", KEEPER, {"plugin.py": "def register(mv):\n    pass\n"})
        self.enable("keeper")
        sub = self.subverse("ops")
        self.enable("keeper", root=sub)
        secrets.set_secret(self.root, "keeper", "api_key", "sk-test-not-shown")  # past this process's cache

        def state(screen):
            table = screen.query_one("#plugins-table", DataTable)
            return str(table.get_row("keeper")[2])

        async with self.dashboard(size=(140, 40)) as d:
            screen = await d.open_plugins()
            self.assertEqual(state(screen), "on")
            with self.subTest("cleared elsewhere while the screen is open"):
                secrets.clear_secret(self.root, "keeper", "api_key")  # as `mv2 plugins secret --clear` would
                await d.until(lambda: state(screen) == "unusable", "the screen to follow the clear")
            with self.subTest("set again elsewhere"):
                self.assertEqual(state(screen), "unusable")  # from the clear: the set has to be seen, not stayed
                secrets.set_secret(self.root, "keeper", "api_key", "sk-test-not-shown")
                await d.until(lambda: state(screen) == "on", "the screen to follow the set")
            await d.close_screen()
            with self.subTest("reopened in a subverse reading its parent's, after a clear"):
                secrets.clear_secret(self.root, "keeper", "api_key")
                d.select("ops")
                screen = await d.open_plugins()
                await d.until(lambda: state(screen) == "unusable", "the subverse's screen")


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
                # A selector keeps the field screen's look; app.tcss once restyled it through a screen named Choice.
                self.assertEqual(edit.widgets["loud"].styles.background, edit.widgets["ratio"].styles.background)
                self.assertEqual(edit.widgets["loud"].styles.align_horizontal, "left")
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


GRANTED = """
def register(mv):
    pass
"""


class GrantPromptTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_turning_on_a_plugin_that_can_do_with_less_asks_which_access_to_grant(self):
        """Space on a plugin that can work with less than it asks for asks which access to grant and records the
        one chosen, Esc records nothing, a plugin with nothing to choose turns on at once, and Space twice in one turn
        opens one chooser; fragile because the grant is a person's only say over a plugin's reach, and a prompt that
        is skipped, stacked or answered for them records a level nobody chose."""
        self.write_plugin("watcher", {"module": "plugin", "api": "1.13",
                                      "access": {"request": "act", "minimum": "observe",
                                                 "why": "observe shows the plan; act installs it"}},
                          {"plugin.py": GRANTED})
        self.write_plugin("plain", {"module": "plugin"}, {"plugin.py": GRANTED})  # no access: act, nothing to choose

        def found(name):
            return next(item for item in plugins.listing(self.root) if item["name"] == name)

        async with self.dashboard() as d:
            shown = await d.open_plugins()

            async def select(name):
                table = shown.query_one("#plugins-table", DataTable)
                await d.until(lambda: name in [item["name"] for item in shown.items], f"{name} in the list")
                table.move_cursor(row=[item["name"] for item in shown.items].index(name), animate=False)
                await d.settle()

            with self.subTest("Space twice in one turn opens one chooser, and Esc records nothing"):
                await select("watcher")
                shown.action_toggle()
                shown.action_toggle()
                await d.until(lambda: isinstance(d.screen, ChoiceScreen) and d.screen.query("#choices"), "the access chooser")
                self.assertEqual(sum(isinstance(screen, ChoiceScreen) for screen in d.app.screen_stack), 1)
                choices = d.screen.query_one("#choices", OptionList)
                self.assertEqual([choices.get_option_at_index(index).id for index in range(choices.option_count)],
                                 ["act", "spend", "observe"])
                self.assertIn("observe shows the plan", str(d.screen.query_one("#dialog-description").render()))
                await d.press("escape")
                await d.until(lambda: d.screen is shown and not shown.busy, "the Plugins screen again")
                self.assertFalse(found("watcher")["on"])
                self.assertIsNone(found("watcher")["access"]["granted"])
            with self.subTest("the level chosen is the one recorded"):
                await d.press("space")
                await d.until(lambda: isinstance(d.screen, ChoiceScreen) and d.screen.query("#choices"), "the access chooser")
                await d.press("down", "enter")
                await d.until(lambda: found("watcher")["on"] and not shown.busy, "watcher to be on")
                self.assertEqual((found("watcher")["access"]["granted"], found("watcher")["access"]["level"]),
                                 ("spend", "spend"))
                await d.until(lambda: "Access here: spend (it asks for act" in str(
                    shown.query_one("#plugin-details").render()), "the access line")
            with self.subTest("a plugin with nothing to choose turns on at once"):
                await select("plain")
                await d.press("space")
                await d.until(lambda: found("plain")["on"] and not shown.busy, "plain to be on")
                self.assertIs(d.screen, shown)
                self.assertIsNone(found("plain")["access"]["granted"])


class GrantedPermissionTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_the_plugins_screen_says_what_its_access_allows_and_what_it_holds_back(self):
        """The Plugins screen lists under 'Allowed to' only what a plugin's access level here permits, and the rest
        as held back with the level each needs, at every level, and only what it asks for while it is off; fragile
        because the permissions a plugin asks for read as allowed when a lowered grant holds them back, telling the
        person a plugin can spawn agents and send messages when it can't (qa-dashboard, PR A)."""
        self.write_plugin("watcher", {"module": "plugin", "api": "1.13",
                                      "access": {"request": "act", "minimum": "observe"}}, {"plugin.py": GRANTED})

        async with self.dashboard() as d:
            async def details():
                shown = await d.open_plugins()
                await d.until(lambda: "watcher" in [item["name"] for item in shown.items], "watcher in the list")
                shown.query_one("#plugins-table", DataTable).move_cursor(
                    row=[item["name"] for item in shown.items].index("watcher"), animate=False)
                await d.until(lambda: str(shown.query_one("#plugin-details").render()).startswith("watcher"),
                              "watcher's details")
                text = " ".join(str(shown.query_one("#plugin-details").render()).split())
                await d.close_screen()
                return text

            with self.subTest("off: nothing is allowed yet, so it says what it asks for"):
                text = await details()
                self.assertIn("read this universe's agents", text.partition("Asks to:")[2])
                self.assertIn("send messages to any agent", text.partition("Asks to:")[2])
                self.assertNotIn("Allowed to", text)
            for level, allowed, held in (
                    ("observe", ["read this universe's agents"],
                     ["ask agents side questions (needs spend)", "send messages to any agent in this universe, as this "
                      "plugin (needs act)", "spawn, resume and kill agents in this universe, and run their harness's "
                      "commands (needs act)"]),
                    ("spend", ["read this universe's agents", "ask agents side questions"],
                     ["send messages to any agent in this universe, as this plugin (needs act)"]),
                    ("act", ["read this universe's agents", "ask agents side questions", "send messages to any agent"],
                     [])):
                with self.subTest(granted=level):
                    if plugins.entry(self.root, "watcher").on:
                        plugins.set_grant(self.root, "watcher", level)
                    else:
                        plugins.set_enabled(self.root, "watcher", True, grant=level)
                    text = await details()
                    self.assertNotIn("change it with", text)  # the old, generic advice
                    if level == "observe":
                        self.assertIn("(a person can grant it with: mv2 plugins grant watcher act)", text)
                    allowed_part, _, held_part = text.partition("Held back by its")
                    for words in allowed:
                        self.assertIn(words, allowed_part.partition("Allowed to:")[2])
                    for words in held:
                        self.assertIn(words, held_part)
                    if held:
                        self.assertIn(f"Held back by its {level} access (", text)
                        self.assertNotIn(held[0].rpartition(" (needs")[0], allowed_part)
                    else:
                        self.assertIn("Access here: act (it asks for act, at least observe)", text)
                        self.assertNotIn("Held back", text)
            # Granted act but capped where a grant can't help: the line names the cap, and the advice how to lift it.
            for cap, environment, settings, says, lift in (
                    ("access_ceiling", {}, {"access_ceiling": "observe"},
                     "this universe's access_ceiling allows at most observe",
                     "this universe's plugins.json caps every plugin at observe (access_ceiling)"),
                    ("MV2_EFFECTS", {"MV2_EFFECTS": "observe"}, {},
                     "MV2_EFFECTS caps every plugin in this process at observe",
                     "MV2_EFFECTS=observe caps every plugin in this process: run it without MV2_EFFECTS")):
                with self.subTest(capped_by=cap), mock.patch.dict(os.environ, environment):
                    (self.root / "plugins.json").write_text(json.dumps(
                        {"schema": 1, "enabled": {"watcher": True}, "access": {"watcher": "act"}, **settings}))
                    text = await details()
                    self.assertIn("Access here: observe (it asks for act, at least observe); " + says, text)
                    self.assertIn(f"Held back by its observe access ({lift}", text)
                    self.assertNotIn("mv2 plugins grant", text)
                    if cap == "MV2_EFFECTS":
                        self.assertNotIn("access_ceiling", text)  # the process's cap is not the universe's


class FollowingTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_an_open_plugins_screen_follows_plugins_json_changed_elsewhere(self):
        """An open Plugins screen shows a grant or an access_ceiling changed from outside (mv2 plugins grant, an edit)
        on its next tick, without being closed, and never while a chooser is open on top of it; fragile because the
        screen read plugins.json once, so it kept saying a level the plugin no longer has until it was reopened."""
        for name in ("watcher", "chooser"):
            self.write_plugin(name, {"module": "plugin", "api": "1.13", "access": {"request": "act", "minimum": "observe"}},
                              {"plugin.py": GRANTED})
        plugins.set_enabled(self.root, "watcher", True, grant="observe")
        async with self.dashboard() as d:
            shown = await d.open_plugins()

            def details():
                return " ".join(str(shown.query_one("#plugin-details").render()).split())

            async def select(name):
                await d.until(lambda: name in [item["name"] for item in shown.items], f"{name} in the list")
                shown.query_one("#plugins-table", DataTable).move_cursor(
                    row=[item["name"] for item in shown.items].index(name), animate=False)
                await d.until(lambda: details().startswith(name), f"{name}'s details")

            await select("watcher")
            self.assertIn("Access here: observe", details())
            with self.subTest("a grant changed from outside"):
                plugins.set_grant(self.root, "watcher", "spend")
                await d.until(lambda: "Access here: spend" in details(), "the new grant on the open screen")
            with self.subTest("an access_ceiling changed from outside"):
                data = json.loads((self.root / "plugins.json").read_text())
                (self.root / "plugins.json").write_text(json.dumps({**data, "access_ceiling": "observe"}))
                await d.until(lambda: "access_ceiling allows at most observe" in details(), "the ceiling on the open screen")
            with self.subTest("a chooser open on top is left alone, and the screen catches up after it"):
                (self.root / "plugins.json").write_text(json.dumps(data))  # no ceiling again
                await d.until(lambda: "access_ceiling" not in details(), "the ceiling gone")
                await select("chooser")
                await d.press("space")
                await d.until(lambda: isinstance(d.screen, ChoiceScreen) and d.screen.query("#choices"), "the chooser")
                chooser = d.screen
                plugins.set_grant(self.root, "watcher", "act")
                await asyncio.sleep(1.5)  # more than one tick of the screen's follow
                await d.settle()
                self.assertIs(d.screen, chooser)
                self.assertTrue(shown.busy)  # the toggle it opened for still waits for the person's answer
                await d.press("escape")
                await select("watcher")
                await d.until(lambda: "Access here: act" in details(), "the change made while the chooser was open")


NOTED = """def register(mv):
    @mv.settings.check
    def check(values):
        if values.get("loud"):
            return ["loud: ignored, this plugin has no such setting"]
"""


class SettingsNotesTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_the_plugins_screen_shows_the_notes_a_plugins_settings_check_returns(self):
        """A note a plugin's settings check returns (1.15) shows on the Plugins screen, under the settings line and on
        its settings form, and leaves the settings usable; the screen names it once, under the plugin's settings while
        that plugin is selected and among the universe's problems otherwise; fragile and unnoticed because a note is
        advice, so the settings keep working without it while the setting it names (an unknown key compaction ignores)
        silently does nothing, and a note named twice reads as two problems."""
        self.write_plugin("noted", {"module": "plugin", "settings": {
            "version": 1, "schema": "schema.json", "defaults": "defaults.json"}}, {
            "plugin.py": NOTED, "schema.json": json.dumps({"type": "object", "properties": {
                "greeting": {"type": "string"}, "loud": {"type": "boolean"}}}),
            "defaults.json": json.dumps({"schema": 1, "greeting": "hello"})})
        plugins.set_enabled(self.root, "noted", True)
        self.write_plugin("plain", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"})
        plugins.set_enabled(self.root, "plain", True)
        note = "Note: loud: ignored, this plugin has no such setting"
        said = note.removeprefix("Note: ")
        async with self.dashboard() as d:
            for written, says in (({"schema": 1, "greeting": "hello"}, False),
                                  ({"schema": 1, "greeting": "hello", "loud": True}, True)):
                with self.subTest(note=says):
                    plugins.settings_of(self.root, "noted")[2].write_text(json.dumps(written))
                    shown = await d.open_plugins()
                    await d.until(lambda: "noted" in [item["name"] for item in shown.items], "noted in the list")
                    shown.query_one("#plugins-table", DataTable).move_cursor(
                        row=[item["name"] for item in shown.items].index("noted"), animate=False)
                    await d.until(lambda: str(shown.query_one("#plugin-details").render()).startswith("noted"),
                                  "noted's details")
                    lines = str(shown.query_one("#plugin-details").render()).splitlines()
                    settings_line = next(i for i, line in enumerate(lines) if line.startswith("Settings: "))
                    self.assertNotIn("(unusable)", lines[settings_line])
                    if says:
                        self.assertEqual(lines[settings_line + 1:settings_line + 2], [note])  # right under it
                        await d.until(lambda: any(said in problem for problem in shown.problems), "the universe's problems")
                        details = lambda: " ".join(str(shown.query_one("#plugin-details").render()).split())
                        self.assertEqual(details().count(said), 1, details())  # not again as a problem below
                        shown.query_one("#plugins-table", DataTable).move_cursor(
                            row=[item["name"] for item in shown.items].index("plain"), animate=False)
                        await d.until(lambda: details().startswith("plain"), "plain's details")
                        self.assertEqual(details().count(said), 1, details())  # another plugin selected: still named
                        shown.query_one("#plugins-table", DataTable).move_cursor(
                            row=[item["name"] for item in shown.items].index("noted"), animate=False)
                        await d.until(lambda: details().startswith("noted"), "noted's details again")
                    else:
                        self.assertFalse(any(line.startswith("Note:") for line in lines))
                    await d.press("enter")
                    await d.until(lambda: isinstance(d.screen, PluginSettingsScreen), "the settings form")
                    form = d.screen
                    self.assertFalse(form.query_one("#plugin-settings-problem").display)  # still usable
                    notes = form.query_one("#plugin-settings-notes")
                    self.assertEqual((notes.display, str(notes.render())), (says, note if says else ""))
                    await d.close_screen()
                    await d.close_screen()


if __name__ == "__main__":
    unittest.main()
