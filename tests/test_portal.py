"""Portal behavior against real on-disk views, with no harness launches."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import DataTable, Input, Label, Markdown, Static
from multiverse.ui.tree import group_key
from multiverse.ui.app import MultiverseApp
from multiverse.ui.panels import LocalView, TablePanel, artifact_path
from isolation import isolate


class PortalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate(self)
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
        (root / "views/default.json").write_text(config if isinstance(config, str) else json.dumps(config))

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

    def shown(self):
        """The local view's titles and texts, and the status line."""
        view = self.app.query_one("#local-view")
        return " ".join([str(label.render()) for label in view.query(Label)] + [m.source for m in view.query(Markdown)]
                        + [str(self.app.query_one("#status", Static).render())])

    def test_an_artifact_path_can_never_leave_its_universe(self):
        """A view's artifact path, which an agent writes, is refused when it climbs out, is absolute (even inside the
        universe), is a directory or a missing file, or goes through a symlink to a file outside; fragile because
        artifact_path is all that stands between a view file and any file the user can read, and a refactor of path
        handling breaks it with no other test noticing."""
        outside = self.root / "universe.json"  # a real file outside the mockups universe
        (self.mockups / "artifacts/escape").symlink_to(outside)
        legitimate = self.mockups / "artifacts/preview.md"
        # not refused too much
        self.assertEqual(artifact_path(self.mockups, "artifacts/preview.md"), legitimate.resolve())
        for path, why in (("../../universe.json", "climbs out to a file that exists"),
                          (str(outside), "absolute, outside"), (str(legitimate), "absolute, even inside the universe"),
                          ("artifacts/escape", "a symlink out"), ("artifacts", "a directory"),
                          ("absent", "a missing file")):
            with self.subTest(path=path, why=why):
                with self.assertRaises(ValueError):
                    artifact_path(self.mockups, path)

    async def test_nested_scope_and_back_restore_selection_folds_and_context(self):
        """Entering nested universes and coming back restores each one's title, columns, widgets, selection, folds and
        search, and keys typed into a draft there never navigate; kept for the lab finding it guards, since a lost
        selection on return is easy to reintroduce when the scope stack changes (LAB-UI001, UI-01)."""
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
            await pilot.press("g", "ctrl+u", *"temporary")
            self.assertEqual(self.app.draft.scope, self.cad)
            await pilot.press("backspace")
            self.assertEqual(self.app.root, self.cad, "Editing a draft must not navigate back")
            await pilot.press("escape")
            self.assertIsNone(self.app.draft)
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

    async def test_widget_failures_never_keep_the_agent_table_from_filling(self):
        """Bad view JSON, a widget type or universe id that is not a string, table rows that do not match, a missing
        artifact, and a widget or show_view that raises all leave the agent rows refreshed and the error on show,
        never "Could not refresh"; fragile because view files are hand- or agent-written JSON that is indexed directly,
        and a break empties the whole agent list for that universe (UI-01, 229eeda)."""
        identity = (self.root / "universe.json").read_text()
        table = {"type": "table", "title": "Queue", "columns": ["ID"], "rows": [["UI-01"]]}
        async with self.app.run_test(size=(150, 35)) as pilot:
            await self.settle(pilot)
            for failure, view, patched, expected in (
                    ("invalid JSON", "{", None, ["View configuration error"]),
                    ("a widget type that is not a string",
                     {"widgets": [{"type": ["markdown"]}, {"type": {"x": 1}}, {"type": "markdown", "text": "kept"}]},
                     None, ["Widget 1 error", "Widget 2 error", "unknown widget type", "kept"]),
                    ("a universe id that is not a string", {"widgets": [table]}, None, ["View configuration error"]),
                    ("table rows that do not match its columns",
                     {"widgets": [{"type": "table", "columns": ["a"], "rows": [["a", "b"]]}]}, None, ["match columns"]),
                    ("a widget that raises", {"widgets": [table]},
                     patch.object(TablePanel, "compose", side_effect=RuntimeError("compose broke")), ["compose broke"]),
                    ("show_view raising", {"widgets": [table]},
                     patch.object(LocalView, "show_view", side_effect=RuntimeError("panel broke")),
                     ["Could not show this view's widgets: panel broke"]),
                    ("a missing artifact", {"widgets": [{"type": "artifacts", "items": [{"path": "missing.svg"}]}]},
                     None, [])):
                with self.subTest(failure):
                    self.app.set_status("")
                    self.write_view(self.root, view)
                    (self.root / "universe.json").write_text(
                        json.dumps({"schema": 1, "id": 5, "name": "lab", "workspace": "."})
                        if failure.startswith("a universe id") else identity)
                    with patched or nullcontext():
                        before = self.app.scan_count
                        self.app.refresh_snapshot()
                        await self.settle(pilot)
                        shown = self.shown()
                        self.assertGreater(self.app.scan_count, before)
                        self.assertNotIn("Could not refresh", shown)
                        self.assertGreater(self.app.query_one("#agents", DataTable).row_count, 0)
                        for text in expected:
                            self.assertIn(text, shown)
                    if failure == "a missing artifact":
                        self.app.query_one("#artifact-0-0").focus()
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIn("missing", str(self.app.screen.query_one("#artifact-error", Static).render()))
                        await pilot.press("escape")

if __name__ == "__main__":
    unittest.main()
