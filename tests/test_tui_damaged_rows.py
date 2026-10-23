"""A damaged agent record is one row in the dashboard, marked and explained; every other row keeps refreshing."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import DataTable, Static
from multiverse.plugins.testing import PluginTestCase
from multiverse.ui.backend import LocalBackend

UNREADABLE_WORKS = os.geteuid() != 0
DAMAGE = {"list": "[]", "truncated": '{"id":', "empty": ""}


class DamagedRowTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.healthy = self.agent("healthy", summary="HEALTHY-MARKER", phase="working")

    def damage(self, directory, file, kind):
        path = directory / file
        if kind == "unreadable":
            path.chmod(0)
            self.addCleanup(path.chmod, 0o600)
        elif kind.endswith("-list"):  # well-formed JSON whose name or harness is not text
            data = json.loads(path.read_text())
            data[kind[:-len("-list")]] = ["not-text"]
            path.write_text(json.dumps(data))
        else:
            path.write_text(DAMAGE[kind])

    def row(self, d, directory):
        """The table's cells for the agent in `directory`."""
        key = next(key for key, row in d.app.visible_rows.items() if row.get("id") == directory.name)
        return [getattr(cell, "plain", str(cell)) for cell in d.app.query_one("#agents", DataTable).get_row(key)]

    def inspector_text(self, d):
        return str(d.app.query_one("#inspect-state", Static).render())

    async def test_one_damaged_record_is_one_marked_row_and_the_rest_refresh(self):
        """With the real app and listing, one victim per damage kind of properties.json and runtime.json is a marked
        row naming its file while the healthy agent's new summary appears after F5 and nothing says Could not refresh;
        a break goes unnoticed in normal use because damage follows only crashes, and then it hides every agent
        (b041664)."""
        kinds = ["list", "truncated", "empty"] + (["unreadable"] if UNREADABLE_WORKS else [])
        victims = {(file, kind): self.agent(f"{file.split('.')[0]}-{kind}")
                   for file, more in (("properties.json", ["name-list", "harness-list"]), ("runtime.json", []))
                   for kind in kinds + more}
        async with self.dashboard(size=(130, 40)) as d:
            for (file, kind), directory in victims.items():
                self.damage(directory, file, kind)
            self.agent_summary(self.healthy, "REFRESHED-AFTER-DAMAGE")
            await d.press("f5")
            self.assertNotIn("Could not refresh", d.status)
            self.assertIn("REFRESHED-AFTER-DAMAGE", " ".join(self.row(d, self.healthy)))
            self.assertNotIn("⚠", self.row(d, self.healthy)[0])
            for (file, kind), directory in victims.items():
                with self.subTest(file=file, kind=kind):
                    row = self.row(d, directory)
                    self.assertIn("⚠", row[0])
                    self.assertIn(f"damaged: {file}", " ".join(row))
                    if file == "runtime.json":  # its properties still name it
                        self.assertIn(f"runtime-{kind}", row[0])
            self.assertEqual(sum("⚠" in row[0] for row in d.table()[1]), len(victims))
            victim = victims["properties.json", "list"]
            d.select(victim.name)
            await d.press("p")
            await d.until(lambda: "Damaged:" in self.inspector_text(d), "the inspector to name the damaged file")
            self.assertIn(f"state/agents/{victim.name}/properties.json", self.inspector_text(d))

    async def test_the_dashboard_offers_exactly_the_recovery_the_listing_hints(self):
        """On real folders, K kills a damaged row only when the listing's hint says kill can recover it (launch history
        shows a terminal may run), and d deletes one only when the hint says remove can: a folder whose properties.json
        is missing, also with launch history left (mv2 remove checks it for a running terminal first), but never an
        agent whose properties.json is there and unreadable; fragile because the listing's recover hint and the
        dashboard's gate must agree on one field across two modules, and a break leaves a native agent running
        behind an ordinary row or a damaged agent one key from the trash (b6c754f, 3dd29b3, 145c4c5)."""
        lost = self.agent("lost")  # no runtime.json, but a launch folder: a terminal may still be running
        (lost / "runtime.json").unlink()
        (lost / "launches" / "0e0e0e0e-0000-4000-8000-000000000001").mkdir(parents=True)
        dormant = self.agent("dormant")  # no runtime.json and no launch history
        (dormant / "runtime.json").unlink()
        broken = self.agent("broken")
        (broken / "properties.json").write_text("[]")
        agents = self.root / "state" / "agents"
        orphan = agents / "5f5f5f5f-0000-4000-8000-0000000000aa"
        (orphan / "plugin-state").mkdir(parents=True)
        (orphan / "plugin-state" / "note.txt").write_text("leftover")
        haunted = agents / "6a6a6a6a-0000-4000-8000-0000000000bb"  # no properties.json, but its launch was requested
        (haunted / "launches" / "7b7b7b7b-0000-4000-8000-0000000000cc").mkdir(parents=True)
        (haunted / "launches" / "7b7b7b7b-0000-4000-8000-0000000000cc" / "request.json").write_text("{}")
        with mock.patch.object(LocalBackend, "kill", return_value={}) as kill, \
                mock.patch.object(LocalBackend, "resume") as resume:
            async with self.dashboard(size=(130, 36)) as d:
                with self.subTest("a lost runtime file with launch history: K kills, r is refused"):
                    self.assertIn("damaged: runtime.json", " ".join(self.row(d, lost)))
                    d.select("lost")
                    self.assertEqual(d.app.selected["damaged"]["recover"], "kill")
                    await d.press("K")
                    self.assertEqual(len(d.app.screen_stack), 2, d.status)
                    await d.press("ctrl+enter")
                    await d.until(lambda: kill.called, "the kill to be requested")
                    self.assertEqual(kill.call_args.args[1], lost.name)
                    # A resume has no such way out: the record is still damaged until kill has found the terminal.
                    d.select("lost")
                    d.app.set_status("")  # the refusal must be said now, not left over from an earlier key
                    await d.press("r")
                    self.assertEqual(len(d.app.screen_stack), 1)
                    self.assertIn("cannot be used", d.status)
                    resume.assert_not_called()
                with self.subTest("a missing runtime file without launch history is just a dormant agent"):
                    self.assertNotIn("⚠", self.row(d, dormant)[0])
                    d.select("dormant")
                    self.assertNotIn("damaged", d.app.selected)
                with self.subTest("a damaged record the listing gives no hint for: K, s and r are refused and say why"):
                    d.select(broken.name)
                    for key in ("K", "s", "r"):  # a key that did nothing at all would pass a check on the screens alone
                        d.app.set_status("")
                        await d.press(key)
                        self.assertEqual(len(d.app.screen_stack), 1, key)
                        self.assertIn("cannot be used", d.status, key)
                    self.assertEqual(kill.call_count, 1)
                    resume.assert_not_called()
                with self.subTest("d is offered where properties.json is missing, not where it is there and unreadable"):
                    for folder, offered in ((haunted, True), (broken, False)):
                        d.select(folder.name)
                        d.app.set_status("")
                        self.assertEqual(d.app.selected["damaged"].get("recover"), "remove" if offered else None, folder.name)
                        await d.press("d")
                        self.assertEqual(len(d.app.screen_stack), 2 if offered else 1, folder.name)
                        if offered:
                            await d.press("escape")  # only looked at: the next row below is deleted for real
                        else:
                            self.assertIn("cannot be deleted while its record is damaged", d.status, folder.name)
                    self.assertTrue(haunted.exists() and broken.exists())
                with self.subTest("the row and the inspector say what resolves each kind of damaged row"):
                    await d.press("p")  # the inspector follows the selection from here on

                    async def shown(directory, name):
                        d.select(name)
                        await d.until(lambda: name in str(d.app.query_one("#inspect-name", Static).render()), "the inspector")
                        return " ".join(self.row(d, directory)), self.inspector_text(d)

                    row, inspector = await shown(orphan, orphan.name)  # nothing to fix: the folder only has to go
                    self.assertIn("leftover", row)
                    self.assertIn("trash", row)
                    self.assertIn("trash", inspector)
                    self.assertNotIn("Fix or restore", inspector)
                    folder = next(line for line in inspector.splitlines() if line.startswith("Leftover folder:"))
                    self.assertTrue(folder.endswith(orphan.name), folder)  # the folder d removes, not its parent
                    row, inspector = await shown(lost, "lost")  # a terminal may be running: K, then the file
                    self.assertIn("K", inspector)
                    self.assertIn("terminal", inspector)
                    row, inspector = await shown(broken, broken.name)  # a real damaged agent: repair it
                    self.assertIn("Fix or restore", inspector)
                    await d.press("p")
                with self.subTest("a record-free folder cannot be moved, and is deleted for real"):
                    d.select(orphan.name)
                    self.assertEqual(d.app.selected["damaged"]["recover"], "remove")
                    await d.press("x")
                    self.assertIsNone(d.app.moving)
                    await d.press("d")
                    self.assertEqual(len(d.app.screen_stack), 2, d.status)
                    await d.press("ctrl+enter")
                    await d.until(lambda: not orphan.exists(), "the folder to be moved to the trash")
                    await d.refresh()
                    self.assertNotIn(orphan.name, [row.get("id") for row in d.app.visible_rows.values()])
                    self.assertTrue(haunted.exists())  # the other folder was not touched

    def agent_summary(self, directory, summary):
        path = directory / "properties.json"
        data = json.loads(path.read_text())
        data["summary"] = summary
        path.write_text(json.dumps(data))


if __name__ == "__main__":
    unittest.main()
