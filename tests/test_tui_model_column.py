import contextlib
import io
import sys
import unittest
from unittest import mock

from multiverse import cli
from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from textual.widgets import DataTable


BRAIN = """
def register(mv):
    mv.dashboard.column("brain", "Brain", 18, lambda row: None if row["name"] == "aliased" else "theirs",
                        replaces="model")
"""


NARROW_BRAIN = """
def register(mv):
    mv.dashboard.column("brain", "Brain", 18, lambda row: "a plugin value longer than its column"
                        if row["name"] == "other" else None, replaces="model")
"""


class ModelColumnTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_the_model_column_names_each_agents_model_and_its_effort_only_where_shown(self):
        """The Model column shows each agent's model by the universe's custom name or the table's, a model neither
        names as recorded and none as -, its effort in parentheses only where the view lists effort with model, a plugin
        standing in for Model falling back to that label, and mv2 list --columns model the same; fragile because the name comes from core, the effort from a view mark, and
        a drift would show a codename, a stale effort, or one effort under another model's name."""
        self.agent("aliased", model="opus", effort="xhigh")
        self.agent("custom", model="claude-sonnet-5-5", effort="low")
        self.agent("typed", model="claude-next-x")
        self.agent("unset")
        settings = self.root / "harnesses" / "claude" / "harness.json"
        write(settings, {**read(settings), "settings": {"model_names": {"claude-sonnet-5-5": "Sonnet (team)"}}})
        table_name = next(row["model_name"] for row in lifecycle.listing(self.root, usage=False)
                          if row["name"] == "aliased")
        self.assertTrue(table_name and table_name != "opus", table_name)  # the table names the alias
        with_effort = {"aliased": f"{table_name} (xhigh)", "custom": "Sonnet (team) (low)", "typed": "claude-next-x",
                       "unset": "-"}
        without = {"aliased": table_name, "custom": "Sonnet (team)", "typed": "claude-next-x", "unset": "-"}
        view = self.root / "views" / "default.json"

        def cells(d, header="Model"):
            headers, rows = d.table()
            if header not in headers:
                return headers, None
            return headers, {row[0].split()[-1]: row[headers.index(header)].strip() for row in rows
                             if row[0].split()[-1] in with_effort}

        with self.subTest("mv2 list --columns model names the model, with its effort"):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "list", "--columns", "name,model"]), \
                    contextlib.redirect_stdout(out):
                cli.main()
            listed = {line.split()[0]: line.split(None, 1)[1].strip() for line in out.getvalue().splitlines()
                      if line.split() and line.split()[0] in with_effort}
            self.assertEqual(listed, with_effort)

        for columns, expected, what in ((["name", "model", "effort"], with_effort, "model and effort: in parentheses"),
                                        (["name", "model"], without, "model alone: no effort"),
                                        (["name", "effort", "phase"], None, "effort without model: nowhere")):
            write(view, {"columns": columns})
            async with self.dashboard(size=(140, 30)) as d:
                with self.subTest(what):
                    headers, found = cells(d)
                    if expected is None:
                        self.assertIsNone(found, headers)
                        _, rows = d.table()
                        self.assertFalse(any("(xhigh)" in cell or "(low)" in cell for row in rows for cell in row), rows)
                    else:
                        self.assertEqual(len(headers), len(columns) - ("effort" in columns), headers)
                        self.assertEqual(found, expected)

        self.write_plugin("brain", {"module": "plugin", "api": "1.13", "extends": ["dashboard.column"]},
                          {"plugin.py": BRAIN})
        self.enable("brain")
        write(view, {"columns": ["name", "model", "effort"]})
        async with self.dashboard(size=(140, 30)) as d:
            with self.subTest("a plugin standing in for Model: its value, and core's label where it has none"):
                await d.until(lambda: "Brain" in d.table()[0] and "theirs" in str(d.table()[1]), "the plugin's column")
                headers, found = cells(d, "Brain")
                self.assertEqual(found, {**{name: "theirs" for name in with_effort}, "aliased": with_effort["aliased"]})


    async def test_a_long_model_name_gives_way_to_its_effort_on_screen(self):
        """A Model cell grows to show a long name and its effort where there's room, and where there isn't, shortens
        the name and keeps the effort whole on screen (as it does past its bound, and in a plugin column standing in
        for Model at that column's own width); fragile because the table keeps each cell's full text and only
        the screen cuts it, so a fixed width hid the effort the person chose with nothing in the data to show it."""
        long_name = "Team Reasoning Model Ext"  # with " (xhigh)", exactly MODEL_WIDE: the most a cell grows to
        self.agent("thinker", model="claude-sonnet-5-5", effort="xhigh")
        settings = self.root / "harnesses" / "claude" / "harness.json"
        write(settings, {**read(settings), "settings": {"model_names": {"claude-sonnet-5-5": long_name}}})

        def drawn(d):
            """The agent's row as the screen draws it, and the Model column's width."""
            table = d.app.query_one("#agents", DataTable)
            lines = [table.render_line(y).text for y in range(table.size.height)]
            width = next(column.width for column in table.columns.values() if column.key.value == "model")
            return next(line for line in lines if "thinker" in line), width

        write(self.root / "views" / "default.json", {"columns": ["name", "model", "effort"]})
        async with self.dashboard(size=(140, 30)) as d:
            with self.subTest("with room, the whole name and effort"):
                line, width = drawn(d)
                self.assertIn(f"{long_name} (xhigh)", line)
        write(self.root / "views" / "default.json",  # enough columns that the long name can't have its room
              {"columns": ["name", "harness", "live", "status", "context", "cost", "id", "model", "effort", "phase",
                           "summary"]})
        async with self.dashboard(size=(112, 30)) as d:
            with self.subTest("without, the name is shortened and the effort kept whole"):
                line, width = drawn(d)
                self.assertIn("… (xhigh)", line)
                self.assertNotIn(long_name, line)
                self.assertLess(width, len(f"{long_name} (xhigh)"))
        self.agent("other", model="claude-sonnet-5-5")
        self.write_plugin("brain", {"module": "plugin", "api": "1.13", "extends": ["dashboard.column"]},
                          {"plugin.py": NARROW_BRAIN})
        self.enable("brain")
        write(self.root / "views" / "default.json", {"columns": ["name", "model", "effort"]})
        async with self.dashboard(size=(140, 30)) as d:
            with self.subTest("a plugin standing in for Model keeps its width; core's fallback is shortened to it"):
                await d.until(lambda: "Brain" in d.table()[0] and "longer" in str(d.table()[1]), "the plugin's column")
                table = d.app.query_one("#agents", DataTable)
                width = next(column.width for column in table.columns.values() if column.key.value == "model")
                self.assertEqual(width, 18)  # the plugin's own, never grown for its values or the fallback
                lines = [table.render_line(y).text for y in range(table.size.height)]
                line = next(line for line in lines if "thinker" in line)
                self.assertIn("… (xhigh)", line)

    async def test_name_and_model_fit_their_widest_cell(self):
        """Name and Model shrink to their widest cell, so short names and models leave no gap before the next column;
        fragile because a column that only ever widens looks right in the data and shows only as space on screen."""
        self.agent("ada", model="opus", effort="xhigh")
        self.agent("long-named-agent-x")  # wider than the Name header, narrower than the column's usual width
        write(self.root / "views" / "default.json", {"columns": ["name", "model", "effort", "summary"]})
        async with self.dashboard(size=(140, 30)) as d:
            table = d.app.query_one("#agents", DataTable)
            widths = {column.key.value: column.width for column in table.columns.values()}
            cells = {field: [table.get_cell(row, field) for row in table.rows] for field in ("name", "model")}
            for field, usual in (("name", 26), ("model", 18)):
                with self.subTest(field):
                    widest = max(max(cell.cell_len for cell in cells[field]), len(table.columns[field].label.plain))
                    self.assertLess(widest, usual)  # the fixture's names and models are short
                    self.assertGreater(widest, len(table.columns[field].label.plain))  # set by a cell, not the header
                    self.assertEqual(widths[field], widest)
                    self.assertFalse([cell.plain for cell in cells[field] if "…" in cell.plain])


if __name__ == "__main__":
    unittest.main()
