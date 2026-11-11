"""Queue observations and the sender's read-first check."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
import io
import json
import os
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import harnesses, store
from multiverse.harnesses.types import Capability, NativeResult
from multiverse.runtime import native, queued_prompts
from multiverse.tools import send
from multiverse.universe import agents, mailbox


class QueueTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directories = {}
        for name in ('a', 'b', 'c'):
            directory = self.root / 'state/agents' / str(uuid4())
            self.directories[name] = directory
            store.write(directory / 'properties.json', {
                'id': directory.name, 'name': name, 'harness': 'fixture', 'workspace': str(self.work),
                'universe_id': store.read(self.root / 'universe.json')['id'], 'native_session_id': str(uuid4())})
            store.write(directory / 'runtime.json', {
                'launch_id': str(uuid4()), 'session_generation': 1, 'state': 'running',
                'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}})
        self.entries, self.sent, self.reads = [], [], []
        self.coverage, self.reason, self.reply_state = 'complete', None, 'ok'
        owner = self
        class Adapter:
            def capabilities(self, session, executor=None):
                return {name: Capability('supported') for name in ('send_steer', 'send_follow_up')}
            def queued_prompts(self, session, executor):
                owner.reads.append(session.agent_id)
                return NativeResult(owner.reply_state, owner.reason, value={
                    'entries': list(owner.entries), 'coverage': owner.coverage,
                    'reason': owner.reason, 'observed_at': store.now()})
            def send(self, session, message, executor):
                owner.sent.append((session.agent_id, message))
                return NativeResult('native_queued')
        self.adapter = Adapter()
        self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.enterContext(patch.object(native, 'session_snapshot', side_effect=lambda root, d, p, f, **kw: (p, f)))

    def environment(self):
        directory = self.directories['a']
        return {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': directory.name, 'MV2_AGENT_DIR': str(directory),
                'MV2_LAUNCH_ID': store.read(directory / 'runtime.json')['launch_id']}

    def queued(self, sender='b', mode='follow-up', *, wire=None):
        directory = self.directories['a']
        message = mailbox.append_message(directory, 'PRIVATE MESSAGE', {
            'agent_id': self.directories[sender].name, 'name': 'old name', 'universe': str(self.root)}, delivery=mode)
        properties, facts = store.read(directory / 'properties.json'), store.read(directory / 'runtime.json')
        attempt = {'launch_id': facts['launch_id'], 'native_session_id': properties['native_session_id'],
                   'state': 'unknown', 'mode': mode}
        if wire:
            attempt['native_message_id'] = wire
        mailbox.update_message(directory, message['id'], submission_state='submitting', attempts=[attempt])
        row = {'message_id': wire or message['id'], 'mode': mode, 'since': store.now(), 'native_id': None}
        self.entries.append(row)
        return message, row

    def test_exact_queue_join_is_read_only_and_never_guesses_source_or_mode(self):
        """Only delivery identities proven for the current conversation may block a send, including retained queues."""
        directory = self.directories['a']
        message, first = self.queued(wire=str(uuid4()))
        _, steer = self.queued('c', 'steer')
        first['mode'], steer['mode'] = 'steer', 'follow-up'  # The harness cannot reclassify the original mode.
        self.entries.append({'message_id': 'foreign-client', 'mode': 'follow-up', 'text': 'PRIVATE TYPED INPUT'})
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        report, senders, unconfirmed = queued_prompts.for_send(self.root, directory.name)
        self.assertEqual(report.state, 'ok', report)
        self.assertEqual(report.coverage, 'complete')
        self.assertEqual([r.mode for r in report.entries], ['follow-up', 'steer'])
        self.assertEqual([r.message_id for r in report.entries], [first['message_id'], steer['message_id']])
        self.assertEqual(senders, {self.directories['b'].name})
        self.assertEqual(unconfirmed, {self.directories['b'].name})
        self.assertNotIn('PRIVATE', json.dumps(asdict(report)))
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}, before)
        self.assertFalse(self.sent)
        for change in ({'launch_id': str(uuid4())}, {'native_session_id': str(uuid4())}):
            with self.subTest(change=change):
                ledger = store.read(directory / 'inbox-state.json')
                old = ledger[message['id']]['attempts'][0]
                ledger[message['id']]['attempts'][0] = {**old, **change}
                store.write(directory / 'inbox-state.json', ledger)
                found, senders, unconfirmed = queued_prompts.for_send(self.root, directory.name)
                self.assertEqual([r.message_id for r in found.entries], [steer['message_id']])
                self.assertFalse(senders)
                ledger[message['id']]['attempts'][0] = old
                store.write(directory / 'inbox-state.json', ledger)
        self.entries[0]['since'] = None
        self.assertIsNone(queued_prompts.report(self.root, directory.name).entries[0].since)
        # Native queues may survive a frontend resume, but an old ID alone is not storage proof.
        properties_path = directory / 'properties.json'
        properties = store.read(properties_path)
        home = str(self.work / 'native-home')
        store.write(properties_path, {**properties, 'native_session_home': home})
        ledger_path = directory / 'inbox-state.json'
        original_ledger = store.read(ledger_path)
        old_launch = str(uuid4())
        cases = (
            ('attempt snapshot', 'attempt', {}, True),
            ('logical ID state snapshot', 'state', {}, True),
            ('ambiguous logical ID state snapshot', 'state retries', {}, False),
            ('missing snapshot', 'missing', {}, False),
            ('missing home', 'attempt', {'home': None}, False),
            ('other home', 'attempt', {'home': str(self.work / 'other-home')}, False),
            ('relative home', 'attempt', {'home': 'native-home'}, False),
            ('other workspace', 'attempt', {'workspace': str(self.work / 'elsewhere')}, False),
            ('other native ID', 'attempt', {'native_id': str(uuid4())}, False),
            ('other launch snapshot', 'attempt', {'launch_id': str(uuid4())}, False),
            ('other input snapshot', 'attempt', {'native_message_id': str(uuid4())}, False),
        )
        try:
            for label, source, changes, matches in cases:
                with self.subTest(retained=label):
                    ledger = json.loads(json.dumps(original_ledger))
                    attempt = ledger[message['id']]['attempts'][0]
                    attempt['launch_id'] = old_launch
                    wire = message['id'] if source.startswith('state') else first['message_id']
                    if source.startswith('state'):
                        attempt.pop('native_message_id')
                    snapshot = {'launch_id': old_launch, 'native_id': properties['native_session_id'],
                                'home': home, 'workspace': str(self.work), **changes}
                    if source == 'attempt':
                        snapshot.setdefault('native_message_id', wire)
                        attempt['delivery_context'] = snapshot
                    elif source.startswith('state'):
                        ledger[message['id']]['delivery_context'] = snapshot
                        if source == 'state retries':
                            ledger[message['id']]['attempts'].append(dict(attempt))
                    store.write(ledger_path, ledger)
                    self.entries[0] = {**first, 'message_id': wire}
                    result, senders, unconfirmed = queued_prompts.for_send(self.root, directory.name)
                    expected = [wire, steer['message_id']] if matches else [steer['message_id']]
                    self.assertEqual([entry.message_id for entry in result.entries], expected, result)
                    self.assertEqual(result.coverage, 'complete' if matches else 'unknown', result)
                    self.assertEqual(bool(senders), matches)
                    self.assertEqual(bool(unconfirmed), matches)
                    self.assertFalse(self.sent)
        finally:
            store.write(properties_path, properties)
            store.write(ledger_path, original_ledger)
            self.entries[0] = first

    def test_queued_follow_ups_refuse_only_the_matching_batch_recipients(self):
        """A blocked recipient must not silently drop another recipient's message or duplicate it on a forced retry."""
        self.queued()
        with patch.dict(os.environ, self.environment()):
            for targets, names in (('c,b', ['c', 'b']), ('b,c,b', ['b', 'c']),
                                   ('missing,c,b', ['missing', 'c', 'b'])):
                with self.subTest(targets=targets):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    before = len(self.reads), len(self.sent)
                    with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit:
                        send.cli_run(self.root, agent=targets, text='text batch')
                    self.assertEqual(exit.exception.code, 1)
                    self.assertIn('Sent to c; it reads this at its next pause.', stdout.getvalue())
                    self.assertIn('Your message to b is not delivered', stderr.getvalue())
                    self.assertEqual(stderr.getvalue().count('resend with --force'), 1)
                    self.assertEqual(len(self.reads), before[0] + 1)
                    self.assertEqual(len(self.sent), before[1] + 1)
                    result = send.cli_run(self.root, agent=targets, text='JSON batch', json=True)
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['blocked_recipients'], ['b'])
                    self.assertEqual([row['agent'] for row in result.get('recipients', [])], names)
                    for row in result['recipients']:
                        self.assertEqual(row['ok'], row['agent'] == 'c')
                        self.assertEqual(row['saved'], row['agent'] == 'c')
                    self.assertEqual(len(self.sent), before[1] + 2)
                    self.assertFalse((self.directories['b'] / 'inbox.jsonl').exists())
            self.assertTrue((self.directories['c'] / 'inbox.jsonl').is_file())
            delivered = (self.directories['c'] / 'inbox.jsonl').read_bytes()
            self.assertTrue(send.run(self.root, agent='b', text='forced retry', force=True)['ok'])
            self.assertEqual((self.directories['c'] / 'inbox.jsonl').read_bytes(), delivered)
            # Several blocked recipients still produce one unsuccessful outcome per recipient, with no write.
            self.queued('c')
            before = {name: (self.directories[name] / 'inbox.jsonl').read_bytes() for name in ('b', 'c')}
            result = send.cli_run(self.root, agent='b,c', text='both blocked', json=True)
            self.assertEqual([row['agent'] for row in result.get('recipients', [])], ['b', 'c'])
            self.assertTrue(all(row['ok'] is False and row['saved'] is False for row in result['recipients']))
            self.assertEqual({name: (self.directories[name] / 'inbox.jsonl').read_bytes() for name in ('b', 'c')}, before)

    def test_send_checks_queued_follow_ups_and_preserves_exceptions(self):
        """Read-first refusal must save nothing for the blocked recipient while force, inbox-only and non-agent callers still send."""
        pending, row = self.queued()
        lines = []
        with patch.dict(os.environ, self.environment()):
            for targets in ('b',):
                with self.subTest(targets=targets):
                    outcome = send.run(self.root, agent=targets, text='outgoing', _present_error=lines.append)
                    self.assertFalse(outcome['ok'])
                    self.assertFalse(outcome['saved'])
                    self.assertEqual(outcome['blocked_recipients'], ['b'])
                    self.assertIn('--force', outcome['reason'])
                    self.assertFalse(self.sent)
                    for name in ('b', 'c'):
                        self.assertFalse((self.directories[name] / 'inbox.jsonl').exists())
            props = store.read(self.directories['b'] / 'properties.json')
            store.write(self.directories['b'] / 'properties.json', {**props, 'name': 'b\x1b[2J'})
            with self.subTest('damaged name cannot inject terminal controls'):
                output = []
                blocked = send.run(self.root, agent=self.directories['b'].name, text='outgoing',
                                   _present_error=output.append)
                self.assertFalse(blocked['ok'])
                self.assertNotIn('\x1b', '\n'.join(output))
            store.write(self.directories['b'] / 'properties.json', {**props, 'name': 'renamed'})
            blocked = send.run(self.root, agent='renamed', text='outgoing')
            self.assertEqual(blocked.get('blocked_recipients'), ['renamed'])
            before = len(self.reads)
            for options in ({'force': True}, {'delivery': 'inbox-only'}, {'queue_only': True},
                            {'sender': {'plugin': 'fixture', 'name': 'plugin'}},
                            {'sender': {'name': 'user'}}):
                with self.subTest(options=options):
                    self.assertTrue(send.run(self.root, agent='renamed', text='outgoing', **options).get('ok', True))
                    self.assertEqual(len(self.reads), before)
            self.entries.clear()
            for state, coverage in (('ok', 'complete'), ('ok', 'unknown'), ('unavailable', 'unknown')):
                self.reply_state, self.coverage = state, coverage
                self.reason = 'Fixture queue cannot be read' if coverage == 'unknown' else None
                output = []
                outcome = send.run(self.root, agent='renamed', text='allowed', _present=output.append)
                self.assertTrue(outcome['ok'], outcome)
                self.assertEqual('queue_check_note' in outcome, coverage == 'unknown')
                self.assertEqual("couldn't check" in '\n'.join(output), coverage == 'unknown')
            self.reply_state, self.coverage, self.reason = 'ok', 'complete', None
            self.queued('b', 'steer')
            self.assertTrue(send.run(self.root, agent='renamed', text='steers do not block')['ok'])
            # Positive presence still blocks under incomplete coverage.
            self.entries.append(row)
            self.coverage, self.reason = 'unknown', 'Only part of the queue is visible'
            self.assertFalse(send.run(self.root, agent='renamed', text='not saved')['ok'])
            # The old inbox advice remains independent of the native queue.
            self.entries.clear()
            mailbox.append_message(self.directories['a'], 'manual reply', {'agent_id': self.directories['b'].name})
            output = []
            send.run(self.root, agent='renamed', text='allowed', _present=output.append)
            self.assertIn('Reminder: you have unread messages from renamed.', '\n'.join(output))
            # Unknown queue notes concern this recipient's unconfirmed follow-ups only.
            ledger_path = self.directories['a'] / 'inbox-state.json'
            ledger = store.read(ledger_path)
            cases = (
                ('unconfirmed', {}, {}, True),
                ('confirmed receipt', {'native_receipt': {'transcript_confirmed': True}}, {}, False),
                ('confirmed timestamp', {'transcript_confirmed_at': store.now()}, {}, False),
                ('damaged timestamp', {'transcript_confirmed_at': {'bad': True}}, {}, True),
                ('truthy is not proof', {'native_receipt': {'transcript_confirmed': 1}}, {}, True),
                ('only steer', {}, {'mode': 'steer'}, False),
                ('prior launch', {}, {'launch_id': str(uuid4())}, False),
                ('other conversation', {}, {'native_session_id': str(uuid4())}, False),
            )
            for label, changes, attempt_changes, expected_note in cases:
                with self.subTest(note=label):
                    altered = json.loads(json.dumps(ledger))
                    altered[pending['id']].update(changes)
                    altered[pending['id']]['attempts'][0].update(attempt_changes)
                    store.write(ledger_path, altered)
                    # A provider without the method must still consult the ledger.
                    with patch.object(self.adapter, 'queued_prompts', None):
                        result = send.run(self.root, agent='c,renamed', text='allowed')
                    self.assertNotIn('queue_check_note', result['recipients'][0], result)
                    self.assertEqual('queue_check_note' in result['recipients'][1], expected_note, result)
            # Confirmation of one attempt cannot hide another queued input from the same sender.
            altered = json.loads(json.dumps(ledger))
            old = altered[pending['id']]['attempts'][0]
            newer = {**old, 'native_message_id': str(uuid4())}
            altered[pending['id']].update(attempts=[old, newer], native_receipt={'transcript_confirmed': True})
            store.write(ledger_path, altered)
            with patch.object(self.adapter, 'queued_prompts', None):
                self.assertIn('queue_check_note', send.run(self.root, agent='renamed', text='allowed'))
                old['delivery_receipt'] = {'state': 'native_queued', 'value': {'transcript_confirmed': True}}
                store.write(ledger_path, altered)
                self.assertNotIn('queue_check_note', send.run(self.root, agent='renamed', text='allowed'))
                store.write(ledger_path, {})  # No submission in this launch, despite saved history.
                self.assertNotIn('queue_check_note', send.run(self.root, agent='renamed', text='allowed'))
            # Stable client IDs can recur, but another launch or conversation owns its own confirmation.
            for scope in ('launch_id', 'native_session_id'):
                for proof in ({'native_receipt': {'transcript_confirmed': True}},
                              {'transcript_confirmed_at': store.now()}):
                    with self.subTest(reused_id_scope=scope, proof=proof):
                        altered = json.loads(json.dumps(ledger))
                        state = altered[pending['id']]
                        current = state['attempts'][0]
                        state.update(attempts=[current, {**current, scope: str(uuid4())}], **proof)
                        store.write(ledger_path, altered)
                        with patch.object(self.adapter, 'queued_prompts', None):
                            result = send.run(self.root, agent='renamed', text='allowed')
                        self.assertTrue(result['ok'])
                        self.assertIn('queue_check_note', result)
            store.write(ledger_path, ledger)
            # A custom or not-yet-bound harness still has enough history to rule out a follow-up.
            properties_path = self.directories['a'] / 'properties.json'
            properties = store.read(properties_path)
            store.write(properties_path, {**properties, 'native_session_id': None})
            self.reads.clear()
            try:
                for history, expected_note in (({}, False), ([], True)):
                    with self.subTest(unbound_history=history):
                        store.write(ledger_path, history)
                        result = send.run(self.root, agent='c', text='allowed while unbound')
                        self.assertTrue(result['ok'], result)
                        self.assertEqual('queue_check_note' in result, expected_note, result)
                        self.assertFalse(self.reads)  # An unbound native queue must never be queried.
            finally:
                store.write(properties_path, properties)
                store.write(ledger_path, ledger)

    def test_failed_slow_or_changed_queue_reads_are_unknown_without_mutation(self):
        """An advisory queue read cannot stall sends, trust stale binding or authorize a native mutation."""
        directory = self.directories['a']
        self.queued()
        original = self.adapter.queued_prompts
        for mutation in ('exception', 'duplicate', 'timestamp', 'generation', 'generation bool',
                         'generation float', 'act', 'special file'):
            with self.subTest(mutation=mutation):
                saved = (directory / 'runtime.json').read_bytes()
                def changed(session, executor):
                    result = original(session, executor)
                    if mutation == 'exception':
                        raise RuntimeError('PRIVATE ERROR')
                    if mutation == 'duplicate':
                        result.value['entries'] *= 2
                    if mutation == 'timestamp':
                        result.value['observed_at'] = '2026-11-10'
                    if mutation == 'generation':
                        agents.update_runtime(directory, session.launch_id, session_generation=2)
                    if mutation in ('generation bool', 'generation float'):
                        value = True if mutation == 'generation bool' else 1.0
                        agents.update_runtime(directory, session.launch_id, session_generation=value)
                    if mutation == 'act':
                        from multiverse.runtime import effects
                        effects.call(self.adapter, 'send', session, None, executor)
                    return result
                cursor = directory / 'inbox.cursor'
                os.mkfifo(cursor)  # Cursor damage is outside this observation.
                if mutation == 'special file':
                    ledger = directory / 'inbox-state.json'
                    old = ledger.read_bytes()
                    ledger.unlink()
                    os.mkfifo(ledger)
                try:
                    with patch.object(self.adapter, 'queued_prompts', side_effect=changed):
                        result = queued_prompts.report(self.root, directory.name)
                    self.assertEqual(result.coverage, 'unknown', result)
                    self.assertFalse(result.entries)
                    self.assertNotIn('PRIVATE', result.reason)
                    self.assertFalse(self.sent)
                finally:
                    cursor.unlink()
                    (directory / 'runtime.json').write_bytes(saved)
                    if mutation == 'special file':
                        ledger.unlink()
                        ledger.write_bytes(old)
        for stage in ('read', 'thread cleanup'):
            with self.subTest(slow=stage):
                release, entered = threading.Event(), threading.Event()
                workers = []
                local = threading.local()
                class Cleanup:
                    def __del__(self):
                        entered.set()
                        release.wait(5)
                def slow(*args):
                    workers.append(threading.current_thread())
                    if stage == 'read':
                        entered.set()
                        release.wait(5)
                    else:
                        local.cleanup = Cleanup()
                    return original(*args)
                try:
                    with patch.object(self.adapter, 'queued_prompts', side_effect=slow), \
                            patch.object(queued_prompts, 'BUDGET', .05):
                        start = time.monotonic()
                        result, _, unconfirmed = queued_prompts.for_send(self.root, directory.name)
                        self.assertTrue(entered.is_set())
                        self.assertLess(time.monotonic() - start, .5)
                        self.assertEqual(result.coverage, 'unknown')
                        self.assertEqual(unconfirmed, {self.directories['b'].name})
                        self.assertIn('longer', result.reason)
                        reads = len(self.reads)
                        self.assertIn('still running', queued_prompts.report(self.root, directory.name).reason)
                        self.assertEqual(len(self.reads), reads)
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(5)
                        self.assertFalse(worker.is_alive())
                self.assertEqual(queued_prompts.report(self.root, directory.name).coverage, 'complete')
