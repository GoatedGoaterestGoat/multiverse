import json
from threading import Event, Thread
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from multiverse.harnesses.types import PromptOpened, TurnEnded, TurnJoined, TurnStarted
from multiverse.runtime import prompt_events, rpc, runner, transcripts, turn_events
from multiverse.runtime.transcripts import TurnReference
from multiverse.store import lock, read, write
from multiverse.tools import runtime_update
from multiverse.universe import agents, mailbox
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
        self.enterContext(patch.object(prompt_events, '_journal_event', return_value='retry'))

    def edges(self):
        return [(row['kind'], row['data']) for row in read(self.path)['pending']]

    def test_admitted_turn_end_precedes_idle_with_only_public_reference(self):
        """A status can overtake a staged end or leak the native turn ID instead of its public cursor."""
        reported = '2026-01-01T00:00:00+00:00'
        with patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, 'launch')), \
                patch.object(agents, '_event') as emit, \
                patch.object(agents, 'now', return_value=reported), \
                patch.object(transcripts, 'turn_references',
                             return_value=(TurnReference(turn='public-turn', ref='input-row', generation='gen'),)):
            runtime_update.run(status='working', turn_session_id='native', turn_events_json=json.dumps([{
                'type': 'started', 'turn': 'private-turn', 'ref': 'private-input', 'kind': 'prompt', 'source': 'person',
            }]))
        emit.assert_called_once_with(self.agent, 'agent.status', 'launch', {'from': 'idle', 'to': 'working'})
        self.assertEqual(read(self.agent / 'runtime.json')['status_changed_at'], reported)

        def stage(native, generation, edges, *, deadline):
            prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation,
                                      deadline=deadline)
        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        relay.event_ready = Mock()
        end = TurnEnded(turn='private-turn', kind='prompt', ref='private-end', state='finished')
        relay._admit_event_transitions(stage, Mock(side_effect=[('native', (end,)), None]), 0)
        watcher = runner.StatusWatcher(self.agent, 'launch', self.root, before_status_event=relay.stage_status_events)
        with patch.object(transcripts, 'turn_references',
                          return_value=(TurnReference(turn='public-turn', ref='last-row', generation='gen'),)):
            watcher._publish('idle', 'native', 0)
        self.assertEqual(self.edges(), [
            ('turn.started', {'turn': 'public-turn', 'ref': 'input-row', 'generation': 'gen', 'kind': 'prompt',
                              'source': 'person', 'message': None, 'first_task': False}),
            ('turn.ended', {'turn': 'public-turn', 'ref': 'last-row', 'generation': 'gen',
                            'kind': 'prompt', 'state': 'finished'}),
            ('agent.status', {'from': 'working', 'to': 'idle'}),
        ])

    def test_active_relay_status_publishes_while_a_turn_row_is_pending(self):
        """The relay's turn proof can hold its own stage lock while the working event is recorded."""
        entered, release, published = Event(), Event(), Event()
        start = TurnStarted(turn='private-turn', ref='private-input', kind='prompt', source='person')

        def stage(native, generation, edges, *, deadline):
            entered.set()
            if release.wait(5):
                prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation,
                                          deadline=deadline)

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        relay.event_ready = Mock()
        relay._admit_event_transitions(stage, Mock(side_effect=[('native', (start,)), None]), 0)
        watcher = runner.StatusWatcher(self.agent, 'launch', self.root, before_status_event=relay.stage_status_events)
        worker = Thread(target=relay.stage_adapter_events)
        working = Thread(target=watcher._publish, args=('working', 'native', 0))
        reference = TurnReference(turn='turn', ref='input-row', generation='gen')
        with patch.object(agents, '_event', side_effect=lambda *args: published.set()) as emit, \
                patch.object(transcripts, 'turn_references', return_value=(reference,)):
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                working.start()
                self.assertTrue(published.wait(3))
                self.assertFalse(self.path.exists())
                emit.assert_called_once_with(self.agent, 'agent.status', 'launch', {'from': 'idle', 'to': 'working'})
            finally:
                release.set()
                worker.join(5)
                if working.ident is not None:
                    working.join(5)
        self.assertFalse(worker.is_alive() or working.is_alive())
        self.assertEqual(self.edges()[0][0], 'turn.started')

    def test_relay_projects_queued_end_before_retrying_missing_start_row(self):
        """A missing start row cannot use up the read window before a later batch gets its first read."""
        start = TurnStarted(turn='private-turn', ref='private-input', kind='prompt', source='person')
        end = TurnEnded(turn=start.turn, ref='private-end', kind='prompt', state='finished')
        last = TurnReference(turn='public-turn', ref='last-row', generation='gen')
        elapsed = 0.0

        loaded = SimpleNamespace(snapshot=SimpleNamespace(reader=Mock()))

        def project(root, directory, launch, native, generation, edges, deadline, previous):
            return transcripts._TurnRead(loaded, tuple(last if edge is end else TurnReference() for edge in edges),
                                         pending_rows=start in edges)

        def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def stage(native, generation, edges, *, deadline):
            prompt_events.stage_turns(self.root, self.agent, 'launch', native, edges, generation=generation,
                                      deadline=deadline)

        relay = rpc.LaunchProxy({}, None, None, record_turn_transitions=stage)
        relay.event_ready = Mock()
        relay._admit_event_transitions(stage, Mock(side_effect=[('native', (start,)), ('native', (end,)), None]), 0)
        with patch.object(rpc, 'time') as clock, patch.object(transcripts, 'time', clock), \
                patch.object(transcripts, '_read_turn_references', side_effect=project):
            clock.monotonic.side_effect = lambda: elapsed
            clock.sleep.side_effect = advance
            relay.stage_adapter_events()
        self.assertEqual(elapsed, .5)
        self.assertEqual([(kind, data['ref']) for kind, data in self.edges()],
                         [('turn.started', None), ('turn.ended', 'last-row')])

    def test_switch_closes_a_witnessed_turn_and_rejects_old_generation_admission(self):
        """An away-and-back snapshot must not make a previously admitted start part of the current generation."""
        edge = TurnStarted(turn='private', kind='prompt', ref=None, source='person')
        with patch.object(transcripts, 'turn_references', return_value=(TurnReference(turn='public'),)):
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (edge,), generation=0)
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'session_generation': 2})
        agents._session_event(self.agent, {'launch': 'launch', 'from': 'native', 'to': 'other', 'reason': 'clear'})
        prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (edge,), generation=0)
        self.assertEqual([kind for kind, _ in self.edges()], ['turn.started', 'turn.ended', 'agent.session'])
        self.assertEqual(self.edges()[1][1], {'turn': 'public', 'ref': None, 'generation': None,
                                             'kind': 'prompt', 'state': 'unknown'})

    def test_stop_keeps_the_wait_while_accepting_the_turn_end(self):
        """Deferring a stopping launch's status must preserve the exact turn outcome it reports."""
        started = TurnStarted(turn='private', kind='prompt', ref=None, source='person')
        ending = TurnEnded(turn='private', kind='prompt', ref=None, state='interrupted')
        with patch.object(transcripts, 'turn_references', return_value=(TurnReference(turn='public'),)):
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (started,), generation=0)
            prompt_events.stage(self.agent, 'launch', 'native',
                                (PromptOpened(id='approval', kind='permission', requires_waiting=True),))
            write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'ending', 'status': 'waiting'})
            agents.update_runtime(self.agent, 'launch', status='working')
            self.assertEqual([kind for kind, _ in self.edges()], ['turn.started', 'prompt.opened'])
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (ending,), generation=0)
        self.assertEqual(self.edges()[-1], ('turn.ended', {'turn': 'public', 'kind': 'prompt', 'state': 'interrupted',
                                                         'ref': None, 'generation': None}))
        agents.update_runtime(self.agent, 'launch', state='stopped', status='unknown')
        self.assertEqual([kind for kind, _ in self.edges()],
                         ['turn.started', 'prompt.opened', 'turn.ended', 'prompt.closed', 'agent.status'])

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
        kind, data = self.edges()[-1]
        self.assertEqual((kind, data['source'], data['message']), ('turn.joined', 'mv2', 'message'))
        self.assertEqual(reads, 2)

        late = TurnJoined(turn='turn', ref='late-row', source='harness', attempt='wire')
        with lock(self.agent / '.inbox.lock'):
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (late,), generation=0, deadline=0)
        kind, data = self.edges()[-1]
        self.assertEqual((kind, data['source'], data['message']), ('turn.joined', 'harness', None))

        clock = SimpleNamespace(monotonic=Mock(side_effect=[10, 10.01]), sleep=Mock())
        with patch.object(turn_events, 'time', clock), \
                patch.object(mailbox, 'delivery_observation_snapshot',
                             side_effect=BlockingIOError()) as read_attempts:
            prompt_events.stage_turns(self.root, self.agent, 'launch', 'native', (
                TurnJoined(turn='turn', ref='expired-retry', source='harness', attempt='wire'),
            ), generation=0, deadline=10.005)
        self.assertEqual(read_attempts.call_count, 1)
        self.assertEqual(self.edges()[-1][1]['source'], 'harness')
