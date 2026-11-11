import contextlib
from datetime import datetime, timedelta, timezone
import io
import sys
import unittest
from unittest import mock

from rich.cells import cell_len
from textual.widgets import DataTable

from multiverse import cli, display
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write
from multiverse.universe import agents as agent_records

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class ReportAgeTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_phase_says_how_long_ago_a_working_or_blocked_agent_reported(self):
        """The age is recomputed each refresh from a stored time; a wrong unit, order or clock goes unnoticed."""
        agents = {"fresh": ("working", 32, None), "busy": ("working", 600, None), "stuck": ("blocked", 7200, None),
                  "asking": ("blocked", 3000, {"kind": "user"}), "old": ("working", 4 * 86400, None),
                  "finished": ("done", 60, None), "legacy": ("working", None, None),
                  "garbled": ("working", "not a time", None),
                  "naive": ("working", "2026-11-10T11:59:00", None), "dated": ("working", "2026-11-10", None),
                  "broken-z": ("working", "2026-11-10T11:59:00Z:00", None),
                  "waits": ("blocked", 3000, "a-dependency-with-a-long-name")}
        for name, (phase, seconds, target) in agents.items():
            directory = self.agent(name)
            if isinstance(target, str):
                target = {"kind": "agent", "id": read(self.agent(target) / "properties.json")["id"],
                          "name": target, "universe": str(self.root)}
            properties = {**read(directory / "properties.json"), "phase": phase}
            if seconds is not None:
                properties["reported_at"] = ago(seconds) if isinstance(seconds, int) else seconds
            properties.update({"blocked_on": target} if target else {})
            write(directory / "properties.json", properties)
        expected = {"fresh": "working (32s)", "busy": "working (10m)", "stuck": "blocked (2h)",
                    "asking": "blocked (50m)", "old": "working (4d)", "finished": "done", "legacy": "working",
                    "garbled": "working", "naive": "working", "dated": "working", "broken-z": "working",
                    "waits": "blocked (50m)"}
        self.enterContext(mock.patch.object(display, "_now", lambda: NOW))

        with self.subTest("mv2 list's Phase says it the same way"):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "list", "--columns", "name,phase"]), \
                    contextlib.redirect_stdout(out):
                cli.main()
            listed = {line.split()[0]: line.split(None, 1)[1].strip() for line in out.getvalue().splitlines()
                      if line.split() and line.split()[0] in expected}
            self.assertEqual(listed, expected)

        def phases(d):
            headers, rows = d.table()
            return {row[0].split()[-1]: row[headers.index("Phase")].strip() for row in rows
                    if row[0].split()[-1] in expected}

        write(self.root / "views" / "default.json", {"columns": ["name", "phase", "blocked_on"]})
        async with self.dashboard(size=(140, 30)) as d:
            with self.subTest("the dashboard's Phase: the age, the target staying in the summary"):
                self.assertEqual(phases(d), expected)
        write(self.root / "views" / "default.json", {"columns": ["name", "phase"]})
        async with self.dashboard(size=(140, 30)) as d:
            with self.subTest("without blocked_on, the age alone"):
                self.assertEqual(phases(d)["asking"], "blocked (50m)")
        write(self.root / "views" / "default.json", {"columns": ["name", "live", "phase", "blocked_on", "summary"]})
        async with self.dashboard(size=(60, 20)) as d:
            with self.subTest("in a narrow terminal the age stays whole on screen"):
                table = d.app.query_one("#agents", DataTable)
                lines = [table.render_line(y).text for y in range(table.size.height)]
                line = next(line for line in lines if "asking" in line)
                self.assertIn("50m)", line)
                width = next(column.width for column in table.columns.values() if column.key.value == "phase")
                self.assertLessEqual(cell_len(phases(d)["waits"]), width)


    async def test_a_report_through_core_restarts_the_phase_age(self):
        """The time crosses from core to the label; a renamed field or unreadable format silently shows no age."""
        directory = self.agent("reporter")
        agent_records.report(self.root, "reporter", "working", "first pass")
        reported = datetime.fromisoformat(read(directory / "properties.json")["reported_at"].replace("Z", "+00:00"))
        clock = [reported + timedelta(seconds=150)]
        self.enterContext(mock.patch.object(display, "_now", lambda: clock[0]))
        write(self.root / "views" / "default.json", {"columns": ["name", "phase"]})
        async with self.dashboard(size=(120, 30)) as d:
            def phase():
                headers, rows = d.table()
                return next(row[headers.index("Phase")].strip() for row in rows if row[0].split()[-1] == "reporter")
            with self.subTest("the age counts from core's recorded report time"):
                self.assertEqual(phase(), "working (2m)")
            with self.subTest("a summary-only re-report restarts it"):
                agent_records.report(self.root, "reporter", summary="with an ETA: 20 minutes")
                again = read(directory / "properties.json")["reported_at"]
                again = datetime.fromisoformat(again.replace("Z", "+00:00"))
                clock[0] = again + timedelta(seconds=5)
                await d.refresh()
                self.assertEqual(phase(), "working (5s)")


if __name__ == "__main__":
    unittest.main()
