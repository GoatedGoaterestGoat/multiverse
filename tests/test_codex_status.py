"""Offline app-server status streams: no native CLI or model calls."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from websockets.asyncio.server import unix_serve

from multiverse import rpc
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.status import status
from multiverse.harnesses.types import NativeEndpoint, SessionHandle
from multiverse.runner import StatusWatcher
from multiverse.store import read, write


class StatusMappingTest(unittest.TestCase):
    def test_status_mapping_is_conservative(self):
        cases = [({'type': 'idle'}, 'idle'), ({'type': 'active', 'activeFlags': []}, 'working'),
                 ({'type': 'active', 'activeFlags': ['waitingOnApproval']}, 'waiting'),
                 ({'type': 'active', 'activeFlags': ['waitingOnUserInput']}, 'waiting'),
                 ({'type': 'active', 'activeFlags': ['waitingOnApproval', 'waitingOnUserInput']}, 'waiting'),
                 ({'type': 'notLoaded'}, 'unknown'), ({'type': 'systemError'}, 'unknown'),
                 ({'type': 'active'}, 'unknown'), ({'type': 'active', 'activeFlags': ['future']}, 'unknown'),
                 ({'type': 'future'}, 'unknown'), (None, 'unknown')]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(status(value), expected)

    def test_plain_unbound_and_wrong_transport_have_no_watcher(self):
        session = SessionHandle('a', 'codex', '/tmp', native_id=str(uuid4()),
                                endpoint=NativeEndpoint('unix-websocket', '/tmp/rpc.sock'))
        plain = CodexAdapter.configured('codex', {'inbox_bridge': False})
        bridge = CodexAdapter.configured('codex', {'inbox_bridge': True})
        self.assertIsNone(plain.status_watch(replace(session, native_mode={'policy': 'plain', 'mode': 'plain'})))
        self.assertIsNone(bridge.status_watch(replace(session, native_id=None)))
        self.assertIsNone(bridge.status_watch(replace(session, endpoint=None)))
        self.assertIsNone(bridge.status_watch(replace(session, endpoint=NativeEndpoint('unix-json', '/tmp/rpc.sock'))))


class StatusStreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mv2-status-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.socket = str(self.root / 'rpc.sock')
        self.session = SessionHandle('agent', 'codex', str(self.root), launch_id='launch',
                                     native_id=str(uuid4()), state='running',
                                     endpoint=NativeEndpoint('unix-websocket', self.socket))
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        self.requests, self.observed = [], []
        self.initial = {'type': 'idle'}
        self.thread_id = self.session.native_id
        self.cwd = str(self.root)
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.before_snapshot = []
        self.hang = False
        self.reject = False
        self.server = await unix_serve(self.handle, self.socket)
        self.task = None
        self.watcher = None

    async def asyncTearDown(self):
        if self.watcher:
            self.watcher.close()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        self.temp.cleanup()

    async def handle(self, connection):
        self.connection = connection
        async for raw in connection:
            value = json.loads(raw)
            self.requests.append(value)
            method = value.get('method')
            if method == 'initialized':
                continue
            if self.hang:
                await self.release.wait()
                return
            if method == 'initialize':
                await connection.send(json.dumps({'id': value['id'], 'result': {}}))
            elif method == 'thread/read':
                for event in self.before_snapshot:
                    await connection.send(json.dumps(event))
                response = {'error': {'code': -32601, 'message': 'unsupported'}} if self.reject else {
                    'result': {'thread': {'id': self.thread_id, 'cwd': self.cwd, 'status': self.initial}}}
                await connection.send(json.dumps({'id': value['id'], **response}))
                self.ready.set()
            else:
                self.fail('Unexpected native request: ' + repr(value))

    def event(self, state, thread_id=None):
        return {'method': 'thread/status/changed', 'params': {
            'threadId': thread_id or self.session.native_id, 'status': state}}

    async def eventually(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.01)

    async def start_stream(self, timeout=1):
        self.task = asyncio.create_task(rpc.watch(*self.adapter.status_watch(self.session), self.observed.append, timeout))
        await self.eventually(lambda: bool(self.observed))

    async def test_snapshot_transitions_exact_filter_and_disconnect(self):
        await self.start_stream()
        self.assertEqual(self.observed, ['idle'])
        await self.connection.send(json.dumps(self.event({'type': 'active', 'activeFlags': []}, 'other-thread')))
        # Native requests belong to the TUI; the watcher sends no approval reply.
        await self.connection.send(json.dumps({'id': 91, 'method': 'item/commandExecution/requestApproval', 'params': {}}))
        states = [({'type': 'active', 'activeFlags': []}, 'working'),
                  ({'type': 'active', 'activeFlags': ['waitingOnApproval']}, 'waiting'),
                  ({'type': 'active', 'activeFlags': ['waitingOnUserInput']}, 'waiting'),
                  ({'type': 'idle'}, 'idle'), ({'type': 'systemError'}, 'unknown'),
                  ({'type': 'notLoaded'}, 'unknown')]
        for value, expected in states:
            count = len(self.observed)
            await self.connection.send(json.dumps(self.event(value)))
            await self.eventually(lambda: len(self.observed) > count)
            self.assertEqual(self.observed[-1], expected)
        self.assertEqual(len(self.observed), 1 + len(states))
        self.assertEqual([r['method'] for r in self.requests], ['initialize', 'initialized', 'thread/read'])
        self.assertEqual(self.requests[-1]['params'], {'threadId': self.session.native_id, 'includeTurns': False})
        await self.connection.close()
        await self.task
        self.assertEqual(self.observed[-1], 'unknown')

    async def test_snapshot_supersedes_earlier_notification(self):
        self.before_snapshot = [self.event({'type': 'active', 'activeFlags': ['waitingOnUserInput']})]
        await self.start_stream()
        self.assertEqual(self.observed, ['idle'])

    async def test_wrong_identity_rejects_even_matching_early_event(self):
        self.thread_id = 'other'
        self.before_snapshot = [self.event({'type': 'active', 'activeFlags': []})]
        await self.start_stream()
        await self.task
        self.assertEqual(self.observed, ['unknown'])

    async def test_wrong_workspace_rejects_snapshot(self):
        self.cwd = '/another-workspace'
        await self.start_stream()
        await self.task
        self.assertEqual(self.observed, ['unknown'])

    async def test_rejected_snapshot_stays_unknown(self):
        self.reject = True
        await self.start_stream()
        await self.task
        self.assertEqual(self.observed, ['unknown'])

    async def test_bounded_handshake(self):
        self.hang = True
        await self.start_stream(timeout=.05)
        await self.task
        self.assertEqual(self.observed, ['unknown'])

    async def test_malformed_stream_clears_status(self):
        await self.start_stream()
        await self.connection.send('[]')
        await self.task
        self.assertEqual(self.observed, ['idle', 'unknown'])

    async def test_cancellation_closes_connection_and_clears_status(self):
        await self.start_stream()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.connection.wait_closed()
        self.assertEqual(self.observed, ['idle', 'unknown'])

    def setup_runtime(self, bound=True, state='running'):
        self.agent_dir = self.root / 'state/agents/agent'
        self.agent_dir.mkdir(parents=True)
        config = self.root / 'harnesses/codex/harness.json'
        config.parent.mkdir(parents=True)
        write(config, {'schema': 2, 'adapter': 'codex', 'settings': {'inbox_bridge': True}})
        self.properties = {'id': 'agent', 'harness': 'codex', 'workspace': str(self.root)}
        if bound:
            self.properties['native_session_id'] = self.session.native_id
        write(self.agent_dir / 'properties.json', self.properties)
        write(self.agent_dir / 'runtime.json', {'launch_id': 'launch', 'state': state, 'status': 'unknown',
              'control_endpoint': {'transport': 'unix-websocket', 'address': self.socket}})
        self.watcher = StatusWatcher(self.agent_dir, 'launch', self.root)
        self.watcher.start()

    def runtime_status(self):
        return read(self.agent_dir / 'runtime.json')['status']

    async def test_runner_waits_for_binding_writes_runtime_and_stops(self):
        self.setup_runtime(bound=False)
        await asyncio.sleep(.15)
        self.assertEqual(self.requests, [])
        self.properties['native_session_id'] = self.session.native_id
        write(self.agent_dir / 'properties.json', self.properties)
        await self.eventually(lambda: self.runtime_status() == 'idle')
        await self.connection.send(json.dumps(self.event({'type': 'active', 'activeFlags': ['waitingOnApproval']})))
        await self.eventually(lambda: self.runtime_status() == 'waiting')
        await asyncio.to_thread(self.watcher.close)
        self.assertFalse(self.watcher.thread.is_alive())
        self.assertEqual(self.runtime_status(), 'unknown')

    async def test_runner_daemon_loss_does_not_reconnect(self):
        self.setup_runtime()
        await self.eventually(lambda: self.runtime_status() == 'idle')
        await self.connection.close()
        await self.eventually(lambda: not self.watcher.thread.is_alive())
        self.assertEqual(self.runtime_status(), 'unknown')
        self.assertEqual(len(self.requests), 3)

    async def test_resume_watcher_waits_for_launch_validation_with_saved_id(self):
        self.setup_runtime(state='starting')
        await asyncio.sleep(.15)
        self.assertEqual(self.requests, [])
        facts = read(self.agent_dir / 'runtime.json')
        write(self.agent_dir / 'runtime.json', {**facts, 'state': 'running'})
        await self.eventually(lambda: self.runtime_status() == 'idle')

    async def test_old_launch_cannot_overwrite_new_runtime(self):
        self.setup_runtime()
        await self.eventually(lambda: self.runtime_status() == 'idle')
        write(self.agent_dir / 'runtime.json', {'launch_id': 'replacement', 'state': 'running', 'status': 'working'})
        await self.eventually(lambda: not self.watcher.thread.is_alive())
        self.assertEqual(self.runtime_status(), 'working')


class RunnerLifetimeTest(unittest.TestCase):
    def test_bridge_is_starting_until_relay_accepts_binding(self):
        from multiverse import runner
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent_id, launch_id, native_id = str(uuid4()), str(uuid4()), str(uuid4())
            directory = root / 'state/agents' / agent_id
            launch = directory / 'launches' / launch_id
            launch.mkdir(parents=True)
            write(directory / 'properties.json', {'id': agent_id, 'name': 'worker', 'harness': 'codex', 'native_session_id': native_id})
            write(directory / 'runtime.json', {'launch_id': launch_id, 'state': 'starting'})
            write(launch / 'request.json', {'argv': ['fake-codex'],
                  'attachment': {'readiness': 'binding'},
                  'rpc_proxy': {'socket_path': '/tmp/mv2-native-' + launch_id + '/rpc.sock', 'settings': {}, 'binds_session': True},
                  'env': {'MV2_CLI': str(root / 'bin/mv2'), 'MV2_UNIVERSE': str(root)}})
            (launch / 'ready').touch()
            process = Mock(pid=123)
            process.poll.return_value = 0
            def waited(timeout=None):
                self.assertEqual(read(directory / 'runtime.json')['state'], 'starting')
                proxy_factory.call_args.args[2](native_id)
                self.assertEqual(read(directory / 'runtime.json')['state'], 'running')
                return 0
            process.wait.side_effect = waited
            with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                 patch.object(runner.subprocess, 'Popen', return_value=process), \
                 patch('multiverse.harnesses.load'), patch('multiverse.rpc.LaunchProxy') as proxy_factory:
                self.assertEqual(runner.main(), 0)

    def test_watcher_follows_terminal_exit_and_signal_but_plain_has_none(self):
        from multiverse import runner
        for bridge, outcome in [(False, 0), (True, 0), (True, 1), (True, SystemExit(143))]:
            with self.subTest(bridge=bridge, outcome=outcome), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                directory = root / 'state/agents/agent'
                launch = directory / 'launches/launch'
                launch.mkdir(parents=True)
                write(directory / 'runtime.json', {'launch_id': 'launch', 'state': 'starting'})
                write(launch / 'request.json', {'argv': ['fake-codex'], 'shared_service': bridge,
                      'attachment': {'observe_status': bridge},
                      'env': {'MV2_CLI': str(root / 'bin/mv2'), 'MV2_UNIVERSE': str(root)}})
                (launch / 'ready').touch()
                process = Mock(pid=123)
                process.poll.return_value = 0
                if isinstance(outcome, BaseException):
                    process.wait.side_effect = outcome
                else:
                    process.wait.return_value = outcome
                watcher = Mock()
                with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                     patch.object(runner.subprocess, 'Popen', return_value=process), \
                     patch.object(runner, 'StatusWatcher', return_value=watcher) as factory:
                    if isinstance(outcome, BaseException):
                        with self.assertRaises(SystemExit):
                            runner.main()
                    else:
                        self.assertEqual(runner.main(), outcome)
                    if bridge:
                        factory.assert_called_once_with(directory, 'launch', root)
                        watcher.start.assert_called_once()
                        watcher.close.assert_called_once()
                    else:
                        factory.assert_not_called()
                self.assertEqual(read(directory / 'runtime.json')['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
