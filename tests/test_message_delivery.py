"""Durable delivery evidence and automatic inbox read positions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.runtime import native
from multiverse.harnesses.types import NativeResult
from multiverse.universe.mailbox import append_message, inbox, message_record, update_message
from multiverse.store import read, write
from multiverse.tools import send, deliver
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
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
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

    def send(self, text=None, **kwargs):
        text = text if text is not None else str(uuid4())
        result = send.run(self.root, agent=self.agent, text=text, **kwargs)
        # Internal delivery tests recover IDs from raw history, never tool output.
        self.assertNotIn('id', result)
        record = next(m for m in inbox(self.directory, limit=None, all_messages=True)['messages'] if m['text'] == text)
        return {**result, 'id': record['id']}

    def test_modes_are_isolated_and_history_preserves_receipts(self):
        normal = self.send(delivery='follow-up')
        urgent = self.send(delivery='steer')
        manual = self.send(delivery='inbox-only')
        self.assertEqual([m['id'] for m in inbox(self.directory)['messages']], [manual['id']])
        history = inbox(self.directory, all_messages=True)['messages']
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]['tracking']['submission_state'], 'accepted')
        self.assertNotIn('acknowledged_at', history[0]['tracking'])
        self.assertEqual([call[2] for call in self.calls], ['follow-up', 'steer'])
        self.assertFalse(hasattr(self.calls[1][1], 'reply_to'))
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
            message_record(self.directory, str(uuid4()))
        write(self.directory / 'properties.json', props)
        write(self.directory / 'runtime.json', {'launch_id': 'resumed', 'state': 'running'})
        self.behavior = NativeResult('native_queued')
        result = native.deliver(self.root, self.agent, message['id'], retry=True)
        self.assertEqual(result['native_session_id'], self.native_id)
        self.assertEqual(result['launch_id'], 'resumed')

    def test_reconciliation_confirms_without_resubmitting(self):
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
        old = {'id': 'legacy', 'text': 'old text', 'delivery': 'steer', 'reply_to': 'old-parent'}
        path = self.directory / 'inbox.jsonl'
        path.write_text(json.dumps(old) + '\n')
        before = path.read_bytes()
        write(self.directory / 'inbox-state.json', {'legacy': {'acknowledged_at': 'old-time'}})
        self.assertEqual(inbox(self.directory)['messages'], [old])
        self.assertEqual(message_record(self.directory, 'legacy')[1], {})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(inbox(self.directory)['messages'], [])

    def test_read_during_submission_keeps_receipts_and_read_position(self):
        manual = self.send(delivery='inbox-only')
        original = native.update_message
        def update(directory, message_id, **changes):
            if changes.get('submission_state') == 'accepted':
                self.assertEqual([m['id'] for m in inbox(directory)['messages']], [manual['id']])
            return original(directory, message_id, **changes)
        with patch.object(native, 'update_message', side_effect=update):
            message = self.send()
        tracking = message_record(self.directory, message['id'])[1]
        self.assertEqual(inbox(self.directory)['messages'], [])
        self.assertNotIn('acknowledged_at', tracking)
        self.assertEqual(tracking['submission_state'], 'accepted')
        self.assertEqual(tracking['native_receipt'], {'receipt': 'native'})

    def test_old_read_marker_does_not_block_delivery_and_is_discarded_on_update(self):
        message = append_message(self.directory, 'legacy failure', {}, delivery='steer')
        write(self.directory / 'inbox-state.json', {message['id']: {
            'acknowledged_at': 'old-time', 'submission_state': 'failed',
            'attempts': [{'state': 'error', 'reason': 'failed before submission'}]}})
        self.assertEqual(native.deliver(self.root, self.agent, message['id'], retry=True)['state'], 'native_queued')
        tracking = read(self.directory / 'inbox-state.json')[message['id']]
        self.assertNotIn('acknowledged_at', tracking)
        self.assertEqual(len(tracking['attempts']), 2)

    def test_concurrent_readers_consume_each_manual_message_once(self):
        for index in range(25):
            append_message(self.directory, str(index), {})
        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(lambda _: inbox(self.directory, limit=1), range(30)))
        self.assertEqual(sorted(int(m['text']) for batch in batches for m in batch['messages']), list(range(25)))

    def test_recipient_recovery_selects_only_eligible_messages_and_hides_ids(self):
        accepted = self.send()
        self.behavior = NativeResult('error', 'definite failure')
        failed = self.send()
        self.behavior = TimeoutError('uncertain')
        unknown = self.send()
        pending = append_message(self.directory, 'pending', {}, delivery='follow-up')
        self.send(delivery='inbox-only')
        checked = []
        def check(session, message_id, executor):
            checked.append(message_id)
            return NativeResult('native_queued', value={'message_id': message_id, 'transcript_confirmed': True})
        adapter = harnesses.load(self.root, 'claude')
        with patch.object(adapter, 'get_delivery', side_effect=check):
            result = deliver.run(self.root, agent=self.agent, check=True)
        self.assertEqual(checked, [accepted['id'], unknown['id'], pending['id']])
        self.assertFalse(result['ok'])  # The definite failure remains visible, without a native check.
        self.assertEqual(len(result['messages']), 4)
        self.assertEqual(sum(m.get('transcript_confirmed') is True for m in result['messages']), 3)
        self.assertEqual([m['reason'] for m in result['messages'] if m.get('ok') is False], ['definite failure'])
        for message in (accepted, failed, unknown, pending):
            self.assertNotIn(message['id'], json.dumps(result))
        before = len(self.calls)
        self.behavior = NativeResult('native_queued')
        retried = deliver.run(self.root, agent=self.agent, retry=True)
        self.assertTrue(retried['ok'])
        self.assertEqual(len(self.calls), before + 1)
        self.assertEqual(self.calls[-1][1].id, failed['id'])
        self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
        self.assertEqual(len(inbox(self.directory)['messages']), 1)

    def test_check_records_receipts_for_accepted_messages_of_the_current_launch(self):
        self.behavior = NativeResult('native_queued')  # accepted, no receipt yet
        older = self.send()
        update_message(self.directory, older['id'], attempts=[{'state': 'native_queued', 'launch_id': 'earlier'}])
        current = self.send()
        missing = self.send()
        checked = []
        def check(session, message_id, executor):
            checked.append(message_id)
            if message_id == missing['id']:
                return NativeResult('unknown', 'No receipt yet')
            return NativeResult('native_queued', value={'transcript_confirmed': True})
        adapter = harnesses.load(self.root, 'claude')
        with patch.object(adapter, 'get_delivery', side_effect=check):
            first = deliver.run(self.root, agent=self.agent, check=True)
            second = deliver.run(self.root, agent=self.agent, check=True)
        # Missing evidence for an accepted message is not a failure, and is asked again.
        self.assertTrue(first['ok'] and second['ok'])
        self.assertEqual(checked, [current['id'], missing['id'], missing['id']])
        tracking = inbox(self.directory, all_messages=True, limit=None)['messages']
        confirmed = {m['id']: m['tracking'].get('transcript_confirmed_at') for m in tracking}
        self.assertTrue(confirmed[current['id']])
        self.assertIsNone(confirmed[older['id']])

    def test_recipient_retry_covers_history_beyond_the_default_inbox_page(self):
        for index in range(55):
            message = append_message(self.directory, str(index), {}, delivery='steer')
            update_message(self.directory, message['id'], submission_state='failed')
        result = deliver.run(self.root, agent=self.agent, retry=True)
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['messages']), 55)
        self.assertEqual(len(self.calls), 55)

    def test_check_reports_an_accepted_message_still_waiting_in_the_native_queue(self):
        self.behavior = NativeResult('native_queued', 'Accepted by native after-turn queue',
                                    {'queued_submission_id': 'native-queue-entry'})
        message = self.send(delivery='follow-up')
        self.behavior = NativeResult('native_queued', 'Message remains in native queue',
                                    {'queued_submission_id': 'native-queue-entry', 'transcript_confirmed': False})
        presented = []
        for _ in range(2):
            result = deliver.run(self.root, agent=self.agent, check=True, _present=presented.append)
            self.assertEqual(len(result['messages']), 1)
            self.assertEqual(result['messages'][0]['state'], 'native_queued')
            self.assertFalse(result['messages'][0]['transcript_confirmed'])
        self.assertTrue(all('message is queued for its next turn' in line for line in presented))
        self.assertEqual(len(self.calls), 1, 'Checking a waiting entry must never send another copy')
        self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
        self.assertEqual(len(self.calls), 1)
        self.behavior = NativeResult('native_queued', 'Message observed in native thread history',
                                    {'transcript_confirmed': True})
        self.assertEqual(len(deliver.run(self.root, agent=self.agent, check=True)['messages']), 1)
        self.assertEqual(deliver.run(self.root, agent=self.agent, check=True)['messages'], [])
