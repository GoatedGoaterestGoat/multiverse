"""Offline app-server status streams: no native CLI or model calls."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from websockets.asyncio.server import unix_serve

from multiverse.runtime import rpc
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.status import status
from multiverse.harnesses.types import NativeEndpoint, SessionHandle
from multiverse.runtime.runner import StatusWatcher
from multiverse.store import read, write


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

    async def test_status_stream_maps_conservatively(self):
        """Codex status notifications map to idle, working, waiting or unknown, ending unknown unless the exact thread
        is confirmed; fragile because Codex adds status types and activeFlags, and a wrong idle feeds recovery's idle
        proof unnoticed."""
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
        with self.subTest('transitions on the exact thread, then disconnect'):
            await self.start_stream()
            self.assertEqual(self.observed, ['idle'])
            await self.connection.send(json.dumps(self.event({'type': 'active', 'activeFlags': []}, 'other-thread')))
            # Native requests belong to the TUI; the watcher sends no approval reply.
            await self.connection.send(json.dumps({'id': 91, 'method': 'item/commandExecution/requestApproval',
                                                   'params': {}}))
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
        with self.subTest('the snapshot supersedes an earlier notification; cancelling closes and clears'):
            self.observed = []
            self.before_snapshot = [self.event({'type': 'active', 'activeFlags': ['waitingOnUserInput']})]
            await self.start_stream()
            self.assertEqual(self.observed, ['idle'])
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            await self.connection.wait_closed()
            self.assertEqual(self.observed, ['idle', 'unknown'])
        with self.subTest('a malformed frame clears the status'):
            self.observed, self.before_snapshot = [], []
            await self.start_stream()
            await self.connection.send('[]')
            await self.task
            self.assertEqual(self.observed, ['idle', 'unknown'])
        early = [self.event({'type': 'active', 'activeFlags': []})]
        # The hung handshake goes last: its connection waits for release until tearDown.
        for changes in ({'thread_id': 'other', 'before_snapshot': early}, {'cwd': '/another-workspace'},
                        {'reject': True}, {'hang': True}):
            with self.subTest(changes=changes):
                self.observed, self.before_snapshot = [], []
                self.thread_id, self.cwd = self.session.native_id, str(self.root)
                for name, value in changes.items():
                    setattr(self, name, value)
                await self.start_stream(timeout=.05 if self.hang else 1)
                await self.task
                self.assertEqual(self.observed, ['unknown'])

    def setup_runtime(self, name, bound=True, state='running'):
        self.agent_dir = self.root / 'state/agents' / name
        write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex',
                                                           'settings': {'inbox_bridge': True}})
        self.properties = {'id': name, 'harness': 'codex', 'workspace': str(self.root)}
        if bound:
            self.properties['native_session_id'] = self.session.native_id
        write(self.agent_dir / 'properties.json', self.properties)
        self.launch_id = str(uuid4())
        write(self.agent_dir / 'runtime.json', {'launch_id': self.launch_id, 'state': state, 'status': 'unknown',
              'control_endpoint': {'transport': 'unix-websocket', 'address': self.socket}})
        self.requests = []
        self.watcher = StatusWatcher(self.agent_dir, self.launch_id, self.root)
        self.watcher.start()

    def runtime_status(self):
        return read(self.agent_dir / 'runtime.json')['status']

    async def test_status_watcher_follows_only_the_validated_thread(self):
        """The status watcher subscribes only after the thread is bound and the launch validated, never reconnects
        after daemon loss, and keeps working without a launch directory; a break shows a plausible but wrong
        status."""
        with self.subTest('waits for binding, works without a launch directory, and stops'):
            with self.assertLogs('multiverse.runtime.attachments', level='WARNING') as messages:
                self.setup_runtime('unbound', bound=False)
                await asyncio.sleep(.15)
                self.assertEqual(self.requests, [])
                self.properties['native_session_id'] = self.session.native_id
                write(self.agent_dir / 'properties.json', self.properties)
                await self.eventually(lambda: self.runtime_status() == 'idle')
                await self.connection.send(json.dumps(self.event({'type': 'active',
                                                                  'activeFlags': ['waitingOnApproval']})))
                await self.eventually(lambda: self.runtime_status() == 'waiting')
                await asyncio.to_thread(self.watcher.close)
            self.assertFalse(self.watcher.thread.is_alive())
            self.assertEqual(self.runtime_status(), 'unknown')
            self.assertEqual(len(messages.output), 1)
            self.assertIn(self.launch_id, messages.output[0])
            self.assertIn('launch directory is missing', messages.output[0])
            self.assertFalse((self.agent_dir / 'launches').exists())
        with self.subTest('daemon loss does not reconnect'):
            self.setup_runtime('lost')
            await self.eventually(lambda: self.runtime_status() == 'idle')
            await self.connection.close()
            await self.eventually(lambda: not self.watcher.thread.is_alive())
            self.assertEqual(self.runtime_status(), 'unknown')
            self.assertEqual(len(self.requests), 3)
        with self.subTest('a resumed launch waits for validation with its saved id'):
            self.setup_runtime('resumed', state='starting')
            await asyncio.sleep(.15)
            self.assertEqual(self.requests, [])
            facts = read(self.agent_dir / 'runtime.json')
            write(self.agent_dir / 'runtime.json', {**facts, 'state': 'running'})
            await self.eventually(lambda: self.runtime_status() == 'idle')
            await asyncio.to_thread(self.watcher.close)


class RunnerLifetimeTest(unittest.TestCase):
    def test_bridge_is_starting_until_relay_accepts_binding(self):
        """A bridge launch stays starting until the relay binds the thread, which is what makes sends wait; a break
        lets early input land in whatever thread the TUI opened."""
        from multiverse.runtime import runner
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
                 patch.object(runner, 'process_started', return_value=None), \
                 patch.object(runner.subprocess, 'Popen', return_value=process), \
                 patch('multiverse.harnesses.load'), patch('multiverse.runtime.rpc.LaunchProxy') as proxy_factory:
                self.assertEqual(runner.main(), 0)


if __name__ == '__main__':
    unittest.main()
