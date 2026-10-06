"""Transport failures remain bounded and never approve unsolicited requests."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from websockets.sync.server import unix_serve

from multiverse.harnesses.types import NativeResult, RpcRequest, RpcSpec, RpcNotification
from multiverse.runtime.rpc import execute
from multiverse.runtime import native
from multiverse.harnesses.codex.protocol import check_connection
from multiverse.harnesses.types import SessionHandle, NativeEndpoint
from multiverse.harnesses.codex import CodexAdapter
from unittest.mock import patch


class RpcTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mv2-rpc-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'socket')

    def serve(self, handler):
        server = unix_serve(handler, path=self.path, close_timeout=.1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)

    @staticmethod
    def plan():
        response = yield RpcRequest('initialize', {'clientInfo': {'name': 'test', 'version': '1'}},
                                    on_success=(RpcNotification('initialized', {}),))
        return NativeResult('ok', value=response['result'])

    def test_response_id_mismatch_is_not_accepted(self):
        def handler(connection):
            request = json.loads(connection.recv())
            connection.send(json.dumps({'id': request['id'] + 1, 'result': {}}))
        self.serve(handler)
        with self.assertRaisesRegex(ValueError, 'ID mismatch'):
            execute(RpcSpec(self.path, self.plan))

    def test_timeout_does_not_hang_and_acceptance_stays_unknown(self):
        def handler(connection):
            connection.recv()
            time.sleep(.3)
        self.serve(handler)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'acceptance may be unknown'):
            execute(RpcSpec(self.path, self.plan), timeout=.1)
        self.assertLess(time.monotonic() - started, 1)

    def test_malformed_thread_is_a_failed_capability_not_an_exception(self):
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

    def test_native_permission_request_is_unanswered_even_from_async_caller(self):
        observed = []
        def handler(connection):
            request = json.loads(connection.recv())
            connection.send(json.dumps({'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {}}))
            connection.send(json.dumps({'id': request['id'], 'result': {'owned': True}}))
            observed.append(json.loads(connection.recv()))
        self.serve(handler)
        async def caller():
            return execute(RpcSpec(self.path, self.plan))
        result = asyncio.run(caller())
        self.assertEqual(result.value, {'owned': True})
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['method'], 'initialized')


if __name__ == '__main__':
    unittest.main()
