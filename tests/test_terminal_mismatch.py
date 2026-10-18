"""Well-formed terminal references cannot hide an attested running launch."""
import fcntl
import json
import os
import subprocess
import sys
from unittest.mock import patch

from multiverse.runtime import attachments, lifecycle, missing_runtime, native
from multiverse.store import read, uid, write
from multiverse.tools import doctor, list_agents, send
from multiverse.universe import records, relocate
from test_multiverse import BaseTest, ROOT, TerminalBase


class TerminalMismatchTest(TerminalBase):
    def started(self, name='worker'):
        result = lifecycle.start(self.root, name, '', 'fake', socket_path=self.socket,
                                 wait=True, wait_timeout=15)
        self.assertTrue(result['ready'], result)
        directory = self.root / 'state/agents' / result['id']
        facts = read(directory / 'runtime.json')
        self.assertTrue(facts.get('pid_started'), facts)
        self.addCleanup(write, directory / 'runtime.json', facts)
        return result, directory, facts

    def test_wrong_pane_or_socket_is_named_damage_and_refuses_resume_before_reconciliation(self):
        result, directory, original = self.started()
        path = directory / 'runtime.json'
        for field, value in (('pane', '%999'), ('socket', str(self.work / 'absent.sock'))):
            with self.subTest(field=field):
                write(path, {**original, 'tmux': {**original['tmux'], field: value}})
                before = path.read_bytes()
                row = lifecycle.listing(self.root, only=result['id'])[0]
                self.assertIn('damaged', row, row)
                self.assertIn('runtime.json', row['damaged']['file'])
                self.assertTrue(any('worker' in p and 'runtime.json' in p
                                    for p in doctor.run(self.root)['problems']))
                with patch.object(native, 'session_snapshot', side_effect=AssertionError('no reconciliation')):
                    with self.assertRaisesRegex(records.RecordError, 'runtime.json.*Restore.*mv2 kill'):
                        lifecycle.start(self.root, resume='worker', socket_path=self.socket)
                self.assertEqual(path.read_bytes(), before)
        panes, error = self.backend.snapshot()
        self.assertIsNone(error)
        self.assertEqual(sum(p.get('@mv2_agent_id') == result['id'] for p in panes.values()), 1)

    def test_kill_recovers_the_exact_launch_after_a_valid_looking_wrong_terminal(self):
        other = lifecycle.start(self.root, 'other', '', 'fake', socket_path=self.socket, wait=True)
        for field, value in (('pane', '%999'), ('socket', str(self.work / 'absent.sock'))):
            with self.subTest(field=field):
                result, directory, original = self.started('worker-' + field)
                write(directory / 'runtime.json', {**original, 'tmux': {**original['tmux'], field: value}})
                with patch.dict(os.environ, {'MV2_TMUX_SOCKET': self.socket}):
                    stopped = lifecycle.kill(self.root, 'worker-' + field, attachment_only=True)
                self.assertEqual(stopped['state'], 'dormant')
                panes, error = self.backend.snapshot()
                self.assertIsNone(error)
                self.assertFalse(any(p.get('@mv2_agent_id') == result['id'] for p in panes.values()))
                self.assertTrue(any(p.get('@mv2_agent_id') == other['id'] for p in panes.values()))
                resumed = lifecycle.start(self.root, resume='worker-' + field, socket_path=self.socket, wait=True)
                self.assertTrue(resumed['ready'], resumed)

    def test_invalid_pid_after_stop_has_the_same_actionable_repair_advice(self):
        _, directory, _ = self.started()
        lifecycle.kill(self.root, 'worker', attachment_only=True)
        path = directory / 'runtime.json'
        write(path, {**read(path), 'pid': 'abc'})
        before = path.read_bytes()
        for operation in (lambda: lifecycle.start(self.root, resume='worker', socket_path=self.socket),
                          lambda: lifecycle.kill(self.root, 'worker')):
            with self.subTest(operation=operation), self.assertRaises(records.RecordError) as caught:
                operation()
            self.assertIn('remove', str(caught.exception))
            self.assertIn('runtime.json', str(caught.exception))
            self.assertEqual(path.read_bytes(), before)

    def test_recovery_never_adopts_a_different_valid_generation(self):
        result, directory, original = self.started()
        foreign = uid()
        request = read(directory / 'launches' / original['launch_id'] / 'request.json')
        write(directory / 'launches' / foreign / 'request.json', request)
        pane = original['tmux']['pane']
        self.backend.run('set-option', '-p', '-t', pane, '@mv2_launch_id', foreign)
        self.addCleanup(self.backend.run, 'set-option', '-p', '-t', pane,
                        '@mv2_launch_id', original['launch_id'])
        before = (directory / 'runtime.json').read_bytes()
        with self.assertRaisesRegex(records.RecordError, 'different saved launch'):
            lifecycle.kill(self.root, result['id'], attachment_only=True)
        self.assertEqual((directory / 'runtime.json').read_bytes(), before)
        self.assertIn(pane, self.backend.snapshot()[0])

    def test_wake_and_removal_refuse_inconsistent_terminal_without_reconciliation(self):
        _, directory, original = self.started()
        path = directory / 'runtime.json'
        write(path, {**original, 'tmux': {**original['tmux'], 'pane': '%999'}})
        before = path.read_bytes()
        with patch.object(native, 'session_snapshot', side_effect=AssertionError('no reconciliation')):
            result = send.run(self.root, agent='worker', text='do not create another launch')
        self.assertFalse(result['ok'], result)
        self.assertIn('runtime.json', result['native_delivery']['reason'])
        self.assertIn('mv2 kill', result['native_delivery']['reason'])
        with self.assertRaisesRegex(records.RecordError, 'runtime.json.*inconsistent'):
            relocate.remove_agent(self.root, 'worker')
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(directory.exists())
        self.assertFalse((self.root / 'trash').exists())


class TerminalMismatchProofTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / uid()
        self.token = 'Sat Oct 17 12:00:00 2026'
        self.facts = {'launch_id': uid(), 'state': 'running', 'pid': 1234, 'pid_started': self.token,
                      'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}}

    def test_only_matching_live_process_identity_establishes_inconsistency(self):
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

    def test_healthy_unknown_and_teardown_paths_do_not_query_processes(self):
        with patch.object(attachments, '_observation', side_effect=AssertionError('no process query')):
            for live in ('alive', 'unknown', 'ending', 'stopped'):
                self.assertIsNone(missing_runtime.mismatch(self.directory, self.facts, {'live': live}))
            for extra in ({'state': 'stopped'}, {'state': 'dormant'}, {'state': 'failed'},
                          {'state': 'ending'}, {'exit_code': 0}, {'ending_at': 'now'}, {'stopped_at': 'now'}):
                self.assertIsNone(missing_runtime.mismatch(self.directory, {**self.facts, **extra}, {'live': 'dormant'}))

    def test_exited_child_does_not_turn_its_cleaning_runner_into_damage(self):
        facts = {**self.facts, 'runner_pid': 5678, 'runner_started': self.token}
        for child in ((True, None), (False, (self.token, 'Z')),
                      (False, ('Sat Oct 17 12:01:00 2026', 'S'))):
            with self.subTest(child=child), patch.object(attachments, '_observation',
                    side_effect=lambda pid, cache: child if pid == 1234 else (False, (self.token, 'S'))):
                self.assertIsNone(missing_runtime.mismatch(self.directory, facts, {'live': 'dormant'}))
        with patch.object(attachments, '_observation',
                side_effect=lambda pid, cache: (False, None) if pid == 1234 else (False, (self.token, 'S'))):
            self.assertIsNotNone(missing_runtime.mismatch(self.directory, facts, {'live': 'dormant'}))

    def test_recovery_rechecks_positive_process_proof_under_the_lock(self):
        self.directory.mkdir(parents=True)
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'worker', 'harness': 'codex'})
        write(self.directory / 'runtime.json', self.facts)
        before = (self.directory / 'runtime.json').read_bytes()
        with patch.object(missing_runtime, 'observe', return_value={'live': 'dormant'}), \
             patch.object(attachments, '_observation', return_value=(False, None)), \
             patch.object(missing_runtime, 'discover', side_effect=AssertionError('no recovery authority')):
            self.assertIsNone(missing_runtime.kill(self.root, self.directory, attachment_only=True))
        self.assertEqual((self.directory / 'runtime.json').read_bytes(), before)


class StartingJsonTest(BaseTest):
    def test_cli_json_shows_starting_without_changing_the_dashboard_contract(self):
        directory, properties, _, _ = lifecycle._record(self.root, 'newcomer', '', 'codex', None, None)
        write(directory / 'properties.json', properties)
        write(directory / 'runtime.json', {'launch_id': uid(), 'state': 'starting',
              'status': 'unknown', 'preparing': True})
        with (directory / '.launch.lock').open('w+') as publisher:
            fcntl.flock(publisher, fcntl.LOCK_EX)
            core = list_agents.run(self.root, json=True)[0]
            self.assertEqual(core['live'], 'unknown')
            self.assertIn('starting', list_agents.run(self.root))
            response = subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root),
                                       'list', '--json'], capture_output=True, text=True, timeout=10)
            self.assertEqual(response.returncode, 0, response.stderr)
            self.assertEqual(json.loads(response.stdout)[0]['live'], 'starting')
