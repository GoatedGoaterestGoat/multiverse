"""Relay loss is visible and refuses input without losing exact-thread controls."""
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import Message, NativeEndpoint, NativeResult, SessionHandle
from multiverse.runtime.runner import RelayPublisher, StatusWatcher
from multiverse.universe.mailbox import inbox
from multiverse.store import read, write
from multiverse.tools import send
from test_multiverse import BaseTest


class RelayHealthTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / str(uuid4())
        self.launch = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'relay', 'harness': 'codex',
            'workspace': str(self.work), 'native_session_id': str(uuid4()),
            'universe_id': read(self.root / 'universe.json')['id']})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'working',
            'pid': 123, 'startup_delivery': 'waiting',
            'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'},
            'control_endpoint': {'transport': 'unix-websocket', 'address': '/fake/native.sock'}})
        write(self.directory / 'launches' / self.launch / 'request.json', {'rpc_proxy': {'settings': {}}})
        self.publish = RelayPublisher(self.directory, self.launch)
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'alive'}))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.enterContext(patch.object(native.NativeExecutor, 'check_session'))

    def test_send_waits_or_fails_before_rpc_unless_the_relay_is_bound(self):
        """While the relay is starting, binding or lost, a send waits or fails before any RPC, wait_ready fails at
        once, and stop and receipt lookup still reach the exact thread; a break submits into a dead socket or makes
        stop unavailable when it is needed (579fc7f)."""
        with self.subTest('binding'):
            adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
            session = SessionHandle('agent', 'codex', '/workspace', native_id=str(uuid4()), state='starting',
                                    endpoint=NativeEndpoint('unix-websocket', '/tmp/native.sock'))
            for mode in ('steer', 'follow-up'):
                executor = Mock()
                result = adapter.send(session, Message('id', 'work', delivery=mode), executor)
                self.assertEqual(result.state, 'unavailable')
                self.assertIn('still binding', result.reason)
                executor.rpc.assert_not_called()
            self.assertEqual(adapter.capabilities(session)['send_steer'].state, 'unavailable')
        with self.subTest('lost while starting or running'):
            for state in ('starting', 'running'):
                agents.update_runtime(self.directory, self.launch, state=state)
                self.publish('socket gone')
                refused = AssertionError('Lost relay must never submit')
                with patch.object(native.NativeExecutor, 'rpc', side_effect=refused):
                    for mode in ('steer', 'follow-up'):
                        result = send.run(self.root, agent='relay', text=state + mode, delivery=mode)
                        self.assertEqual(result['state'], 'unavailable')
                        self.assertIn('relay lost', result['native_delivery']['reason'])
            history = inbox(self.directory, all_messages=True)['messages']
            self.assertTrue(all(m['tracking']['submission_state'] == 'failed' for m in history))
        with self.subTest('lost overrides optional status and fails readiness'):
            self.publish('daemon shutdown')
            StatusWatcher(self.directory, self.launch, self.root)._publish('working')
            row = lifecycle.listing(self.root)[0]
            self.assertEqual((row['live'], row['status']), ('alive', 'disconnected'))
            self.assertIn('resume', row['connection_error'])
            adapter = harnesses.load(self.root, 'codex')
            result = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, .1)
            self.assertFalse(result['ready'])
            self.assertNotIn('timed_out', result)
            self.assertIn('relay lost', result['error'])
        with self.subTest('receipts and stop still reach the exact thread'):
            _, adapter, session, _ = native.context(self.root, 'relay')
            executor = Mock()
            executor.rpc.return_value = NativeResult('ok')
            capabilities = adapter.capabilities(session, executor)
            self.assertEqual(capabilities['send_follow_up'].state, 'unavailable')
            self.assertEqual(capabilities['get_delivery'].state, 'supported')
            self.assertEqual(capabilities['stop'].state, 'supported')
            self.assertEqual(adapter.stop(session, executor).state, 'ok')
            self.assertEqual(adapter.get_delivery(session, 'internal-message-id', executor).state, 'ok')
