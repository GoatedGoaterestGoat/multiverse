"""Codex remote launch relay with a scripted offline app-server."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

from multiverse.universe import agents
from multiverse.runtime import rpc
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

    def test_update_check_default_covers_spawn_resume_wake_and_plain_fallback(self):
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
        for settings in ({}, {'inbox_bridge': True}, {'inbox_bridge': False}):
            adapter = CodexAdapter.configured('codex', settings)
            for mode in ('spawn', 'empty', 'resume', 'wake'):
                with self.subTest(settings=settings, mode=mode):
                    context = replace(self.context, values={**self.values, 'task': ''}) if mode == 'empty' else self.context
                    if mode == 'wake':
                        context = replace(context, resume_prompt='--config check_for_update_on_startup=true')
                    command = adapter.spawn(context) if mode in ('spawn', 'empty') else adapter.resume(session, context)
                    for candidate in (command, command.fallback):
                        if candidate is None:
                            continue
                        end = candidate.argv.index('--') if '--' in candidate.argv else len(candidate.argv)
                        self.assertEqual(candidate.argv[:end].count('check_for_update_on_startup=false'), 1)
                        position = candidate.argv.index('check_for_update_on_startup=false')
                        self.assertEqual(candidate.argv[position - 1], '-c')

    def test_explicit_update_check_value_is_preserved_for_each_argument_source(self):
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
        for value in ('true', 'false'):
            for flags in (['-c', f'check_for_update_on_startup={value}'],
                          ['--config', f'check_for_update_on_startup = {value}'],
                          [f'--config=check_for_update_on_startup={value}'],
                          [f'-ccheck_for_update_on_startup={value}'],
                          [f'-c=check_for_update_on_startup={value}']):
                for source in ('args', 'spawn_args', 'resume_args'):
                    for bridge in (False, True):
                        settings = {'inbox_bridge': bridge, source: flags}
                        adapter = CodexAdapter.configured('codex', settings)
                        for mode in ('spawn', 'resume'):
                            with self.subTest(value=value, flags=flags, source=source, bridge=bridge, mode=mode):
                                command = adapter.spawn(self.context) if mode == 'spawn' else adapter.resume(session, self.context)
                                before_prompt = command.argv[:command.argv.index('--')]
                                observed = [arg for arg in before_prompt if 'check_for_update_on_startup' in arg]
                                expected = value if source in ('args', mode + '_args') else 'false'
                                self.assertEqual(len(observed), 1, before_prompt)
                                self.assertEqual(observed[0].rsplit('=', 1)[1].strip(), expected)
                        self.assertEqual(settings[source], flags)

    def test_unrelated_config_text_does_not_suppress_update_check_default(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True,
            'args': ['-c', 'developer_instructions="check_for_update_on_startup=true"']})
        command = adapter.spawn(self.context)
        self.assertIn('check_for_update_on_startup=false', command.argv)

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
        self.assertEqual(command.shared_service.cwd, str(Path.home().resolve()))
        self.assertEqual(command.rpc_proxy.settings['workspace'], self.values['workspace'])
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
        command = adapter.spawn(replace(self.context, executor=executor, environment={'MV2_AGENT_ID': 'agent'},
                                        agent_environment={'MV2_AGENT_ID': 'agent'}))
        self.assertIn('/server-root', command.rpc_proxy.settings['roots'])
        self.assertIn('NATIVE_SECRET', ' '.join(command.argv))
        self.assertEqual(command.rpc_proxy.settings['agent_environment']['MV2_AGENT_ID'], 'agent')
        self.assertFalse(any('shell_environment_policy.set.MV2_' in arg for arg in command.argv))
        self.assertEqual(executor.rpc.call_count, 1)

    def test_deleted_daemon_cwd_diagnostic_keeps_strict_and_auto_contracts(self):
        executor = Mock()
        executor.rpc.return_value = NativeResult('error',
            'Native RPC -32603: failed to resolve feature override precedence: No such file or directory (os error 2)')
        context = replace(self.context, executor=executor, environment={'CODEX_HOME': '/native storage'})
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
        strict = CodexAdapter.configured('codex', {'inbox_bridge': True})
        with self.assertRaisesRegex(ValueError, 'deleted working directory') as raised:
            strict.resume(session, context)
        self.assertIn('daemon restart', str(raised.exception))
        self.assertIn("CODEX_HOME='/native storage'", str(raised.exception))
        self.assertIn('when other Codex work is idle', str(raised.exception))
        auto = CodexAdapter.configured('codex', {})
        command = auto.resume(session, context)
        self.assertEqual(command.native_mode['mode'], 'plain')
        self.assertIn('deleted working directory', command.native_mode['fallback_reason'])
        self.assertIn(self.native_id, command.argv)
        self.assertEqual(executor.rpc.call_count, 2, 'Diagnosis must not mutate or restart the daemon')

    def test_resume_translates_only_explicit_permission_flags(self):
        cases = [
            (['--dangerously-bypass-approvals-and-sandbox'], {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}),
            (['--sandbox', 'read-only', '--ask-for-approval', 'on-request'], {'sandbox': 'read-only', 'approvalPolicy': 'on-request'}),
            (['--sandbox=workspace-write', '--ask-for-approval=never'], {'sandbox': 'workspace-write', 'approvalPolicy': 'never'}),
            (['-s', 'danger-full-access', '-a', 'never'], {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}),
            (['-sread-only', '-a=on-request'], {'sandbox': 'read-only', 'approvalPolicy': 'on-request'}),
            (['-sworkspace-write'], {'sandbox': 'workspace-write'}),
            (['-anever'], {'approvalPolicy': 'never'}),
            (['--dangerously-bypass-approvals-and-sandbox', '-sread-only', '-aon-request'],
             {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}),
            ([], {}),
        ]
        for preference in ('args', 'resume_args'):
            for flags, expected in cases:
                with self.subTest(preference=preference, flags=flags):
                    settings = {'inbox_bridge': True, preference: flags}
                    adapter = CodexAdapter.configured('codex', settings)
                    session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
                    command = adapter.resume(session, self.context)
                    self.assertEqual(command.rpc_proxy.settings['resume_permissions'], expected)
                    self.assertEqual(command.argv[-1], self.native_id)
                    for flag in flags:
                        self.assertNotIn(flag, command.argv)
                    self.assertEqual(settings[preference], flags)
        # No resume grant is inferred from a spawn-only preference.
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True,
                                          'spawn_args': ['--dangerously-bypass-approvals-and-sandbox']})
        self.assertEqual(adapter.resume(session, self.context).rpc_proxy.settings['resume_permissions'], {})

    def test_plain_permission_arguments_are_unchanged(self):
        flags = ['--dangerously-bypass-approvals-and-sandbox']
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False, 'args': flags})
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
        for command in (adapter.spawn(self.context), adapter.resume(session, self.context)):
            self.assertIn(flags[0], command.argv)
            self.assertIsNone(command.rpc_proxy)

    def test_malformed_resume_permission_flags_fail_before_native_calls(self):
        for flags in (['--sandbox'], ['--sandbox=wrong'], ['-s'], ['-sread-only', '--sandbox=workspace-write'],
                      ['-a'], ['--ask-for-approval=wrong'], ['-anever', '-aon-request'],
                      ['--dangerously-bypass-approvals-and-sandbox=true']):
            with self.subTest(flags=flags):
                adapter = CodexAdapter.configured('codex', {'inbox_bridge': True, 'resume_args': flags})
                executor = Mock()
                session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
                with self.assertRaises(ValueError):
                    adapter.resume(session, replace(self.context, executor=executor))
                executor.rpc.assert_not_called()

    def test_send_waits_for_verified_launch_including_queued_followups(self):
        from multiverse.harnesses.types import Message
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id, state='starting',
                                endpoint=self.context.endpoint)
        for mode in ('steer', 'follow-up'):
            executor = Mock()
            result = adapter.send(session, Message('id', 'work', delivery=mode), executor)
            self.assertEqual(result.state, 'unavailable')
            self.assertIn('still binding', result.reason)
            executor.rpc.assert_not_called()
        self.assertEqual(adapter.capabilities(session)['send_steer'].state, 'unavailable')

    def test_plain_default_omits_generated_root_and_reports_it(self):
        command = CodexAdapter.configured('codex', {'inbox_bridge': False}).spawn(self.context)
        self.assertNotIn('--add-dir', command.argv)
        self.assertIn('omitted its extra writable agent folder', command.notices[0])
        self.assertIn(self.values['agent_dir'], command.notices[0])
        self.assertEqual(command.argv[-2:], ('--', self.values['task']))
        self.assertIsNone(command.rpc_proxy)

    def test_wake_waits_for_exact_tui_starting_turn_reply(self):
        protocol = LaunchProtocol({'bootstrap': 'b', 'roots': [], 'workspace': '/workspace', 'native_id': self.native_id})
        protocol.outgoing({'id': 1, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
        protocol.incoming({'id': 1, 'result': {'thread': {'id': self.native_id, 'cwd': '/workspace'}}})
        self.assertFalse(protocol.prompt_submitted)
        protocol.outgoing({'id': 2, 'method': 'turn/start', 'params': {'threadId': str(uuid4())}})
        protocol.incoming({'id': 2, 'result': {'turn': {}}})
        self.assertFalse(protocol.prompt_submitted)
        protocol.outgoing({'id': 3, 'method': 'turn/start', 'params': {'threadId': self.native_id}})
        protocol.incoming({'id': 3, 'error': {'code': -1, 'message': 'Prompt failed'}})
        self.assertFalse(protocol.prompt_submitted)
        self.assertIn('Prompt failed', protocol.prompt_error)
        protocol.outgoing({'id': 4, 'method': 'turn/start', 'params': {'threadId': self.native_id}})
        protocol.incoming({'id': 4, 'result': {'turn': {'id': 'native-turn'}}})
        self.assertTrue(protocol.prompt_submitted)

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

    def test_resume_rejects_missing_or_mismatched_effective_grant_fields(self):
        for policy in ({}, {'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'on-request'},
                       {'sandbox': None, 'approvalPolicy': 'never'}):
            with self.subTest(policy=policy):
                protocol = LaunchProtocol({'bootstrap': 'b', 'roots': [], 'workspace': '/workspace',
                                           'native_id': self.native_id,
                                           'resume_permissions': {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}})
                protocol.outgoing({'id': 1, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
                with self.assertRaisesRegex(ValueError, 'did not apply requested'):
                    protocol.incoming({'id': 1, 'result': {'thread': {'id': self.native_id, 'cwd': '/workspace'}, **policy}})
                self.assertFalse(protocol.bound)


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
        self.losses = []
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

    async def connect(self, expect_upstream=True, **callbacks):
        self.protocol = LaunchProtocol(self.settings)
        self.proxy = rpc.LaunchProxy(self.spec, self.protocol,
            lambda native_id: agents.bind_session(self.root, 'agent', native_id, 'launch'), self.errors.append,
            record_lost=self.losses.append, **callbacks)
        await asyncio.to_thread(self.proxy.start)
        self.client = await unix_connect(self.spec['socket_path'], max_size=64 * 1024 * 1024)
        if expect_upstream:
            await asyncio.wait_for(self.connected.wait(), 2)

    async def bind_primary(self):
        await self.connect()
        await self.client.send('{"id":2,"method":"thread/start","params":{}}')
        await self.receive()
        response = json.dumps({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})
        await self.upstream.send(response)
        self.assertEqual(await self.client.recv(), response)

    async def test_primary_daemon_loss_is_sticky_and_does_not_reconnect(self):
        await self.bind_primary()
        await self.upstream.close(code=1001, reason='daemon shutdown')
        await asyncio.wait_for(self.client.wait_closed(), 2)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.losses), 1)
        self.assertIn('daemon shutdown', self.losses[0])
        async with unix_connect(self.spec['socket_path']) as retry:
            await asyncio.wait_for(retry.wait_closed(), 2)
            self.assertEqual(retry.close_code, 1011)
            self.assertIn('resume', retry.close_reason)
        self.assertEqual(len(self.losses), 1)

    async def test_rejected_secondary_launch_and_disconnect_do_not_lose_primary(self):
        await self.bind_primary()
        async with unix_connect(self.spec['socket_path']) as secondary:
            await secondary.send('{"id":4,"method":"thread/start","params":{}}')
            self.assertIn('error', json.loads(await secondary.recv()))
        await asyncio.sleep(.05)
        self.assertEqual(self.losses, [])
        await self.client.send('{"id":8,"method":"future/method"}')
        self.assertEqual(await self.receive(), '{"id":8,"method":"future/method"}')

    async def test_native_navigation_cannot_replace_bound_conversation(self):
        await self.bind_primary()
        # Codex 0.163.0 /new and /clear issue thread/start; /resume issues
        # thread/resume on this same transport. Reject before the daemon sees it.
        for request_id, method, params, reason in (
            (10, 'thread/start', {}, 'already owns'),
            (11, 'thread/resume', {'threadId': str(uuid4())}, 'does not match'),
            (12, 'thread/fork', {'threadId': self.native_id}, 'separate Multiverse launch'),
        ):
            await self.client.send(json.dumps({'id': request_id, 'method': method, 'params': params}))
            response = json.loads(await asyncio.wait_for(self.client.recv(), 2))
            self.assertEqual(response['id'], request_id)
            self.assertIn(reason, response['error']['message'])
            self.assertTrue(self.received.empty())
            self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.native_id)
        # A rejected navigation leaves the original session usable.
        request = {'id': 13, 'method': 'turn/start', 'params': {'threadId': self.native_id, 'input': []}}
        await self.client.send(json.dumps(request))
        self.assertEqual(json.loads(await self.receive()), request)
        await self.upstream.send('{"id":13,"result":{"turn":{"id":"original-turn"}}}')
        self.assertEqual(json.loads(await self.client.recv())['result']['turn']['id'], 'original-turn')
        self.assertEqual(self.losses, [])

    async def test_missing_daemon_before_binding_reports_lost(self):
        Path(self.spec['upstream_path']).unlink()
        await self.connect(expect_upstream=False)
        await asyncio.wait_for(self.client.wait_closed(), 2)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.losses), 1)
        self.assertIn('No such file', self.losses[0])

    async def test_normal_bound_terminal_exit_is_not_a_relay_failure(self):
        await self.bind_primary()
        await self.client.close(code=1000, reason='terminal exit')
        await asyncio.wait_for(self.upstream.wait_closed(), 2)
        await asyncio.sleep(.05)
        self.assertEqual(self.losses, [])

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

    async def test_resume_without_flags_preserves_native_tui_policy_fields(self):
        self.settings['native_id'] = self.native_id
        await self.connect()
        await self.client.send(json.dumps({'id': 3, 'method': 'thread/resume', 'params': {
            'threadId': self.native_id, 'config': {'model_reasoning_effort': 'high'},
            'sandbox': 'workspace-write', 'approvalPolicy': 'on-request'}}))
        request = json.loads(await self.receive())
        self.assertEqual(request['params']['developerInstructions'], 'MV2 bootstrap')
        self.assertEqual(request['params']['config'], {'model_reasoning_effort': 'high'})
        self.assertEqual(request['params']['sandbox'], 'workspace-write')
        self.assertEqual(request['params']['approvalPolicy'], 'on-request')
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

    async def test_resume_applies_grants_and_validates_native_effective_reply(self):
        self.settings.update(native_id=self.native_id,
                             resume_permissions={'sandbox': 'danger-full-access', 'approvalPolicy': 'never'})
        await self.connect()
        await self.client.send(json.dumps({'id': 3, 'method': 'thread/resume', 'params': {
            'threadId': self.native_id, 'sandbox': None, 'approvalPolicy': None}}))
        request = json.loads(await self.receive())
        self.assertEqual(request['params']['sandbox'], 'danger-full-access')
        self.assertEqual(request['params']['approvalPolicy'], 'never')
        response = json.dumps({'id': 3, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)},
                                                 'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never'}})
        await self.upstream.send(response)
        self.assertEqual(await self.client.recv(), response)
        self.assertTrue(self.protocol.bound)
        self.assertEqual(self.errors, [])

    async def test_ignored_loaded_thread_grant_does_not_bind_or_name(self):
        self.settings.update(native_id=self.native_id,
                             resume_permissions={'sandbox': 'danger-full-access', 'approvalPolicy': 'never'})
        await self.connect()
        await self.client.send(json.dumps({'id': 3, 'method': 'thread/resume', 'params': {'threadId': self.native_id}}))
        await self.receive()
        await self.upstream.send(json.dumps({'id': 3, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)},
                                              'sandbox': {'type': 'workspaceWrite'}, 'approvalPolicy': 'never'}}))
        await asyncio.wait_for(self.client.wait_closed(), 2)
        self.assertIn('did not apply requested sandbox', self.errors[-1])
        self.assertNotIn('native_session_id', read(self.directory / 'properties.json'))
        self.assertFalse(self.protocol.bound)
        self.assertEqual(self.naming, [])

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

    async def test_native_refusal_is_published_before_forward_and_native_retry_cannot_bind(self):
        self.settings['native_id'] = self.native_id
        refusals = []
        await self.connect(record_refusal=lambda evidence: refusals.append(deepcopy(evidence)))
        request = {'id': 2, 'method': 'thread/resume', 'params': {'threadId': self.native_id}}
        await self.client.send(json.dumps(request))
        await self.receive()
        message = f'thread {self.native_id} already has an active writer'
        response = json.dumps({'id': 2, 'error': {'code': -32600, 'message': message}})
        await self.upstream.send(response)
        self.assertEqual(await self.client.recv(), response)
        self.assertEqual(refusals[0]['message'], message)
        self.assertEqual(refusals[0]['code'], -32600)
        first = deepcopy(refusals[0])
        # R is a native retry, not a new mv2 launch. Its response still reaches
        # the terminal, but cannot bind or release input in this failed launch.
        await self.client.send(json.dumps({**request, 'id': 3}))
        await self.receive()
        retry = json.dumps({'id': 3, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})
        await self.upstream.send(retry)
        self.assertEqual(await self.client.recv(), retry)
        self.assertFalse(self.protocol.bound)
        self.assertNotIn('native_session_id', read(self.directory / 'properties.json'))
        self.assertTrue(all(evidence == first for evidence in refusals))
        self.assertEqual(self.naming, [])
        await self.client.send(json.dumps({'id': 4, 'method': 'turn/start',
                                          'params': {'threadId': self.native_id, 'input': []}}))
        rejected = json.loads(await self.client.recv())
        self.assertIn(message, rejected['error']['message'])
        self.assertTrue(self.received.empty())

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
