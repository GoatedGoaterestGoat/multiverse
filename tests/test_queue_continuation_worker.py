"""Only new submitted input can authorize one native FIFO restart."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.harnesses.types import NativeResult, SessionHandle
from multiverse.runtime import lifecycle, native, queue_continuation, runner
from multiverse.store import read, write
from multiverse.tools import deliver
from multiverse.universe import agents
from multiverse.universe.mailbox import inbox, message_record, update_message
from test_message_delivery import DeliveryFixture
from test_native_backends import NativeBackendTest


class QueueContinuationWorkerTest(DeliveryFixture):
    def setUp(self):
        super().setUp()
        self.adapter = native.harnesses.load(self.root, 'claude')
        self.adapter.continue_queued = Mock()
        self.paused = {'native_session_id': self.native_id, 'turn_id': 'old', 'state': 'paused'}
        agents.update_runtime(self.directory, self.launch, queue_state=self.paused)

    def test_only_new_submitted_input_gets_one_restart_claim(self):
        """A restart claim belongs to one new submission; leaking it to checks or retries silently repeats queue/start after lost acceptance."""
        self.send('manual', delivery='inbox-only')
        self.assertEqual(agents.runtime(self.directory)['queue_state'], self.paused)
        for mode in ('steer', 'follow-up'):
            with self.subTest(mode=mode):
                sent = self.send(mode, delivery=mode)
                session, message, _ = self.calls[-1]
                self.assertEqual(session.queue_state['restart'], {'id': sent['id'], 'state': 'claimed'})
                ordinary = SessionHandle.from_records(read(self.directory / 'properties.json'),
                                                      agents.runtime(self.directory), self.directory)
                self.assertNotIn('restart', ordinary.queue_state)
                count = len(self.calls)
                native.deliver(self.root, self.agent, sent['id'], check=True)
                native.deliver(self.root, self.agent, sent['id'], retry=True)
                self.assertEqual(len(self.calls), count)
        self.behavior = TimeoutError('lost native acceptance')
        lost = self.send('lost')
        self.assertEqual(message_record(self.directory, lost['id'])[1]['submission_state'], 'unknown')
        count = len(self.calls)
        deliver.run(self.root, agent=self.agent, retry=True)
        self.assertEqual(len(self.calls), count)
        self.adapter.continue_queued.assert_not_called()
        # A receipt retry may submit a definite failure, but cannot mint a restart.
        self.behavior = NativeResult('error', 'definite refusal')
        failed = self.send('refused')
        self.behavior = NativeResult('native_queued', value={})
        native.deliver(self.root, self.agent, failed['id'], retry=True)
        self.assertNotIn('restart', self.calls[-1][0].queue_state)
        for damaged in ([], {**self.paused, 'restart': ['bad']}):
            with self.subTest(damaged=damaged):
                agents.update_runtime(self.directory, self.launch, queue_state=damaged)
                count = len(self.calls)
                refused = self.send('bad pause record ' + str(damaged))
                self.assertEqual(refused['state'], 'unknown')
                self.assertEqual(len(self.calls), count)

    def test_primary_events_keep_pause_order_and_scope_across_resume(self):
        """Pause survives attachment replacement but stale turns and generations cannot re-pause a newer turn, a silent event-order race."""
        publish = lambda **event: queue_continuation.record_primary_event(self.directory, self.launch,
            {'native_session_id': self.native_id, **event})
        publish(kind='started', turn_id='one')
        publish(kind='interrupt', turn_id='one')
        self.assertEqual(agents.runtime(self.directory)['queue_state']['state'], 'interrupting')
        publish(kind='completed', turn_id='one', status='finished')
        self.assertEqual(agents.runtime(self.directory)['queue_state']['state'], 'idle')
        publish(kind='started', turn_id='two')
        publish(kind='completed', turn_id='two', status='interrupted')
        self.assertEqual(agents.runtime(self.directory)['queue_state']['state'], 'paused')
        publish(kind='started', turn_id='three')
        publish(kind='completed', turn_id='two', status='interrupted')
        self.assertEqual(agents.runtime(self.directory)['queue_state']['state'], 'running')
        publish(kind='completed', turn_id='three', status='interrupted')
        facts = agents.runtime(self.directory)
        properties = read(self.directory / 'properties.json')
        session = SessionHandle.from_records(properties, facts, self.directory)
        command = Mock(native_mode={'mode': 'bridge'})
        adapter = Mock(retained_usage=None)
        fresh = lifecycle._initial_runtime(str(uuid4()), command, adapter, session, facts, True, None)
        self.assertEqual(fresh['queue_state'], facts['queue_state'])
        foreign = lifecycle._initial_runtime(str(uuid4()), command, adapter,
                                            replace(session, native_id=str(uuid4())), facts, True, None)
        self.assertNotIn('queue_state', foreign)
        before = (self.directory / 'runtime.json').read_bytes()
        with self.assertRaises(ValueError):
            queue_continuation.record_primary_event(self.directory, 'replaced',
                {'kind': 'started', 'turn_id': 'other', 'native_session_id': self.native_id})
        self.assertEqual((self.directory / 'runtime.json').read_bytes(), before)
        with patch.object(runner, 'mutate_runtime'), patch.object(runner, 'runtime', return_value=facts):
            watcher = runner.StatusWatcher(self.directory, self.launch, self.root)
            watcher._publish('idle')
        self.adapter.continue_queued.assert_not_called()

    def test_checks_after_resume_show_paused_or_cancelled_without_restart(self):
        """Receipt selection crosses launch replacement and kill; losing those rows silently hides paused or cancelled accepted input."""
        self.behavior = NativeResult('native_queued', value={'queued_submission_id': 'q'})
        message = self.send('accepted', delivery='follow-up')
        write(self.directory / 'runtime.json', {**agents.runtime(self.directory), 'launch_id': str(uuid4())})
        self.behavior = NativeResult('native_queued', 'Native queue paused',
                                    {'queued_submission_id': 'q', 'paused': True})
        result = deliver.run(self.root, agent=self.agent, check=True)
        self.assertEqual(len(result['messages']), 1)
        self.assertTrue(result['messages'][0]['paused'])
        update_message(self.directory, message['id'], submission_state='cancelled',
                       last_receipt={'state': 'unavailable', 'reason': 'Cancelled by explicit agent stop'})
        result = deliver.run(self.root, agent=self.agent, check=True)
        self.assertEqual(result['messages'][0]['state'], 'cancelled')
        self.assertFalse(result['ok'])
        self.assertEqual(deliver.run(self.root, agent=self.agent, check=True), {'messages': [], 'ok': True})
        self.assertEqual(len(self.calls), 1)


class PausedWakeTest(NativeBackendTest):
    def test_paused_wake_queues_after_binding_instead_of_starting_a_prompt(self):
        """A paused wake must enter the native FIFO after binding; a command-line wake silently jumps ahead of already accepted input."""
        self.adapter.continue_queued = Mock()
        agents.update_runtime(self.directory, self.launch, state='stopped',
            queue_state={'native_session_id': self.properties['native_session_id'], 'state': 'paused'})
        from multiverse.universe.mailbox import append_message
        message = append_message(self.directory, 'new wake', {}, delivery='follow-up',
                                 target_session=self.properties['native_session_id'])
        with patch.object(native, 'observe', return_value={'live': 'dormant'}):
            result = native.send_or_wake(self.root, self.directory.name, message['id'], new_message=True)
        self.assertEqual(result['state'], 'pending')
        facts = agents.runtime(self.directory)
        launch = self.directory / 'launches' / facts['launch_id']
        self.assertEqual((launch / 'task.md').read_text(), '')
        self.assertIsNone(read(launch / 'request.json')['resume_message_id'])
        tracking = message_record(self.directory, message['id'])[1]
        self.assertEqual(tracking['submission_state'], 'pending')
        self.assertEqual(tracking['deferred_launch_id'], facts['launch_id'])
        self.assertTrue(tracking['restart_on_delivery'])
        agents.update_runtime(self.directory, facts['launch_id'], state='running', pid=123,
                              native_relay={'state': 'ready'}, startup_checked=True)
        with patch.object(native, 'owned'), patch.object(native, 'observe', return_value={'live': 'alive'}):
            native.flush_startup(self.root, self.directory, facts['launch_id'])
            native.flush_startup(self.root, self.directory, facts['launch_id'])
        sent = [call for call in self.adapter.calls if call[0] == 'send']
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1].queue_state['restart']['id'], message['id'])
        with self.subTest('failed startup stays retryable without inventing a new restart intent'):
            agents.update_runtime(self.directory, facts['launch_id'], state='stopped')
            later = append_message(self.directory, 'wake whose bridge fails', {}, delivery='follow-up',
                                   target_session=self.properties['native_session_id'])
            with patch.object(native, 'observe', return_value={'live': 'dormant'}):
                native.send_or_wake(self.root, self.directory.name, later['id'], new_message=True)
            generation = agents.runtime(self.directory)['launch_id']
            native.flush_startup(self.root, self.directory, generation, error='Bridge startup failed')
            self.assertEqual(message_record(self.directory, later['id'])[1]['submission_state'], 'failed')
            agents.update_runtime(self.directory, generation, state='running', pid=123,
                                  native_relay={'state': 'ready'}, startup_delivery='ready')
            with patch.object(native, 'owned'):
                native.deliver(self.root, self.directory.name, later['id'], retry=True)
            sent = [call for call in self.adapter.calls if call[0] == 'send']
            self.assertEqual(len(sent), 2)
            self.assertNotIn('restart', sent[-1][1].queue_state)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (QueueContinuationWorkerTest, PausedWakeTest)
                              for name in cls.__dict__ if name.startswith('test_'))
