"""Durable delivery evidence and automatic inbox read positions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import threading
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.runtime import native, attachments
from multiverse.harnesses.types import NativeResult
from multiverse.harnesses.claude import ClaudeAdapter
from multiverse.universe.mailbox import append_message, inbox, message_record, update_message
from multiverse.store import read, write, now
from multiverse.tools import send, deliver
from multiverse.tools import inbox as inbox_tool
from test_multiverse import BaseTest


class DeliveryFixture(BaseTest):
    """A running Claude recipient whose adapter records each send and answers with self.behavior. It holds no tests,
    so a module that builds on it runs only its own."""

    def setUp(self):
        super().setUp()
        self.terminal = {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
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
        self.recipient()

    def recipient(self, **properties):
        """A new recipient with its own ids and no recorded sends, so each case of a table starts clean."""
        self.agent = str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.native_id = str(uuid4())
        self.launch = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.agent, 'name': f'recipient-{self.agent[:8]}',
            'harness': 'claude', 'workspace': str(self.work), 'native_session_id': self.native_id, **properties})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'tmux': self.terminal})
        self.calls = []
        self.behavior = NativeResult('native_queued', value={'receipt': 'native'})

    def send(self, text=None, **kwargs):
        text = text if text is not None else str(uuid4())
        result = send.run(self.root, agent=self.agent, text=text, **kwargs)
        # Internal delivery tests recover IDs from raw history, never tool output.
        self.assertNotIn('id', result)
        record = next(m for m in inbox(self.directory, limit=None, all_messages=True)['messages'] if m['text'] == text)
        return {**result, 'id': record['id']}


class InboxDeliveryTest(DeliveryFixture):
    def test_concurrent_senders_have_unique_ids_and_preserve_individual_modes(self):
        """24 concurrent senders get unique ids and keep their own modes, and 16 concurrent deliver --retry calls
        submit once; fragile because correctness rests on file locks, and races show only as rare silent
        duplicates; cross-universe sender labels must also survive framing or identically named agents silently
        become indistinguishable to the recipient."""
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
        origin = self.work / 'origin-universe'
        write(origin / 'universe.json', {'name': 'review'})
        for scope, suffix in ((self.root, ''), (origin, ' (in universe review)')):
            with self.subTest(sender_universe=str(scope)):
                self.send('sender context', sender={'name': 'auditor', 'universe': str(scope)})
                self.assertEqual(self.calls[-1][1].framed, f'Message from auditor{suffix}:\nsender context')

    def test_definite_failure_is_retryable_and_unknown_never_is(self):
        """A definite failure (rejected, or a harness load error) is saved and retryable, while unknown or submitting
        never is; fragile because this classification decides whether a retry double-sends, and a misclassified
        timeout looks like a normal failure."""
        self.behavior = NativeResult('error', 'Rejected before acceptance')
        rejected = self.send()
        self.assertEqual(message_record(self.directory, rejected['id'])[1]['submission_state'], 'failed')
        self.behavior = NativeResult('native_queued')
        self.assertTrue(native.deliver(self.root, self.agent, rejected['id'])['reused'])
        self.assertEqual(native.deliver(self.root, self.agent, rejected['id'], retry=True)['state'], 'native_queued')
        with patch.object(harnesses, 'load', side_effect=ValueError('malformed settings')):
            misconfigured = self.send()
        self.assertFalse(misconfigured['ok'])
        tracking = message_record(self.directory, misconfigured['id'])[1]
        self.assertEqual(tracking['submission_state'], 'failed')
        self.assertIn('malformed settings', tracking['attempts'][-1]['reason'])
        retried = native.deliver(self.root, self.agent, misconfigured['id'], retry=True)
        self.assertEqual(retried['state'], 'native_queued')
        self.behavior = TimeoutError('connection lost')
        uncertain = self.send()
        self.assertEqual(uncertain['state'], 'unknown')
        crashed = append_message(self.directory, 'crash', {}, delivery='steer', target_session=self.native_id)
        update_message(self.directory, crashed['id'], submission_state='submitting', attempts=[{'state': 'unknown'}])
        before = len(self.calls)
        self.behavior = NativeResult('native_queued')
        for message in (uncertain, crashed):
            with self.subTest(message=message['text']):
                self.assertEqual(native.deliver(self.root, self.agent, message['id'], retry=True)['state'], 'unknown')
        self.assertEqual(len(self.calls), before)
        self.assertEqual(inbox(self.directory)['messages'], [])

    def test_check_and_retry_select_eligible_messages_across_the_whole_history(self):
        """deliver --check selects accepted, unknown and pending messages and --retry only definite failures, across
        the whole history, and concurrent checks claim one final cancellation report; fragile because stale
        snapshots can double-report work that kill deliberately cancelled."""
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
        for index in range(55):
            message = append_message(self.directory, str(index), {}, delivery='steer')
            update_message(self.directory, message['id'], submission_state='failed')
        result = deliver.run(self.root, agent=self.agent, retry=True)
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['messages']), 55)
        self.assertEqual(len(self.calls), before + 1 + 55)
        for staged in (False, True):
            with self.subTest(cancelled_startup=staged):
                self.recipient()
                message = append_message(self.directory, 'cancel me', {}, delivery='follow-up')
                if staged:
                    update_message(self.directory, message['id'], submission_state='pending',
                                   deferred_launch_id=self.launch)
                adapter = harnesses.load(self.root, 'claude')
                with patch.object(adapter, 'stop', create=True, return_value=NativeResult('ok', value={
                        'cancelled_message_ids': [] if staged else [message['id']]})):
                    native.stop(self.root, self.agent)
                before = message_record(self.directory, message['id'])[1]
                lines = []
                # Final cancellation remains reportable even if the harness can no longer load.
                with patch.object(harnesses, 'load', side_effect=ValueError('harness removed')):
                    snapshot, barrier = deliver.mailbox.inbox, threading.Barrier(2)
                    def simultaneous_snapshot(*args, **kwargs):
                        result = snapshot(*args, **kwargs)
                        barrier.wait(timeout=5)
                        return result
                    with patch.object(deliver.mailbox, 'inbox', side_effect=simultaneous_snapshot), \
                            ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(lambda _: deliver.run(self.root, agent=self.agent,
                                                check=True, _present=lines.append), range(2)))
                    self.assertEqual(sorted(len(result['messages']) for result in results), [0, 1])
                    checked = next(result for result in results if result['messages'])
                    self.assertFalse(checked['ok'], checked)
                    self.assertEqual([item['state'] for item in checked['messages']], ['cancelled'])
                    self.assertEqual(sum('Send a new message' in line for line in lines), 1)
                    self.assertEqual(deliver.run(self.root, agent=self.agent, check=True)['messages'], [])
                    self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
                after = message_record(self.directory, message['id'])[1]
                self.assertEqual(after['submission_state'], 'cancelled')
                self.assertEqual(after.get('attempts'), before.get('attempts'))
                self.assertEqual(self.calls, [])

    def test_an_ended_acceptance_needs_exit_and_same_idle_conversation_proof(self):
        """Lost acceptances need independent exit and exact idle-session evidence; guessing from a new launch can
        invite a duplicate resend, while hiding the old acceptance silently loses a message (LC-30)."""
        cases = ('lost', 'confirmed', 'unknown', 'legacy-lost', 'legacy-match', 'current-launch',
                 'working', 'unready', 'other-session', 'other-home', 'no-cleanup', 'live-child',
                 'unknown-process', 'legacy-no-home', 'legacy-unreadable', 'concurrent-checks')
        for case in cases:
            with self.subTest(case=case):
                self.recipient(native_session_home=str(self.work / 'claude-home'))
                old = self.launch
                message = self.send('a message accepted before stopping', sender={'name': 'old-name'})
                _, tracking = message_record(self.directory, message['id'])
                context = tracking.get('delivery_context')
                if case in ('lost', 'confirmed'):
                    self.assertEqual(context['text'], self.calls[0][1].framed)
                legacy = case.startswith('legacy-')
                if legacy:
                    update_message(self.directory, message['id'], delivery_context=None)
                home = str(self.work / 'claude-home')
                write(self.directory / 'launches' / old / 'request.json', {
                    'env': {'CLAUDE_CONFIG_DIR': home, 'HOME': str(self.work)},
                    'unset_env': []})
                cleanup = self.directory / 'launches' / old / 'cleanup.json'
                write(cleanup, {'schema': 1, 'launch_id': old, 'state': 'complete',
                               'pid': 990001, 'child_pid': 990002, 'at': now()})
                if case == 'no-cleanup':
                    cleanup.unlink()
                if case == 'legacy-no-home':
                    write(self.directory / 'launches' / old / 'request.json',
                          {'env': {}, 'unset_env': ['CLAUDE_CONFIG_DIR']})
                current = old if case == 'current-launch' else str(uuid4())
                write(self.directory / 'runtime.json', {'launch_id': current, 'state': 'running',
                    'status': 'working' if case == 'working' else 'idle', 'pid': 990003,
                    'startup_checked': case != 'unready', 'tmux': self.terminal})
                if case in ('other-session', 'other-home'):
                    properties = read(self.directory / 'properties.json')
                    properties['native_session_id' if case == 'other-session' else 'native_session_home'] = (
                        str(uuid4()) if case == 'other-session' else str(self.work / 'elsewhere'))
                    write(self.directory / 'properties.json', properties)
                    if case == 'other-session':
                        # An authorized lineage change still cannot prove loss from another session.
                        self.enterContext(patch.object(native, '_session_changes', return_value=True))
                answer = (NativeResult('unknown', 'storage unreadable') if case in ('unknown', 'legacy-match', 'legacy-unreadable')
                          else NativeResult('ok', value={'delivered': case == 'confirmed'}))
                adapter = harnesses.load(self.root, 'claude')
                adapter.storage_env = ('CLAUDE_CONFIG_DIR',)
                self.behavior = NativeResult('unknown', 'No receipt yet')
                observations = ((False, ('Sun Oct 18 23:07:11 2026', 'S')) if case == 'live-child'
                                else (False, None) if case == 'unknown-process' else (True, None))
                lines = []
                actual = (ClaudeAdapter.configured('claude', {}).accepted_message_evidence
                          if case in ('lost', 'legacy-lost', 'legacy-unreadable') else None)
                blocked = Path(home) / 'projects' / 'unrelated-project' / (self.native_id + '.jsonl')
                if case == 'legacy-unreadable':
                    blocked.parent.mkdir(parents=True, exist_ok=True)
                original_stat = Path.stat
                def stat(path, *args, **kwargs):
                    if case == 'legacy-unreadable' and path == blocked:
                        raise PermissionError('legacy workspace unknown; this folder may hold the transcript')
                    return original_stat(path, *args, **kwargs)
                with patch.object(adapter, 'accepted_message_evidence', return_value=answer,
                                  side_effect=actual, create=True) as evidence, \
                        patch.object(adapter, 'session_home', side_effect=lambda env: env.get('CLAUDE_CONFIG_DIR'), create=True), \
                        patch.object(attachments, '_observation', return_value=observations), patch.object(Path, 'stat', stat):
                    if case == 'concurrent-checks':
                        snapshot = deliver.mailbox.inbox
                        barrier = threading.Barrier(2)
                        def simultaneous(*args, **kwargs):
                            result = snapshot(*args, **kwargs)
                            barrier.wait(timeout=5)
                            return result
                        with patch.object(deliver.mailbox, 'inbox', side_effect=simultaneous), ThreadPoolExecutor(2) as pool:
                            results = list(pool.map(lambda _: deliver.run(self.root, agent=self.agent,
                                                    check=True, _present=lines.append), range(2)))
                        self.assertEqual(sorted(len(item['messages']) for item in results), [0, 1])
                        checked = next(item for item in results if item['messages'])
                    else:
                        checked = deliver.run(self.root, agent=self.agent, check=True, _present=lines.append)
                    queried = case in ('lost', 'confirmed', 'unknown', 'legacy-lost', 'legacy-match', 'legacy-unreadable', 'concurrent-checks')
                    self.assertEqual(evidence.call_count, int(queried))
                    if queried:
                        self.assertEqual(evidence.call_args.kwargs['framed'], not legacy)
                        args = evidence.call_args.args
                        self.assertEqual((args[0].native_id, args[0].workspace, args[1], args[4]),
                                         (self.native_id, '' if legacy else str(self.work), message['id'], home))
                        self.assertEqual(args[2], message['text'] if legacy else self.calls[0][1].framed)
                    final = case in ('lost', 'legacy-lost', 'concurrent-checks')
                    self.assertEqual(checked['ok'], not final)
                    state = message_record(self.directory, message['id'])[1]
                    self.assertEqual(state['submission_state'], 'cancelled' if final else 'accepted')
                    if final:
                        self.assertEqual(state['final_reason'], 'lost')
                        self.assertTrue(any('lost when' in line and 'draft' in line for line in lines))
                        self.assertEqual(deliver.run(self.root, agent=self.agent, check=True)['messages'], [])
                    elif case == 'confirmed':
                        self.assertTrue(state['native_receipt']['transcript_confirmed'])
                    self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
                self.assertEqual(len(self.calls), 1, 'No reconciliation or retry can replay an accepted message')

    def test_check_confirms_without_resending(self):
        """deliver --check keeps unconfirmed acceptances across launches, treats a still-queued native entry as
        accepted, reports failed messages, and never resends; fragile because check and retry share code paths, and
        a resend duplicates a prompt in the conversation (d292f88)."""
        with self.subTest('an uncertain submission is confirmed'):
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
        with self.subTest('an unconfirmed acceptance cannot disappear when its launch is replaced'):
            self.recipient()
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
            self.assertEqual(checked, [older['id'], current['id'], missing['id'], missing['id']])
            tracking = inbox(self.directory, all_messages=True, limit=None)['messages']
            confirmed = {m['id']: m['tracking'].get('transcript_confirmed_at') for m in tracking}
            self.assertTrue(confirmed[current['id']])
            self.assertTrue(confirmed[older['id']])
            self.assertEqual(len(self.calls), 3)
        with self.subTest('an entry still waiting in the native queue'):
            self.recipient()
            self.behavior = NativeResult('native_queued', 'Accepted by native after-turn queue',
                                         {'queued_submission_id': 'native-queue-entry'})
            message = self.send(delivery='follow-up')
            self.behavior = NativeResult('native_queued', 'Message remains in native queue',
                                         {'queued_submission_id': 'native-queue-entry', 'transcript_confirmed': False})
            for _ in range(2):
                result = deliver.run(self.root, agent=self.agent, check=True)
                self.assertEqual(len(result['messages']), 1)
                self.assertEqual(result['messages'][0]['state'], 'native_queued')
                self.assertFalse(result['messages'][0]['transcript_confirmed'])
            self.assertEqual(len(self.calls), 1, 'Checking a waiting entry must never send another copy')
            self.assertEqual(deliver.run(self.root, agent=self.agent, retry=True)['messages'], [])
            self.assertEqual(len(self.calls), 1)
            self.behavior = NativeResult('native_queued', 'Message observed in native thread history',
                                         {'transcript_confirmed': True})
            self.assertEqual(len(deliver.run(self.root, agent=self.agent, check=True)['messages']), 1)
            self.assertEqual(deliver.run(self.root, agent=self.agent, check=True)['messages'], [])
        with self.subTest('a failed message is reported, never reconciled or resent'):
            self.recipient()
            message = append_message(self.directory, 'refused', {}, delivery='steer')
            update_message(self.directory, message['id'], submission_state='failed',
                attempts=[{'state': 'error', 'ok': False, 'reason': 'Harness startup refused',
                           'value': {'wake_refused': True}}])
            with patch.object(native, 'deliver', side_effect=AssertionError('failed receipt is already definite')), \
                    patch.object(native, 'send_or_wake', side_effect=AssertionError('check must not retry')):
                result = deliver.run(self.root, agent=self.agent, check=True)
            self.assertFalse(result['ok'], result)
            self.assertEqual(len(result['messages']), 1)
            self.assertIn('Harness startup refused', result['messages'][0]['reason'])

    def test_read_cursor_and_receipts_survive_each_other(self):
        """Inbox reads and delivery receipts, which both write inbox-state.json, never clobber each other under
        concurrency; a break silently drops a receipt or shows a message twice."""
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
        for index in range(25):
            append_message(self.directory, str(index), {})
        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(lambda _: inbox(self.directory, limit=1), range(30)))
        self.assertEqual(sorted(int(m['text']) for batch in batches for m in batch['messages']), list(range(25)))

    def test_old_inbox_and_receipt_formats_keep_their_meaning(self):
        """Old native-delivery.json receipts, old inbox lines and old acknowledged_at markers keep their meaning and
        are not rewritten; fragile because they exist only in universes from older mv2, which no new code path
        writes."""
        with self.subTest('an old native receipt'):
            message = append_message(self.directory, 'old accepted', {}, delivery='steer')
            write(self.directory / 'native-delivery.json', {message['id']: [{'state': 'native_queued',
                'native_session_id': self.native_id}]})
            self.assertTrue(native.deliver(self.root, self.agent, message['id'], retry=True)['reused'])
            self.assertEqual(self.calls, [])
        with self.subTest('old inbox lines with reply references and read markers'):
            self.recipient()
            old = [{'id': 'legacy', 'text': 'old text', 'delivery': 'steer', 'reply_to': 'old-parent'},
                   {'id': 'older', 'reply_to': 'old-parent', 'text': 'still readable', 'from': {'name': 'reviewer'}}]
            path = self.directory / 'inbox.jsonl'
            path.write_text(''.join(json.dumps(line) + '\n' for line in old))
            before = path.read_bytes()
            write(self.directory / 'inbox-state.json', {line['id']: {'acknowledged_at': 'old-time'} for line in old})
            self.assertEqual(inbox(self.directory, peek=True)['messages'], old)
            for line in old:
                self.assertEqual(message_record(self.directory, line['id'])[1], {})
            history = inbox_tool.run(self.root, agent=self.agent, all_messages=True)
            self.assertEqual([message['id'] for message in history['messages']], ['legacy', 'older'])
            batch = inbox_tool.run(self.root, agent=self.agent)
            self.assertEqual(batch['messages'], [{'text': 'old text', 'delivery': 'steer'},
                                                 {'from': {'name': 'reviewer'}, 'text': 'still readable'}])
            for internal in ('legacy', 'older', 'old-parent'):
                self.assertNotIn(internal, json.dumps(batch))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(inbox(self.directory)['messages'], [])
        with self.subTest('an old read marker on a failed message'):
            self.recipient()
            message = append_message(self.directory, 'legacy failure', {}, delivery='steer')
            write(self.directory / 'inbox-state.json', {message['id']: {
                'acknowledged_at': 'old-time', 'submission_state': 'failed',
                'attempts': [{'state': 'error', 'reason': 'failed before submission'}]}})
            self.assertEqual(native.deliver(self.root, self.agent, message['id'], retry=True)['state'], 'native_queued')
            tracking = read(self.directory / 'inbox-state.json')[message['id']]
            self.assertNotIn('acknowledged_at', tracking)
            self.assertEqual(len(tracking['attempts']), 2)
