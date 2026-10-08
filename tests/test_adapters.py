"""Independent adapter contracts and core native execution with no provider calls."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import display, harnesses, universe
from multiverse.runtime import native
from multiverse.ui.backend import LocalBackend, harnesses as discover
from multiverse.harnesses.types import CAPABILITIES, SessionHandle, Capability, CommandSpec, LaunchContext, NativeEndpoint, NativeResult
from multiverse.universe.mailbox import append_message, inbox
from multiverse.store import read, write
from multiverse.tools import capabilities, deliver, native_status
from test_multiverse import BaseTest


class AdapterContractTest(BaseTest):
    def setUp(self):
        super().setUp()
        # Claude resumes only a session whose transcript exists; keep that storage out of the real one.
        self.claude_storage = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.claude_storage)}))

    def values(self):
        values = dict(name='literal', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello "quoted"',
                    bootstrap_json='"Hello"', task='--literal $(false) `false` {literal}',
                    agent_dir=str(self.work / 'agent'), harness_dir='/local/harness', session_file=str(self.work / 'agent/session.jsonl'),
                    bootstrap_file='/launch/bootstrap.md', task_file='/launch/task.md', workspace=str(self.work), model='chosen', control_socket='/tmp/owned/rpc.sock')
        session_file = Path(values['session_file'])
        session_file.parent.mkdir(exist_ok=True)
        session_file.write_text(json.dumps({'type': 'session', 'id': values['session_id']}) + '\n')
        return values

    def session(self, name, values):
        if name == 'claude':
            saved = self.claude_storage / 'projects' / '-workspace' / f"{values['session_id']}.jsonl"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text('{"type": "user"}\n')
        return SessionHandle(values['agent_id'], name, values['workspace'],
                             native_id=values['session_id'], session_file=values['session_file'])

    def test_builtins_and_legacy_have_equivalent_launches(self):
        for name in harnesses.BUILTINS:
            with self.subTest(name=name):
                adapter = harnesses.load(self.root, name)
                if name == 'codex':
                    adapter = harnesses.CodexAdapter.configured('codex', {'inbox_bridge': False})
                if name == 'claude':  # the legacy recipe is the plain launch: no bridge flags
                    adapter = harnesses.ClaudeAdapter.configured('claude', {'inbox_bridge': False})
                legacy = harnesses.LegacyAdapter(name, read(harnesses.PACKAGES / name / 'defaults.json'))
                for mode in ('spawn', 'resume'):
                    context = LaunchContext(mode, self.values(), 'chosen')
                    args = (self.session(name, context.values), context) if mode == 'resume' else (context,)
                    actual, expected = getattr(adapter, mode)(*args), getattr(legacy, mode)(*args)
                    expected_argv = list(expected.argv)
                    if name == 'codex':
                        position = expected_argv.index('--')
                        expected_argv[position:position] = ['-c', 'check_for_update_on_startup=false']
                    self.assertEqual(actual.argv, tuple(expected_argv))
                    self.assertEqual(actual.unset_env, expected.unset_env)
                    self.assertEqual(actual.control_endpoint, expected.control_endpoint)
                self.assertEqual(set(adapter.capabilities(SessionHandle("agent", name, str(self.work)))), set(CAPABILITIES))
                for required in ('spawn', 'resume', 'send_steer', 'send_follow_up'):
                    self.assertTrue(adapter.capabilities(SessionHandle("agent", name, str(self.work)))[required].required)

    def test_wake_appends_a_literal_interactive_resume_prompt(self):
        prompt = 'Message from reviewer:\n--literal $(false) `false` {text}'
        for name in ('codex', 'claude', 'pi'):
            for bridge in (False, True):
                with self.subTest(name=name, bridge=bridge):
                    path = self.root / 'harnesses' / name / 'harness.json'
                    spec = read(path)
                    spec['settings']['inbox_bridge'] = bridge
                    write(path, spec)
                    adapter = harnesses.load(self.root, name)
                    values = self.values()
                    session = self.session(name, values)
                    command = adapter.resume(session, LaunchContext('resume', values, 'chosen', resume_prompt=prompt))
                    self.assertEqual(command.argv[-1], prompt)
                    self.assertNotIn('-p', command.argv)
                    self.assertNotIn('--print', command.argv)
                    self.assertIn(values['session_file'] if name == 'pi' else session.native_id, command.argv)
                    self.assertEqual(adapter.capabilities(session)['wake'].state, 'supported')
        legacy = harnesses.LegacyAdapter('custom', {'spawn': ['custom'], 'resume': ['custom', '{session_id}']})
        self.assertEqual(legacy.capabilities(SessionHandle('agent', 'custom', '/tmp'))['wake'].state, 'unavailable')

    def test_configuration_is_declarative_and_never_rewritten(self):
        config = self.root / 'harnesses/codex/harness.json'
        data = read(config)
        data['settings'] = {'model': 'local-model', 'executable': '/custom/codex',
                            'args': ['--sandbox', 'read-only'], 'resume_args': ['--add-dir', '/custom/worktree'],
                            'unset_env': ['CUSTOM_PARENT']}
        write(config, data)
        original = config.read_bytes()
        adapter = harnesses.load(self.root, 'codex')
        values = self.values()
        result = adapter.resume(self.session('codex', values), LaunchContext('resume', values, adapter.default_model))
        self.assertEqual(result.argv[0], '/custom/codex')
        self.assertIn('/custom/worktree', result.argv)
        self.assertIn('read-only', result.argv)
        self.assertIn('local-model', result.argv)
        # Settings replace the default list, so a harness can opt back into NO_COLOR.
        self.assertEqual(set(result.unset_env), {'CODEX_THREAD_ID', 'CLAUDECODE', 'CUSTOM_PARENT'})
        default = harnesses.load(self.root, 'claude').spawn(LaunchContext('spawn', values, None))
        self.assertIn('NO_COLOR', default.unset_env)
        self.assertEqual(config.read_bytes(), original)

    def test_native_session_naming_capabilities_and_launch_arguments(self):
        values = self.values()
        values['name'] = 'literal-name'
        for provider in ('claude', 'pi'):
            for bridge in (False, True):
                adapter = harnesses.BUILTINS[provider].configured(provider, {'inbox_bridge': bridge})
                session = self.session(provider, values)
                self.assertEqual(adapter.capabilities(session)['session_name'].state, 'supported')
                for mode in ('spawn', 'resume'):
                    with self.subTest(provider=provider, bridge=bridge, mode=mode):
                        context = LaunchContext(mode, values)
                        command = adapter.spawn(context) if mode == 'spawn' else adapter.resume(session, context)
                        self.assertEqual(command.argv[command.argv.index('--name') + 1], values['name'])
        for bridge, state in ((False, 'unavailable'), (True, 'supported')):
            adapter = harnesses.CodexAdapter.configured('codex', {'inbox_bridge': bridge})
            capability = adapter.capabilities(self.session('codex', values))['session_name']
            self.assertEqual(capability.state, state)
        legacy = harnesses.LegacyAdapter('custom', {'spawn': ['custom', '{name}']})
        self.assertEqual(legacy.capabilities(self.session('custom', values))['session_name'].state, 'unavailable')

    def test_builtins_accept_empty_initial_prompt_without_empty_native_turn(self):
        for name in harnesses.BUILTINS:
            with self.subTest(name=name):
                adapter = harnesses.load(self.root, name)
                values = {**self.values(), 'task': ''}
                command = adapter.spawn(LaunchContext('spawn', values, 'chosen'))
                self.assertNotIn('', command.argv)
                self.assertNotIn('@' + values['task_file'], command.argv)
                self.assertIn('chosen', command.argv)

    def test_unsupported_side_questions_never_execute_native_commands(self):
        for name in harnesses.BUILTINS:
            with self.subTest(name=name), patch.object(native, 'execute') as execute, patch.object(native, 'execute_rpc') as rpc:
                adapter = harnesses.load(self.root, name)
                session = SessionHandle('agent', name, str(self.work), native_id=str(uuid4()))
                result = adapter.ask(session, 'Side question', native.NativeExecutor(self.work))
                self.assertEqual(result.state, 'unavailable')
                execute.assert_not_called()
                rpc.assert_not_called()

    def test_legacy_preserves_every_custom_argument_and_asset(self):
        path = self.root / 'harnesses/codex/harness.json'
        config = read(harnesses.PACKAGES / 'codex/defaults.json')
        config['spawn'][1:1] = ['--profile', 'my-settings', '--sandbox', 'read-only']
        config['bootstrap'] += '\nA local instruction.'
        config['private_preference'] = {'anything': 'retained'}
        write(path, config)
        before = path.read_bytes()
        adapter = harnesses.load(self.root, 'codex')
        self.assertIs(type(adapter), harnesses.LegacyAdapter)
        self.assertEqual(adapter.spec, config)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('my-settings', adapter.spawn(LaunchContext('spawn', self.values())).argv)

    def test_codex_bridge_keeps_native_preferences_and_rejects_resume_overrides_without_rewriting(self):
        path = self.root / 'harnesses/codex/harness.json'
        spec = read(path)
        spec['settings'] = {'inbox_bridge': True, 'model': 'chosen',
                            'spawn_args': ['--sandbox', 'workspace-write'], 'args': ['--add-dir', '/custom']}
        write(path, spec)
        original = path.read_bytes()
        adapter = harnesses.load(self.root, 'codex')
        values = {**self.values(), 'control_socket': '/tmp/owned/rpc.sock'}
        command = adapter.spawn(LaunchContext('spawn', values, 'chosen'))
        self.assertEqual(command.shared_service.argv[-3:], ('app-server', 'daemon', 'start'))
        self.assertEqual(command.control_endpoint.address, values['control_socket'])
        self.assertIn('/custom', command.rpc_proxy.settings['roots'])
        self.assertIn('workspace-write', command.argv)
        self.assertIn('chosen', command.argv)
        with self.assertRaisesRegex(ValueError, 'saved permissions'):
            adapter.resume(self.session('codex', values), LaunchContext('resume', values, 'chosen'))
        self.assertEqual(path.read_bytes(), original)
        # Initial grants are allowed in spawn_args and inherited on resume.
        spec['settings']['spawn_args'] += spec['settings'].pop('args')
        write(path, spec)
        original = path.read_bytes()
        adapter = harnesses.load(self.root, 'codex')
        resumed = adapter.resume(self.session('codex', values), LaunchContext('resume', values, 'chosen'))
        self.assertNotIn(values['agent_dir'], resumed.argv, 'Native resume inherits the source-owned writable grant')
        self.assertEqual(path.read_bytes(), original)

    def test_resume_uses_explicit_session_not_launch_template_identity(self):
        for name in harnesses.BUILTINS:
            adapter = harnesses.load(self.root, name)
            legacy = harnesses.LegacyAdapter(name, {
                'spawn': ['custom'],
                'resume': ['custom', '{agent_id}', '{session_id}', '{session_file}', '{workspace}'],
            })
            values = self.values()
            session = self.session(name, values)
            other = {**values, 'agent_id': 'wrong-agent', 'session_id': str(uuid4()),
                     'session_file': '/wrong/session.jsonl', 'workspace': '/wrong/worktree'}
            for implementation in (adapter, legacy):
                with self.subTest(name=name, implementation=type(implementation).__name__):
                    expected = implementation.resume(session, LaunchContext('resume', values, 'chosen'))
                    actual = implementation.resume(session, LaunchContext('resume', other, 'chosen'))
                    self.assertEqual(actual, expected)
                    with self.assertRaisesRegex(ValueError, 'different harness'):
                        implementation.resume(replace(session, harness='another'), LaunchContext('resume', values))

    def test_resume_never_falls_back_to_launch_template_session(self):
        for name in harnesses.BUILTINS:
            with self.subTest(name=name):
                adapter = harnesses.load(self.root, name)
                values = self.values()
                session = self.session(name, values)
                missing = replace(session, session_file=None) if name == 'pi' else replace(session, native_id=None)
                with self.assertRaisesRegex(ValueError, 'native session ID|session file is missing'):
                    adapter.resume(missing, LaunchContext('resume', values))

    def test_resume_checks_and_normalizes_saved_uuid_without_a_separate_validation_call(self):
        for name in ('codex', 'claude'):
            with self.subTest(name=name):
                adapter = harnesses.load(self.root, name)
                values = self.values()
                session = self.session(name, values)
                context = LaunchContext('resume', values)
                command = adapter.resume(replace(session, native_id=session.native_id.upper()), context)
                self.assertIn(session.native_id, command.argv)
                with self.assertRaisesRegex(ValueError, 'must be a UUID'):
                    adapter.resume(replace(session, native_id='invalid'), context)

    def test_shared_daemon_launch_preserves_environment_preferences_and_literal_prompt(self):
        path = self.root / 'harnesses/codex/harness.json'
        config = read(path)
        config['settings'] = {'inbox_bridge': True, 'unset_env': ['PARENT_ONLY'],
                              'spawn_args': ['-cshell_environment_policy.exclude=["CUSTOM_EXCLUDED"]']}
        write(path, config)
        before = path.read_bytes()
        adapter = harnesses.load(self.root, 'codex')
        executor = Mock()
        executor.rpc.return_value = NativeResult('ok', value={'exclude': ['USER_EXCLUDED'], 'writable_roots': []})
        values = {**self.values(), 'task': '--config=shell_environment_policy=literal-prompt'}
        environment = {'CODEX_HOME': str(self.work / 'native home'), 'MV2_AGENT_ID': 'current',
                       'MV2_UNIVERSE': str(self.root), 'PATH': '/literal/tools'}
        command = adapter.spawn(LaunchContext('spawn', values, 'chosen', environment,
                                             NativeEndpoint('unix-websocket', '/native/socket'), executor))
        argv = command.argv
        self.assertEqual(argv[argv.index('--remote') + 1], 'unix://' + values['control_socket'])
        self.assertEqual(command.rpc_proxy.upstream_path, '/native/socket')
        excludes = [v for v in argv if v.startswith('shell_environment_policy.exclude=')][-1]
        self.assertEqual(json.loads(excludes.partition('=')[2]),
                         ['USER_EXCLUDED', 'CUSTOM_EXCLUDED', 'MV2_*', 'CODEX_THREAD_ID', 'CLAUDECODE', 'PARENT_ONLY'])
        self.assertIn('shell_environment_policy.set.MV2_AGENT_ID="current"', argv)
        self.assertEqual(argv[-1], values['task'])
        self.assertEqual(command.shared_service.environment['CODEX_HOME'], environment['CODEX_HOME'])
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_and_unknown_settings_fail_cleanly_and_discovery_survives(self):
        path = self.root / 'harnesses/codex/harness.json'
        cases = [[], {'schema': 19}, {'schema': 2, 'adapter': 'missing'},
                 {'schema': 2, 'adapter': 'codex', 'settings': []},
                 {'schema': 2, 'adapter': 'codex', 'settings': {'typo': True}},
                 {'schema': 2, 'adapter': 'codex', 'settings': {'args': '--bad'}},
                 {'schema': 2, 'adapter': 'codex', 'settings': {'args': ['{unknown}']}},
                 {'schema': 2, 'adapter': 'claude', 'settings': {'inbox_bridge': 'yes'}},
                 {'schema': 1, 'spawn': []}, {'schema': 1, 'spawn': [22]},
                 {'schema': 1, 'spawn': ['binary'], 'bootstrap': []}]
        for data in cases:
            with self.subTest(data=data):
                write(path, data)
                with self.assertRaises(ValueError):
                    harnesses.load(self.root, 'codex')
                self.assertNotIn('codex', discover(self.root))

    def test_pi_exact_session_file_required_and_native_id_preserved(self):
        adapter = harnesses.load(self.root, 'pi')
        values = self.values()
        session_file = Path(values['session_file'])
        session = replace(self.session('pi', values), native_id=None)
        context = LaunchContext('resume', values)
        session_file.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            adapter.resume(session, context)
        for contents in ('', 'not json', '{}', '{"type":"session","id":"bad"}', '{"type":"session","id":123}'):
            session_file.write_text(contents)
            with self.assertRaises(ValueError):
                adapter.resume(session, context)
        native_id = str(uuid4())
        # Native files are JSONL, not pretty-printed JSON.
        session_file.write_text(json.dumps({'type': 'session', 'id': native_id}) + '\n')
        command = adapter.resume(replace(session, native_id=native_id.upper()), context)
        self.assertIn(str(session_file), command.argv)
        adapter.resume(session, context)  # A valid Pi file can precede UUID binding.
        with self.assertRaisesRegex(ValueError, 'different native'):
            adapter.resume(replace(session, native_id=str(uuid4())), context)

    def test_pi_bridge_is_source_owned_opt_in_and_requires_current_handshake(self):
        path = self.root / 'harnesses/pi/harness.json'
        self.assertFalse((path.parent / 'extension.ts').exists())
        config = read(path)
        config['settings']['inbox_bridge'] = True
        write(path, config)
        adapter = harnesses.load(self.root, 'pi')
        argv = adapter.spawn(LaunchContext('spawn', self.values())).argv
        self.assertEqual(argv[argv.index('--extension') + 1], str(harnesses.PACKAGES / 'pi/extension.ts'))
        for bridge, expected in ((None, 'unavailable'), ('pi-socket-v1', 'supported')):
            session = SessionHandle('agent', 'pi', str(self.work), native_id=str(uuid4()), state='running',
                                    bridge=bridge, endpoint=NativeEndpoint('unix-json', '/tmp/test.sock'))
            caps = adapter.capabilities(session)
            self.assertEqual(caps['send_steer'].state, expected)
            self.assertEqual(caps['send_follow_up'].state, expected)
            self.assertEqual(caps['ask'].state, 'unavailable')

    def test_plain_codex_does_not_claim_steering_or_fall_back_to_queue(self):
        adapter = harnesses.load(self.root, 'codex')
        session = SessionHandle('agent', 'codex', str(self.work), native_id=str(uuid4()))
        from multiverse.harnesses.types import Message
        from unittest.mock import Mock
        executor = Mock()
        self.assertEqual(adapter.capabilities(session)['send_steer'].state, 'unavailable')
        self.assertEqual(adapter.send(session, Message('id', 'text'), executor).state, 'unavailable')
        executor.rpc.assert_not_called()
        executor.command.assert_not_called()

    def test_pi_sends_framed_text_with_separate_receipt_metadata(self):
        from multiverse.harnesses.types import Message
        from unittest.mock import Mock
        adapter = harnesses.load(self.root, 'pi')
        session = SessionHandle('agent', 'pi', str(self.work), native_id=str(uuid4()), state='running',
                                bridge='pi-socket-v1', endpoint=NativeEndpoint('unix-json', '/tmp/test.sock'))
        executor = Mock()
        for mode in ('steer', 'follow-up'):
            message = Message(str(uuid4()), 'literal body', delivery=mode, framed='Message from reviewer:\nliteral body')
            adapter.send(session, message, executor)
            payload = executor.request.call_args.args[1]
            self.assertEqual(payload, {'operation': 'send', 'message_id': message.id,
                                      'text': message.framed, 'mode': mode})
            self.assertNotIn(message.id, payload['text'])


class NativeDeliveryTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.native_id = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.agent, 'name': 'worker', 'harness': 'codex',
               'native_session_id': self.native_id, 'workspace': str(self.work)})
        write(self.directory / 'runtime.json', {'launch_id': 'current', 'state': 'running'})

    def test_unsupported_steering_never_falls_back_to_queue_or_terminal_input(self):
        message = append_message(self.directory, 'steer me', {}, delivery='steer')
        with patch.object(native, 'owned'), patch.object(native, 'execute') as execute:
            result = native.deliver(self.root, self.agent, message['id'])
            self.assertEqual(result['state'], 'unavailable')
            execute.assert_not_called()
        self.assertEqual(inbox(self.directory)['messages'], [])
        self.assertEqual(len(inbox(self.directory, all_messages=True)['messages']), 1)
        self.assertEqual(native_status.run(self.root, agent=self.agent, operation='ask')['state'], 'unavailable')

    def test_wrong_recipient_and_stale_terminal_never_receive(self):
        with self.assertRaisesRegex(ValueError, 'recipient inbox'):
            native.deliver(self.root, self.agent, str(uuid4()))
        message = append_message(self.directory, 'not to stale pane', {}, delivery='steer')
        with patch.object(native, 'owned', side_effect=ValueError('wrong launch')), patch.object(native, 'execute') as execute:
            result = native.deliver(self.root, self.agent, message['id'])
            self.assertIn(result['state'], ('error', 'unknown'))
            execute.assert_not_called()

    def test_shared_tools_and_backend_return_same_capabilities(self):
        self.assertEqual(capabilities.run(self.root, agent=self.agent), LocalBackend().capabilities(self.root, self.agent))
        self.assertEqual(native_status.run(self.root, agent=self.agent), LocalBackend().native_status(self.root, self.agent))

    def test_optional_usage_failure_is_diagnosable_and_does_not_break_snapshot(self):
        for result in (NativeResult('unavailable', 'not installed'), NativeResult('error', 'broken pipe')):
            with patch.object(harnesses.CodexAdapter, 'get_usage', return_value=result):
                snapshot = LocalBackend().snapshot(self.root)
                row = snapshot['agents'][0]
                self.assertEqual(row['tokens'], 'N/A')
                self.assertEqual(row['usage']['state'], result.state)
                self.assertIn(result.reason, row['usage_detail'])
        with patch.object(harnesses.CodexAdapter, 'get_usage', side_effect=RuntimeError('adapter failed')):
            self.assertEqual(LocalBackend().snapshot(self.root)['agents'][0]['usage']['state'], 'error')
        self.assertEqual(display.usage_total({'state': 'ok', 'value': {'total_tokens': 0}}), '0')
        self.assertEqual(display.usage_total({'state': 'ok', 'value': {'total_tokens': None}}), 'N/A')

    def test_native_subprocess_scrubs_identity_and_preserves_literal_argv(self):
        script = 'import json,os,sys; print(json.dumps([sys.argv[1:], sorted(k for k in os.environ if k.startswith("MV2_") or k in ("CODEX_THREAD_ID","CLAUDECODE"))]))'
        with patch.dict(os.environ, MV2_AGENT_ID='parent', CODEX_THREAD_ID='parent', CLAUDECODE='parent'):
            result = native.execute(CommandSpec((sys.executable, '-c', script, '$(false)\n{literal}')), self.work)
        self.assertEqual(json.loads(result.stdout), [['$(false)\n{literal}'], []])


if __name__ == '__main__':
    unittest.main()
