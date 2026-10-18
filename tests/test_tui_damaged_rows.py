"""A damaged agent record is one row in the dashboard, marked and explained; every other row keeps refreshing."""
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Static
from multiverse.plugins.testing import PluginTestCase

UNREADABLE_WORKS = os.geteuid() != 0


class DamagedRowTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.healthy = self.agent("healthy", summary="HEALTHY-MARKER", phase="working")
        self.victim = self.agent("victim", summary="victim's own", phase="blocked")
        self.originals = {name: (self.victim / name).read_bytes() for name in ("properties.json", "runtime.json")}
        self.addCleanup(self.restore)

    def restore(self):
        for name, data in self.originals.items():
            path = self.victim / name
            path.exists() and path.chmod(0o600)
            path.write_bytes(data)

    def rows(self, d):
        headers, rows = d.table()
        return headers, {row[0].lstrip("│├╰─ ▸▾").rstrip(" ⚠●"): row for row in rows}  # the tree guides and markers are not the name

    def inspector_text(self, d):
        return str(d.app.query_one("#inspect-state", Static).render())

    async def test_a_damaged_properties_file_is_a_marked_row_and_the_healthy_one_still_refreshes(self):
        async with self.dashboard(size=(130, 36)) as d:
            (self.victim / "properties.json").write_text("[]")
            self.agent_summary(self.healthy, "REFRESHED-AFTER-DAMAGE")
            await d.press("f5")
            self.assertNotIn("Could not refresh", d.status)
            _, rows = self.rows(d)
            text = " ".join(" ".join(row) for row in rows.values())
            self.assertIn("REFRESHED-AFTER-DAMAGE", text)
            self.assertIn("damaged: properties.json: must be a JSON object, not a list", text)
            marked = [name for name, row in rows.items() if "⚠" in row[0]]
            self.assertEqual(len(marked), 1)
            d.select(marked[0])
            await d.press("p")
            await d.until(lambda: "Damaged:" in self.inspector_text(d), "the inspector to name the damaged file")
            shown = self.inspector_text(d)
            self.assertIn(f"state/agents/{self.victim.name}/properties.json", shown)
            self.assertIn("must be a JSON object, not a list", shown)

    async def test_a_damaged_runtime_keeps_the_agents_name_and_says_which_file(self):
        (self.victim / "runtime.json").write_text('{"state":')
        async with self.dashboard(size=(130, 36)) as d:
            _, rows = self.rows(d)
            self.assertIn("victim", rows)
            self.assertIn("⚠", rows["victim"][0])
            self.assertIn("damaged: runtime.json", " ".join(rows["victim"]))
            self.assertNotIn("Could not refresh", d.status)

    @unittest.skipUnless(UNREADABLE_WORKS, "root reads every file")
    async def test_an_unreadable_file_is_a_damaged_row_too(self):
        (self.victim / "properties.json").chmod(0)
        self.addCleanup((self.victim / "properties.json").chmod, 0o600)
        async with self.dashboard(size=(130, 36)) as d:
            _, rows = self.rows(d)
            self.assertIn("healthy", rows)
            self.assertIn("cannot be read: Permission denied", " ".join(" ".join(row) for row in rows.values()))

    async def test_nothing_acts_on_a_damaged_row_and_the_status_line_says_why(self):
        (self.victim / "properties.json").write_text("")
        async with self.dashboard(size=(130, 36)) as d:
            _, rows = self.rows(d)
            damaged = next(name for name, row in rows.items() if "⚠" in row[0])
            d.select(damaged)
            for key in ("m", "K", "s", "r", "enter"):
                d.app.set_status("")  # each key must say it itself, not leave the last key's message standing
                await d.press(key)
                self.assertEqual(len(d.app.screen_stack), 1, f"{key} opened a screen on a damaged row")
                self.assertIn("cannot be used", d.status, key)
                self.assertIn("properties.json", d.status)
                self.assertIn("is empty", d.status)
            for key, verb in (("x", "moved"), ("d", "deleted")):  # a move or a delete says so the same way
                await d.press(key)
                self.assertIn(f"cannot be {verb[:-1]}d while its record is damaged", d.status, key)
                self.assertIsNone(d.app.moving)
                self.assertEqual(len(d.app.screen_stack), 1)

    async def test_kill_goes_through_on_a_damaged_row_only_when_the_listing_says_kill_can_recover_it(self):
        from unittest import mock
        from multiverse.tools import list_agents
        from multiverse.ui.backend import LocalBackend
        (self.victim / "properties.json").write_text("[]")
        real = list_agents.run

        def marked(*args, **kwargs):
            rows = real(*args, **kwargs)
            if kwargs.get("json"):
                for row in rows:
                    if row.get("damaged") and RECOVER:
                        row["damaged"] = {**row["damaged"], "recover": "kill"}
            return rows

        with mock.patch.object(list_agents, "run", marked), mock.patch.object(LocalBackend, "kill", return_value={}) as kill:
            for RECOVER in (False, True):
                async with self.dashboard(size=(130, 36)) as d:
                    _, rows = self.rows(d)
                    d.select(next(name for name, row in rows.items() if "⚠" in row[0]))
                    await d.press("K")
                    if not RECOVER:
                        self.assertEqual(len(d.app.screen_stack), 1)
                        self.assertIn("cannot be used", d.status)
                        continue
                    self.assertEqual(len(d.app.screen_stack), 2, d.status)
                    shown = " ".join(str(widget.render()) for widget in d.screen.query(Static))
                    self.assertIn("tagged terminal (its runtime record is missing)", shown)
                    await d.press("ctrl+enter")
                    await d.until(lambda: kill.called, "the kill to be requested")
                    self.assertEqual(kill.call_args.args[1], self.victim.name)

    async def test_putting_the_file_back_puts_the_row_back(self):
        (self.victim / "properties.json").write_text("[]")
        async with self.dashboard(size=(130, 36)) as d:
            self.assertTrue(any("⚠" in row[0] for row in d.table()[1]))
            self.restore()
            await d.press("f5")
            _, rows = self.rows(d)
            self.assertIn("victim", rows)
            self.assertFalse(any("⚠" in row[0] for row in d.table()[1]))

    def agent_summary(self, directory, summary):
        path = directory / "properties.json"
        data = json.loads(path.read_text())
        data["summary"] = summary
        path.write_text(json.dumps(data))


if __name__ == "__main__":
    unittest.main()
