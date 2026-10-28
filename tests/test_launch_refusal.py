"""A native refusal is primary and latched until a new mv2 launch."""
from uuid import uuid4
import json
import inspect as python_inspect
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from multiverse.runtime import lifecycle, native, refusal, runner
from multiverse.store import read, write
from multiverse.universe import agents
from multiverse.universe.mailbox import append_message, message_record, update_message
from multiverse.tools import inspect, list_agents, doctor
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.plugins import CHECKED_VERSION, version_text
from test_multiverse import BaseTest


CHECKED_CLI = version_text(CHECKED_VERSION)


class Clock:
    """The observer's stop event, as a fake clock that only moves when the observer waits."""
    elapsed = 0.

    def is_set(self):
        return self.elapsed >= 75  # Bound the failing old implementation too.

    def wait(self, seconds):
        self.elapsed += seconds


class LaunchRefusalTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.fresh()

    def fresh(self):
        """A new starting agent and launch, so each row of a table starts from its own records."""
        self.identity, self.thread, self.launch = (str(uuid4()) for _ in range(3))
        self.directory = self.root / 'state' / 'agents' / self.identity
        write(self.directory / 'properties.json', {'id': self.identity, 'name': 'refused', 'harness': 'codex',
              'workspace': str(self.work), 'native_session_id': self.thread})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'starting',
                                              'startup_delivery': 'waiting'})
        (self.directory / 'launches' / self.launch).mkdir(parents=True)
        self.evidence = {'source': 'native-rpc', 'method': 'thread/resume', 'code': -32600,
                         'message': 'This conversation is open in another app', 'native_id': self.thread}

    def test_wait_ready_cannot_report_ready_before_a_later_refusal(self):
        """wait_ready never reports ready before a refusal that arrives mid-wait, and settled screens keep the process
        checks; fragile because it is a polling race, and a break reports ready for a refused session (1eb49e0)."""
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        with self.subTest(observation='pending, refused mid-wait'):
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
            with patch.object(lifecycle, 'launch_result', side_effect=sample), \
                    patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 1)
            self.assertFalse(result['ready'], result)
            self.assertIn(self.evidence['message'], result['error'])
            self.assertGreaterEqual(len(calls), 2)
        self.fresh()
        attachment = {'readiness': 'process'}
        request = {'attachment': attachment}
        write(self.directory / 'launches' / self.launch / 'request.json', request)
        for state in ('started', 'unsupported', 'unverified'):
            with self.subTest(observation=state):
                facts = {'launch_id': self.launch, 'state': 'running', 'pid': 123, 'startup_checked': True,
                         'native_startup_observation': {'state': state}, 'attachment': attachment}
                write(self.directory / 'runtime.json', facts)
                with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                    result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 1)
                self.assertTrue(result['ready'])
                self.assertEqual(bool(result.get('notices')), state == 'unverified')
                if state == 'unverified':
                    self.assertIn('could not be confirmed', result['notices'][0])
                self.assertFalse(lifecycle.launch_ready(adapter, {}, {**facts, 'pid': None}, self.directory, request,
                                                        'alive'))
                self.assertFalse(lifecycle.launch_ready(adapter, {}, facts, self.directory, request, 'dormant'))

    def test_refusal_record_is_fenced_first_wins_and_never_recreates(self):
        """The refusal record belongs to its launch, the first cause wins over later diagnostics, and a late refusal
        never recreates a removed agent; a break hides the real cause or brings back a removed agent's folder."""
        with self.subTest('the first native refusal wins over an R retry, an exit and relay diagnostics'):
            refusal.record(self.directory, self.launch, self.evidence)
            agents.update_runtime(self.directory, self.launch, state='running', reason='generic child exited',
                                  error='relay lost', native_refusal=None)
            refusal.record(self.directory, self.launch, {**self.evidence, 'message': 'later retry failed'})
            facts = agents.runtime(self.directory)
            self.assertEqual(facts['reason'], self.evidence['message'])
            self.assertEqual(facts['native_refusal']['code'], -32600)
            self.assertFalse(lifecycle.launch_ready(object(), {}, facts, self.directory, {}, 'alive'))
        self.fresh()
        with self.subTest('inspect, list and doctor show the native cause over a later connection diagnostic'):
            refusal.record(self.directory, self.launch, self.evidence)
            facts = agents.runtime(self.directory)
            row = {**read(self.directory / 'properties.json'), 'runtime': facts, 'live': 'alive',
                   'status': 'unknown', 'phase': 'working', 'universe': str(self.root),
                   'connection_error': 'generic relay lost', 'launch_error': self.evidence['message'],
                   'reason': self.evidence['message']}
            with patch.object(inspect, 'facts', return_value=row), patch.object(inspect, 'run', return_value='screen'):
                self.assertEqual(inspect.cli_run(self.root, agent='refused', json=True)['error'],
                                 self.evidence['message'])
            with patch.object(lifecycle, 'listing', return_value=[row]):
                self.assertNotIn('generic relay', list_agents.run(self.root))
                self.assertIn(self.evidence['message'], list_agents.run(self.root))
                problems = doctor.run(self.root)['problems']
                self.assertTrue(any(self.evidence['message'] in problem for problem in problems))
                self.assertFalse(any('generic relay' in problem for problem in problems))
        self.fresh()
        with self.subTest('stale or foreign evidence cannot change this launch'):
            for launch, evidence in ((str(uuid4()), self.evidence),
                                     (self.launch, {**self.evidence, 'native_id': str(uuid4())})):
                with self.assertRaises(ValueError):
                    refusal.record(self.directory, launch, evidence)
            self.assertNotIn('native_refusal', agents.runtime(self.directory))
        self.fresh()
        with self.subTest("the observer's startup result is fenced to its launch and agent"):
            facts = {'launch_id': self.launch, 'state': 'running', 'attachment': {'readiness': 'process'}}
            agents.update_runtime(self.directory, self.launch,
                                  **{key: value for key, value in facts.items() if key != 'launch_id'})
            monitor = runner.RefusalMonitor(self.directory, self.launch, self.root,
                                            SimpleNamespace(launch_refusal=lambda evidence: None), 'fixture')
            with patch.object(lifecycle, 'note_ready'):
                monitor._settled('started')
            self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'started')
            write(self.directory / 'runtime.json', {**facts, 'launch_id': 'replacement'})
            monitor._settled('unverified')
            self.assertNotIn('native_startup_observation', agents.runtime(self.directory))
            shutil.rmtree(self.directory)
            monitor._settled('started')
            self.assertFalse(self.directory.exists())
        self.fresh()
        with self.subTest('a late screen refusal never recreates a removed agent'):
            shutil.rmtree(self.directory)
            with self.assertRaises(ValueError):
                refusal.record(self.directory, self.launch, self.evidence, observation_deadline=55)
            self.assertFalse(self.directory.exists())

    def test_screen_observer_runs_only_where_it_can_be_right(self):
        """The startup-screen observer runs only for the checked Codex release on plain launches and stops once a
        bridge binding is validated or the relay is lost; fragile because it is pinned to one version, and a Codex change silently
        disables or misfires refusal detection (995f8d9)."""
        for readiness, relay, observe in (('binding', 'ready', False), ('legacy-binding', 'ready', False),
                                           ('binding', 'starting', True), ('process', 'ready', True)):
            with self.subTest(readiness=readiness, relay=relay):
                write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
                      'attachment': {'readiness': readiness}, 'native_relay': {'state': relay},
                      'native_versions': {'cli': CHECKED_CLI}})
                describe = Mock(return_value=self.evidence)
                monitor = runner.RefusalMonitor(self.directory, self.launch, self.root,
                                                SimpleNamespace(launch_refusal=describe), 'fixture')
                backend = Mock()
                backend.run.side_effect = ['native widget', '0']
                with patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')) as owned:
                    monitor._run()
                self.assertEqual(describe.called, observe)
                self.assertEqual(owned.called, observe)
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        for version, relay in (('0.162.1', 'ready'), ('0.165.0', 'ready'), (None, 'ready'),
                               ([], 'ready'), (CHECKED_CLI, 'lost'), ('0.163.0', 'lost')):
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

    def observe_native_frames(self, first_name, started=None):
        self.fresh()
        fixtures = Path(__file__).parent / 'fixtures/codex'
        first = json.loads((fixtures / first_name).read_text())[0]
        refused = json.loads((fixtures / 'refusal-0.163.0.json').read_text())[0]
        clock = Clock()
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
              'startup_checked': True, 'attachment': {'readiness': 'process'},
              'native_versions': {'cli': CHECKED_CLI}})
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

    def test_frame_sequences_end_observation_correctly(self):
        """On real 0.163.0 frame sequences a started composer ends observation before a later /resume refusal, and a
        loading composer never hides an initial refusal; fragile because the Codex TUI changes per release (d3dd903)."""
        with self.subTest('a started composer, then a /resume refusal'):
            clock, backend, recorded = self.observe_native_frames('started-0.163.0.json')
            recorded.assert_not_called()
            self.assertEqual(backend.run.call_count, 2, 'Successful composer should end all pane polling')
            self.assertEqual(clock.elapsed, 0)
            facts = agents.runtime(self.directory)
            self.assertNotIn('native_refusal', facts)
            self.assertFalse(facts.get('reason'))
            self.assertFalse((self.directory / 'launches' / self.launch / 'native-refusal.json').exists())
            self.assertEqual(facts['native_startup_observation']['state'], 'started')
        for first in ('loading-0.163.0.json', 'resuming-0.163.0.json'):
            with self.subTest(first=first):
                clock, backend, recorded = self.observe_native_frames(first)
                recorded.assert_called_once()
                self.assertEqual(clock.elapsed, 10)
                self.assertIn('open in another app', agents.runtime(self.directory)['reason'])
        for answer in (1, 'ready', {'ready': True}):
            with self.subTest(startup_hook=answer):
                clock, _, recorded = self.observe_native_frames('started-0.163.0.json', Mock(return_value=answer))
                recorded.assert_called_once()
                self.assertEqual(clock.elapsed, 10)
        with self.subTest('a startup hook never overrides a native refusal'):
            success = Mock(return_value=True)
            clock, _, recorded = self.observe_native_frames('refusal-0.163.0.json', success)
            recorded.assert_called_once()
            self.assertEqual(clock.elapsed, 0)
            success.assert_not_called()

    def test_observation_window_against_the_default_wait(self):
        """The 55 s observation window ends before the default wait, expiry and refusal are atomic, and agent.ready
        fires once, after startup delivery; a short caller wait preserves later refusal observation, since coupling
        the deadlines otherwise silently drops failures or reports healthy launches as failed (668eca3, 858e2be)."""
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        default_timeout = python_inspect.signature(lifecycle.start).parameters['wait_timeout'].default
        refused = json.loads((Path(__file__).parent / 'fixtures/codex/refusal-0.163.0.json').read_text())[0]
        with self.subTest('the plain observer stops after the startup window'):
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
                  'attachment': {'readiness': 'process'}, 'native_versions': {'cli': CHECKED_CLI}})
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
        self.fresh()
        with self.subTest('the default wait outlasts an unknown screen by five seconds'):
            attachment = {'readiness': 'process'}
            agents.update_runtime(self.directory, self.launch, state='running', pid=123,
                                  startup_checked=True, startup_delivery=None, attachment=attachment,
                                  native_versions={'cli': CHECKED_CLI},
                                  native_startup_observation={'state': 'pending'})
            write(self.directory / 'launches' / self.launch / 'request.json', {'attachment': attachment})
            monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
            monitor.stopped = Clock()
            monitor.describe = Mock(return_value=None)
            monitor.started = Mock(return_value=False)
            backend = Mock()
            backend.run.side_effect = lambda command, *args: 'Trust this folder?' if command == 'capture-pane' else '0'
            with patch.object(runner.time, 'monotonic', side_effect=lambda: monitor.stopped.elapsed), \
                    patch.object(lifecycle.time, 'sleep', side_effect=lambda seconds: monitor._run()), \
                    patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                    patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
                result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, default_timeout)
            self.assertTrue(result['ready'], result)
            self.assertIn('could not be confirmed', result['notices'][0])
            self.assertEqual(monitor.stopped.elapsed, 55)
            self.assertGreaterEqual(default_timeout - monitor.stopped.elapsed, 5)
        self.fresh()
        with self.subTest('the default wait does not wait for a screen query already in flight'):
            class Waiting:
                elapsed = 0.
                def monotonic(self):
                    return self.elapsed
                def sleep(self, seconds):
                    self.elapsed += seconds
            clock = Waiting()
            entered, release = threading.Event(), threading.Event()
            attachment = {'readiness': 'process'}
            agents.update_runtime(self.directory, self.launch, state='running', pid=123,
                                  startup_checked=True, startup_delivery=None, attachment=attachment,
                                  native_versions={'cli': CHECKED_CLI},
                                  native_startup_observation={'state': 'pending'})
            write(self.directory / 'launches' / self.launch / 'request.json', {'attachment': attachment})
            backend = Mock()
            def query(command, *args):
                if command == 'capture-pane':
                    entered.set()
                    release.wait(10)
                    return refused['text']
                return '0\t0\t0\t80'
            backend.run.side_effect = query
            monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
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
        self.fresh()
        with self.subTest('an expired screen announces ready only after startup delivery, once'):
            agents.update_runtime(self.directory, self.launch, state='running', pid=123, startup_checked=True,
                                  startup_delivery='waiting', attachment={'readiness': 'process'},
                                  native_startup_observation={'state': 'pending'})
            write(self.directory / 'launches' / self.launch / 'request.json', {'attachment': {'readiness': 'process'}})
            elapsed = [0.]
            with patch.object(lifecycle.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                    patch.object(lifecycle.time, 'sleep',
                                 side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)), \
                    patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                    patch('multiverse.plugins.events.subscribers', return_value=True), \
                    patch.object(lifecycle, '_event') as announced:
                result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 60,
                                              include_delivery=False)
                self.assertTrue(result['ready'])
                self.assertFalse(announced.called)
                self.assertFalse((self.directory / 'launches' / self.launch / 'ready-event').exists())
                agents.update_runtime(self.directory, self.launch, startup_delivery='ready')
                lifecycle.note_ready(self.root, self.directory, self.launch)
                lifecycle.note_ready(self.root, self.directory, self.launch)
            self.assertEqual(len([call for call in announced.call_args_list if call.args[1] == 'agent.ready']), 1)
        self.fresh()
        with self.subTest('screen expiry is final but a short caller wait keeps observing'):
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
                    patch.object(lifecycle.time, 'sleep',
                                 side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)), \
                    patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 1)
            self.assertTrue(result['ready'])
            self.assertTrue(result['ok'])
            self.assertNotIn('timed_out', result)
            self.assertEqual(elapsed[0], 1)
            self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'pending')
            self.assertIn('could not be confirmed', result['notices'][0])
            with patch.object(refusal.time, 'monotonic', return_value=2):
                self.assertIsNotNone(refusal.record(self.directory, self.launch, self.evidence, observation_deadline=55))
            self.assertEqual(agents.runtime(self.directory)['reason'], self.evidence['message'])
        self.fresh()
        with self.subTest('a frame completed after the observation deadline cannot refuse startup'):
            elapsed = [0.]
            agents.update_runtime(self.directory, self.launch, state='running',
                                  attachment={'readiness': 'process'}, native_versions={'cli': CHECKED_CLI},
                                  native_startup_observation={'state': 'pending'})
            monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, adapter, 'fixture')
            backend = Mock()
            def late(command, *args):
                if command == 'capture-pane':
                    elapsed[0] = 56.
                    return refused['text']
                return '0\t0\t0\t80'
            backend.run.side_effect = late
            with patch.object(runner.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                    patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
                monitor._run()
            self.assertNotIn('native_refusal', agents.runtime(self.directory))
            self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'unverified')

    def test_a_refusal_fails_queued_input_and_keeps_a_wake_prompt_uncertain(self):
        """A native refusal fails queued startup input, leaves a registered wake prompt uncertain, and saves new input
        without submitting it; a break silently duplicates or loses prompts."""
        with self.subTest('input queued before the refusal'):
            queued = append_message(self.directory, 'not submitted', {}, delivery='steer', target_session=self.thread)
            update_message(self.directory, queued['id'], submission_state='pending', deferred_launch_id=self.launch)
            wake = append_message(self.directory, 'possibly submitted', {}, delivery='steer',
                                  target_session=self.thread)
            update_message(self.directory, wake['id'], submission_state='submitting', delivery_method='resume_prompt',
                           wake_launch_id=self.launch, attempts=[{'state': 'unknown'}])
            refusal.record(self.directory, self.launch, self.evidence)
            self.assertEqual(message_record(self.directory, queued['id'])[1]['submission_state'], 'failed')
            self.assertEqual(message_record(self.directory, wake['id'])[1]['submission_state'], 'unknown')
            self.assertEqual(message_record(self.directory, wake['id'])[1]['attempts'][-1]['reason'],
                             self.evidence['message'])
        self.fresh()
        with self.subTest('input sent after the refusal'):
            refusal.record(self.directory, self.launch, self.evidence)
            message = append_message(self.directory, 'later', {}, delivery='steer', target_session=self.thread)
            with patch.object(native, 'owned'), patch.object(native, 'observe', return_value={'live': 'alive'}):
                result = native.deliver(self.root, self.identity, message['id'])
            self.assertFalse(result['ok'])
            self.assertEqual(result['reason'], self.evidence['message'])
            self.assertEqual(message_record(self.directory, message['id'])[1]['submission_state'], 'failed')
