"""A plugin's failure inside the dashboard stays the plugin's: a screen or widget that raises, palette search, names in messages."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.command import CommandPalette
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

PLUGIN = '''
from multiverse.plugins import api

def register(mv):
    mv.dashboard.action("Clean [/tmp]", lambda context: "cleaned")
    mv.dashboard.action("Cleaner [bold]x", lambda context: "cleaner")
    mv.dashboard.screen("compose bomb", "screens:ComposeBomb")
    mv.dashboard.screen("mount bomb", "screens:MountBomb")
    mv.dashboard.screen("fine", "screens:Fine")
    mv.dashboard.screen("key bomb", "screens:KeyBomb")
    mv.dashboard.screen("worker bomb", "screens:WorkerBomb")
    mv.dashboard.screen("handler bomb", "screens:HandlerBomb")
    mv.dashboard.screen("render bomb", "screens:RenderBomb")
    mv.dashboard.screen("timer bomb", "screens:TimerBomb")
    mv.dashboard.panel("widget bomb", "screens:BombPanel")
    mv.dashboard.panel("render bomb", "screens:RenderPanel")
'''
SCREENS = '''
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

class Boom(Static):
    def on_mount(self):
        raise RuntimeError("widget exploded")

class DeadRender(Static):
    def render(self):
        raise RuntimeError("panel render exploded")

class Evil:
    def __rich_console__(self, console, options):
        raise RuntimeError("renderable exploded")
        yield ""

class RenderPanel:
    def parse(self, widget, title):
        return {"type": widget["type"], "title": title}
    def compose(self, view, root, widget, index):
        return [DeadRender("x"), Static(Evil())]

class BombPanel:
    def parse(self, widget, title):
        return {"type": widget["type"], "title": title}
    def compose(self, view, root, widget, index):
        return [Boom("boom")]
'''


class ContainTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("fragile", {"module": "plugin", "extends": ["dashboard.action", "dashboard.screen", "dashboard.panel"]},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS})
        self.enable("fragile")
        self.agent("worker")

    async def test_a_plugin_screen_that_raises_while_composing_or_mounting_is_closed_and_reported(self):
        async with self.dashboard() as d:
            base = d.screen
            for title in ("fragile: compose bomb", "fragile: mount bomb"):
                await d.run_action(title)
                await d.until(lambda: d.screen is base and "fragile" in d.status and "failed" in d.status, f"{title} to be contained")
                self.assertTrue(d.app.is_running)
            await d.refresh()
            self.assertIn("plugin error", d.header)
            self.assertIsNotNone(await d.run_action("fragile: fine"))  # and the dashboard still opens a good one
            await d.close_screen()

    async def test_exceptions_in_a_plugin_screens_bindings_workers_handlers_timers_and_render_are_contained(self):
        cases = (("fragile: key bomb", ("x",), "binding exploded"),
                 ("fragile: worker bomb", (), "worker exploded"),
                 ("fragile: handler bomb", ("y",), "handler exploded"),
                 ("fragile: timer bomb", (), "timer exploded"),
                 ("fragile: render bomb", (), "render exploded"))
        async with self.dashboard() as d:
            base = d.screen
            for title, keys, message in cases:
                await d.run_action(title)
                if keys:
                    await d.press(*keys)
                await d.until(lambda: d.app.is_running and message in d.status, f"{message} to be reported")
                await d.until(lambda: d.screen is base, f"{title} to be closed")
                self.assertIn("its screen or widget failed", d.status)
                self.assertTrue(d.app.is_running)
            await d.refresh()
            self.assertIn("5 plugin errors", d.header)
            self.assertIsNotNone(await d.run_action("fragile: fine"))
            await d.close_screen()

    async def test_a_widget_of_a_class_form_panel_that_raises_does_not_end_the_dashboard(self):
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name"], "plugin_panels": "none",
                                                    "widgets": [{"type": "widget bomb", "title": "Bomb"},
                                                                {"type": "markdown", "title": "Fine", "text": "still here"}]})
        async with self.dashboard() as d:
            await d.until(lambda: "fragile" in d.status and "failed" in d.status, "the widget's failure to be reported")
            self.assertTrue(d.app.is_running)
            self.assertIn("still here", d.local_view())
            await d.press("j")
            self.assertTrue(d.app.is_running)

    async def test_a_class_form_panel_whose_widget_fails_to_render_does_not_end_the_dashboard(self):
        write(self.root / "views" / "default.json", {"title": "T", "accent": "#89b4fa", "columns": ["name"], "plugin_panels": "none",
                                                    "widgets": [{"type": "render bomb", "title": "Bomb"},
                                                                {"type": "markdown", "title": "Fine", "text": "still here too"}]})
        async with self.dashboard() as d:
            await d.until(lambda: "fragile" in d.status and "failed" in d.status, "the render failure to be reported")
            await d.until(lambda: "still here too" in d.local_view(), "the other widget")
            self.assertTrue(d.app.is_running)
            await d.press("j")
            self.assertTrue(d.app.is_running)
            await d.refresh()
            self.assertIn("plugin error", d.header)

    async def test_palette_search_keeps_plugin_titles_literal_and_every_match_listed(self):
        async with self.dashboard() as d:
            await d.press("ctrl+p")
            await d.until(lambda: isinstance(d.screen, CommandPalette), "the command palette")
            await d.press("c", "l", "e", "a", "n")

            def titles():
                options = d.screen.query_one("OptionList")
                return [option.prompt.plain if hasattr(option.prompt, "plain") else str(option.prompt)
                        for option in (options.get_option_at_index(i) for i in range(options.option_count))]

            await d.until(lambda: any("Clean [/tmp]" in text for text in titles()), "the first plugin entry")
            shown = " | ".join(titles())
            self.assertIn("Cleaner [bold]x", shown)  # the later entry is still there, and unstyled text
            self.assertNotIn("No matches", shown)

    async def test_a_plugin_that_is_off_in_the_scope_is_named_by_the_universe_not_its_hidden_directory(self):
        sub = self.subverse("ops")
        self.enable("fragile", on=False)
        self.enable("fragile", root=sub)
        async with self.dashboard() as d:
            d.select("worker")
            await d.run_action("fragile: Clean [/tmp]")
            await d.until(lambda: "off in" in d.status, "the off message")
            self.assertIn("is off in test", d.status)  # the universe's name, not '.multiverse'
            self.assertNotIn(".multiverse", d.status)


if __name__ == "__main__":
    unittest.main()
