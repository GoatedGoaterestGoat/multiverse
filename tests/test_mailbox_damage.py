"""Damaged bookkeeping never authorizes resubmission of uncertain work."""
from unittest.mock import patch
from multiverse import harnesses
from multiverse.harnesses.types import NativeResult
from multiverse.runtime import native
from multiverse.store import read, uid, write
from multiverse.tools import send, deliver, doctor, inbox
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

    def test_damaged_delivery_bookkeeping_saves_new_messages_as_failed(self):
        """A damaged inbox-state.json or retired ledger saves a new message as failed, named, with no native call, and
        after repair --retry resends only those; fragile because the classification is rebuilt from the damaged
        file, and a wrong rebuild double-sends with no error (14bc474, abac381)."""
        accepted = mailbox.append_message(self.directory, 'old accepted', {}, delivery='steer')
        mailbox.update_message(self.directory, accepted['id'], submission_state='accepted')
        uncertain = mailbox.append_message(self.directory, 'old uncertain', {}, delivery='steer')
        mailbox.update_message(self.directory, uncertain['id'], submission_state='unknown')
        path = self.directory / 'inbox-state.json'
        damage = ('[]', '{"trunc\n', '', '{"message":null}', '{"message":{"attempts":5}}')
        for damaged in damage:
            with self.subTest(damaged=damaged):
                path.write_text(damaged)
                result = send.run(self.root, agent=self.directory.name, text='new ' + repr(damaged))
                self.assertTrue(result['saved'])
                self.assertFalse(result['ok'])
                self.assertIn(str(path), result['native_delivery']['reason'])
                self.assertEqual(self.calls, [])
                for options in ({'all_messages': True}, {'peek': True}):
                    with self.assertRaises(ValueError) as caught:
                        inbox.run(self.root, agent=self.directory.name, **options)
                    self.assertIn(str(path), str(caught.exception))
                    self.assertIn('repair', str(caught.exception).lower())
                    self.assertIn('reset', str(caught.exception).lower())
        path.unlink()
        history = mailbox.inbox(self.directory, all_messages=True)['messages'][2:]
        self.assertEqual(len(history), len(damage))
        self.assertEqual({m['tracking']['submission_state'] for m in history}, {'failed'})
        checked = deliver.run(self.root, agent=self.directory.name, check=True)['messages']
        self.assertEqual({m['state'] for m in checked if m['text'].startswith('new ')}, {'error'})  # never uncertain
        retried = deliver.run(self.root, agent=self.directory.name, retry=True)
        self.assertTrue(retried['ok'])
        self.assertEqual(len(retried['messages']), len(damage))
        self.assertEqual(self.calls, [m['id'] for m in history])
        path.unlink()
        deliver.run(self.root, agent=self.directory.name, retry=True)
        self.assertEqual(len(self.calls), len(damage))
        (self.directory / 'native-delivery.json').write_text('[]')
        result = send.run(self.root, agent=self.directory.name, text='never submit through damaged legacy evidence')
        self.assertTrue(result['saved'])
        self.assertFalse(result['ok'])
        self.assertIn('native-delivery.json', result['native_delivery']['reason'])
        self.assertEqual(len(self.calls), len(damage))

    def test_doctor_names_incomplete_tail_and_preserves_complete_messages(self):
        """A torn inbox.jsonl tail is reported and nothing is appended after it; fragile because only a crash
        mid-write makes that state, and appending after a partial line silently corrupts the next message."""
        mailbox.append_message(self.directory, 'complete', {})
        path = self.directory / 'inbox.jsonl'
        with path.open('ab') as stream:
            stream.write(b'{"id":"truncated')
        result = doctor.run(self.root)
        self.assertFalse(result['ok'])
        self.assertTrue(any('inbox.jsonl' in item and 'incomplete' in item.lower() for item in result['problems']), result)
        self.assertEqual([m['text'] for m in mailbox.inbox(self.directory, peek=True)['messages']], ['complete'])
        batch = mailbox.inbox(self.directory)
        self.assertEqual([m['text'] for m in batch['messages']], ['complete'])
        self.assertEqual(read(self.directory / 'inbox.cursor'), batch['cursor'])
        self.assertEqual(mailbox.inbox(self.directory)['messages'], [])
        with self.assertRaisesRegex(ValueError, 'Incomplete inbox tail'):
            mailbox.append_message(self.directory, 'do not overwrite', {})
