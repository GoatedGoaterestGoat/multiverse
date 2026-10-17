"""A failure in a plugin's own widgets is the plugin's even when no plugin code is in the traceback: Textual
parsing a plugin Static's text, drawing what its render() returned, or running a timer it set to a core function."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

PLUGIN = '''
def register(mv):
    mv.dashboard.screen("markup", "screens:Markup")
    mv.dashboard.screen("bad render", "screens:BadRender")
    mv.dashboard.screen("partial timer", "screens:PartialTimer")
    mv.dashboard.screen("bad css", "screens:BadCss")
    mv.dashboard.screen("fine", "screens:Fine")
    mv.dashboard.panel("markup panel", "screens:MarkupPanel")
    mv.dashboard.panel("later panel", "screens:LaterPanel")
'''
SCREENS = '''
import functools
from textual.screen import ModalScreen
from textual.widgets import Static

class Closable(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen", "Close")]
    def __init__(self, root, backend):
        super().__init__()

class Markup(Closable):
    def compose(self):
        yield Static("cost [/tmp] usage")      # brackets in plain text: Textual reads them as markup

class Odd(Static):
    def render(self):
        return object()                        # not something Textual can draw

class BadRender(Closable):
    def compose(self):
        yield Odd("odd")

class PartialTimer(Closable):
    def compose(self):
        yield Static("timer")
    def on_mount(self):
        self.set_timer(0.05, functools.partial(int, "not a number"))   # the callback is not plugin code

class BadCss(Closable):
    DEFAULT_CSS = "BadCss Static { colr: red; }"
    def compose(self):
        yield Static("css")

class Fine(Closable):
    def compose(self):
        yield Static("fine", id="fine")

ARMED = []

class Later(Static):
    def render(self):
        return "fine for now"
    def go_wrong(self):
        raise RuntimeError("went wrong on the main screen")
    def later(self):
        self.screen.call_later(self.go_wrong)   # run by the main screen's own message loop

class LaterPanel:
    def parse(self, widget, title):
        return {"type": widget["type"], "title": title}
    def compose(self, view, root, widget, index):
        return [Later("later")]

class MarkupPanel:
    def parse(self, widget, title):
        return {"type": widget["type"], "title": title}
    def compose(self, view, root, widget, index):
        return [Static("agent [/scratch] here")]
'''


class OwnerTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("owner", {"module": "plugin", "extends": ["dashboard.screen", "dashboard.panel"]},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS})
        self.enable("owner")
        self.agent("worker")

    async def test_a_plugin_screens_widgets_failing_in_textual_code_are_still_the_plugins(self):
        cases = (("owner: markup", "MarkupError"), ("owner: bad render", "render"), ("owner: partial timer", "ValueError"))
        async with self.dashboard() as d:
            base = d.screen
            for title, what in cases:
                await d.run_action(title)
                await d.until(lambda: d.app.is_running and "owner: its screen or widget failed" in d.status,
                              f"{title} to be reported as the plugin's")
                await d.until(lambda: d.screen is base, f"{title} to be closed")
                self.assertIn(what, d.status)
                self.assertTrue(d.app.is_running)
                d.app.set_status("")
            self.assertIsNotNone(await d.run_action("owner: fine"))
            await d.close_screen()

    async def test_a_plugin_panels_bracketed_text_does_not_end_the_dashboard(self):
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name"], "plugin_panels": "none",
                                                    "widgets": [{"type": "markup panel", "title": "Bracket"},
                                                                {"type": "markdown", "title": "Fine", "text": "still here"}]})
        async with self.dashboard() as d:
            await d.until(lambda: "owner" in d.status and "failed" in d.status, "the panel's failure to be reported")
            await d.until(lambda: "still here" in " ".join(d.local_view()), "the other widget")
            self.assertTrue(d.app.is_running)
            await d.press("j")
            self.assertTrue(d.app.is_running)

    async def test_a_plugin_error_escaping_a_main_screen_handler_does_not_stop_the_main_screen(self):
        import importlib
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name"], "plugin_panels": "none",
                                                    "widgets": [{"type": "later panel", "title": "Later"},
                                                                {"type": "markdown", "title": "Fine", "text": "still here"}]})
        async with self.dashboard() as d:
            await d.until(lambda: "fine for now" in " ".join(d.local_view()), "the plugin's panel")
            screens = importlib.import_module(f"{self.loaded('owner').package}.screens")
            later = d.app.screen.query_one(screens.Later)
            later.later()  # its error escapes a handler of the main screen, as a render error during an update does
            await d.until(lambda: "owner: its screen or widget failed" in d.status, "the failure to be the plugin's")
            main = d.app.screen_stack[0]
            self.assertTrue(main.is_running, "the main screen's message loop ended")
            self.assertFalse(main._closing)
            await d.press("j")
            self.assertTrue(d.app.is_running)
            self.assertNotIn("fine for now", " ".join(d.local_view()))

    async def test_a_plugin_screen_with_invalid_css_says_what_is_wrong(self):
        async with self.dashboard() as d:
            await d.run_action("owner: bad css")
            await d.until(lambda: d.app.is_running and "owner" in d.status, "the CSS failure to be reported")
            self.assertNotIn("object at 0x", d.status)
            self.assertIn("colr", d.status)

    async def test_the_palette_opens_a_plugin_screen_over_another_open_screen(self):
        async with self.dashboard() as d:
            plugins_screen = await d.open_plugins()
            entry = next(callback for name, _, callback in d.app.plugin_palette() if name == "owner: fine")
            entry()
            await d.until(lambda: d.screen is not plugins_screen, "the plugin screen to open over the Plugins screen")
            self.assertTrue(d.screen.query("#fine"))
            await d.close_screen()
            self.assertIs(d.screen, plugins_screen)

    async def test_a_plugin_screen_that_cannot_open_now_says_why(self):
        async with self.dashboard() as d:
            d.app.operating = True  # something else is under way
            entry = next(callback for name, _, callback in d.app.plugin_palette() if name == "owner: fine")
            entry()
            await d.until(lambda: "owner: fine" in d.status, "a line saying why it did not open")
            self.assertFalse(d.screen.query("#fine"))
            d.app.operating = False


if __name__ == "__main__":
    unittest.main()
