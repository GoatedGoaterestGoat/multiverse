"""Codex auto selection: one preparation attempt, safe fallback, visible evidence."""
from dataclasses import asdict
import json
import os
import socket
import sys
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse import agents, harnesses, native, runner
from multiverse.harnesses.types import CommandSpec, NativeResult
from multiverse.store import append_message, message_record, read, update_message, write
from multiverse.tools.capabilities import run as capabilities
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
        self.enterContext(patch.dict(os.environ, {'CODEX_HOME': str(self.work / 'native-home')}))
        self.set_case()

    def set_case(self, **changes):
        write(self.case, {'info': self.info, **changes})

    def configure(self, **settings):
        write(self.config, {'schema': 2, 'adapter': 'codex', 'settings': {
            'executable': str(self.executable), **settings}})

    def launch(self, name='auto', resume=None, native_home=None, **kwargs):
        with patch.dict(os.environ, {'CODEX_HOME': str(native_home or self.work / 'native-home')}), \
             patch.object(native.NativeExecutor, 'rpc', return_value=NativeResult('ok', value={'exclude': [], 'writable_roots': []})):
            result = agents.start(self.root, name, 'literal task', resume=resume, socket_path=self.socket, **kwargs)
        directory = agents.directory(self.root, result['id'])
        until(lambda: (directory / 'executed.json').exists())
        return result, directory

    def test_auto_uses_bridge_without_an_extra_service_start(self):
        self.configure()
        launched, directory = self.launch()
        self.assertEqual(agents.runtime(directory)['native_mode'], {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None})
        self.assertIn('--remote', read(directory / 'executed.json'))
        calls = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertEqual(calls[:2], [['app-server', 'daemon', 'start'], ['--help']])
        self.assertEqual(len(calls), 3)
        self.assertIn('bridge (auto)', capabilities(self.root, agent=launched['id'])['spawn']['reason'])
        self.assertEqual(capabilities(self.root, agent=launched['id'])['session_name']['state'], 'supported')

    def test_auto_falls_back_before_child_for_each_unavailable_bridge(self):
        cases = [({'failure': 'old CLI: no app-server daemon'}, 'no app-server daemon'),
                 ({'failure': 'daemon start failed'}, 'daemon start failed'),
                 ({'info': {**self.info, 'appServerVersion': '2'}}, 'version mismatch'),
                 ({'help': 'usage: codex [PROMPT]'}, '--remote')]
        for index, (changes, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                self.set_case(**changes)
                self.configure(args=['--dangerously-bypass-approvals-and-sandbox'])
                before = self.config.read_bytes()
                launched, directory = self.launch(name=f'fallback-{index}', wait=True, wait_timeout=5)
                self.assertTrue(launched['ready'], launched)
                facts = agents.runtime(directory)
                self.assertEqual(facts['native_mode']['mode'], 'plain')
                self.assertIn(reason, facts['native_mode']['fallback_reason'])
                self.assertIsNone(facts['control_endpoint'])
                argv = read(directory / 'executed.json')
                self.assertNotIn('--remote', argv)
                self.assertIn('--dangerously-bypass-approvals-and-sandbox', argv)
                caps = capabilities(self.root, agent=launched['id'])
                self.assertIn(reason, caps['resume']['reason'])
                self.assertEqual(caps['session_name']['state'], 'unavailable')
                self.assertIsNone(harnesses.load(self.root, 'codex').status_watch(
                    native.context(self.root, launched['id'])[2]))
                self.assertEqual(self.config.read_bytes(), before)
                agents.kill(self.root, launched['id'])
        self.assertTrue(self.address.exists(), 'Fallback must not remove the shared service socket')

    def test_explicit_true_is_strict_and_false_never_prepares_bridge(self):
        self.set_case(failure='daemon unavailable')
        self.configure(inbox_bridge=True)
        with self.assertRaisesRegex(RuntimeError, 'daemon unavailable'):
            self.launch('strict')
        strict_dir = agents.directory(self.root, 'strict')
        self.assertEqual(agents.runtime(strict_dir)['state'], 'failed')
        self.assertEqual(agents.runtime(strict_dir)['native_mode']['policy'], 'required')
        self.assertFalse((strict_dir / 'executed.json').exists())
        self.configure(inbox_bridge=False)
        with patch.object(native, 'start_shared_service', side_effect=AssertionError('Plain must not start a service')):
            result, directory = self.launch('plain', wait=True, wait_timeout=5)
        self.assertTrue(result['ready'])
        self.assertEqual(agents.runtime(directory)['native_mode'], {'policy': 'plain', 'mode': 'plain', 'fallback_reason': None})

    def test_resume_reselects_mode_and_preserves_exact_identity(self):
        self.set_case(failure='temporarily unavailable')
        self.configure(args=['--dangerously-bypass-approvals-and-sandbox'])
        first, directory = self.launch(wait=True, wait_timeout=5)
        native_id = str(uuid4())
        agents.bind_session(self.root, first['id'], native_id)
        agents.kill(self.root, first['id'])
        (directory / 'executed.json').unlink()
        self.set_case()
        resumed, directory = self.launch(resume=first['id'])
        self.assertEqual(resumed['native_session_id'], native_id)
        self.assertEqual(resumed['runtime']['native_mode']['mode'], 'bridge')
        request = read(directory / 'launches' / resumed['runtime']['launch_id'] / 'request.json')
        self.assertIn(native_id, request['argv'])
        self.assertEqual(request['rpc_proxy']['settings']['resume_permissions'],
                         {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'})
        self.assertEqual(resumed['workspace'], first['workspace'])
        self.assertEqual(resumed['task'], first['task'])
        # The fixture has no model work to cancel. End its real child and pane,
        # then make the bridge unavailable for the next exact-session resume.
        with patch.object(native, 'stop', return_value=NativeResult('ok')):
            agents.kill(self.root, first['id'])
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

    def test_config_rpc_unavailability_falls_back_before_any_native_child(self):
        self.configure()
        with patch.object(native.NativeExecutor, 'rpc', return_value=NativeResult('unavailable', 'old config API')):
            launched = agents.start(self.root, 'config-fallback', 'task', socket_path=self.socket, wait=True, wait_timeout=5)
        self.assertTrue(launched['ready'], launched)
        self.assertEqual(launched['runtime']['native_mode']['mode'], 'plain')
        self.assertIn('old config API', launched['runtime']['native_mode']['fallback_reason'])


class AutoRunnerTest(BaseTest):
    def test_relay_start_failure_only_falls_back_before_child_start(self):
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
                     patch.object(runner.subprocess, 'Popen', return_value=process) as child, \
                     patch.object(harnesses, 'load'), patch('multiverse.rpc.LaunchProxy', return_value=proxy):
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
