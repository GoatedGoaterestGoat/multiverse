import os
import subprocess
import sys
from threading import Event, Thread
from unittest.mock import Mock, patch

from multiverse.harnesses.types import NativeResult, PromptClosed, PromptOpened, SessionHandle
from multiverse.plugins import agent_files, events
from multiverse.runtime import lifecycle, native, prompt_events, rpc, runner
from multiverse.store import read, write
from multiverse.tools import runtime_update
from multiverse.universe import agents
from test_multiverse import BaseTest


class PromptEventsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = self.root / 'state' / 'agents' / 'test-agent'
        self.path = self.agent / 'prompts' / 'journal' / 'events.json'
        self.real_emit = prompt_events._journal_event
        self.emit = self.enterContext(patch.object(prompt_events, '_journal_event', return_value='retry'))
        write(self.agent / 'properties.json', {'id': 'test-agent', 'native_session_id': 'first'})
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running'})

    def test_an_unobserved_open_is_synthesized_once_with_its_metadata(self):
        """A quick prompt can end before listing sees it, so a repeated close must add neither a duplicate nor a payload."""
        close, = prompt_events.parse(
            '[{"type":"closed","id":"q","kind":"question","state":"person","requires_waiting":false}]')
        for _ in range(2):
            prompt_events.record(self.agent, 'launch', 'first', (close,))
        events = read(self.path)['pending']
        self.assertEqual([(row['kind'], row['data']) for row in events], [
            ('prompt.opened', {'id': 'q', 'kind': 'question', 'requires_waiting': False}),
            ('prompt.closed', {'id': 'q', 'kind': 'question', 'state': 'person', 'cause': None}),
        ])
        self.assertEqual(len({row['key'] for row in events}), 2)
        with self.assertRaisesRegex(ValueError, '^Invalid prompt-event metadata$'):
            prompt_events.parse('[{"type":"opened","id":"q","kind":"question","requires_waiting":false,"text":"private"}]')

    def test_launch_and_conversation_switches_exclude_stale_edges(self):
        """A delayed edge from an old launch or conversation must not open or close the current prompt."""
        opened = PromptOpened(id='p', kind='permission', requires_waiting=True)
        closed = PromptClosed(id='p', kind='permission', state='person')
        prompt_events.record(self.agent, 'older', 'first', (opened,))
        self.assertFalse(self.path.exists())
        prompt_events.record(self.agent, 'launch', 'first', (opened,))
        write(self.agent / 'properties.json', {'id': 'test-agent', 'native_session_id': 'second'})
        prompt_events.record(self.agent, 'launch', 'first', (closed,))
        prompt_events.record(self.agent, 'launch', 'second', (opened,))
        prompt_events.finish(self.agent, 'launch', native_id='first', cause='switched')
        prompt_events.finish(self.agent, 'launch', native_id='second')
        events = read(self.path)['pending']
        self.assertEqual([(row['kind'], row['data'].get('cause')) for row in events], [
            ('prompt.opened', None), ('prompt.closed', 'switched'),
            ('prompt.opened', None), ('prompt.closed', 'stopped'),
        ])

    def test_wait_exit_fences_held_relay_edges_and_replays_them_in_order(self):
        """An admitted fast open and close must precede status even when the relay publisher is still staging."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'waiting'})
        entered, release, status_fence = Event(), Event(), Event()

        def stage(native_id, generation, edges):
            entered.set()
            if release.wait(5):
                prompt_events.stage(self.agent, 'launch', native_id, edges, generation=generation)

        relay = rpc.LaunchProxy({}, None, None, record_prompt_transitions=stage)
        relay.prompt_batches.append((stage, 'first', 0, (
            PromptOpened(id='p', kind='permission', requires_waiting=True),
            PromptClosed(id='p', kind='permission', state='person'),
        )))

        def fence(new_status):
            status_fence.set()
            relay.stage_status_events(new_status)

        watcher = runner.StatusWatcher(self.agent, 'launch', self.root, before_status_event=fence)
        publisher = Thread(target=relay.stage_adapter_events)
        status = Thread(target=watcher._publish, args=('working', 'first', 0))
        publisher.start()
        try:
            self.assertTrue(entered.wait(5))
            status.start()
            self.assertTrue(status_fence.wait(5))
            self.assertEqual(read(self.agent / 'runtime.json')['status'], 'working')
            self.emit.assert_not_called()
        finally:
            release.set()
            publisher.join(5)
            if status.ident is not None:
                status.join(5)
        self.assertFalse(publisher.is_alive() or status.is_alive())
        pending = read(self.path)['pending']
        self.assertEqual([row['kind'] for row in pending], ['prompt.opened', 'prompt.closed', 'agent.status'])
        self.emit.reset_mock()
        self.emit.side_effect = ['written', 'retry']
        prompt_events.publish(self.agent)
        self.assertEqual(read(self.path)['pending'], pending[1:])
        self.assertEqual([call.args[1] for call in self.emit.call_args_list], pending[:2])
        self.emit.reset_mock()
        self.emit.side_effect = None
        self.emit.return_value = 'written'
        prompt_events.publish(self.agent)
        self.assertEqual([call.args[1] for call in self.emit.call_args_list], pending[1:])
        self.assertEqual(read(self.path)['pending'], [])

    def test_large_batch_cannot_evict_its_own_replay_keys(self):
        """Losing a bulk acknowledgement must not replay an early open after its key falls out of the journal tail."""
        batch = tuple(PromptOpened(id='p' * 124 + f'{index:04}', kind='permission', requires_waiting=True)
                      for index in range(256))
        prompt_events.stage(self.agent, 'launch', 'first', batch)
        queued = read(self.path)['pending']
        self.emit.side_effect = self.real_emit
        with patch.object(events, 'subscribers', return_value=['fixture']):
            with patch.object(agent_files, 'write_json', side_effect=OSError('lost acknowledgement')):
                prompt_events.publish(self.agent)
            recorded, _ = events.read(self.root, 0)
            self.assertGreater(len(recorded), 0)
            self.assertEqual(read(self.path)['pending'], queued)
            prompt_events.publish(self.agent)
        recorded, _ = events.read(self.root, 0)
        self.assertEqual([item['data']['id'] for item in recorded], [item.id for item in batch])
        self.assertEqual(read(self.path)['pending'], [])
        self.assertEqual(read(events.directory(self.root) / events.RECEIPTS), {})

    def test_status_staging_does_not_wait_for_a_held_journal_append(self):
        """A slow writer may hold publication ownership without blocking the next runtime callback's staging."""
        opened = PromptOpened(id='p', kind='permission', requires_waiting=True)
        prompt_events.stage(self.agent, 'launch', 'first', (opened,))
        entered, release, wrote = Event(), Event(), Event()

        def append(*args):
            entered.set()
            release.wait(5)
            return 'retry'

        def callback():
            runtime_update.run(status='waiting', prompt_session_id='first', prompt_events_json=(
                '[{"type":"opened","id":"q","kind":"permission","requires_waiting":true}]'))
            wrote.set()

        self.emit.side_effect = append
        publisher = Thread(target=prompt_events.publish, args=(self.agent,))
        status = Thread(target=callback)
        with patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, 'launch')):
            publisher.start()
            try:
                self.assertTrue(entered.wait(5))
                status.start()
                self.assertTrue(wrote.wait(3))
                self.assertEqual(read(self.agent / 'runtime.json')['status'], 'waiting')
                self.assertEqual(list(read(self.path)['open']), ['p', 'q'])
            finally:
                release.set()
                publisher.join(5)
                if status.ident is not None:
                    status.join(5)

    def test_wait_exit_checks_exact_receipts_and_keeps_unseen_closes_late(self):
        """Unknown catalog coverage must not hide an ended prompt or invent closure for an unseen or retained one."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'waiting'})
        prompt_events.stage(self.agent, 'launch', 'first', (
            PromptOpened(id='closed', kind='permission', requires_waiting=True),
            PromptOpened(id='unseen', kind='permission', requires_waiting=True),
            PromptOpened(id='retained', kind='question', requires_waiting=False),
        ))
        session = SessionHandle('test-agent', 'fake', str(self.work), launch_id='launch', native_id='first')
        executor = Mock(facts={'session_generation': 0})
        adapter = Mock()
        adapter.prompt_outcome = Mock(side_effect=lambda session, answer, executor: NativeResult('ok', value=(
            {'closed': True, 'state': 'submitted', 'cause': None} if answer.id == 'closed' else {'state': 'submitted'}))
        )
        with patch.object(native, 'context', return_value=(self.agent, adapter, session, executor)):
            agents.update_runtime(self.agent, 'launch', status='working')
        calls = [call.args[1] for call in adapter.prompt_outcome.call_args_list]
        self.assertEqual([(value.id, value.metadata_only) for value in calls], [('closed', True), ('unseen', True)])
        self.assertEqual(list(read(self.path)['open']), ['unseen', 'retained'])
        prompt_events.record(self.agent, 'launch', 'first', (
            PromptClosed(id='unseen', kind='permission', state='person'),))
        self.assertEqual([(item['kind'], item['data'].get('id')) for item in read(self.path)['pending']], [
            ('prompt.opened', 'closed'), ('prompt.opened', 'unseen'), ('prompt.opened', 'retained'),
            ('prompt.closed', 'closed'), ('agent.status', None), ('prompt.closed', 'unseen'),
        ])

    def test_stopping_session_closes_its_prompts_before_its_wait_status_ends(self):
        """A stop must close the prompt before leaving waiting without closing one whose cancellation failed."""
        facts = self.agent / 'runtime.json'
        write(facts, {**read(facts), 'status': 'waiting'})
        prompt_events.stage(self.agent, 'launch', 'first', (
            PromptOpened(id='fallback', kind='permission', requires_waiting=True),
            PromptOpened(id='known', kind='permission', requires_waiting=True),
            PromptOpened(id='retained', kind='question', requires_waiting=False),
        ))
        live = {'live': 'alive'}
        backend = Mock()
        backend.signal_child.side_effect = lambda *args: live.update(live='dormant')

        def refuse_stop(root, reference):
            self.assertEqual(read(facts)['state'], 'ending')
            with patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, 'launch')):
                runtime_update.run(status='unknown', prompt_session_id='first', prompt_events_json=(
                    '[{"type":"closed","id":"known","kind":"permission","state":"person","cause":"person"}]'))
                runtime_update.run(status='working')
            self.assertEqual(read(facts)['status'], 'waiting')
            raise RuntimeError('cancellation refused')

        with patch.object(lifecycle, 'observe', return_value=live), \
                patch.object(lifecycle, 'owned', return_value=(backend, '%1')), \
                patch.multiple(lifecycle.missing_runtime, damage=Mock(return_value=None), mismatch=Mock(return_value=None)), \
                patch.multiple(lifecycle.attachments, wait=Mock(), problem=Mock(return_value=None)), \
                patch.object(native, 'stop', side_effect=refuse_stop) as cancel:
            with self.assertRaisesRegex(RuntimeError, 'cancellation refused'):
                lifecycle.kill(self.root, self.agent.name)
            self.assertEqual(read(facts)['state'], 'running')
            self.assertEqual(read(facts)['status'], 'working')
            self.assertNotIn('deferred_stop_status', read(facts))
            after_refusal = read(self.path)
            self.assertEqual(list(after_refusal['open']), ['fallback', 'retained'])
            self.assertEqual([(row['kind'], row['data']) for row in after_refusal['pending'][3:]], [
                ('prompt.closed', {'id': 'known', 'kind': 'permission', 'state': 'person', 'cause': 'person'}),
                ('agent.status', {'from': 'waiting', 'to': 'working'}),
            ])
            agents.update_runtime(self.agent, 'launch', status='waiting')
            before_stop = read(self.path)

            def harness_status(root, reference):
                self.assertEqual(read(facts)['state'], 'ending')
                with patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, 'launch')):
                    runtime_update.run(status='working')
                    runtime_update.run(status='unknown')
                    self.assertNotIn('deferred_stop_status', read(facts))
                    runtime_update.run(status='idle')
                self.assertEqual(read(facts)['status'], 'waiting')
                self.assertEqual(read(self.path), before_stop)
                return NativeResult('ok')

            cancel.side_effect = harness_status
            lifecycle.kill(self.root, self.agent.name)
        self.assertNotIn('deferred_stop_status', read(facts))
        pending = read(self.path)['pending'][len(before_stop['pending']):]
        self.assertEqual([row['kind'] for row in pending],
                         ['prompt.closed', 'prompt.closed', 'agent.status', 'agent.killed'])
        self.assertEqual(pending[-2]['data'], {'from': 'waiting', 'to': 'unknown'})
        self.assertEqual(pending[0]['data'], {'id': 'fallback', 'kind': 'permission',
                                             'state': 'gone', 'cause': 'stopped'})
        self.assertEqual(pending[1]['data'], {'id': 'retained', 'kind': 'question',
                                             'state': 'gone', 'cause': 'stopped'})

    def test_a_delayed_admission_cannot_follow_the_same_conversation_back(self):
        """The same native ID after an away-and-back navigation must not admit the old generation's buffered edges."""
        facts_path = self.agent / 'runtime.json'
        write(facts_path, {**read(facts_path), 'session_generation': 2})
        edge = PromptOpened(id='stale', kind='permission', requires_waiting=True)
        prompt_events.stage(self.agent, 'launch', 'first', (edge,), generation=0)
        self.assertFalse(self.path.exists())
        prompt_events.stage(self.agent, 'launch', 'first', (edge,), generation=2)
        self.assertEqual(list(read(self.path)['open']), ['stale'])

    def test_a_deferred_adapter_drain_keeps_its_admission_generation(self):
        """Backpressure must not relabel an adapter's old buffered edge after returning to the same conversation."""
        stage = Mock()
        relay = rpc.LaunchProxy({}, None, None, record_prompt_transitions=stage)
        relay.MAX_EVENT_BATCHES = 1
        relay.event_ready = Mock()
        edge = (PromptOpened(id='q', kind='permission', requires_waiting=True),)
        drain = Mock(return_value=('first', edge))
        relay._admit_event_transitions(stage, drain, 0)
        self.assertIn(stage, relay.event_drains)
        relay.stage_adapter_events()
        stage.assert_called_once_with('first', 0, edge)
        relay.event_generation = 2
        relay._retry_event_drains()
        self.assertEqual(drain.call_count, 1)
        self.assertFalse(relay.prompt_batches)

    def test_callback_closure_uses_the_exact_saved_answer_before_status(self):
        """A terminal closure can arrive while an mv2 answer is settling and must report that saved outcome."""
        write(self.agent / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'waiting'})
        write(self.agent / 'prompts' / 'p.json', {
            'schema': 1, 'id': 'p', 'agent': 'test-agent',
            'session': {'agent_id': 'test-agent', 'harness': 'fake', 'workspace': str(self.work),
                        'launch_id': 'launch', 'native_id': 'first'},
            'result': {'id': 'p', 'state': 'submitted', 'cause': None},
        })
        with patch.object(runtime_update, 'callback', return_value=(self.root, self.agent, 'launch')):
            runtime_update.run(status='working', prompt_session_id='first', prompt_events_json=(
                '[{"type":"closed","id":"p","kind":"permission","state":"gone","cause":"resolved"}]'))
        pending = read(self.path)['pending']
        self.assertEqual([row['kind'] for row in pending], ['prompt.opened', 'prompt.closed', 'agent.status'])
        self.assertEqual(pending[1]['data'], {'id': 'p', 'kind': 'permission', 'state': 'submitted', 'cause': None})

    def test_bad_prompt_metadata_does_not_discard_a_valid_status(self):
        """Metadata from the previous conversation must be dropped without stopping current launch status."""
        valid = '[{"type":"opened","id":"p","kind":"permission","requires_waiting":true}]'
        command = [sys.executable, '-m', runtime_update.__name__, '-u', str(self.root), '--agent', self.agent.name]
        cases = (
            ('working', ['--prompt-session-id', 'previous', '--prompt-events-json', valid], 'another conversation'),
            ('idle', ['--prompt-session-id', 'first', '--prompt-events-json', 'private text'], 'invalid prompt-event'),
            ('working', ['--prompt-events-json', valid], 'incomplete prompt-event'),
        )
        for status, arguments, warning in cases:
            with self.subTest(warning=warning):
                result = subprocess.run([*command, '--status', status, *arguments], capture_output=True,
                                        env={**os.environ, 'MV2_LAUNCH_ID': 'launch'}, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(warning, result.stderr)
                self.assertNotIn('private text', result.stderr + result.stdout)
                self.assertNotIn(valid, result.stderr + result.stdout)
                self.assertEqual(read(self.agent / 'runtime.json')['status'], status)
                self.assertFalse(self.path.exists())
        old = subprocess.run([*command, '--status', 'idle'], capture_output=True,
                             env={**os.environ, 'MV2_LAUNCH_ID': 'old'}, text=True, timeout=15)
        self.assertEqual(old.returncode, 1, old.stderr)
        self.assertIn('Outdated launch', old.stderr)
        self.assertEqual(read(self.agent / 'runtime.json')['status'], 'working')
