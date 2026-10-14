"""Plugin-supplied text is shown literally: a plugin's "[/x]" is text, never Rich markup, in every slot it can reach."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

BAD = "[/x]"
PLUGIN = '''
from multiverse.plugins import api

def register(mv):
    mv.dashboard.column("shout", "Disk [/x]", 12, lambda row: "val [/x] [bold]", compact=True, group=lambda row: "grp [/x]")
    mv.dashboard.action("Clean [/x]", lambda context: "done [/x]")
    mv.dashboard.screen("board [/x]", "screens:Board")
    mv.dashboard.settings(fields=[api.Field("a", "Label [/x]", "text", placeholder="ph [/x]", help="help [/x]"),
                                  api.Field("m", "Mode [/x]", "choice", options=("one [/x]", "two"))])

    @mv.dashboard.inspector
    def fact(row):
        return "fact [/x] [bold]x"

    @mv.dashboard.panel("note", title="Title [/x]", auto=True)
    def note(context):
        return [api.Markdown("md [/x]"), api.Table(("c [/x]",), (("cell [/x]",),))]
'''
SCREENS = '''
from multiverse.plugins.ui import FieldScreen
from textual.widgets import Static

class Board(FieldScreen):
    def compose(self):
        yield Static("board", id="board")
        yield Static("", classes="cerror")
'''


class PluginTextTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("texty", {"module": "plugin", "version": "1.0 [/x]", "description": "does [/x] things",
                                    "settings": {"version": 1}, "extends": [
                                        "dashboard.column", "dashboard.action", "dashboard.screen", "dashboard.settings",
                                        "dashboard.inspector", "dashboard.panel"]},
                          {"plugin.py": PLUGIN, "screens.py": SCREENS})
        self.enable("texty")
        self.agent("worker")
        write(self.root / "views" / "default.json",
              {"title": "T", "accent": "#89b4fa", "columns": ["name", "texty:shout", "ops:[/x]"]})

    async def test_the_table_the_palette_the_plugins_screen_and_its_settings_show_plugin_text_literally(self):
        async with self.dashboard() as d:
            headers, rows = d.table()
            self.assertIn("Disk [/x]", headers)
            self.assertIn("[/x]", " ".join(headers))  # the missing column's title too
            self.assertIn("val [/x] [bold]", [cell for row in rows for cell in row])
            self.assertIn("grp [/x]", [cell for row in rows for cell in row])
            self.assertIn("texty: Clean [/x]", d.palette())
            plugins = await d.open_plugins()
            row = plugins.query_one("#plugins-table").get_row_at(0)
            self.assertEqual([getattr(cell, "plain", cell) for cell in row][:4], ["texty", "on", "on", "1.0 [/x]"])
            self.assertIn("does [/x] things", str(plugins.query_one("#plugin-details").render()))
            screen = await d.open_settings("texty")
            self.assertEqual(screen.widgets["a"].placeholder, "ph [/x]")
            await d.close_screen()
            await d.close_screen()

    async def test_the_palette_itself_opens_and_filters_with_plugin_text(self):
        from textual.command import CommandPalette
        async with self.dashboard() as d:
            await d.press("ctrl+p")
            await d.until(lambda: isinstance(d.screen, CommandPalette), "the command palette")
            await d.press("C", "l", "e", "a", "n")
            await d.until(lambda: d.screen.query_one("OptionList").option_count >= 1, "a match")
            self.assertTrue(d.app.is_running)

    async def test_the_inspector_and_the_portal_show_plugin_text_literally(self):
        async with self.dashboard() as d:
            d.select("worker")
            await d.press("p")
            await d.until(lambda: "fact [/x] [bold]x" in str(d.app.query_one("#inspect-plugins").render()), "the plugin line")
            texts = d.local_view()
            self.assertIn("md [/x]", texts)
            self.assertIn("Title [/x] · texty", [text for text in texts if "Title" in text] or [""])
            self.assertTrue(d.app.is_running)

    async def test_the_header_and_status_show_a_plugin_actions_text_literally(self):
        async with self.dashboard() as d:
            d.select("worker")
            await d.run_action("texty: Clean [/x]")
            await d.until(lambda: "done [/x]" in d.status, "the status line")


if __name__ == "__main__":
    unittest.main()
