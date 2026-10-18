"""The idle observer schedules owned continuation without blocking native status."""
from unittest.mock import Mock
from uuid import uuid4

from multiverse.runtime.runner import StatusWatcher
from multiverse.store import write
from multiverse.universe import agents
from test_multiverse import BaseTest


class IdleContinuationTriggerTest(BaseTest):
    def test_initial_idle_schedules_continuation_after_runtime_publication(self):
        identity, launch = str(uuid4()), str(uuid4())
        directory = self.root / 'state/agents' / identity
        write(directory / 'properties.json', {'id': identity, 'name': 'worker', 'harness': 'fixture'})
        write(directory / 'runtime.json', {'launch_id': launch, 'state': 'running', 'status': 'unknown'})
        agents.update_runtime(directory, launch, status='unknown')
        watcher = StatusWatcher(directory, launch, self.root)
        watcher.queue_continuation = Mock()
        watcher._publish('idle')
        watcher.queue_continuation.notify.assert_called_once()
        self.assertEqual(agents.runtime(directory)['status'], 'idle')


import fcntl
import threading
import unittest
from unittest.mock import patch

from multiverse.runtime import native
from multiverse.runtime.queue_continuation import QueueContinuation
from multiverse.harnesses.types import NativeResult
from multiverse.store import read
from multiverse.universe.mailbox import append_message, update_message
from test_native_backends import NativeBackendTest


class QueueContinuationWorkerTest(NativeBackendTest):
    def setUp(self):
        super().setUp()
        self.worker = QueueContinuation(self.directory, self.launch, self.root)
        self.adapter.continue_queued = Mock(return_value=NativeResult('ok', value={'continued': True}))
        self.enterContext(patch.object(native, 'owned'))
        self.addCleanup(self.worker.close)

    def accepted(self, text='owned', delivery='follow-up', **tracking):
        message = append_message(self.directory, text, {}, delivery=delivery,
                                 target_session=self.properties['native_session_id'])
        values = dict(submission_state='accepted', delivery_method='native',
                      target_session=self.properties['native_session_id'],
                      attempts=[{'launch_id': self.launch, 'native_session_id': self.properties['native_session_id']}],
                      native_receipt={'queued_submission_id': 'queue-' + message['id'], 'transcript_confirmed': False})
        update_message(self.directory, message['id'], **{**values, **tracking})
        return message

    def once(self):
        self.assertTrue(self.worker.notify())
        token, self.worker.pending = self.worker.pending, None
        self.worker.active = token
        try:
            done = self.worker._work(token)
        finally:
            self.worker.active = None
        if done:
            self.worker.completed = token
        return done

    def test_current_accepted_follow_up_continues_without_rewriting_receipts(self):
        message = self.accepted()
        state = (self.directory / 'inbox-state.json').read_bytes()
        log = (self.directory / 'inbox.jsonl').read_bytes()
        self.assertTrue(self.once())
        args = self.adapter.continue_queued.call_args.args
        self.assertEqual(args[0].native_id, self.properties['native_session_id'])
        self.assertEqual(args[1], {message['id']: 'queue-' + message['id']})
        self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), state)
        self.assertEqual((self.directory / 'inbox.jsonl').read_bytes(), log)
        self.assertFalse(self.worker.notify())
        self.adapter.continue_queued.assert_called_once()
        self.assertEqual(agents.runtime(self.directory)['queue_continuation']['value'], {'continued': True})

    def test_receipt_selection_excludes_unowned_inactive_or_consumed_entries(self):
        for values in ({'submission_state': 'pending'}, {'submission_state': 'failed'},
                       {'submission_state': 'unknown'}, {'submission_state': 'submitting'},
                       {'delivery_method': 'resume_prompt'},
                       {'target_session': str(uuid4())}, {'attempts': []},
                       {'attempts': [{'launch_id': 'old', 'native_session_id': self.properties['native_session_id']}]},
                       {'attempts': [{'launch_id': self.launch, 'native_session_id': str(uuid4())}]},
                       {'native_receipt': {'queued_submission_id': 'old', 'transcript_confirmed': True}},
                       {'native_receipt': {'turn_id': 'native-history', 'transcript_confirmed': False}},
                       {'native_receipt': {}}):
            self.accepted(**values)
        self.accepted(delivery='steer')
        self.accepted(delivery='inbox-only')
        self.assertTrue(self.once())
        self.adapter.continue_queued.assert_not_called()

    def test_wrong_typed_receipt_blocks_continuation_with_named_ledger_damage(self):
        self.accepted('valid accepted receipt')
        damaged = self.accepted('damaged accepted receipt')
        path = self.directory / 'inbox-state.json'
        states = read(path)
        states[damaged['id']]['native_receipt'] = []
        write(path, states)
        before = path.read_bytes()
        self.assertTrue(self.once())
        self.adapter.continue_queued.assert_not_called()
        diagnostic = agents.runtime(self.directory)['queue_continuation']
        self.assertEqual(diagnostic['state'], 'error')
        self.assertIn(str(path), diagnostic['reason'])
        self.assertIn('invalid delivery tracking', diagnostic['reason'])
        self.assertIn(damaged['id'], diagnostic['reason'])
        self.assertEqual(path.read_bytes(), before)

    def test_acceptance_after_idle_is_detected_without_another_native_notification(self):
        # A valid state file with no accepted entries needs no native RPC.
        self.accepted(submission_state='pending')
        self.once()
        self.adapter.continue_queued.assert_not_called()
        self.assertFalse(self.worker.notify())
        message = self.accepted('accepted after idle')
        self.once()
        self.assertEqual(self.adapter.continue_queued.call_args.args[1], {message['id']: 'queue-' + message['id']})
        self.assertFalse(self.worker.notify())

    def test_receipt_only_updates_do_not_repeat_checks_for_the_same_accepted_set(self):
        message = self.accepted()
        self.once()
        update_message(self.directory, message['id'], native_receipt={
            'queued_submission_id': 'queue-' + message['id'], 'transcript_confirmed': False,
            'observed_by_check': True})
        self.once()
        self.adapter.continue_queued.assert_called_once()

    def test_acceptance_during_a_scan_is_not_hidden_by_its_completion_token(self):
        first = self.accepted('first')
        newer = []
        def continue_first(*args):
            newer.append(self.accepted('arrived during native operation'))
            self.assertTrue(self.worker.notify())
            return NativeResult('ok', value={'continued': True})
        self.adapter.continue_queued.side_effect = continue_first
        self.once()
        self.assertIsNotNone(self.worker.pending)
        old = self.worker.completed
        self.assertNotEqual(self.worker.pending, old)
        self.adapter.continue_queued.side_effect = None
        # The pending token remains scheduled; its second scan sees both receipts.
        token = self.worker.pending
        self.assertTrue(self.worker._work(token))
        self.assertEqual(set(self.adapter.continue_queued.call_args.args[1]), {first['id'], newer[0]['id']})

    def test_locks_are_nonblocking_and_busy_checks_are_not_marked_completed(self):
        self.accepted()
        for name in ('.launch.lock', '.delivery.lock'):
            with self.subTest(lock=name), (self.directory / name).open('a') as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    self.assertFalse(self.once())
                    self.assertIsNone(self.worker.completed)
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            self.worker.pending = None
        self.assertTrue(self.once())
        self.adapter.continue_queued.assert_called_once()

    def test_lost_starting_ending_refused_changed_or_unknown_launch_never_calls_provider(self):
        self.accepted()
        for change in ({'launch_id': 'old'}, {'state': 'starting'}, {'status': 'working'}, {'status': 'waiting'},
                       {'status': 'unknown'}, {'startup_checked': False}, {'ending_at': 'now'},
                       {'native_refusal': {'message': 'owned elsewhere'}}, {'binding_conflict': 'wrong'},
                       {'startup_delivery': 'waiting'}, {'startup_delivery': 'failed'},
                       {'native_relay': {'state': 'lost'}}, {'attachment': {'readiness': 'process'}},
                       {'native_status_version': True}):
            with self.subTest(change=change):
                write(self.directory / 'runtime.json', {**self.facts, **change})
                self.assertFalse(self.worker.notify())
        write(self.directory / 'runtime.json', self.facts)
        self.assertTrue(self.worker.notify())
        token = self.worker.pending
        write(self.directory / 'runtime.json', {**self.facts, 'native_status_version': 1})
        self.assertTrue(self.worker._work(token))
        self.adapter.continue_queued.assert_not_called()

    def test_ambiguous_transport_keeps_acceptance_and_does_not_repeat_for_same_token(self):
        self.accepted()
        state = (self.directory / 'inbox-state.json').read_bytes()
        self.adapter.continue_queued.side_effect = TimeoutError('ambiguous native start')
        self.once()
        self.assertFalse(self.worker.notify())
        self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), state)
        self.assertIn('ambiguous native start', agents.runtime(self.directory)['queue_continuation']['reason'])
        self.adapter.continue_queued.assert_called_once()

    def test_close_during_the_accepted_scan_cannot_start_another_native_operation(self):
        self.accepted()
        scan = self.worker._accepted
        def closing_scan(session):
            selected = scan(session)
            self.worker.close()
            return selected
        with patch.object(self.worker, '_accepted', side_effect=closing_scan):
            self.once()
        self.adapter.continue_queued.assert_not_called()

    def test_close_drains_the_owned_worker_and_never_restarts_it(self):
        self.accepted()
        entered, release = threading.Event(), threading.Event()
        def paused(*args):
            entered.set()
            release.wait(2)
            return NativeResult('ok', value={'continued': True})
        self.adapter.continue_queued.side_effect = paused
        self.worker.start()
        self.worker.notify()
        try:
            self.assertTrue(entered.wait(2))
            closing = threading.Thread(target=self.worker.close)
            closing.start()
            # close sets its flag before joining the still-owned RPC thread.
            for _ in range(100):
                with self.worker.mutex:
                    if self.worker.closed:
                        break
                threading.Event().wait(.001)
            self.assertTrue(self.worker.closed)
            self.assertFalse(self.worker.notify())
            self.assertTrue(closing.is_alive())
        finally:
            release.set()
        closing.join(2)
        self.assertFalse(closing.is_alive())
        self.assertFalse(self.worker.thread.is_alive())
        self.worker.start()
        self.assertFalse(self.worker.thread.is_alive())

    def test_removed_agent_and_damaged_state_fail_closed_without_recreation(self):
        self.accepted()
        self.assertTrue(self.worker.notify())
        token = self.worker.pending
        (self.directory / 'inbox-state.json').write_text('{broken')
        self.assertTrue(self.worker._work(token))
        self.adapter.continue_queued.assert_not_called()
        import shutil
        shutil.rmtree(self.directory)
        self.assertFalse(self.worker.notify())
        self.assertTrue(self.worker._work(token))
        self.assertFalse(self.directory.exists())

    def test_runner_owns_and_drains_continuation_before_closing_the_relay(self):
        from multiverse.runtime import runner, rpc
        request_path = self.directory / 'launches' / self.launch / 'request.json'
        request = read(request_path)
        request['attachment']['observe_status'] = True
        write(request_path, request)
        process, proxy, watcher, worker = Mock(pid=123), Mock(), Mock(), Mock()
        process.poll.return_value = process.wait.return_value = 0
        ordering = Mock()
        ordering.attach_mock(worker.close, 'worker_close')
        ordering.attach_mock(proxy.close, 'relay_close')
        with patch.object(runner.sys, 'argv', ['runner', str(request_path.parent)]), \
                patch.object(runner, 'process_started', return_value=None), \
                patch.object(runner.subprocess, 'Popen', return_value=process), \
                patch.object(rpc, 'LaunchProxy', return_value=proxy), \
                patch.object(runner, 'StatusWatcher', return_value=watcher), \
                patch('multiverse.runtime.queue_continuation.QueueContinuation', return_value=worker), \
                patch.object(runner, 'launch_diagnostic', return_value={}):
            self.assertEqual(runner.main(), 0)
        worker.start.assert_called_once()
        worker.close.assert_called_once()
        watcher.start.assert_called_once()
        self.assertIs(watcher.queue_continuation, worker)
        self.assertEqual([call[0] for call in ordering.mock_calls], ['worker_close', 'relay_close'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (IdleContinuationTriggerTest, QueueContinuationWorkerTest)
                              for name in cls.__dict__ if name.startswith('test_'))
