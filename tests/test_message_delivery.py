"""Durable message routing, concurrent submissions and selective acknowledgements."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse import agents, harnesses, native
from multiverse.harnesses.types import NativeResult
from multiverse.store import ack, append_message, inbox, message_record, read, update_message, write
from multiverse.tools import send
from test_multiverse import BaseTest


class InboxDeliveryTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.native_id = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.agent, 'name': 'recipient', 'harness': 'claude',
            'workspace': str(self.work), 'native_session_id': self.native_id})
        write(self.directory / 'runtime.json', {'launch_id': 'launch', 'state': 'running'})
        self.enterContext(patch.object(native, 'owned'))
        self.calls = []
        self.behavior = NativeResult('native_queued', value={'receipt': 'native'})
        owner = self
        class Adapter:
            def get_delivery(self, session, message_id, executor):
                return owner.behavior
            def send(self, session, message, executor):
                owner.calls.append((session, message, message.delivery))
                if isinstance(owner.behavior, Exception):
                    raise owner.behavior
                return owner.behavior
        self.enterContext(patch.object(harnesses, 'load', return_value=Adapter()))

    def send(self, text='message', **kwargs):
        return send.run(self.root, agent=self.agent, text=text, **kwargs)

    def test_modes_are_isolated_and_history_has_receipts_not_fake_ack(self):
        normal = self.send(delivery='follow-up')
        urgent = self.send(delivery='steer', reply_to=normal['id'])
        manual = self.send(delivery='inbox-only')
        self.assertEqual([m['id'] for m in inbox(self.directory)['messages']], [manual['id']])
        history = inbox(self.directory, all_messages=True)['messages']
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]['tracking']['submission_state'], 'accepted')
        self.assertNotIn('acknowledged_at', history[0]['tracking'])
        self.assertEqual([call[2] for call in self.calls], ['follow-up', 'steer'])
        self.assertEqual(self.calls[1][1].reply_to, normal['id'])
        ack(self.directory, message_ids=[urgent['id']])
        self.assertIn('acknowledged_at', message_record(self.directory, urgent['id'])[1])
        self.assertNotIn('acknowledged_at', message_record(self.directory, normal['id'])[1])
        ack(self.directory, inbox(self.directory)['cursor'])
        self.assertNotIn('acknowledged_at', message_record(self.directory, normal['id'])[1])
        self.assertEqual(inbox(self.directory)['messages'], [])

    def test_concurrent_senders_have_unique_ids_and_preserve_individual_modes(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            messages = list(pool.map(lambda i: self.send(str(i), delivery='steer' if i % 2 else 'follow-up'), range(24)))
        self.assertEqual(len({m['id'] for m in messages}), 24)
        self.assertEqual(len(self.calls), 24)
        self.assertEqual(len(inbox(self.directory, all_messages=True)['messages']), 24)
        for _, message, mode in self.calls:
            self.assertEqual(mode, 'steer' if int(message.text) % 2 else 'follow-up')
        self.assertEqual([m['id'] for m in inbox(self.directory, all_messages=True)['messages']],
                         [call[1].id for call in self.calls])
        # Concurrent duplicate deliver commands cannot submit a second copy.
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: native.deliver(self.root, self.agent, messages[0]['id'], retry=True), range(16)))
        self.assertTrue(all(r['reused'] for r in results))
        self.assertEqual(len(self.calls), 24)

    def test_definite_failure_retry_and_unknown_outcome_are_distinct(self):
        self.behavior = NativeResult('error', 'Rejected before acceptance')
        rejected = self.send()
        self.assertEqual(message_record(self.directory, rejected['id'])[1]['submission_state'], 'failed')
        self.behavior = NativeResult('native_queued')
        self.assertTrue(native.deliver(self.root, self.agent, rejected['id'])['reused'])
        self.assertEqual(native.deliver(self.root, self.agent, rejected['id'], retry=True)['state'], 'native_queued')
        self.behavior = TimeoutError('connection lost')
        uncertain = self.send()
        self.assertEqual(uncertain['state'], 'unknown')
        before = len(self.calls)
        self.behavior = NativeResult('native_queued')
        self.assertEqual(native.deliver(self.root, self.agent, uncertain['id'], retry=True)['state'], 'unknown')
        self.assertEqual(len(self.calls), before)
        self.assertEqual(inbox(self.directory)['messages'], [])

    def test_crash_during_submission_never_replays_automatically(self):
        message = append_message(self.directory, 'crash', {}, delivery='steer', target_session=self.native_id)
        update_message(self.directory, message['id'], submission_state='submitting', attempts=[{'state': 'unknown'}])
        result = native.deliver(self.root, self.agent, message['id'], retry=True)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(self.calls, [])

    def test_exact_recipient_and_session_are_preserved_across_resume(self):
        self.behavior = NativeResult('error', 'before send')
        message = self.send()
        props = read(self.directory / 'properties.json')
        write(self.directory / 'properties.json', {**props, 'native_session_id': str(uuid4())})
        with self.assertRaisesRegex(ValueError, 'different native session'):
            native.deliver(self.root, self.agent, message['id'], retry=True)
        with self.assertRaisesRegex(ValueError, 'recipient inbox'):
            ack(self.directory, message_ids=[str(uuid4())])
        write(self.directory / 'properties.json', props)
        write(self.directory / 'runtime.json', {'launch_id': 'resumed', 'state': 'running'})
        self.behavior = NativeResult('native_queued')
        result = native.deliver(self.root, self.agent, message['id'], retry=True)
        self.assertEqual(result['native_session_id'], self.native_id)
        self.assertEqual(result['launch_id'], 'resumed')

    def test_reconciliation_confirms_without_resubmitting_or_acknowledging(self):
        self.behavior = TimeoutError('lost response')
        message = self.send()
        self.behavior = NativeResult('native_queued', value={'transcript_confirmed': True})
        result = native.deliver(self.root, self.agent, message['id'], check=True)
        self.assertEqual(result['state'], 'native_queued')
        state = message_record(self.directory, message['id'])[1]
        self.assertEqual(state['submission_state'], 'accepted')
        self.assertIn('transcript_confirmed_at', state)
        self.assertNotIn('acknowledged_at', state)
        self.assertEqual(native.deliver(self.root, self.agent, message['id'], retry=True)['state'], 'native_queued')
        self.assertEqual(len(self.calls), 1)

    def test_legacy_native_receipts_prevent_duplicate_recovery(self):
        message = append_message(self.directory, 'old accepted', {}, delivery='steer')
        write(self.directory / 'native-delivery.json', {message['id']: [{'state': 'native_queued',
            'native_session_id': self.native_id}]})
        self.assertTrue(native.deliver(self.root, self.agent, message['id'], retry=True)['reused'])
        self.assertEqual(self.calls, [])

    def test_bad_configuration_failure_is_saved_in_delivery_history(self):
        with patch.object(harnesses, 'load', side_effect=ValueError('malformed settings')):
            message = self.send()
        self.assertFalse(message['ok'])
        tracking = message_record(self.directory, message['id'])[1]
        self.assertEqual(tracking['submission_state'], 'failed')
        self.assertIn('malformed settings', tracking['attempts'][-1]['reason'])

    def test_old_inbox_records_keep_manual_semantics_without_rewriting(self):
        old = {'id': 'legacy', 'text': 'old text', 'delivery': 'steer'}
        path = self.directory / 'inbox.jsonl'
        path.write_text(json.dumps(old) + '\n')
        before = path.read_bytes()
        self.assertEqual(inbox(self.directory)['messages'], [old])
        ack(self.directory, inbox(self.directory)['cursor'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(inbox(self.directory)['messages'], [])

    def test_ack_during_submission_keeps_receipt_and_ack(self):
        original = native.update_message
        def update(directory, message_id, **changes):
            if changes.get('submission_state') == 'accepted':
                ack(directory, message_ids=[message_id])
            return original(directory, message_id, **changes)
        with patch.object(native, 'update_message', side_effect=update):
            message = self.send()
        tracking = message_record(self.directory, message['id'])[1]
        self.assertIn('acknowledged_at', tracking)
        self.assertEqual(tracking['submission_state'], 'accepted')
        self.assertEqual(tracking['native_receipt'], {'receipt': 'native'})
