"""Optional provider observations do not change legacy adapter requirements."""
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.types import SessionHandle, NativeResult, CommandSpec, ObservedBinding
from multiverse.runtime import attachments, lifecycle, native
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
        self.launch = str(uuid4())
        self.facts = {'launch_id': self.launch, 'state': 'running', 'pid': 123,
                      'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}}
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
        agents.rebind_session(self.root, self.identity, self.old, target, 'clear', self.launch, home=str(self.work))
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

    def test_listing_uses_one_reconciled_snapshot_but_direct_usage_still_checks(self):
        calls = []
        def observe(session):
            calls.append(session.native_id)
            return None
        adapter = SimpleNamespace(observed_binding=observe,
                                  get_usage=lambda session: NativeResult('ok', value={'native_id': session.native_id}))
        with patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            [row] = lifecycle.listing(self.root)
            self.assertEqual(row['usage']['value']['native_id'], self.old)
            self.assertEqual(calls, [self.old])
            native.usage_snapshot(self.root, self.properties, self.facts)
            self.assertEqual(calls, [self.old, self.old])

    def test_listing_does_not_repeat_a_failed_identity_observation_for_usage(self):
        observed = Mock(side_effect=ValueError('damaged native evidence'))
        measured = Mock()
        adapter = SimpleNamespace(observed_binding=observed, get_usage=measured)
        with patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            [row] = lifecycle.listing(self.root)
        self.assertEqual(row['usage']['state'], 'error')
        self.assertEqual(row['usage']['reason'], 'damaged native evidence')
        observed.assert_called_once()
        measured.assert_not_called()

    def test_an_empty_observation_error_still_prevents_unreconciled_usage(self):
        adapter = SimpleNamespace(observed_binding=Mock(side_effect=ValueError()), get_usage=Mock())
        with patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            [row] = lifecycle.listing(self.root)
        self.assertEqual(row['usage']['state'], 'error')
        adapter.get_usage.assert_not_called()

    def test_public_send_cannot_reconcile_a_mismatched_terminal(self):
        token = 'Sat Oct 17 12:00:00 2026'
        self.facts.update(pid_started=token)
        write(self.directory / 'runtime.json', self.facts)
        target = str(uuid4())
        observed = []
        def observer(session):
            observed.append(session.native_id)
            return ObservedBinding(native_id=target, home=str(self.work), evidence='fixture-process-file')
        adapter = SimpleNamespace(observed_binding=observer)
        with patch.object(harnesses, 'load', return_value=adapter), \
             patch.object(native, 'observe', return_value={'live': 'dormant', 'reason': 'Recorded pane is absent'}), \
             patch.object(attachments, '_observation', return_value=(False, (token, 'S'))):
            result = send.run(self.root, agent=self.identity, text='must remain queued on the primary', sender={'name':'user'})
        print('SEND', {'ok': result.get('ok'), 'observer_calls': len(observed),
                       'original_id': self.old, 'saved_id': read(self.directory / 'properties.json')['native_session_id']}, flush=True)
        self.assertEqual(observed, [], 'public send must detect runtime mismatch before native identity reconciliation')
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.old)
        self.assertFalse((self.directory / 'sessions.jsonl').exists())

    def test_listing_refreshes_observation_for_new_reconciled_exit_facts(self):
        target = str(uuid4())
        exited = {**self.facts, 'state': 'stopped', 'exit_code': 0}
        changed = False
        def observer(session):
            nonlocal changed
            if not changed:
                changed = True
                # The runner's exit writer needs no launch lock and may run
                # while a provider identity observation is in progress.
                write(self.directory / 'runtime.json', exited)
            return ObservedBinding(native_id=target, home=str(self.work), evidence='fixture-process-file')
        adapter = SimpleNamespace(observed_binding=observer,
            get_usage=lambda session: NativeResult('ok', value={'native_id':session.native_id}))
        def terminal(properties, facts, snapshots=None):
            return {'live':'stopped' if 'exit_code' in facts else 'alive', 'reason':''}
        with patch.object(harnesses, 'load', return_value=adapter), \
             patch.object(lifecycle, 'observe', side_effect=terminal):
            row, = lifecycle.listing(self.root)
        print('LIST', {'live': row.get('live'), 'runtime_state':row.get('runtime',{}).get('state'),
                       'exit_code':row.get('runtime',{}).get('exit_code')}, flush=True)
        self.assertEqual(row['runtime']['exit_code'], 0, 'actual reconciliation returned newer exit facts')
        self.assertEqual(row['live'], 'stopped', 'terminal observation must describe returned runtime facts')

    def test_bad_properties_during_early_observation_do_not_abort_listing(self):
        properties = {k: v for k, v in self.properties.items() if k != 'universe_id'}
        write(self.directory / 'properties.json', properties)
        pane = {'pane_id': '%1', 'session_id': '$1', 'pane_pid': '123', 'pane_dead': '0',
                '@mv2_agent_id': self.identity,
                '@mv2_universe_id': 'private-universe', '@mv2_launch_id': self.launch}
        from multiverse.terminal.tmux import Tmux
        with patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)), \
             patch.object(harnesses, 'load', return_value=SimpleNamespace()):
            rows = lifecycle.listing(self.root)
        self.assertEqual(len(rows), 1)
        self.assertIn('damaged', rows[0], 'malformed records must remain isolated to their own row')

    def test_healed_terminal_recovery_drops_stale_dormant_observation(self):
        token = 'Sat Oct 17 12:00:00 2026'
        self.facts.update(pid_started=token)
        write(self.directory / 'runtime.json', self.facts)
        from multiverse.runtime import missing_runtime
        with patch.object(attachments, '_observation', return_value=(False, (token, 'S'))), \
             patch.object(lifecycle, 'observe', side_effect=[{'live': 'dormant'}, {'live': 'alive'}]), \
             patch.object(missing_runtime, 'observe', return_value={'live': 'alive'}), \
             patch.object(lifecycle, 'owned', side_effect=RuntimeError('normal owned terminal reached')) as owned:
            with self.assertRaisesRegex(RuntimeError, 'normal owned terminal reached'):
                lifecycle.kill(self.root, self.identity, attachment_only=True)
        owned.assert_called_once()
