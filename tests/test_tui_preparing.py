"""An agent that is still being prepared reads `starting`; an abandoned or inconsistent preparation does not."""
import fcntl
import json
from pathlib import Path
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Static
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write


class PreparingRowTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.fine = self.agent("fine")
        self.new = self.agent("newcomer", phase="starting")
        self.launch_id = str(uuid4())
        # The record `start()` publishes with the agent, before any terminal or child can exist.
        write(self.new / "runtime.json", {"launch_id": self.launch_id, "state": "starting", "status": "unknown",
                                          "started_at": "2026-10-17T00:00:00Z", "preparing": True})
        self.lock = self.new / ".launch.lock"
        self.lock.write_text("")

    def live_cell(self, d, name):
        headers, rows = d.table()
        column = headers.index("Live")
        row = next(row for row in rows if row[0].lstrip("│├╰─ ▸▾").rstrip(" ⚠●") == name)
        return row[column]

    async def inspector(self, d, name):
        d.select(name)
        await d.press("p")
        await d.until(lambda: name in str(d.app.query_one("#inspect-name", Static).render()), "the inspector")
        return str(d.app.query_one("#inspect-state", Static).render())

    async def test_while_the_publisher_holds_its_lock_the_agent_reads_starting_everywhere(self):
        with self.lock.open("r+") as publisher:
            fcntl.flock(publisher, fcntl.LOCK_EX)  # the process that is preparing the launch
            async with self.dashboard(size=(130, 36)) as d:
                self.assertEqual(self.live_cell(d, "newcomer"), "starting")
                self.assertIn("· starting", await self.inspector(d, "newcomer"))
                self.assertNotIn("starting", self.live_cell(d, "fine"))
                self.assertNotIn("damaged", d.app.selected)
                d.app.set_status("")
                await d.press("K")  # nothing to kill yet: no screen, no refusal
                self.assertEqual(len(d.app.screen_stack), 1)

    async def test_an_abandoned_preparation_is_not_labelled_starting(self):
        async with self.dashboard(size=(130, 36)) as d:  # nobody holds the lock
            self.assertNotEqual(self.live_cell(d, "newcomer"), "starting")
            self.assertEqual(self.live_cell(d, "newcomer"), "dormant")
            shown = await self.inspector(d, "newcomer")
            self.assertNotIn("· starting", shown)  # the live state; the reported phase is separately `starting`
            self.assertIn("Launch preparation ended before native startup", shown)

    async def test_a_marker_with_native_startup_evidence_is_a_damaged_runtime_row(self):
        launch = self.new / "launches" / self.launch_id
        launch.mkdir(parents=True)
        (launch / "request.json").write_text(json.dumps({}))
        with self.lock.open("r+") as publisher:
            fcntl.flock(publisher, fcntl.LOCK_EX)
            async with self.dashboard(size=(130, 36)) as d:
                self.assertNotEqual(self.live_cell(d, "newcomer"), "starting")
                headers, rows = d.table()
                text = " ".join(" ".join(row) for row in rows)
                self.assertIn("damaged: runtime.json", text)
                self.assertIn("native startup evidence", text)


if __name__ == "__main__":
    unittest.main()
