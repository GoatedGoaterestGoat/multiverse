"""Consumed user-item IDs confirm only the exact bound Codex delivery."""
from copy import deepcopy
import asyncio
import inspect
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.types import NativeResult, SessionHandle
from multiverse.runtime import native, rpc
from multiverse.store import lock, now, read, write
from multiverse.tools import deliver
from multiverse.universe import agents, mailbox, records, templates
from isolation import isolate

try:
    from multiverse.runtime.delivery_observations import DeliveryPublisher
except ImportError:
    DeliveryPublisher = None  # The unchanged transport regression also runs on the old implementation.


def item_event(native='primary', client='wire-attempt', method='item/completed'):
    return {'method': method, 'params': {'threadId': native, 'turnId': 'old-turn',
        'item': {'type': 'userMessage', 'id': 'native-item', 'clientId': client,
                 'content': [{'type': 'text', 'text': 'PRIVATE BODY NOT RECEIPT METADATA'}]}}}


class ReceiptObservationTest(unittest.TestCase):
    def connection(self, cli=(0, 170, 0), greeting='codex/0.170.0'):
        root = LaunchProtocol({'native_id': 'primary', 'navigation_cli': cli, 'workspace': '/workspace'})
        root.bound = True
        connection = root.for_connection('owner', lambda: True)
        connection.outgoing({'id': 1, 'method': 'initialize', 'params': {}})
        connection.incoming({'id': 1, 'result': {'userAgent': greeting}})
        return connection

    def test_only_exact_consumed_ids_cross_the_checked_primary_boundary(self):
        """Queue receipts or child events must never become confirmation of another input."""
        expected = {'kind': 'user_message', 'native_session_id': 'primary', 'turn_id': 'old-turn',
                    'item_id': 'native-item', 'client_message_id': 'wire-attempt'}
        for version in ('0.166.0', '0.167.0', '0.167.1', '0.167.2', '0.169.0', '0.170.0', '0.171.0'):
            connection = self.connection(tuple(map(int, version.split('.'))), 'codex/' + version)
            for method in ('item/started', 'item/completed'):
                with self.subTest(version=version, method=method):
                    event = item_event(method=method)
                    connection.incoming(event)
                    self.assertEqual(connection.delivery_observation(event), expected)
                    event['params']['item']['content'] = object()
                    self.assertEqual(connection.delivery_observation(event), expected)
        connection = self.connection()
        for cli, greeting in (((0, 170, 0), 'codex/0.169.0'), ((0, 169, 0), 'codex/0.170.0'),
                              ((0, 168, 0), 'codex/0.168.0'), ((0, 170, 1), 'codex/0.170.1'),
                              ((0, 171, 0), 'codex/0.170.0'), ((0, 170, 0), 'codex/0.171.0'),
                              ((0, 171, 1), 'codex/0.171.1'), (None, 'codex/0.170.0'),
                              ((False, 170, 0), 'codex/0.170.0'), ((0, 170, 0), 'unverified')):
            with self.subTest(cli=cli, greeting=greeting):
                self.assertIsNone(self.connection(cli, greeting).delivery_observation(item_event()))
        for target, field, value in ((connection, 'ended', True), (connection.root, 'bound', False),
                                     (connection.root, 'launch_refusal', {'message': 'refused'}),
                                     (connection, 'owns_primary', lambda: False)):
            previous = getattr(target, field)
            try:
                setattr(target, field, value)
                self.assertIsNone(connection.delivery_observation(item_event()))
            finally:
                setattr(target, field, previous)
        for native in ('side', 'attached', 'old-primary', None):
            event = item_event(native=native)
            event['params']['parentThreadId'] = 'primary'
            self.assertIsNone(connection.delivery_observation(event))
        for field in ('threadId', 'turnId', 'id', 'clientId'):
            for invalid in (None, '', True, 7, '\0', '\ud800', 'x' * 4097):
                with self.subTest(field=field, invalid=repr(invalid)[:40]):
                    event = item_event()
                    target = event['params'] if field in ('threadId', 'turnId') else event['params']['item']
                    target[field] = invalid
                    self.assertIsNone(connection.delivery_observation(event))
        for change in ({'id': None}, {'method': 'turn/started'}, {'method': 'thread/queue/updated'},
                       {'params': []}, {'params': {'threadId': 'primary', 'item': None}}):
            self.assertIsNone(connection.delivery_observation({**item_event(), **change}))
        for kind in ('agentMessage', 'enteredReviewMode', 'commandExecution'):
            event = item_event()
            event['params']['item']['type'] = kind
            self.assertIsNone(connection.delivery_observation(event))
        event = item_event()
        before = deepcopy(event)
        self.assertEqual(connection.delivery_observation(event), expected)
        self.assertEqual(event, before)
        connection.root.native_id = 'next-primary'
        self.assertIsNone(connection.delivery_observation(event))
        self.assertEqual(connection.delivery_observation(item_event(native='next-primary')),
                         {**expected, 'native_session_id': 'next-primary'})


class DeliveryRecords:
    def records(self):
        isolate(self)
        self.temporary = tempfile.TemporaryDirectory(prefix='mv2-receipt-', dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name).resolve()
        self.root = templates.create(self.work / '.multiverse', 'receipts', workdir=self.work)
        self.agent, self.launch, self.native = str(uuid4()), str(uuid4()), str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.home = self.work / 'native-home'
        self.home.mkdir()
        self.properties = {'id': self.agent, 'name': 'recipient', 'harness': 'codex',
            'workspace': str(self.work), 'native_session_id': self.native, 'native_session_home': str(self.home)}
        self.facts = {'launch_id': self.launch, 'state': 'running', 'session_generation': 0,
            'native_versions': {'cli': '0.170.0', 'daemon': '0.170.0'}, 'native_relay': {'state': 'ready'},
            'native_mode': {'mode': 'bridge'},
            'control_endpoint': {'transport': 'unix-websocket', 'address': str(self.work / 'relay')}}
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', self.facts)
        self.publisher = DeliveryPublisher(self.directory, self.launch, agent_id=self.agent) if DeliveryPublisher else None

    def seed(self, *, attempts=None, state='accepted'):
        message = mailbox.append_message(self.directory, 'PRIVATE SAVED MESSAGE', {'name': 'sender'},
                                         'follow-up', target_session=self.native)
        if attempts is None:
            attempts = [self.attempt()]
        mailbox.update_message(self.directory, message['id'], submission_state=state, attempts=attempts,
            target_session=self.native, delivery_method='native', native_receipt={'queued_submission_id': 'queue-id'})
        return message, attempts

    def attempt(self, **changes):
        wire = changes.pop('wire', str(uuid4()))
        launch = changes.get('launch_id', self.launch)
        context = {'launch_id': launch, 'native_id': self.native, 'native_message_id': wire,
                   'home': str(self.home), 'workspace': str(self.work), 'since': now(), 'text': 'EXISTING CONTEXT'}
        return {'state': 'native_queued', 'native_message_id': wire, 'native_session_id': self.native,
                'agent_id': self.agent, 'launch_id': launch, 'mode': 'follow-up', 'at': now(),
                'delivery_context': context, **changes}

    def evidence(self, wire):
        return {'kind': 'user_message', 'native_session_id': self.native, 'turn_id': 'old-turn',
                'item_id': 'native-item', 'client_message_id': wire}

    def tracking(self, message):
        return mailbox.message_record(self.directory, message['id'])[1]


class DeliveryPublicationTest(DeliveryRecords, unittest.TestCase):
    def setUp(self):
        self.records()
        if self.publisher is None:
            self.skipTest('This implementation has no delivery publisher')

    def test_unique_attempt_scope_and_duplicate_receipts_preserve_the_latest_submission(self):
        """Late or repeated native events must not settle a retry or borrow another launch's storage."""
        old, latest = self.attempt(launch_id='old-launch'), self.attempt()
        message, _ = self.seed(attempts=[old, latest], state='unknown')
        with patch.object(mailbox, '_event') as events:
            self.assertTrue(self.publisher(self.evidence(old['native_message_id'])))
            saved = self.tracking(message)
            self.assertTrue(saved['attempts'][0]['delivery_receipt']['value']['transcript_confirmed'])
            self.assertEqual(saved['submission_state'], 'unknown')
            self.assertNotIn('transcript_confirmed', saved['native_receipt'])
            self.assertTrue(self.publisher(self.evidence(latest['native_message_id'])))
            confirmed = self.tracking(message)
            self.assertTrue(confirmed['native_receipt']['transcript_confirmed'])
            self.assertTrue(self.publisher(self.evidence(latest['native_message_id'])))
            self.assertEqual(self.tracking(message), confirmed)
            self.assertEqual([call.args[2]['stage'] for call in events.call_args_list], ['accepted', 'confirmed'])
        for bad in ('missing-context', 'wrong-home', 'wrong-workspace', 'wrong-native', 'wrong-logical', 'ambiguous-context',
                    'current-wrong-home', 'current-wrong-workspace'):
            with self.subTest(proof=bad):
                attempt = self.attempt(launch_id=self.launch if bad.startswith('current-') else 'old-launch')
                bad = bad.removeprefix('current-')
                if bad == 'missing-context':
                    attempt.pop('delivery_context')
                elif bad == 'wrong-native':
                    attempt['native_session_id'] = 'foreign'
                elif bad == 'wrong-logical':
                    attempt['message_id'] = 'foreign-logical-message'
                elif bad in ('wrong-home', 'wrong-workspace'):
                    attempt['delivery_context'][bad.removeprefix('wrong-')] = '/foreign'
                copies = [attempt, deepcopy(attempt)] if bad == 'ambiguous-context' else [attempt]
                if bad == 'ambiguous-context':
                    for copy in copies:
                        copy.pop('delivery_context')
                candidate, _ = self.seed(attempts=copies)
                if bad == 'ambiguous-context':
                    mailbox.update_message(self.directory, candidate['id'], delivery_context={
                        'launch_id': 'old-launch', 'native_id': self.native,
                        'native_message_id': attempt['native_message_id'],
                        'home': str(self.home), 'workspace': str(self.work)})
                before = self.tracking(candidate)
                self.assertTrue(self.publisher(self.evidence(attempt['native_message_id'])))
                self.assertEqual(self.tracking(candidate), before)

    def test_busy_send_and_fresh_launch_navigation_fences_never_lend_confirmation(self):
        """An early echo must wait for send completion, while stale launch and navigation evidence is discarded."""
        diagnostics, finished = [], threading.Event()
        def diagnose():
            try:
                diagnostics.append(self.publisher.diagnostic('capacity'))
            finally:
                finished.set()
        thread = threading.Thread(target=diagnose)
        with lock(self.directory / '.runtime.lock'):
            thread.start()
            nonblocking = finished.wait(.5)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(nonblocking, 'Optional diagnostics must not wait for the runtime lock')
        self.assertEqual(diagnostics, [False])
        message, attempts = self.seed(state='submitting')
        event = self.evidence(attempts[0]['native_message_id'])
        for name in ('.launch.lock', '.delivery.lock', '.inbox.lock'):
            with lock(self.directory / name):
                self.assertFalse(self.publisher(event), name)
                self.assertNotIn('transcript_confirmed', read(self.directory / 'inbox-state.json')[message['id']]['native_receipt'])
        for changes in ({'launch_id': 'replacement'}, {'ending_at': now()}, {'state': 'dormant'},
                        {'navigation': {'state': 'admitted'}}, {'native_mode': {'mode': 'plain'}},
                        {'native_relay': {'state': 'lost'}}):
            write(self.directory / 'runtime.json', {**self.facts, **changes})
            self.assertTrue(self.publisher(event))
            self.assertNotIn('transcript_confirmed', self.tracking(message)['native_receipt'])
        write(self.directory / 'runtime.json', self.facts)
        for changes in ({'native_session_id': 'replacement'}, {'id': str(uuid4())}):
            write(self.directory / 'properties.json', {**self.properties, **changes})
            self.assertTrue(self.publisher(event))
            self.assertNotIn('transcript_confirmed', self.tracking(message)['native_receipt'])
        write(self.directory / 'properties.json', self.properties)
        mailbox.update_message(self.directory, message['id'], submission_state='accepted')
        self.assertTrue(self.publisher(event))
        self.assertTrue(self.tracking(message)['native_receipt']['transcript_confirmed'])

    def test_missing_observations_keep_the_existing_receipt_lookup_and_unknown_result(self):
        """Unobserved consumption must keep bounded lookup rather than infer success from queue absence."""
        message, attempts = self.seed()
        calls = []
        result = NativeResult('unknown', 'No receipt in the bounded lookup')
        class Adapter:
            def get_delivery(self, session, identifier, executor):
                calls.append(identifier)
                return result
        session = SessionHandle.from_records(self.properties, self.facts, self.directory)
        executor = SimpleNamespace(properties=self.properties, facts=self.facts)
        with patch.object(native, 'context', return_value=(self.directory, Adapter(), session, executor)):
            self.assertEqual(native.deliver(self.root, self.agent, message['id'], check=True)['state'], 'unknown')
            self.assertNotIn('transcript_confirmed', self.tracking(message)['native_receipt'])
            result = NativeResult('native_queued', value={'transcript_confirmed': True, 'turn_id': 'stored-turn'})
            self.assertTrue(native.deliver(self.root, self.agent, message['id'], check=True)['value']['transcript_confirmed'])
        self.assertEqual(calls, [attempts[0]['native_message_id']] * 2)

    def test_canonical_legacy_state_and_latest_native_attempt_are_settled_without_starvation(self):
        """Canonical read fields and trailing diagnostics must not hide an already consumed native attempt."""
        attempt = self.attempt()
        message, _ = self.seed(attempts=[attempt, {'state': 'error', 'reason': 'Pre-submission diagnostic', 'at': now()}],
                               state='failed')
        path = self.directory / 'inbox-state.json'
        states = read(path)
        states[message['id']]['acknowledged_at'] = now()
        write(path, states)
        for _ in range(3):
            self.assertTrue(self.publisher(self.evidence(attempt['native_message_id'])))
        tracking = self.tracking(message)
        self.assertTrue(tracking['native_receipt']['transcript_confirmed'])
        self.assertEqual(tracking['submission_state'], 'accepted')
        self.assertEqual(tracking['attempts'][-1]['reason'], 'Pre-submission diagnostic')
        custom = 'custom-codex'
        write(self.directory / 'properties.json', {**self.properties, 'harness': custom})
        later, attempts = self.seed()
        publisher = DeliveryPublisher(self.directory, self.launch, custom, agent_id=self.agent)
        self.assertTrue(publisher(self.evidence(attempts[0]['native_message_id'])))
        self.assertTrue(self.tracking(later)['native_receipt']['transcript_confirmed'])

    def test_canonical_snapshot_still_rejects_a_real_concurrent_attempt_replacement(self):
        """Normalizing legacy state must not let a stale match stamp a different native attempt."""
        message, attempts = self.seed()
        expected = mailbox.delivery_observation_snapshot(self.directory)[message['id']]
        original = self.evidence(attempts[0]['native_message_id'])
        replacement = self.attempt()
        mailbox.update_message(self.directory, message['id'], attempts=[replacement],
                               native_receipt={'queued_submission_id': 'replacement'})
        before = self.tracking(message)
        self.assertFalse(mailbox.confirm_delivery_attempt(self.directory, message['id'], expected, 0, original,
                                                        current_index=0))
        self.assertTrue(self.publisher(original))
        self.assertEqual(self.tracking(message), before)
        self.assertTrue(self.publisher(self.evidence(replacement['native_message_id'])))
        self.assertTrue(self.tracking(message)['native_receipt']['transcript_confirmed'])

    def test_final_commit_rechecks_opened_read_bounds_after_concurrent_ledger_growth(self):
        """A small admission snapshot must not permit an unbounded final ledger read after another writer grows it."""
        message, attempts = self.seed()
        expected = mailbox.delivery_observation_snapshot(self.directory)[message['id']]
        mailbox.update_message(self.directory, message['id'], padding='x' * (records.MAX_BYTES + 1))
        before = (self.directory / 'inbox-state.json').read_bytes()
        with self.assertRaises(ValueError):
            mailbox.confirm_delivery_attempt(self.directory, message['id'], expected, 0,
                self.evidence(attempts[0]['native_message_id']), current_index=0)
        self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), before)
        write(self.directory / 'inbox-state.json', {message['id']: expected})
        self.assertTrue(self.publisher(self.evidence(attempts[0]['native_message_id'])))
        self.assertTrue(self.tracking(message)['native_receipt']['transcript_confirmed'])

    def test_stored_identity_survives_a_folder_move_but_never_adopts_another_agents_attempt(self):
        """The folder is an address, while stored and captured launch IDs must agree with the saved attempt."""
        message, attempts = self.seed()
        write(self.directory / 'properties.json', {**self.properties, 'id': str(uuid4())})
        self.assertTrue(self.publisher(self.evidence(attempts[0]['native_message_id'])))
        self.assertNotIn('transcript_confirmed', self.tracking(message)['native_receipt'])
        write(self.directory / 'properties.json', self.properties)
        relocated = self.directory.with_name('recovered-folder')
        self.directory.rename(relocated)
        self.directory = relocated
        publisher = DeliveryPublisher(relocated, self.launch, agent_id=self.agent)
        self.assertTrue(publisher(self.evidence(attempts[0]['native_message_id'])))
        self.assertTrue(self.tracking(message)['native_receipt']['transcript_confirmed'])
        foreign, attempts = self.seed(attempts=[self.attempt(agent_id=str(uuid4()))])
        before = self.tracking(foreign)
        self.assertTrue(publisher(self.evidence(attempts[0]['native_message_id'])))
        self.assertEqual(self.tracking(foreign), before)

    def test_late_positive_keeps_cancelled_manual_and_final_dispositions_intact(self):
        """A native observation must not reopen a deliberate final disposition through ledger normalization."""
        for disposition in ('cancelled', 'manual', 'launch-only', 'dismissed', 'uncheckable'):
            with self.subTest(disposition=disposition):
                message, attempts = self.seed(state=disposition if disposition in ('cancelled', 'manual', 'launch-only') else 'accepted')
                updates = ({'last_receipt': {'state': 'unavailable', 'value': {'launch_only': True, 'final': True}}}
                           if disposition == 'launch-only' else
                           {'dismissed_at': now()} if disposition == 'dismissed' else
                           {'uncheckable_reported_at': now()} if disposition == 'uncheckable' else {})
                mailbox.update_message(self.directory, message['id'], **updates)
                before = self.tracking(message)
                self.assertTrue(self.publisher(self.evidence(attempts[0]['native_message_id'])))
                after = self.tracking(message)
                self.assertTrue(after['attempts'][0]['delivery_receipt']['value']['transcript_confirmed'])
                self.assertEqual({key: value for key, value in after.items() if key != 'attempts'},
                                 {key: value for key, value in before.items() if key != 'attempts'})

    def test_duplicate_saved_identities_never_confirm_or_rewrite_the_ambiguous_record(self):
        """Automatic observation must not silently erase duplicate logical or attempt identity fields."""
        message, attempts = self.seed()
        path = self.directory / 'inbox-state.json'
        state = self.tracking(message)
        original = json.dumps({message['id']: state})
        wire = attempts[0]['native_message_id']
        duplicates = [('logical ID', '{' + json.dumps(message['id']) + ':' + json.dumps(state) + ','
                       + json.dumps(message['id']) + ':' + json.dumps(state) + '}')]
        for key, value in (('native_message_id', wire), ('agent_id', self.agent)):
            field = json.dumps(key) + ': ' + json.dumps(value)
            duplicates.append((key, original.replace(field, json.dumps(key) + ': "foreign", ' + field, 1)))
        for name, raw in duplicates:
            with self.subTest(field=name):
                path.write_text(raw)
                # Ordinary ledger reads keep their established compatibility behavior.
                self.assertEqual(self.tracking(message)['attempts'][0]['native_message_id'], wire)
                with self.assertRaises(ValueError):
                    self.publisher(self.evidence(wire))
                self.assertEqual(path.read_text(), raw)
        path.write_text(original)
        expected = mailbox.delivery_observation_snapshot(self.directory)[message['id']]
        path.write_text(duplicates[1][1])
        with self.assertRaises(ValueError):
            mailbox.confirm_delivery_attempt(self.directory, message['id'], expected, 0, self.evidence(wire),
                                             current_index=0)
        self.assertEqual(path.read_text(), duplicates[1][1])
        path.write_text(original)
        history = self.directory / 'inbox.jsonl'
        raw = history.read_text()
        field = json.dumps('id') + ': ' + json.dumps(message['id'])
        ambiguous = raw.replace(field, '"id": "foreign", ' + field, 1)
        self.assertNotEqual(raw, ambiguous)
        history.write_text(ambiguous)
        with self.assertRaises(ValueError):
            self.publisher(self.evidence(wire))
        self.assertEqual(history.read_text(), ambiguous)

    def test_unique_legacy_submission_promotes_past_a_diagnostic_without_allowing_another_send(self):
        """A real legacy native submit remains current when only a non-native diagnostic follows it."""
        message, attempts = self.seed()
        legacy = deepcopy(attempts[0])
        legacy.pop('native_message_id')
        legacy['message_id'] = message['id']
        legacy['delivery_context'].pop('native_message_id')
        diagnostic = {'state': 'error', 'reason': 'Pre-submission diagnostic', 'at': now()}
        mailbox.update_message(self.directory, message['id'], attempts=[legacy, diagnostic], submission_state='failed')
        self.assertEqual(native._receipt_attempt(self.tracking(message))[0], 0)
        self.assertTrue(self.publisher(self.evidence(message['id'])))
        self.assertEqual(self.tracking(message)['submission_state'], 'accepted')
        self.assertTrue(self.tracking(message)['native_receipt']['transcript_confirmed'])
        session = SessionHandle.from_records(self.properties, self.facts, self.directory)
        executor = native.NativeExecutor(str(self.work), self.properties, self.facts)
        with patch.object(native, 'context', return_value=(self.directory, object(), session, executor)):
            self.assertTrue(native.deliver(self.root, self.agent, message['id'], retry=True)['reused'])
        replacement = deepcopy(legacy)
        mailbox.update_message(self.directory, message['id'], attempts=[legacy, replacement, diagnostic],
                               native_receipt={'queued_submission_id': 'new'}, submission_state='unknown',
                               transcript_confirmed_at=None)
        self.assertTrue(self.publisher(self.evidence(message['id'])))
        self.assertNotIn('transcript_confirmed', self.tracking(message)['native_receipt'])


class DeliveryObservationTransportTest(DeliveryRecords, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records()
        self.errors, self.received = [], []
        self.connected = asyncio.Event()

        async def peer(connection):
            self.upstream = connection
            self.connected.set()
            async for payload in connection:
                value = json.loads(payload)
                self.received.append(value)
                if value.get('method') == 'initialize':
                    result = {'userAgent': 'codex/0.170.0'}
                elif value.get('method') == 'thread/resume':
                    result = {'thread': {'id': self.native, 'cwd': str(self.work)}}
                elif value.get('method') == 'initialized':
                    continue
                else:
                    result = {'echo': value.get('id')}
                await connection.send(json.dumps({'id': value.get('id'), 'result': result}))
        self.server = await unix_serve(peer, str(self.work / 'native'))
        self.protocol = LaunchProtocol({'native_id': self.native, 'workspace': str(self.work),
            'bootstrap': '', 'roots': [], 'navigation_cli': [0, 170, 0]})
        callbacks = ({'record_delivery_observation': self.publisher}
                     if self.publisher and 'record_delivery_observation' in inspect.signature(rpc.LaunchProxy).parameters
                     else {})
        if 'record_delivery_error' in inspect.signature(rpc.LaunchProxy).parameters:
            def diagnostic(code):
                result = self.publisher.diagnostic(code)
                if result is not False:
                    self.errors.append(code)
                return result
            callbacks['record_delivery_error'] = diagnostic
        self.proxy = rpc.LaunchProxy({'socket_path': str(self.work / 'relay'),
            'upstream_path': str(self.work / 'native')}, self.protocol, lambda identifier: None,
            self.errors.append, **callbacks)
        await asyncio.to_thread(self.proxy.start)
        self.client = await unix_connect(str(self.work / 'relay'))
        await asyncio.wait_for(self.connected.wait(), 3)
        for frame in ({'id': 1, 'method': 'initialize', 'params': {}},
                      {'id': 2, 'method': 'thread/resume', 'params': {'threadId': self.native}}):
            await self.client.send(json.dumps(frame))
            self.assertEqual(json.loads(await asyncio.wait_for(self.client.recv(), 3))['id'], frame['id'])

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.to_thread(self.proxy.close)
        self.server.close()
        await self.server.wait_closed()

    async def observe(self, event):
        payload = json.dumps(event)
        await self.upstream.send(payload)
        try:
            forwarded = await asyncio.wait_for(self.client.recv(), 3)
        except TimeoutError:
            self.fail('Optional delivery bookkeeping blocked incoming forwarding')
        self.assertEqual(forwarded, payload)

    async def confirmed(self, message):
        # Poll the atomically replaced receipt without taking the inbox lock:
        # a locked poll can repeatedly win between the observer snapshot and CAS.
        async with asyncio.timeout(4):
            while not ((read(self.directory / 'inbox-state.json').get(message['id']) or {})
                       .get('native_receipt') or {}).get('transcript_confirmed'):
                await asyncio.sleep(.02)
        self.assertTrue(self.tracking(message)['native_receipt']['transcript_confirmed'])

    async def test_bridge_confirms_without_check_and_never_overwrites_a_newer_retry(self):
        """A consumed queued input must stop appearing in deliver --check without a history read."""
        message, attempts = self.seed(state='submitting')
        before_cursor = (self.directory / 'inbox.cursor').read_bytes() if (self.directory / 'inbox.cursor').exists() else None
        event = item_event(native=self.native, client=attempts[0]['native_message_id'], method='item/started')
        with lock(self.directory / '.launch.lock'):
            await self.observe(event)
            await asyncio.sleep(.1)
            self.assertNotIn('transcript_confirmed', self.tracking(message)['native_receipt'])
            # This is the ordinary submit's final write after its RPC. Publication
            # must wait for it rather than being overwritten by this saved snapshot.
            mailbox.update_message(self.directory, message['id'], submission_state='accepted', attempts=attempts,
                                   native_receipt={'queued_submission_id': 'queue-id'})
        try:
            await self.confirmed(message)
        except TimeoutError:
            self.fail(f'The bridge did not confirm: {self.errors!r}; pending={self.proxy.delivery_observations!r}; saved={self.tracking(message)!r}')
        saved = self.tracking(message)
        event['method'] = 'item/completed'
        await self.observe(event)
        await asyncio.sleep(.1)
        self.assertEqual(self.tracking(message), saved)
        with patch('multiverse.runtime.native.context', side_effect=AssertionError('Confirmed inputs need no RPC')):
            self.assertEqual(deliver.run(self.root, agent=self.agent, check=True)['messages'], [])
        self.assertEqual((self.directory / 'inbox.cursor').read_bytes()
                         if (self.directory / 'inbox.cursor').exists() else None, before_cursor)
        self.assertEqual([frame['method'] for frame in self.received], ['initialize', 'thread/resume'])
        self.assertNotIn('PRIVATE BODY', repr(self.proxy.delivery_observations))

    async def test_failed_publication_and_bounded_pressure_preserve_terminal_forwarding(self):
        """Optional receipt failures must leave the same terminal usable and retain bounded lookup fallback."""
        if self.publisher is None:
            self.skipTest('This implementation has no delivery publisher')
        message, attempts = self.seed()
        event = item_event(native=self.native, client=attempts[0]['native_message_id'])
        with patch.object(mailbox, 'confirm_delivery_attempt', side_effect=OSError('PRIVATE FAILURE TEXT')):
            await self.observe(event)
            async with asyncio.timeout(3):
                while not self.errors:
                    await asyncio.sleep(.02)
        self.assertNotIn('PRIVATE FAILURE', repr(self.errors))
        self.assertNotIn('transcript_confirmed', self.tracking(message)['native_receipt'])
        await self.client.send(json.dumps({'id': 8, 'method': 'ordinary/input'}))
        self.assertEqual(json.loads(await asyncio.wait_for(self.client.recv(), 3))['id'], 8)
        self.proxy.MAX_DELIVERY_OBSERVATIONS = 2
        with lock(self.directory / '.delivery.lock'):
            for index in range(6):
                await self.observe(item_event(native=self.native, client=str(uuid4())))
            self.assertLessEqual(len(self.proxy.delivery_observations), 2)
        async with asyncio.timeout(4):
            while self.proxy.delivery_observations:
                await asyncio.sleep(.02)
        await self.observe(event)
        await self.confirmed(message)

    async def test_busy_runtime_diagnostics_never_block_incoming_forwarding_or_shutdown(self):
        """An overflow diagnostic must not recreate a runtime-lock wait on the relay loop."""
        if self.publisher is None:
            self.skipTest('This implementation has no delivery publisher')
        # Model the ordinary runner error callback too: optional diagnostics
        # must not accidentally reuse this blocking path from incoming forwarding.
        self.proxy.record_error = lambda reason: agents.update_runtime(self.directory, self.launch, error=reason)
        self.proxy.MAX_DELIVERY_OBSERVATIONS = 1
        with lock(self.directory / '.delivery.lock'), lock(self.directory / '.runtime.lock'):
            for _ in range(4):
                await self.observe(item_event(native=self.native, client=str(uuid4())))
            self.assertIsNotNone(self.proxy.delivery_observation_error)
            await self.client.send(json.dumps({'id': 9, 'method': 'ordinary/input'}))
            self.assertEqual(json.loads(await asyncio.wait_for(self.client.recv(), 3))['id'], 9)
            self.assertFalse(self.publisher.diagnostic('capacity'))
            await asyncio.to_thread(self.proxy.close)
        self.assertFalse(self.proxy.thread.is_alive())

    async def test_real_core_send_early_echo_survives_both_acceptance_and_transport_uncertainty(self):
        """A real submit's final snapshot must not erase its earlier bridge-observed user item."""
        if self.publisher is None:
            self.skipTest('This implementation has no delivery publisher')
        loop = asyncio.get_running_loop()
        release = threading.Event()
        seen = []
        owner = self
        outcome = NativeResult('native_queued', value={'queued_submission_id': 'queue-id'})
        class Adapter:
            delivery_attempt_ids = True
            def send(self, session, message, executor):
                seen.append(message.id)
                frame = item_event(native=owner.native, client=message.id)
                asyncio.run_coroutine_threadsafe(owner.upstream.send(json.dumps(frame)), loop).result(3)
                if not release.wait(4):
                    raise AssertionError('The test did not release the native submit')
                return outcome
        session = SessionHandle.from_records(self.properties, self.facts, self.directory)
        executor = native.NativeExecutor(str(self.work), self.properties, self.facts)
        with patch.object(native, 'context', return_value=(self.directory, Adapter(), session, executor)), \
                patch.object(executor, 'check_session'):
            for result in (NativeResult('native_queued', value={'queued_submission_id': 'queue-id'}),
                           NativeResult('unknown', 'The native response was lost')):
                outcome = result
                release.clear()
                message = mailbox.append_message(self.directory, 'REAL CORE INPUT', {'name': 'sender'},
                                                 'follow-up', target_session=self.native)
                job = asyncio.create_task(asyncio.to_thread(native.deliver, self.root, self.agent, message['id']))
                try:
                    payload = json.loads(await asyncio.wait_for(self.client.recv(), 3))
                    self.assertEqual(payload['params']['item']['clientId'], seen[-1])
                    self.assertEqual(self.tracking(message)['submission_state'], 'submitting')
                    await asyncio.sleep(.1)
                    self.assertNotIn('transcript_confirmed', self.tracking(message).get('native_receipt') or {})
                finally:
                    release.set()
                await asyncio.wait_for(job, 4)
                await self.confirmed(message)
                self.assertTrue(native.deliver(self.root, self.agent, message['id'], retry=True)['reused'])
                self.assertEqual(self.tracking(message)['submission_state'], 'accepted')
        self.assertEqual(len(seen), 2)
