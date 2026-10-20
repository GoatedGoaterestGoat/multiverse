"""A plugin's failure is that plugin's alone: it closes only its own screens, and a core bug is never blamed on it."""
import asyncio
import importlib
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Input
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write
from multiverse.ui import columns

FRAGILE = '''
def register(mv):
    mv.dashboard.panel("ticker", "panel:Ticker")
    mv.dashboard.screen("board", "panel:Board")
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

from textual.screen import ModalScreen

from textual.message import Message

BOARD_ARMED, BOARD_TICKS, BOARD_IN_HANDLER, BOARD_RESUMED, BOARD_WATCHED = [], [], [], [], []

class Probe(ModalScreen):
    def compose(self):
        yield Static("probe", id="probe")

class Blast(Message):
    pass

class Board(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen", "Close")]
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("board", id="board")
    def on_mount(self):
        self.set_interval(0.05, self.tick)
        self.watch(self.app, "title", self.app_title, init=False)  # a watcher on another object, registered by the board
    def app_title(self):
        if BOARD_ARMED:
            BOARD_WATCHED.append("the title watcher")
    def watch_stack_updates(self):  # the screen's own reactive changes whenever the screen above it opens or closes
        if BOARD_ARMED:
            BOARD_WATCHED.append("watch_stack_updates")
    def tick(self):
        BOARD_TICKS.append(1)
        if BOARD_ARMED and BOARD_IN_HANDLER:
            self.post_message(Blast())   # fails in the board's own handler: its message loop ends
        elif BOARD_ARMED:
            raise RuntimeError("board exploded")
    def on_blast(self, message):
        raise RuntimeError("board's handler exploded")
    def on_screen_resume(self):
        if BOARD_ARMED:  # a failed board that still acts when it is shown again
            BOARD_RESUMED.append(1)
            self.app.push_screen(Probe())

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
        self.write_plugin("fragile", {"module": "plugin", "extends": ["dashboard.panel", "dashboard.screen"]},
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

    async def test_a_plugin_failure_closes_only_its_own_screens(self):
        """A plugin failing in a panel under another plugin's screen, in its screen beneath another's or on top of
        one closes only the failing plugin's screens and keeps the other's half-written input, and none of the failed
        plugin's own code runs on a screen of its that is waiting to close (its handlers, its watch_ methods, the
        watchers it registered on the app); fragile because it hooks Textual's private exception, dispatch and
        screen-stack handling, a break loses the user's unsaved edit (3ce2b98), and a watcher runs without any
        message, so only a probe that records it shows it still runs."""
        rows = (("its panel, under another plugin's screen", self.panel_beneath),
                ("its screen, beneath another plugin's", self.failure_beneath),
                ("its screen's own loop ending, beneath another plugin's", self.handler_beneath),
                ("its screen, on top of another plugin's", self.failure_on_top))
        for name, row in rows:
            with self.subTest(name):
                loader.reset()  # each row arms a fresh import of the plugin, and no panel is kept from the last
                columns.reset_plugin_caches()
                async with self.dashboard() as d:
                    await row(d)

    async def panel_beneath(self, d):
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

    async def handler_beneath(self, d):
        self.panel_module().BOARD_IN_HANDLER.append(True)
        await self.failure_beneath(d)

    async def failure_beneath(self, d):
        # The review's stack: the main screen, fragile's board, editor's notes above it with unsaved text.
        board = await d.run_action("fragile: board")
        notes = await d.run_action("editor: notes")
        self.assertEqual([type(s).__name__ for s in d.app.screen_stack[1:]], ["Board", "Notes"])
        notes.query_one("#note", Input).value = "half-written"
        module = self.panel_module()
        module.BOARD_ARMED.append(True)  # fragile's board, underneath, now fails
        await d.until(lambda: "fragile" in d.status and "failed" in d.status, "fragile's failure to be reported")
        await d.settle()
        self.assertIs(d.screen, notes, "the editor plugin's screen above fragile's was closed for fragile's failure")
        self.assertEqual(notes.query_one("#note", Input).value, "half-written")
        ticks = len(module.BOARD_TICKS)
        await asyncio.sleep(0.3)
        self.assertEqual(len(module.BOARD_TICKS), ticks, "the failed board's timer still runs under the notes")
        d.app.title = "changed"  # a watcher the failed board registered on the app
        await d.settle()
        await d.close_screen()  # the person closes the notes: the failed board goes with them, not shown again
        await d.until(lambda: len(d.app.screen_stack) == 1 or module.BOARD_RESUMED, "the failed board to close once it was on top")
        await d.settle()
        self.assertEqual(module.BOARD_RESUMED, [], "the failed board still ran its handler when it was shown again")
        self.assertEqual(module.BOARD_WATCHED, [], "the failed board's watchers still ran")
        self.assertEqual(len(d.app.screen_stack), 1, "the failed board did not close")
        self.assertNotIn(board, d.app.screen_stack)
        self.assertTrue(d.app.is_running)

    async def failure_on_top(self, d):
        notes = await d.run_action("editor: notes")
        notes.query_one("#note", Input).value = "half-written"
        await d.run_action("fragile: board")
        self.panel_module().BOARD_ARMED.append(True)
        await d.until(lambda: "fragile" in d.status and "failed" in d.status, "fragile's failure to be reported")
        await d.until(lambda: d.screen is notes, "fragile's board to close")
        self.assertEqual(notes.query_one("#note", Input).value, "half-written")
        self.assertEqual(len(d.app.screen_stack), 2)
        self.assertEqual(self.panel_module().BOARD_WATCHED, [], "the failed board's watcher ran while it was closed")

    async def test_a_core_error_raised_while_handling_a_plugins_error_still_ends_the_dashboard(self):
        """A core error raised while handling a plugin's error still ends the dashboard; a break goes unnoticed by
        design, since mv2's own bugs would be blamed on a plugin and hidden."""
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
