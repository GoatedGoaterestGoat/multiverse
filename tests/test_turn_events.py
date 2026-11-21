import errno
import json
import os
import select
from contextlib import contextmanager
from threading import Event, Thread
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse import store
from multiverse.harnesses.types import PromptClosed, PromptOpened, TurnEnded, TurnJoined, TurnStarted
from multiverse.plugins import agent_files, events, host, projection_wake
from multiverse.runtime import prompt_events, rpc, runner, transcripts, turn_events
from multiverse.runtime.transcripts import TurnReference
from multiverse.store import lock, read, write
from multiverse.tools import runtime_update
from multiverse.universe import agents, mailbox, records, sessions
from test_multiverse import BaseTest


class TurnEventsTest(TestCase):
    def test_outbox_pairs_retried_edges_and_later_ends_by_internal_turn(self):
        """A replay can duplicate edges or erase a continued turn's new delivery and later end."""
        turns = {}
        start = TurnStarted(turn='n1', kind='prompt', ref='first-row', source='person', first_task=True)
        joined = TurnJoined(turn='n1', ref='join-row', source='harness')
        stranded = TurnJoined(turn='n0', ref='other-row', source='person')
        end = TurnEnded(turn='n1', kind='prompt', ref='end-1', state='finished')
        continued = TurnEnded(turn='n1', kind='prompt', ref='end-2', state='error')
        missing = TurnReference()
        orphan_ref = TurnReference(turn='unseen-start', ref='early-input', generation='gen')
        self.assertEqual(turn_events.reduce(turns, stranded, orphan_ref, mv2=True, message='early-message'),
                         (('turn.joined', {'turn': 'unseen-start', 'ref': 'early-input', 'generation': 'gen',
                                           'source': 'mv2', 'message': 'early-message'}),))
        self.assertEqual(turn_events.reduce(turns, stranded, missing), ())
        self.assertNotIn('active', turns)
        ref = TurnReference(turn='first-row', ref='first-row', generation='gen')
        self.assertEqual(turn_events.reduce(turns, start, ref, mv2=True, message='message-1', task_launch=True),
                         (('turn.started', {'turn': 'first-row', 'ref': 'first-row', 'generation': 'gen',
                                            'source': 'mv2', 'message': None, 'kind': 'prompt',
                                            'first_task': True}),))
        self.assertEqual(turn_events.reduce(turns, start, ref), ())
        self.assertEqual(turn_events.reduce(turns, joined, missing)[0][0], 'turn.joined')
        self.assertEqual(turn_events.reduce(turns, joined, missing), ())
        self.assertEqual(turn_events.reduce(turns, end, missing)[0][1]['state'], 'finished')
        late_join = TurnJoined(turn='n1', ref='continuation-input', source='harness', attempt='attempt-2')
        late_ref = TurnReference(turn='first-row', ref='join-2', generation='gen')
        self.assertEqual(turn_events.reduce(turns, late_join, late_ref, mv2=True, message='message-2'),
                         (('turn.joined', {'turn': 'first-row', 'ref': 'join-2', 'generation': 'gen',
                                           'source': 'mv2', 'message': 'message-2'}),))
        self.assertEqual(turn_events.reduce(turns, late_join, late_ref), ())
        self.assertEqual(turn_events.reduce(turns, continued, missing)[0][1]['state'], 'error')
        self.assertEqual(turn_events.reduce(turns, end, missing), ())
        self.assertEqual(turn_events.reduce(turns, start, ref), ())

    def test_new_turn_closes_missing_end_and_an_end_alone_survives(self):
        """An unreported end must not strand its start or let an orphan end close a different open turn."""
        turns = {}
        a = TurnStarted(turn='a', kind='prompt', ref=None, source='harness', first_task=True)
        b = TurnStarted(turn='b', kind='command', ref=None, source='person', first_task=True)
        lone = TurnEnded(turn='c', kind='prompt', ref=None, state='interrupted')
        ref = TurnReference(turn='first-row')
        initial = turn_events.reduce(turns, a, ref, task_launch=True)[0][1]
        self.assertEqual((initial['first_task'], initial['source'], initial['message']), (True, 'mv2', None))
        unproved = turn_events.reduce({}, a, ref)[0][1]
        self.assertEqual((unproved['first_task'], unproved['source'], unproved['message']), (False, 'harness', None))
        self.assertEqual(turn_events.reduce(turns, lone, TurnReference())[0][1]['state'], 'interrupted')
        close, start = turn_events.reduce(turns, b, TurnReference(), task_launch=True)
        self.assertEqual(close, ('turn.ended', {'turn': 'first-row', 'kind': 'prompt', 'ref': None,
                                               'generation': None, 'state': 'unknown'}))
        self.assertEqual(start[0], 'turn.started')
        self.assertEqual((start[1]['first_task'], start[1]['source'], start[1]['message']), (False, 'person', None))
        self.assertEqual(turn_events.close_active(turns), {'turn': None, 'kind': 'command', 'ref': None,
                                                          'generation': None, 'state': 'unknown'})
        self.assertIsNone(turn_events.close_active(turns))

    def test_callback_accepts_only_bounded_turn_metadata(self):
        """A malformed callback must not carry input text to the journal or expose it in a diagnostic."""
        start = {'type': 'started', 'turn': 'native-1', 'ref': 'row-1', 'kind': 'prompt', 'source': 'person'}
        self.assertEqual(turn_events.parse(json.dumps([start])),
                         (TurnStarted(turn='native-1', ref='row-1', kind='prompt', source='person'),))
        for extra in ({'text': 'private question'}, {'source': 'mv2'}, {'turn': 'x' * 257},
                      {'ref': 'private text'}, {'first_task': 1}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, '^Invalid turn-event metadata$'):
                turn_events.parse(json.dumps([{**start, **extra}]))


class TurnJournalTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = self.root / 'state' / 'agents' / 'worker'
        self.path = self.agent / 'prompts' / 'journal' / 'events.json'
        write(self.agent / 'properties.json', {'id': 'worker', 'native_session_id': 'native',
                                               'harness': 'fake', 'workspace': str(self.work)})
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'idle'})
        self.enterContext(patch.object(events, 'subscribers', return_value=['fixture']))
        self.journal = self.enterContext(patch.object(prompt_events, '_journal_event', return_value='retry'))

    def edges(self):
        return [(row['kind'], row['data']) for row in read(self.path)['pending']]

    def drain(self):
        entries = []
        def capture(directory, row):
            entries.append((row['kind'], row['data']))
            return 'unsubscribed'
        self.journal.side_effect = capture
        try:
            for _ in range(8):
                if not read(self.path)['pending']:
                    return entries
                prompt_events.publish(self.agent, resolve_turns=True)
        finally:
            self.journal.side_effect = None
        self.fail('Pending turn events did not drain')

    def test_host_retries_after_a_projection_scan_failure(self):
        """A failed topology read cannot leave durable turn work without a projector."""
        stopping = Event()

        def repaired():
            stopping.set()
            return ()

        def scan(_):
            if tree.call_count == 1:
                raise RuntimeError('fixture topology failure')
            return repaired()

        with patch.object(host.universes, 'tree', side_effect=scan) as tree, \
                patch.object(host.projection_wake, 'wait'), patch.object(host, '_log') as log:
            host._project_agent_events(self.root, stopping)
        self.assertEqual(tree.call_count, 2)
        log.assert_called_once_with(self.root, 'turn projection scan failed (RuntimeError)')

    def test_staging_wakes_the_host_after_an_accepted_turn(self):
        """A newly queued edge can otherwise wait for the host's recovery scan."""
        edge = TurnStarted(turn='queued-turn', ref='queued-row', kind='prompt', source='person')
        with projection_wake.receiver(self.root) as descriptor:
            prompt_events.stage_turns(self.root, self.agent, 'old-launch', 'native', (edge,))
            self.assertFalse(select.select([descriptor], [], [], 0)[0])
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (edge,))
            self.assertEqual(self.edges()[0][0], '_turn.edge')
            self.assertEqual(select.select([descriptor], [], [], 0)[0], [descriptor])
            self.assertEqual(os.read(descriptor, 4096), b'\x00')

    def test_admitted_turn_end_precedes_idle_with_only_public_reference(self):
        """A status can overtake a staged end or leak the native turn ID instead of its public cursor."""
        reported = '2026-01-01T00:00:00+00:00'
        with patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, 'launch')), \
                patch.object(agents, '_event') as emit, \
                patch.object(agents, 'now', return_value=reported), \
                patch.object(transcripts, 'turn_references', side_effect=AssertionError('report projected a row')):
            runtime_update.run(status='working', turn_session_id='native', turn_events_json=json.dumps([{
                'type': 'started', 'turn': 'private-turn', 'ref': 'private-input', 'kind': 'prompt', 'source': 'person',
            }]))
        emit.assert_not_called()
        self.assertEqual(read(self.agent / 'runtime.json')['status_changed_at'], reported)

        def stage(native, generation, edges):
            prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation)
        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        relay.event_ready = Mock()
        end = TurnEnded(turn='private-turn', kind='prompt', ref='private-end', state='finished')
        relay._admit_event_transitions(stage, Mock(side_effect=[('native', (end,)), None]), 0)
        watcher = runner.StatusWatcher(self.agent, 'launch', self.root, before_status_event=relay.stage_status_events)
        with patch.object(transcripts, 'turn_references', side_effect=AssertionError('report projected a row')) as read_rows:
            watcher._publish('idle', 'native', 0)
        read_rows.assert_not_called()
        self.assertEqual(read(self.agent / 'runtime.json')['status'], 'idle')
        self.assertEqual([kind for kind, _ in self.edges()],
                         ['_turn.edge', 'agent.status', '_turn.edge', 'agent.status'])
        references = (TurnReference(turn='public-turn', ref='input-row', generation='gen'),
                      TurnReference(turn='public-turn', ref='last-row', generation='gen'))
        with patch.object(transcripts, 'turn_references', return_value=references) as project:
            entries = self.drain()
        project.assert_called_once()
        self.assertEqual(entries, [
            ('turn.started', {'turn': 'public-turn', 'ref': 'input-row', 'generation': 'gen', 'kind': 'prompt',
                              'source': 'person', 'message': None, 'first_task': False, 'resolved': True}),
            ('agent.status', {'from': 'idle', 'to': 'working'}),
            ('turn.ended', {'turn': 'public-turn', 'ref': 'last-row', 'generation': 'gen',
                            'kind': 'prompt', 'state': 'finished', 'resolved': True}),
            ('agent.status', {'from': 'working', 'to': 'idle'}),
        ])

    def test_fresh_status_companions_keep_replacement_retries_after_write_attempt(self):
        """A first-use shortcut must not overwrite an ambiguously committed fence on retry."""
        for backup in (False, True):
            with self.subTest(backup=backup):
                fence = agents._NativeFence(self.agent, str(uuid4()), launch='launch')
                fence.fresh_companions = {backup}
                path = agents._status_fence_path(self.agent, fence.key, backup=backup)
                predecessors = [['_turn.batch', {'edge': 'first'}]]

                def interrupted_write(target, value):
                    write(target, value)
                    raise OSError('completion of the write is uncertain')

                with patch.object(agents, 'open_replaced', wraps=store.open_replaced) as replaces, \
                        patch.object(agent_files, 'write_json', side_effect=interrupted_write):
                    saved = agents._write_fence_file(fence, predecessors, backup=backup)
                self.assertFalse(saved)
                replaces.assert_not_called()
                original = path.read_bytes()
                open_regular = store.open_regular
                present = iter((False, True))

                def transient_absence(target):
                    if not next(present):
                        raise FileNotFoundError(errno.ENOENT, 'File not found', target)
                    return open_regular(target)

                attempts = Mock(side_effect=transient_absence)
                with patch.object(store, 'open_regular', attempts), patch.object(agent_files, 'write_json') as overwrite:
                    self.assertTrue(agents._write_fence_file(fence, [], backup=backup))
                self.assertEqual(attempts.call_count, 2)
                overwrite.assert_not_called()
                self.assertEqual(path.read_bytes(), original)

    def test_active_relay_status_publishes_while_a_turn_row_is_pending(self):
        """The relay's turn proof can hold its own stage lock while the working event is recorded."""
        entered, release, published = Event(), Event(), Event()
        start = TurnStarted(turn='private-turn', ref='private-input', kind='prompt', source='person')

        def stage(native, generation, edges):
            entered.set()
            if release.wait(5):
                prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation)

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        relay.event_ready = Mock()
        relay._admit_event_transitions(stage, Mock(side_effect=[('native', (start,)), None]), 0)
        watcher = runner.StatusWatcher(self.agent, 'launch', self.root, before_status_event=relay.stage_status_events)
        worker = Thread(target=relay.stage_adapter_events)
        working = Thread(target=watcher._publish, args=('working', 'native', 0))
        reference = TurnReference(turn='turn', ref='input-row', generation='gen')
        def emit(directory, row):
            if row['kind'] == 'agent.status':
                published.set()
            return 'retry'

        with patch.object(prompt_events, '_journal_event', side_effect=emit), \
                patch.object(transcripts, 'turn_references', return_value=(reference,)):
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                working.start()
                self.assertTrue(published.wait(3))
                self.assertEqual(self.edges(), [('agent.status', {'from': 'idle', 'to': 'working'})])
            finally:
                release.set()
                worker.join(5)
                if working.ident is not None:
                    working.join(5)
        self.assertFalse(worker.is_alive() or working.is_alive())
        self.assertEqual([kind for kind, _ in self.edges()], ['agent.status', '_turn.edge'])

    def test_idle_runtime_does_not_wait_for_the_host_to_prove_an_end(self):
        """A held provider cannot delay idle or let status pass the known end in the journal."""
        entered, release = Event(), Event()
        callback = (self.root, self.agent, 'launch')
        start = TurnStarted(turn='native-turn', ref='input', kind='prompt', source='person')
        end = TurnEnded(turn=start.turn, ref='finish', kind='prompt', state='finished')

        def project(*args, **options):
            entered.set()
            release.wait(5)
            return (TurnReference(turn='public-turn', ref='public-input', generation='generation'),)

        def update(edge, status):
            runtime_update.run(status=status, turn_session_id='native',
                               turn_events_json=json.dumps([turn_events.encode(edge)]))

        with patch.object(runtime_update, 'callback', return_value=callback), \
                patch.object(transcripts, 'turn_references', side_effect=project):
            update(start, 'working')
            worker = Thread(target=prompt_events.publish, args=(self.agent,), kwargs={'resolve_turns': True})
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                update(end, 'idle')
                self.assertEqual(read(self.agent / 'runtime.json')['status'], 'idle')
                self.journal.assert_not_called()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        with patch.object(transcripts, 'turn_references',
                          return_value=(TurnReference(turn='public-turn', ref='public-end', generation='generation'),)):
            entries = self.drain()
        self.assertEqual([kind for kind, _ in entries], ['turn.started', 'agent.status', 'turn.ended', 'agent.status'])
        self.assertEqual(entries[-2][1]['ref'], 'public-end')

    def test_two_callbacks_keep_turn_edges_and_status_in_runtime_commit_order(self):
        """An overlapping idle report cannot journal a turn end before the committed working status."""
        start = TurnStarted(turn='native-turn', kind='prompt', ref='opener', source='person')
        end = TurnEnded(turn=start.turn, kind='prompt', ref='end', state='finished')
        real_lock = agents.lock
        interleaved = False

        @contextmanager
        def after_lock(path, *args, **kwargs):
            nonlocal interleaved
            with real_lock(path, *args, **kwargs):
                yield
            if path == self.agent / '.runtime.lock' and not interleaved:
                interleaved = True
                self.assertEqual(agents.runtime(self.agent)['status'], 'working')
                agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                                      turn_batch=(self.root, 'native', (end,)))

        with patch.object(agents, 'lock', side_effect=after_lock):
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'working'},
                                  turn_batch=(self.root, 'native', (start,)))
        self.assertEqual(agents.runtime(self.agent)['status'], 'idle')
        self.assertEqual([(kind, data.get('to')) for kind, data in self.edges()], [
            ('_turn.edge', None), ('agent.status', 'working'), ('_turn.edge', None), ('agent.status', 'idle'),
        ])

    def test_subscription_change_cannot_publish_an_older_unadmitted_status(self):
        """New status interest cannot publish an earlier status outside the ordered queue."""
        real_lock = agents.lock
        subscribed = False

        @contextmanager
        def after_lock(path, *args, **kwargs):
            nonlocal subscribed
            with real_lock(path, *args, **kwargs):
                yield
            if path == self.agent / '.runtime.lock' and not subscribed:
                subscribed = True
                agents.update_runtime(self.agent, 'launch', status='idle')

        with patch.object(events, 'subscribers', side_effect=lambda *_: ['fixture'] if subscribed else []), \
                patch.object(agents, 'lock', side_effect=after_lock), \
                patch.object(events, 'for_agent') as direct:
            agents.update_runtime(self.agent, 'launch', status='working')
        self.assertEqual(agents.runtime(self.agent)['status'], 'idle')
        self.assertEqual([(kind, data.get('to')) for kind, data in self.edges()], [('agent.status', 'idle')])
        direct.assert_not_called()

    def test_host_recovers_a_committed_status_without_another_report(self):
        """A crash after runtime commit or outbox staging must leave exactly one status in order."""
        class Interrupted(BaseException):
            pass

        facts = self.agent / 'runtime.json'
        for after_stage in (False, True):
            with self.subTest(after_stage=after_stage):
                write(facts, {'launch_id': 'launch', 'state': 'running', 'status': 'working'})
                edge = TurnEnded(turn=f'first-{after_stage}', kind='prompt', ref=None, state='finished')
                prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (edge,))
                saved_write = agents.write

                def write_runtime(path, data):
                    if after_stage and path == facts and data['status'] == 'idle' and 'journal_status' not in data:
                        raise Interrupted()
                    saved_write(path, data)

                def stop_at_fence(status):
                    if not after_stage:
                        raise Interrupted()

                with patch.object(agents, 'write', side_effect=write_runtime), self.assertRaises(Interrupted):
                    agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                                          before_status_event=stop_at_fence)
                intent, = read(facts)['journal_status']
                self.assertEqual(intent['data']['to'], 'idle')
                self.assertTrue(agents.recover_status_events(self.agent))
                self.assertNotIn('journal_status', read(facts))
                self.assertEqual([(kind, data.get('to')) for kind, data in self.drain()], [
                    ('turn.ended', None), ('agent.status', 'idle'),
                ])

    def test_a_status_backlog_keeps_runtime_readable_and_replays_in_order(self):
        """A prolonged outbox write failure cannot make the runtime record unreadable."""
        expected = ['working', 'idle'] * 20
        with patch.object(records, 'MAX_BYTES', 4096):
            with patch.object(prompt_events, 'stage_ordered', return_value=False):
                for status in expected:
                    agents.update_runtime(self.agent, 'launch', status=status)
            self.assertEqual(agents.runtime(self.agent)['status'], 'idle')
            self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual([data['to'] for _, data in self.drain()], expected)
        self.assertEqual(list(self.agent.glob('journal-status-*.json')), [])

    def test_an_outage_retains_the_present_report_status_and_its_turn_edges(self):
        """A newer report still updates runtime while the earlier ordered admission is unavailable."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'working'})
        end = TurnEnded(turn='previous', kind='prompt', ref=None, state='finished')
        start = TurnStarted(turn='next', kind='prompt', ref=None, source='person')
        with patch.object(prompt_events, 'stage_ordered', return_value=False):
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                                  turn_batch=(self.root, 'native', (end,)))
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'working'},
                                  turn_batch=(self.root, 'native', (start,)))
            self.assertEqual(agents.runtime(self.agent)['status'], 'working')
        self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual([(kind, data.get('to')) for kind, data in self.drain()], [
            ('turn.ended', None), ('agent.status', 'idle'),
            ('turn.started', None), ('agent.status', 'working'),
        ])

    def test_relay_order_survives_status_recovery_in_prefixes(self):
        """The relay can drain only part of a backlog, and only the exact watcher can add a predecessor."""
        statuses = ['working', 'idle'] * 5
        later = TurnStarted(turn='later', kind='prompt', ref=None, source='person')
        end = TurnEnded(turn='later', kind='prompt', ref=None, state='finished')

        def stage(native, generation, edges):
            return prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation)

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        with patch.object(prompt_events, 'stage_ordered', return_value=False):
            for status in statuses:
                agents.update_runtime(self.agent, 'launch', status=status)
        relay.turn_batches.append((stage, 'native', 0, (later,)))
        relay.stage_adapter_events()
        self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual([(kind, data.get('to')) for kind, data in self.drain()], [
            *[('agent.status', status) for status in statuses], ('turn.started', None),
        ])

        with patch.object(prompt_events, 'stage_ordered', return_value=False):
            agents.update_runtime(self.agent, 'launch', status='working')
            relay.turn_batches.append((stage, 'native', 0, (end,)))
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                                  before_status_event=relay.stage_status_events)
        self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual([(kind, data.get('to')) for kind, data in self.drain()], [
            ('agent.status', 'working'), ('turn.ended', None), ('agent.status', 'idle'),
        ])

    def test_a_refused_relay_record_can_use_the_watchers_exact_reservation(self):
        """A refused end already in the relay must precede the idle that fenced it."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'working'})
        end = TurnEnded(turn='earlier', kind='prompt', ref=None, state='finished')
        rejected = Mock(return_value=False)
        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=rejected)
        relay.turn_batches.append((rejected, 'native', 0, (end,)))
        agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                              before_status_event=relay.stage_status_events)
        self.assertEqual(agents.runtime(self.agent)['status'], 'idle')
        rejected.assert_called_once_with('native', 0, (end,))
        self.assertFalse(relay.turn_batches)
        self.assertEqual([kind for kind, _ in self.drain()], ['turn.ended', 'agent.status'])

    def test_an_unfinished_watcher_fence_keeps_only_its_exact_prefix(self):
        """Recovering between a refused fence and its retry cannot pass the end or pull in a later start."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'working'})
        end = TurnEnded(turn='earlier', kind='prompt', ref=None, state='finished')
        later = TurnStarted(turn='later', kind='prompt', ref=None, source='person')
        ready = False

        def stage(native, generation, edges):
            if not ready:
                relay.turn_batches.append((stage, native, generation, (later,)))
                return False
            return prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation)

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        relay.turn_batches.append((stage, 'native', 0, (end,)))
        with patch.object(agents, 'retain_fenced_batch', return_value=False):
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                                  before_status_event=relay.stage_status_events)
        self.assertEqual(agents.runtime(self.agent)['status'], 'idle')
        self.assertTrue(agents.recover_status_events(self.agent))
        ready = True
        relay.stage_adapter_events()
        self.assertEqual([kind for kind, _ in self.drain()], ['turn.ended', 'agent.status', 'turn.started'])

    def test_a_held_fence_recovers_before_a_newer_runtime_status(self):
        """A later report can commit while the earlier idle waits for its exact relay end."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'working'})
        edge = TurnEnded(turn='earlier', kind='prompt', ref=None, state='finished')
        failed = []

        def save(path, value):
            if path.name.startswith('journal-fence-'):
                failed.append(path)
                raise OSError('fixture fence file temporarily unavailable')
            return real_save(path, value)

        real_save = agent_files.write_json
        rejected = Mock(return_value=False)
        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=rejected)
        relay.turn_batches.append((rejected, 'native', 0, (edge,)))
        with patch.object(agent_files, 'write_json', side_effect=save), \
                patch.object(agents, 'retain_fenced_batch', return_value=False), \
                patch.object(agents, 'reserve_fenced_batches', return_value=False):
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                                  before_status_event=relay.stage_status_events)
            agents.update_runtime(self.agent, 'launch', status='working')
            self.assertEqual(agents.runtime(self.agent)['status'], 'working')
            self.assertFalse(agents.recover_status_events(self.agent))
        self.assertTrue(failed)
        relay.stage_adapter_events()
        self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual([(kind, data.get('to')) for kind, data in self.drain()],
                         [('turn.ended', None), ('agent.status', 'idle'), ('agent.status', 'working')])
        self.assertEqual(list(self.agent.glob('journal-fence-*.json')), [])

    def test_exit_preserves_its_relay_prefix_before_marking_the_launch_stopped(self):
        """The final status and exit must wait in order behind an already accepted end through failed admission."""
        status = self.agent / 'runtime.json'
        write(status, {'launch_id': 'launch', 'state': 'running', 'status': 'working'})
        end = TurnEnded(turn='earlier', kind='prompt', ref=None, state='finished')
        record = Mock(return_value=False)
        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=record)
        relay.turn_batches.append((record, 'native', 0, (end,)))

        with patch.object(agents, 'reserve_fenced_batches', return_value=False) as refused:
            runner._finish_relay_events(self.agent, 'launch', relay)
        self.assertEqual(refused.call_count, 2)
        agents.update_runtime(self.agent, 'launch', state='stopped', status='unknown')
        agents.stage_after_status(self.agent, 'agent.exited', 'launch', {})
        relay.stage_adapter_events()
        self.assertEqual([kind for kind, _ in self.drain()], ['turn.ended', 'agent.status', 'agent.exited'])

    def test_a_large_first_refused_relay_batch_spills_before_the_runtime_write(self):
        """Even the first refused batch can be too large for the runtime reader."""
        edges = tuple(TurnEnded(turn=f'{index}-' + 't' * 240, kind='prompt', ref='r' * 240,
                                state='finished') for index in range(16))
        with patch.object(records, 'MAX_BYTES', 4096):
            with patch.object(prompt_events, 'stage_ordered', return_value=False):
                prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', edges, generation=0)
            self.assertLessEqual((self.agent / 'runtime.json').stat().st_size, records.MAX_BYTES)
            self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual(len(self.drain()), len(edges))

    def test_inflight_relay_batches_keep_their_admission_slots_on_refusal(self):
        """Admission must count the prefix a background writer has yet to accept."""
        edge = TurnEnded(turn='same-turn', kind='prompt', ref=None, state='finished')
        drain = Mock(return_value=('native', (edge,)))

        def refuse(native_id, generation, edges):
            relay._admit_event_transitions(refuse, drain, generation)
            return False

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=refuse)
        relay.MAX_EVENT_BATCHES = 1
        relay.event_ready = Mock()
        relay.turn_batches.append((refuse, 'native', 0, (edge,)))
        self.assertIs(relay.stage_adapter_events(), False)
        self.assertEqual(relay._event_batch_count(), 1)
        drain.assert_not_called()

    def test_a_report_cannot_pass_the_conversation_its_callback_observed(self):
        """A new binding becomes visible before its event is staged, so both must hold runtime order."""
        old, new = str(uuid4()), str(uuid4())
        properties = self.agent / 'properties.json'
        write(properties, {**read(properties), 'native_session_id': old, 'native_session_home': str(self.work)})
        first = TurnStarted(turn='old', kind='prompt', ref=None, source='person')
        second = TurnStarted(turn='new', kind='prompt', ref=None, source='person')
        prompt_events.stage_turns(self.root, self.agent, 'launch', old, (first,))
        entered, release, reported = Event(), Event(), Event()
        errors = []
        stage = agents._stage_session_locked

        def pause(directory, row):
            entered.set()
            if not release.wait(5):
                errors.append('The test did not release session staging')
            return stage(directory, row)

        def rebind():
            try:
                agents.rebind_session(self.root, self.agent.name, old, new, 'clear', 'launch')
            except Exception as error:
                errors.append(error)

        def report():
            try:
                agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'working'},
                                      turn_batch=(self.root, new, (second,)))
            except Exception as error:
                errors.append(error)
            finally:
                reported.set()

        switch, callback = Thread(target=rebind, daemon=True), Thread(target=report, daemon=True)
        with patch.object(agents, '_stage_session_locked', side_effect=pause):
            switch.start()
            try:
                self.assertTrue(entered.wait(5), errors)
                self.assertEqual(read(properties)['native_session_id'], new)
                callback.start()
                self.assertFalse(reported.wait(.1), 'The callback passed the pending conversation boundary')
            finally:
                release.set()
                switch.join(5)
                if callback.ident is not None:
                    callback.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(switch.is_alive() or callback.is_alive())
        self.assertTrue(reported.is_set())
        self.assertEqual([kind for kind, _ in self.drain()],
                         ['turn.started', 'turn.ended', 'agent.session', 'turn.started', 'agent.status'])

    def test_terminal_runtime_still_commits_when_session_event_proof_is_unreadable(self):
        """A damaged lineage can delay the event without keeping a dead launch displayed as running."""
        old, new = str(uuid4()), str(uuid4())
        properties = self.agent / 'properties.json'
        write(properties, {**read(properties), 'native_session_id': old, 'native_session_home': str(self.work)})
        first = TurnStarted(turn='old', kind='prompt', ref=None, source='person')
        prompt_events.stage_turns(self.root, self.agent, 'launch', old, (first,))
        with patch.object(agents, '_stage_session_locked', return_value=False):
            agents.rebind_session(self.root, self.agent.name, old, new, 'clear', 'launch')
        journal = self.agent / 'sessions.jsonl'
        previous = journal.read_bytes()
        try:
            journal.write_bytes(b'invalid lineage\n')
            agents.update_runtime(self.agent, 'launch', state='stopped', status='unknown', exit_code=0)
            self.assertEqual((agents.runtime(self.agent)['state'], agents.runtime(self.agent)['exit_code']),
                             ('stopped', 0))
            self.assertFalse(agents.recover_status_events(self.agent))
        finally:
            journal.write_bytes(previous)
        self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual([kind for kind, _ in self.drain()],
                         ['turn.started', 'turn.ended', 'agent.session', 'agent.status'])

    def test_rebind_waits_for_relay_fence_when_no_admission_binding_was_readable(self):
        """A repaired pointer check must not release the old conversation before the relay retry."""
        old, new = str(uuid4()), str(uuid4())
        properties = self.agent / 'properties.json'
        write(properties, {**read(properties), 'native_session_id': old, 'native_session_home': str(self.work)})
        agents.update_runtime(self.agent, 'launch', status='working')
        self.drain()

        def stage(native, generation, edges):
            return prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation)

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        end = TurnEnded(turn='earlier', kind='prompt', ref=None, state='finished')
        relay.turn_batches.append((stage, old, 0, (end,)))
        with patch.object(records, 'properties', side_effect=OSError('fixture binding unavailable')), \
                patch.object(sessions, 'journal', side_effect=OSError('fixture lineage unavailable')):
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                                  before_status_event=relay.stage_status_events)
        self.assertEqual(agents.runtime(self.agent)['status'], 'idle')
        with self.assertRaisesRegex(ValueError, 'Cannot switch conversations'):
            agents.rebind_session(self.root, self.agent.name, old, new, 'clear', 'launch')
        self.assertEqual(read(properties)['native_session_id'], old)
        self.assertEqual(sessions.journal(self.agent), [])
        relay.stage_adapter_events()
        agents.rebind_session(self.root, self.agent.name, old, new, 'clear', 'launch')
        self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual([kind for kind, _ in self.drain()], ['turn.ended', 'agent.status', 'agent.session'])

    def test_a_switch_fences_batches_a_busy_background_relay_kept(self):
        """A switch would otherwise invalidate queued relay edges when a competing status owns runtime."""
        end = TurnEnded(turn='previous', kind='prompt', ref=None, state='finished')

        def stage(native, generation, edges):
            return prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation)

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        relay.turn_batches.append((stage, 'native', 0, (end,)))
        with lock(self.agent / '.runtime.lock'):
            self.assertIs(relay.stage_adapter_events(), False)
            self.assertEqual(len(relay.turn_batches), 1)
        agents.stage_before_boundary(self.agent, relay.stage_adapter_events)
        write(self.agent / 'properties.json', {'id': 'worker', 'native_session_id': 'other',
                                               'harness': 'fake', 'workspace': str(self.work)})
        agents._session_event(self.agent, {'launch': 'launch', 'from': 'native', 'to': 'other', 'reason': 'clear'})
        self.assertEqual([kind for kind, _ in self.drain()], ['turn.ended', 'agent.session'])

    def test_a_failed_native_write_leaves_the_callback_edges_and_status_in_order(self):
        """A failed first outbox save and an interrupted spill can otherwise erase a batch or leave display stale."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'working'})
        end = TurnEnded(turn='previous', kind='prompt', ref=None, state='finished')
        start = TurnStarted(turn='next', kind='prompt', ref=None, source='person')
        save = agent_files.write_json
        failures = {'outbox': 0, 'spill': 0}

        def interrupt(path, value):
            if (path == self.path and failures['outbox'] < 2
                    and any(row['kind'] == '_turn.edge' for row in value['pending'])):
                failures['outbox'] += 1
                raise OSError('fixture outbox interrupted')
            if path.name.startswith('journal-status-') and failures['spill'] < 1:
                failures['spill'] += 1
                raise OSError('fixture spill interrupted')
            return save(path, value)

        with patch.object(agent_files, 'write_json', side_effect=interrupt):
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'idle'},
                                  turn_batch=(self.root, 'native', (end,)))
            agents.mutate_runtime(self.agent, 'launch', lambda _: {'status': 'working'},
                                  turn_batch=(self.root, 'native', (start,)))
        self.assertEqual(failures, {'outbox': 2, 'spill': 1})
        self.assertEqual(agents.runtime(self.agent)['status'], 'working')
        self.assertTrue(agents.recover_status_events(self.agent))
        self.assertEqual([(kind, data.get('to')) for kind, data in self.drain()], [
            ('turn.ended', None), ('agent.status', 'idle'), ('turn.started', None), ('agent.status', 'working'),
        ])

    def test_turn_projection_overflow_keeps_older_edges_in_order_with_nulls(self):
        """Capacity can remove expensive work, but cannot drop the edge or move it after later proof."""
        start = TurnStarted(turn='turn', ref='private-start', kind='prompt', source='person')
        joined = TurnJoined(turn=start.turn, ref='private-join', source='person')
        end = TurnEnded(turn=start.turn, ref='private-end', kind='prompt', state='finished')

        def project(root, directory, launch, native, generation, edges, **options):
            self.assertNotIn(start, edges)
            return tuple(TurnReference(turn='turn', ref='public-row', generation='gen') for edge in edges)

        with patch.object(prompt_events, '_TURN_WORK_LIMIT', 2), \
                patch.object(transcripts, 'turn_references', side_effect=project):
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (start, joined, end))
            self.assertEqual(len(read(self.path)['pending']), 2)
            segment, = tuple(self.path.parent.glob('overflow-*.json'))
            prompt_events.publish(self.agent, resolve_turns=True)
            self.assertEqual(read(self.path)['front'][0]['kind'], 'turn.started')
            self.assertTrue(segment.is_file(), 'a busy journal must keep the promoted segment')
            entries = self.drain()
            self.assertEqual(list(self.path.parent.glob('overflow-*.json')), [])
        self.assertEqual([(kind, data['resolved'], data['ref']) for kind, data in entries],
                         [('turn.started', False, None), ('turn.joined', True, 'public-row'),
                          ('turn.ended', True, 'public-row')])

    def test_wait_receipt_reserves_order_while_new_status_commits(self):
        """The next callback must commit without waiting for the first callback's receipt."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'waiting'})
        opened = PromptOpened(id='permission', kind='permission', requires_waiting=True)
        closed = PromptClosed(id=opened.id, kind=opened.kind, requires_waiting=True, state='person')
        prompt_events.stage(self.agent, 'launch', 'native', (opened,))
        entered, release = Event(), Event()

        def receipt(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return (closed,)

        first = Thread(target=agents.update_runtime, args=(self.agent, 'launch'), kwargs={'status': 'working'})
        with patch.object(prompt_events, '_wait_receipts', side_effect=receipt):
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                agents.update_runtime(self.agent, 'launch', status='idle')
                self.assertEqual(agents.runtime(self.agent)['status'], 'idle')
                self.assertEqual([kind for kind, _ in self.edges()],
                                 ['prompt.opened', '_prompt.status', 'agent.status'])
            finally:
                release.set()
                first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual([(kind, data.get('to')) for kind, data in self.drain()], [
            ('prompt.opened', None), ('prompt.closed', None),
            ('agent.status', 'working'), ('agent.status', 'idle'),
        ])

    def test_wait_close_in_a_later_overflow_segment_is_recorded_once_before_status(self):
        """A proved close can take its reserved position after its duplicate has spilled."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'waiting'})
        opened = PromptOpened(id='permission', kind='permission', requires_waiting=True)
        closed = PromptClosed(id=opened.id, kind=opened.kind, requires_waiting=True, state='person')
        start = TurnStarted(turn='next', kind='prompt', ref=None, source='person')
        prompt_events.stage(self.agent, 'launch', 'native', (opened,))
        entered, release = Event(), Event()
        receipt_calls = 0

        def receipt(*args):
            nonlocal receipt_calls
            receipt_calls += 1
            if receipt_calls != 1:
                return ()
            entered.set()
            self.assertTrue(release.wait(5))
            return (closed,)

        first = Thread(target=agents.update_runtime, args=(self.agent, 'launch'), kwargs={'status': 'working'})
        with patch.object(prompt_events, '_TURN_WORK_LIMIT', 1), \
                patch.object(prompt_events, '_wait_receipts', side_effect=receipt):
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                prompt_events.stage(self.agent, 'launch', 'native', (closed,))
                prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (start,))
                self.assertEqual([kind for kind, _ in self.edges()], ['_turn.edge'])
                self.assertEqual(len(tuple(self.path.parent.glob('overflow-*.json'))), 3)
            finally:
                release.set()
                first.join(5)
            self.assertFalse(first.is_alive())
            self.assertEqual(len(tuple(self.path.parent.glob('wait-proof-*.json'))), 1)
            self.assertEqual([kind for kind, _ in self.drain()], [
                'prompt.opened', 'prompt.closed', 'agent.status', 'turn.started',
            ])
        self.assertEqual(receipt_calls, 1)
        self.assertEqual(list(self.path.parent.glob('wait-proof-*.json')), [])
        self.assertEqual(read(self.path).get('superseded_closes'), {})

    def test_failed_projection_still_publishes_the_observed_end_before_idle(self):
        """Malformed storage must not drop the observed end or strand the status behind it."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'working'})
        edge = TurnEnded(turn='turn', ref='native-row', kind='prompt', state='interrupted')
        with patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, 'launch')), \
                patch.object(transcripts, 'turn_references', side_effect=OSError('transcript unavailable')):
            runtime_update.run(status='idle', turn_session_id='native',
                               turn_events_json=json.dumps([turn_events.encode(edge)]))
            entries = self.drain()
        self.assertEqual(entries, [
            ('turn.ended', {'turn': None, 'ref': None, 'generation': None, 'resolved': False,
                            'kind': 'prompt', 'state': 'interrupted'}),
            ('agent.status', {'from': 'working', 'to': 'idle'}),
        ])

    def test_unsubscribed_turn_callbacks_do_not_queue_projection(self):
        """A universe that listens only to status events should never make transcript work."""
        edge = TurnStarted(turn='turn', ref='input', kind='prompt', source='person')
        with patch.object(events, 'subscribers', return_value=[]), \
                patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, 'launch')):
            runtime_update.run(status='working', turn_session_id='native',
                               turn_events_json=json.dumps([turn_events.encode(edge)]))
        self.assertEqual(read(self.agent / 'runtime.json')['status'], 'working')
        self.assertFalse(self.path.exists())

    def test_relay_projects_queued_end_before_retrying_missing_start_row(self):
        """A missing start row cannot use up the read window before a later batch gets its first read."""
        start = TurnStarted(turn='private-turn', ref='private-input', kind='prompt', source='person')
        end = TurnEnded(turn=start.turn, ref='private-end', kind='prompt', state='finished')
        last = TurnReference(turn='public-turn', ref='last-row', generation='gen')
        elapsed = 0.0

        snapshot = SimpleNamespace(reader=Mock())

        def project(root, directory, launch, native, generation, edges, deadline, previous, **options):
            return transcripts._TurnRead(snapshot, 'gen', None,
                                         tuple(last if edge == end else TurnReference() for edge in edges),
                                         pending_rows=start in edges)

        def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def stage(native, generation, edges):
            prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation)

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        relay.event_ready = Mock()
        relay._admit_event_transitions(stage, Mock(side_effect=[('native', (start,)), ('native', (end,)), None]), 0)
        with patch.object(rpc, 'time') as clock, patch.object(transcripts, 'time', clock), \
                patch.object(transcripts, '_read_turn_references', side_effect=project):
            clock.monotonic.side_effect = lambda: elapsed
            clock.sleep.side_effect = advance
            relay.stage_adapter_events()
            entries = self.drain()
        self.assertEqual(elapsed, .5)
        self.assertEqual([(kind, data['ref']) for kind, data in entries],
                         [('turn.started', None), ('turn.ended', 'last-row')])

    def test_switch_closes_a_witnessed_turn_and_rejects_old_generation_admission(self):
        """An away-and-back snapshot must not make a previously admitted start part of the current generation."""
        edge = TurnStarted(turn='private', kind='prompt', ref=None, source='person')
        prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (edge,), generation=0)
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'session_generation': 2})
        agents._session_event(self.agent, {'launch': 'launch', 'from': 'native', 'to': 'other', 'reason': 'clear'})
        prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (edge,), generation=0)
        with patch.object(transcripts, 'turn_references', return_value=(TurnReference(turn='public'),)):
            entries = self.drain()
        self.assertEqual([kind for kind, _ in entries], ['turn.started', 'turn.ended', 'agent.session'])
        self.assertEqual(entries[1][1], {'turn': None, 'ref': None, 'generation': None, 'resolved': False,
                                         'kind': 'prompt', 'state': 'unknown'})

    def test_stop_keeps_the_wait_while_accepting_the_turn_end(self):
        """Deferring a stopping launch's status must preserve the exact turn outcome it reports."""
        started = TurnStarted(turn='private', kind='prompt', ref=None, source='person')
        ending = TurnEnded(turn='private', kind='prompt', ref=None, state='interrupted')
        prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (started,), generation=0)
        prompt_events.stage(self.agent, 'launch', 'native',
                            (PromptOpened(id='approval', kind='permission', requires_waiting=True),))
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'ending', 'status': 'waiting'})
        agents.update_runtime(self.agent, 'launch', status='working')
        self.assertEqual(read(self.agent / 'runtime.json')['status'], 'waiting')
        prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (ending,), generation=0)
        agents.update_runtime(self.agent, 'launch', state='stopped', status='unknown')
        entries = self.drain()
        self.assertEqual([kind for kind, _ in entries],
                         ['turn.started', 'prompt.opened', 'turn.ended', 'prompt.closed', 'agent.status'])
        self.assertEqual(entries[2], ('turn.ended', {'turn': None, 'kind': 'prompt', 'state': 'interrupted',
                                                   'ref': None, 'generation': None, 'resolved': False}))

    def test_attribution_requires_one_scoped_attempt_and_respects_its_exact_item(self):
        """An ambiguous or stale delivery attempt can otherwise attribute the person's input to a plugin."""
        (self.agent / 'inbox.jsonl').touch()
        session = SimpleNamespace(agent_id='worker', launch_id='launch', native_id='native')
        edge = TurnStarted(turn='turn', kind='prompt', ref='item', source='person', attempt='wire')
        attempt = {'native_message_id': 'wire', 'mode': 'steer', 'agent_id': 'worker',
                   'launch_id': 'launch', 'native_session_id': 'native'}
        snapshot = {'message': {'attempts': [attempt]}}
        with patch.object(mailbox, 'delivery_observation_snapshot', return_value=snapshot):
            self.assertEqual(turn_events.attribution(self.agent, session, 0, (edge,), deadline=0), {0: 'message'})
            snapshot['other'] = {'attempts': [dict(attempt)]}
            self.assertEqual(turn_events.attribution(self.agent, session, 0, (edge,), deadline=0), {})
            snapshot.pop('other')
            attempt['delivery_receipt'] = {'value': {'evidence': 'native-user-item', 'turn_id': 'turn',
                                                      'item_id': 'earlier-item', 'client_message_id': 'wire'}}
            self.assertEqual(turn_events.attribution(self.agent, session, 0, (edge,), deadline=0), {})
            attempt['delivery_receipt']['value']['item_id'] = 'item'
            self.assertEqual(turn_events.attribution(self.agent, session, 0, (edge,), deadline=0), {0: 'message'})

    def test_turn_attribution_keeps_a_saved_attempt_after_brief_inbox_contention(self):
        """A concurrent inbox reader must not turn a proved delivery into a harness-origin input."""
        (self.agent / 'inbox.jsonl').write_text(json.dumps({
            'schema': 2, 'id': 'message', 'delivery': 'steer', 'text': 'fixture',
        }) + '\n')
        write(self.agent / 'inbox-state.json', {'message': {'submission_state': 'accepted', 'attempts': [{
            'native_message_id': 'wire', 'mode': 'steer', 'agent_id': 'worker',
            'launch_id': 'launch', 'native_session_id': 'native',
        }]}})
        read_snapshot = mailbox.delivery_observation_snapshot
        reads = 0

        def briefly_busy(directory):
            nonlocal reads
            reads += 1
            if reads == 1:
                with lock(directory / '.inbox.lock'):
                    return read_snapshot(directory)
            return read_snapshot(directory)

        edge = TurnJoined(turn='turn', ref='row', source='harness', attempt='wire')
        with patch.object(mailbox, 'delivery_observation_snapshot', side_effect=briefly_busy):
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (edge,), generation=0)
            [(kind, data)] = self.drain()
        self.assertEqual((kind, data['source'], data['message']), ('turn.joined', 'mv2', 'message'))
        self.assertEqual(reads, 2)

        late = TurnJoined(turn='turn', ref='late-row', source='harness', attempt='wire')
        with lock(self.agent / '.inbox.lock'), patch.object(prompt_events, '_TURN_ROW_WAIT', 0):
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (late,), generation=0)
            [(kind, data)] = self.drain()
        self.assertEqual((kind, data['source'], data['message']), ('turn.joined', 'harness', None))

        clock = SimpleNamespace(monotonic=Mock(side_effect=[10, 10.01]), sleep=Mock())
        with patch.object(turn_events, 'time', clock), \
                patch.object(prompt_events, 'time', SimpleNamespace(monotonic=lambda: 10)), \
                patch.object(prompt_events, '_TURN_ROW_WAIT', .005), \
                patch.object(mailbox, 'delivery_observation_snapshot',
                             side_effect=BlockingIOError()) as read_attempts:
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (
                TurnJoined(turn='turn', ref='expired-retry', source='harness', attempt='wire'),
            ), generation=0)
            [(_, data)] = self.drain()
        self.assertEqual(read_attempts.call_count, 1)
        self.assertEqual(data['source'], 'harness')
