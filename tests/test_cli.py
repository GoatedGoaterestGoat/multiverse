"""Default dashboard launch and first-use initialization."""
from contextlib import chdir
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from multiverse import cli, universe
from multiverse.universe import templates
from multiverse.store import read, write


class DefaultDashboardTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.enterContext(chdir(self.work))
        self.enterContext(patch.dict(os.environ, {"MV2_UNIVERSE": ""}))
        self.tui = self.enterContext(patch("multiverse.ui.app.start"))

    def launch(self, *args):
        cli.run(cli.parser().parse_args(args))

    def test_no_command_initializes_defaults_and_opens_dashboard_without_agents(self):
        self.launch()
        root = self.work / ".multiverse"
        self.tui.assert_called_once_with(root)
        self.assertEqual(universe.workspace(root), self.work)
        self.assertEqual(read(root / "harnesses/codex/harness.json")["settings"], {})
        self.assertEqual(list((root / "state/agents").iterdir()), [])

    def test_no_command_reuses_parent_universe_without_rewriting_it(self):
        root = templates.create(self.work / ".multiverse", "existing", workdir=self.work)
        before = (root / "universe.json").read_bytes()
        child = self.work / "src"
        child.mkdir()
        with chdir(child):
            self.launch()
        self.tui.assert_called_once_with(root)
        self.assertEqual((root / "universe.json").read_bytes(), before)
        self.assertFalse((child / ".multiverse").exists())

    def test_explicit_and_environment_selections_are_respected(self):
        selected = templates.create(self.work / "selected", "selected", workdir=self.work)
        other = templates.create(self.work / "other", "other", workdir=self.work)
        with patch.dict(os.environ, {"MV2_UNIVERSE": str(other)}):
            self.launch("-u", str(selected))
            self.tui.assert_called_with(selected)
            self.launch()
            self.tui.assert_called_with(other)
        self.assertFalse((self.work / ".multiverse").exists())

    def test_invalid_selection_or_existing_state_is_not_replaced(self):
        missing = self.work / "missing"
        with self.assertRaises(universe.UniverseNotFoundError):
            self.launch("-u", str(missing))
        with patch.dict(os.environ, {"MV2_UNIVERSE": str(missing)}):
            with self.assertRaises(universe.UniverseNotFoundError):
                self.launch()
        self.assertFalse(missing.exists())
        root = self.work / ".multiverse"
        root.mkdir()
        sentinel = root / "keep.txt"
        sentinel.write_text("existing data")
        with self.assertRaises(ValueError):
            self.launch()
        write(root / "universe.json", {"schema": 999})
        with self.assertRaisesRegex(ValueError, "Unsupported universe schema"):
            self.launch()
        self.assertEqual(sentinel.read_text(), "existing data")
        self.tui.assert_not_called()

    def test_explicit_tui_and_help_do_not_initialize(self):
        with self.assertRaises(universe.UniverseNotFoundError):
            self.launch("tui")
        with patch("sys.stdout"), self.assertRaises(SystemExit) as raised:
            cli.parser().parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertFalse((self.work / ".multiverse").exists())
        self.tui.assert_not_called()
