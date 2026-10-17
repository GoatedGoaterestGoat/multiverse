"""Shared-conversation identity and native launch refusals, without a real daemon."""
from copy import deepcopy
import unittest
from uuid import uuid4
from dataclasses import replace
import json
from pathlib import Path

from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import LaunchContext, NativeEndpoint, SessionHandle


class NativeRefusalTest(unittest.TestCase):
    def setUp(self):
        self.native_id = str(uuid4())
        self.settings = {'native_id': self.native_id, 'workspace': '/workspace',
                         'roots': [], 'bootstrap': '', 'resume_permissions': {}}
        self.protocol = LaunchProtocol(self.settings)

    def request(self, identifier=1):
        return self.protocol.outgoing({'id': identifier, 'method': 'thread/resume',
            'params': {'threadId': self.native_id}})

    def test_native_error_is_retained_without_binding_or_reply_mutation(self):
        self.request()
        reply = {'id': 1, 'error': {'code': -32600,
            'message': f'thread {self.native_id} already has an active writer'}}
        original = deepcopy(reply)
        self.assertIsNone(self.protocol.incoming(reply))
        self.assertFalse(self.protocol.bound)
        self.assertEqual(self.protocol.launch_refusal['code'], -32600)
        self.assertEqual(self.protocol.launch_refusal['message'], reply['error']['message'])
        self.assertEqual(self.protocol.launch_refusal['method'], 'thread/resume')
        self.assertEqual(reply, original)

    def test_native_retry_never_clears_refusal_or_releases_input(self):
        self.request()
        self.protocol.incoming({'id': 1, 'error': {'code': -32600, 'message': 'writer is busy'}})
        first = deepcopy(self.protocol.launch_refusal)
        self.request(2)
        self.assertIsNone(self.protocol.incoming({'id': 2, 'result': {
            'thread': {'id': self.native_id, 'cwd': '/workspace'}}}))
        self.assertEqual(self.protocol.launch_refusal, first)
        self.assertFalse(self.protocol.bound)
        with self.assertRaisesRegex(ValueError, 'writer is busy'):
            self.protocol.outgoing({'id': 3, 'method': 'turn/start',
                'params': {'threadId': self.native_id, 'input': []}})

    def test_pipelined_input_cannot_escape_pending_binding(self):
        self.request()
        with self.assertRaisesRegex(ValueError, 'binding is not confirmed'):
            self.protocol.outgoing({'id': 2, 'method': 'turn/start',
                'params': {'threadId': self.native_id, 'input': []}})

    def test_navigation_rejection_does_not_fail_a_bound_launch(self):
        self.request()
        self.protocol.incoming({'id': 1, 'result': {
            'thread': {'id': self.native_id, 'cwd': '/workspace'}}})
        with self.assertRaises(ValueError):
            self.protocol.outgoing({'id': 2, 'method': 'thread/start', 'params': {}})
        self.assertIsNone(self.protocol.launch_refusal)
        self.assertTrue(self.protocol.bound)


class PlainRefusalTest(unittest.TestCase):
    def test_version_support_gate_accepts_only_tested_layouts(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        capture = json.loads((Path(__file__).parent / 'fixtures/codex/refusal-0.163.0.json').read_text())[0]
        for version in ('0.163.0', 'codex-cli 0.163.0', (0, 163, 0), [0, 163, 0]):
            with self.subTest(version=version):
                self.assertTrue(adapter.supports_launch_refusal(version))
                self.assertIsNotNone(adapter.launch_refusal({**capture, 'cli_version': version}))
        for version in (None, '', '0.162.1', '0.164.0', 'codex-cli 0.163.0-dev',
                        {}, [0, 163], [False, 163, False], (0, '163', 0), True):
            with self.subTest(version=version):
                self.assertFalse(adapter.supports_launch_refusal(version))
                self.assertIsNone(adapter.launch_refusal({**capture, 'cli_version': version}))

    def evidence(self, width=120):
        import textwrap
        lines = ['  >_ OpenAI Codex (v0.163.0)', '  fixture workspace', '']
        lines.extend(textwrap.wrap('🔒 This conversation is open in another app', width))
        lines.extend(textwrap.wrap('Close it there and press R to continue here.', width))
        lines.append('')
        lines.extend(textwrap.wrap('r retry   f fork   esc/⌃c/q exit   ⌃t transcript', width))
        return {'cli_version': '0.163.0', 'text': '\n'.join(lines) + '\n\n',
                'cursor_visible': False, 'native_id': str(uuid4())}

    def classify(self, evidence):
        from multiverse.harnesses.codex.refusal import plain_refusal
        return plain_refusal(evidence)

    def test_active_native_screen_at_tested_widths(self):
        for width in (24, 40, 80, 120):
            with self.subTest(width=width):
                refusal = self.classify(self.evidence(width))
                self.assertIn('This conversation is open in another app', refusal['message'])
                self.assertIsNone(refusal['code'])
                self.assertEqual(refusal['source'], 'native-screen')

    def test_real_0163_owned_pane_captures(self):
        path = Path(__file__).parent / 'fixtures' / 'codex' / 'refusal-0.163.0.json'
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        for capture in json.loads(path.read_text()):
            with self.subTest(width=capture['width']):
                evidence = {**capture, 'cli_version': '0.163.0', 'native_id': str(uuid4())}
                self.assertEqual(adapter.launch_refusal(evidence)['source'], 'native-screen')
                evidence['text'] += '\n› quoted native refusal above; normal composer below\n'
                self.assertIsNone(adapter.launch_refusal(evidence))

    def test_history_prompt_and_incomplete_screen_are_not_native_refusals(self):
        evidence = self.evidence()
        variants = [
            {**evidence, 'text': evidence['text'] + '› Ask Codex to do anything\n'},
            {**evidence, 'cursor_visible': True},
            {**evidence, 'text': evidence['text'].replace('🔒 ', '> ')},
            {**evidence, 'text': evidence['text'].replace('f fork', 'quoted prose')},
            {**evidence, 'cli_version': '0.164.0'},
            {**evidence, 'cli_version': None},
        ]
        for value in variants:
            with self.subTest(value=value):
                self.assertIsNone(self.classify(value))


class PlainStartupTest(unittest.TestCase):
    """Actual empty composers from an isolated, model-free 0.163.0 TUI."""
    def setUp(self):
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        self.fixtures = Path(__file__).parent / 'fixtures/codex'

    def capture(self, name):
        return [{**frame, 'cli_version': '0.163.0'}
                for frame in json.loads((self.fixtures / name).read_text())]

    def test_real_ready_composers_end_startup_observation(self):
        for frame in self.capture('started-0.163.0.json'):
            with self.subTest(width=frame['width']):
                self.assertTrue(self.adapter.launch_started(frame))
                self.assertIsNone(self.adapter.launch_refusal(frame))

    def test_loading_and_refused_native_composers_do_not_confirm_startup(self):
        for name in ('loading-0.163.0.json', 'resuming-0.163.0.json', 'refusal-0.163.0.json'):
            for frame in self.capture(name):
                with self.subTest(fixture=name, width=frame['width']):
                    self.assertFalse(self.adapter.launch_started(frame))
        loading = self.capture('loading-0.163.0.json')[0]
        self.assertFalse(self.adapter.launch_started({**loading,
            'text': loading['text'] + '\n  fixture default · /fixture/workspace\n'}))
        pending = self.capture('resuming-0.163.0.json')[0]
        self.assertFalse(self.adapter.launch_started({**pending, 'text': pending['text'].replace(
            'Resuming session…', 'Forking session…') + '\n  fixture default · /fixture/workspace\n'}))

    def test_cursor_history_and_malformed_evidence_do_not_confirm_startup(self):
        frame = self.capture('started-0.163.0.json')[1]
        variants = [None, {}, {**frame, 'cursor_visible': False}, {**frame, 'cursor_visible': 1},
            {**frame, 'cursor_x': 1}, {**frame, 'cursor_x': True}, {**frame, 'cursor_y': True},
            {**frame, 'cursor_y': -1}, {**frame, 'cursor_y': 1000}, {**frame, 'cursor_y': '9'},
            {key: value for key, value in frame.items() if key != 'cursor_y'},
            {**frame, 'text': None}, {**frame, 'text': '\x1b[0m' + frame['text']},
            {**frame, 'text': 'x' * 32769}, {**frame, 'text': frame['text'] + '\0'},
            {**frame, 'text': frame['text'].replace('› Ask', '> › Ask')},
            {**frame, 'text': frame['text'] + '\n› A different composer below\n'},
            {**frame, 'text': frame['text'].split('  fixture default')[0]},
            {**frame, 'text': frame['text'].replace(' · ', ' unknown-footer ')},
            {**frame, 'text': frame['text'].split('  fixture default')[0] + '  ? for shortcuts\n'},
            {**frame, 'cursor_y': frame['cursor_y'] - 1}]
        for index, value in enumerate(variants):
            with self.subTest(case=index):
                self.assertFalse(self.adapter.launch_started(value))

    def test_untested_versions_never_confirm_startup(self):
        frame = self.capture('started-0.163.0.json')[1]
        for version in (None, '', '0.162.1', '0.164.0', [False, 163, False], {'version': '0.163.0'}):
            with self.subTest(version=version):
                self.assertFalse(self.adapter.launch_started({**frame, 'cli_version': version}))


class SharedIdentityTest(unittest.TestCase):
    def test_old_bridge_environment_requests_restart_without_mutating_it(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        session = SessionHandle('agent', 'codex', '/workspace', native_id=str(uuid4()),
                                native_mode={'mode': 'bridge'})
        environment = {'MV2_AGENT_ID': 'agent', 'MV2_LAUNCH_ID': str(uuid4()),
                       'CODEX_THREAD_ID': session.native_id}
        before = dict(environment)
        self.assertIn('with no other client attached', adapter.agent_identity_problem(session, environment))
        self.assertEqual(environment, before)
        self.assertIsNone(adapter.agent_identity_problem(replace(session, native_mode={'mode': 'plain'}), environment))
        self.assertIsNone(adapter.agent_identity_problem(session, {**environment, 'MV2_IDENTITY_MODE': 'codex-agent-v1'}))

    def context(self, launch):
        stable = {'MV2_AGENT_ID': 'agent', 'MV2_AGENT_DIR': '/outside/state/agents/agent',
                  'MV2_UNIVERSE': '/outside', 'MV2_CLI': '/tools/mv2', 'MV2_PYTHON': '/tools/python', 'PATH': '/tools'}
        return LaunchContext('spawn', {'name': 'worker', 'workspace': '/workspace',
            'agent_dir': stable['MV2_AGENT_DIR'], 'agent_id': 'agent', 'bootstrap_json': json.dumps('core bootstrap'),
            'task': 'hello', 'session_id': str(uuid4()), 'control_socket': f'/tmp/{launch}/rpc.sock'},
            environment={**stable, 'MV2_LAUNCH_ID': launch, 'MV2_CONTROL_SOCKET': f'/tmp/{launch}/rpc.sock',
                         'MV2_TMUX_SOCKET': f'/tmux/{launch}', 'CODEX_HOME': '/native/home'},
            agent_environment=stable, endpoint=NativeEndpoint('unix-websocket', '/native.sock'))

    def native_frame(self, command, config=None):
        return LaunchProtocol(command.rpc_proxy.settings).outgoing({'id': 1, 'method': 'thread/start',
            'params': {'config': config or {}}})

    def test_tool_identity_is_stable_private_attachment_changes(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        a, b = (adapter.spawn(self.context(launch)) for launch in ('old', 'new'))
        self.assertNotEqual(a.rpc_proxy.socket_path, b.rpc_proxy.socket_path)
        first = self.native_frame(a)['params']['config']['shell_environment_policy']
        second = self.native_frame(b)['params']['config']['shell_environment_policy']
        self.assertEqual(first, second)
        self.assertEqual(first['set']['MV2_IDENTITY_MODE'], 'codex-agent-v1')
        self.assertEqual(first['set']['CODEX_HOME'], '/native/home')
        for key in ('MV2_LAUNCH_ID', 'MV2_CONTROL_SOCKET', 'MV2_TMUX_SOCKET', 'CODEX_THREAD_ID'):
            self.assertNotIn(key, first['set'])
        self.assertFalse(any('shell_environment_policy.set.MV2_' in arg for arg in a.argv))
        self.assertEqual(a.agent_identity, adapter.agent_identity_policy())
        self.assertIn('MV2_*', first['exclude'])

    def test_native_or_user_config_cannot_restore_volatile_identity(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        command = adapter.spawn(self.context('launch'))
        native = {'shell_environment_policy.set.MV2_LAUNCH_ID': 'old',
                  'shell_environment_policy': {'set': {'MV2_CONTROL_SOCKET': '/old',
                      'MV2_TMUX_SOCKET': '/old-tmux', 'MV2_AGENT_ID': 'other',
                      'MV2_MY_PLUGIN_SETTING': 'plugin-value',
                      'APP_TOKEN': 'secret', 'CODEX_HOME': '/wrong'}}}
        values = self.native_frame(command, native)['params']['config']['shell_environment_policy']['set']
        self.assertNotIn('MV2_LAUNCH_ID', values)
        self.assertNotIn('MV2_CONTROL_SOCKET', values)
        self.assertNotIn('MV2_TMUX_SOCKET', values)
        self.assertEqual(values['MV2_AGENT_ID'], 'agent')
        self.assertEqual(values['CODEX_HOME'], '/native/home')
        self.assertEqual(values['APP_TOKEN'], 'secret')
        self.assertEqual(values['MV2_MY_PLUGIN_SETTING'], 'plugin-value')

    def test_plain_and_auto_fallback_keep_launch_policy(self):
        for settings in ({'inbox_bridge': False}, {}):
            adapter = CodexAdapter.configured('codex', settings)
            command = adapter.spawn(self.context('launch'))
            plain = command.fallback if command.shared_service else command
            self.assertIsNone(plain.agent_identity)

    def test_bridge_bootstrap_omits_prompt_binding_plain_retains_it(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        context = self.context('launch')
        context = replace(context, values={**context.values,
            'bootstrap_json': json.dumps('core bootstrap\n' + adapter.bootstrap)})
        command = adapter.spawn(context)
        self.assertNotIn('session-bind', command.rpc_proxy.settings['bootstrap'])
        self.assertIn('core bootstrap', command.rpc_proxy.settings['bootstrap'])

    def test_explicit_user_instructions_are_preserved(self):
        default = CodexAdapter.configured('codex', {}).bootstrap
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True,
            'args': ['-c', 'developer_instructions=' + json.dumps(default)]})
        self.assertEqual(adapter.spawn(self.context('launch')).rpc_proxy.settings['bootstrap'], default)
