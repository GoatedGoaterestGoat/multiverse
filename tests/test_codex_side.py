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

    def test_native_side_open_input_and_close_preserve_primary(self):
        before = self.primary_state()
        self.open_side()
        for identifier, method in enumerate(('thread/inject_items', 'turn/start', 'turn/interrupt'), 20):
            request = {'id': identifier, 'method': method, 'params': {'threadId': self.side}}
            self.assertEqual(self.view.outgoing(request), request)
            self.assertIsNone(self.view.incoming({'id': identifier, 'result': {}}))
        request = {'id': 30, 'method': 'thread/unsubscribe', 'params': {'threadId': self.side}}
        self.assertEqual(self.view.outgoing(request), request)
        self.assertIsNone(self.view.incoming({'id': 30, 'result': {'status': 'unsubscribed'}}))
        self.assertEqual(self.primary_state(), before)
        self.assertEqual(self.view.outgoing(self.fork(31)), self.fork(31), 'A closed side must not prevent the next /btw')

    def test_only_current_owner_can_fork_the_bound_primary(self):
        other = self.connection('control', lambda: False)
        self.initialize(other)
        with self.assertRaises(LocalProtocolError):
            other.outgoing(self.fork())
        self.owner = False
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing(self.fork())
        self.owner = True
        self.assertEqual(self.view.outgoing(self.fork()), self.fork())

    def test_invalid_forks_are_local_and_do_not_change_primary(self):
        before = self.primary_state()
        cases = [{'ephemeral': False}, {'ephemeral': 1}, {'threadId': self.side},
                 {'path': '/other/rollout'}, {'lastTurnId': 'turn'}, {'beforeTurnId': 'turn'},
                 {'excludeTurns': False}, {'experimentalPredictionMode': True},
                 {'cwd': '/other/workspace'}]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(LocalProtocolError):
                self.view.outgoing(self.fork(**change))
            self.assertEqual(self.primary_state(), before)

    def test_malformed_fork_reply_is_local_and_does_not_bind(self):
        before = self.primary_state()
        for change in ({'id': self.main}, {'id': 'not-a-uuid'}, {'ephemeral': False},
                       {'forkedFromId': str(uuid4())}, {'cwd': '/other'}, {'path': '/stored/side'}):
            with self.subTest(change=change):
                self.view.outgoing(self.fork())
                with self.assertRaises(LocalProtocolError):
                    self.view.incoming(self.forked(**change))
                self.assertEqual(self.primary_state(), before)

    def test_side_errors_and_notifications_do_not_expose_primary_refusal_or_usage(self):
        self.open_side()
        self.protocol.launch_refusal = {'message': 'primary refusal'}
        for frame in ({'method': 'thread/status/changed', 'params': {'threadId': self.side, 'status': {'type': 'idle'}}},
                      {'method': 'thread/tokenUsage/updated', 'params': {'threadId': self.side}},
                      {'method': 'item/tool/call', 'id': 'tool', 'params': {'threadId': self.side}}):
            self.assertIsNone(self.view.incoming(frame))
            self.assertIsNone(self.view.launch_refusal)
            self.assertIsNone(self.view.usage(frame))
        session = SessionHandle('agent', 'codex', '/fixture/workspace', native_id=self.main)
        self.assertIsNone(status_notification(session, {'method': 'thread/status/changed',
            'params': {'threadId': self.side, 'status': {'type': 'idle'}}}))

    def test_primary_prompt_and_side_reply_ids_are_scoped_to_the_connection(self):
        request = {'id': 10, 'method': 'turn/start', 'params': {'threadId': self.main, 'input': []}}
        self.view.outgoing(request)
        other = self.connection('control', lambda: False)
        self.initialize(other)
        other.incoming({'id': 10, 'result': {'turn': {'id': 'unrelated'}}})
        self.assertFalse(self.protocol.prompt_submitted)
        self.view.incoming({'id': 10, 'result': {'turn': {'id': 'primary'}}})
        self.assertTrue(self.protocol.prompt_submitted)

    def test_primary_selection_after_side_does_not_reapply_startup_config(self):
        self.open_side()
        before = self.primary_state()
        request = {'id': 20, 'method': 'thread/resume', 'params': {
            'threadId': self.main, 'approvalPolicy': None, 'sandbox': None, 'excludeTurns': True}}
        self.assertEqual(self.view.outgoing(request), request)
        self.assertIsNone(self.view.incoming({'id': 20, 'result': {'thread': {
            'id': self.main, 'cwd': '/fixture/workspace'}}}))
        self.assertEqual(self.primary_state(), before)
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing({'id': 21, 'method': 'thread/resume', 'params': {'threadId': self.side}})

    def test_unknown_native_version_keeps_side_fork_unavailable_without_changing_launch(self):
        for version in (None, 'unparseable', 'codex/0.162.1', 'codex/0.164.0'):
            with self.subTest(version=version):
                view = self.connection('unknown-' + str(version), lambda: True)
                self.initialize(view, version)
                with self.assertRaises(LocalProtocolError):
                    view.outgoing(self.fork())
        self.assertTrue(self.protocol.bound)
        self.assertEqual(self.protocol.native_id, self.main)

    def test_secondary_primary_requests_do_not_bind_or_reconfigure(self):
        self.view.outgoing({'id': 12, 'method': 'turn/start', 'params': {'threadId': self.main}})
        other = self.connection('other', lambda: False)
        self.initialize(other)
        before = self.primary_state()
        request = {'id': 12, 'method': 'thread/read', 'params': {'threadId': self.main}}
        self.assertEqual(other.outgoing(request), request)
        self.assertIsNone(other.incoming({'id': 12, 'result': {'thread': {
            'id': self.main, 'cwd': '/fixture/workspace'}}}))
        self.assertEqual(self.primary_state(), before)
        with self.assertRaises(LocalProtocolError):
            other.outgoing({'id': 13, 'method': 'thread/resume', 'params': {'threadId': self.main}})
        self.assertEqual(self.primary_state(), before)

    def test_primary_reselection_cannot_replace_history_path_or_workspace(self):
        before = self.primary_state()
        for change in ({'path': '/other/rollout'}, {'history': []}, {'cwd': '/other'}):
            with self.subTest(change=change), self.assertRaises(LocalProtocolError):
                self.view.outgoing({'id': 20, 'method': 'thread/resume', 'params': {
                    'threadId': self.main, **change}})
        self.assertEqual(self.primary_state(), before)

    def test_real_primary_usage_is_accepted_and_equivalent_side_usage_is_ignored(self):
        self.open_side()
        def frame(native):
            return {'method': 'thread/tokenUsage/updated', 'params': {
                'threadId': native, 'tokenUsage': {'last': {'totalTokens': 12345}, 'modelContextWindow': 100000}}}
        value = frame(self.main)
        self.view.incoming(value)
        observed = self.view.usage(value)
        self.assertEqual(observed['native_session_id'], self.main)
        self.assertEqual(observed['context']['tokens'], 12345)
        value = frame(self.side)
        self.view.incoming(value)
        self.assertIsNone(self.view.usage(value))

    def test_native_error_retries_and_unsubscribe_confirmation_are_local(self):
        error = {'error': {'code': -32603, 'message': 'Native side error'}}
        self.view.outgoing(self.fork())
        self.assertIsNone(self.view.incoming({'id': 10, **error}))
        self.open_side()
        request = {'id': 20, 'method': 'thread/unsubscribe', 'params': {'threadId': self.side}}
        self.view.outgoing(request)
        self.assertIsNone(self.view.incoming({'id': 20, **error}))
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing(self.fork(21))
        self.view.outgoing({**request, 'id': 22})
        self.view.incoming({'id': 22, 'result': {'status': 'unsubscribed'}})
        self.assertEqual(self.view.outgoing(self.fork(23)), self.fork(23))
        self.assertIsNone(self.protocol.launch_refusal)

    def test_pending_bound_closed_view_and_host_reply_namespace(self):
        self.view.outgoing(self.fork())
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing(self.fork(11))
        # A native server request may use the same ID as a client request.
        reply = {'id': 10, 'result': {'content': 'host response'}}
        self.assertEqual(self.view.outgoing(reply), reply)
        self.view.incoming(self.forked())
        for identifier in range(100, 164):
            self.view.outgoing({'id': identifier, 'method': 'thread/read', 'params': {'threadId': self.side}})
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing({'id': 164, 'method': 'thread/read', 'params': {'threadId': self.side}})
        before = self.primary_state()
        self.view.closed()
        self.assertEqual(self.view.side_requests, {})
        self.assertIsNone(self.view.side_id)
        self.assertIsNone(self.view.incoming(self.forked()))
        self.assertEqual(self.primary_state(), before)

    def test_startup_binding_reply_ids_are_also_connection_scoped(self):
        root = LaunchProtocol({**self.settings, 'native_id': None})
        owner = root.for_connection('first', lambda: False)
        other = root.for_connection('second', lambda: False)
        self.initialize(owner)
        self.initialize(other)
        outgoing = owner.outgoing({'id': 5, 'method': 'thread/start', 'params': {}})
        self.assertEqual(outgoing['params']['developerInstructions'], 'primary bootstrap')
        reply = {'id': 5, 'result': {'thread': {'id': self.main, 'cwd': '/fixture/workspace'},
                                    'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never'}}
        self.assertIsNone(other.incoming(reply))
        self.assertFalse(root.bound)
        self.assertEqual(owner.incoming(reply), self.main)
        self.assertTrue(root.bound)
        self.assertEqual(root.native_id, self.main)

    def test_invalid_side_ids_are_dropped_without_reflection_or_primary_error(self):
        before = self.primary_state()
        for identifier in (None, True, [], {}):
            with self.subTest(identifier=identifier):
                with self.assertRaises(LocalProtocolError) as caught:
                    self.view.outgoing(self.fork(identifier))
                self.assertIsNone(caught.exception.request_id)
        for params in (None, [], 'bad'):
            with self.subTest(params=params), self.assertRaises(LocalProtocolError):
                self.view.outgoing({**self.fork(), 'params': params})
        self.open_side()
        with self.assertRaises(LocalProtocolError) as caught:
            self.view.outgoing({'id': False, 'method': 'turn/start', 'params': {'threadId': self.side}})
        self.assertIsNone(caught.exception.request_id)
        self.assertEqual(self.primary_state(), before)

    def test_saturated_side_reserves_bounded_native_cleanup_capacity(self):
        self.open_side()
        for identifier in range(100, 164):
            self.view.outgoing({'id': identifier, 'method': 'thread/read', 'params': {'threadId': self.side}})
        for identifier, method in ((200, 'turn/interrupt'), (201, 'turn/interrupt'), (202, 'thread/unsubscribe')):
            self.view.outgoing({'id': identifier, 'method': method, 'params': {'threadId': self.side}})
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing({'id': 203, 'method': 'turn/interrupt', 'params': {'threadId': self.side}})
        self.assertEqual(len(self.view.side_requests), 67)
        self.view.incoming({'id': 202, 'result': {'status': 'unsubscribed'}})
        self.assertIsNone(self.view.side_id)
        self.assertEqual(self.view.side_requests, {})

    def test_other_connection_cannot_consume_side_reply_or_publish_binding(self):
        other = self.connection('control', lambda: False)
        self.initialize(other)
        self.view.outgoing(self.fork(10))
        self.assertIsNone(other.incoming(self.forked(10)))
        self.assertIsNone(other.side_id)
        self.assertEqual(self.view.fork_request, 10)
        self.assertIsNone(self.view.incoming(self.forked(10)))
        self.assertEqual(self.view.side_id, self.side)
        self.assertTrue(self.protocol.bound)

    def test_primary_workspace_and_grant_validation_remain_ordinary_errors(self):
        for change in ({'cwd': '/other'}, {'sandbox': {'type': 'readOnly'}}):
            root = LaunchProtocol(self.settings)
            view = root.for_connection('initial', lambda: False)
            view.outgoing({'id': 5, 'method': 'thread/resume', 'params': {'threadId': self.main}})
            reply = {'thread': {'id': self.main, 'cwd': change.get('cwd', '/fixture/workspace')},
                     'sandbox': change.get('sandbox', {'type': 'dangerFullAccess'}), 'approvalPolicy': 'never'}
            with self.subTest(change=change), self.assertRaises(ValueError):
                view.incoming({'id': 5, 'result': reply})
            self.assertFalse(root.bound)
