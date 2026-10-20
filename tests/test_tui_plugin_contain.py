"""A plugin screen that fails is closed and the failure named as the plugin's, wherever in the screen it fails."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase

PLUGIN = '''
def register(mv):
    mv.dashboard.screen("compose bomb", "screens:ComposeBomb")
    mv.dashboard.screen("mount bomb", "screens:MountBomb")
    mv.dashboard.screen("fine", "screens:Fine")
    mv.dashboard.screen("key bomb", "screens:KeyBomb")
    mv.dashboard.screen("worker bomb", "screens:WorkerBomb")
    mv.dashboard.screen("handler bomb", "screens:HandlerBomb")
    mv.dashboard.screen("render bomb", "screens:RenderBomb")
    mv.dashboard.screen("timer bomb", "screens:TimerBomb")
    mv.dashboard.screen("markup", "screens:Markup")
    mv.dashboard.screen("bad render", "screens:BadRender")
    mv.dashboard.screen("partial timer", "screens:PartialTimer")
    mv.dashboard.screen("bad css", "screens:BadCss")
'''
SCREENS = '''
import functools
from textual.screen import ModalScreen
from textual.widgets import Static

class ComposeBomb(ModalScreen):
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("half")
        raise RuntimeError("compose exploded")

class MountBomb(ModalScreen):
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("mounted")
    def on_mount(self):
        raise RuntimeError("mount exploded")

class KeyBomb(ModalScreen):
    BINDINGS = [("x", "explode", "Explode")]
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("keys", id="keys")
    def action_explode(self):
        raise RuntimeError("binding exploded")

class WorkerBomb(ModalScreen):
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("worker", id="worker")
    def on_mount(self):
        self.run_worker(self.work(), exit_on_error=True)
    async def work(self):
        raise RuntimeError("worker exploded")

class HandlerBomb(ModalScreen):
    BINDINGS = [("y", "ask", "Ask")]
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("handler", id="handler")
    def action_ask(self):
        self.post_message(Blast())
    def on_blast(self, message):
        raise RuntimeError("handler exploded")

from textual.message import Message
class Blast(Message):
    pass

class Dead(Static):
    def render(self):
        raise RuntimeError("render exploded")

class RenderBomb(ModalScreen):
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("render", id="render")
        yield Dead("dead")

class TimerBomb(ModalScreen):
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("timer", id="timer")
    def on_mount(self):
        self.set_timer(0.05, self.boom)
    def boom(self):
        raise RuntimeError("timer exploded")

class Fine(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen", "Close")]
    def __init__(self, root, backend):
        super().__init__()
    def compose(self):
        yield Static("fine", id="fine")

# Failures in Textual's own code, with no plugin frame in the traceback.
class Markup(Fine):
    def compose(self):
        yield Static("cost [/tmp] usage")      # brackets in plain text: Textual reads them as markup

class Odd(Static):
    def render(self):
        return object()                        # not something Textual can draw

class BadRender(Fine):
    def compose(self):
        yield Odd("odd")

class PartialTimer(Fine):
    def compose(self):
        yield Static("timer")
    def on_mount(self):
        self.set_timer(0.05, functools.partial(int, "not a number"))   # the callback is not plugin code

class BadCss(Fine):
    DEFAULT_CSS = "BadCss Static { colr: red; }"
    def compose(self):
        yield Static("css")
'''


class ContainTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("fragile", {"module": "plugin", "extends": ["dashboard.screen"]},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS})
        self.enable("fragile")
        self.agent("worker")

    async def test_a_failing_plugin_screen_is_closed_and_named(self):
        """In one dashboard session a plugin screen failing in compose, mount, a binding, worker, handler, timer,
        render, markup, a renderable Textual cannot draw, a timer callback of Textual's own or its CSS is closed,
        named in the status with what went wrong and counted against the plugin, while the dashboard and a good
        screen keep working; fragile because attribution must work with no plugin frame in the traceback, relying
        on Textual internals (a5aa597)."""
        cases = (("fragile: compose bomb", (), "compose exploded"),
                 ("fragile: mount bomb", (), "mount exploded"),
                 ("fragile: key bomb", ("x",), "binding exploded"),
                 ("fragile: worker bomb", (), "worker exploded"),
                 ("fragile: handler bomb", ("y",), "handler exploded"),
                 ("fragile: timer bomb", (), "timer exploded"),
                 ("fragile: render bomb", (), "render exploded"),
                 ("fragile: markup", (), "MarkupError"),
                 ("fragile: bad render", (), "render"),
                 ("fragile: partial timer", (), "ValueError"),
                 ("fragile: bad css", (), "colr"))
        async with self.dashboard() as d:
            base = d.screen
            for title, keys, message in cases:
                with self.subTest(title):
                    d.app.set_status("")
                    await d.run_action(title)
                    if keys:
                        await d.press(*keys)
                    await d.until(lambda: d.app.is_running and message in d.status, f"{message} to be reported")
                    await d.until(lambda: d.screen is base, f"{title} to be closed")
                    self.assertTrue(d.status.startswith("fragile: ") and "failed" in d.status, d.status)
                    self.assertNotIn("object at 0x", d.status)
                    self.assertTrue(d.app.is_running)
            await d.refresh()
            self.assertIn(f"{len(cases)} plugin errors", d.header)
            self.assertIsNotNone(await d.run_action("fragile: fine"))  # and the dashboard still opens a good one
            await d.close_screen()

if __name__ == "__main__":
    unittest.main()
