"""A plugin's failure is that plugin's alone: it closes only its own screens, and a core bug is never blamed on it."""
import asyncio
import importlib
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Input
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

FRAGILE = '''
def register(mv):
    mv.dashboard.panel("ticker", "panel:Ticker")
'''
FRAGILE_PANEL = '''
from textual.widgets import Static

ARMED = []

def explode():
    raise ValueError("a plugin's own trouble")

class Ticking(Static):
    def on_mount(self):
        self.set_interval(0.05, self.tick)
    def tick(self):
        if ARMED:
            raise RuntimeError("ticker exploded")

class Ticker:
    def parse(self, widget, title):
        return {"type": widget["type"], "title": title}
    def compose(self, view, root, widget, index):
        return [Ticking("tick")]
'''
EDITOR = '''
def register(mv):
    mv.dashboard.screen("notes", "screens:Notes")
'''
EDITOR_SCREENS = '''
from textual.screen import ModalScreen
from textual.widgets import Input, Static

class Notes(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen", "Close")]
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("notes", id="notes")
        yield Input(id="note")
'''


class BlameTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("fragile", {"module": "plugin", "extends": ["dashboard.panel"]},
                          {"plugin.py": FRAGILE, "panel.py": FRAGILE_PANEL})
        self.write_plugin("editor", {"module": "plugin", "extends": ["dashboard.screen"]},
                          {"plugin.py": EDITOR, "screens.py": EDITOR_SCREENS})
        self.enable("fragile")
        self.enable("editor")
        self.agent("worker")
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name"], "plugin_panels": "none",
                                                    "widgets": [{"type": "ticker", "title": "Ticker"},
                                                                {"type": "markdown", "title": "Fine", "text": "still here"}]})

    def panel_module(self):
        return importlib.import_module(f"{self.loaded('fragile').package}.panel")

    async def test_one_plugins_failure_leaves_another_plugins_open_screen_and_its_edit_alone(self):
        async with self.dashboard() as d:
            await d.until(lambda: "tick" in " ".join(d.local_view()), "the ticker panel")
            notes = await d.run_action("editor: notes")
            notes.query_one("#note", Input).value = "half-written"
            self.panel_module().ARMED.append(True)  # now the other plugin's panel fails underneath
            await d.until(lambda: "fragile" in d.status and "failed" in d.status, "fragile's failure to be reported")
            await d.settle()
            self.assertIs(d.screen, notes, "the editor plugin's screen was closed for fragile's failure")
            self.assertEqual(notes.query_one("#note", Input).value, "half-written")
            self.assertNotIn("editor", d.status)
            await d.close_screen()
            await d.until(lambda: "still here" in " ".join(d.local_view()), "the main view")
            self.assertNotIn("tick", d.local_view(), "fragile's dead widget is still on the main screen")
            self.assertTrue(d.app.is_running)

    async def test_a_core_error_raised_while_handling_a_plugins_error_still_ends_the_dashboard(self):
        explode = self.panel_module().explode

        def broken_help(app):
            try:
                explode()
            except ValueError:
                raise KeyError("a bug of the dashboard's own")

        with self.assertRaises(KeyError):
            async with self.dashboard() as d:
                with mock.patch.object(type(d.app), "action_help", broken_help):
                    await d.pilot.press("question_mark")
                    for _ in range(50):
                        if not d.app.is_running:
                            break
                        await asyncio.sleep(0.05)
                self.assertNotIn("its screen or widget failed", d.status)


if __name__ == "__main__":
    unittest.main()
