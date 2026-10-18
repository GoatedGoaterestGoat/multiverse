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
from multiverse.harnesses.types import LaunchContext, NativeEndpoint, SessionHandle
from multiverse.store import read, write


class CodexLaunchTest(unittest.TestCase):
    def setUp(self):
        self.native_id = str(uuid4())
        self.values = {'name': 'worker', 'workspace': '/workspace', 'agent_dir': '/outside/state/agent',
                       'bootstrap_json': json.dumps('bootstrap'), 'task': '--literal prompt',
                       'session_id': self.native_id, 'control_socket': '/tmp/owned/rpc.sock'}
        self.context = LaunchContext('spawn', self.values, endpoint=NativeEndpoint('unix-websocket', '/tmp/native.sock'))

    def test_update_check_is_off_on_every_launch_path_unless_set(self):
        """Every launch path passes check_for_update_on_startup=false exactly once unless the user set it; fragile
        because Codex accepts five -c spellings across three argument sources, and a miss leaves a wake prompt stuck
        behind the update dialog (2fd65b2)."""
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
        unrelated = ['-c', 'developer_instructions="check_for_update_on_startup=true"']
        for settings in ({}, {'inbox_bridge': True}, {'inbox_bridge': False},
                         {'inbox_bridge': True, 'args': unrelated}):
            adapter = CodexAdapter.configured('codex', settings)
            for mode in ('spawn', 'empty', 'resume', 'wake'):
                with self.subTest(settings=settings, mode=mode):
                    context = self.context
                    if mode == 'empty':
                        context = replace(context, values={**self.values, 'task': ''})
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
                                command = (adapter.spawn(self.context) if mode == 'spawn'
                                           else adapter.resume(session, self.context))
                                before_prompt = command.argv[:command.argv.index('--')]
                                observed = [arg for arg in before_prompt if 'check_for_update_on_startup' in arg]
                                expected = value if source in ('args', mode + '_args') else 'false'
                                self.assertEqual(len(observed), 1, before_prompt)
                                self.assertEqual(observed[0].rsplit('=', 1)[1].strip(), expected)
                        self.assertEqual(settings[source], flags)

    def test_resume_permission_flags_become_grants(self):
        """Resume flags (--sandbox, -s, --ask-for-approval, -a, --dangerously-bypass...) become thread/resume grants
        and malformed ones fail before any native call; fragile because of the many spellings, and a miss silently
        resumes a full-access agent read-only (e6a11a4)."""
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.native_id)
        cases = [
            (['--dangerously-bypass-approvals-and-sandbox'],
             {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}),
            (['--sandbox', 'read-only', '--ask-for-approval', 'on-request'],
             {'sandbox': 'read-only', 'approvalPolicy': 'on-request'}),
            (['--sandbox=workspace-write', '--ask-for-approval=never'],
             {'sandbox': 'workspace-write', 'approvalPolicy': 'never'}),
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
        for flags in (['--sandbox'], ['--sandbox=wrong'], ['-s'], ['-sread-only', '--sandbox=workspace-write'],
                      ['-a'], ['--ask-for-approval=wrong'], ['-anever', '-aon-request'],
                      ['--dangerously-bypass-approvals-and-sandbox=true']):
            with self.subTest(malformed=flags):
                adapter = CodexAdapter.configured('codex', {'inbox_bridge': True, 'resume_args': flags})
                executor = Mock()
                with self.assertRaises(ValueError):
                    adapter.resume(session, replace(self.context, executor=executor))
                executor.rpc.assert_not_called()

    def test_wake_waits_for_exact_tui_starting_turn_reply(self):
        """A wake prompt counts as submitted only on the exact thread's successful turn/start reply; a break goes
        unnoticed because a failed wake is marked delivered and nothing else shows it."""
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

    async def fresh(self):
        """Start the next row on a new relay, scripted daemon and agent folder."""
        await self.asyncTearDown()
        await self.asyncSetUp()

    async def receive(self):
        return await asyncio.wait_for(self.received.get(), 2)

    async def test_start_merges_instructions_and_roots_and_binds_exact_reply(self):
        """mv2's bootstrap and writable roots are merged into the TUI's thread/start and the exact reply binds, also
        for a request.json saved before naming; a break goes unnoticed because the agent works, just without mv2's
        instructions."""
        for named in (True, False):
            with self.subTest(named=named):
                await self.fresh()
                if not named:
                    del self.settings['name']
                await self.connect()
                await self.client.send(json.dumps({'id': 2, 'method': 'thread/start', 'params': {
                    'developerInstructions': 'User configured instructions',
                    'config': {'sandbox_workspace_write': {'writable_roots': ['/user-root'],
                                                           'network_access': True}}}}))
                request = json.loads(await self.receive())
                self.assertEqual(request['params']['developerInstructions'],
                                 'MV2 bootstrap\n\nUser configured instructions')
                policy = request['params']['config']['sandbox_workspace_write']
                self.assertEqual(policy['writable_roots'], ['/mv2-state', '/user-root'])
                self.assertTrue(policy['network_access'])
                response = json.dumps({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)}}})
                await self.upstream.send(response)
                self.assertEqual(await self.client.recv(), response)
                self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.native_id)
                if named:
                    self.assertEqual([v['method'] for v in self.naming],
                                     ['initialize', 'initialized', 'thread/name/set'])
                    self.assertEqual(self.naming[-1]['params'], {'threadId': self.native_id, 'name': 'relay-agent'})
                else:
                    self.assertEqual(self.naming, [])
                self.assertEqual(self.errors, [])

    async def test_resume_grants_are_applied_and_verified(self):
        """Resume grants are applied and Codex's effective reply is checked before binding; fragile because Codex
        silently ignores grants for an already-loaded thread (e6a11a4)."""
        grants = {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}
        with self.subTest('without flags the TUI policy fields pass unchanged'):
            await self.fresh()
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
        with self.subTest('grants are applied and the effective reply binds'):
            await self.fresh()
            self.settings.update(native_id=self.native_id, resume_permissions=grants)
            await self.connect()
            await self.client.send(json.dumps({'id': 3, 'method': 'thread/resume', 'params': {
                'threadId': self.native_id, 'sandbox': None, 'approvalPolicy': None}}))
            request = json.loads(await self.receive())
            self.assertEqual(request['params']['sandbox'], 'danger-full-access')
            self.assertEqual(request['params']['approvalPolicy'], 'never')
            response = json.dumps({'id': 3, 'result': {'thread': {'id': self.native_id, 'cwd': str(self.root)},
                                                     'sandbox': {'type': 'dangerFullAccess'},
                                                     'approvalPolicy': 'never'}})
            await self.upstream.send(response)
            self.assertEqual(await self.client.recv(), response)
            self.assertTrue(self.protocol.bound)
            self.assertEqual(self.errors, [])
        with self.subTest('a grant the loaded thread ignored neither binds nor names'):
            await self.fresh()
            self.settings.update(native_id=self.native_id, resume_permissions=grants)
            await self.connect()
            await self.client.send(json.dumps({'id': 3, 'method': 'thread/resume',
                                              'params': {'threadId': self.native_id}}))
            await self.receive()
            await self.upstream.send(json.dumps({'id': 3, 'result': {
                'thread': {'id': self.native_id, 'cwd': str(self.root)},
                'sandbox': {'type': 'workspaceWrite'}, 'approvalPolicy': 'never'}}))
            await asyncio.wait_for(self.client.wait_closed(), 2)
            self.assertIn('did not apply requested sandbox', self.errors[-1])
            self.assertNotIn('native_session_id', read(self.directory / 'properties.json'))
            self.assertFalse(self.protocol.bound)
            self.assertEqual(self.naming, [])
        settings = {'bootstrap': 'b', 'roots': [], 'workspace': '/workspace', 'native_id': self.native_id,
                    'resume_permissions': grants}
        for policy in ({}, {'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'on-request'},
                       {'sandbox': None, 'approvalPolicy': 'never'}):
            with self.subTest(effective=policy):
                protocol = LaunchProtocol(settings)
                protocol.outgoing({'id': 1, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
                with self.assertRaisesRegex(ValueError, 'did not apply requested'):
                    protocol.incoming({'id': 1, 'result': {
                        'thread': {'id': self.native_id, 'cwd': '/workspace'}, **policy}})
                self.assertFalse(protocol.bound)
        # Through a connection view too, these stay ordinary errors that fail the launch, not local rejections.
        for change in ({'cwd': '/other'}, {'sandbox': {'type': 'readOnly'}}):
            with self.subTest(view=change):
                root = LaunchProtocol(settings)
                view = root.for_connection('initial', lambda: False)
                view.outgoing({'id': 5, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
                reply = {'thread': {'id': self.native_id, 'cwd': change.get('cwd', '/workspace')},
                         'sandbox': change.get('sandbox', {'type': 'dangerFullAccess'}), 'approvalPolicy': 'never'}
                with self.assertRaises(ValueError):
                    view.incoming({'id': 5, 'result': reply})
                self.assertFalse(root.bound)

    async def test_stale_malformed_or_wrong_launch_never_binds(self):
        """A stale or malformed launch, or a reply for another thread or workspace, never binds; a break binds a
        replaced launch or a wrong thread with no visible symptom."""
        with self.subTest('stale launch'):
            await self.fresh()
            await self.connect()
            await self.client.send('{"id":2,"method":"thread/start","params":{}}')
            await self.receive()
            write(self.directory / 'runtime.json', {'launch_id': 'replacement'})
            await self.upstream.send(json.dumps({'id': 2, 'result': {
                'thread': {'id': self.native_id, 'cwd': str(self.root)}}}))
            await asyncio.wait_for(self.client.wait_closed(), 2)
            self.assertNotIn('native_session_id', read(self.directory / 'properties.json'))
            self.assertTrue(self.errors)
            self.assertEqual(self.naming, [])
        with self.subTest('malformed launch'):
            await self.fresh()
            await self.connect()
            await self.client.send('{"id":7,"method":"thread/start","params":null}')
            response = json.loads(await self.client.recv())
            self.assertEqual(response['id'], 7)
            self.assertEqual(response['error']['code'], -32602)
            self.assertTrue(self.errors)
            self.assertTrue(self.received.empty())
            self.assertNotIn('native_session_id', read(self.directory / 'properties.json'))
        with self.subTest('wrong thread or workspace'):
            protocol = LaunchProtocol({'bootstrap': 'b', 'roots': [], 'workspace': '/workspace',
                                       'native_id': self.native_id})
            with self.assertRaisesRegex(ValueError, 'does not match'):
                protocol.outgoing({'id': 1, 'method': 'thread/resume', 'params': {'threadId': str(uuid4())}})
            protocol.outgoing({'id': 1, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
            with self.assertRaisesRegex(ValueError, 'different thread ID'):
                protocol.incoming({'id': 1, 'result': {'thread': {'id': str(uuid4()), 'cwd': '/workspace'}}})
            protocol.outgoing({'id': 2, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
            with self.assertRaisesRegex(ValueError, 'workspace'):
                protocol.incoming({'id': 2, 'result': {'thread': {'id': self.native_id, 'cwd': '/other'}}})
            self.assertFalse(protocol.bound)

    async def test_navigation_and_second_clients_cannot_replace_the_bound_thread(self):
        """/new, /clear, /resume, /fork and second connections cannot replace the bound thread; fragile because
        Codex 0.163 sends these on the same transport, and a break silently moves all mv2 delivery to another
        conversation."""
        await self.bind_primary()
        async with unix_connect(self.spec['socket_path']) as secondary:
            await secondary.send('{"id":4,"method":"thread/start","params":{}}')
            self.assertIn('error', json.loads(await secondary.recv()))
        await asyncio.sleep(.05)
        self.assertEqual(self.losses, [])
        await self.client.send('{"id":8,"method":"future/method"}')
        self.assertEqual(await self.receive(), '{"id":8,"method":"future/method"}')
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
        # The launch protocol itself, without a connection view, rejects without failing its launch.
        protocol = LaunchProtocol({'native_id': self.native_id, 'workspace': '/workspace',
                                   'roots': [], 'bootstrap': '', 'resume_permissions': {}})
        protocol.outgoing({'id': 1, 'method': 'thread/resume', 'params': {'threadId': self.native_id}})
        protocol.incoming({'id': 1, 'result': {'thread': {'id': self.native_id, 'cwd': '/workspace'}}})
        with self.assertRaises(ValueError):
            protocol.outgoing({'id': 2, 'method': 'thread/start', 'params': {}})
        self.assertIsNone(protocol.launch_refusal)
        self.assertTrue(protocol.bound)

    async def test_native_refusal_is_published_before_forward_and_native_retry_cannot_bind(self):
        """The relay publishes a native refusal before forwarding it, and a native R retry cannot bind; a break lets
        the retry quietly attach mv2 to a conversation another writer owns (661fc41)."""
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

    async def test_relay_loss_is_classified_by_who_closed(self):
        """Which side closed, bound or not, decides whether relay loss is recorded, with matching close codes and no
        reconnect; fragile because it depends on websocket close codes, and a silent reconnect to a restarted daemon
        skips recovery's idle proof."""
        for bound, closer in ((True, 'daemon'), (False, 'daemon'), (True, 'terminal'), (False, 'terminal'),
                              (False, 'missing daemon')):
            with self.subTest(bound=bound, closer=closer):
                await self.fresh()
                if closer == 'missing daemon':
                    Path(self.spec['upstream_path']).unlink()
                    await self.connect(expect_upstream=False)
                    await asyncio.wait_for(self.client.wait_closed(), 2)
                elif bound:
                    await self.bind_primary()
                else:
                    await self.connect()
                if closer == 'daemon':
                    await self.upstream.close(code=1001, reason='daemon shutdown')
                    await asyncio.wait_for(self.client.wait_closed(), 2)
                    self.assertEqual(self.client.close_code, 1001)
                elif closer == 'terminal':
                    await self.client.close(code=1000, reason='terminal exit')
                    await asyncio.wait_for(self.upstream.wait_closed(), 2)
                    self.assertEqual(self.upstream.close_reason, 'terminal exit')
                await asyncio.sleep(.05)
                if closer == 'terminal':
                    self.assertEqual(self.losses, [])
                else:
                    self.assertEqual(len(self.losses), 1)
                    self.assertIn('daemon shutdown' if closer == 'daemon' else 'No such file', self.losses[0])
                    async with unix_connect(self.spec['socket_path']) as retry:
                        await asyncio.wait_for(retry.wait_closed(), 2)
                        self.assertEqual(retry.close_code, 1011)
                        self.assertIn('resume', retry.close_reason)
                    self.assertEqual(len(self.losses), 1)
                await asyncio.to_thread(self.proxy.close)
                self.assertFalse(self.proxy.thread.is_alive())

    async def test_large_unknown_and_approval_frames_pass_through(self):
        """A 12 MB resume reply keeps the connection open, oversized messages are still bounded, and unknown, binary
        and approval frames pass through unanswered; fragile because a frame limit or pass-through change breaks
        large conversations in a reconnect loop with no error (1166668)."""
        with self.subTest('unknown, binary and approval frames'):
            await self.fresh()
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
        with self.subTest('large resume history and following frames'):
            await self.fresh()
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
        with self.subTest('oversized native message'):
            await self.fresh()
            with patch.object(rpc.LaunchProxy, 'MAX_MESSAGE_SIZE', 1024):
                await self.connect()
                await self.upstream.send(json.dumps({'method': 'future/event', 'text': 'x' * 2048}))
                await asyncio.wait_for(self.client.wait_closed(), 2)
                await asyncio.to_thread(self.proxy.close)
                self.assertTrue(self.errors)
                self.assertEqual(self.upstream.close_code, 1009)


if __name__ == '__main__':
    unittest.main()
