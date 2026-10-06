"""Portal behavior against real on-disk views, with no harness launches."""
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Button, DataTable, Input, Markdown, Static
from multiverse.ui.backend import LocalBackend
from multiverse.ui.tree import group_key
from multiverse.ui.views import view
from multiverse.ui.app import MultiverseApp
from multiverse.ui.panels import ArtifactPreview, artifact_path


class PortalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.testing = self.root / "subverses/testing"
        self.mockups = self.root / "subverses/mockups"
        self.cad = self.mockups / "subverses/cad"
        for path, title in ((self.root, "Lab"), (self.testing, "Testing queue"),
                            (self.mockups, "Mockup previews"), (self.cad, "CAD checks")):
            (path / "views").mkdir(parents=True)
            (path / "universe.json").write_text(json.dumps({"schema": 1, "id": title, "name": path.name, "workspace": "."}))
            self.write_view(path, {"title": title, "columns": ["name", "phase"]})
        self.write_view(self.testing, {"title": "Testing queue", "columns": ["name", "live"], "widgets": [
            {"type": "table", "title": "Retest queue", "columns": ["ID", "Status"], "rows": [["UI-01", "retest"]]}]})
        (self.mockups / "artifacts").mkdir()
        (self.mockups / "artifacts/preview.md").write_text("# Gearbox preview\n\n**12 constraints** satisfied.")
        self.write_view(self.mockups, {"title": "Mockup previews", "columns": ["name", "summary"], "widgets": [
            {"type": "markdown", "title": "Design brief", "text": "**Gearbox** · concept B"},
            {"type": "artifacts", "title": "Previews", "items": [{"label": "Gearbox drawing", "path": "artifacts/preview.md"}]}]})
        self.app = MultiverseApp(self.root, refresh_interval=0)

    def write_view(self, root, config):
        (root / "views/default.json").write_text(json.dumps(config))

    async def settle(self, pilot):
        await pilot.pause()
        await self.app.workers.wait_for_complete()
        await pilot.pause()

    async def enter(self, pilot, root):
        self.app.select_key(group_key(root))
        self.app.query_one("#agents").focus()
        await pilot.press("enter")
        await self.settle(pilot)
        self.assertEqual(self.app.root, root)

    async def test_local_titles_columns_widgets_and_multilevel_context_restored(self):
        async with self.app.run_test(size=(150, 35)) as pilot:
            await self.settle(pilot)
            self.app.select_key(group_key(self.testing))
            await pilot.press("space", "slash", *"mockup", "enter")
            await self.enter(pilot, self.mockups)
            self.assertEqual(self.app.title, "Mockup previews")
            self.assertEqual([c.key.value for c in self.app.query_one("#agents", DataTable).ordered_columns], ["name", "summary"])
            self.assertTrue(self.app.query_one("#local-view").display)
            self.assertEqual(len(self.app.query("#local-view Markdown")), 1)
            await self.enter(pilot, self.cad)
            self.assertEqual(self.app.title, "CAD checks")
            self.assertFalse(self.app.query_one("#local-view").display)
            await pilot.press("backspace")
            await self.settle(pilot)
            self.assertEqual(self.app.root, self.mockups)
            self.assertEqual(self.app.selected_key, group_key(self.cad))
            self.assertTrue(await pilot.click("#breadcrumb"))
            await self.settle(pilot)
            self.assertEqual(self.app.root, self.root)
            self.assertEqual(self.app.selected_key, group_key(self.mockups))
            self.assertEqual(self.app.query_one("#filter", Input).value, "mockup")
            self.assertIn(str(self.testing), self.app.collapsed)
            await pilot.press("escape")
            await self.enter(pilot, self.testing)
            self.assertEqual(self.app.title, "Testing queue")
            table = self.app.query_one("#view-table-0", DataTable)
            self.assertEqual(table.get_row_at(0)[0].plain, "UI-01")
            self.assertEqual([c.key.value for c in self.app.query_one("#agents", DataTable).ordered_columns], ["name", "live"])

    async def test_artifact_keyboard_mouse_refresh_and_resize(self):
        async with self.app.run_test(size=(150, 35)) as pilot:
            await self.settle(pilot)
            index = list(self.app.visible_rows).index(group_key(self.mockups))
            await pilot.double_click("#agents", offset=(5, index + 1))
            await self.settle(pilot)
            self.assertEqual(self.app.root, self.mockups)
            button = self.app.query_one("#artifact-1-0", Button)
            button.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ArtifactPreview)
            self.assertIn("Gearbox preview", self.app.screen.query_one(Markdown).source)
            await pilot.press("escape")
            await pilot.resize_terminal(70, 24)
            await pilot.pause()
            local = self.app.query_one("#local-view")
            self.assertGreater(local.region.height, 2)
            self.assertLessEqual(local.region.right, 70)
            self.app.refresh_snapshot()
            await self.settle(pilot)
            self.assertIs(self.app.query_one("#artifact-1-0"), button)
            button.scroll_visible(animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click("#artifact-1-0"))
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ArtifactPreview)

    async def test_delayed_parent_scan_cannot_overwrite_child(self):
        entered, release = Event(), Event()
        original = self.app.backend.snapshot
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            def slow(root):
                if root == self.root:
                    entered.set()
                    release.wait(3)
                return original(root)
            with patch.object(self.app.backend, "snapshot", side_effect=slow):
                self.app.refresh_snapshot()
                await asyncio.to_thread(entered.wait, 1)
                self.app.select_key(group_key(self.testing))
                await pilot.press("enter")
                release.set()
                await self.settle(pilot)
                self.assertEqual(self.app.snapshot["root"], self.testing)
                self.assertEqual(self.app.title, "Testing queue")

    async def test_inspector_replaces_view_without_squeezing_hierarchy(self):
        async with self.app.run_test(size=(140, 32)) as pilot:
            await self.settle(pilot)
            await self.enter(pilot, self.mockups)
            await pilot.press("p")
            await pilot.pause()
            self.assertFalse(self.app.query_one("#local-view").display)
            self.assertGreater(self.app.query_one("#agents").region.width, 65)
            await pilot.press("p")
            await pilot.pause()
            self.assertTrue(self.app.query_one("#local-view").display)
            await pilot.resize_terminal(70, 24)
            await pilot.press("p")
            await pilot.pause()
            self.assertGreater(self.app.query_one("#agents").region.height, 5)
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(self.app.query_one("#local-view").display)

    async def test_invalid_widget_and_missing_artifact_leave_navigation_usable(self):
        self.write_view(self.cad, {"widgets": [
            {"type": "table", "columns": ["a"], "rows": [["a", "b"]]},
            {"type": "artifacts", "items": [{"path": "missing.svg"}]}]})
        async with self.app.run_test() as pilot:
            await self.settle(pilot)
            await self.enter(pilot, self.cad)
            self.assertIn("match columns", self.app.query_one("#local-view Markdown").source)
            self.app.query_one("#artifact-1-0").focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("missing", str(self.app.screen.query_one("#artifact-error", Static).render()))
            await pilot.press("escape", "backspace")
            await self.settle(pilot)
            self.assertEqual(self.app.root, self.root)

    def test_artifact_paths_and_invalid_json(self):
        for path in ("../outside", "/etc/passwd", "artifacts", "absent"):
            with self.assertRaises(ValueError):
                artifact_path(self.mockups, path)
        (self.mockups / "artifacts/escape").symlink_to(self.root / "universe.json")
        with self.assertRaises(ValueError):
            artifact_path(self.mockups, "artifacts/escape")
        (self.cad / "views/default.json").write_text("{")
        config = view(self.cad)
        self.assertEqual(config["widgets"][0]["title"], "View configuration error")


if __name__ == "__main__":
    unittest.main()
