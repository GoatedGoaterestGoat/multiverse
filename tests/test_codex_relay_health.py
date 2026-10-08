"""Relay loss is visible and refuses input without losing exact-thread controls."""
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.harnesses.types import NativeResult
from multiverse.runtime.runner import RelayPublisher, StatusWatcher
from multiverse.universe.mailbox import inbox
from multiverse.store import read, write
from multiverse.tools import list_agents, send
from test_multiverse import BaseTest


class RelayHealthTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / str(uuid4())
        self.launch = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'relay', 'harness': 'codex',
            'workspace': str(self.work), 'native_session_id': str(uuid4())})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'working',
            'pid': 123, 'startup_delivery': 'waiting',
            'control_endpoint': {'transport': 'unix-websocket', 'address': '/fake/native.sock'}})
        write(self.directory / 'launches' / self.launch / 'request.json', {'rpc_proxy': {'settings': {}}})
        self.publish = RelayPublisher(self.directory, self.launch)
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'alive'}))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.enterContext(patch.object(native.NativeExecutor, 'check_session'))

    def test_loss_overrides_optional_status_and_fails_readiness(self):
        self.publish('daemon shutdown')
        StatusWatcher(self.directory, self.launch, self.root)._publish('working')
        row = lifecycle.listing(self.root)[0]
        self.assertEqual((row['live'], row['status']), ('alive', 'disconnected'))
        self.assertIn('resume', row['connection_error'])
        output = list_agents.run(self.root)
        self.assertIn('disconnected', output)
        self.assertIn(row['connection_error'], output)
        result = lifecycle.wait_ready(self.root, self.directory, self.launch, harnesses.load(self.root, 'codex'), .1)
        self.assertFalse(result['ready'])
        self.assertNotIn('timed_out', result)
        self.assertIn('relay lost', result['error'])

    def test_lost_starting_or_running_bridge_refuses_new_messages_before_rpc(self):
        for state in ('starting', 'running'):
            agents.update_runtime(self.directory, self.launch, state=state)
            self.publish('socket gone')
            with patch.object(native.NativeExecutor, 'rpc', side_effect=AssertionError('Lost relay must never submit')):
                for mode in ('steer', 'follow-up'):
                    result = send.run(self.root, agent='relay', text=state + mode, delivery=mode)
                    self.assertEqual(result['state'], 'unavailable')
                    self.assertIn('relay lost', result['native_delivery']['reason'])
        history = inbox(self.directory, all_messages=True)['messages']
        self.assertTrue(all(m['tracking']['submission_state'] == 'failed' for m in history))

    def test_receipts_and_stop_remain_available_on_exact_native_thread(self):
        self.publish('socket gone')
        _, adapter, session, _ = native.context(self.root, 'relay')
        executor = Mock()
        executor.rpc.return_value = NativeResult('ok')
        capabilities = adapter.capabilities(session, executor)
        self.assertEqual(capabilities['send_follow_up'].state, 'unavailable')
        self.assertEqual(capabilities['get_delivery'].state, 'supported')
        self.assertEqual(capabilities['stop'].state, 'supported')
        self.assertEqual(adapter.stop(session, executor).state, 'ok')
        self.assertEqual(adapter.get_delivery(session, 'internal-message-id', executor).state, 'ok')

    def test_stale_or_ended_launch_cannot_publish_loss(self):
        for launch, state in (('replacement', 'running'), (self.launch, 'stopped'), (self.launch, 'ending')):
            write(self.directory / 'runtime.json', {'launch_id': launch, 'state': state})
            self.publish('must not overwrite')
            self.assertNotIn('native_relay', read(self.directory / 'runtime.json'))
