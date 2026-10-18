"""Plugin-supplied text is shown literally: a plugin's "[/x]" is text, never Rich markup, in every slot it can reach."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.command import CommandPalette
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

BAD = "[/x]"
PLUGIN = '''
from multiverse.plugins import api

def register(mv):
    mv.dashboard.column("shout", "Disk [/x]", 12, lambda row: "val [/x] [bold]", compact=True, group=lambda row: "grp [/x]")
    mv.dashboard.action("Clean [/x]", lambda context: "done [/x]")
    mv.dashboard.action("Cleaner [bold]x", lambda context: "cleaner")
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

    async def test_plugin_text_is_never_parsed_as_markup(self):
        """[/x] in plugin column titles and cells, palette entries and search, the Plugins screen, settings, the
        inspector, panels and the status line stays literal; fragile because every new UI slot must opt out of markup,
        and a plugin showing a path like [/tmp] raises MarkupError and ends the dashboard."""
        async with self.dashboard() as d:
            with self.subTest("the table, the palette, the Plugins screen and its settings"):
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
            with self.subTest("palette search"):
                await d.press("ctrl+p")
                await d.until(lambda: isinstance(d.screen, CommandPalette), "the command palette")
                await d.press("C", "l", "e", "a", "n")

                def titles():
                    options = d.screen.query_one("OptionList")
                    return [option.prompt.plain if hasattr(option.prompt, "plain") else str(option.prompt)
                            for option in (options.get_option_at_index(i) for i in range(options.option_count))]

                await d.until(lambda: any("Clean [/x]" in text for text in titles()), "the first plugin entry")
                shown = " | ".join(titles())
                self.assertIn("Cleaner [bold]x", shown)  # the later entry is still there, and unstyled text
                self.assertNotIn("No matches", shown)
                self.assertTrue(d.app.is_running)
                await d.close_screen()
            with self.subTest("an action's status line"):
                d.select("worker")
                await d.run_action("texty: Clean [/x]")
                await d.until(lambda: "done [/x]" in d.status, "the status line")
            with self.subTest("the inspector and the panels"):
                d.select("worker")
                await d.press("p")
                await d.until(lambda: "fact [/x] [bold]x" in str(d.app.query_one("#inspect-plugins").render()),
                              "the plugin line")
                texts = d.local_view()
                self.assertIn("md [/x]", texts)
                self.assertIn("Title [/x] · texty", [text for text in texts if "Title" in text] or [""])
                self.assertTrue(d.app.is_running)

if __name__ == "__main__":
    unittest.main()
