"""A damaged runtime identity cannot hide a running launch."""
from copy import deepcopy
import os
from unittest.mock import patch

from multiverse import harnesses
from multiverse.runtime import attachments, lifecycle, missing_runtime, native
from multiverse.store import read, write, uid
from multiverse.tools import doctor, send
from multiverse.universe import records
from test_multiverse import BaseTest, TerminalBase, until


class RuntimeIdentityDamageTest(TerminalBase):
    def test_null_launch_identity_cannot_allow_a_duplicate_launch(self):
        from multiverse.runtime import lifecycle
        from multiverse.store import read, write
        first = lifecycle.start(self.root, 'damaged-launch', '', 'fake', socket_path=self.socket,
                                wait=True, wait_timeout=15)
        self.assertTrue(first['ready'], first)
        directory = self.root / 'state' / 'agents' / first['id']
        facts = read(directory / 'runtime.json')
        write(directory / 'runtime.json', {**facts, 'launch_id': None})
        try:
            outcome = lifecycle.start(self.root, resume='damaged-launch', socket_path=self.socket,
                                      wait=True, wait_timeout=15)
        except ValueError as error:
            print('null launch identity refused:', error, flush=True)
            return
        panes, error = self.backend.snapshot()
        self.assertIsNone(error)
        owned = [row for row in panes.values() if row.get('@mv2_agent_id') == first['id']]
        print('null launch identity resumed:', outcome.get('ready'), 'tagged panes:', len(owned), flush=True)
        self.assertLessEqual(len(owned), 1, 'a structurally damaged runtime must not hide its live terminal')

    def started(self):
        result = lifecycle.start(self.root, 'worker', '', 'fake', socket_path=self.socket,
                                 wait=True, wait_timeout=15)
        self.assertTrue(result['ready'], result)
        directory = self.root / 'state' / 'agents' / result['id']
        return result, directory, read(directory / 'runtime.json')

    def test_damaged_authority_fields_refuse_before_reconciliation_and_keep_the_file(self):
        result, directory, original = self.started()
        path = directory / 'runtime.json'
        self.addCleanup(write, path, original)
        missing = object()
        cases = [('launch_id', value) for value in (missing, None, '', [], 42, 'not-a-uuid')]
        cases += [('state', value) for value in (missing, None, [], 'invalid')]
        cases += [('tmux', value) for value in (missing, None, [], {})]
        cases += [('tmux.socket', value) for value in (missing, '', [], 42, 'relative.sock')]
        cases += [(field, value) for field in ('tmux.pane', 'tmux.session')
                  for value in (missing, None, '', [], 42, 'wrong-format')]
        cases += [('pid', True), ('runner_pid', []), ('pid_started', []), ('exit_code', '0')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                facts = deepcopy(original)
                container, key = (facts['tmux'], field.split('.')[1]) if '.' in field else (facts, field)
                if value is missing:
                    del container[key]
                else:
                    container[key] = value
                write(path, facts)
                before = path.read_bytes()
                with patch.object(native, 'session_snapshot', side_effect=AssertionError('no reconciliation with damaged authority')):
                    with self.assertRaisesRegex(records.RecordError, 'runtime.json.*running terminal'):
                        lifecycle.start(self.root, resume='worker', socket_path=self.socket)
                self.assertEqual(path.read_bytes(), before)
                row = lifecycle.listing(self.root, only=result['id'])[0]
                self.assertEqual(row['damaged']['file'], f'state/agents/{result["id"]}/runtime.json')
                self.assertEqual((row['name'], row['harness']), ('worker', 'fake'))
        self.assertTrue(any('worker' in problem and 'runtime.json' in problem
                            for problem in doctor.run(self.root)['problems']))
        panes, error = self.backend.snapshot()
        self.assertIsNone(error)
        self.assertEqual(sum(pane.get('@mv2_agent_id') == result['id'] for pane in panes.values()), 1)

    def test_explicit_kill_recovers_damaged_authority_without_touching_another_agent(self):
        result, directory, facts = self.started()
        other = lifecycle.start(self.root, 'other', '', 'fake', socket_path=self.socket, wait=True)
        write(directory / 'runtime.json', {**facts, 'launch_id': None, 'tmux': []})
        stopped = lifecycle.kill(self.root, 'worker', attachment_only=True)
        self.assertEqual(stopped['state'], 'dormant')
        self.assertIn('damaged launch authority', stopped['native_cancellation']['reason'])
        panes, error = self.backend.snapshot()
        self.assertIsNone(error)
        self.assertFalse(any(pane.get('@mv2_agent_id') == result['id'] for pane in panes.values()))
        self.assertTrue(any(pane.get('@mv2_agent_id') == other['id'] for pane in panes.values()))

    def test_resume_recovers_only_after_stop_and_reports_damaged_authority(self):
        result, directory, _ = self.started()
        lifecycle.kill(self.root, 'worker')
        stopped = read(directory / 'runtime.json')
        write(directory / 'runtime.json', {**stopped, 'launch_id': None})
        resumed = lifecycle.start(self.root, resume='worker', socket_path=self.socket, wait=True)
        self.assertTrue(resumed['ready'], resumed)
        self.assertNotEqual(resumed['runtime']['launch_id'], result['runtime']['launch_id'])
        self.assertIn('damaged launch authority', ' '.join(resumed.get('notices', [])))
        self.assertNotIn('was missing', ' '.join(resumed.get('notices', [])))

    def test_wake_checks_damaged_authority_before_reconciliation(self):
        _, directory, facts = self.started()
        path = directory / 'runtime.json'
        self.addCleanup(write, path, facts)
        write(path, {**facts, 'tmux': {'pane': None}})
        before = path.read_bytes()
        with patch.object(native, 'session_snapshot', side_effect=AssertionError('no reconciliation')):
            result = send.run(self.root, agent='worker', text='must not create another launch')
        self.assertFalse(result['ok'])
        self.assertIn('runtime.json', result['native_delivery']['reason'])
        self.assertIn('running terminal', result['native_delivery']['reason'])
        self.assertEqual(path.read_bytes(), before)

    def test_replacement_during_discovery_is_not_overwritten(self):
        _, directory, _ = self.started()
        lifecycle.kill(self.root, 'worker')
        original = read(directory / 'runtime.json')
        damaged = {**original, 'launch_id': None}
        write(directory / 'runtime.json', damaged)
        replacement = {**original, 'launch_id': uid()}
        def discovery(*args, **kwargs):
            write(directory / 'runtime.json', replacement)
            return ['independent discovery finished']
        with patch.object(missing_runtime, 'require_stopped', side_effect=discovery):
            with self.assertRaisesRegex(records.RecordError, 'changed during terminal discovery'):
                lifecycle.start(self.root, resume='worker', socket_path=self.socket)
        self.assertEqual(read(directory / 'runtime.json'), replacement)

    def test_supported_wake_can_recover_after_verified_stop(self):
        _, directory, _ = self.started()
        lifecycle.kill(self.root, 'worker')
        stopped = read(directory / 'runtime.json')
        write(directory / 'runtime.json', {**stopped, 'launch_id': None})
        adapter = harnesses.load(self.root, 'fake')
        adapter.resume_prompt_args = ('{task}',)
        with patch.object(harnesses, 'load', return_value=adapter), \
             patch.dict(os.environ, {'MV2_TMUX_SOCKET': self.socket}):
            result = send.run(self.root, agent='worker', text='wake after verified stop')
        self.assertTrue(result['ok'], result)
        self.assertIn('damaged launch authority', ' '.join(result.get('notices', [])))
        until(lambda: read(directory / 'runtime.json').get('startup_checked') is True)
        lifecycle.kill(self.root, 'worker')


class RuntimeAuthorityProofTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory, self.properties, _, _ = lifecycle._record(self.root, 'proof', '', 'codex', None, None)
        write(self.directory / 'properties.json', self.properties)

    def test_no_terminal_does_not_discard_live_or_unknown_process_evidence(self):
        started = 'Fri Oct 16 12:00:00 2026'
        for pid_key, start_key in (('pid', 'pid_started'), ('runner_pid', 'runner_started')):
            for token, observation in ((started, (False, (started, 'S'))),
                                       (None, (False, (started, 'S'))),
                                       ('damaged', (False, (started, 'S'))),
                                       (started, (False, None))):
                facts = {'launch_id': None, 'state': 'stopped', pid_key: 42, start_key: token}
                with self.subTest(pid=pid_key, token=token, observation=observation), \
                     patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
                     patch.object(attachments, '_observation', return_value=observation), \
                     patch.object(attachments, '_command', return_value='private fixture'):
                    with self.assertRaisesRegex(records.RecordError, 'runtime.json.*PID 42'):
                        missing_runtime.require_stopped(self.directory, self.properties, facts=facts)

    def test_absent_reused_and_zombie_processes_do_not_block_recovery(self):
        started = 'Fri Oct 16 12:00:00 2026'
        facts = {'launch_id': None, 'state': 'stopped', 'pid': 42, 'pid_started': started}
        for observation in ((True, None), (False, (started, 'Z')),
                            (False, ('Sat Oct 17 12:00:00 2026', 'S'))):
            with self.subTest(observation=observation), \
                 patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
                 patch.object(attachments, '_observation', return_value=observation):
                notices = missing_runtime.require_stopped(self.directory, self.properties, facts=facts)
                self.assertIn('damaged launch authority', notices[0])

    def test_an_unreachable_server_retained_in_damaged_facts_blocks_recovery(self):
        facts = {'launch_id': None, 'tmux': {'socket': '/recorded-runtime.sock'}}
        def snapshot(backend):
            return {}, ('Permission denied' if '/recorded-runtime.sock' in backend.prefix else None)
        with patch.object(missing_runtime.Tmux, 'snapshot', snapshot):
            with self.assertRaisesRegex(records.RecordError, 'runtime.json.*recorded-runtime.sock.*Permission denied'):
                missing_runtime.require_stopped(self.directory, self.properties, facts=facts)

    def test_healthy_and_minimal_preparation_authority_need_no_discovery(self):
        healthy = {'launch_id': uid(), 'state': 'running',
                   'tmux': {'socket': '/private.sock', 'session': '$1', 'pane': '%2'}}
        preparing = {'launch_id': uid(), 'state': 'starting', 'preparing': True}
        with patch.object(missing_runtime, 'launches', side_effect=AssertionError('no history scan')), \
             patch.object(missing_runtime.Tmux, 'snapshot', side_effect=AssertionError('no tmux query')):
            self.assertIsNone(missing_runtime.damage(self.directory, healthy))
            self.assertIsNone(missing_runtime.damage(self.directory, preparing))
        self.assertIsNone(missing_runtime.damage(self.directory, {}))

    def test_non_null_start_tokens_must_be_parseable(self):
        facts = {'launch_id': uid(), 'state': 'running',
                 'tmux': {'socket': '/private.sock', 'session': '$1', 'pane': '%2'}}
        for key in ('pid_started', 'runner_started'):
            for value in ('', 'damaged', '2026-10-17', []):
                with self.subTest(key=key, value=value):
                    self.assertIsNotNone(missing_runtime.damage(self.directory, {**facts, key: value}))
            self.assertIsNone(missing_runtime.damage(self.directory, {**facts, key: None}))
            self.assertIsNone(missing_runtime.damage(self.directory,
                {**facts, key: 'Sat Oct 17 12:00:00 2026'}))

    def test_observation_without_an_exit_is_not_legacy_process_reuse_proof(self):
        facts = {'launch_id': None, 'state': 'starting', 'pid': 42,
                 'observed_at': '2026-10-17T00:00:00+00:00'}
        with patch.object(attachments, '_observation', return_value=(False, ('Sat Oct 17 12:00:00 2026', 'S'))), \
             patch.object(attachments, '_command', return_value='private fixture'):
            self.assertIsNotNone(attachments.recorded_process_problem(self.directory, facts))
            self.assertIsNone(attachments.recorded_process_problem(self.directory, {**facts, 'exit_code': 0}))
