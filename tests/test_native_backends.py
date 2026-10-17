"""A non-Codex service uses the same launch, observation and recovery boundary."""
from dataclasses import asdict, replace
import asyncio
import json
from itertools import product
from pathlib import Path
import shutil
import sys
import threading
from unittest.mock import Mock, patch

from websockets.sync.server import unix_serve
from websockets.sync.client import unix_connect
from websockets.exceptions import ConnectionClosed

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native, recovery, rpc, runner
from multiverse.harnesses.base import ConfiguredAdapter
from multiverse.harnesses.types import (AttachmentSpec, CommandSpec, NativeActivity, NativeEndpoint,
    NativeResult, RpcNotification, RpcProxySpec, RpcRequest, RpcSpec, ServiceIdentity, SharedServiceSpec,
    SessionHandle)
from multiverse.store import now, read, write
from test_multiverse import BaseTest
from test_operations import FourthAdapter


class ServiceAdapter(FourthAdapter):
    """Independent provider: no Codex/base inheritance or native method names."""
    name = 'service-test'

    def __init__(self, address, home):
        super().__init__()
        self.address, self.home = address, home
        self.policy = AttachmentSpec('service', 'binding', 'idle-existing-service')
        self.observation = None

    def command(self, context):
        info = {'address': self.address, 'client': '1', 'service': '1'}
        return CommandSpec((sys.executable, '-c', 'pass'),
            shared_service=SharedServiceSpec((sys.executable, '-c', f'print({json.dumps(info)!r})'),
                'address', ('client', 'service'), {'TEST_NATIVE_HOME': str(self.home)},
                identity=ServiceIdentity('service-test', {'storage': str(self.home)})),
            control_endpoint=NativeEndpoint('unix-websocket', context.values['control_socket']),
            rpc_proxy=RpcProxySpec(context.values['control_socket'],
                context.endpoint.address if context.endpoint else '',
                {'native_id': context.values['session_id'], 'workspace': context.values['workspace'],
                 'grant': 'local-only'}, binds_session=True), attachment=self.policy)

    def spawn(self, context):
        self.calls.append(('spawn', context))
        return self.command(context)

    def resume(self, session, context):
        self.calls.append(('resume', session, context))
        if session.native_id != context.values['session_id'] or session.workspace != context.values['workspace']:
            raise ValueError('Wrong native binding')
        return self.command(context)

    def launch_proxy(self, settings):
        return BindingProtocol(settings)

    def attachment_recovery(self, session):
        def plan():
            yield RpcRequest('hello-service', {}, (RpcNotification('client-ready', {'observer': True}),))
            response = yield RpcRequest('conversation-inventory', {'id': session.native_id})
            value = response['result']
            observed = NativeActivity(value['id'], value['workspace'], session.service,
                value['status'], value['active'], value['waiting'], value['queued'], value['background'])
            return NativeResult('ok', value=self.observation or observed)
        return RpcSpec(session.service.endpoint.address, plan, max_requests=2)


class BindingProtocol:
    def __init__(self, settings):
        self.settings, self.bound = settings, False

    def outgoing(self, value):
        return value

    def incoming(self, value):
        bound = value.get('result')
        if bound != self.settings:
            raise ValueError('Native identity, workspace or effective grant differs')
        self.bound = True
        return bound['native_id']


class NativeBackendTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.address = str(self.work / 'service.sock')
        self.adapter = ServiceAdapter(self.address, self.work / 'storage')
        self.requests = []
        self.inventory = {'status': 'idle', 'active': False, 'waiting': False, 'queued': False, 'background': False}
        def serve(connection):
            for data in connection:
                request = json.loads(data)
                self.requests.append(request)
                if 'id' not in request:
                    continue
                value = {} if request['method'] != 'conversation-inventory' else {
                    **self.inventory, 'id': request['params']['id'], 'workspace': str(self.work)}
                if request['method'] == 'attach-conversation':
                    value = request['params']
                connection.send(json.dumps({'id': request['id'], 'result': value}))
        server = unix_serve(serve, path=self.address, close_timeout=.1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}))
        self.backend = Mock()
        self.terminal = {'socket': '/fake', 'session': '$1', 'pane': '%1'}
        self.backend.create.return_value = self.backend.respawn.return_value = self.terminal
        self.enterContext(patch.object(lifecycle, 'Tmux', return_value=self.backend))
        self.result = lifecycle.start(self.root, name='service-worker', harness='service-test')
        self.directory = agents.directory(self.root, self.result['id'])
        self.addCleanup(self.clean_sockets)
        self.properties = read(self.directory / 'properties.json')
        self.facts = agents.runtime(self.directory)
        self.launch = self.facts['launch_id']
        self.facts.update(state='running', status='idle', pid=123, startup_checked=True, startup_delivery='ready',
            native_relay={'state': 'ready'}, recovery_snapshot={'safe': True, 'at': now(),
                'native_id': self.properties['native_session_id'], 'status_version': 0,
                'workspace': self.properties['workspace'], 'service': self.facts['native_service']})
        write(self.directory / 'runtime.json', self.facts)

    def clean_sockets(self):
        for path in (self.directory / 'launches').glob('*'):
            shutil.rmtree(Path('/tmp') / ('mv2-native-' + path.name), ignore_errors=True)

    def session(self):
        return SessionHandle.from_records(self.properties, self.facts, self.directory)

    def test_explicit_service_identity_attachment_and_protocol_are_independent(self):
        session = self.session()
        self.assertEqual(session.service.identity.provider, 'service-test')
        self.assertEqual(session.service.endpoint.address, self.address)
        request = read(self.directory / 'launches' / self.launch / 'request.json')
        self.assertNotEqual(request['rpc_proxy']['socket_path'], self.address)
        self.assertEqual(session.service.lifetime, 'external')
        self.assertEqual(self.properties['native_environment'], {'TEST_NATIVE_HOME': str(self.adapter.home)})
        result = recovery.check(self.properties, self.facts, self.directory)
        self.assertEqual(result.state, 'ok')
        self.assertIsInstance(result.value, NativeActivity)
        self.assertEqual([r['method'] for r in self.requests],
                         ['hello-service', 'client-ready', 'conversation-inventory'])
        self.assertNotIn('id', self.requests[1])
        self.assertEqual(self.requests[-1]['params']['id'], session.native_id)

    def test_unknown_busy_or_malformed_inventory_never_qualifies(self):
        for key in ('active', 'waiting', 'queued', 'background'):
            for value in (True, None, 0, 'false'):
                with self.subTest(key=key, value=value):
                    self.inventory[key] = value
                    self.assertEqual(recovery.check(self.properties, self.facts, self.directory).state, 'unavailable')
            self.inventory[key] = False
        for status in ('unknown', 'working', 'waiting', None):
            self.inventory['status'] = status
            self.assertEqual(recovery.check(self.properties, self.facts, self.directory).state, 'unavailable')

    def test_observation_must_bind_service_session_and_workspace(self):
        good = NativeActivity(self.session().native_id, str(self.work), self.session().service,
                              'idle', False, False, False, False)
        for observation in ({'safe': True}, replace(good, native_id='other'),
                            replace(good, workspace='/elsewhere'), replace(good, service=None)):
            self.adapter.observation = observation
            self.assertEqual(recovery.check(self.properties, self.facts, self.directory).state, 'unavailable')

    def test_recovery_respawns_attachment_and_never_runs_service_start(self):
        with patch.object(recovery.subprocess, 'Popen'):
            runner.RelayPublisher(self.directory, self.launch)('service unavailable')
        pane = {'pane_id': '%1', 'pane_dead': '1', '@mv2_agent_id': self.properties['id'],
                '@mv2_universe_id': self.properties['universe_id'], '@mv2_launch_id': self.launch}
        self.backend.snapshot.return_value = ({'%1': pane}, None)
        with patch.object(recovery, 'owned', return_value=(self.backend, '%1')), \
             patch.object(recovery, 'frontend_children', return_value=False), \
             patch.object(native, 'start_shared_service', side_effect=AssertionError('Recovery started a service')), \
             patch.object(lifecycle, 'wait_ready', return_value={'ready': True}):
            recovery.run(self.directory, self.launch)
        facts = agents.runtime(self.directory)
        self.assertNotEqual(facts['launch_id'], self.launch)
        self.assertEqual(facts['auto_recovery']['state'], 'resuming')
        self.assertEqual(facts['native_service'], self.facts['native_service'])
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.session().native_id)
        self.backend.respawn.assert_called_once()
        self.assertEqual(self.backend.configure_status.call_count, 2)
        request = read(self.directory / 'launches' / facts['launch_id'] / 'request.json')
        self.assertIsNone(request['fallback'])
        self.assertIsNone(request['resume_message_id'])
        self.assertEqual(request['rpc_proxy']['upstream_path'], self.address)
        self.assertTrue(all(call[-1].resume_prompt is None for call in self.adapter.calls))

    def test_scope_change_rejects_resume_before_starting_service_or_terminal(self):
        self.adapter.home = self.work / 'different-account'
        with patch.object(native, 'start_shared_service') as start, self.assertRaisesRegex(ValueError, 'scope'):
            lifecycle.start(self.root, resume=self.properties['id'])
        start.assert_not_called()
        self.assertEqual(agents.runtime(self.directory), self.facts)
        self.backend.respawn.assert_not_called()

    def test_mouse_uses_the_final_prepared_attachment_before_runner_ready(self):
        original = self.adapter.command
        def prepared(context):
            command = original(context)
            return replace(command, attachment=replace(command.attachment, mouse=bool(context.endpoint)))
        self.backend.run.reset_mock()
        with patch.object(self.adapter, 'command', side_effect=prepared):
            result = lifecycle.start(self.root, name='final-mouse', harness='service-test')
        directory = agents.directory(self.root, result['id'])
        self.addCleanup(shutil.rmtree, Path('/tmp') / ('mv2-native-' + result['runtime']['launch_id']), True)
        self.assertTrue(result['runtime']['attachment']['mouse'])
        self.backend.run.assert_any_call('set-option', '-t', '$1', 'mouse', 'on')
        self.assertTrue((directory / 'launches' / result['runtime']['launch_id'] / 'ready').exists())

    def test_declared_fallback_can_have_a_different_mouse_policy(self):
        original = self.adapter.command
        def prepared(context):
            command = original(context)
            return replace(command, fallback=CommandSpec((sys.executable, '-c', 'pass'), attachment=AttachmentSpec(mouse=True)))
        with patch.object(self.adapter, 'command', side_effect=prepared):
            result = lifecycle.start(self.root, name='different-fallback', harness='service-test')
        self.addCleanup(shutil.rmtree, Path('/tmp') / ('mv2-native-' + result['runtime']['launch_id']), True)
        request = read(agents.directory(self.root, result['id']) / 'launches' / result['runtime']['launch_id'] / 'request.json')
        self.assertFalse(request['attachment']['mouse'])
        self.assertTrue(request['fallback']['attachment']['mouse'])

    def fallback_fixture(self, initial, selected):
        launch = self.directory / 'launches' / self.launch
        request = read(launch / 'request.json')
        request['attachment']['mouse'] = initial
        request['fallback'] = asdict(CommandSpec(('plain',), attachment=AttachmentSpec(mouse=selected)))
        write(launch / 'request.json', request)
        proxy = Mock()
        proxy.protocol.bound = False
        proxy.start.side_effect = RuntimeError('relay unavailable')
        return launch, proxy

    def test_runner_applies_selected_fallback_mouse_policy_before_child_startup(self):
        original_request = read(self.directory / 'launches' / self.launch / 'request.json')
        for initial, selected in ((False, True), (True, False), (False, False), (True, True)):
            with self.subTest(initial=initial, selected=selected):
                write(self.directory / 'runtime.json', self.facts)
                write(self.directory / 'launches' / self.launch / 'request.json', original_request)
                launch, proxy = self.fallback_fixture(initial, selected)
                self.backend.run.reset_mock()
                expected = (('set-option', '-t', '%1', 'mouse', 'on') if selected
                            else ('set-option', '-u', '-t', '%1', 'mouse'))
                process = Mock(pid=123)
                process.poll.return_value = process.wait.return_value = 0
                def start(*args, **kwargs):
                    if initial != selected:
                        self.backend.run.assert_called_once_with(*expected)
                    else:
                        self.backend.run.assert_not_called()
                    self.assertEqual(agents.runtime(self.directory)['attachment']['mouse'], selected)
                    return process
                with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                     patch.object(rpc, 'LaunchProxy', return_value=proxy), \
                     patch.object(runner, 'process_started', return_value=None), \
                     patch.object(runner.subprocess, 'Popen', side_effect=start) as child, \
                     patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')), \
                     patch.object(runner, 'launch_diagnostic', return_value={}):
                    self.assertEqual(runner.main(), 0)
                child.assert_called_once()
                self.assertIsNone(agents.runtime(self.directory)['native_service'])

    def test_stale_generation_or_foreign_pane_cannot_change_fallback_options_or_start_child(self):
        original_request = read(self.directory / 'launches' / self.launch / 'request.json')
        for stale, initial, selected in product((True, False), repeat=3):
            with self.subTest(stale=stale, initial=initial, selected=selected):
                write(self.directory / 'runtime.json', self.facts)
                write(self.directory / 'launches' / self.launch / 'request.json', original_request)
                launch, proxy = self.fallback_fixture(initial, selected)
                if stale:
                    def replaced():
                        write(self.directory / 'runtime.json', {**self.facts, 'launch_id': 'replacement'})
                        raise RuntimeError('relay unavailable')
                    proxy.start.side_effect = replaced
                self.backend.run.reset_mock()
                with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                     patch.object(rpc, 'LaunchProxy', return_value=proxy), \
                     patch.object(runner.subprocess, 'Popen') as child, \
                     patch('multiverse.terminal.tmux.owned', side_effect=ValueError('Foreign pane')) as owned, \
                     patch.object(runner, 'launch_diagnostic', return_value={}), patch.object(native, 'flush_startup'):
                    if stale:
                        with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                            runner.main()
                    else:
                        self.assertEqual(runner.main(), 1)
                child.assert_not_called()
                self.backend.run.assert_not_called()
                if stale:
                    owned.assert_not_called()
                else:
                    owned.assert_called_once()

    def test_changed_service_or_endpoint_invalidates_the_recent_proof(self):
        service = self.facts['native_service']
        for changed in (None, {**service, 'lifetime': 'owned'},
                        {**service, 'identity': {'provider': 'other', 'scope': {'storage': '/other'}}},
                        {**service, 'endpoint': {'transport': 'unix-websocket', 'address': '/other.sock'}}):
            facts = {**self.facts, 'native_service': changed}
            self.assertIsNotNone(recovery.eligibility(self.properties, facts))
        self.assertIsNotNone(recovery.eligibility({**self.properties, 'workspace': '/other'}, self.facts))

    def test_recovery_plan_cannot_target_another_service(self):
        specification = self.adapter.attachment_recovery(self.session())
        with patch.object(self.adapter, 'attachment_recovery', return_value=replace(specification, socket_path='/other')), \
             patch.object(rpc, 'execute') as execute:
            self.assertEqual(recovery.check(self.properties, self.facts, self.directory).state, 'unavailable')
        execute.assert_not_called()

    def test_automatic_command_must_keep_service_binding_and_recovery_policy(self):
        command = self.adapter.command(self.adapter.calls[-1][-1])
        native.validate_recovery_command(command, self.session())
        for changed in (replace(command, shared_service=None), replace(command, attachment=None),
                        replace(command, rpc_proxy=replace(command.rpc_proxy, binds_session=False)),
                        replace(command, rpc_proxy=replace(command.rpc_proxy, upstream_path='/other.sock')),
                        replace(command, shared_service=replace(command.shared_service, lifetime='owned'))):
            with self.subTest(command=changed), self.assertRaises(ValueError):
                native.validate_recovery_command(changed, self.session())

    def test_launch_relay_cannot_target_a_different_service_endpoint(self):
        original = self.adapter.command
        def prepared(context):
            command = original(context)
            return replace(command, rpc_proxy=replace(command.rpc_proxy, upstream_path='/other.sock'))
        with patch.object(self.adapter, 'command', side_effect=prepared), \
             self.assertRaisesRegex(RuntimeError, 'different service endpoint'):
            lifecycle.start(self.root, name='other-endpoint', harness='service-test')

    def test_service_lifetime_cannot_authorize_core_ownership(self):
        specification = replace(self.adapter.command(self.adapter.calls[-1][-1]).shared_service, lifetime='owned')
        with patch.object(native.subprocess, 'run') as command, self.assertRaisesRegex(ValueError, 'externally owned'):
            native.start_shared_service(specification, self.work, self.work / 'log', ())
        command.assert_not_called()

    def test_plain_unsupported_or_changed_policy_never_schedules_recovery(self):
        for policy in (None, asdict(AttachmentSpec()),
                       {**self.facts['attachment'], 'work_owner': 'terminal'}):
            write(self.directory / 'runtime.json', {**self.facts, 'attachment': policy})
            with patch.object(recovery.subprocess, 'Popen') as worker:
                runner.RelayPublisher(self.directory, self.launch)('lost')
            worker.assert_not_called()
            self.assertEqual(agents.runtime(self.directory)['auto_recovery']['state'], 'skipped')
        with patch.object(self.adapter, 'attachment_recovery', return_value=None):
            self.assertEqual(recovery.check(self.properties, self.facts, self.directory).state, 'unavailable')

    def test_binding_readiness_requires_provider_validation_and_startup_input(self):
        session = self.session()
        self.assertTrue(native.attachment_ready(self.adapter, session))
        self.assertFalse(native.attachment_ready(self.adapter, replace(session, native_relay=None)))
        self.assertFalse(native.attachment_ready(self.adapter, replace(session, native_id=None)))
        with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            write(self.directory / 'runtime.json', {**self.facts, 'startup_delivery': 'waiting'})
            self.assertFalse(lifecycle.wait_ready(self.root, self.directory, self.launch, self.adapter, .01)['ready'])
            write(self.directory / 'runtime.json', self.facts)
            self.assertTrue(lifecycle.wait_ready(self.root, self.directory, self.launch, self.adapter, .01)['ready'])

    def test_real_relay_binds_only_after_provider_identity_workspace_and_grant_validation(self):
        request = read(self.directory / 'launches' / self.launch / 'request.json')
        settings = request['rpc_proxy']['settings']
        for index, change in enumerate(({}, {'native_id': 'wrong'}, {'workspace': '/wrong'}, {'grant': 'wrong'})):
            with self.subTest(change=change):
                spec = {**request['rpc_proxy'], 'socket_path': str(self.work / f'relay-{index}.sock')}
                bound, errors = [], []
                proxy = rpc.LaunchProxy(spec, self.adapter.launch_proxy(settings), bound.append, errors.append)
                proxy.start()
                try:
                    with unix_connect(spec['socket_path'], close_timeout=.1) as client:
                        client.send(json.dumps({'id': 1, 'method': 'attach-conversation',
                                                'params': {**settings, **change}}))
                        if change:
                            with self.assertRaises(ConnectionClosed):
                                client.recv(timeout=2)
                            self.assertEqual(bound, [])
                            self.assertIn('effective grant', errors[0])
                        else:
                            self.assertEqual(json.loads(client.recv(timeout=2))['result'], settings)
                            self.assertEqual(bound, [self.session().native_id])
                finally:
                    proxy.close()

    def test_terminal_adapters_default_to_no_service_or_recovery(self):
        plain = ConfiguredAdapter('plain', {'spawn': ['native'], 'resume': ['native', '{session_id}']})
        self.assertIsNone(plain.attachment_recovery(self.session()))
        self.assertIsNone(plain.retained_usage(self.session(), self.facts))
        for name in ('claude', 'pi'):
            adapter = harnesses.BUILTINS[name].configured(name, {})
            self.assertIsNone(adapter.attachment_recovery(self.session()))
            self.assertIsNone(adapter.retained_usage(self.session(), self.facts))

    def test_old_style_command_provider_gets_legacy_binding_without_recovery(self):
        original = self.adapter.command
        def legacy(context):
            command = original(context)
            return replace(command, attachment=None, rpc_proxy=replace(command.rpc_proxy, binds_session=False))
        with patch.object(self.adapter, 'command', side_effect=legacy):
            result = lifecycle.start(self.root, name='old-provider', harness='service-test')
        self.addCleanup(shutil.rmtree, Path('/tmp') / ('mv2-native-' + result['runtime']['launch_id']), True)
        policy = result['runtime']['attachment']
        self.assertEqual((policy['readiness'], policy['work_owner'], policy['recovery']),
                         ('legacy-binding', 'unknown', 'unavailable'))
        session = SessionHandle.from_records(result, result['runtime'], self.directory)
        self.assertIsNotNone(native.recovery_unavailable(session))

    def test_old_saved_request_keeps_binding_and_startup_input_gates_without_recovery(self):
        launch = self.directory / 'launches' / self.launch
        request = read(launch / 'request.json')
        request.pop('attachment')
        request['rpc_proxy'].pop('binds_session')
        write(launch / 'request.json', request)
        write(self.directory / 'runtime.json', {k: v for k, v in self.facts.items()
                                               if k not in ('attachment', 'native_service')})
        process = Mock(pid=123)
        process.poll.return_value = 0
        def waited(timeout=None):
            self.assertEqual(agents.runtime(self.directory)['state'], 'starting')
            factory.call_args.args[2](self.session().native_id)
            facts = agents.runtime(self.directory)
            self.assertEqual(facts['state'], 'running')
            self.assertEqual(facts['attachment']['readiness'], 'legacy-binding')
            self.assertIsNotNone(recovery.eligibility(self.properties, facts))
            agents.update_runtime(self.directory, self.launch, startup_checked=True, startup_delivery='waiting')
            with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                self.assertFalse(lifecycle.wait_ready(self.root, self.directory, self.launch, self.adapter, .01)['ready'])
                agents.update_runtime(self.directory, self.launch, startup_delivery='ready')
                self.assertTrue(lifecycle.wait_ready(self.root, self.directory, self.launch, self.adapter, .01)['ready'])
            return 0
        process.wait.side_effect = waited
        with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
             patch.object(rpc, 'LaunchProxy') as factory, \
             patch.object(runner, 'process_started', return_value=None), \
             patch.object(runner.subprocess, 'Popen', return_value=process), \
             patch.object(runner, 'launch_diagnostic', return_value={}), \
             patch.object(runner, 'StartupDelivery'), patch.object(runner, 'StatusWatcher'), \
             patch.object(recovery, 'RecoveryMonitor') as monitor:
            self.assertEqual(runner.main(), 0)
        monitor.assert_not_called()

    def test_wait_for_old_live_records_does_not_infer_recovery(self):
        launch = self.directory / 'launches' / self.launch
        request = read(launch / 'request.json')
        request.pop('attachment')
        request['rpc_proxy'].pop('binds_session')
        write(launch / 'request.json', request)
        facts = {k: v for k, v in self.facts.items() if k not in ('attachment', 'native_service', 'native_relay')}
        write(self.directory / 'runtime.json', facts)
        with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            self.assertTrue(lifecycle.wait_ready(self.root, self.directory, self.launch, self.adapter, .01)['ready'])
        self.assertIsNotNone(recovery.eligibility(self.properties, facts))

    def test_service_and_relay_without_policy_do_not_start_monitors(self):
        launch = self.directory / 'launches' / self.launch
        request = read(launch / 'request.json')
        request['attachment'] = None
        write(launch / 'request.json', request)
        process = Mock(pid=123)
        process.poll.return_value = process.wait.return_value = 0
        with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
             patch.object(runner, 'process_started', return_value=None), \
             patch.object(runner.subprocess, 'Popen', return_value=process), \
             patch.object(rpc, 'LaunchProxy'), patch.object(runner, 'StatusWatcher') as status, \
             patch.object(recovery, 'RecoveryMonitor') as monitor, patch.object(runner, 'StartupDelivery'), \
             patch.object(runner, 'launch_diagnostic', return_value={}):
            self.assertEqual(runner.main(), 0)
        status.assert_not_called()
        monitor.assert_not_called()

    def test_rpc_watch_uses_provider_notifications_too(self):
        specification = self.adapter.attachment_recovery(self.session())
        published = []
        async def observe():
            task = asyncio.create_task(rpc.watch(specification, lambda value: None, published.append))
            for _ in range(100):
                if published:
                    break
                await asyncio.sleep(.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run(observe())
        self.assertIsInstance(published[0], NativeActivity)
        self.assertEqual([r['method'] for r in self.requests],
                         ['hello-service', 'client-ready', 'conversation-inventory'])

    def test_core_does_not_invent_initialize_notifications(self):
        def plan():
            yield RpcRequest('initialize', {})
            return NativeResult('ok')
        self.assertEqual(rpc.execute(RpcSpec(self.address, plan)).state, 'ok')
        self.assertEqual([r['method'] for r in self.requests], ['initialize'])
