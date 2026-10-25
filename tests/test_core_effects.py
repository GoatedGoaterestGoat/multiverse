"""Effect checks at core entry points, independent of plugin API permission checks."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.harnesses.types import EFFECTS, HarnessAdapter, NativeResult, ObservedBinding
from multiverse.plugins import access
from multiverse.runtime import effects, lifecycle, native
from multiverse.store import read, write
from multiverse.tools import capabilities, deliver, inbox, send
from multiverse.universe import agents, mailbox
from test_message_delivery import DeliveryFixture


class CoreEffectsTest(DeliveryFixture):
    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_denied_actions_leave_no_messages_launches_or_receipts(self):
        """Checking only at the adapter is too late: a denied action must not publish a send or launch first."""
        message = mailbox.append_message(self.directory, 'saved', {'name': 'person'}, delivery='steer',
                                         target_session=self.native_id)
        actions = (
            ('send', lambda: send.run(self.root, agent=self.agent, text='blocked')),
            ('inbox-only send', lambda: send.run(self.root, agent=self.agent, text='blocked', delivery='inbox-only')),
            ('spawn', lambda: lifecycle.start(self.root, name='blocked')),
            ('resume', lambda: lifecycle.start(self.root, resume=self.agent)),
            ('kill', lambda: lifecycle.kill(self.root, self.agent)),
            ('stop', lambda: native.stop(self.root, self.agent)),
            ('submission', lambda: native.deliver(self.root, self.agent, message['id'])),
            ('wake', lambda: native.send_or_wake(self.root, self.agent, message['id'])),
            ('held flush', lambda: native.flush_startup(self.root, self.directory, self.launch)),
            ('reopen', lambda: native.reopen_launch_only(self.root, self.directory, message['id'])),
            ('retry', lambda: deliver.run(self.root, agent=self.agent, retry=True)),
            ('dismiss', lambda: deliver.run(self.root, agent=self.agent, dismiss=True)),
            ('consume inbox', lambda: inbox.run(self.root, agent=self.agent)),
        )
        # A removed gate must fail safely rather than start a native process.
        with patch.object(lifecycle, '_record', side_effect=AssertionError('launch preparation reached')), \
                patch.object(native, 'context', side_effect=AssertionError('native context reached')):
            for level in ('observe', 'spend'):
                for label, action in actions:
                    with self.subTest(level=level, action=label):
                        before = self.files()
                        with access.within(level, 'reader'), self.assertRaisesRegex(access.Denied, 'needs act'):
                            action()
                        self.assertEqual(self.files(), before)
        self.assertEqual(self.calls, [])

    def test_observe_checks_and_spend_questions_keep_their_distinct_effects(self):
        """Read-only reconciliation must work without act, while side questions require spend and never send."""
        saved = self.send('accepted')
        mailbox.append_message(self.directory, 'manual', {'name': 'person'}, delivery='inbox-only')
        adapter = SimpleNamespace(
            name='fixture', capabilities=Mock(return_value={}),
            get_usage=Mock(return_value=NativeResult('ok', value={'total_tokens': 2})),
            ask=Mock(return_value=NativeResult('ok', value='answer')),
        )
        with access.within('observe', 'reader'):
            self.assertTrue(deliver.run(self.root, agent=self.agent, check=True)['ok'])
            self.assertEqual(len(inbox.run(self.root, agent=self.agent, peek=True)['messages']), 1)
            self.assertEqual(len(inbox.run(self.root, agent=self.agent, all_messages=True)['messages']), 2)
            self.assertFalse((self.directory / 'inbox.cursor').exists())
            with self.assertRaisesRegex(access.Denied, 'needs spend'):
                native.operation(self.root, self.agent, 'ask', 'question')
        with patch('multiverse.harnesses.load', return_value=adapter):
            with access.within('observe', 'reader'):
                self.assertEqual(native.operation(self.root, self.agent, 'get_usage')['state'], 'ok')
                self.assertEqual(capabilities.run(self.root, agent=self.agent), {})
            with access.within('spend', 'helper'):
                self.assertEqual(native.operation(self.root, self.agent, 'ask', 'question')['value'], 'answer')
                with access.within('observe', 'nested reader'), self.assertRaises(access.Denied):
                    native.operation(self.root, self.agent, 'ask', 'question')
            with access.within(None, 'off plugin'):
                for operation in ('get_usage', 'ask'):
                    with self.subTest(off=operation), self.assertRaises(access.Denied):
                        native.operation(self.root, self.agent, operation, 'question')
        adapter.ask.assert_called_once()
        # A nested dispatch refusal is policy, not a native error result.
        with patch('multiverse.harnesses.load', return_value=adapter), \
                patch.object(adapter, 'get_usage', side_effect=access.Denied('nested call needs act')), \
                access.within('observe', 'reader'), self.assertRaises(access.Denied):
            native.operation(self.root, self.agent, 'get_usage')
        with patch.object(native, 'deliver', side_effect=access.Denied('receipt access revoked')), \
                access.within('observe', 'reader'), self.assertRaises(access.Denied):
            deliver.run(self.root, agent=self.agent, check=True)
        with patch.object(native, 'send_or_wake', side_effect=access.Denied('nested submission refused')), \
                access.within('act', 'sender'), self.assertRaises(access.Denied):
            send.run(self.root, agent=self.agent, text='refused dispatch')
        refused = mailbox.inbox(self.directory, limit=None, all_messages=True)['messages'][-1]
        self.assertEqual(refused['tracking']['submission_state'], 'pending')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(mailbox.message_record(self.directory, saved['id'])[1]['submission_state'], 'accepted')
        # Every original protocol method is classified, including callers added
        # later that bypass today's high-level dispatch functions.
        for method, value in vars(HarnessAdapter).items():
            if callable(value) and not method.startswith('_'):
                self.assertIn(method, EFFECTS)
        with access.within('observe', 'reader'), self.assertRaises(access.Denied):
            effects.call(adapter, 'ask', object(), 'not submitted', None)
        adapter.ask.assert_called_once()

    def test_observe_can_record_a_verified_native_session_change(self):
        """An observe grant must keep /clear bookkeeping without granting a launch or changing storage."""
        home = str(self.work / 'config')
        properties = read(self.directory / 'properties.json')
        properties['native_session_home'] = home
        write(self.directory / 'properties.json', properties)
        current = str(uuid4())
        adapter = SimpleNamespace(observed_binding=lambda session:
            ObservedBinding(native_id=current, home=home, evidence='fixture-process'))
        with access.within('observe', 'reader'):
            updated, _ = native.session_snapshot(self.root, self.directory, properties,
                                                 agents.runtime(self.directory), adapter=adapter)
        self.assertEqual(updated['native_session_id'], current)
        self.assertEqual(updated['native_session_home'], home)
        _, rows = agents.sessions.effective(self.directory, updated)
        self.assertEqual([(row['from'], row['to'], row['reason']) for row in rows],
                         [(self.native_id, current, 'detected')])
