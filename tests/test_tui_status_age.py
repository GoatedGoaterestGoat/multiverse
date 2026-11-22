"""The Status cell's time inactive (the `inactive` view mark)."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest import mock

from textual.widgets import DataTable

from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import lifecycle
from multiverse.ui.columns_screen import ColumnsScreen


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class InactiveMarkTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_the_status_cell_says_how_long_an_agent_has_been_inactive_only_where_shown(self):
        """With `inactive` and `status` in the view, an idle or waiting agent's Status cell says how long since its
        status last changed, `idle (3m)`, a working one that is active now just `working`, and one whose change time
        isn't known just its status; without the mark, every cell is the bare status. Fragile because the time comes
        from core's row and the mark from the view, and a slip shows a working agent as stale or a time nobody asked
        for."""
        self.agent("resting", runtime={"status": "idle", "status_changed_at": ago(minutes=3, seconds=5)})
        self.agent("asking", runtime={"status": "waiting", "status_changed_at": ago(minutes=5, seconds=5)})
        self.agent("busy", runtime={"status": "working", "status_changed_at": ago(hours=2)})
        self.agent("unknown", runtime={"status": "idle"})
        listing = lifecycle.listing

        def live(*args, **kwargs):  # these records' terminals don't exist: they stand in for live agents
            rows = listing(*args, **kwargs)
            for row in rows:
                if row.get("name") in expected:
                    row.update(live="alive", status=row["runtime"]["status"], active_now=row["name"] == "busy")
            return rows

        view = self.root / "views" / "default.json"
        expected = {"resting": "idle (3m)", "asking": "waiting (5m)", "busy": "working", "unknown": "idle"}
        for columns, shown in ((["name", "status", "inactive", "summary"], expected),
                               (["name", "status", "summary"], {name: label.split(" (")[0]
                                                                for name, label in expected.items()})):
            with self.subTest(columns=columns):
                view.write_text(json.dumps({"columns": columns}) + "\n")
                with mock.patch.object(lifecycle, "listing", live):
                    async with self.dashboard(size=(140, 30)) as d:
                        headers, rows = d.table()
                        cells = {name: row[headers.index("Status")].strip() for row in rows for name in expected
                                 if name in row[0].split()}  # a waiting agent's name carries its mark too
                self.assertEqual(cells, shown)
        with self.subTest("a Status cell with no room for the time says only the status"):
            view.write_text(json.dumps({"columns": ["name", "harness", "model", "live", "context", "cost", "status",
                                                    "inactive", "focus", "phase", "summary", "id", "updated_at"]}))
            with mock.patch.object(lifecycle, "listing", live):
                async with self.dashboard(size=(122, 30)) as d:
                    table = d.app.query_one("#agents", DataTable)
                    status = next(key for key in table.columns if key.value == "status")
                    cells = [table.get_cell(key, status).plain for key in table.rows]
                    self.assertLess(table.columns[status].width, len("waiting (5m)"))
                    self.assertIn("waiting", cells)  # not a cut "waiting (5" or "waiting (5m)" left to the table
        view.write_text(json.dumps({"columns": ["name", "status", "inactive", "summary"]}) + "\n")
        with self.subTest("C lists it under Status"):
            async with self.dashboard(size=(140, 30)) as d:
                await d.press("C")
                await d.until(lambda: isinstance(d.screen, ColumnsScreen), "the Columns screen")
                keys = [row[0] for row in d.screen.rows()]
                self.assertEqual(keys[keys.index("status") + 1], "inactive")


if __name__ == "__main__":
    unittest.main()
