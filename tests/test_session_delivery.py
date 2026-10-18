"""Messages follow their agent; receipt checks retain the original conversation."""
import unittest
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.types import NativeResult
from multiverse.harnesses.claude import ClaudeAdapter
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from multiverse.tools import deliver
from multiverse.universe import agents
from multiverse.universe.mailbox import append_message, message_record, update_message
from test_message_delivery import InboxDeliveryTest


class SessionDeliveryTest(InboxDeliveryTest):
    def setUp(self):
        super().setUp()
        props = read(self.directory / 'properties.json')
        props.update(native_session_home=str(self.work), native_session_provider='claude')
        write(self.directory / 'properties.json', props)
        self.adapter = native.harnesses.load(self.root, 'claude')

    def clear(self):
        previous = read(self.directory / 'properties.json')['native_session_id']
        target = str(uuid4())
        agents.rebind_session(self.root, self.agent, previous, target, 'clear', self.launch, home=str(self.work))
        return target

    def test_accepted_receipt_is_checked_in_its_original_owned_conversation(self):
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

    def test_pending_startup_message_is_sent_once_to_the_current_conversation(self):
        message = append_message(self.directory, 'saved before clear', {}, delivery='steer', target_session=self.native_id)
        update_message(self.directory, message['id'], submission_state='pending', deferred_launch_id=self.launch)
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'startup_delivery': 'waiting', 'tmux': self.terminal})
        current = self.clear()
        native.flush_startup(self.root, self.directory, self.launch)
        native.flush_startup(self.root, self.directory, self.launch)
        self.assertEqual([(s.native_id, m.id) for s, m, _ in self.calls], [(current, message['id'])])
        tracking = message_record(self.directory, message['id'])[1]
        self.assertEqual(tracking['target_session'], current)
        change, = tracking['session_changes']
        self.assertEqual((change['from'], change['to']), (self.native_id, current))
        self.assertEqual(change['transitions'][0]['reason'], 'clear')

    def test_claude_receipt_file_remains_readable_after_clear_without_native_io(self):
        self.behavior = NativeResult('native_queued')
        message = self.send('Claude receipt before clear')
        write(self.directory / 'claude-receipts' / f'{message["id"]}.json',
              {'route': 'steer', 'at': 123})
        self.clear()
        with patch.object(self.adapter, 'get_delivery', side_effect=lambda *args: ClaudeAdapter.get_delivery(None, *args)):
            result = deliver.run(self.root, agent=self.agent, check=True)
        self.assertTrue(result['ok'])
        self.assertTrue(result['messages'][0]['transcript_confirmed'])
        self.assertEqual(len(self.calls), 1)

    def test_unknown_receipt_queries_old_conversation_without_replaying_into_new_one(self):
        self.behavior = TimeoutError('may have arrived')
        message = self.send('uncertain before clear')
        current = self.clear()
        with patch.object(self.adapter, 'get_delivery', return_value=NativeResult('unknown', 'No receipt yet')) as check:
            result = native.deliver(self.root, self.agent, message['id'], check=True)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(check.call_args.args[0].native_id, self.native_id)
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], current)
        self.assertEqual(message_record(self.directory, message['id'])[1]['submission_state'], 'unknown')
        self.assertEqual(len(self.calls), 1)

    def test_definite_failure_retry_follows_multiple_clears_and_keeps_original_log(self):
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

    def test_wake_prompt_uses_current_session_and_records_its_retarget(self):
        message = append_message(self.directory, 'wake after clear', {}, delivery='follow-up', target_session=self.native_id)
        current = self.clear()
        prompt, tracking = lifecycle._wake_prompt(self.root, self.directory,
                                                read(self.directory / 'properties.json'), message['id'])
        self.assertIn('wake after clear', prompt)
        self.assertEqual(tracking['target_session'], current)
        self.assertEqual(tracking['session_changes'][0]['from'], self.native_id)

    def test_unknown_and_interrupted_submissions_are_never_retargeted_or_replayed(self):
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

    def test_foreign_or_wrong_home_target_is_never_checked_or_retargeted(self):
        message = append_message(self.directory, 'foreign', {}, delivery='steer', target_session=str(uuid4()))
        self.clear()
        for check in (False, True):
            with self.assertRaisesRegex(ValueError, 'different native session'):
                native.deliver(self.root, self.agent, message['id'], retry=not check, check=check)
        historical = append_message(self.directory, 'wrong scope', {}, delivery='steer', target_session=self.native_id)
        props = read(self.directory / 'properties.json')
        write(self.directory / 'properties.json', {**props, 'native_session_home': str(self.work / 'other')})
        result = native.deliver(self.root, self.agent, historical['id'], check=True)
        self.assertFalse(result['ok'])
        self.assertEqual(self.calls, [])

    def test_a_journal_from_another_harness_cannot_authorize_delivery(self):
        message = append_message(self.directory, 'wrong harness', {}, delivery='steer', target_session=self.native_id)
        self.clear()
        props = read(self.directory / 'properties.json')
        write(self.directory / 'properties.json', {**props, 'harness': 'pi'})
        with patch.object(self.adapter, 'get_delivery') as check:
            for checking in (False, True):
                result = native.deliver(self.root, self.agent, message['id'], check=checking)
                self.assertFalse(result['ok'])
            check.assert_not_called()
        self.assertEqual(self.calls, [])


def load_tests(loader, tests, pattern):
    # Reuse the fixture, not the parent's test cases.
    suite = unittest.TestSuite()
    for name in SessionDeliveryTest.__dict__:
        if name.startswith('test_'):
            suite.addTest(SessionDeliveryTest(name))
    return suite
