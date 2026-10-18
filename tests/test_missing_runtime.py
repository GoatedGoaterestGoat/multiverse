"""Missing-runtime safety through private tmux and actual CLI records."""
import json
from qa.test_damaged_lifecycle import LifecycleFixture


class MissingRuntimeTest(LifecycleFixture):
    def started(self):
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        row = self.agents()['a']
        directory = self.root / 'state' / 'agents' / row['id']
        self.original = (directory / 'runtime.json').read_bytes()
        facts = json.loads(self.original)
        cleanup = json.loads((directory / 'launches' / facts['launch_id'] / 'cleanup.json').read_text())
        self.assertEqual(cleanup['child_pid'], facts['pid'])
        self.assertEqual(cleanup['child_started'], facts['pid_started'])
        (directory / 'runtime.json').unlink()
        return row, directory

    def test_missing_runtime_kill_closes_only_tagged_terminal(self):
        row, directory = self.started()
        self.mv2('spawn', '--name', 'other', '--harness', 'sleeper', '--wait')
        answer = self.mv2('kill', row['id'], '--json', check=False)
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assertEqual(self.agents()['other']['live'], 'alive')
        self.assertEqual(self.agents()['a']['live'], 'dormant')
        self.assertIn('runtime.json', json.loads(answer.stdout)['native_cancellation']['reason'])

    def test_missing_runtime_live_resume_refuses_and_doctor_names_file(self):
        row, directory = self.started()
        result = self.mv2('resume', 'a', check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('runtime.json', result.stdout + result.stderr)
        self.assertIn('running terminal', result.stdout + result.stderr)
        doctor = self.mv2('doctor', check=False)
        self.assertIn('runtime.json', doctor.stdout + doctor.stderr)
        self.assertEqual(self.agents()['a']['name'], 'a')
        self.assertEqual(self.agents()['a']['harness'], 'sleeper')
        self.assertEqual(self.agents()['a']['damaged']['recover'], 'kill')
        (directory / 'runtime.json').write_bytes(self.original)

    def test_missing_runtime_absent_terminal_can_resume_with_notice(self):
        row, directory = self.started()
        # A completed launch's real cleanup records prove its former processes gone.
        (directory / 'runtime.json').write_bytes(self.original)
        self.mv2('kill', 'a')
        (directory / 'runtime.json').unlink()
        result = json.loads(self.mv2('resume', 'a', '--wait', '--json').stdout)
        self.assertTrue(result['ready'])
        self.assertIn('runtime.json was missing', ' '.join(result.get('notices', [])))
        self.assertNotEqual(result['runtime']['launch_id'], row['runtime']['launch_id'])


from unittest.mock import patch, Mock
from multiverse.runtime import missing_runtime, attachments, lifecycle
from multiverse.store import uid, write
from multiverse.universe import agents, records
from test_multiverse import BaseTest


class DiscoveryTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory, self.properties, _, _ = lifecycle._record(self.root, 'lost', '', 'codex', None, None)
        write(self.directory / 'properties.json', self.properties)
        self.launch = self.directory / 'launches' / uid()
        self.request = {'env': {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': self.directory.name,
            'MV2_AGENT_DIR': str(self.directory), 'MV2_LAUNCH_ID': self.launch.name,
            'MV2_TMUX_SOCKET': '/recorded.sock',
            'MV2_CONTROL_SOCKET': '/tmp/mv2-native-' + self.launch.name + '/rpc.sock'}, 'native_mode': {'mode': 'bridge'},
            'attachment': {'work_owner': 'service'}, 'shared_service': True,
            'rpc_proxy': {'socket_path': '/tmp/mv2-native-' + self.launch.name + '/rpc.sock',
                          'upstream_path': '/shared.sock', 'binds_session': True},
            'control_endpoint': {'transport': 'unix-websocket', 'address': '/shared.sock'}}
        write(self.launch / 'request.json', self.request)
        self.pane = {'pane_id': '%2', 'session_id': '$1', 'pane_pid': '1234', 'pane_dead': '0',
            '@mv2_agent_id': self.directory.name, '@mv2_universe_id': self.properties['universe_id'],
            '@mv2_launch_id': self.launch.name}

    def test_known_servers_are_checked_and_unknown_servers_refuse_by_name(self):
        calls = []
        def snapshot(backend):
            calls.append(backend.prefix)
            return {}, ('Permission denied' if '/recorded.sock' in backend.prefix else None)
        with patch.object(missing_runtime.Tmux, 'snapshot', snapshot), self.assertRaisesRegex(
                records.RecordError, 'runtime.json.*recorded.sock.*Permission denied'):
            missing_runtime.require_stopped(self.directory, self.properties)
        self.assertTrue(any('/recorded.sock' in call for call in calls))

    def test_foreign_tags_are_ignored_and_multiple_exact_panes_refuse(self):
        for field in ('@mv2_agent_id', '@mv2_universe_id'):
            with self.subTest(field=field), patch.object(missing_runtime.Tmux, 'snapshot',
                    return_value=({'%2': {**self.pane, field: uid()}}, None)):
                self.assertEqual(missing_runtime.require_stopped(self.directory, self.properties),
                                 [missing_runtime.NOTICE])
        other = {**self.pane, 'pane_id': '%3', 'pane_pid': '1235'}
        with patch.object(missing_runtime.Tmux, 'snapshot', return_value=({'%2': self.pane, '%3': other}, None)),                 self.assertRaisesRegex(records.RecordError, 'multiple tagged terminals'):
            missing_runtime.kill(self.root, self.directory)
        self.assertFalse((self.directory / 'runtime.json').exists())

    def test_bad_or_missing_request_does_not_invent_a_server(self):
        path = self.launch / 'request.json'
        for content in ('[]', '{"env":[]}', '{"env":{"MV2_TMUX_SOCKET":5}}'):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(records.RecordError):
                missing_runtime.discover(self.directory, self.properties)
        path.unlink()
        with self.assertRaisesRegex(records.RecordError, 'request.json'):
            missing_runtime.discover(self.directory, self.properties)

    def test_active_cleanup_uses_process_identity_instead_of_pid_guessing(self):
        write(self.launch / 'cleanup.json', {'schema': 1, 'launch_id': self.launch.name,
              'state': 'active', 'pid': 42, 'pid_started': 'Fri Oct 16 12:00:00 2026'})
        with patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)),                 patch.object(attachments.os, 'kill', return_value=None), \
                patch.object(attachments, 'process_snapshot', return_value=('Fri Oct 16 12:00:00 2026', 'S')),                 patch.object(attachments, '_command', return_value='runner'):
            with self.assertRaisesRegex(records.RecordError, 'PID 42.*runner'):
                missing_runtime.require_stopped(self.directory, self.properties)
        for observation in ((False, ('Fri Oct 16 12:00:00 2026', 'Z')),
                            (False, ('Sat Oct 17 12:00:00 2026', 'S')), (True, None)):
            with self.subTest(observation=observation),                     patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)),                     patch.object(attachments, '_observation', return_value=observation):
                self.assertEqual(missing_runtime.require_stopped(self.directory, self.properties),
                                 [missing_runtime.NOTICE])

    def test_only_exact_bridge_request_yields_private_relay_never_shared(self):
        adapter = Mock(family='codex')
        terminal = {'socket': '/recorded.sock', 'pane': '%2', 'session': '$1'}
        facts = missing_runtime._qualified_facts(self.root, self.directory, self.properties, terminal,
                                                self.launch.name, self.request, adapter)
        self.assertEqual(facts['control_endpoint']['address'], self.request['rpc_proxy']['socket_path'])
        self.assertNotIn('native_service', facts)
        self.assertNotIn('pid', facts)
        bad = [
            {**self.request, 'native_mode': {'mode': 'plain'}},
            {**self.request, 'rpc_proxy': None},
            {**self.request, 'rpc_proxy': []},
            {**self.request, 'attachment': []},
            {**self.request, 'shared_service': False},
            {**self.request, 'env': {**self.request['env'], 'MV2_LAUNCH_ID': uid()}},
            {**self.request, 'rpc_proxy': {**self.request['rpc_proxy'], 'upstream_path': '/different.sock'}},
            {**self.request, 'rpc_proxy': {**self.request['rpc_proxy'], 'socket_path': '/shared.sock'}},
            {**self.request, 'control_endpoint': {'transport': 'unix-json', 'address': '/shared.sock'}},
        ]
        for request in bad:
            with self.subTest(request=request):
                facts = missing_runtime._qualified_facts(self.root, self.directory, self.properties, terminal,
                                                        self.launch.name, request, adapter)
                self.assertNotIn('control_endpoint', facts)

    def test_stop_keeps_partial_counts_and_uses_only_private_relay(self):
        from multiverse.harnesses.types import NativeResult
        self.properties['workspace'] = str(self.work)
        self.properties['native_session_id'] = uid()
        write(self.directory / 'properties.json', self.properties)
        open_pane = True
        def snapshot(backend):
            return ({'%2': self.pane} if open_pane else {}), None
        def run(backend, *args):
            nonlocal open_pane
            self.assertEqual(args, ('kill-pane', '-t', '%2'))
            open_pane = False
        def stop(session, executor):
            executor.check_session()
            self.assertIsNone(session.service)
            self.assertEqual(session.endpoint.address, self.request['rpc_proxy']['socket_path'])
            return NativeResult('unknown', 'native final cleanup unavailable', {'cancelled_turns': 2})
        adapter = Mock(family='codex', stop=stop)
        with patch.object(missing_runtime.Tmux, 'snapshot', snapshot),                 patch.object(missing_runtime.Tmux, 'run', run),                 patch.object(missing_runtime.harnesses, 'load', return_value=adapter):
            result = missing_runtime.kill(self.root, self.directory)
        self.assertEqual(result['native_cancellation']['cancelled_turns'], 2)
        self.assertEqual(result['native_cancellation']['state'], 'unknown')
        self.assertFalse(open_pane)

    def test_late_fallback_request_cannot_cancel_through_old_relay(self):
        from multiverse.harnesses.types import NativeResult
        self.properties['workspace'] = str(self.work)
        write(self.directory / 'properties.json', self.properties)
        open_pane = True
        def snapshot(backend):
            return ({'%2': self.pane} if open_pane else {}), None
        def run(backend, *args):
            nonlocal open_pane
            open_pane = False
        def stop(session, executor):
            write(self.launch / 'request.json', {**self.request, 'native_mode': {'mode': 'plain'}, 'rpc_proxy': None})
            executor.check_session()
            self.fail('request fence must reject overwritten late-plain selection')
        with patch.object(missing_runtime.Tmux, 'snapshot', snapshot),                 patch.object(missing_runtime.Tmux, 'run', run),                 patch.object(missing_runtime.harnesses, 'load', return_value=Mock(family='codex', stop=stop)):
            result = missing_runtime.kill(self.root, self.directory)
        self.assertEqual(result['native_cancellation']['state'], 'unknown')
        self.assertIn('request changed', result['native_cancellation']['reason'])

    def test_stop_never_closes_a_replacement_after_slow_native_reply(self):
        from multiverse.harnesses.types import NativeResult
        self.properties['workspace'] = str(self.work)
        write(self.directory / 'properties.json', self.properties)
        for replaced in ('pane', 'generation'):
            with self.subTest(replaced=replaced):
                (self.directory / 'runtime.json').unlink(missing_ok=True)
                pane = dict(self.pane)
                def stop(session, executor):
                    executor.check_session()
                    if replaced == 'pane':
                        pane['@mv2_launch_id'] = uid()
                    else:
                        write(self.directory / 'runtime.json', {'launch_id': uid(), 'state': 'running'})
                    return NativeResult('ok')
                with patch.object(missing_runtime.Tmux, 'snapshot', return_value=({'%2': pane}, None)),                         patch.object(missing_runtime.Tmux, 'run') as close,                         patch.object(missing_runtime.harnesses, 'load',
                                     return_value=Mock(family='codex', stop=stop)),                         self.assertRaisesRegex(records.RecordError, 'ownership changed'):
                    missing_runtime.kill(self.root, self.directory)
                close.assert_not_called()

    def test_unreachable_private_relay_is_unknown_but_verified_terminal_closes(self):
        open_pane = True
        self.properties['workspace'] = str(self.work)
        write(self.directory / 'properties.json', self.properties)
        def snapshot(backend):
            return ({'%2': self.pane} if open_pane else {}), None
        def run(backend, *args):
            nonlocal open_pane
            open_pane = False
        with patch.object(missing_runtime.Tmux, 'snapshot', snapshot),                 patch.object(missing_runtime.Tmux, 'run', run),                 patch.object(missing_runtime.harnesses, 'load', return_value=Mock(
                    family='codex', stop=Mock(side_effect=OSError('private relay unavailable')))):
            result = missing_runtime.kill(self.root, self.directory)
        self.assertEqual(result['native_cancellation']['state'], 'unknown')
        self.assertIn('private relay unavailable', result['native_cancellation']['reason'])
        self.assertFalse(open_pane)

    def test_recorded_child_blocks_when_runner_is_gone_and_runtime_is_lost(self):
        write(self.launch / 'cleanup.json', {'schema': 1, 'launch_id': self.launch.name,
              'state': 'active', 'pid': 42, 'pid_started': 'Fri Oct 16 12:00:00 2026',
              'child_pid': 43, 'child_started': 'Fri Oct 16 12:00:01 2026', 'child_at': '2026-10-16T12:00:02+00:00'})
        def observation(pid, observations):
            result = (True, None) if pid == 42 else (False, ('Fri Oct 16 12:00:01 2026', 'S'))
            observations[pid] = result
            return result
        with patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)),                 patch.object(attachments, '_observation', side_effect=observation),                 patch.object(attachments, '_command', return_value='native-child'):
            with self.assertRaisesRegex(records.RecordError, 'child process.*PID 43'):
                missing_runtime.require_stopped(self.directory, self.properties)
