"""A native refusal is primary and latched until a new mv2 launch."""
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import Mock, patch

from multiverse.runtime import lifecycle, native, refusal, runner
from multiverse.store import read, write
from multiverse.universe import agents
from multiverse.universe.mailbox import append_message, message_record, update_message
from multiverse.tools import inspect, list_agents, doctor
from test_multiverse import BaseTest


class LaunchRefusalTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.identity, self.thread, self.launch = (str(uuid4()) for _ in range(3))
        self.directory = self.root / 'state' / 'agents' / self.identity
        write(self.directory / 'properties.json', {'id': self.identity, 'name': 'refused', 'harness': 'codex',
              'workspace': str(self.work), 'native_session_id': self.thread})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'starting',
                                              'startup_delivery': 'waiting'})
        self.evidence = {'source': 'native-rpc', 'method': 'thread/resume', 'code': -32600,
                         'message': 'This conversation is open in another app', 'native_id': self.thread}

    def test_screen_observer_stops_after_validated_bridge_binding_but_observes_plain(self):
        for readiness, relay, observe in (('binding', 'ready', False), ('legacy-binding', 'ready', False),
                                           ('binding', 'starting', True), ('process', 'ready', True)):
            with self.subTest(readiness=readiness, relay=relay):
                write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
                      'attachment': {'readiness': readiness}, 'native_relay': {'state': relay},
                      'native_versions': {'cli': '0.163.0'}})
                describe = Mock(return_value=self.evidence)
                monitor = runner.RefusalMonitor(self.directory, self.launch, self.root,
                                                SimpleNamespace(launch_refusal=describe), 'fixture')
                backend = Mock()
                backend.run.side_effect = ['native widget', '0']
                with patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')) as owned:
                    monitor._run()
                self.assertEqual(describe.called, observe)
                self.assertEqual(owned.called, observe)

    def test_first_native_refusal_wins_over_r_retry_exit_and_relay_diagnostics(self):
        refusal.record(self.directory, self.launch, self.evidence)
        agents.update_runtime(self.directory, self.launch, state='running', reason='generic child exited',
                              error='relay lost', native_refusal=None)
        refusal.record(self.directory, self.launch, {**self.evidence, 'message': 'later retry failed'})
        facts = agents.runtime(self.directory)
        self.assertEqual(facts['reason'], self.evidence['message'])
        self.assertEqual(facts['native_refusal']['code'], -32600)
        self.assertFalse(lifecycle.launch_ready(object(), {}, facts, self.directory, {}, 'alive'))

    def test_definite_startup_queue_fails_but_a_registered_wake_prompt_stays_uncertain(self):
        queued = append_message(self.directory, 'not submitted', {}, delivery='steer', target_session=self.thread)
        update_message(self.directory, queued['id'], submission_state='pending', deferred_launch_id=self.launch)
        wake = append_message(self.directory, 'possibly submitted', {}, delivery='steer', target_session=self.thread)
        update_message(self.directory, wake['id'], submission_state='submitting', delivery_method='resume_prompt',
                       wake_launch_id=self.launch, attempts=[{'state': 'unknown'}])
        refusal.record(self.directory, self.launch, self.evidence)
        self.assertEqual(message_record(self.directory, queued['id'])[1]['submission_state'], 'failed')
        self.assertEqual(message_record(self.directory, wake['id'])[1]['submission_state'], 'unknown')
        self.assertEqual(message_record(self.directory, wake['id'])[1]['attempts'][-1]['reason'], self.evidence['message'])

    def test_new_input_is_saved_with_native_cause_without_native_submission(self):
        refusal.record(self.directory, self.launch, self.evidence)
        message = append_message(self.directory, 'later', {}, delivery='steer', target_session=self.thread)
        with patch.object(native, 'owned'), patch.object(native, 'observe', return_value={'live': 'alive'}):
            result = native.deliver(self.root, self.identity, message['id'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], self.evidence['message'])
        self.assertEqual(message_record(self.directory, message['id'])[1]['submission_state'], 'failed')

    def test_stale_or_foreign_evidence_cannot_change_this_launch(self):
        for launch, evidence in ((str(uuid4()), self.evidence),
                                 (self.launch, {**self.evidence, 'native_id': str(uuid4())})):
            with self.assertRaises(ValueError):
                refusal.record(self.directory, launch, evidence)
        self.assertNotIn('native_refusal', agents.runtime(self.directory))

    def test_output_uses_native_cause_even_when_a_later_connection_diagnostic_exists(self):
        refusal.record(self.directory, self.launch, self.evidence)
        facts = agents.runtime(self.directory)
        row = {**read(self.directory / 'properties.json'), 'runtime': facts, 'live': 'alive',
               'status': 'unknown', 'phase': 'working', 'universe': str(self.root),
               'connection_error': 'generic relay lost', 'launch_error': self.evidence['message'],
               'reason': self.evidence['message']}
        with patch.object(inspect, 'facts', return_value=row), patch.object(inspect, 'run', return_value='screen'):
            self.assertEqual(inspect.cli_run(self.root, agent='refused', json=True)['error'], self.evidence['message'])
        with patch.object(lifecycle, 'listing', return_value=[row]):
            self.assertNotIn('generic relay', list_agents.run(self.root))
            self.assertIn(self.evidence['message'], list_agents.run(self.root))
            problems = doctor.run(self.root)['problems']
            self.assertTrue(any(self.evidence['message'] in problem for problem in problems))
            self.assertFalse(any('generic relay' in problem for problem in problems))
