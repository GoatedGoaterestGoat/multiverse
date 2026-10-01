"""Codex remote launch relay with a scripted offline app-server."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

from multiverse import agents, rpc
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.types import LaunchContext, NativeEndpoint, NativeResult, SessionHandle
from multiverse.store import read, write


class CodexLaunchTest(unittest.TestCase):
    def setUp(self):
        self.native_id = str(uuid4())
        self.values = {'name': 'worker', 'workspace': '/workspace', 'agent_dir': '/outside/state/agent',
                       'bootstrap_json': json.dumps('bootstrap'), 'task': '--literal prompt',
                       'session_id': self.native_id, 'control_socket': '/tmp/owned/rpc.sock'}
        self.context = LaunchContext('spawn', self.values, endpoint=NativeEndpoint('unix-websocket', '/tmp/native.sock'))

    def test_launch_moves_bootstrap_and_grants_to_relay_and_keeps_permissions(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True,
                    'spawn_args': ['--dangerously-bypass-approvals-and-sandbox', '--add-dir', '/custom']})
        command = adapter.spawn(self.context)
        self.assertNotIn('--add-dir', command.argv)
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', command.argv)
        self.assertEqual(command.argv[-1], self.values['task'])
        self.assertEqual(command.rpc_proxy.settings['bootstrap'], 'bootstrap')
        self.assertEqual(command.rpc_proxy.settings['name'], 'worker')
        self.assertEqual(command.rpc_proxy.settings['roots'], ['/outside/state/agent', '/custom'])
        self.assertEqual(command.control_endpoint.address, self.values['control_socket'])
        self.assertEqual(command.rpc_proxy.upstream_path, '/tmp/native.sock')
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
        resumed = adapter.resume(session, self.context)
        self.assertNotIn('--add-dir', resumed.argv)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', resumed.argv)
        self.assertEqual(resumed.rpc_proxy.settings['native_id'], self.native_id)
        self.assertEqual(resumed.rpc_proxy.settings['name'], 'worker')
        self.assertEqual(resumed.rpc_proxy.settings['roots'], [])

    def test_native_environment_exclusions_and_roots_remain(self):
        executor = Mock()
        executor.rpc.return_value = NativeResult('ok', value={'exclude': ['NATIVE_SECRET'], 'writable_roots': ['/server-root']})
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        command = adapter.spawn(replace(self.context, executor=executor, environment={'MV2_AGENT_ID': 'agent'}))
        self.assertIn('/server-root', command.rpc_proxy.settings['roots'])
        self.assertIn('NATIVE_SECRET', ' '.join(command.argv))
        self.assertIn('shell_environment_policy.set.MV2_AGENT_ID="agent"', command.argv)
        self.assertEqual(executor.rpc.call_count, 1)

    def test_plain_launch_unchanged(self):
        command = CodexAdapter.configured('codex', {}).spawn(self.context)
        self.assertIn('--add-dir', command.argv)
        self.assertIsNone(command.rpc_proxy)

    def test_wrong_thread_and_workspace_are_not_bound(self):
        protocol = LaunchProtocol({'bootstrap': 'b', 'roots': [], 'workspace': '/workspace', 'native_id': self.native_id})
        with self.assertRaisesRegex(ValueError, 'does not match'):
            protocol.outgoing({'id': 1, 'method': 'thread/resume', 'params': {'threadId': str(uuid4())}})
        protocol.outgoing({'id': 1, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
        with self.assertRaisesRegex(ValueError, 'different thread ID'):
            protocol.incoming({'id': 1, 'result': {'thread': {'id': str(uuid4()), 'cwd': '/workspace'}}})
        protocol.outgoing({'id': 2, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
        with self.assertRaisesRegex(ValueError, 'workspace'):
            protocol.incoming({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': '/other'}}})


class LaunchRelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mv2-relay-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.directory = self.root / 'state/agents/agent'
        self.directory.mkdir(parents=True)
        self.properties = {'id': 'agent', 'name': 'agent', 'harness': 'codex', 'workspace': str(self.root)}
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', {'launch_id': 'launch', 'state': 'running'})
        self.native_id = str(uuid4())
        self.spec = {'socket_path': str(self.root / 'relay.sock'), 'upstream_path': str(self.root / 'native.sock')}
        self.settings = {'name': 'relay-agent', 'bootstrap': 'MV2 bootstrap', 'roots': ['/mv2-state'],
                         'workspace': str(self.root), 'native_id': None}
        self.received = asyncio.Queue()
        self.naming = []
        self.name_reply = {'result': {}}
        self.errors = []
        self.connected = asyncio.Event()
        async def server(connection):
            if not self.connected.is_set():
                self.upstream = connection
                self.connected.set()
            try:
                async for payload in connection:
                    value = json.loads(payload) if isinstance(payload, str) else {}
                    if value.get('method') in ('initialize', 'initialized', 'thread/name/set'):
                        self.naming.append(value)
                        if value['method'] == 'initialize':
                            await connection.send(json.dumps({'id': value['id'], 'result': {}}))
                        elif value['method'] == 'thread/name/set' and self.name_reply is not None:
                            await connection.send(json.dumps({'id': value['id'], **self.name_reply}))
                        continue
                    await self.received.put(payload)
            except Exception:
                pass
        self.server = await unix_serve(server, self.spec['upstream_path'], max_size=64 * 1024 * 1024)
        self.proxy = None
        self.client = None

    async def asyncTearDown(self):
        if self.client:
            await self.client.close()
        if self.proxy:
            await asyncio.to_thread(self.proxy.close)
        self.server.close()
        await self.server.wait_closed()
        self.temp.cleanup()

    async def connect(self):
        self.protocol = LaunchProtocol(self.settings)
        self.proxy = rpc.LaunchProxy(self.spec, self.protocol,
            lambda native_id: agents.bind_session(self.root, 'agent', native_id, 'launch'), self.errors.append)
        await asyncio.to_thread(self.proxy.start)
        self.client = await unix_connect(self.spec['socket_path'], max_size=64 * 1024 * 1024)
        await asyncio.wait_for(self.connected.wait(), 2)

    async def receive(self):
        return await asyncio.wait_for(self.received.get(), 2)

    async def test_start_merges_instructions_and_roots_and_binds_exact_reply(self):
        await self.connect()
        await self.client.send(json.dumps({'id': 2, 'method': 'thread/start', 'params': {
            'developerInstructions': 'User configured instructions',
            'config': {'sandbox_workspace_write': {'writable_roots': ['/user-root'], 'network_access': True}}}}))
        request = json.loads(await self.receive())
        self.assertEqual(request['params']['developerInstructions'], 'MV2 bootstrap\n\nUser configured instructions')
        policy = request['params']['config']['sandbox_workspace_write']
        self.assertEqual(policy['writable_roots'], ['/mv2-state', '/user-root'])
        self.assertTrue(policy['network_access'])
        response = json.dumps({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})
        await self.upstream.send(response)
        self.assertEqual(await self.client.recv(), response)
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.native_id)
        self.assertEqual([v['method'] for v in self.naming], ['initialize', 'initialized', 'thread/name/set'])
        self.assertEqual(self.naming[-1]['params'], {'threadId': self.native_id, 'name': 'relay-agent'})
        self.assertEqual(self.errors, [])

    async def test_resume_injects_bootstrap_but_preserves_saved_grants(self):
        self.settings['native_id'] = self.native_id
        await self.connect()
        await self.client.send(json.dumps({'id': 3, 'method': 'thread/resume', 'params': {
            'threadId': self.native_id, 'config': {'model_reasoning_effort': 'high'}}}))
        request = json.loads(await self.receive())
        self.assertEqual(request['params']['developerInstructions'], 'MV2 bootstrap')
        self.assertEqual(request['params']['config'], {'model_reasoning_effort': 'high'})
        response = json.dumps({'id': 3, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})
        await self.upstream.send(response)
        self.assertEqual(await self.client.recv(), response)
        self.assertEqual(self.naming[-1]['params'], {'threadId': self.native_id, 'name': 'relay-agent'})

    async def test_naming_errors_leave_terminal_usable_with_diagnostic(self):
        self.name_reply = {'error': {'code': -32601, 'message': 'Naming unavailable'}}
        await self.connect()
        await self.client.send('{"id":2,"method":"thread/start","params":{}}')
        await self.receive()
        response = json.dumps({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})
        await self.upstream.send(response)
        self.assertEqual(await self.client.recv(), response)
        self.assertIn('Naming unavailable', self.errors[-1])
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.native_id)
        await self.client.send('{"id":4,"method":"future/method"}')
        self.assertEqual(await self.receive(), '{"id":4,"method":"future/method"}')

    async def test_naming_timeout_is_bounded_and_does_not_fail_launch(self):
        self.name_reply = None
        with patch.object(rpc.LaunchProxy, 'POST_BIND_TIMEOUT', .1):
            await self.connect()
            await self.client.send('{"id":2,"method":"thread/start","params":{}}')
            await self.receive()
            response = json.dumps({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})
            await self.upstream.send(response)
            self.assertEqual(await asyncio.wait_for(self.client.recv(), 2), response)
        self.assertIn('timed out', self.errors[-1])

    async def test_old_saved_launch_without_name_still_binds(self):
        del self.settings['name']
        await self.connect()
        await self.client.send('{"id":2,"method":"thread/start","params":{}}')
        await self.receive()
        response = json.dumps({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})
        await self.upstream.send(response)
        self.assertEqual(await self.client.recv(), response)
        self.assertEqual(self.naming, [])

    async def test_failed_launch_reply_does_not_name_a_thread(self):
        await self.connect()
        await self.client.send('{"id":2,"method":"thread/start","params":{}}')
        await self.receive()
        response = '{"id":2,"error":{"code":-32000,"message":"Launch failed"}}'
        await self.upstream.send(response)
        self.assertEqual(await self.client.recv(), response)
        self.assertEqual(self.naming, [])

    async def test_unknown_frames_binary_and_approvals_pass_through_unchanged(self):
        await self.connect()
        for payload in ['{ "id": 1, "method": "future/method", "params": {} }', b'opaque binary',
                        json.dumps({'method': 'future/notification', 'params': {'text': 'x' * 250000}})]:
            await self.client.send(payload)
            self.assertEqual(await self.receive(), payload)
            await self.upstream.send(payload)
            self.assertEqual(await self.client.recv(), payload)
        approval = '{"id":99,"method":"item/commandExecution/requestApproval","params":{}}'
        await self.upstream.send(approval)
        self.assertEqual(await self.client.recv(), approval)
        self.assertTrue(self.received.empty(), 'Relay must not answer native approval requests')
        reply = '{"id":99,"result":{"decision":"decline"}}'
        await self.client.send(reply)
        self.assertEqual(await self.receive(), reply)

    async def test_large_resume_history_and_following_frames_keep_connection_open(self):
        self.settings['native_id'] = self.native_id
        await self.connect()
        await self.client.send(json.dumps({'id': 3, 'method': 'thread/resume',
                                          'params': {'threadId': self.native_id}}))
        await self.receive()
        # A real resumed session returned 12,405,815 bytes and repeatedly hit
        # the old 1 MiB limit, making the native TUI reconnect indefinitely.
        response = json.dumps({'id': 3, 'result': {'thread': {
            'id': self.native_id, 'cwd': str(self.root), 'turns': [
                {'items': [{'type': 'commandExecution', 'aggregatedOutput': 'x' * (13 * 1024 * 1024)}]}]}}})
        await self.upstream.send(response)
        self.assertEqual(await asyncio.wait_for(self.client.recv(), 5), response)
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.native_id)
        # Large client messages (e.g. images) must also survive the relay.
        payload = json.dumps({'method': 'future/method', 'params': {'text': 'x' * (2 * 1024 * 1024)}})
        await self.client.send(payload)
        self.assertEqual(await self.receive(), payload)
        await self.upstream.send('{"method":"thread/status/changed","params":{}}')
        self.assertEqual(await self.client.recv(), '{"method":"thread/status/changed","params":{}}')
        self.assertEqual(self.errors, [])

    async def test_relay_still_bounds_oversized_native_messages(self):
        from unittest.mock import patch
        with patch.object(rpc.LaunchProxy, 'MAX_MESSAGE_SIZE', 1024):
            await self.connect()
            await self.upstream.send(json.dumps({'method': 'future/event', 'text': 'x' * 2048}))
            await asyncio.wait_for(self.client.wait_closed(), 2)
            await asyncio.to_thread(self.proxy.close)
            self.assertTrue(self.errors)
            self.assertEqual(self.upstream.close_code, 1009)

    async def test_malformed_launch_fails_visibly_without_forwarding(self):
        await self.connect()
        await self.client.send('{"id":7,"method":"thread/start","params":null}')
        response = json.loads(await self.client.recv())
        self.assertEqual(response['id'], 7)
        self.assertEqual(response['error']['code'], -32602)
        self.assertTrue(self.errors)
        self.assertTrue(self.received.empty())
        self.assertNotIn('native_session_id', read(self.directory / 'properties.json'))

    async def test_stale_launch_cannot_bind_reply(self):
        await self.connect()
        await self.client.send('{"id":2,"method":"thread/start","params":{}}')
        await self.receive()
        write(self.directory / 'runtime.json', {'launch_id': 'replacement'})
        await self.upstream.send(json.dumps({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}}))
        await asyncio.wait_for(self.client.wait_closed(), 2)
        self.assertNotIn('native_session_id', read(self.directory / 'properties.json'))
        self.assertTrue(self.errors)
        self.assertEqual(self.naming, [])

    async def test_daemon_disconnect_closes_client_with_same_code(self):
        await self.connect()
        await self.upstream.close(code=1001, reason='daemon shutdown')
        await asyncio.wait_for(self.client.wait_closed(), 2)
        self.assertEqual(self.client.close_code, 1001)

    async def test_terminal_disconnect_closes_upstream(self):
        await self.connect()
        await self.client.close(code=1000, reason='terminal exit')
        await asyncio.wait_for(self.upstream.wait_closed(), 2)
        self.assertEqual(self.upstream.close_reason, 'terminal exit')
        await asyncio.to_thread(self.proxy.close)
        self.assertFalse(self.proxy.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
