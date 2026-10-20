"""Exact-thread recovery safety, bounded outages and real idle TUI recovery."""
from copy import deepcopy
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.runtime import recovery
from multiverse.harnesses.codex.protocol import recovery_check
from multiverse.harnesses.types import NativeEndpoint, NativeResult, SessionHandle, NativeServiceHandle, ServiceIdentity, AttachmentSpec
from multiverse.runtime.runner import RelayPublisher, StatusWatcher, UsagePublisher
from multiverse.universe.mailbox import append_message, update_message
from multiverse.store import now, read, write
from multiverse.terminal.tmux import Tmux
from native_safety import require_checked_codex, isolated_home
from native_processes import PrivateDaemonGroups
from test_multiverse import TerminalBase, until


class RecoveryProtocolTest(unittest.TestCase):
    def setUp(self):
        self.session = SessionHandle('agent', 'codex', '/tmp/work', native_id='exact-thread',
                                     endpoint=NativeEndpoint('unix-websocket', '/tmp/native.sock'))
        self.thread = {'id': 'exact-thread', 'cwd': '/tmp/work', 'status': {'type': 'idle'}}
        self.replies = {'initialize': {}, 'thread/read': {'thread': self.thread},
                        'thread/loaded/list': {'data': []},
                        'thread/turns/list': {'data': [{'status': 'completed'}]},
                        'thread/queue/list': {'data': []}, 'thread/backgroundTerminals/list': {'data': []}}

    def execute(self, transform=None):
        plan, self.requests = recovery_check(self.session), []
        request = next(plan)
        while True:
            self.requests.append(request)
            response = {'result': deepcopy(self.replies[request.method])}
            if transform:
                response = transform(request, response, len(self.requests))
            try:
                request = plan.send(response)
            except StopIteration as done:
                return done.value

    def test_idle_proof_reads_codex_state_read_only(self):
        """The read-only idle check over Codex status, activeFlags, turn status, queue and background terminals
        qualifies only an exact idle thread; fragile because each is a Codex shape that changes, and a wrong idle
        restarts an agent mid-work."""
        self.assertEqual(self.execute().state, 'ok')
        self.assertEqual([r.method for r in self.requests], ['initialize', 'thread/read',
            'thread/turns/list', 'thread/queue/list', 'thread/backgroundTerminals/list', 'thread/read'])
        self.assertTrue(all(r.params.get('threadId') == 'exact-thread' for r in self.requests[1:]))
        for change in ({'id': 'other'}, {'cwd': '/tmp/other'}, {'status': {'type': 'notLoaded'}},
                       {'status': {'type': 'active', 'activeFlags': []}},
                       {'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']}},
                       {'status': {'type': 'active', 'activeFlags': ['waitingOnUserInput']}},
                       {'canAcceptDirectInput': False}):
            with self.subTest(change=change):
                self.replies['thread/read']['thread'] = {**self.thread, **change}
                self.assertNotEqual(self.execute().state, 'ok')
        self.replies['thread/read']['thread'] = self.thread
        for method, data in (('thread/backgroundTerminals/list', [{'processId': 'running'}]),
                             ('thread/queue/list', [{'id': 'queued'}]),
                             ('thread/turns/list', [{'status': 'inProgress'}]),
                             ('thread/turns/list', [{'status': 'future'}])):
            with self.subTest(method=method, data=data):
                original = self.replies[method]
                self.replies[method] = {'data': data}
                self.assertNotEqual(self.execute().state, 'ok')
                self.replies[method] = original
        def unsupported(request, response, index):
            return {'error': {'code': -32601, 'message': 'unsupported'}} if index == 5 else response
        def malformed(request, response, index):
            return {'result': {'data': 'wrong'}} if index == 5 else response
        def active(request, response, index):
            if index == 6:
                response['result']['thread']['status'] = {'type': 'active', 'activeFlags': []}
            return response
        with self.subTest('unsupported inventory'):
            self.assertNotEqual(self.execute(unsupported).state, 'ok')
        with self.subTest('malformed inventory'), self.assertRaises(ValueError):
            self.execute(malformed)
        with self.subTest('activity before the final read'):
            self.assertNotEqual(self.execute(active).state, 'ok')


class RecoveryWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mv2-recovery-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'state/agents' / str(uuid4())
        self.launch, self.native_id = str(uuid4()), str(uuid4())
        self.properties = {'id': self.directory.name, 'name': 'idle', 'universe_id': str(uuid4()),
                           'workspace': str(self.root), 'harness': 'codex', 'native_session_id': self.native_id}
        self.facts = {'launch_id': self.launch, 'state': 'running', 'status': 'idle', 'pid': 123,
                      'startup_checked': True, 'startup_delivery': 'ready',
                      'control_endpoint': {'transport': 'unix-websocket', 'address': '/tmp/unavailable.sock'},
                      'tmux': {'socket': '/fake/tmux.sock', 'session': '$1', 'pane': '%1'},
                      'recovery_snapshot': {'safe': True, 'at': now(), 'native_id': self.native_id,
                                            'status_version': 0}}
        self.facts['native_service'] = asdict(NativeServiceHandle(
            ServiceIdentity('codex', {'CODEX_HOME': '/isolated'}), NativeEndpoint(**self.facts['control_endpoint'])))
        self.facts['attachment'] = asdict(AttachmentSpec('service', 'binding', 'idle-existing-service', True, True))
        self.facts['recovery_snapshot']['service'] = self.facts['native_service']
        self.facts['recovery_snapshot']['workspace'] = self.properties['workspace']
        # Only recovery's own reference: the runner's threads in this process keep the real Popen.
        self.popen = Mock()
        fake = SimpleNamespace(**{**vars(subprocess), 'Popen': self.popen})
        self.enterContext(patch.object(recovery, 'subprocess', fake))
        self.enterContext(patch.object(recovery, 'process_started', return_value=None))
        self.reset()

    def reset(self, **changes):
        shutil.rmtree(self.directory, ignore_errors=True)
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', {**self.facts, **changes})
        (self.directory / '.launch.lock').touch()
        self.popen.reset_mock()

    def publish_loss(self):
        RelayPublisher(self.directory, self.launch)('upstream disappeared')
        return read(self.directory / 'runtime.json')

    def accept(self, attempts, delivery='steer', **tracking):
        message = append_message(self.directory, 'input', {}, delivery=delivery, target_session=self.native_id)
        update_message(self.directory, message['id'], submission_state='accepted', target_session=self.native_id,
                       attempts=attempts, **tracking)

    def test_lost_relay_starts_recovery_once_only_when_qualified(self):
        """A lost relay starts one recovery worker only from a fresh idle proof of the current launch, within the
        restart budget, with no message in flight; fragile because it combines proof age, launch, restart history and
        in-flight messages, and a wrong start replays or loses a message."""
        old, proof = '2000-01-01T00:00:00+00:00', self.facts['recovery_snapshot']
        expired = {**proof, 'at': old, 'reason': 'Exact native thread is idle'}
        restarts = [{'at': now()}, {'at': now()}]
        loss, terminal_exit = 'upstream disappeared', 'terminal connection closed (code 1006): no close frame'
        def status(value):
            return lambda: StatusWatcher(self.directory, self.launch, self.root)._publish(value)
        def uncertain():
            message = append_message(self.directory, 'uncertain', {}, delivery='steer', target_session=self.native_id)
            update_message(self.directory, message['id'], submission_state='unknown', target_session=self.native_id)
        def waiting_for_the_launch_lock():
            append_message(self.directory, 'pending input', {}, delivery='steer', target_session=self.native_id)
        def accepted_after_the_proof():
            self.accept([{'at': now()}], native_receipt={'transcript_confirmed': True})
        def completed_before_the_proof():
            self.accept([{'at': old}], delivery='follow-up', native_receipt={'transcript_confirmed': False})
            self.assertIsNone(recovery.in_flight(self.directory, self.facts, self.native_id, since=now()))
        def accepted_at(attempts):
            def accept():
                self.accept(attempts)
                self.assertIsNotNone(recovery.in_flight(self.directory, self.facts, self.native_id, since=now()))
            return accept
        rows = [('qualified', {}, None, loss, 'checking', None),
                ('no proof', {'recovery_snapshot': None}, None, loss, 'skipped', None),
                ('busy proof', {'recovery_snapshot': {'safe': False, 'reason': 'Background processes are running'}},
                 None, loss, 'skipped', None),
                ('proof of another thread', {'recovery_snapshot': {**proof, 'native_id': 'other'}},
                 None, loss, 'skipped', None),
                ('old proof', {'recovery_snapshot': {**proof, 'at': old}}, None, loss, 'skipped', None),
                ('expired safe proof', {'recovery_snapshot': expired}, None, loss, 'skipped', 'expired'),
                ('status changed since the proof', {'recovery_snapshot': {**proof, 'status_version': 1}},
                 None, loss, 'skipped', None),
                ('native activity', {}, status('waiting'), loss, 'skipped', None),
                ('optional status loss', {}, status('unknown'), loss, 'checking', None),
                ('manual terminal exit', {}, None, terminal_exit, 'skipped', None),
                ('restart limit', {'automatic_restarts': restarts}, None, loss, 'skipped', 'limit'),
                ('uncertain message', {}, uncertain, loss, 'skipped', None),
                ('message waiting for the launch lock', {}, waiting_for_the_launch_lock, loss, 'skipped', None),
                ('confirmed acceptance after the proof', {}, accepted_after_the_proof, loss, 'skipped', None),
                ('completed acceptance before the proof', {}, completed_before_the_proof, loss, 'checking', None)]
        rows += [(('unknown acceptance time', attempts), {}, accepted_at(attempts), loss, 'skipped', None)
                 for attempts in ([], [{'at': 'invalid'}], [{'at': '2000-01-01'}])]
        for case, changes, prepare, cause, expected, reason in rows:
            with self.subTest(case=case):
                self.reset(**changes)
                if prepare:
                    prepare()
                ledger = self.directory / 'inbox-state.json'
                before = ledger.read_bytes() if ledger.exists() else None
                RelayPublisher(self.directory, self.launch)(cause)
                facts = agents.runtime(self.directory)
                self.assertEqual(facts['auto_recovery']['state'], expected)
                if reason:
                    self.assertIn(reason, facts['auto_recovery']['reason'])
                self.assertEqual(ledger.read_bytes() if ledger.exists() else None, before)
                if expected == 'checking':
                    self.assertEqual(facts['native_relay']['state'], 'lost')
                    recovery.schedule(self.directory, self.launch)
                self.assertEqual(self.popen.call_count, 1 if expected == 'checking' else 0)

    def test_late_writers_from_an_old_launch_never_change_the_new_one(self):
        """Every late Codex writer from an old launch (relay publisher, status watcher, usage publisher, recovery
        worker) is ignored and never recreates a removed agent; a break silently corrupts a replacement launch's
        runtime or brings back a removed agent's folder."""
        write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex',
                                                           'settings': {'inbox_bridge': True}})
        usage = {'native_session_id': self.native_id, 'context': {'tokens': 17457}, 'source': 'codex-app-server'}
        def status():
            StatusWatcher(self.directory, self.launch, self.root)._publish('idle')
        def watch():
            watcher = StatusWatcher(self.directory, self.launch, self.root)
            watcher.start()
            watcher.thread.join(5)
            self.assertFalse(watcher.thread.is_alive())
        def relay():
            RelayPublisher(self.directory, self.launch)('old connection closed')
        def publish_usage(value=usage):
            return lambda: UsagePublisher(self.directory, self.launch)(value)
        replacement = {**self.facts, 'launch_id': 'replacement', 'status': 'working'}
        for case, before, late in (('status', replacement, status), ('status watcher', replacement, watch),
                                   ('relay loss', replacement, relay), ('usage', replacement, publish_usage()),
                                   ('relay loss after a stop', {**self.facts, 'state': 'stopped'}, relay),
                                   ('relay loss while ending', {**self.facts, 'state': 'ending'}, relay),
                                   ('usage after a stop', {**self.facts, 'state': 'stopped'}, publish_usage()),
                                   ('usage of another thread', self.facts,
                                    publish_usage({**usage, 'native_session_id': 'other'}))):
            with self.subTest(case=case):
                self.reset(**before)
                late()
                self.assertEqual(agents.runtime(self.directory), before)
                self.popen.assert_not_called()
        with self.subTest('the current launch still publishes usage'):
            self.reset()
            publish_usage()()
            self.assertEqual(agents.runtime(self.directory)['usage']['context'], usage['context'])
        for case, late in (('status', status), ('relay loss', relay), ('usage', publish_usage()),
                           ('recovery worker', lambda: recovery.run(self.directory, self.launch))):
            with self.subTest(removed=case):
                self.reset()
                if case == 'recovery worker':
                    self.publish_loss()
                shutil.rmtree(self.directory)
                late()
                self.assertFalse(self.directory.exists())
        for changes in ({'ending_at': now(), 'auto_recovery': {'state': 'cancelled'}}, {'launch_id': 'replacement'}):
            with self.subTest(during_the_check=changes):
                self.reset()
                self.publish_loss()
                def racing_check(*args):
                    write(self.directory / 'runtime.json', {**read(self.directory / 'runtime.json'), **changes})
                    return NativeResult('ok')
                with patch.object(recovery, 'check', side_effect=racing_check), \
                        patch.object(lifecycle, 'start') as start:
                    recovery.run(self.directory, self.launch)
                start.assert_not_called()
                facts = read(self.directory / 'runtime.json')
                self.assertEqual(facts['launch_id'], changes.get('launch_id', self.launch))
                if 'auto_recovery' in changes:
                    self.assertEqual(facts['auto_recovery']['state'], 'cancelled')

    def recover(self, check, backoff=(0, 1), timeout=None, sleep=None):
        """Run the worker after a fresh loss on a fake clock; self.elapsed is the time it slept."""
        self.reset()
        self.publish_loss()
        self.elapsed = [0.]
        def advance(seconds):
            self.elapsed[0] += seconds
        self.backend = Mock()
        with patch.object(recovery.time, 'monotonic', side_effect=lambda: self.elapsed[0]), \
                patch.object(recovery.time, 'sleep', side_effect=sleep or advance), \
                patch.object(recovery, 'BACKOFF', backoff), \
                patch.object(recovery, 'RESTORE_TIMEOUT', recovery.RESTORE_TIMEOUT if timeout is None else timeout), \
                patch.object(recovery, 'check', side_effect=check) as checked, \
                patch.object(recovery, 'owned', return_value=(self.backend, '%1')) as owned, \
                patch.object(recovery, 'frontend_children', return_value=True), \
                patch.object(lifecycle, 'start') as start:
            recovery.run(self.directory, self.launch)
        start.assert_not_called()
        return checked, owned, agents.runtime(self.directory)

    def test_retry_and_restoration_deadlines(self):
        """With a fake clock, outage retries and the wait for conversation restoration stay within their deadlines
        and never restart late; fragile because of timing, and a break loops forever or restarts an agent after the
        user moved on (59108e5)."""
        unrestored = 'Codex did not restore this conversation after its restart'
        pending = NativeResult('pending', unrestored)
        with self.subTest('outage retries are bounded'):
            checked, owned, facts = self.recover(ConnectionRefusedError('daemon down'), backoff=(0, 0, 0))
            self.assertEqual(checked.call_count, 3)
            self.assertEqual(facts['auto_recovery']['state'], 'failed')
            self.assertEqual(facts['auto_recovery']['attempts'], 3)
            self.assertIn('timed out', facts['native_relay']['reason'])
        with self.subTest('restoration waits past transport retries until full qualification'):
            checked, owned, facts = self.recover([pending, pending, pending, NativeResult('ok')])
            self.assertEqual(checked.call_count, 4)
            owned.assert_called_once()
            self.backend.signal_child.assert_not_called()
            self.assertIn('terminal child processes', facts['auto_recovery']['reason'])
        with self.subTest('an unrestored conversation stops at the restoration bound'):
            checked, owned, facts = self.recover(lambda *args: pending, timeout=5)
            self.assertEqual(self.elapsed[0], 5)
            self.assertGreater(checked.call_count, 1)
            owned.assert_not_called()
            self.assertEqual(facts['auto_recovery']['state'], 'skipped')
            self.assertIn(unrestored, facts['auto_recovery']['reason'])
            self.assertEqual(facts['launch_id'], self.launch)
        for reason in ('Native thread is active', 'Native status is unknown', 'Wrong native identity'):
            with self.subTest('restoration never retries a definite refusal', reason=reason):
                checked, owned, facts = self.recover([NativeResult('pending', 'Restoring'),
                                                      NativeResult('unavailable', reason)])
                self.assertEqual(checked.call_count, 2)
                owned.assert_not_called()
                self.assertIn(reason, facts['auto_recovery']['reason'])
        def late_idle(*args):
            if self.elapsed[0] == 0:
                return NativeResult('pending', 'Conversation was not restored in time')
            self.elapsed[0] = 6  # The bounded RPC finished after the restoration budget.
            return NativeResult('ok')
        with self.subTest('a late idle qualification cannot restart after the deadline'):
            checked, owned, facts = self.recover(late_idle, timeout=5)
            owned.assert_not_called()
            self.assertIn('not restored in time', facts['auto_recovery']['reason'])
        def activity(_delay):
            StatusWatcher(self.directory, self.launch, self.root)._publish('working')
        with self.subTest('native activity while waiting for restoration cancels it'):
            checked, owned, facts = self.recover(lambda *args: NativeResult('pending', 'Restoring'),
                                                 backoff=recovery.BACKOFF, sleep=activity)
            checked.assert_called_once()
            owned.assert_not_called()
            self.assertIn('invalidated', facts['auto_recovery']['reason'])
        calls = []
        def pending_then_down(*args):
            calls.append(None)
            if len(calls) == 1:
                return NativeResult('pending', 'Restoring')
            raise ConnectionRefusedError('daemon unavailable')
        with self.subTest('a pending restoration keeps the transport failure budget'):
            checked, owned, facts = self.recover(pending_then_down, backoff=(0, 1, 2))
            self.assertEqual(len(calls), 4, 'Three failed transports remain bounded after a pending read')
            self.assertEqual(facts['auto_recovery']['state'], 'failed')
            owned.assert_not_called()

    def test_idle_proof_freshness_under_racing_events(self):
        """Each order of watcher, relay and acceptance events keeps, invalidates or refreshes the idle proof
        correctly; fragile because these callbacks race on separate threads, and a stale proof restarts a working
        agent (1105de8)."""
        class Clock:
            def __init__(self):
                self.calls = 0
            def wait(self, seconds):
                self.calls += 1
                return self.calls > 2
        def monitor(**check):
            watching = recovery.RecoveryMonitor(self.directory, self.launch, self.root)
            watching.stopped = Clock()
            with patch.object(recovery, 'check', **check):
                watching._run()
        with self.subTest('a duplicate idle after the loss keeps the proof but needs a fresh check'):
            self.reset()
            watcher = StatusWatcher(self.directory, self.launch, self.root)
            watcher._publish('unknown')  # The observer's transport can close first.
            self.publish_loss()
            watcher._publish('idle')  # Already queued before the primary connection closed.
            facts = agents.runtime(self.directory)
            self.assertEqual(facts['status'], 'unknown', 'A lost relay must not appear connected')
            self.assertEqual(facts.get('native_status_version', 0), 0)
            self.assertEqual(facts['recovery_snapshot'], self.facts['recovery_snapshot'])
            with patch.object(recovery, 'BACKOFF', (0,)), patch.object(recovery, 'check',
                    return_value=NativeResult('unavailable', 'Fresh native check refused')) as checked, \
                    patch.object(lifecycle, 'start') as start:
                recovery.run(self.directory, self.launch)
            checked.assert_called_once()
            start.assert_not_called()
            self.assertIn('Fresh native check refused', agents.runtime(self.directory)['auto_recovery']['reason'])
        for status in ('working', 'waiting'):
            with self.subTest('real activity after the loss invalidates the proof', status=status):
                self.reset()
                self.publish_loss()
                StatusWatcher(self.directory, self.launch, self.root)._publish(status)
                facts = agents.runtime(self.directory)
                self.assertEqual(facts['status'], 'unknown')
                self.assertEqual(facts['native_status_version'], 1)
                self.assertIsNone(facts['recovery_snapshot'])
                with patch.object(recovery, 'check') as checked, patch.object(lifecycle, 'start') as start:
                    recovery.run(self.directory, self.launch)
                checked.assert_not_called()
                start.assert_not_called()
                self.assertIn('invalidated', agents.runtime(self.directory)['auto_recovery']['reason'])
        with self.subTest("the observer's own unknown does not hide a real transition"):
            self.reset()
            watcher = StatusWatcher(self.directory, self.launch, self.root)
            for status in ('working', 'unknown', 'idle'):
                watcher._publish(status)
            facts = agents.runtime(self.directory)
            self.assertEqual(facts['status'], 'idle')
            self.assertEqual(facts['native_status_version'], 2)
            self.assertIsNone(facts['recovery_snapshot'])
        def racing_acceptance(*args):
            self.accept([{'at': now()}])
            return NativeResult('ok')
        with self.subTest('a message accepted during the check invalidates the sample'):
            self.reset()
            monitor(side_effect=racing_acceptance)
            proof = read(self.directory / 'runtime.json')['recovery_snapshot']
            self.assertFalse(proof['safe'])
            self.assertIn('accepted after', proof['reason'])
        with self.subTest('a pending restoration keeps the proof without refreshing it'):
            self.reset()
            monitor(return_value=NativeResult('pending', 'Restoring'))
            self.assertEqual(agents.runtime(self.directory)['recovery_snapshot'], self.facts['recovery_snapshot'])
            self.assertEqual(self.publish_loss()['auto_recovery']['state'], 'checking')

    def test_recovery_never_signals_busy_or_foreign_panes(self):
        """Recovery never signals a pane with live children and never respawns a pane it does not own by agent,
        universe and launch tags; a break kills work or another agent's pane, silently."""
        with self.subTest('fresh native background work'):
            self.reset()
            self.publish_loss()
            busy = NativeResult('unavailable', 'Background processes running')
            with patch.object(recovery, 'check', return_value=busy), \
                    patch.object(lifecycle, 'start') as start, patch.object(recovery, 'owned') as owned:
                recovery.run(self.directory, self.launch)
            start.assert_not_called()
            owned.assert_not_called()
        with self.subTest('frontend children'):
            self.reset()
            self.publish_loss()
            backend = Mock()
            with patch.object(recovery, 'check', return_value=NativeResult('ok')), \
                    patch.object(recovery, 'owned', return_value=(backend, '%1')), \
                    patch.object(recovery, 'frontend_children', return_value=True), \
                    patch.object(lifecycle, 'start') as start:
                recovery.run(self.directory, self.launch)
            backend.signal_child.assert_not_called()
            start.assert_not_called()
        pane = {'pane_id': '%1', 'pane_dead': '1', '@mv2_agent_id': self.properties['id'],
                '@mv2_universe_id': self.properties['universe_id'], '@mv2_launch_id': self.launch}
        backend = Tmux('/fake/tmux.sock')
        with patch.object(backend, 'run') as command:
            for change in ({'pane_dead': '0'}, {'@mv2_launch_id': 'replacement'},
                           {'@mv2_agent_id': 'other'}, {'@mv2_universe_id': 'other'}):
                with self.subTest(change=change), patch.object(backend, 'snapshot',
                        return_value=({'%1': {**pane, **change}}, None)), self.assertRaises(ValueError):
                    backend.respawn(self.properties, self.facts, self.root, ['runner'])
            command.assert_not_called()
            with patch.object(backend, 'snapshot', return_value=({'%1': pane}, None)):
                self.assertEqual(backend.respawn(self.properties, self.facts, self.root, ['runner']),
                                 self.facts['tmux'])
            command.assert_called_once_with('respawn-pane', '-t', '%1', '-c', str(self.root), 'runner')


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class RecoveryLiveTest(TerminalBase):
    def setUp(self):
        super().setUp()
        require_checked_codex(self)
        self.requests = []
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                title = 'title' in value.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
                inputs = value.get('input', [])
                if not title:
                    parent.requests.append(value)
                item = {'type': 'message', 'role': 'assistant', 'id': 'message',
                        'content': [{'type': 'output_text', 'text': '{"title":"Recovery test"}' if title else 'Done.'}]}
                if (not title and 'make-background' in json.dumps(inputs)
                        and (not inputs or inputs[-1].get('type') != 'function_call_output')):
                    item = {'type': 'function_call', 'call_id': 'background', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': 'sleep 90', 'yield_time_ms': 10, 'max_output_tokens': 100})}
                events = [{'type': 'response.created', 'response': {'id': 'response'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': 'response',
                           'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.home = isolated_home(self.work / 'home', config=
            'model="gpt-6-sol"\nmodel_provider="offline"\n[features]\nplugins=false\n'
            '[model_providers.offline]\nname="Offline"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'base_url="http://127.0.0.1:{self.server.server_port}/v1"\n'
            f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n')
        real = str(Path(shutil.which('codex')).resolve())
        managed = self.home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True); managed.symlink_to(real)
        self.commands = self.work / 'codex-commands.jsonl'
        shim = self.work / 'codex-shim'
        shim.write_text(f'#!{sys.executable}\nimport json,os,sys\n'
                        f'with open({str(self.commands)!r}, "a") as out: out.write(json.dumps(sys.argv[1:4])+"\\n")\n'
                        f'os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n')
        shim.chmod(0o700)
        self.env = {**os.environ, 'CODEX_HOME': str(self.home), 'CODEX_SQLITE_HOME': str(self.home),
                    'NO_PROXY': '127.0.0.1,localhost', 'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '',
                    'http_proxy': '', 'https_proxy': '', 'all_proxy': ''}
        self.daemon_groups = PrivateDaemonGroups()
        self.enterContext(self.daemon_groups.observe_starts())
        def stop_owned_daemon():
            try:
                self.daemon('stop')
            finally:
                self.daemon_groups.close()
        self.addCleanup(stop_owned_daemon)
        spec = read(self.root / 'harnesses/codex/harness.json')
        spec['settings'] = {'executable': str(shim), 'inbox_bridge': True,
                            'args': ['--dangerously-bypass-approvals-and-sandbox']}
        write(self.root / 'harnesses/codex/harness.json', spec)

    def daemon(self, command):
        process = subprocess.run(['codex', 'app-server', 'daemon', command], env=self.env,
                                 cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        if command in ('start', 'restart') and 'pid' in result:
            self.daemon_groups.remember(result)
        return result

    def launch(self, name, task):
        with patch.dict(os.environ, self.env):
            result = lifecycle.start(self.root, name, task, socket_path=self.socket, wait=True, wait_timeout=30)
        self.assertTrue(result['ready'], result)
        directory = agents.directory(self.root, result['id'])
        def cleanup():
            try:
                lifecycle.kill(self.root, result['id'])
            except (OSError, ValueError, RuntimeError):
                pass
        self.addCleanup(cleanup)
        return result, directory

    @contextmanager
    def recovery_trace(self, directory):
        """Keep bounded metadata and print it before fixture cleanup on failure."""
        trace, stopped = deque(maxlen=256), threading.Event()
        keys = ('launch_id', 'state', 'status', 'native_status_value', 'native_status_version',
                'pid', 'pid_started', 'runner_pid', 'runner_started', 'startup_checked',
                'startup_delivery', 'native_relay', 'recovery_snapshot', 'auto_recovery',
                'ending_at', 'reason', 'error', 'tmux')
        def snapshot():
            try:
                return {key: value for key, value in agents.runtime(directory).items() if key in keys}
            except (OSError, ValueError) as error:
                return {'read_error': str(error)}
        initial = snapshot()
        trace.append({'at': now(), 'runtime': initial})
        def observe():
            previous = initial
            while not stopped.is_set():
                facts = snapshot()
                if facts != previous:
                    trace.append({'at': now(), 'runtime': facts})
                    previous = facts
                stopped.wait(.05)
        observer = threading.Thread(target=observe, name='test-recovery-trace', daemon=True)
        observer.start()
        try:
            yield
        except BaseException:
            stopped.set()
            observer.join(2)
            try:
                facts, records = snapshot(), {}
                for launch in sorted((directory / 'launches').glob('*')):
                    records[launch.name] = {'ready': (launch / 'ready').exists(),
                                            'request_saved': (launch / 'request.json').exists()}
                    for path in [launch / 'registration.json', launch / 'cleanup.json',
                                 *sorted((launch / 'roles').glob('*.json'))]:
                        if path.exists():
                            try:
                                value = read(path)
                            except (OSError, ValueError) as error:
                                value = {'read_error': str(error)}
                            records[launch.name][str(path.relative_to(launch))] = value
                try:
                    pane = self.backend.run('capture-pane', '-p', '-t', facts['tmux']['pane'])
                except (KeyError, OSError, RuntimeError) as error:
                    pane = 'Pane unavailable: ' + str(error)
                print('\nRECOVERY FAILURE EVIDENCE\n' + json.dumps({
                    'fixture': str(self.work), 'runtime': facts, 'trace': list(trace),
                    'launches': records, 'pane': pane}, indent=2), file=sys.stderr, flush=True)
            except Exception as error:
                print('Could not collect recovery failure evidence: ' + str(error), file=sys.stderr, flush=True)
            raise
        finally:
            stopped.set()
            observer.join(2)

    def test_idle_agent_reconnects_and_busy_agent_stays_down_after_daemon_restart(self):
        """On live Codex, with an idle and a busy agent on one private daemon and one restart, the idle one reconnects
        in the same pane with no model turn or extra daemon start while Codex really reports the other's background
        job; fragile because it rests on Codex restart and restoration internals the unit tables cannot see."""
        _, directory = self.launch('idle', 'idle-recovery-probe')
        _, busy = self.launch('busy', 'make-background')
        with self.recovery_trace(directory), self.recovery_trace(busy):
            until(lambda: (agents.runtime(directory).get('recovery_snapshot') or {}).get('safe'), seconds=30)
            def background():
                proof = agents.runtime(busy).get('recovery_snapshot') or {}
                return not proof.get('safe') and 'background' in proof.get('reason', '').lower()
            until(background, seconds=30)
            native_id = read(directory / 'properties.json')['native_session_id']
            old, busy_old = agents.runtime(directory), agents.runtime(busy)
            UsagePublisher(directory, old['launch_id'])({'native_session_id': native_id,
                                                       'context': {'tokens': 17457}, 'source': 'codex-app-server'})
            measurement = agents.runtime(directory)['usage']
            count = len(self.requests)
            fd, slave = pty.openpty()
            client = subprocess.Popen([*self.backend.prefix, 'attach-session', '-t', old['tmux']['session']],
                stdin=slave, stdout=slave, stderr=slave,
                env={**{key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')},
                     'TERM': 'xterm-256color'})
            os.close(slave)
            def close_client():
                if client.poll() is None:
                    client.terminate()
                    client.wait(timeout=5)
                os.close(fd)
            self.addCleanup(close_client)
            until(lambda: self.backend.run('list-clients', '-t', old['tmux']['pane']), seconds=5)
            clients = self.backend.run('list-clients', '-t', old['tmux']['pane'], '-F', '#{client_name}')
            started = [json.loads(line) for line in self.commands.read_text().splitlines()].count(
                ['app-server', 'daemon', 'start'])
            self.daemon('restart')  # Only this fixture's isolated daemon.
            def ready():
                facts = agents.runtime(directory)
                return (facts.get('launch_id') != old['launch_id'] and facts.get('startup_checked')
                        and (facts.get('auto_recovery') or {}).get('state') == 'reconnected')
            until(ready, seconds=60)
            current = agents.runtime(directory)
            self.assertEqual(read(directory / 'properties.json')['native_session_id'], native_id)
            self.assertEqual(current['tmux'], old['tmux'])
            self.assertEqual(self.backend.run('list-clients', '-t', current['tmux']['pane'], '-F', '#{client_name}'),
                             clients)
            self.assertIsNone(client.poll())
            self.assertEqual(current['last_usage'], measurement)
            self.assertEqual(current['automatic_restarts'][-1]['from_launch_id'], old['launch_id'])
            request = read(directory / 'launches' / current['launch_id'] / 'request.json')
            self.assertEqual(request['rpc_proxy']['settings']['resume_permissions'],
                             {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'})
            self.assertIsNone(request['fallback'])
            self.assertEqual(len(self.requests), count, 'Recovery must never start a model turn or replay the task')
            commands = [json.loads(line) for line in self.commands.read_text().splitlines()]
            self.assertEqual(commands.count(['app-server', 'daemon', 'start']), started)
            self.assertFalse(any(command[:2] == ['app-server', 'daemon'] and command[2] != 'start'
                                 for command in commands))
            self.assertIn(current['launch_id'], self.backend.run('show-option', '-v', '-t', current['tmux']['session'],
                                                                '@mv2_status_command'))
            until(lambda: (agents.runtime(busy).get('native_relay') or {}).get('state') == 'lost', seconds=10)
            stayed = agents.runtime(busy)
            self.assertEqual(stayed['launch_id'], busy_old['launch_id'])
            self.assertEqual(stayed['auto_recovery']['state'], 'skipped')
            self.assertIn('background', stayed['auto_recovery']['reason'].lower())
            self.assertNotIn('automatic_restarts', stayed)
