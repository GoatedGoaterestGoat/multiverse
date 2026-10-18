"""Well-formed terminal references cannot hide an attested running launch."""
from unittest.mock import patch

from multiverse.runtime import attachments, lifecycle, missing_runtime
from multiverse.store import read, uid, write
from test_multiverse import BaseTest


class TerminalMismatchProofTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / uid()
        self.token = 'Sat Oct 17 12:00:00 2026'
        self.facts = {'launch_id': uid(), 'state': 'running', 'pid': 1234, 'pid_started': self.token,
                      'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}}

    def test_only_a_live_matching_process_makes_a_record_inconsistent(self):
        """Only a live process with the saved start time makes a dormant-looking record inconsistent, a cleaning runner
        whose child exited does not, and healthy and teardown states never query processes; fragile both ways, since
        a false negative hides a running launch and a false positive blocks healthy agents after every exit
        (02569a6)."""
        cases = [('matching', (False, (self.token, 'S')), True),
                 ('absent', (True, None), False), ('unknown', (False, None), False),
                 ('zombie', (False, (self.token, 'Z')), False),
                 ('dead', (False, (self.token, 'X')), False),
                 ('reused', (False, ('Sat Oct 17 12:01:00 2026', 'S')), False)]
        for label, observation, expected in cases:
            with self.subTest(label=label), patch.object(attachments, '_observation', return_value=observation):
                error = missing_runtime.mismatch(self.directory, self.facts, {'live': 'dormant'})
                self.assertEqual(error is not None, expected)
                if error:
                    self.assertEqual(error.file, self.directory / 'runtime.json')
                    self.assertEqual(error.recover, 'kill')
        for token in (None, '', 'invalid'):
            with self.subTest(token=token), patch.object(attachments, '_observation',
                                                       return_value=(False, (self.token, 'S'))):
                facts = {**self.facts, 'pid_started': token}
                self.assertIsNone(missing_runtime.mismatch(self.directory, facts, {'live': 'dormant'}))
        runner = {**self.facts, 'runner_pid': 5678, 'runner_started': self.token}
        for child in ((True, None), (False, (self.token, 'Z')), (False, ('Sat Oct 17 12:01:00 2026', 'S'))):
            with self.subTest(child=child, runner='live'), patch.object(attachments, '_observation',
                    side_effect=lambda pid, cache: child if pid == 1234 else (False, (self.token, 'S'))):
                self.assertIsNone(missing_runtime.mismatch(self.directory, runner, {'live': 'dormant'}))
        with self.subTest(child='unknown', runner='live'), patch.object(attachments, '_observation',
                side_effect=lambda pid, cache: (False, None) if pid == 1234 else (False, (self.token, 'S'))):
            self.assertIsNotNone(missing_runtime.mismatch(self.directory, runner, {'live': 'dormant'}))
        with patch.object(attachments, '_observation', side_effect=AssertionError('no process query')):
            for live in ('alive', 'unknown', 'ending', 'stopped'):
                with self.subTest(live=live):
                    self.assertIsNone(missing_runtime.mismatch(self.directory, self.facts, {'live': live}))
            for extra in ({'state': 'stopped'}, {'state': 'dormant'}, {'state': 'failed'},
                          {'state': 'ending'}, {'exit_code': 0}, {'ending_at': 'now'}, {'stopped_at': 'now'}):
                with self.subTest(extra=extra):
                    self.assertIsNone(missing_runtime.mismatch(self.directory, {**self.facts, **extra},
                                                               {'live': 'dormant'}))

    def test_kill_rechecks_process_proof_under_the_lock(self):
        """kill rechecks process proof under the launch lock: proof that is gone by then recovers nothing, and a
        terminal healed by then takes the normal owned path; fragile because acting on a stale snapshot skips closing
        a live pane or recovers without authority (02569a6)."""
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'worker', 'harness': 'codex',
              'universe_id': read(self.root / 'universe.json')['id'], 'workspace': str(self.work)})
        write(self.directory / 'runtime.json', self.facts)
        before = (self.directory / 'runtime.json').read_bytes()
        with self.subTest('proof gone'), \
             patch.object(missing_runtime, 'observe', return_value={'live': 'dormant'}), \
             patch.object(attachments, '_observation', return_value=(False, None)), \
             patch.object(missing_runtime, 'discover', side_effect=AssertionError('no recovery authority')):
            self.assertIsNone(missing_runtime.kill(self.root, self.directory, attachment_only=True))
            self.assertEqual((self.directory / 'runtime.json').read_bytes(), before)
        with self.subTest('terminal healed'), \
             patch.object(attachments, '_observation', return_value=(False, (self.token, 'S'))), \
             patch.object(lifecycle, 'observe', side_effect=[{'live': 'dormant'}, {'live': 'alive'}]), \
             patch.object(missing_runtime, 'observe', return_value={'live': 'alive'}), \
             patch.object(lifecycle, 'owned', side_effect=RuntimeError('normal owned terminal reached')) as owned:
            with self.assertRaisesRegex(RuntimeError, 'normal owned terminal reached'):
                lifecycle.kill(self.root, self.directory.name, attachment_only=True)
            owned.assert_called_once()
