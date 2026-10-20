"""Messages follow their agent; receipt checks retain the original conversation."""
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.types import NativeResult
from multiverse.harnesses.claude import ClaudeAdapter
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from multiverse.tools import deliver
from multiverse.universe import agents
from multiverse.universe.mailbox import append_message, message_record, update_message
from test_message_delivery import DeliveryFixture


class SessionDeliveryTest(DeliveryFixture):
    def setUp(self):
        super().setUp()
        self.adapter = native.harnesses.load(self.root, 'claude')

    def recipient(self, **properties):
        super().recipient(native_session_home=str(self.work), native_session_provider='claude', **properties)

    def clear(self):
        previous = read(self.directory / 'properties.json')['native_session_id']
        target = str(uuid4())
        agents.rebind_session(self.root, self.agent, previous, target, 'clear', self.launch, home=str(self.work))
        return target

    def test_checks_after_clear_use_the_original_conversation(self):
        """A check after /clear looks in the message's original conversation and storage; a break goes unnoticed
        because the properties pointer has moved, so it reports no receipt or reads the wrong transcript."""
        with self.subTest('an accepted message'):
            self.behavior = NativeResult('native_queued')
            message = self.send('accepted before clear')
            current = self.clear()
            checked = []
            def receipt(session, message_id, executor):
                checked.append((session.native_id, session.home, message_id))
                return NativeResult('native_queued', value={'transcript_confirmed': True})
            with patch.object(self.adapter, 'get_delivery', side_effect=receipt):
                result = deliver.run(self.root, agent=self.agent, check=True)
            self.assertTrue(result['ok'])
            self.assertEqual(checked, [(self.native_id, str(self.work), message['id'])])
            self.assertTrue(message_record(self.directory, message['id'])[1]['transcript_confirmed_at'])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], current)
        with self.subTest('an uncertain message'):
            self.recipient()
            self.behavior = TimeoutError('may have arrived')
            message = self.send('uncertain before clear')
            current = self.clear()
            unknown = NativeResult('unknown', 'No receipt yet')
            with patch.object(self.adapter, 'get_delivery', return_value=unknown) as check:
                result = native.deliver(self.root, self.agent, message['id'], check=True)
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(check.call_args.args[0].native_id, self.native_id)
            self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], current)
            self.assertEqual(message_record(self.directory, message['id'])[1]['submission_state'], 'unknown')
            self.assertEqual(len(self.calls), 1)
        with self.subTest("Claude's receipt file, read without native calls"):
            self.recipient()
            self.behavior = NativeResult('native_queued')
            message = self.send('Claude receipt before clear')
            write(self.directory / 'claude-receipts' / f'{message["id"]}.json',
                  {'route': 'steer', 'at': 123})
            self.clear()
            def claude_receipt(*args):
                return ClaudeAdapter.get_delivery(None, *args)
            with patch.object(self.adapter, 'get_delivery', side_effect=claude_receipt):
                result = deliver.run(self.root, agent=self.agent, check=True)
            self.assertTrue(result['ok'])
            self.assertTrue(result['messages'][0]['transcript_confirmed'])
            self.assertEqual(len(self.calls), 1)

    def test_never_submitted_messages_follow_the_new_conversation_once(self):
        """Never-submitted messages are retargeted once to the current conversation on all three send paths, with the
        move journaled; fragile because startup flush, retry and wake each retarget separately (28f9aa9)."""
        with self.subTest('the startup queue'):
            message = append_message(self.directory, 'saved before clear', {}, delivery='steer',
                                     target_session=self.native_id)
            update_message(self.directory, message['id'], submission_state='pending', deferred_launch_id=self.launch)
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running',
                                                    'startup_delivery': 'waiting', 'tmux': self.terminal})
            current = self.clear()
            native.flush_startup(self.root, self.directory, self.launch)
            native.flush_startup(self.root, self.directory, self.launch)
            self.assertEqual([(s.native_id, m.id) for s, m, _ in self.calls], [(current, message['id'])])
            tracking = message_record(self.directory, message['id'])[1]
            self.assertEqual(tracking['target_session'], current)
            change, = tracking['session_changes']
            self.assertEqual((change['from'], change['to']), (self.native_id, current))
            self.assertEqual(change['transitions'][0]['reason'], 'clear')
        with self.subTest('a retry after two clears'):
            self.recipient()
            self.behavior = NativeResult('error', 'not accepted')
            message = self.send('retry after clear')
            self.clear()
            current = self.clear()
            self.behavior = NativeResult('native_queued')
            result = deliver.run(self.root, agent=self.agent, retry=True)
            self.assertTrue(result['ok'])
            self.assertEqual(self.calls[-1][0].native_id, current)
            original, tracking = message_record(self.directory, message['id'])
            self.assertEqual(original['target_session'], self.native_id)
            self.assertEqual(tracking['target_session'], current)
            self.assertEqual(len(tracking['session_changes'][0]['transitions']), 2)
        with self.subTest('a wake prompt'):
            self.recipient()
            message = append_message(self.directory, 'wake after clear', {}, delivery='follow-up',
                                     target_session=self.native_id)
            current = self.clear()
            prompt, tracking = lifecycle._wake_prompt(self.root, self.directory,
                                                    read(self.directory / 'properties.json'), message['id'])
            self.assertIn('wake after clear', prompt)
            self.assertEqual(tracking['target_session'], current)
            self.assertEqual(tracking['session_changes'][0]['from'], self.native_id)

    def test_unknown_and_interrupted_submissions_are_never_retargeted_or_replayed(self):
        """Accepted, unknown or submitting messages are never retargeted; a break duplicates an uncertain message into
        the new conversation, unseen."""
        for state in ('accepted', 'unknown', 'submitting'):
            message = append_message(self.directory, state, {}, delivery='steer', target_session=self.native_id)
            update_message(self.directory, message['id'], submission_state=state,
                           attempts=[{'state': 'unknown', 'reason': 'may have reached old session'}])
        self.clear()
        for message in native.inbox(self.directory, all_messages=True)['messages']:
            result = native.deliver(self.root, self.agent, message['id'], retry=True)
            self.assertTrue(result['reused'])
            tracking = message_record(self.directory, message['id'])[1]
            self.assertNotIn('session_changes', tracking)
            self.assertEqual(message['target_session'], self.native_id)
        self.assertEqual(self.calls, [])

    def test_the_session_journal_authorizes_only_its_own_agent_home_and_harness(self):
        """The session journal authorizes delivery only for its own agent, home and harness; a break delivers into
        another agent's or account's conversation."""
        with self.subTest('a foreign conversation or another home'):
            message = append_message(self.directory, 'foreign', {}, delivery='steer', target_session=str(uuid4()))
            self.clear()
            for check in (False, True):
                with self.assertRaisesRegex(ValueError, 'different native session'):
                    native.deliver(self.root, self.agent, message['id'], retry=not check, check=check)
            historical = append_message(self.directory, 'wrong scope', {}, delivery='steer',
                                        target_session=self.native_id)
            props = read(self.directory / 'properties.json')
            write(self.directory / 'properties.json', {**props, 'native_session_home': str(self.work / 'other')})
            result = native.deliver(self.root, self.agent, historical['id'], check=True)
            self.assertFalse(result['ok'])
            self.assertEqual(self.calls, [])
        with self.subTest('a journal from another harness'):
            self.recipient()
            message = append_message(self.directory, 'wrong harness', {}, delivery='steer',
                                     target_session=self.native_id)
            self.clear()
            props = read(self.directory / 'properties.json')
            write(self.directory / 'properties.json', {**props, 'harness': 'pi'})
            with patch.object(self.adapter, 'get_delivery') as check:
                for checking in (False, True):
                    result = native.deliver(self.root, self.agent, message['id'], check=checking)
                    self.assertFalse(result['ok'])
                check.assert_not_called()
            self.assertEqual(self.calls, [])
        with self.subTest('the exact recipient and conversation across a resume'):
            self.recipient()
            self.behavior = NativeResult('error', 'before send')
            message = self.send()
            props = read(self.directory / 'properties.json')
            write(self.directory / 'properties.json', {**props, 'native_session_id': str(uuid4())})
            with self.assertRaisesRegex(ValueError, 'different native session'):
                native.deliver(self.root, self.agent, message['id'], retry=True)
            with self.assertRaisesRegex(ValueError, 'recipient inbox'):
                message_record(self.directory, str(uuid4()))
            write(self.directory / 'properties.json', props)
            resumed = str(uuid4())
            write(self.directory / 'runtime.json', {'launch_id': resumed, 'state': 'running', 'tmux': self.terminal})
            self.behavior = NativeResult('native_queued')
            result = native.deliver(self.root, self.agent, message['id'], retry=True)
            self.assertEqual(result['native_session_id'], self.native_id)
            self.assertEqual(result['launch_id'], resumed)
