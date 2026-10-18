"""Default dashboard launch and first-use initialization."""
from contextlib import chdir
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from multiverse import cli, universe
from multiverse.universe import templates
from multiverse.store import write
from isolation import isolate


class DefaultDashboardTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.enterContext(chdir(self.work))
        self.enterContext(patch.dict(os.environ, {"MV2_UNIVERSE": ""}))
        self.tui = self.enterContext(patch("multiverse.ui.app.start"))

    def launch(self, *args):
        cli.run(cli.parser().parse_args(args))

    def test_bare_mv2_never_initializes_over_or_under_an_existing_universe(self):
        """Bare mv2 never initializes over an existing .multiverse, nests a universe under a parent's, or creates a
        missing -u target; fragile because it is the one command that writes a universe unasked, and a shadowing
        universe looks normal while hiding agents' records."""
        with self.subTest('a parent universe is reused, not rewritten or nested'):
            parent = self.work / "parent"
            root = templates.create(parent / ".multiverse", "existing", workdir=parent)
            before = (root / "universe.json").read_bytes()
            child = parent / "src"
            child.mkdir()
            with chdir(child):
                self.launch()
            self.tui.assert_called_once_with(root)
            self.assertEqual((root / "universe.json").read_bytes(), before)
            self.assertFalse((child / ".multiverse").exists())
            self.tui.reset_mock()
        with self.subTest('a missing selection is not created'):
            missing = self.work / "missing"
            with self.assertRaises(universe.UniverseNotFoundError):
                self.launch("-u", str(missing))
            with patch.dict(os.environ, {"MV2_UNIVERSE": str(missing)}):
                with self.assertRaises(universe.UniverseNotFoundError):
                    self.launch()
            self.assertFalse(missing.exists())
        with self.subTest('existing state that is not a universe is not replaced'):
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
