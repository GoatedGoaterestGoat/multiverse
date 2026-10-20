"""An unmanaged native side conversation never replaces the mv2 primary."""
from copy import deepcopy
import unittest
from uuid import uuid4

from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.codex.status import notification as status_notification
from multiverse.harnesses.types import LocalProtocolError, SessionHandle


class CodexSideTest(unittest.TestCase):
    def setUp(self):
        self.main, self.side = str(uuid4()), str(uuid4())
        self.settings = {'native_id': self.main, 'workspace': '/fixture/workspace',
                         'bootstrap': 'primary bootstrap', 'roots': [],
                         'resume_permissions': {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}}
        self.protocol = LaunchProtocol(self.settings)
        self.protocol.bound = True
        self.owner = True
        self.view = self.connection('tui', lambda: self.owner)
        self.initialize(self.view)

    def connection(self, name, owner):
        factory = getattr(self.protocol, 'for_connection', None)
        return factory(name, owner) if factory else self.protocol

    def initialize(self, view, version='codex/0.163.0'):
        view.outgoing({'id': 1, 'method': 'initialize', 'params': {}})
        view.incoming({'id': 1, 'result': {'userAgent': version}})

    def fork(self, identifier=10, **changes):
        return {'id': identifier, 'method': 'thread/fork', 'params': {
            'threadId': self.main, 'ephemeral': True, 'excludeTurns': True, **changes}}

    def forked(self, identifier=10, **changes):
        return {'id': identifier, 'result': {'thread': {
            'id': self.side, 'forkedFromId': self.main, 'ephemeral': True,
            'cwd': '/fixture/workspace', 'path': None, **changes}}}

    def open_side(self):
        request = self.fork()
        self.assertEqual(self.view.outgoing(request), request)
        self.assertIsNone(self.view.incoming(self.forked()))

    def primary_state(self):
        return deepcopy({key: getattr(self.protocol, key) for key in (
            'native_id', 'bound', 'pending', 'pending_method', 'launch_refusal',
            'prompt_request', 'prompt_submitted', 'prompt_error')})

    def test_side_thread_lifecycle_leaves_the_primary_untouched(self):
        """A /btw side thread's whole lifecycle, down to closing one saturated with requests, leaves the primary
        binding, refusal, usage and status untouched and allows the next /btw; a break goes unnoticed because messages,
        status or context quietly come from the side thread, launch identity leaks into its tools, or a full side cannot close."""
        before = self.primary_state()
        error = {'error': {'code': -32603, 'message': 'Native side error'}}
        self.view.outgoing(self.fork())
        self.assertIsNone(self.view.incoming({'id': 10, **error}), 'A native fork error is local and allows a retry')
        with self.subTest('fork tools keep the same identity and exclusions as the primary'):
            self.protocol.settings['agent_environment'] = {'MV2_IDENTITY_MODE': 'codex-agent-v1',
                'MV2_AGENT_ID': 'primary', 'MV2_UNIVERSE': '/fixture/universe', 'MV2_AGENT_DIR': '/fixture/agent'}
            request = self.fork(config={'shell_environment_policy': {
                'set': {'MV2_LAUNCH_ID': 'old', 'MV2_CONTROL_SOCKET': '/old', 'MV2_AGENT_ID': 'wrong',
                        'USER_SETTING': 'keep'}, 'exclude': ['AWS_*']}})
            original = deepcopy(request)
            forwarded = self.view.outgoing(request)
            policy = forwarded['params']['config']['shell_environment_policy']
            self.assertEqual(policy['set'], {**self.protocol.settings['agent_environment'], 'USER_SETTING': 'keep'})
            self.assertEqual(policy['exclude'], ['AWS_*', 'MV2_*'])
            self.assertEqual(request, original, 'The caller frame is not mutated')
            self.view.incoming(self.forked())
        for identifier, method in enumerate(('thread/inject_items', 'turn/start', 'turn/interrupt'), 20):
            request = {'id': identifier, 'method': method, 'params': {'threadId': self.side}}
            self.assertEqual(self.view.outgoing(request), request)
            self.assertIsNone(self.view.incoming({'id': identifier, 'result': {}}))
        session = SessionHandle('agent', 'codex', '/fixture/workspace', native_id=self.main)
        for status in ('idle', 'active'):
            frame = {'method': 'thread/status/changed', 'params': {'threadId': self.side, 'status': {'type': status}}}
            self.assertIsNone(self.view.incoming(frame))
            self.assertIsNone(status_notification(session, frame))
        def usage(native):
            return {'method': 'thread/tokenUsage/updated', 'params': {
                'threadId': native, 'tokenUsage': {'last': {'totalTokens': 12345}, 'modelContextWindow': 100000}}}
        self.assertIsNone(self.view.incoming(usage(self.side)))
        self.assertIsNone(self.view.usage(usage(self.side)))
        self.view.incoming(usage(self.main))
        observed = self.view.usage(usage(self.main))
        self.assertEqual(observed['native_session_id'], self.main)
        self.assertEqual(observed['context']['tokens'], 12345)
        self.assertEqual(self.primary_state(), before)
        # Returning to the primary is a UI selection, not a new launch: no bootstrap or grants are reapplied.
        request = {'id': 30, 'method': 'thread/resume', 'params': {
            'threadId': self.main, 'approvalPolicy': None, 'sandbox': None, 'excludeTurns': True}}
        self.assertEqual(self.view.outgoing(request), request)
        self.assertIsNone(self.view.incoming({'id': 30, 'result': {'thread': {
            'id': self.main, 'cwd': '/fixture/workspace'}}}))
        self.assertEqual(self.primary_state(), before)
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing({'id': 31, 'method': 'thread/resume', 'params': {'threadId': self.side}})
        request = {'id': 40, 'method': 'thread/unsubscribe', 'params': {'threadId': self.side}}
        self.view.outgoing(request)
        self.assertIsNone(self.view.incoming({'id': 40, **error}))
        with self.assertRaises(LocalProtocolError, msg='An unconfirmed close keeps the side open'):
            self.view.outgoing(self.fork(41))
        # A saturated side still has bounded room for the native interrupt, its retry and the close.
        for identifier in range(100, 164):
            self.view.outgoing({'id': identifier, 'method': 'thread/read', 'params': {'threadId': self.side}})
        for identifier, method in ((200, 'turn/interrupt'), (201, 'turn/interrupt'), (202, 'thread/unsubscribe')):
            self.view.outgoing({'id': identifier, 'method': method, 'params': {'threadId': self.side}})
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing({'id': 203, 'method': 'turn/interrupt', 'params': {'threadId': self.side}})
        self.assertEqual(len(self.view.side_requests), 67)
        self.assertIsNone(self.view.incoming({'id': 202, 'result': {'status': 'unsubscribed'}}))
        self.assertIsNone(self.view.side_id)
        self.assertEqual(self.view.side_requests, {})
        self.assertEqual(self.primary_state(), before)
        reopened = self.view.outgoing(self.fork(43))
        self.assertEqual({key: value for key, value in reopened['params'].items() if key != 'config'},
                         self.fork(43)['params'], 'A closed side must not prevent the next /btw')
        self.assertIsNone(self.view.incoming(self.forked(43)))
        self.protocol.launch_refusal = {'message': 'primary refusal'}
        idle = {'type': 'idle'}
        for frame in ({'method': 'thread/status/changed', 'params': {'threadId': self.side, 'status': idle}},
                      {'method': 'thread/tokenUsage/updated', 'params': {'threadId': self.side}},
                      {'method': 'item/tool/call', 'id': 'tool', 'params': {'threadId': self.side}}):
            self.assertIsNone(self.view.incoming(frame))
            self.assertIsNone(self.view.launch_refusal)
            self.assertIsNone(self.view.usage(frame))

    def test_only_the_exact_fork_from_the_current_owner_is_allowed(self):
        """Only the exact 0.163.0 thread/fork shape from the current owner is allowed, and any other request stays
        local; fragile because it pins Codex's fork parameters, and a looser check rebinds the primary."""
        before = self.primary_state()
        for version in (None, 'unparseable', 'codex/0.162.1', 'codex/0.164.0'):
            with self.subTest(version=version):
                view = self.connection('unknown-' + str(version), lambda: True)
                self.initialize(view, version)
                with self.assertRaises(LocalProtocolError):
                    view.outgoing(self.fork())
        for change in ({'ephemeral': None}, {'ephemeral': 1}, {'threadId': self.side},
                       {'path': '/other/rollout'}, {'lastTurnId': 'turn'}, {'beforeTurnId': 'turn'},
                       {'excludeTurns': False}, {'experimentalPredictionMode': True}, {'cwd': '/other/workspace'}):
            with self.subTest(fork=change), self.assertRaises(LocalProtocolError):
                self.view.outgoing(self.fork(**change))
        for identifier in (None, True, [], {}):
            with self.subTest(identifier=identifier):
                with self.assertRaises(LocalProtocolError) as caught:
                    self.view.outgoing(self.fork(identifier))
                self.assertIsNone(caught.exception.request_id)
        for params in (None, [], 'bad'):
            with self.subTest(params=params), self.assertRaises(LocalProtocolError):
                self.view.outgoing({**self.fork(), 'params': params})
        for change in ({'path': '/other/rollout'}, {'history': []}, {'cwd': '/other'}):
            with self.subTest(reselection=change), self.assertRaises(LocalProtocolError):
                self.view.outgoing({'id': 20, 'method': 'thread/resume', 'params': {'threadId': self.main, **change}})
        other = self.connection('control', lambda: False)
        self.initialize(other)
        with self.subTest('another connection'), self.assertRaises(LocalProtocolError):
            other.outgoing(self.fork())
        self.owner = False
        with self.subTest('a former owner'), self.assertRaises(LocalProtocolError):
            self.view.outgoing(self.fork())
        self.owner = True
        for change in ({'id': self.main}, {'id': 'not-a-uuid'}, {'ephemeral': False},
                       {'forkedFromId': str(uuid4())}, {'cwd': '/other'}, {'path': '/stored/side'}):
            with self.subTest(reply=change):
                self.assertEqual(self.view.outgoing(self.fork()), self.fork())
                with self.assertRaises(LocalProtocolError):
                    self.view.incoming(self.forked(**change))
        self.open_side()
        with self.assertRaises(LocalProtocolError) as caught:
            self.view.outgoing({'id': False, 'method': 'turn/start', 'params': {'threadId': self.side}})
        self.assertIsNone(caught.exception.request_id)
        self.assertTrue(self.protocol.bound)
        self.assertEqual(self.primary_state(), before)

    def test_primary_queue_events_preserve_interrupt_order_and_ignore_the_side(self):
        """Primary interrupt ordering controls durable queue pause; a side or late old turn must not silently pause a newer primary turn."""
        observe = getattr(self.view, 'primary_event', lambda direction, frame: None)
        def incoming(kind, turn, status=None, native=None):
            return {'method': 'turn/' + kind, 'params': {'threadId': native or self.main,
                    'turn': {'id': turn, **({'status': status} if status else {})}}}
        def event(kind, turn, **fields):
            return {'kind': kind, 'native_session_id': self.main, 'turn_id': turn, **fields}
        rows = [
            ('incoming', incoming('started', 'one'), event('started', 'one')),
            ('outgoing', {'id': 30, 'method': 'turn/interrupt',
                'params': {'threadId': self.main, 'turnId': 'one'}}, event('interrupt', 'one')),
            ('incoming', {'id': 30, 'result': {}}, None),
            ('incoming', incoming('completed', 'one', 'interrupted'), event('completed', 'one', status='interrupted')),
            ('incoming', incoming('started', 'two'), event('started', 'two')),
            ('incoming', incoming('completed', 'one', 'interrupted'), None),
            ('incoming', incoming('completed', 'two', 'completed'), event('completed', 'two', status='finished')),
            ('incoming', incoming('started', 'side', native=self.side), None),
            ('incoming', incoming('completed', 'side', 'interrupted', self.side), None),
            ('outgoing', {'id': 31, 'method': 'turn/interrupt',
                'params': {'threadId': self.main, 'turnId': ''}}, None),
        ]
        for direction, frame, expected in rows:
            with self.subTest(direction=direction, frame=frame):
                self.assertEqual(observe(direction, frame), expected)
        self.owner = False
        self.assertIsNone(observe('incoming', incoming('started', 'other-client')))
        self.owner = True
        self.view.closed()
        self.assertIsNone(observe('incoming', incoming('started', 'late')))
