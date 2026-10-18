"""The idle-time worker continues owned accepted follow-ups without submitting them again."""
import fcntl
import shutil
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.runtime import native
from multiverse.runtime.queue_continuation import QueueContinuation
from multiverse.harnesses.types import NativeResult
from multiverse.store import read, write
from multiverse.universe import agents
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

    def test_continuation_runs_only_for_owned_accepted_followups_of_the_idle_launch(self):
        """The background worker continues only owned, accepted, unconsumed follow-ups of the current idle, ready
        launch; fragile because selection crosses two files and about 26 conditions, and a wrong row starts an extra
        native turn or another session's queue (1635478)."""
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
        with self.subTest('only unowned, inactive or consumed receipts'):
            self.assertTrue(self.once())
            self.adapter.continue_queued.assert_not_called()
        message = self.accepted()
        state = (self.directory / 'inbox-state.json').read_bytes()
        log = (self.directory / 'inbox.jsonl').read_bytes()
        with self.subTest('status changed after the notification'):
            self.assertTrue(self.worker.notify())
            token = self.worker.pending
            write(self.directory / 'runtime.json', {**self.facts, 'native_status_version': 1})
            self.assertTrue(self.worker._work(token))
            self.adapter.continue_queued.assert_not_called()
        write(self.directory / 'runtime.json', self.facts)
        self.worker.pending = None
        with self.subTest('current accepted follow-up'):
            self.assertTrue(self.once())
            args = self.adapter.continue_queued.call_args.args
            self.assertEqual(args[0].native_id, self.properties['native_session_id'])
            self.assertEqual(args[1], {message['id']: 'queue-' + message['id']})
            self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), state)
            self.assertEqual((self.directory / 'inbox.jsonl').read_bytes(), log)
            self.assertFalse(self.worker.notify())
            self.adapter.continue_queued.assert_called_once()
            self.assertEqual(agents.runtime(self.directory)['queue_continuation']['value'], {'continued': True})

    def test_damaged_ledger_or_removed_agent_fails_closed(self):
        """A damaged ledger or a removed agent stops continuation with a named diagnostic and recreates nothing; a break
        goes unseen because the worker runs in the background."""
        self.accepted('valid accepted receipt')
        damaged = self.accepted('damaged accepted receipt')
        path = self.directory / 'inbox-state.json'
        with self.subTest('wrong-typed receipt'):
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
        with self.subTest('unreadable ledger'):
            path.write_text('{broken')
            self.assertTrue(self.worker.notify())
            token = self.worker.pending
            self.assertTrue(self.worker._work(token))
            self.adapter.continue_queued.assert_not_called()
        with self.subTest('removed agent'):
            shutil.rmtree(self.directory)
            self.assertFalse(self.worker.notify())
            self.assertTrue(self.worker._work(token))
            self.assertFalse(self.directory.exists())

    def test_completion_token_rules(self):
        """The same accepted set is checked once, even after an ambiguous start, new acceptance starts a new check
        without another native notification, and a busy lock retries later; fragile because the token mixes the status
        version, the ledger file's stat and the accepted set, and an error repeats native starts or strands a
        message."""
        continued = self.adapter.continue_queued
        first = self.accepted()
        with self.subTest('busy locks'):
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
            continued.assert_called_once()
        with self.subTest('receipt-only update'):
            update_message(self.directory, first['id'], native_receipt={
                'queued_submission_id': 'queue-' + first['id'], 'transcript_confirmed': False,
                'observed_by_check': True})
            self.once()
            continued.assert_called_once()
        with self.subTest('acceptance after idle'):
            self.accepted('not yet accepted', submission_state='pending')
            self.once()
            continued.assert_called_once()
            self.assertFalse(self.worker.notify())
            message = self.accepted('accepted after idle')
            self.once()
            self.assertEqual(continued.call_count, 2)
            self.assertEqual(continued.call_args.args[1], {first['id']: 'queue-' + first['id'],
                                                           message['id']: 'queue-' + message['id']})
            self.assertFalse(self.worker.notify())
        with self.subTest('ambiguous transport'):
            self.accepted('accepted before an ambiguous start')
            state = (self.directory / 'inbox-state.json').read_bytes()
            continued.side_effect = TimeoutError('ambiguous native start')
            self.once()
            self.assertFalse(self.worker.notify())
            self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), state)
            self.assertIn('ambiguous native start', agents.runtime(self.directory)['queue_continuation']['reason'])
            self.assertEqual(continued.call_count, 3)

    def test_acceptance_during_a_scan_is_not_hidden_by_its_completion_token(self):
        """Acceptance that arrives during the native call is not hidden by the scan's completion token; fragile because
        it happens only inside the RPC window, and the message is stranded."""
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

    def test_shutdown_drains_the_worker_before_the_relay_closes(self):
        """No native operation starts after close, and the runner drains the worker before closing the relay; fragile
        because these are thread races at runner exit, never seen interactively."""
        self.accepted()
        with self.subTest('close during the accepted scan'):
            scan = self.worker._accepted
            def closing_scan(session):
                selected = scan(session)
                self.worker.close()
                return selected
            with patch.object(self.worker, '_accepted', side_effect=closing_scan):
                self.once()
            self.adapter.continue_queued.assert_not_called()
        with self.subTest('close drains the owned worker'):
            worker = QueueContinuation(self.directory, self.launch, self.root)
            self.addCleanup(worker.close)
            entered, release = threading.Event(), threading.Event()
            def paused(*args):
                entered.set()
                release.wait(2)
                return NativeResult('ok', value={'continued': True})
            self.adapter.continue_queued.side_effect = paused
            worker.start()
            worker.notify()
            try:
                self.assertTrue(entered.wait(2))
                closing = threading.Thread(target=worker.close)
                closing.start()
                # close sets its flag before joining the still-owned RPC thread.
                for _ in range(100):
                    with worker.mutex:
                        if worker.closed:
                            break
                    threading.Event().wait(.001)
                self.assertTrue(worker.closed)
                self.assertFalse(worker.notify())
                self.assertTrue(closing.is_alive())
            finally:
                release.set()
            closing.join(2)
            self.assertFalse(closing.is_alive())
            self.assertFalse(worker.thread.is_alive())
            worker.start()
            self.assertFalse(worker.thread.is_alive())
        with self.subTest('runner closes the worker before the relay'):
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
    return unittest.TestSuite(QueueContinuationWorkerTest(name) for name in QueueContinuationWorkerTest.__dict__
                              if name.startswith('test_'))
