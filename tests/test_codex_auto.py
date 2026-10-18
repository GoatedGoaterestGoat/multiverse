"""Codex auto selection: one preparation attempt, safe fallback, visible evidence."""
from dataclasses import asdict
import json
import os
import socket
import sys
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native, runner
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import CommandSpec, LaunchContext, NativeEndpoint, NativeResult, SessionHandle
from multiverse.universe.mailbox import append_message, message_record, update_message
from multiverse.store import read, write
from multiverse.tools.capabilities import run as capabilities
from native_safety import isolated_home
from test_multiverse import TerminalBase, BaseTest, until


class AutoTerminalTest(TerminalBase):
    def setUp(self):
        super().setUp()
        self.address = self.work / 'native.sock'
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.address))
        self.addCleanup(listener.close)
        self.case = self.work / 'native-case.json'
        self.events = self.work / 'native-calls.jsonl'
        self.executable = self.work / 'codex-fixture'
        self.executable.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, time
case = json.loads(pathlib.Path({str(self.case)!r}).read_text())
with pathlib.Path({str(self.events)!r}).open('a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:] == ['app-server', 'daemon', 'start']:
    if case.get('failure'):
        print(case['failure'], file=sys.stderr)
        sys.exit(2)
    print(json.dumps(case['info']))
elif sys.argv[1:] == ['--help']:
    print(case.get('help', '  --remote <URL>'))
else:
    directory = pathlib.Path(os.environ['MV2_AGENT_DIR'])
    (directory / 'executed.json').write_text(json.dumps(sys.argv[1:]))
    print('FAKE CODEX READY', flush=True)
    time.sleep(120)
''')
        self.executable.chmod(0o755)
        self.info = {'socketPath': str(self.address), 'cliVersion': '1', 'appServerVersion': '1'}
        self.config = self.root / 'harnesses/codex/harness.json'
        self.enterContext(patch.dict(os.environ, {'CODEX_HOME': str(isolated_home(self.work / 'native-home'))}))
        self.set_case()

    def set_case(self, **changes):
        write(self.case, {'info': self.info, **changes})

    def configure(self, **settings):
        write(self.config, {'schema': 2, 'adapter': 'codex', 'settings': {
            'executable': str(self.executable), **settings}})

    def launch(self, name='auto', resume=None, native_home=None, rpc=None, **kwargs):
        rpc = rpc or NativeResult('ok', value={'exclude': [], 'writable_roots': []})
        with patch.dict(os.environ, {'CODEX_HOME': str(native_home or self.work / 'native-home')}), \
             patch.object(native.NativeExecutor, 'rpc', return_value=rpc):
            result = lifecycle.start(self.root, name, 'literal task', resume=resume, socket_path=self.socket, **kwargs)
        directory = agents.directory(self.root, result['id'])
        until(lambda: (directory / 'executed.json').exists())
        return result, directory

    def calls(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def test_mode_selection_falls_back_before_any_native_child(self):
        """Each inbox_bridge policy and Codex condition picks bridge or plain before any native child starts, starting
        the daemon at most once; fragile because it depends on Codex flags, daemon availability and config/read
        support that change by version, and a wrong pick starts a second native child or silently drops mv2
        delivery."""
        with self.subTest('harness auto capability is undecided, not launch-only'):
            for setting, mechanism in (({}, None), ({'inbox_bridge': True}, None),
                                       ({'inbox_bridge': False}, 'launch-only')):
                self.configure(**setting)
                found = capabilities(self.root, harness='codex')
                for operation in ('send_steer', 'send_follow_up'):
                    self.assertEqual(found[operation]['mechanism'], mechanism)
        start = ['app-server', 'daemon', 'start']
        plain = []  # launches checked once all have started, so their readiness waits overlap
        def launch(name, rpc=None, **settings):
            self.configure(**settings)
            before, config = len(self.calls()), self.config.read_bytes()
            launched, directory = self.launch(name=name, rpc=rpc)
            self.assertLessEqual(self.calls()[before:].count(start), 1)
            self.assertEqual(self.config.read_bytes(), config)
            return launched, directory, self.calls()[before:]
        with self.subTest('auto with a working daemon'):
            launched, directory, calls = launch('auto')
            self.assertEqual(agents.runtime(directory)['native_mode'],
                             {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None})
            self.assertIn('--remote', read(directory / 'executed.json'))
            self.assertEqual(calls[:2], [start, ['--help']])
            self.assertEqual(len(calls), 3)
            self.assertIn('bridge (auto)', capabilities(self.root, agent=launched['id'])['spawn']['reason'])
            self.assertEqual(capabilities(self.root, agent=launched['id'])['session_name']['state'], 'supported')
        unavailable = [({'failure': 'old CLI: no app-server daemon'}, 'no app-server daemon'),
                       ({'failure': 'daemon start failed'}, 'daemon start failed'),
                       ({'help': 'usage: codex [PROMPT]'}, '--remote')]
        for index, (changes, reason) in enumerate(unavailable):
            with self.subTest('auto without a bridge', reason=reason):
                self.set_case(**changes)
                launched, directory, _ = launch(f'fallback-{index}',
                                                args=['--dangerously-bypass-approvals-and-sandbox'])
                plain.append(('fallback', launched, directory, harnesses.load(self.root, 'codex'), reason))
        self.set_case(info={**self.info, 'cliVersion': '0.159.3', 'appServerVersion': '0.160.0'})
        unsupported = NativeResult('unavailable', 'config/read unsupported')
        for policy in ({}, {'inbox_bridge': True}):
            with self.subTest('different versions', policy=policy):
                launched, directory, _ = launch('required' if policy else 'auto-versions', **policy)
                self.assertEqual(launched['runtime']['native_mode']['mode'], 'bridge')
                self.assertEqual(launched['runtime']['native_versions'], {'cli': '0.159.3', 'daemon': '0.160.0'})
                self.assertIn('--remote', read(directory / 'executed.json'))
                self.assertIn('versions differ', capabilities(self.root, agent=launched['id'])['resume']['reason'])
                # Different versions must still pass the real configuration API.
                if policy:
                    with self.assertRaisesRegex(RuntimeError, 'config/read unsupported'):
                        self.launch('bad-required', rpc=unsupported)
                else:
                    launched, directory, _ = launch('bad-auto', rpc=unsupported)
                    plain.append(('config', launched, directory, harnesses.load(self.root, 'codex'),
                                  'config/read unsupported'))
        with self.subTest('required without a daemon'):
            self.set_case(failure='daemon unavailable')
            self.configure(inbox_bridge=True)
            with self.assertRaisesRegex(RuntimeError, 'daemon unavailable'):
                self.launch('strict')
            strict_dir = agents.directory(self.root, 'strict')
            self.assertEqual(agents.runtime(strict_dir)['state'], 'failed')
            self.assertEqual(agents.runtime(strict_dir)['native_mode']['policy'], 'required')
            self.assertFalse((strict_dir / 'executed.json').exists())
        with self.subTest('plain never prepares a bridge'):
            with patch.object(native, 'start_shared_service',
                              side_effect=AssertionError('Plain must not start a service')):
                launched, directory, _ = launch('plain', inbox_bridge=False)
            plain.append(('plain', launched, directory, harnesses.load(self.root, 'codex'), None))
        with self.subTest('auto without config/read'):
            self.set_case()
            launched, directory, _ = launch('config-fallback', rpc=NativeResult('unavailable', 'old config API'))
            plain.append(('config', launched, directory, harnesses.load(self.root, 'codex'), 'old config API'))
        for row, launched, directory, adapter, reason in plain:
            with self.subTest('plain launch', row=row, reason=reason):
                ready = lifecycle.wait_ready(self.root, directory, launched['runtime']['launch_id'], adapter, 5)
                self.assertTrue(ready['ready'], ready)
                if row == 'plain':
                    self.assertEqual(agents.runtime(directory)['native_mode'],
                                     {'policy': 'plain', 'mode': 'plain', 'fallback_reason': None})
                    continue
                self.assertEqual(ready['runtime']['native_mode']['mode'], 'plain')
                self.assertIn(reason, ready['runtime']['native_mode']['fallback_reason'])
                if row == 'fallback':
                    facts = agents.runtime(directory)
                    self.assertIsNone(facts['control_endpoint'])
                    argv = read(directory / 'executed.json')
                    self.assertNotIn('--remote', argv)
                    self.assertIn('--dangerously-bypass-approvals-and-sandbox', argv)
                    caps = capabilities(self.root, agent=launched['id'])
                    self.assertIn(reason, caps['resume']['reason'])
                    self.assertEqual(caps['session_name']['state'], 'unavailable')
                    self.assertIsNone(harnesses.load(self.root, 'codex').status_watch(
                        native.context(self.root, launched['id'])[2]))
                    lifecycle.kill(self.root, launched['id'])
        self.assertTrue(self.address.exists(), 'Fallback must not remove the shared service socket')
        with self.subTest('deleted daemon working directory'):
            executor = Mock()
            executor.rpc.return_value = NativeResult('error', 'Native RPC -32603: failed to resolve feature override '
                                                     'precedence: No such file or directory (os error 2)')
            native_id = str(uuid4())
            values = {'name': 'worker', 'workspace': '/workspace', 'agent_dir': '/outside/state/agent',
                      'bootstrap_json': json.dumps('bootstrap'), 'task': '--literal prompt',
                      'session_id': native_id, 'control_socket': '/tmp/owned/rpc.sock'}
            context = LaunchContext('spawn', values, endpoint=NativeEndpoint('unix-websocket', '/tmp/native.sock'),
                                    executor=executor, environment={'CODEX_HOME': '/native storage'})
            session = SessionHandle('agent', 'codex', '/workspace', native_id=native_id)
            strict = CodexAdapter.configured('codex', {'inbox_bridge': True})
            with self.assertRaisesRegex(ValueError, 'deleted working directory') as raised:
                strict.resume(session, context)
            self.assertIn('daemon restart', str(raised.exception))
            self.assertIn("CODEX_HOME='/native storage'", str(raised.exception))
            self.assertIn('when other Codex work is idle', str(raised.exception))
            command = CodexAdapter.configured('codex', {}).resume(session, context)
            self.assertEqual(command.native_mode['mode'], 'plain')
            self.assertIn('deleted working directory', command.native_mode['fallback_reason'])
            self.assertIn(native_id, command.argv)
            self.assertEqual(executor.rpc.call_count, 2, 'Diagnosis must not mutate or restart the daemon')

    def test_resume_reselects_mode_and_preserves_exact_identity(self):
        """Resume may change the mode but keeps the exact native thread, workspace, task and saved CODEX_HOME; a break
        goes unnoticed because Codex quietly opens or creates a different conversation."""
        self.set_case(failure='temporarily unavailable')
        self.configure(args=['--dangerously-bypass-approvals-and-sandbox'])
        first, directory = self.launch(wait=True, wait_timeout=5)
        native_id = str(uuid4())
        agents.bind_session(self.root, first['id'], native_id)
        lifecycle.kill(self.root, first['id'])
        (directory / 'executed.json').unlink()
        self.set_case(info={**self.info, 'cliVersion': '0.159.3', 'appServerVersion': '0.160.0'})
        resumed, directory = self.launch(resume=first['id'])
        self.assertEqual(resumed['native_session_id'], native_id)
        self.assertEqual(resumed['runtime']['native_mode']['mode'], 'bridge')
        self.assertEqual(resumed['runtime']['native_versions'], {'cli': '0.159.3', 'daemon': '0.160.0'})
        request = read(directory / 'launches' / resumed['runtime']['launch_id'] / 'request.json')
        self.assertIn(native_id, request['argv'])
        self.assertEqual(request['rpc_proxy']['settings']['resume_permissions'],
                         {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'})
        self.assertEqual(resumed['workspace'], first['workspace'])
        self.assertEqual(resumed['task'], first['task'])
        # The fixture has no model work to cancel. End its real child and pane,
        # then make the bridge unavailable for the next exact-session resume.
        with patch.object(native, 'stop', return_value=NativeResult('ok')):
            lifecycle.kill(self.root, first['id'])
        (directory / 'executed.json').unlink()
        self.set_case(failure='unavailable again')
        plain, directory = self.launch(resume=first['id'], native_home=self.work / 'different-home',
                                       wait=True, wait_timeout=5)
        self.assertTrue(plain['ready'])
        self.assertEqual(plain['native_session_id'], native_id)
        self.assertEqual(plain['runtime']['native_mode']['mode'], 'plain')
        self.assertIn('unavailable again', plain['runtime']['native_mode']['fallback_reason'])
        request = read(directory / 'launches' / plain['runtime']['launch_id'] / 'request.json')
        self.assertIn(native_id, request['argv'])
        self.assertEqual(request['env']['CODEX_HOME'], str(self.work / 'native-home'))


class AutoRunnerTest(BaseTest):
    def test_relay_start_failure_only_falls_back_before_child_start(self):
        """The runner falls back to plain only if the relay failed before the bridge child started, and fails a
        message queued during startup instead of replaying it; fragile because relay start and child start race, and
        a break runs two native frontends on one thread."""
        for auto, bound in ((True, False), (False, False), (True, True)):
            with self.subTest(auto=auto, bound=bound):
                directory = self.root / 'state/agents' / str(uuid4())
                launch_id = str(uuid4())
                launch = directory / 'launches' / launch_id
                write(directory / 'properties.json', {'id': directory.name, 'name': 'runner', 'harness': 'codex'})
                write(directory / 'runtime.json', {'launch_id': launch_id, 'state': 'starting'})
                pending = append_message(directory, 'queued during relay startup', {'name': 'sender'}, 'steer')
                update_message(directory, pending['id'], submission_state='pending', deferred_launch_id=launch_id)
                fallback = asdict(CommandSpec(('plain-native',), native_mode={'policy': 'auto', 'mode': 'plain'})) if auto else None
                write(launch / 'request.json', {'argv': ['bridge-native'], 'fallback': fallback,
                    'rpc_proxy': {'socket_path': '/tmp/mv2-native-' + launch_id + '/rpc.sock', 'settings': {}},
                    'env': {'MV2_CLI': '/repo/bin/mv2', 'MV2_UNIVERSE': str(self.root)}})
                (launch / 'ready').touch()
                proxy = Mock()
                proxy.protocol.bound = bound
                proxy.start.side_effect = RuntimeError('relay socket unavailable')
                process = Mock(pid=123)
                process.poll.return_value = 0
                process.wait.return_value = 0
                with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                     patch.object(runner, 'process_started', return_value=None), \
                     patch.object(runner.subprocess, 'Popen', return_value=process) as child, \
                     patch('multiverse.terminal.tmux.owned', return_value=(Mock(**{'run.return_value': ''}), '%1')), \
                     patch.object(harnesses, 'load'), patch('multiverse.runtime.rpc.LaunchProxy', return_value=proxy):
                    code = runner.main()
                if auto and not bound:
                    self.assertEqual(code, 0)
                    child.assert_called_once()
                    self.assertEqual(child.call_args.args[0], ['plain-native'])
                    selection = agents.runtime(directory)['native_mode']
                    self.assertEqual(selection['mode'], 'plain')
                    self.assertIn('relay socket unavailable', selection['fallback_reason'])
                    self.assertIsNone(read(launch / 'request.json')['rpc_proxy'])
                    message, tracking = message_record(directory, pending['id'])
                    self.assertEqual(message['text'], 'queued during relay startup')
                    self.assertEqual(tracking['submission_state'], 'failed')
                    self.assertIn('plain mode', tracking['last_receipt']['reason'])
                else:
                    self.assertEqual(code, 1)
                    child.assert_not_called()
