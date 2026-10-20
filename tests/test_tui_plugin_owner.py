"""A plugin panel on the main screen that fails is the plugin's alone: the main screen and the dashboard go on, even
when the error escapes a handler of the main screen or of the app, or no plugin code is in the traceback."""
import importlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write
from multiverse.ui import columns

PLUGIN = '''
def register(mv):
    mv.dashboard.panel("markup panel", "panels:MarkupPanel")
    mv.dashboard.panel("later panel", "panels:LaterPanel")
    mv.dashboard.panel("widget bomb", "panels:BombPanel")
    mv.dashboard.panel("render bomb", "panels:RenderPanel")
'''
PANELS = '''
from textual.widgets import Static

class Panel:
    def parse(self, widget, title):
        return {"type": widget["type"], "title": title}

class MarkupPanel(Panel):
    def compose(self, view, root, widget, index):
        return [Static("agent [/scratch] here")]      # brackets in plain text: Textual reads them as markup

class Later(Static):
    def render(self):
        return "fine for now"
    def go_wrong(self):
        raise RuntimeError("went wrong on the main screen")
    def later(self):
        self.screen.call_later(self.go_wrong)   # run by the main screen's own message loop
    def on_the_app(self):
        self.app.call_later(self.go_wrong)      # run by the app's own message loop

class LaterPanel(Panel):
    def compose(self, view, root, widget, index):
        return [Later("later")]

class Boom(Static):
    def on_mount(self):
        raise RuntimeError("widget exploded")

class BombPanel(Panel):
    def compose(self, view, root, widget, index):
        return [Boom("boom")]

class DeadRender(Static):
    def render(self):
        raise RuntimeError("panel render exploded")

class Evil:
    def __rich_console__(self, console, options):
        raise RuntimeError("renderable exploded")
        yield ""

class RenderPanel(Panel):
    def compose(self, view, root, widget, index):
        return [DeadRender("x"), Static(Evil())]
'''


class OwnerTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("owner", {"module": "plugin", "extends": ["dashboard.panel"]},
                          {"plugin.py": PLUGIN, "panels.py": PANELS})
        self.enable("owner")
        self.agent("worker")

    async def test_a_failing_plugin_panel_never_stops_the_main_screen(self):
        """A main-screen plugin panel that raises in mount or render, shows markup, or lets an error escape the main
        screen's or the app's handler is named while the main screen's loop runs on and j still works; fragile because
        it depends on Textual's private message loops, and a break freezes the main screen or quits the dashboard
        (f842be3, 91c53b5)."""
        rows = (("bracketed text in its widget", "markup panel", None),
                ("an error escaping a main screen handler", "later panel", "later"),  # as a render error in an update
                ("an error escaping an app handler", "later panel", "on_the_app"),  # as one while the app handles one
                ("its widget raising in mount", "widget bomb", None),
                ("its widget failing to render", "render bomb", None))
        for name, kind, trigger in rows:
            with self.subTest(name):
                loader.reset()  # each row's errors are its own
                columns.reset_plugin_caches()
                write(self.root / "views" / "default.json", {
                    "title": "T", "accent": "#89b4fa", "columns": ["name"], "plugin_panels": "none",
                    "widgets": [{"type": kind, "title": "Plugin"},
                                {"type": "markdown", "title": "Fine", "text": "still here"}]})
                async with self.dashboard() as d:
                    if trigger:
                        await d.until(lambda: "fine for now" in " ".join(d.local_view()), "the plugin's panel")
                        panels = importlib.import_module(f"{self.loaded('owner').package}.panels")
                        getattr(d.app.screen.query_one(panels.Later), trigger)()
                    await d.until(lambda: d.status.startswith("owner: ") and "failed" in d.status,
                                  "the failure to be the plugin's")
                    await d.until(lambda: "still here" in " ".join(d.local_view()), "the other widget")
                    await d.settle()
                    self.assertTrue(d.app.is_running, "the app's message loop ended: the dashboard quit")
                    main = d.app.screen_stack[0]
                    self.assertTrue(main.is_running, "the main screen's message loop ended")
                    self.assertFalse(main._closing)
                    await d.press("j")
                    self.assertTrue(d.app.is_running)
                    self.assertNotIn("fine for now", " ".join(d.local_view()))
                    await d.refresh()
                    self.assertIn("plugin error", d.header)

if __name__ == "__main__":
    unittest.main()
