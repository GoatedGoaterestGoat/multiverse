"""Missing or damaged runtime authority: discovery declares a launch stopped only with proof."""
from copy import deepcopy
from pathlib import Path
import shutil
from unittest.mock import patch, Mock

from multiverse.harnesses.types import NativeResult
from multiverse.runtime import missing_runtime, attachments, lifecycle, paths
from multiverse.store import read, uid, write
from multiverse.universe import records, relocate
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

    def test_discovery_declares_stopped_only_with_proof(self):
        """require_stopped, discover and remove call a lost launch stopped only with proof (every reachable recorded
        server, exact tags, a valid request, no live recorded process), and remove then keeps the damaged bytes;
        fragile because every tmux discovery failure must fail closed, and failing open allows a duplicate launch or
        trashes a live agent."""
        for label, facts, unreachable in (('a launch request', None, '/recorded.sock'),
                                          ('damaged runtime facts', {'launch_id': None,
                                           'tmux': {'socket': '/recorded-runtime.sock'}}, '/recorded-runtime.sock')):
            calls = []

            def snapshot(backend, *, facts=None):
                calls.append(backend.prefix)
                return {}, ('Permission denied' if unreachable in backend.prefix else None)
            with self.subTest(server=label), patch.object(missing_runtime.Tmux, 'snapshot', snapshot), \
                    self.assertRaisesRegex(records.RecordError, 'runtime.json.*' + unreachable + '.*Permission denied'):
                missing_runtime.require_stopped(self.directory, self.properties, facts=facts)
            self.assertTrue(any(unreachable in call for call in calls))
        for field in ('@mv2_agent_id', '@mv2_universe_id'):
            with self.subTest(foreign=field), patch.object(missing_runtime.Tmux, 'snapshot',
                    return_value=({'%2': {**self.pane, field: uid()}}, None)):
                self.assertEqual(missing_runtime.require_stopped(self.directory, self.properties),
                                 [missing_runtime.NOTICE])
        other = {**self.pane, 'pane_id': '%3', 'pane_pid': '1235'}
        with self.subTest('multiple exact panes'), patch.object(missing_runtime.Tmux, 'snapshot',
                return_value=({'%2': self.pane, '%3': other}, None)), \
                self.assertRaisesRegex(records.RecordError, 'multiple tagged terminals'):
            missing_runtime.kill(self.root, self.directory)
        self.assertFalse((self.directory / 'runtime.json').exists())
        from multiverse.terminal.recovery import address
        legacy = str(address(missing_runtime.Tmux(legacy=True)))
        def legacy_only(backend, *, facts=None):
            return ({'%2': self.pane} if backend.prefix[-1] == legacy else {}), None
        with self.subTest('legacy default remains in discovery'), \
                patch.object(missing_runtime.Tmux, 'snapshot', legacy_only):
            found = missing_runtime.discover(self.directory, self.properties)
            self.assertEqual([item[2] for item in found], [self.launch.name])
        current = str(address(missing_runtime.Tmux()))
        def current_only(backend, *, facts=None):
            return ({'%2': self.pane} if backend.prefix[-1] == current else {}), None
        with self.subTest('the default is saved as its exact socket'), \
                patch.object(missing_runtime.Tmux, 'snapshot', current_only):
            found = missing_runtime.discover(self.directory, self.properties)
            self.assertEqual(found[0][1]['socket'], current)
        path = self.launch / 'request.json'
        for content in ('[]', '{"env":[]}', '{"env":{"MV2_TMUX_SOCKET":5}}'):
            path.write_text(content)
            with self.subTest(request=content), self.assertRaises(records.RecordError) as caught:
                missing_runtime.discover(self.directory, self.properties)
            self.assertEqual(caught.exception.file, path)
            self.assertIn(str(path), caught.exception.reason)
            self.assertIn('repair', caught.exception.reason.lower())
            self.assertNotIn('restore read access', caught.exception.reason.lower())
            self.assertNotIn('mv2 kill', caught.exception.reason)
        path.unlink()
        with self.subTest(request='missing before native startup'), \
                patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)):
            self.assertEqual(missing_runtime.discover(self.directory, self.properties), [])
        write(path, self.request)
        with self.subTest('an abandoned older launch cannot hide the later tagged terminal'):
            abandoned = self.directory / 'launches' / uid()
            abandoned.mkdir()
            (abandoned / 'task.md').write_text('never started')
            with patch.object(missing_runtime.Tmux, 'snapshot', return_value=({'%2': self.pane}, None)):
                found = missing_runtime.discover(self.directory, self.properties)
            self.assertEqual([item[2] for item in found], [self.launch.name])
        runtime = self.directory / 'runtime.json'
        write(runtime, {'launch_id': None, 'state': 'running', 'pid': 12345})
        before = runtime.read_bytes()
        for expected, snapshot, process in (('cannot inspect terminal server', ({}, 'Permission denied'), None),
                                            ('recorded child is still running', ({}, None),
                                             'recorded child is still running'),
                                            ('process identity cannot be verified', ({}, None),
                                             'process identity cannot be verified')):
            with self.subTest(remove=expected), \
                    patch.object(missing_runtime.Tmux, 'snapshot', return_value=snapshot), \
                    patch.object(attachments, 'recorded_process_problem', return_value=process), \
                    self.assertRaisesRegex(records.RecordError, expected):
                relocate.remove_agent(self.root, 'lost')
            self.assertEqual(runtime.read_bytes(), before)
            self.assertFalse((self.root / 'trash').exists())
        for content in (b'{torn', b'[]', b'', b'\xff'):
            runtime.write_bytes(content)
            with self.subTest(resume_torn=content), self.assertRaises(records.RecordError) as caught:
                lifecycle.start(self.root, resume='lost')
            self.assertIn(str(runtime), caught.exception.reason)
            self.assertIn('restore', caught.exception.reason.lower())
            self.assertIn('restore', lifecycle.listing(self.root, only=self.directory.name)[0]['damaged']['reason'].lower())
            with self.subTest(remove_torn=content), \
                    patch.object(missing_runtime.Tmux, 'snapshot', return_value=({'%2': self.pane}, None)), \
                    self.assertRaisesRegex(records.RecordError, 'running terminal.*mv2 kill'):
                relocate.remove_agent(self.root, 'lost')
            self.assertEqual(runtime.read_bytes(), content)
        original_open = Path.open
        def unreadable(path, *args, **kwargs):
            if path == runtime:
                raise PermissionError(13, 'fixture denied runtime', str(path))
            return original_open(path, *args, **kwargs)
        for operation in (relocate.remove_agent, lifecycle.kill):
            with self.subTest(unreadable=operation.__name__), patch.object(Path, 'open', unreadable), \
                    patch.object(missing_runtime, 'discover', side_effect=AssertionError('unreadable is not absent')), \
                    self.assertRaisesRegex(records.RecordError, 'cannot safely read.*Restore'):
                operation(self.root, 'lost')
        runtime.unlink()
        shutil.rmtree(self.launch)
        with self.subTest(remove='no runtime and no history'), \
                patch.object(missing_runtime.Tmux, 'snapshot', return_value=({'%2': self.pane}, None)), \
                self.assertRaisesRegex(records.RecordError, 'runtime.json.*tagged terminal.*saved launch request'):
            relocate.remove_agent(self.root, 'lost')
        self.assertTrue(self.directory.exists())
        self.assertFalse(runtime.exists())
        write(self.launch / 'request.json', self.request)
        write(runtime, {'launch_id': None, 'state': 'running', 'pid': 12345})
        before = runtime.read_bytes()
        runtime.write_bytes(b'{torn')
        before = runtime.read_bytes()
        with self.subTest(remove='complete absence with torn runtime'), \
                patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
                patch.object(attachments, 'recorded_process_problem', return_value=None), \
                patch.object(attachments, 'problem', return_value=None), \
                patch.object(relocate, 'owned', side_effect=AssertionError('no terminal closure from damaged facts')):
            result = relocate.remove_agent(self.root, 'lost')
            self.assertEqual((Path(result['trash']) / 'runtime.json').read_bytes(), before)
            self.assertFalse(self.directory.exists())

    def test_only_exact_bridge_request_yields_private_relay_never_shared(self):
        """Only an exact bridge request yields the launch's private relay as the stop endpoint; fragile because the
        shared Codex socket sits in the same request, and using it cancels every agent's turns on that daemon,
        unreported."""
        adapter = Mock(family='codex')
        terminal = {'socket': '/recorded.sock', 'pane': '%2', 'session': '$1'}
        facts = missing_runtime._qualified_facts(self.root, self.directory, self.properties, terminal,
                                                self.launch.name, self.request, adapter)
        self.assertEqual(facts['control_endpoint']['address'], self.request['rpc_proxy']['socket_path'])
        self.assertNotIn('native_service', facts)
        self.assertNotIn('pid', facts)
        # A saved request attests its own path, even when this caller selected
        # another run folder. Shared upstreams never become cancellation targets.
        durable = deepcopy(self.request)
        run_root = self.work / 'run'
        endpoint = str(run_root / 'native' / self.launch.name / 'rpc.sock')
        durable['env'].update(MV2_RUN_DIR=str(run_root), MV2_CONTROL_SOCKET=endpoint)
        durable['rpc_proxy']['socket_path'] = endpoint
        with patch.object(paths, 'run_directory', return_value=self.work / 'later'):
            facts = missing_runtime._qualified_facts(self.root, self.directory, self.properties, terminal,
                                                    self.launch.name, durable, adapter)
        self.assertEqual(facts['control_endpoint']['address'], endpoint)
        for invalid in ('relative', None, [], ''):
            refused = deepcopy(durable)
            refused['env']['MV2_RUN_DIR'] = invalid
            with self.subTest(run_directory=invalid):
                facts = missing_runtime._qualified_facts(self.root, self.directory, self.properties, terminal,
                                                        self.launch.name, refused, adapter)
                self.assertNotIn('control_endpoint', facts)
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

    def test_runtime_only_kill_uses_exact_tags_without_inventing_native_authority(self):
        """A lost properties record must not strand its pane, but recovery must not cancel a guessed conversation,
        close another generation or erase PID proof needed when a child outlives the pane."""
        (self.directory / 'properties.json').unlink()
        (self.launch / 'request.json').unlink()
        facts = {'launch_id': self.launch.name, 'state': 'running', 'pid': 990001, 'pid_started': 'saved child',
                 'runner_pid': 990002, 'runner_started': 'saved runner',
                 'tmux': {'socket': '/recorded.sock', 'session': '$1', 'pane': '%2'}}
        write(self.directory / 'runtime.json', facts)
        for label in ('different launch', 'unreadable server', 'replaced record', 'exact pane'):
            pane = {**self.pane}
            if label == 'different launch':
                pane['@mv2_launch_id'] = uid()
            error = 'Permission denied' if label == 'unreadable server' else None
            def close(*args):
                self.assertEqual(args, ('kill-pane', '-t', '%2'))
                panes.clear()
            def snapshot(backend, *, facts=None):
                if label == 'replaced record':
                    write(self.directory / 'properties.json', self.properties)
                return dict(panes), error
            panes = {'%2': pane}
            def wait(*args, **kwargs):
                retained = read(self.directory / 'runtime.json')
                for key in ('pid', 'pid_started', 'runner_pid', 'runner_started'):
                    self.assertEqual(retained.get(key), facts[key], 'Closing the pane must retain process proof')
            with self.subTest(label=label), patch.object(missing_runtime.Tmux, 'snapshot', snapshot), \
                    patch.object(missing_runtime.Tmux, 'run', side_effect=close) as control, \
                    patch.object(missing_runtime.harnesses, 'load', side_effect=AssertionError('no native identity')), \
                    patch.object(attachments, 'wait', side_effect=wait), patch.object(attachments, 'problem', return_value=None):
                if label == 'exact pane':
                    result = lifecycle.kill(self.root, self.directory.name)
                    self.assertEqual(result['state'], 'dormant')
                    self.assertEqual(result['native_cancellation']['state'], 'unknown')
                    control.assert_called_once()
                else:
                    with self.assertRaises(records.RecordError):
                        lifecycle.kill(self.root, self.directory.name)
                    control.assert_not_called()
                    self.assertEqual(read(self.directory / 'runtime.json'), facts)
            (self.directory / 'properties.json').unlink(missing_ok=True)

    def test_stop_aborts_if_the_launch_changes_during_the_native_stop(self):
        """A pane, generation or request that changes during a slow native stop aborts the close, while an
        unreachable or partial stop through the private relay still closes the verified pane and keeps its counts;
        fragile because the window is a slow RPC that shows only under timing, and a break closes a replacement
        launch."""
        self.properties['workspace'] = str(self.work)
        write(self.directory / 'properties.json', self.properties)
        def identity(pane, args):
            self.assertEqual(args[:4], ('display-message', '-p', '-t', pane['pane_id']))
            return '\t'.join(('1234', '5678', pane['session_id'], pane['pane_id'],
                              pane['@mv2_agent_id'], pane['@mv2_universe_id'], pane['@mv2_launch_id']))
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
                with patch.object(missing_runtime.Tmux, 'snapshot', return_value=({'%2': pane}, None)), \
                        patch.object(missing_runtime.Tmux, 'run', side_effect=lambda *args: identity(pane, args)) as control, \
                        patch.object(missing_runtime.harnesses, 'load', return_value=Mock(family='codex', stop=stop)), \
                        self.assertRaisesRegex(records.RecordError, 'ownership changed'):
                    missing_runtime.kill(self.root, self.directory)
                self.assertEqual([call.args[0] for call in control.call_args_list], ['display-message'])

        def late_fallback(session, executor):
            write(self.launch / 'request.json', {**self.request, 'native_mode': {'mode': 'plain'}, 'rpc_proxy': None})
            executor.check_session()
            self.fail('request fence must reject overwritten late-plain selection')

        def partial(session, executor):
            executor.check_session()
            self.assertIsNone(session.service)
            self.assertEqual(session.endpoint.address, self.request['rpc_proxy']['socket_path'])
            return NativeResult('unknown', 'native final cleanup unavailable', {'cancelled_turns': 2})
        for label, stop, reason in (('late plain fallback', late_fallback, 'request changed'),
                                    ('unreachable relay', Mock(side_effect=OSError('private relay unavailable')),
                                     'private relay unavailable'),
                                    ('partial counts', partial, 'native final cleanup unavailable')):
            with self.subTest(stop=label):
                (self.directory / 'runtime.json').unlink(missing_ok=True)
                write(self.launch / 'request.json', self.request)
                if label == 'partial counts':
                    write(self.directory / 'properties.json', {**self.properties, 'native_session_id': uid()})
                open_pane = True

                def snapshot(backend, *, facts=None):
                    return ({'%2': self.pane} if open_pane else {}), None

                def run(backend, *args):
                    nonlocal open_pane
                    if args[0] == 'display-message':
                        return identity(self.pane, args)
                    self.assertEqual(args, ('kill-pane', '-t', '%2'))
                    open_pane = False
                with patch.object(missing_runtime.Tmux, 'snapshot', snapshot), \
                        patch.object(missing_runtime.Tmux, 'run', run), \
                        patch.object(missing_runtime.harnesses, 'load', return_value=Mock(family='codex', stop=stop)):
                    result = missing_runtime.kill(self.root, self.directory)
                self.assertEqual(result['native_cancellation']['state'], 'unknown')
                self.assertIn(reason, result['native_cancellation']['reason'])
                self.assertFalse(open_pane)
                if label == 'partial counts':
                    self.assertEqual(result['native_cancellation']['cancelled_turns'], 2)
