"""Shared-conversation identity and native launch refusals, without a real daemon."""
from copy import deepcopy
from dataclasses import replace
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import json
from pathlib import Path

from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.identity import storage_environment
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

    def test_no_input_passes_before_the_thread_is_confirmed(self):
        """No turn/start passes while the binding is pending or refused, and a native retry never clears the refusal;
        fragile because the Codex TUI pipelines turn/start without waiting for the thread/resume reply, and a break
        puts input into an unconfirmed or foreign conversation (661fc41)."""
        def turn():
            return {'id': 9, 'method': 'turn/start', 'params': {'threadId': self.native_id, 'input': []}}
        with self.subTest(binding='pending'):
            self.request()
            with self.assertRaisesRegex(ValueError, 'binding is not confirmed'):
                self.protocol.outgoing(turn())
        with self.subTest(binding='refused'):
            reply = {'id': 1, 'error': {'code': -32600,
                'message': f'thread {self.native_id} already has an active writer'}}
            original = deepcopy(reply)
            self.assertIsNone(self.protocol.incoming(reply))
            self.assertFalse(self.protocol.bound)
            self.assertEqual(self.protocol.launch_refusal['code'], -32600)
            self.assertEqual(self.protocol.launch_refusal['message'], reply['error']['message'])
            self.assertEqual(self.protocol.launch_refusal['method'], 'thread/resume')
            self.assertEqual(reply, original)
            with self.assertRaisesRegex(ValueError, 'already has an active writer'):
                self.protocol.outgoing(turn())
        with self.subTest(binding='native retry after refusal'):
            first = deepcopy(self.protocol.launch_refusal)
            self.request(2)
            self.assertIsNone(self.protocol.incoming({'id': 2, 'result': {
                'thread': {'id': self.native_id, 'cwd': '/workspace'}}}))
            self.assertEqual(self.protocol.launch_refusal, first)
            self.assertFalse(self.protocol.bound)
            with self.assertRaisesRegex(ValueError, 'already has an active writer'):
                self.protocol.outgoing(turn())


class PlainRefusalTest(unittest.TestCase):
    def evidence(self, width=120):
        import textwrap
        lines = ['  >_ OpenAI Codex (v0.163.0)', '  fixture workspace', '']
        lines.extend(textwrap.wrap('🔒 This conversation is open in another app', width))
        lines.extend(textwrap.wrap('Close it there and press R to continue here.', width))
        lines.append('')
        lines.extend(textwrap.wrap('r retry   f fork   esc/⌃c/q exit   ⌃t transcript', width))
        return {'cli_version': '0.164.0', 'text': '\n'.join(lines) + '\n\n',
                'cursor_visible': False, 'native_id': str(uuid4())}

    def classify(self, evidence):
        from multiverse.harnesses.codex.refusal import plain_refusal
        return plain_refusal(evidence)

    def test_refusal_classifier_on_real_0163_frames(self):
        """Captured 0.163.0 panes, and near-misses, classify as refused or not only for tested layouts; fragile because
        it scrapes Codex's TUI text, and a false positive marks a healthy agent refused (3a83ea2, 661fc41)."""
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        captures = json.loads((Path(__file__).parent / 'fixtures/codex/refusal-0.163.0.json').read_text())
        # 0.164.0 kept 0.163.0's layouts: both checked releases keep the refusal check.
        for version in ('0.164.0', 'codex-cli 0.164.0', (0, 164, 0), [0, 164, 0],
                        '0.163.0', 'codex-cli 0.163.0', (0, 163, 0), [0, 163, 0]):
            with self.subTest(version=version):
                self.assertTrue(adapter.supports_launch_refusal(version))
                self.assertIsNotNone(adapter.launch_refusal({**captures[0], 'cli_version': version}))
        for version in (None, '', '0.162.1', '0.165.0', 'codex-cli 0.163.0-dev', 'codex-cli codex-cli 0.163.0',
                        {}, [0, 163], [False, 163, False], (0, '163', 0), True):
            with self.subTest(version=version):
                self.assertFalse(adapter.supports_launch_refusal(version))
                self.assertIsNone(adapter.launch_refusal({**captures[0], 'cli_version': version}))
        for width in (24, 40, 80, 120):
            with self.subTest(drawn_width=width):
                refusal = self.classify(self.evidence(width))
                self.assertIn('This conversation is open in another app', refusal['message'])
                self.assertIsNone(refusal['code'])
                self.assertEqual(refusal['source'], 'native-screen')
        for capture, version in ((capture, version) for capture in captures for version in ('0.163.0', '0.164.0')):
            with self.subTest(captured_width=capture['width'], version=version):
                evidence = {**capture, 'cli_version': version, 'native_id': str(uuid4())}
                self.assertEqual(adapter.launch_refusal(evidence)['source'], 'native-screen')
                evidence['text'] += '\n› quoted native refusal above; normal composer below\n'
                self.assertIsNone(adapter.launch_refusal(evidence))
        evidence = self.evidence()
        variants = [
            {**evidence, 'text': evidence['text'] + '› Ask Codex to do anything\n'},
            {**evidence, 'cursor_visible': True},
            {**evidence, 'text': evidence['text'].replace('🔒 ', '> ')},
            {**evidence, 'text': evidence['text'].replace('f fork', 'quoted prose')},
            {**evidence, 'cli_version': '0.165.0'},
            {**evidence, 'cli_version': None},
        ]
        for value in variants:
            with self.subTest(near_miss=value):
                self.assertIsNone(self.classify(value))


class PlainStartupTest(unittest.TestCase):
    """Recorded 0.163.0 layouts, retained by the checked CLI and verified natively."""
    def setUp(self):
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        self.fixtures = Path(__file__).parent / 'fixtures/codex'

    def capture(self, name):
        return [{**frame, 'cli_version': '0.164.0'}
                for frame in json.loads((self.fixtures / name).read_text())]

    def test_startup_detector_on_real_0163_frames(self):
        """Captured 0.163.0 composers, footers and cursors confirm plain startup only when the composer is really
        ready; fragile because it depends on Codex's footer and cursor layout, and a false start releases queued input
        into a loading screen (a9ca2fc, 373f62c, 88a3348)."""
        for frame in self.capture('started-0.163.0.json'):
            with self.subTest(ready=frame['width']):
                self.assertTrue(self.adapter.launch_started(frame))
                self.assertTrue(self.adapter.launch_started({**frame, 'cli_version': '0.163.0'}))  # its own release
                self.assertIsNone(self.adapter.launch_refusal(frame))
        layouts = self.capture('started-layouts-0.163.0.json')
        self.assertEqual(len(layouts), 12)
        for frame in layouts:
            with self.subTest(footer=frame['footer'], width=frame['width']):
                self.assertTrue(self.adapter.launch_started(frame))
                self.assertIsNone(self.adapter.launch_refusal(frame))
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
        for name in ('loading-0.163.0.json', 'resuming-0.163.0.json'):
            for frame in self.capture(name):
                clipped = {**frame, 'width': 24, 'text': frame['text'].replace(
                    '› Ask Codex to do anything', '› Ask Codex to do anyth')}
                for footer in ('  model', '  100% context left'):
                    with self.subTest(clipped=name, footer=footer):
                        clipped_footer = {**clipped, 'text': clipped['text'] + footer + '\n'}
                        self.assertFalse(self.adapter.launch_started(clipped_footer))
        frame = layouts[-1]
        prefix = '\n'.join(frame['text'].splitlines()[:frame['cursor_y'] + 1]) + '\n\n'
        for footer in ('  ? for shortcuts', '  Waiting for startup  esc cancel', '  esc cancel', '',
                       '  ? for shortcuts  101% context left', '  -1% context left', '  1000% context left',
                       '  F1 for shortcuts', '  ctrl+space for shortcuts', '  ← for agents',
                       '  ← for agents · F1 for shortcuts', '  ⌃c again to quit', '  esc to edit last message'):
            with self.subTest(hint_only=footer):
                self.assertFalse(self.adapter.launch_started({**frame, 'text': prefix + footer + '\n'}))
        for width in (None, '', False, 0, 16, 23, 25, 10000):
            with self.subTest(wrong_width=width):
                self.assertFalse(self.adapter.launch_started({**frame, 'width': width}))
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
            {**frame, 'text': frame['text'].split('  fixture default')[0] + '  ? for shortcuts\n'},
            {**frame, 'cursor_y': frame['cursor_y'] - 1}]
        for index, value in enumerate(variants):
            with self.subTest(malformed=index):
                self.assertFalse(self.adapter.launch_started(value))
        for version in (None, '', '0.162.1', '0.165.0', [False, 163, False], {'version': '0.163.0'}):
            with self.subTest(version=version):
                self.assertFalse(self.adapter.launch_started({**frame, 'cli_version': version}))


class SharedIdentityTest(unittest.TestCase):
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

    def test_tool_shell_identity_is_stable_and_never_per_launch(self):
        """Codex tools keep stable identity and storage, including another harness's values or absence; drift can silently authorize the wrong launch or open another home."""
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        for selection, permitted in ((None, False), ({}, False), ({'mode': 'plain'}, False),
                                     ({'mode': 'unknown'}, False), (['bridge'], False),
                                     ({'policy': 'auto', 'mode': 'bridge'}, True),
                                     ({'policy': 'required', 'mode': 'bridge'}, True)):
            with self.subTest(source='recorded launch opt-in', selection=selection):
                session = SessionHandle('agent', 'codex', '/workspace', native_mode=selection)
                hook = getattr(adapter, 'allow_recorded_launch', lambda session: False)
                self.assertIs(hook(session), permitted)
        with self.subTest(source='two launches'):
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
        with self.subTest(source='native or user config'):
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
        for storage, expected in (
                ({}, {'CODEX_HOME': '/launch/home/.codex'}),
                ({'CODEX_HOME': ''}, {'CODEX_HOME': '/launch/home/.codex'}),
                ({'CODEX_SQLITE_HOME': ''}, {'CODEX_HOME': '/launch/home/.codex'}),
                ({'CODEX_HOME': '/native/home', 'CODEX_SQLITE_HOME': '/native/database'},
                 {'CODEX_HOME': '/native/home', 'CODEX_SQLITE_HOME': '/native/database'}),
                ({'CODEX_HOME': 'relative-home', 'CODEX_SQLITE_HOME': 'relative-database'},
                 {'CODEX_HOME': str(Path('relative-home').resolve()),
                  'CODEX_SQLITE_HOME': str(Path('relative-database').resolve())})):
            for settings in ({'inbox_bridge': False}, {'args': ['--no-daemon']}, {}, {'inbox_bridge': True}):
                for mode in ('spawn', 'resume'):
                    with self.subTest(storage=storage, settings=settings, mode=mode), \
                            patch.dict(os.environ, {'HOME': '/launch/home'}), \
                            patch('multiverse.harnesses.codex.plugins.codex_version', return_value=None):
                        adapter = CodexAdapter.configured('codex', {**settings, 'env': {'DEBUG': 'keep'}})
                        context = self.context('storage')
                        environment = {key: value for key, value in context.environment.items()
                                       if key not in adapter.storage_env}
                        context = replace(context, mode=mode, environment={**environment, **storage})
                        session = SessionHandle('agent', 'codex', '/workspace', native_id=context.values['session_id'])
                        command = adapter.spawn(context) if mode == 'spawn' else adapter.resume(session, context)
                        if command.shared_service:
                            self.assertEqual(command.agent_identity, adapter.agent_identity_policy())
                            self.assertEqual(command.shared_service.environment, expected)
                            stable = command.rpc_proxy.settings['agent_environment']
                            self.assertEqual({key: stable[key] for key in adapter.storage_env if key in stable}, expected)
                        plain = command.fallback if command.shared_service else command
                        if plain is not None:
                            self.assertIsNone(plain.agent_identity)
                            self.assertEqual({key: plain.environment[key] for key in adapter.storage_env
                                              if key in plain.environment}, expected)
                            self.assertEqual(plain.environment['DEBUG'], 'keep')
        foreign_keys = ('CLAUDE_CONFIG_DIR', 'PI_CODING_AGENT_DIR')
        for foreign_key in foreign_keys:
            for foreign_home in (None, '', '/launch/foreign-home'):
                for proven in (None, (), (foreign_key,)):
                    for mode in ('spawn', 'resume', 'fork'):
                        with self.subTest(foreign_key=foreign_key, foreign_home=foreign_home, proven=proven, mode=mode):
                            context = self.context('foreign-storage')
                            environment = dict(context.environment)
                            if foreign_home is not None:
                                environment[foreign_key] = foreign_home
                            context = replace(context, mode='resume' if mode == 'resume' else 'spawn',
                                              environment=environment, storage_keys=proven)
                            adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
                            session = SessionHandle('agent', 'codex', '/workspace', native_id=context.values['session_id'])
                            command = adapter.resume(session, context) if mode == 'resume' else adapter.spawn(context)
                            native_values = {key: '/native/' + key for key in foreign_keys}
                            native = {'shell_environment_policy': {'set': {
                                **native_values, 'KEEP_SETTING': 'keep'}, 'exclude': ['AWS_*']}}
                            original = deepcopy(native)
                            protocol = LaunchProtocol(json.loads(json.dumps(command.rpc_proxy.settings)))
                            method = 'thread/start' if mode == 'spawn' else 'thread/resume'
                            params = {'threadId': session.native_id, 'config': native}
                            if mode == 'fork':
                                protocol.native_id, protocol.bound = session.native_id, True
                                protocol = protocol.for_connection('storage-test', lambda: True)
                                protocol.version = (0, 164, 0)
                                method = 'thread/fork'
                                params.update(ephemeral=True, excludeTurns=True)
                            request = protocol.outgoing({'id': 1, 'method': method, 'params': params})
                            policy = request['params']['config']['shell_environment_policy']
                            controlled = foreign_keys if proven is None else proven
                            for key in foreign_keys:
                                if key not in controlled:
                                    self.assertEqual(policy['set'].get(key), native_values[key])
                                    self.assertNotIn(key, command.rpc_proxy.settings['agent_environment'])
                                elif key not in environment:
                                    self.assertNotIn(key, policy['set'])
                                else:
                                    self.assertEqual(policy['set'][key], environment[key])
                            self.assertEqual(set(policy['exclude']),
                                {'AWS_*', 'MV2_*', 'CODEX_HOME', 'CODEX_SQLITE_HOME', *controlled})
                            self.assertEqual(policy['set']['KEEP_SETTING'], 'keep')
                            self.assertEqual(policy['set']['CODEX_HOME'], '/native/home')
                            self.assertEqual(policy['set']['MV2_AGENT_ID'], 'agent')
                            self.assertEqual(native, original)
        with tempfile.TemporaryDirectory(prefix='mv2-codex-storage-alias-') as temporary:
            for key in ('CODEX_HOME', 'CODEX_SQLITE_HOME'):
                with self.subTest(retargeted_storage=key):
                    parent = Path(temporary).resolve() / key
                    saved, original, different = (parent / name for name in ('saved', 'original', 'different'))
                    saved.mkdir(parents=True)
                    different.mkdir()
                    context = self.context('alias')
                    context = replace(context, environment={**context.environment, key: str(saved)})
                    before = storage_environment(context)
                    saved.rename(original)
                    saved.symlink_to(different, target_is_directory=True)
                    self.assertEqual(storage_environment(context), {**before, key: str(different)})
                    self.assertEqual(context.environment[key], str(saved), 'Selection never rewrites the saved input')
