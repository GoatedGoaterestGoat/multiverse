"""Damaged bookkeeping never authorizes resubmission of uncertain work."""
from unittest.mock import patch
from multiverse import harnesses
from multiverse.harnesses.types import NativeResult
from multiverse.runtime import native
from multiverse.store import uid, write
from multiverse.tools import send, deliver, doctor
from multiverse.universe import mailbox
from test_multiverse import BaseTest


class MailboxDamageTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state' / 'agents' / uid()
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'recipient',
            'harness': 'claude', 'workspace': str(self.work), 'native_session_id': uid()})
        write(self.directory / 'runtime.json', {'launch_id': uid(), 'state': 'running',
            'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}})
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.calls = []
        owner = self
        class Adapter:
            def send(self, session, message, executor):
                owner.calls.append(message.id)
                return NativeResult('native_queued', value={'client_user_message_id': message.id})
            def get_delivery(self, session, message_id, executor):
                return NativeResult('unknown', 'No receipt')
            def get_usage(self, session, executor):
                return NativeResult('unavailable')
        self.enterContext(patch.object(harnesses, 'load', return_value=Adapter()))

    def test_new_messages_are_saved_named_and_retryable_after_state_repair(self):
        path = self.directory / 'inbox-state.json'
        for damaged in ('[]', '{"trunc\n', '', '{"message":null}', '{"message":{"attempts":5}}'):
            with self.subTest(damaged=damaged):
                path.write_text(damaged)
                result = send.run(self.root, agent=self.directory.name, text='new ' + repr(damaged))
                self.assertTrue(result['saved'])
                self.assertFalse(result['ok'])
                self.assertIn('inbox-state.json', result['native_delivery']['reason'])
                self.assertEqual(self.calls, [])
        path.unlink()
        history = mailbox.inbox(self.directory, all_messages=True)['messages']
        self.assertEqual(len(history), 5)
        self.assertEqual({m['tracking']['submission_state'] for m in history}, {'failed'})
        retried = deliver.run(self.root, agent=self.directory.name, retry=True)
        self.assertTrue(retried['ok'])
        self.assertEqual(len(self.calls), 5)
        path.unlink()
        deliver.run(self.root, agent=self.directory.name, retry=True)
        self.assertEqual(len(self.calls), 5)

    def test_repair_does_not_reclassify_older_uncertain_or_accepted_messages(self):
        accepted = mailbox.append_message(self.directory, 'old accepted', {}, delivery='steer')
        mailbox.update_message(self.directory, accepted['id'], submission_state='accepted')
        uncertain = mailbox.append_message(self.directory, 'old uncertain', {}, delivery='steer')
        mailbox.update_message(self.directory, uncertain['id'], submission_state='unknown')
        path = self.directory / 'inbox-state.json'
        path.write_text('{broken')
        result = send.run(self.root, agent=self.directory.name, text='definitely never submitted')
        self.assertTrue(result['saved'])
        path.unlink()
        retried = deliver.run(self.root, agent=self.directory.name, retry=True)
        self.assertEqual(len(retried['messages']), 1)
        self.assertEqual(len(self.calls), 1)

    def test_doctor_names_incomplete_tail_and_preserves_complete_messages(self):
        mailbox.append_message(self.directory, 'complete', {})
        path = self.directory / 'inbox.jsonl'
        with path.open('ab') as stream:
            stream.write(b'{"id":"truncated')
        result = doctor.run(self.root)
        self.assertFalse(result['ok'])
        self.assertTrue(any('inbox.jsonl' in item and 'incomplete' in item.lower() for item in result['problems']), result)
        self.assertEqual([m['text'] for m in mailbox.inbox(self.directory, peek=True)['messages']], ['complete'])
        with self.assertRaisesRegex(ValueError, 'Incomplete inbox tail'):
            mailbox.append_message(self.directory, 'do not overwrite', {})

    def test_retired_delivery_ledger_damage_is_named_before_any_native_call(self):
        (self.directory / 'native-delivery.json').write_text('[]')
        result = send.run(self.root, agent=self.directory.name, text='never submit through damaged legacy evidence')
        self.assertTrue(result['saved'])
        self.assertFalse(result['ok'])
        self.assertIn('native-delivery.json', result['native_delivery']['reason'])
        self.assertEqual(self.calls, [])
