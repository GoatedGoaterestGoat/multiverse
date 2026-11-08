"""Transport failures remain bounded and never approve unsolicited requests."""
import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from websockets.sync.server import unix_serve

from multiverse.harnesses.types import NativeResult, RpcRequest, RpcSpec, RpcNotification
from multiverse.runtime.rpc import execute, watch
from multiverse.runtime import native
from multiverse.harnesses.codex.protocol import check_connection
from multiverse.harnesses.types import SessionHandle, NativeEndpoint
from multiverse.harnesses.codex import CodexAdapter
from unittest.mock import patch
from uuid import uuid4
from websockets.exceptions import ConnectionClosed


class RpcTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mv2-rpc-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'socket')

    def serve(self, handler, **options):
        server = unix_serve(handler, path=self.path, close_timeout=.1, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)

    @staticmethod
    def plan():
        response = yield RpcRequest('initialize', {'clientInfo': {'name': 'test', 'version': '1'}},
                                    on_success=(RpcNotification('initialized', {}),))
        return NativeResult('ok', value=response['result'])

    def test_uncorrelated_or_malformed_reply_is_never_acceptance(self):
        """An uncorrelated or malformed reply is an error, never acceptance; a break marks a message delivered when
        someone else's reply arrived."""
        with self.subTest('reply to another request id'):
            def handler(connection):
                request = json.loads(connection.recv())
                connection.send(json.dumps({'id': request['id'] + 1, 'result': {}}))
            self.serve(handler)
            with self.assertRaisesRegex(ValueError, 'ID mismatch'):
                execute(RpcSpec(self.path, self.plan))
        self.path = str(Path(self.temp.name) / 'malformed-socket')
        with self.subTest('a thread that is not an object'):
            def handler(connection):
                request = json.loads(connection.recv())
                connection.send(json.dumps({'id': request['id'], 'result': {}}))
                connection.recv()  # initialized notification
                request = json.loads(connection.recv())
                connection.send(json.dumps({'id': request['id'], 'result': {'thread': None}}))
            self.serve(handler)
            session = SessionHandle('agent', 'codex', self.temp.name, native_id='exact', state='running',
                                    endpoint=NativeEndpoint('unix-websocket', self.path))
            adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
            executor = native.NativeExecutor(self.temp.name)
            with patch.object(executor, 'check_session'):
                observed = adapter.capabilities(session, executor)['send_steer']
            self.assertEqual(observed.state, 'error')
            self.assertIn('thread must be an object', observed.reason)

    def test_timeout_does_not_hang_and_acceptance_stays_unknown(self):
        """An RPC timeout is bounded and leaves acceptance unknown; fragile because this classification is what stops a
        retry from double-sending, and a hang inside an agent's tool call is silent."""
        def handler(connection):
            connection.recv()
            time.sleep(.3)
        self.serve(handler)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'acceptance may be unknown'):
            execute(RpcSpec(self.path, self.plan), timeout=.1)
        self.assertLess(time.monotonic() - started, 1)

    def test_native_permission_request_is_unanswered_even_from_async_caller(self):
        """A server-initiated approval request is never answered by mv2, even from an async caller; a break goes
        unnoticed because an auto-reply silently approves a command."""
        observed, received = [], threading.Event()
        def handler(connection):
            request = json.loads(connection.recv())
            connection.send(json.dumps({'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {}}))
            connection.send(json.dumps({'id': request['id'], 'result': {'owned': True}}))
            observed.append(json.loads(connection.recv()))
            received.set()
        self.serve(handler)
        async def caller():
            return execute(RpcSpec(self.path, self.plan))
        result = asyncio.run(caller())
        self.assertEqual(result.value, {'owned': True})
        self.assertTrue(received.wait(10), 'the server never got the next message')  # its thread records it
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['method'], 'initialized')

    def test_response_budget_is_bounded_and_does_not_expand_requests(self):
        """A larger history budget must not silently uncap replies, notifications or outgoing input."""
        for label, limit, size, streaming, notification in (
            ('default', None, 2 << 20, False, False),
            ('explicit', 1024, 2048, False, False),
            ('stream-default', None, 2 << 20, True, False),
            ('stream-explicit', 1024, 2048, True, False),
            ('notification', 1024, 2048, False, True),
            ('stream-notification', 1024, 2048, True, True),
        ):
            with self.subTest(case=label):
                self.path = str(Path(self.temp.name) / label)
                def handler(connection, size=size, notification=notification):
                    try:
                        request = json.loads(connection.recv())
                        if notification:
                            connection.send(json.dumps({'method': 'event', 'params': {'body': 'x' * size}}))
                        connection.send(json.dumps({'id': request['id'], 'result': {'body': 'x' * size}}))
                        connection.recv(timeout=2)  # initialized, or the bounded rejection closes it
                    except ConnectionClosed:
                        pass
                self.serve(handler)
                spec = RpcSpec(self.path, self.plan, **({} if limit is None else {'max_response_bytes': limit}))
                if streaming:
                    published = []
                    asyncio.run(watch(spec, lambda value: value, published.append))
                    self.assertEqual(published, ['unknown'])
                else:
                    with self.assertRaisesRegex(RuntimeError, 'message too big.*acceptance may be unknown'):
                        execute(spec)

        for invalid in (None, True, False, 0, -1, 1.5, '1024', (64 << 20) + 1):
            with self.subTest(invalid=invalid), patch('multiverse.runtime.rpc.unix_connect',
                    side_effect=AssertionError('Invalid response budget reached the socket')) as connect:
                spec = RpcSpec(self.path, self.plan, max_response_bytes=invalid)
                with self.assertRaisesRegex(ValueError, 'response budget'):
                    execute(spec)
                published = []
                asyncio.run(watch(spec, lambda value: value, published.append))
                self.assertEqual(published, ['unknown'])
                connect.assert_not_called()

        self.path = str(Path(self.temp.name) / 'request-limit')
        received = []
        def handler(connection):
            try:
                request = json.loads(connection.recv())
                received.append(request['method'])
                connection.send(json.dumps({'id': request['id'], 'result': {}}))
            except ConnectionClosed:
                pass
        self.serve(handler, max_size=4 << 20)
        def large_request():
            yield RpcRequest('turn/start', {'input': 'x' * (2 << 20)})
            return NativeResult('ok')
        with self.assertRaisesRegex(ValueError, 'request exceeds 1 MiB'):
            execute(RpcSpec(self.path, large_request, max_response_bytes=64 << 20))
        self.assertEqual(received, [])

    def test_large_receipt_replies_confirm_without_resending_or_loading_a_thread(self):
        """Large native history must not turn an accepted message into an uncheckable delivery or invite a duplicate."""
        from test_message_delivery import DeliveryFixture
        from multiverse.store import read, lock
        from multiverse.tools import deliver
        from multiverse.universe.mailbox import append_message, message_record, update_message

        rows = (
            ('small-history', 0, 'history', True, 'active', 'cli'),
            ('large-history', 2 << 20, 'history', True, 'active', 'cli'),
            ('large-queue', 2 << 20, 'queue', True, 'active', 'cli'),
            ('unloaded-history', 2 << 20, 'history', True, 'notLoaded', 'direct'),
            ('unmatched-history', 2 << 20, 'history', False, 'active', 'direct'),
            ('reconciliation', 2 << 20, 'history', True, 'active', 'reconcile'),
        )
        for label, size, location, matches, state, route in rows:
            with self.subTest(case=label), contextlib.ExitStack() as stack:
                fixture = DeliveryFixture()
                stack.callback(fixture.doCleanups)
                fixture.setUp()
                fixture.recipient(harness='codex')
                message = append_message(fixture.directory, 'receipt probe', {}, delivery='follow-up',
                                         target_session=fixture.native_id)
                token = str(uuid4())
                attempt = {'state': 'native_queued', 'launch_id': fixture.launch,
                           'native_session_id': fixture.native_id, 'native_message_id': token}
                update_message(fixture.directory, message['id'], submission_state='accepted',
                    delivery_method='native', target_session=fixture.native_id, attempts=[attempt],
                    native_receipt={'queued_submission_id': 'original-queue', 'transcript_confirmed': False})
                self.path = str(Path(self.temp.name) / label)
                session = SessionHandle(fixture.agent, 'codex', str(fixture.work), native_id=fixture.native_id,
                    launch_id=fixture.launch, state='running', endpoint=NativeEndpoint('unix-websocket', self.path))
                adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
                executor = native.NativeExecutor(str(fixture.work), read(fixture.directory / 'properties.json'),
                                                  read(fixture.directory / 'runtime.json'))
                stack.enter_context(patch.object(executor, 'check_session'))
                stack.enter_context(patch.object(native, 'context', return_value=(fixture.directory, adapter, session, executor)))
                methods = []
                queued = ([{'id': 'original-queue', 'clientUserMessageId': token,
                            'input': [{'type': 'text', 'text': 'x' * size}]}] if location == 'queue' else [])
                items = [{'id': 'command', 'type': 'commandExecution', 'status': 'completed',
                          'aggregatedOutput': 'x' * size},
                         {'id': 'user', 'type': 'userMessage', 'clientId': token if matches else 'other', 'content': []}]
                replies = {'initialize': {'userAgent': 'codex/0.166.0'},
                    'thread/read': {'thread': {'id': fixture.native_id, 'cwd': str(fixture.work),
                        'status': {'type': state}, 'canAcceptDirectInput': state != 'notLoaded'}},
                    'thread/queue/list': {'data': queued, 'nextCursor': None},
                    'thread/turns/list': {'data': [{'id': 'turn', 'items': items}], 'nextCursor': None}}

                def handler(connection, replies=replies, methods=methods):
                    try:
                        for wire in connection:
                            request = json.loads(wire)
                            if 'id' not in request:
                                continue
                            methods.append(request['method'])
                            connection.send(json.dumps({'id': request['id'], 'result': replies[request['method']]}))
                    except ConnectionClosed:
                        pass
                self.serve(handler)
                if route == 'cli':
                    observed = deliver.run(fixture.root, agent=fixture.agent, check=True)
                    self.assertEqual(len(observed['messages']), 1)
                    receipt = observed['messages'][0]
                elif route == 'direct':
                    receipt = native.deliver(fixture.root, fixture.agent, message['id'], check=True)
                else:
                    with lock(fixture.directory / '.launch.lock'), lock(fixture.directory / '.delivery.lock'):
                        _, tracking = message_record(fixture.directory, message['id'])
                        receipt = native._check_delivery_receipt(fixture.directory, adapter, session, executor,
                                                                message, tracking)
                self.assertEqual(receipt['state'], 'native_queued' if matches else 'unknown', receipt.get('reason'))
                if matches:
                    value = receipt if route == 'cli' else receipt['value']
                    self.assertIs(value['transcript_confirmed'], location != 'queue')
                tracking = message_record(fixture.directory, message['id'])[1]
                self.assertEqual(tracking['submission_state'], 'accepted')
                self.assertEqual(len(tracking['attempts']), 1)
                self.assertLessEqual(set(methods), {'initialize', 'thread/read', 'thread/queue/list', 'thread/turns/list'})
                self.assertNotIn('thread/resume', methods)
                self.assertNotIn('turn/start', methods)
                self.assertNotIn('thread/queue/add', methods)


if __name__ == '__main__':
    unittest.main()
