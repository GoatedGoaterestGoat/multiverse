import contextlib
import io
import shutil
import sys
import unittest
from unittest import mock

from textual.widgets import DataTable, Static

from multiverse import cli
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write
from multiverse.ui import theme
from multiverse.universe import agents, blocked


STAND_IN = """
def register(mv):
    mv.dashboard.column("doing", "Doing", 30, lambda row: None if row["name"] in ("on-user", "working")
                        else "theirs", replaces="summary")
"""


class BlockedOnSummaryTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_a_blocked_agents_summary_says_what_it_waits_on_where_shown_and_never_misleads(self):
        """The target crosses core, a view mark and two columns, so a drift points the person at the wrong agent."""
        lead, user_agent, ada = self.agent("lead"), self.agent("user"), self.agent("ada")
        far = self.subverse("qa")
        self.agent("tester", root=far)
        waiting = {name: self.agent(name) for name in ("on-lead", "on-user", "on-self", "on-agent-user", "on-far",
                                                         "on-gone", "working")}

        def block(name, reference, universe=None, summary=None):
            directory = waiting[name]
            agents.report(self.root, name, "blocked", summary,
                          blocked_on=blocked.select(self.root, directory, reference, universe))

        block("on-lead", "lead")
        block("on-user", "user", summary="decide the model list")
        block("on-self", "self", summary="tests running, ETA 5 min")
        block("on-agent-user", f"agent:{read(user_agent / 'properties.json')['id']}")
        block("on-far", "tester", str(far))
        block("on-gone", "ada")
        shutil.rmtree(ada)
        block("working", "lead", summary="the old summary")
        agents.report(self.root, "working", "working", "back to work")  # any other phase clears it
        properties = read(lead / "properties.json")
        write(lead / "properties.json", {**properties, "name": "lead-renamed"})
        for directory in waiting.values():  # how long ago each reported has its own test (test_tui_report_age)
            write(directory / "properties.json",
                  {k: v for k, v in read(directory / "properties.json").items() if k != "reported_at"})
        expected = {"on-lead": "Waiting for lead-renamed", "on-user": "Waiting for user: decide the model list",
                    "on-self": "Waiting for self: tests running, ETA 5 min", "on-agent-user": "Waiting for agent user",
                    "on-far": "Waiting for tester in qa", "on-gone": "Waiting for ada (gone)",
                    "working": "back to work"}
        plain = {**dict.fromkeys(expected, "—"), "on-user": "decide the model list",
                 "on-self": "tests running, ETA 5 min", "working": "back to work"}
        view = self.root / "views" / "default.json"

        def cells(d, header="Working on"):
            headers, rows = d.table()
            column = headers.index(header) if header in headers else None
            found = {}
            for row in rows:
                for name in waiting:
                    if name in row[0].split():
                        found[name] = row[column].strip() if column is not None else None
            return headers, found

        with self.subTest("text mv2 list: Phase plain, the target leading the summary"):
            out = io.StringIO()
            argv = ["mv2", "-u", str(self.root), "list", "--columns", "name,phase,summary"]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
                cli.main()
            listed = {}
            for line in out.getvalue().splitlines():
                parts = line.split(None, 2)
                if parts and parts[0] in waiting:
                    listed[parts[0]] = (parts[1], parts[2].strip() if len(parts) > 2 else "")
            self.assertEqual({name: listed[name][1] for name in expected}, expected)
            self.assertEqual({listed[name][0] for name in expected if name != "working"}, {"blocked"})

        write(view, {"columns": ["name", "phase", "blocked_on", "summary"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("a view listing blocked_on and summary: the target leads the summary, Phase stays plain"):
                headers, found = cells(d)
                self.assertEqual(len(headers), 3, headers)
                self.assertEqual({name: found[name] for name in expected}, expected)
                self.assertEqual({cells(d, "Phase")[1][name] for name in expected if name != "working"}, {"blocked"})
            with self.subTest("the target is drawn in its own colour, apart from the summary"):
                table = d.app.query_one("#agents", DataTable)
                key = next(key for key, row in d.app.visible_rows.items() if row.get("name") == "on-user")
                cell = table.get_row(key)[2]
                coloured = "".join(cell.plain[span.start:span.end] for span in cell.spans
                                   if theme.YELLOW.lower() in str(span.style).lower())
                self.assertEqual(coloured, "Waiting for user")
            with self.subTest("the inspector's work line says it the same way"):
                d.select("on-user")
                await d.press("p")
                work = d.app.query_one("#inspect-work", Static)
                await d.until(lambda: "Waiting for user" in str(work.render()), "the inspector's work line")
                self.assertEqual(str(work.render()), expected["on-user"])

        write(view, {"columns": ["name", "phase", "summary"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("a view that doesn't list blocked_on shows each summary as reported"):
                self.assertEqual({name: cells(d)[1][name] for name in expected}, plain)

        write(view, {"columns": ["name", "phase", "blocked_on"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("without the Summary column it shows nowhere, the inspector included"):
                headers, rows = d.table()
                self.assertNotIn("Working on", headers)
                self.assertFalse(any("Waiting for" in cell for row in rows for cell in row), rows)
                row = next(row for row in d.app.visible_rows.values() if row.get("name") == "on-user")
                self.assertNotIn("blocked_on", row)  # nor in v's details, which read this row
                d.select("on-user")
                await d.press("p")
                work = d.app.query_one("#inspect-work", Static)
                await d.until(lambda: "decide" in str(work.render()), "the inspector's work line")
                self.assertEqual(str(work.render()), "decide the model list")

        write(view, {"columns": ["name", "phase", "blocked_on", "core:summary"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("core:summary is core's Summary, and shows it as summary does"):
                self.assertEqual({name: cells(d)[1][name] for name in expected}, expected)

        self.write_plugin("doing", {"module": "plugin", "api": "1.13", "extends": ["dashboard.column"]},
                          {"plugin.py": STAND_IN})
        self.enable("doing")
        write(view, {"columns": ["name", "phase", "blocked_on", "summary"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("a plugin standing in for Summary keeps its own value, core's where it has none"):
                await d.until(lambda: "Doing" in d.table()[0] and "theirs" in str(d.table()[1]), "the plugin's column")
                found = cells(d, "Doing")[1]
                self.assertEqual({name: found[name] for name in expected},
                                 {**dict.fromkeys(expected, "theirs"), "on-user": expected["on-user"],
                                  "working": expected["working"]})


if __name__ == "__main__":
    unittest.main()
