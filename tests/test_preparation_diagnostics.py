"""Bad definitions are named without hiding unrelated agent problems."""
import contextlib
import io
import sys
from unittest.mock import patch
from multiverse import cli
from multiverse.store import uid, write
from multiverse.tools import doctor
from multiverse.universe import agents
from test_multiverse import BaseTest


class PreparationDiagnosticTest(BaseTest):
    def test_bad_harness_doctor_names_each_file_and_keeps_other_problems(self):
        paths = [self.root / 'harnesses' / name / 'harness.json' for name in ('broken-one', 'broken-two')]
        for path in paths:
            path.parent.mkdir()
            path.write_text('{"schema":\n')
        directory = self.root / 'state' / 'agents' / uid()
        write(directory / 'properties.json', {'id': directory.name, 'name': 'other', 'harness': 'claude',
            'workspace': str(self.work / 'missing-workspace')})
        problems = doctor.run(self.root)['problems']
        for path in paths:
            self.assertTrue(any(str(path) in reason for reason in problems), problems)
        self.assertTrue(any('other' in reason and 'missing-workspace' in reason for reason in problems), problems)

    def test_unknown_harness_spawn_error_is_readable_and_named(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             patch.object(sys, 'argv', ['mv2', '-u', str(self.root), 'spawn', '--name', 'new', '--harness', 'absent']):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('new did not start', err.getvalue())
        self.assertIn('Unknown harness', err.getvalue())
        self.assertIn(str(self.root / 'harnesses' / 'absent' / 'harness.json'), err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())

    def test_missing_peer_properties_advice_says_restore_or_remove(self):
        directory = self.root / 'state' / 'agents' / uid()
        directory.mkdir()
        with self.assertRaises(ValueError) as caught:
            agents.check_available_name(self.root, 'new')
        self.assertIn(directory.name, str(caught.exception))
        self.assertIn('restore or remove', str(caught.exception))
