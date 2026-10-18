"""A native refusal is primary and latched until a new mv2 launch."""
from uuid import uuid4
import json
import inspect as python_inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from multiverse.runtime import lifecycle, native, refusal, runner
from multiverse.store import read, write
from multiverse.universe import agents
from multiverse.universe.mailbox import append_message, message_record, update_message
from multiverse.tools import inspect, list_agents, doctor
from multiverse.harnesses.codex import CodexAdapter
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
        (self.directory / 'launches' / self.launch).mkdir(parents=True)
        self.evidence = {'source': 'native-rpc', 'method': 'thread/resume', 'code': -32600,
                         'message': 'This conversation is open in another app', 'native_id': self.thread}

    def test_initial_wait_cannot_return_ready_before_a_later_native_refusal(self):
        facts = {'launch_id': self.launch, 'state': 'running', 'pid': 123, 'startup_checked': True,
                 'native_startup_observation': {'state': 'pending'},
                 'attachment': {'readiness': 'process'}}
        write(self.directory / 'runtime.json', facts)
        write(self.directory / 'launches' / self.launch / 'request.json', {'attachment': facts['attachment']})
        original = lifecycle.launch_result
        calls = []
        def sample(root, directory):
            if calls:
                refusal.record(self.directory, self.launch, self.evidence)
            calls.append(True)
            return original(root, directory)
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        with patch.object(lifecycle, 'launch_result', side_effect=sample), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 1)
        self.assertFalse(result['ready'], result)
        self.assertIn(self.evidence['message'], result['error'])
        self.assertGreaterEqual(len(calls), 2)

    def test_settled_screen_states_keep_process_checks_and_explain_unknown_layout(self):
        attachment = {'readiness': 'process'}
        request = {'attachment': attachment}
        write(self.directory / 'launches' / self.launch / 'request.json', request)
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        for state in ('started', 'unsupported', 'unverified'):
            with self.subTest(state=state):
                facts = {'launch_id': self.launch, 'state': 'running', 'pid': 123, 'startup_checked': True,
                         'native_startup_observation': {'state': state}, 'attachment': attachment}
                write(self.directory / 'runtime.json', facts)
                with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                    result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 1)
                self.assertTrue(result['ready'])
                self.assertEqual(bool(result.get('notices')), state == 'unverified')
                if state == 'unverified':
                    self.assertIn('could not be confirmed', result['notices'][0])
                self.assertFalse(lifecycle.launch_ready(adapter, {}, {**facts, 'pid': None}, self.directory, request, 'alive'))
                self.assertFalse(lifecycle.launch_ready(adapter, {}, facts, self.directory, request, 'dormant'))

    def test_observer_startup_result_is_generation_fenced_and_does_not_recreate_removed_agent(self):
        facts = {'launch_id': self.launch, 'state': 'running', 'attachment': {'readiness': 'process'}}
        agents.update_runtime(self.directory, self.launch, **{key: value for key, value in facts.items() if key != 'launch_id'})
        monitor = runner.RefusalMonitor(self.directory, self.launch, self.root,
                                        SimpleNamespace(launch_refusal=lambda evidence: None), 'fixture')
        with patch.object(lifecycle, 'note_ready'):
            monitor._settled('started')
        self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'started')
        write(self.directory / 'runtime.json', {**facts, 'launch_id': 'replacement'})
        monitor._settled('unverified')
        self.assertNotIn('native_startup_observation', agents.runtime(self.directory))
        import shutil
        shutil.rmtree(self.directory)
        monitor._settled('started')
        self.assertFalse(self.directory.exists())

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

    def observe_native_frames(self, first_name, started=None):
        fixtures = Path(__file__).parent / 'fixtures/codex'
        first = json.loads((fixtures / first_name).read_text())[0]
        refused = json.loads((fixtures / 'refusal-0.163.0.json').read_text())[0]
        class Clock:
            elapsed = 0.
            def is_set(self):
                return self.elapsed >= 75
            def wait(self, seconds):
                self.elapsed += seconds
        clock = Clock()
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
              'startup_checked': True, 'attachment': {'readiness': 'process'},
              'native_versions': {'cli': '0.163.0'}})
        agents.update_runtime(self.directory, self.launch, native_startup_observation={'state': 'pending'})
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
        monitor.stopped = clock
        if started is not None:
            monitor.started = started
        backend = Mock()
        def command(name, *args):
            frame = first if clock.elapsed < 10 else refused
            if name == 'capture-pane':
                return frame['text']
            if name == 'display-message':
                flag = '1' if frame['cursor_visible'] else '0'
                # Also reproduce against the old flag-only capture path.
                return (flag + '\t' + str(frame.get('cursor_x', 0)) + '\t' + str(frame.get('cursor_y', 0))
                        if 'cursor_x' in args[-1] else flag)
            self.fail('Unexpected terminal operation: ' + name)
        backend.run.side_effect = command
        with patch.object(runner.time, 'monotonic', side_effect=lambda: clock.elapsed), \
                patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')), \
                patch.object(refusal, 'record', wraps=refusal.record) as recorded:
            monitor._run()
        return clock, backend, recorded

    def test_successful_plain_composer_ends_observation_before_later_navigation_refusal(self):
        clock, backend, recorded = self.observe_native_frames('started-0.163.0.json')
        recorded.assert_not_called()
        self.assertEqual(backend.run.call_count, 2, 'Successful composer should end all pane polling')
        self.assertEqual(clock.elapsed, 0)
        self.assertNotIn('native_refusal', agents.runtime(self.directory))
        self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'started')

    def test_native_loading_composer_does_not_hide_a_real_initial_refusal(self):
        for first in ('loading-0.163.0.json', 'resuming-0.163.0.json'):
            with self.subTest(first=first):
                clock, backend, recorded = self.observe_native_frames(first)
                recorded.assert_called_once()
                self.assertEqual(clock.elapsed, 10)
                self.assertIn('open in another app', agents.runtime(self.directory)['reason'])

    def test_startup_hook_needs_boolean_true_and_never_overrides_a_native_refusal(self):
        for answer in (1, 'ready', {'ready': True}):
            with self.subTest(answer=answer):
                clock, _, recorded = self.observe_native_frames('started-0.163.0.json', Mock(return_value=answer))
                recorded.assert_called_once()
                self.assertEqual(clock.elapsed, 10)
        success = Mock(return_value=True)
        clock, _, recorded = self.observe_native_frames('refusal-0.163.0.json', success)
        recorded.assert_called_once()
        self.assertEqual(clock.elapsed, 0)
        success.assert_not_called()

    def test_first_native_refusal_wins_over_r_retry_exit_and_relay_diagnostics(self):
        refusal.record(self.directory, self.launch, self.evidence)
        agents.update_runtime(self.directory, self.launch, state='running', reason='generic child exited',
                              error='relay lost', native_refusal=None)
        refusal.record(self.directory, self.launch, {**self.evidence, 'message': 'later retry failed'})
        facts = agents.runtime(self.directory)
        self.assertEqual(facts['reason'], self.evidence['message'])
        self.assertEqual(facts['native_refusal']['code'], -32600)
        self.assertFalse(lifecycle.launch_ready(object(), {}, facts, self.directory, {}, 'alive'))

    def test_observer_skips_unsupported_versions_and_lost_relays_before_tmux(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        for version, relay in (('0.162.1', 'ready'), ('0.164.0', 'ready'), (None, 'ready'),
                               ([], 'ready'), ('0.163.0', 'lost')):
            with self.subTest(version=version, relay=relay):
                write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
                      'attachment': {'readiness': 'process'}, 'native_relay': {'state': relay},
                      'native_versions': {'cli': version}})
                monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
                monitor.describe = Mock(return_value=self.evidence)
                backend = Mock()
                backend.run.side_effect = ['native widget', '0']
                with patch('multiverse.plugins.harness.version_of', return_value=None) as probe, \
                        patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')) as owned:
                    monitor._run()
                if relay == 'lost':
                    probe.assert_not_called()
                owned.assert_not_called()
                monitor.describe.assert_not_called()

    def test_plain_observer_stops_after_the_startup_window(self):
        class Clock:
            elapsed = 0.
            def is_set(self):
                return self.elapsed >= 75  # Bound the failing old implementation too.
            def wait(self, seconds):
                self.elapsed += seconds
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
              'attachment': {'readiness': 'process'}, 'native_versions': {'cli': '0.163.0'}})
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        agents.update_runtime(self.directory, self.launch, native_startup_observation={'state': 'pending'})
        monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
        monitor.stopped = Clock()
        monitor.describe = Mock(return_value=None)
        backend = Mock()
        backend.run.side_effect = lambda command, *args: 'normal native pane' if command == 'capture-pane' else '0'
        with patch.object(runner.time, 'monotonic', side_effect=lambda: monitor.stopped.elapsed), \
                patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
            monitor._run()
        self.assertLessEqual(monitor.stopped.elapsed, 55)
        self.assertLessEqual(monitor.describe.call_count, 240)
        self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'unverified')

    def test_default_wait_outlasts_unknown_screen_observation_by_five_seconds(self):
        class Clock:
            elapsed = 0.
            def is_set(self):
                return self.elapsed >= 75
            def wait(self, seconds):
                self.elapsed += seconds
        attachment = {'readiness': 'process'}
        agents.update_runtime(self.directory, self.launch, state='running', pid=123,
                              startup_checked=True, startup_delivery=None, attachment=attachment,
                              native_versions={'cli': '0.163.0'},
                              native_startup_observation={'state': 'pending'})
        write(self.directory / 'launches' / self.launch / 'request.json', {'attachment': attachment})
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
        monitor.stopped = Clock()
        monitor.describe = Mock(return_value=None)
        monitor.started = Mock(return_value=False)
        backend = Mock()
        backend.run.side_effect = lambda command, *args: 'Trust this folder?' if command == 'capture-pane' else '0'
        default_timeout = python_inspect.signature(lifecycle.start).parameters['wait_timeout'].default
        with patch.object(runner.time, 'monotonic', side_effect=lambda: monitor.stopped.elapsed), \
                patch.object(lifecycle.time, 'sleep', side_effect=lambda seconds: monitor._run()), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
            result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, default_timeout)
        self.assertTrue(result['ready'], result)
        self.assertIn('could not be confirmed', result['notices'][0])
        self.assertEqual(monitor.stopped.elapsed, 55)
        self.assertGreaterEqual(default_timeout - monitor.stopped.elapsed, 5)

    def test_default_wait_does_not_wait_for_a_screen_query_already_in_flight(self):
        import threading
        class Clock:
            elapsed = 0.
            def monotonic(self):
                return self.elapsed
            def sleep(self, seconds):
                self.elapsed += seconds
        clock = Clock()
        entered, release = threading.Event(), threading.Event()
        attachment = {'readiness': 'process'}
        agents.update_runtime(self.directory, self.launch, state='running', pid=123,
                              startup_checked=True, startup_delivery=None, attachment=attachment,
                              native_versions={'cli': '0.163.0'},
                              native_startup_observation={'state': 'pending'})
        write(self.directory / 'launches' / self.launch / 'request.json', {'attachment': attachment})
        frame = json.loads((Path(__file__).parent / 'fixtures/codex/refusal-0.163.0.json').read_text())[0]
        backend = Mock()
        def query(command, *args):
            if command == 'capture-pane':
                entered.set()
                release.wait(10)
                return frame['text']
            return '0\t0\t0\t80'
        backend.run.side_effect = query
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
        default_timeout = python_inspect.signature(lifecycle.start).parameters['wait_timeout'].default
        with patch.object(runner.time, 'monotonic', side_effect=clock.monotonic), \
                patch.object(lifecycle.time, 'sleep', side_effect=clock.sleep), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                patch('multiverse.plugins.events.subscribers', return_value=True), \
                patch.object(lifecycle, '_event') as announced, \
                patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
            monitor.start()
            try:
                self.assertTrue(entered.wait(2))
                result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, default_timeout)
            finally:
                release.set()
                monitor.thread.join(2)
        self.assertTrue(result['ready'], result)
        self.assertLessEqual(clock.elapsed, default_timeout - 4.9)
        self.assertIn('could not be confirmed', result['notices'][0])
        self.assertNotIn('native_refusal', agents.runtime(self.directory))
        ready = [call for call in announced.call_args_list if call.args[1] == 'agent.ready']
        self.assertEqual(len(ready), 1)
        self.assertTrue((self.directory / 'launches' / self.launch / 'ready-event').exists())
        self.assertFalse(monitor.thread.is_alive())

    def test_expiry_and_native_refusal_are_atomic_and_short_explicit_wait_is_unchanged(self):
        agents.update_runtime(self.directory, self.launch, state='running', pid=123, startup_checked=True,
                              startup_delivery=None, attachment={'readiness': 'process'},
                              native_startup_observation={'state': 'pending'})
        write(self.directory / 'launches' / self.launch / 'request.json', {'attachment': {'readiness': 'process'}})
        self.assertTrue(refusal.settle_startup(self.directory, self.launch, 'unverified'))
        with patch.object(refusal.time, 'monotonic', return_value=1):
            self.assertIsNone(refusal.record(self.directory, self.launch, self.evidence, observation_deadline=55))
        self.assertNotIn('native_refusal', agents.runtime(self.directory))
        self.assertFalse((self.directory / 'launches' / self.launch / 'native-refusal.json').exists())
        agents.update_runtime(self.directory, self.launch, native_startup_observation={'state': 'pending'})
        elapsed = [0.]
        with patch.object(lifecycle.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(lifecycle.time, 'sleep', side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            result = lifecycle.wait_ready(self.root, self.directory, self.launch,
                CodexAdapter.configured('codex', {'inbox_bridge': False}), 1)
        self.assertTrue(result['timed_out'])
        self.assertEqual(elapsed[0], 1)
        self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'pending')

    def test_expired_screen_does_not_announce_ready_before_startup_delivery(self):
        agents.update_runtime(self.directory, self.launch, state='running', pid=123, startup_checked=True,
                              startup_delivery='waiting', attachment={'readiness': 'process'},
                              native_startup_observation={'state': 'pending'})
        write(self.directory / 'launches' / self.launch / 'request.json', {'attachment': {'readiness': 'process'}})
        elapsed = [0.]
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        with patch.object(lifecycle.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(lifecycle.time, 'sleep', side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                patch('multiverse.plugins.events.subscribers', return_value=True), \
                patch.object(lifecycle, '_event') as announced:
            result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 60, include_delivery=False)
            self.assertTrue(result['ready'])
            self.assertFalse(announced.called)
            self.assertFalse((self.directory / 'launches' / self.launch / 'ready-event').exists())
            agents.update_runtime(self.directory, self.launch, startup_delivery='ready')
            lifecycle.note_ready(self.root, self.directory, self.launch)
            lifecycle.note_ready(self.root, self.directory, self.launch)
        self.assertEqual(len([call for call in announced.call_args_list if call.args[1] == 'agent.ready']), 1)

    def test_late_screen_refusal_never_recreates_a_removed_agent(self):
        import shutil
        shutil.rmtree(self.directory)
        with self.assertRaises(ValueError):
            refusal.record(self.directory, self.launch, self.evidence, observation_deadline=55)
        self.assertFalse(self.directory.exists())

    def test_frame_completed_after_observation_deadline_cannot_refuse_startup(self):
        frame = json.loads((Path(__file__).parent / 'fixtures/codex/refusal-0.163.0.json').read_text())[0]
        elapsed = [0.]
        agents.update_runtime(self.directory, self.launch, state='running',
                              attachment={'readiness': 'process'}, native_versions={'cli': '0.163.0'},
                              native_startup_observation={'state': 'pending'})
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
        backend = Mock()
        def query(command, *args):
            if command == 'capture-pane':
                elapsed[0] = 56.
                return frame['text']
            return '0\t0\t0\t80'
        backend.run.side_effect = query
        with patch.object(runner.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
            monitor._run()
        self.assertNotIn('native_refusal', agents.runtime(self.directory))
        self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'unverified')

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
