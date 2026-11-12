"""Default dashboard launch and first-use initialization."""
from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from multiverse import cli, universe
from multiverse.universe import templates
from multiverse.store import write
from isolation import isolate
from test_multiverse import BaseTest


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
            with self.subTest('an explicit child path must not select its parent'):
                with self.assertRaises(universe.UniverseNotFoundError):
                    self.launch('-u', str(child))
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


class ExplicitUniverseTest(BaseTest):
    def invoke(self, selected, *command, standalone=False):
        from multiverse.tools import _common, list_agents
        stdout, stderr, code = io.StringIO(), io.StringIO(), 0
        with patch.object(sys, 'argv', ['mv2', '-u', str(selected), *command]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                if standalone:
                    _common.main(list_agents.configure, list_agents.run)
                else:
                    cli.main()
            except SystemExit as stopped:
                code = stopped.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_explicit_selection_never_runs_in_an_enclosing_universe(self):
        """A mistyped explicit universe must not read, migrate or mutate its otherwise valid parent."""
        self.enterContext(patch.dict(os.environ, {'MV2_UNIVERSE': str(self.root)}))
        folder = self.work / 'ordinary'
        folder.mkdir()
        abandoned = self.root / 'subverses/abandoned'
        (abandoned / 'state/agents').mkdir(parents=True)
        loop = self.work / 'loop'
        loop.symlink_to(loop.name)
        malformed = self.work / 'unsupported'
        write(malformed / 'universe.json', {'schema': 999})
        for index, selected in enumerate((folder, folder / 'missing', abandoned, loop, malformed, '')):
            for standalone in (False, True):
                with self.subTest(selected=selected, standalone=standalone), \
                        patch.object(cli, 'first_use_migration', wraps=cli.first_use_migration) as migration:
                    command = () if standalone else ('list', '--json')
                    code, out, err = self.invoke(selected, *command, standalone=standalone)
                    self.assertEqual(code, 1, (out, err))
                    self.assertIn(str(selected), err)
                    migration.assert_not_called()
            with self.subTest(selected=selected, operation='create'):
                name = 'wrong-target-' + str(index)
                code, out, err = self.invoke(selected, 'subverse', name)
                self.assertEqual(code, 1, (out, err))
                self.assertFalse((self.root / 'subverses' / name).exists())
        with patch('multiverse.plugins.commands') as commands:
            self.assertEqual(cli.plugin_commands(['-u', str(folder), 'echo']), (None, {}))
            commands.assert_not_called()
        child = templates.create(self.root / 'subverses/valid', 'valid', workdir=self.work)
        for selected in (self.root, self.work, child):
            with self.subTest(selected=selected, valid=True):
                self.assertEqual(self.invoke(selected, 'list', '--json')[0], 0)
        # An explicit selection retains the same bounded recovery of a mid-replace marker.
        marker, stat, missing = child / 'universe.json', Path.stat, [2]

        def gap(path, *args, **kwargs):
            if path == marker and missing[0]:
                missing[0] -= 1
                raise FileNotFoundError(2, 'replacement gap', str(path))
            return stat(path, *args, **kwargs)

        with patch.object(Path, 'stat', gap):
            self.assertEqual(self.invoke(child, 'list', '--json')[0], 0)
        self.assertEqual(missing[0], 0)
        # A selection must not silently change between validation and the tool's own fresh read.
        from multiverse.tools import subverse
        for stage in ('migration', 'dispatch'):
            with self.subTest(removed_during=stage):
                selected = templates.create(self.root / 'subverses' / stage, stage, workdir=self.work)
                nested = templates.create(selected / '.multiverse', 'decoy', workdir=self.work)
                module, function = (cli, 'first_use_migration') if stage == 'migration' else (subverse, 'run')
                original = getattr(module, function)

                def removed(*args, **kwargs):
                    (selected / 'universe.json').unlink()
                    return original(*args, **kwargs)

                with patch.object(module, function, side_effect=removed):
                    code, out, err = self.invoke(selected, 'subverse', 'wrong-after-' + stage)
                self.assertEqual(code, 1, (out, err))
                self.assertFalse((self.root / 'subverses' / ('wrong-after-' + stage)).exists())
                self.assertFalse((nested / 'subverses' / ('wrong-after-' + stage)).exists())
                self.assertEqual(universe.resolve(selected / 'state'), nested)  # Selection scope ended.
