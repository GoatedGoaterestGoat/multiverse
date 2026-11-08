import contextlib
import io
import shutil
import sys
import unittest
from unittest import mock

from rich.cells import cell_len
from textual.widgets import DataTable, Static

from multiverse import cli
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write
from multiverse.ui.columns import COLUMNS
from multiverse.universe import agents, blocked


PROGRESS = """
def register(mv):
    mv.dashboard.column("progress", "Progress", 10, lambda row: None if row["name"] in ("on-user", "working")
                        else "theirs", replaces="phase")
"""


class BlockedOnPhaseTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_a_blocked_agents_phase_says_what_it_waits_on_where_shown_and_never_misleads(self):
        """A blocked agent's Phase cell says what it is blocked on only where the view lists blocked_on, follows a
        rename, says when the agent is gone, never reads an agent named user as the person, keeps a long name inside
        its parentheses, grows Phase only into the room a narrow terminal leaves, shows the same under core:phase and leaves a plugin column standing in for Phase its own,
        and text mv2 list always shows it; fragile because the dashboard strips the field where it
        isn't listed and the cell is narrow, so a wrong or clipped name would quietly point the person at the wrong
        agent."""
        lead, user_agent, ada = self.agent("lead"), self.agent("user"), self.agent("ada")
        far = self.subverse("qa")
        tester = self.agent("tester", root=far)
        long = self.agent("a-reviewer-with-a-very-long-name")
        waiting = {name: self.agent(name) for name in ("on-lead", "on-user", "on-self", "on-agent-user", "on-far",
                                                         "on-long", "on-gone", "working")}

        def block(name, reference, universe=None):
            directory = waiting[name]
            agents.report(self.root, name, "blocked",
                          blocked_on=blocked.select(self.root, directory, reference, universe))

        block("on-lead", "lead")
        block("on-user", "user")
        block("on-self", "self")
        block("on-agent-user", f"agent:{read(user_agent / 'properties.json')['id']}")
        block("on-far", "tester", str(far))
        block("on-long", "a-reviewer-with-a-very-long-name")
        block("on-gone", "ada")
        shutil.rmtree(ada)
        block("working", "lead")
        agents.report(self.root, "working", "working")  # any other phase clears it
        properties = read(lead / "properties.json")
        write(lead / "properties.json", {**properties, "name": "lead-renamed"})
        expected = {"on-lead": "blocked (lead-renamed)", "on-user": "blocked (user)", "on-self": "blocked (self)",
                    "on-agent-user": "blocked (agent user)", "on-far": "blocked (tester in qa)",
                    "on-gone": "blocked (ada: gone)", "working": "working"}
        view = self.root / "views" / "default.json"

        def phases(d, header="Phase"):
            headers, rows = d.table()
            column = headers.index(header) if header in headers else None
            found = {}
            for row in rows:
                words = row[0].split()
                for name in waiting:
                    if name in words:
                        found[name] = row[column].strip() if column is not None else None
            return headers, found

        with self.subTest("text mv2 list always says it, as the person's dashboard would"):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "list", "--columns", "name,phase"]), \
                    contextlib.redirect_stdout(out):
                cli.main()
            listed = {line.split()[0]: line.split(None, 1)[1].strip() for line in out.getvalue().splitlines()
                      if line.split() and line.split()[0] in waiting}
            self.assertEqual({name: listed[name] for name in expected}, expected)
            self.assertEqual(listed["on-long"], "blocked (a-reviewer-with-a-very-long-name)")

        write(view, {"columns": ["name", "phase", "blocked_on"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("a view that lists blocked_on shows it in Phase, not a column of its own"):
                headers, found = phases(d)
                self.assertEqual(len(headers), 2, headers)
                self.assertEqual({name: found[name] for name in expected}, expected)
            with self.subTest("the cell grows to show it, and a long name is shortened inside the parentheses"):
                table = d.app.query_one("#agents", DataTable)
                width = next(column.width for column in table.columns.values() if column.key.value == "phase")
                for name, cell in found.items():
                    self.assertLessEqual(cell_len(cell), width, cell)  # all of it is drawn
                self.assertTrue(found["on-long"].startswith("blocked (a-reviewer"), found["on-long"])
                self.assertTrue(found["on-long"].endswith("…)"), found["on-long"])

        write(view, {"columns": ["name", "live", "phase", "blocked_on", "summary"]})
        async with self.dashboard(size=(60, 20)) as d:
            with self.subTest("in a narrow terminal Phase grows only into the room the other columns leave"):
                table = d.app.query_one("#agents", DataTable)
                self.assertLessEqual(sum(column.width + 2 for column in table.columns.values()),
                                     table.scrollable_content_region.width, [(c.key.value, c.width) for c in table.columns.values()])
                self.assertIn("Working on", d.table()[0])
                headers, found = phases(d)
                width = next(column.width for column in table.columns.values() if column.key.value == "phase")
                for name, cell in found.items():
                    self.assertLessEqual(cell_len(cell), width, cell)
                self.assertTrue(found["on-long"].endswith("…)") or found["on-long"] == "blocked", found["on-long"])
            with self.subTest("a Phase cell too narrow to name it says only blocked, never a clipped 'blocked ('"):
                row = next(row for row in d.app.visible_rows.values() if row.get("name") == "on-long")
                self.assertEqual(COLUMNS["phase"].render(row, width=11).plain, "blocked")
                self.assertEqual(COLUMNS["phase"].render(row, width=12).plain, "blocked (a…)")

        write(view, {"columns": ["name", "phase"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("a view that doesn't list it shows a blocked agent as plainly blocked"):
                self.assertEqual(phases(d)[1]["on-lead"], "blocked")

        write(view, {"columns": ["name", "live", "blocked_on"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("without the Phase column it shows nowhere, the inspector included"):
                headers, rows = d.table()
                self.assertNotIn("Phase", headers)
                self.assertFalse(any("blocked (" in cell for row in rows for cell in row), rows)
                row = next(row for row in d.app.visible_rows.values() if row.get("name") == "on-lead")
                self.assertNotIn("blocked_on", row)  # nor in v's details, which read this row
                d.select("on-lead")
                await d.press("p")
                state = d.app.query_one("#inspect-state", Static)
                await d.until(lambda: "Reported:" in str(state.render()), "the inspector's Reported line")
                self.assertIn("Reported: blocked", str(state.render()))
                self.assertNotIn("blocked (", str(state.render()))

        self.write_plugin("progress", {"module": "plugin", "api": "1.13", "extends": ["dashboard.column"]},
                          {"plugin.py": PROGRESS})
        self.enable("progress")
        write(view, {"columns": ["name", "phase", "blocked_on"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("a plugin column standing in for Phase stays the plugin's, core's phase where it has none"):
                await d.until(lambda: "Progress" in d.table()[0] and "theirs" in str(d.table()[1]),
                              "the plugin's column in Phase's place")
                headers, found = phases(d, "Progress")
                self.assertNotIn("Phase", headers)
                self.assertEqual({name: found[name] for name in expected},
                                 {**{name: "theirs" for name in expected}, "on-user": "blocked", "working": "working"})
        write(view, {"columns": ["name", "core:phase", "blocked_on"]})
        async with self.dashboard(size=(140, 40)) as d:
            with self.subTest("core:phase is core's Phase, and shows it as phase does"):
                headers, found = phases(d)
                self.assertNotIn("Progress", headers)
                self.assertEqual({name: found[name] for name in expected}, expected)
                table = d.app.query_one("#agents", DataTable)
                width = next(column.width for column in table.columns.values() if column.key.value == "core:phase")
                for name, cell in found.items():
                    self.assertLessEqual(cell_len(cell), width, cell)
                self.assertTrue(found["on-long"].endswith("…)"), found["on-long"])


if __name__ == "__main__":
    unittest.main()
