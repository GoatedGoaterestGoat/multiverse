"""A child exit must not admit a replacement while its runner owns connections."""
import os
from dataclasses import asdict
from pathlib import Path
import socket
import subprocess
import sys
import time
import threading
import shutil
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.runtime import lifecycle
from multiverse.runtime import attachments, recovery
from multiverse.harnesses.types import AttachmentSpec, NativeActivity, NativeEndpoint, NativeResult, NativeServiceHandle, ServiceIdentity, RpcSpec
from multiverse.store import read, write
from multiverse.terminal.tmux import Tmux, observe
from test_multiverse import BaseTest, TerminalBase, ROOT, until


CHILD = r'''
import socket, sys, time
from pathlib import Path
from types import SimpleNamespace
from multiverse import harnesses
from multiverse.runtime import runner, rpc, lifecycle
base, launch, native = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
class Proxy:
 def __init__(self, spec, protocol, bind, *args, **kwargs): self.bind=bind; self.protocol=protocol
 def start(self):
  self.connection=socket.socket(socket.AF_UNIX); self.connection.connect(str(base/'provider.sock')); self.bind(native)
 def close(self): self.connection.close(); (base/'relay-closed').touch()
class Watcher:
 def __init__(self,*args): pass
 def start(self): pass
 def close(self):
  (base/'closing').touch()
  deadline=time.monotonic()+10
  while not (base/'release').exists():
   if time.monotonic()>deadline: raise RuntimeError('fixture release missing')
   time.sleep(.01)
class Startup:
 def __init__(self,*args): pass
 def start(self): pass
 def close(self): pass
harnesses.load=lambda *args: SimpleNamespace(launch_proxy=lambda settings: object())
rpc.LaunchProxy=Proxy; runner.StatusWatcher=Watcher; runner.StartupDelivery=Startup
runner.launch_diagnostic=lambda *args: {}; lifecycle._event=lambda *args, **kwargs: None
sys.argv=['runner',str(launch)]
raise SystemExit(runner.main())
'''


LEGACY_RUNNER = r'''
import os, subprocess, sys, time
from pathlib import Path
from multiverse.store import read, now
from multiverse.universe.agents import update_runtime
launch=Path(sys.argv[1]); directory=launch.parent.parent
while not (launch/'ready').exists(): time.sleep(.01)
request=read(launch/'request.json')
child=subprocess.Popen(request['argv'], env={**os.environ, **request['env']})
update_runtime(directory, launch.name, state='running', runner_pid=os.getpid(), pid=child.pid)
code=child.wait()
update_runtime(directory, launch.name, state='stopped', exit_code=code, stopped_at=now())
'''


class LegacyAttachmentTest(TerminalBase):
    def test_pre_update_runner_without_cleanup_record_can_stop_and_resume(self):
        legacy = self.work / 'legacy-runner.py'
        legacy.write_text(LEGACY_RUNNER)
        original = Tmux.create
        def create(backend, name, workdir, argv):
            return original(backend, name, workdir, [sys.executable, str(legacy), argv[-1]])
        with patch.object(Tmux, 'create', create):
            result, directory = self.spawn()
        until(lambda: read(directory / 'runtime.json').get('runner_pid'))
        launch = directory / 'launches' / result['runtime']['launch_id']
        self.assertFalse((launch / 'cleanup.json').exists())
        self.assertNotIn('runner_started', read(directory / 'runtime.json'))
        self.assertEqual(lifecycle.kill(self.root, result['id'])['state'], 'dormant')
        self.assertFalse((launch / 'cleanup.json').exists(), 'Old runners never gain a marker')
        resumed = lifecycle.start(self.root, resume=result['id'], socket_path=self.socket)
        self.assertNotEqual(resumed['runtime']['launch_id'], launch.name)


class AttachmentTeardownTest(BaseTest):
    def test_missing_launch_directory_skips_metadata_and_logs_once_without_recreation(self):
        directory = self.root / 'state' / 'agents' / str(uuid4())
        launch = directory / 'launches' / str(uuid4())
        log = attachments.connection_logger(launch)
        item = {'id': str(uuid4()), 'role': 'status stream', 'endpoint': '/isolated/service.sock', 'state': 'opening'}
        with self.assertLogs('multiverse.runtime.attachments', level='WARNING') as captured:
            log(item)
            log({**item, 'state': 'closed'})
        self.assertEqual(len(captured.output), 1)
        self.assertIn(launch.name, captured.output[0])
        self.assertIn('launch directory is missing', captured.output[0])
        self.assertFalse(directory.exists())

    def test_teardown_scan_and_closed_history_pruning_are_serialized(self):
        directory = self.root / 'state' / 'agents' / str(uuid4())
        launch = directory / 'launches' / str(uuid4())
        launch.mkdir(parents=True)
        facts = {'launch_id': launch.name}
        attachments.begin(launch, None)
        attachments.finish(launch)
        log = attachments.connection_logger(launch)
        first = None
        for _ in range(100):
            item = {'id': str(uuid4()), 'role': 'control RPC', 'endpoint': '/isolated/service.sock', 'state': 'closed'}
            log(item)
            first = first or launch / 'roles' / ('connection-' + item['id'] + '.json')
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        errors, results = [], []
        original = attachments.read
        def read_record(path):
            if Path(path) == first and threading.current_thread().name == 'teardown-scan':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture scan was not released')
            return original(path)
        def scan():
            try:
                results.append(attachments.problem(directory, facts))
            except Exception as error:
                errors.append(error)
        def prune():
            try:
                log({'id': str(uuid4()), 'role': 'control RPC', 'endpoint': '/isolated/service.sock', 'state': 'closed'})
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        reader = threading.Thread(target=scan, name='teardown-scan')
        writer = threading.Thread(target=prune, name='connection-prune')
        with patch.object(attachments, 'read', side_effect=read_record):
            reader.start()
            try:
                self.assertTrue(entered.wait(3))
                writer.start()
                self.assertFalse(done.wait(.05), 'Pruning must wait for the teardown snapshot')
            finally:
                release.set()
                reader.join(3)
                if writer.ident is not None:
                    writer.join(3)
        self.assertFalse(reader.is_alive() or writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [None])
        self.assertFalse(first.exists())
        active = attachments.Role(launch, 'recovery worker', pending=True)
        self.assertIn('recovery worker', attachments.problem(directory, facts))
        active.close()
        self.assertIsNone(attachments.problem(directory, facts))

    def test_closed_connection_history_is_bounded_without_pruning_active_pending_or_unknown(self):
        directory = self.root / 'state' / 'agents' / str(uuid4())
        launch = directory / 'launches' / str(uuid4())
        launch.mkdir(parents=True)
        facts = {'launch_id': launch.name}
        write(directory / 'runtime.json', facts)
        attachments.begin(launch, None)
        attachments.finish(launch)
        log = attachments.connection_logger(launch)
        active = {'id': str(uuid4()), 'role': 'terminal relay', 'endpoint': '/isolated/service.sock', 'state': 'opening'}
        log(active)
        pending = attachments.Role(launch, 'recovery worker', pending=True)
        unknown = launch / 'roles' / ('connection-' + str(uuid4()) + '.json')
        write(unknown, {'state': 'closed'})
        for _ in range(220):
            item = {'id': str(uuid4()), 'role': 'control RPC', 'endpoint': '/isolated/service.sock', 'state': 'opening'}
            log(item)
            # Old per-connection lock files must be bounded as well.
            (launch / 'roles' / ('connection-' + item['id'] + '.lock')).touch()
            log({**item, 'state': 'closed'})
        self.assertTrue(unknown.exists(), 'Malformed evidence is not proof of closure')
        unknown.unlink()
        self.assertEqual(len(list((launch / 'roles').glob('connection-*.json'))), 101)
        self.assertEqual(len(list((launch / 'roles').glob('connection-*.lock'))), 100)
        with self.assertRaisesRegex(RuntimeError, 'still closing'):
            attachments.wait(directory, launch.name, .01)
        log({**active, 'state': 'closed'})
        self.assertIn('recovery worker', attachments.problem(directory, facts))
        pending.close()
        attachments.wait(directory, launch.name, .01)
        self.assertEqual(len(list((launch / 'roles').glob('connection-*.json'))), 100)

    def test_repeated_monitor_safety_reads_do_not_create_connection_history(self):
        directory = self.root / 'state' / 'agents' / str(uuid4())
        launch = directory / 'launches' / str(uuid4())
        service = NativeServiceHandle(ServiceIdentity('fixture', {'FIXTURE_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', '/isolated/service.sock'))
        props = {'id': directory.name, 'name': 'monitor', 'harness': 'fixture',
                 'workspace': str(self.work), 'native_session_id': str(uuid4())}
        facts = {'launch_id': launch.name, 'state': 'running', 'status': 'idle', 'startup_checked': True,
                 'control_endpoint': asdict(service.endpoint), 'native_service': asdict(service),
                 'native_relay': {'state': 'ready'},
                 'attachment': asdict(AttachmentSpec('service', 'binding', 'idle-existing-service', True, True))}
        write(directory / 'properties.json', props)
        write(directory / 'runtime.json', facts)
        (directory / '.launch.lock').touch()
        attachments.begin(launch, None)
        attachments.finish(launch)
        role = attachments.Role(launch, 'recovery monitor')
        specification = RpcSpec(service.endpoint.address, lambda: iter(()))
        activity = NativeActivity(props['native_session_id'], str(self.work), service, 'idle', False, False, False, False)
        def execute(spec, timeout, record_connection=None):
            if record_connection:
                item = {'id': str(uuid4()), 'role': 'control RPC', 'endpoint': spec.socket_path, 'state': 'opening'}
                record_connection(item)
                record_connection({**item, 'state': 'closed'})
            return NativeResult('ok', value=activity)
        monitor = recovery.RecoveryMonitor(directory, launch.name, self.root)
        monitor.stopped = Mock()
        monitor.stopped.wait.side_effect = [False, False] * 250 + [True]
        with patch.object(recovery.harnesses, 'load', return_value=SimpleNamespace(attachment_recovery=lambda session: specification)), \
             patch.object(recovery.rpc, 'execute', side_effect=execute) as rpc:
            monitor._run()
        self.assertEqual(rpc.call_count, 250)
        self.assertEqual(list((launch / 'roles').glob('connection-*')), [])
        self.assertIn('recovery monitor', attachments.problem(directory, facts))
        role.close()
        attachments.wait(directory, launch.name, .01)

    def test_connection_records_only_metadata_and_blocks_until_its_close(self):
        directory = self.root / 'state' / 'agents' / str(uuid4())
        launch = directory / 'launches' / str(uuid4())
        launch.mkdir(parents=True)
        facts = {'launch_id': launch.name}
        attachments.begin(launch, None)
        attachments.finish(launch)
        log = attachments.connection_logger(launch)
        metadata = {'id': str(uuid4()), 'role': 'test RPC', 'endpoint': '/isolated/provider.sock',
                    'state': 'opening', 'prompt': 'MUST NEVER BE RECORDED'}
        log(metadata)
        self.assertIsNotNone(attachments.problem(directory, facts))
        log({**metadata, 'state': 'closed'})
        self.assertIsNone(attachments.problem(directory, facts))
        row, = (launch / 'roles').glob('*.json')
        self.assertNotIn('MUST NEVER', row.read_text())

    def test_pending_worker_blocks_even_after_runner_finished_and_close_never_recreates_removed_records(self):
        directory = self.root / 'state' / 'agents' / str(uuid4())
        launch = directory / 'launches' / str(uuid4())
        launch.mkdir(parents=True)
        facts = {'launch_id': launch.name, 'state': 'stopped'}
        write(directory / 'runtime.json', facts)
        attachments.begin(launch, None)
        attachments.finish(launch)
        worker = attachments.Role(launch, 'recovery worker', pending=True)
        self.assertIn('recovery worker', attachments.problem(directory, facts))
        with self.assertRaisesRegex(RuntimeError, 'still closing'):
            attachments.wait(directory, launch.name, .01)
        worker.claimed(os.getpid(), None)
        self.assertIsNotNone(attachments.problem(directory, facts))
        worker.close()
        self.assertIsNone(attachments.problem(directory, facts))
        shutil.rmtree(directory)
        worker.close()
        self.assertFalse(directory.exists())

    def test_foreign_and_malformed_cleanup_records_fail_closed_without_process_control(self):
        directory = self.root / 'state' / 'agents' / str(uuid4())
        launch = directory / 'launches' / str(uuid4())
        for value in ([], {'schema': 1, 'launch_id': 'foreign', 'state': 'complete'}):
            write(launch / 'cleanup.json', value)
            with patch.object(attachments.os, 'kill') as kill:
                self.assertIn('invalid cleanup', attachments.problem(directory, {'launch_id': launch.name}))
                kill.assert_not_called()

    def test_exited_child_with_open_old_relay_blocks_resume_until_cleanup_finishes(self):
        agent, native_id, launch_id = (str(uuid4()) for _ in range(3))
        directory = self.root / 'state' / 'agents' / agent
        launch = directory / 'launches' / launch_id
        props = {'id': agent, 'name': 'fixture', 'universe_id': read(self.root / 'universe.json')['id'],
                 'harness': 'codex', 'workspace': str(self.work), 'task': '', 'native_session_id': native_id,
                 'native_session_provider': 'codex'}
        terminal = {'socket': str(self.work / 'tmux.sock'), 'session': '$1', 'pane': '%1'}
        write(directory / 'properties.json', props)
        write(directory / 'runtime.json', {'launch_id': launch_id, 'state': 'starting', 'tmux': terminal})
        write(launch / 'request.json', {'argv': ['/usr/bin/true'],
            'attachment': {'readiness': 'binding', 'observe_status': True},
            'rpc_proxy': {'socket_path': f'/tmp/mv2-native-{launch_id}/rpc.sock', 'settings': {}, 'binds_session': True},
            'control_endpoint': {'transport': 'unix-websocket', 'address': f'/tmp/mv2-native-{launch_id}/rpc.sock'},
            'env': {'MV2_CLI': str(ROOT / 'bin' / 'mv2'), 'MV2_UNIVERSE': str(self.root)}})
        (launch / 'ready').touch()
        env = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
        env.update(MV2_PLUGINS='off', PYTHONPATH=str(ROOT / 'src'))
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.work / 'provider.sock'))
            server.listen()
            server.settimeout(5)
            process = subprocess.Popen([sys.executable, '-c', CHILD, str(self.work), str(launch), native_id],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                connection, _ = server.accept()
                with connection:
                    until(lambda: (self.work / 'closing').exists(), seconds=5)
                    facts = read(directory / 'runtime.json')
                    pane = {'pane_id': '%1', 'pane_dead': '0', 'pane_pid': str(process.pid),
                            '@mv2_agent_id': agent, '@mv2_universe_id': props['universe_id'], '@mv2_launch_id': launch_id}
                    self.assertEqual(observe(props, facts, {terminal['socket']: ({'%1': pane}, None)})['live'], 'stopped')
                    connection.settimeout(.05)
                    with self.assertRaises(TimeoutError):
                        connection.recv(1)
                    reached = []
                    def resume(*args):
                        reached.append(True)
                        raise RuntimeError('adapter preparation reached before teardown')
                    adapter = SimpleNamespace(default_model=None, session_mode='assigned', bootstrap='', resume=resume)
                    with patch.object(Tmux, 'snapshot', return_value=({'%1': pane}, None)), \
                         patch.object(lifecycle.harnesses, 'load', return_value=adapter), \
                         patch.object(lifecycle.catalog, 'bootstrap', return_value='fixture'), \
                         patch.object(lifecycle, 'TEARDOWN_TIMEOUT', .1, create=True):
                        with self.assertRaisesRegex(RuntimeError, 'closing'):
                            lifecycle.start(self.root, resume=agent)
                    self.assertEqual(reached, [])
                    (self.work / 'release').touch()
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    connection.settimeout(1)
                    self.assertEqual(connection.recv(1), b'')
                    record = read(launch / 'cleanup.json')
                    self.assertEqual((record['launch_id'], record['state']), (launch_id, 'complete'))
            finally:
                (self.work / 'release').touch()
                if process.poll() is None:
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
