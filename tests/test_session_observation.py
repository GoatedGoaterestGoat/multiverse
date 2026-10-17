"""Optional provider observations do not change legacy adapter requirements."""
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.types import SessionHandle, NativeResult, CommandSpec
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write, lock
from multiverse.universe import agents
from multiverse.tools import send
from multiverse.universe.mailbox import inbox
from test_multiverse import BaseTest


class SessionObservationTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.identity, self.old = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state' / 'agents' / self.identity
        self.properties = {'id': self.identity, 'name': 'worker', 'harness': 'pi', 'workspace': str(self.work),
                           'native_session_id': self.old, 'native_session_home': str(self.work),
                           'native_session_provider': 'fixture', 'universe_id': read(self.root / 'universe.json')['id'],
                           'task': ''}
        self.facts = {'launch_id': 'launch', 'state': 'running', 'pid': 123}
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', self.facts)

    def test_handle_keeps_process_and_storage_from_the_same_launch_snapshot(self):
        session = SessionHandle.from_records(self.properties, self.facts, self.directory)
        self.assertEqual((session.home, session.pid), (str(self.work), 123))
        self.assertIsNone(SessionHandle('id', 'pi', '/tmp').pid)

    def test_connection_problem_is_live_only_guarded_and_relay_error_wins(self):
        calls = []
        def problem(session):
            calls.append(session)
            return 'Native inbox disconnected; restart this agent'
        adapter = SimpleNamespace(connection_problem=problem)
        with patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(native, 'usage_snapshot', return_value={}), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            self.assertIn('inbox disconnected', lifecycle.listing(self.root)[0]['connection_error'])
            self.facts['native_relay'] = {'state': 'lost', 'reason': 'relay disconnected'}
            write(self.directory / 'runtime.json', self.facts)
            self.assertEqual(lifecycle.listing(self.root)[0]['connection_error'], 'relay disconnected')
            self.assertEqual(len(calls), 1)
        self.facts.pop('native_relay')
        write(self.directory / 'runtime.json', self.facts)
        with patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(native, 'usage_snapshot', return_value={}), \
                patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}):
            self.assertIsNone(lifecycle.listing(self.root)[0]['connection_error'])
            self.assertEqual(len(calls), 1)
        for adapter in (SimpleNamespace(), SimpleNamespace(connection_problem=lambda session: 1 / 0)):
            with patch.object(harnesses, 'load', return_value=adapter), \
                    patch.object(native, 'usage_snapshot', return_value={}), \
                    patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                row = lifecycle.listing(self.root)[0]
                self.assertEqual(row['name'], 'worker')
                self.assertIsNone(row['connection_error'])

    def test_native_use_reconciles_an_exact_observation_before_usage(self):
        from multiverse.harnesses.types import ObservedBinding
        target = str(uuid4())
        observed = ObservedBinding(native_id=target, home=str(self.work), evidence='fixture-process-file')
        adapter = SimpleNamespace(observed_binding=lambda session: observed,
                                  get_usage=lambda session: NativeResult('ok', value={'native_id': session.native_id}))
        with patch.object(harnesses, 'load', return_value=adapter):
            result = native.usage_snapshot(self.root, self.properties, self.facts)
        self.assertEqual(result['value']['native_id'], target)
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], target)
        self.assertIn('"reason": "detected"', (self.directory / 'sessions.jsonl').read_text())

    def test_first_message_after_session_change_targets_the_new_session(self):
        from multiverse.harnesses.types import ObservedBinding
        target = str(uuid4())
        adapter = SimpleNamespace(observed_binding=lambda session: ObservedBinding(
            native_id=target, home=str(self.work), evidence='fixture-process-file'))
        with patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(native, 'send_or_wake', return_value={'state': 'native_queued'}):
            result = send.run(self.root, agent=self.identity, text='after clear', sender={'name': 'user'})
        self.assertTrue(result['ok'])
        message, = inbox(self.directory, all_messages=True)['messages']
        self.assertEqual(message['target_session'], target)

    def test_unknown_evidence_never_changes_the_binding(self):
        for observed in (None, {'native_id': str(uuid4())}):
            adapter = SimpleNamespace(observed_binding=lambda session: observed,
                                      get_usage=lambda session: NativeResult('ok', value={'native_id': session.native_id}))
            with patch.object(harnesses, 'load', return_value=adapter):
                result = native.usage_snapshot(self.root, self.properties, self.facts)
            self.assertEqual(result['state'], 'ok' if observed is None else 'error')
            self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.old)

    def test_an_observer_bug_is_visible_without_erasing_the_listing_row(self):
        def broken(session):
            raise TypeError('bad native observation')
        adapter = SimpleNamespace(observed_binding=broken)
        with patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            row, = lifecycle.listing(self.root)
        self.assertEqual(row['name'], 'worker')
        self.assertEqual(row['connection_error'], 'bad native observation')

    def test_changed_generation_refuses_old_observer_without_writing(self):
        from multiverse.harnesses.types import ObservedBinding
        adapter = SimpleNamespace(observed_binding=lambda session: ObservedBinding(
            native_id=str(uuid4()), home=str(self.work), evidence='fixture-process-file'))
        write(self.directory / 'runtime.json', {**self.facts, 'launch_id': 'new'})
        with patch.object(harnesses, 'load', return_value=adapter):
            result = native.usage_snapshot(self.root, self.properties, self.facts)
        self.assertEqual(result['state'], 'error')
        self.assertIn('Launch changed', result['reason'])
        self.assertFalse((self.directory / 'sessions.jsonl').exists())

    def test_resume_rereads_binding_after_acquiring_launch_lock(self):
        target = str(uuid4())
        stale = dict(self.properties)
        agents.rebind_session(self.root, self.identity, self.old, target, 'clear', 'launch', home=str(self.work))
        seen = []
        def resume(session, context):
            seen.append(session.native_id)
            return CommandSpec(('/fake/program',))
        adapter = SimpleNamespace(name='pi', family='fixture', session_mode='assigned', default_model=None,
                                  bootstrap='', resume=resume)
        terminal = Mock()
        terminal.create.return_value = {'socket': '/fake', 'session': '$1', 'pane': '%1'}
        with patch.object(lifecycle, '_record', return_value=(self.directory, stale, 'pi', None)), \
                patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle.shutil, 'which', return_value='/fake/program'), \
                patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                patch.object(lifecycle, 'Tmux', return_value=terminal):
            result = lifecycle.start(self.root, resume=self.identity)
        self.assertEqual(seen, [target])
        self.assertEqual(result['native_session_id'], target)
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], target)

    def test_usage_does_not_wait_for_a_busy_launch_or_measure_the_old_session(self):
        from multiverse.harnesses.types import ObservedBinding
        target = str(uuid4())
        measured = Mock()
        adapter = SimpleNamespace(observed_binding=lambda session: ObservedBinding(
            native_id=target, home=str(self.work), evidence='fixture-process-file'), get_usage=measured)
        with lock(self.directory / '.launch.lock'), patch.object(harnesses, 'load', return_value=adapter):
            result = native.usage_snapshot(self.root, self.properties, self.facts)
        self.assertEqual(result['state'], 'error')
        self.assertIn('updating; retry', result['reason'])
        measured.assert_not_called()
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.old)
