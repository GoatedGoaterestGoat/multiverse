"""Codex declarative contributions, native override precedence and version gates."""
from copy import deepcopy
import asyncio
import os
import shutil
import shlex
import sys
from dataclasses import replace
import hashlib
import json
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from native_processes import PrivateProcessGroup

from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex import plugins as codex
from multiverse.harnesses.codex.launch import LaunchProtocol, launch_settings
from multiverse.harnesses.types import LaunchContext, NativeEndpoint, NativeResult, SessionHandle
from multiverse.plugins import loader
from multiverse.plugins.testing import PluginTestCase


class CodexPluginsTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.root / 'state' / 'agents' / 'target'
        self.agent.mkdir(parents=True)
        self.values = dict(name='worker', agent_id='target', agent_dir=str(self.agent),
            workspace=str(self.work / 'project'), bootstrap_json=json.dumps('core bootstrap'),
            bootstrap_file=str(self.agent / 'launches' / 'launch' / 'bootstrap.md'),
            session_id=str(uuid4()), task='--literal prompt', control_socket=str(self.work / 'rpc.sock'))
        self.context = LaunchContext('spawn', self.values, environment={'PATH': '/bin', 'MV2_AGENT_ID': 'target', 'MV2_UNIVERSE': str(self.root),
            'MV2_AGENT_DIR': str(self.agent), 'MV2_LAUNCH_ID': 'launch',
            'MV2_CLI': '/target/bin/mv2', 'MV2_PYTHON': '/target/python',
            'MV2_CONTROL_SOCKET': self.values['control_socket']},
            endpoint=NativeEndpoint('unix-websocket', str(self.work / 'native.sock')))
        self.context = replace(self.context, agent_environment={key: value for key, value in self.context.environment.items()
            if key in ('PATH', 'MV2_AGENT_ID', 'MV2_UNIVERSE', 'MV2_AGENT_DIR', 'MV2_CLI', 'MV2_PYTHON')})
        version = patch.object(codex, 'codex_version', return_value=(0, 162, 1))
        version.start()
        self.addCleanup(version.stop)

    def add_plugin(self, name='demo', fields=None, permissions=None, entries=None, files=None):
        directory = self.write_plugin(name, {'permissions': permissions or ['harness.codex', 'process.spawn'],
            'harnesses': {'codex': entries if entries is not None else [fields or {'config': {'model_verbosity': 'low'}}]}},
            files=files)
        self.enable(name)
        loader.reset()  # a table row may rewrite a plugin an earlier row loaded
        return directory

    def command(self, settings=None, daemon=(0, 162, 1), mode='spawn', final=True, context=None):
        settings = {'inbox_bridge': False, **(settings or {})}
        if settings.get('inbox_bridge') is None:
            settings.pop('inbox_bridge')
        context = context or self.context
        if final:
            executor = Mock()
            executor.rpc.return_value = NativeResult('ok', value={'exclude': [], 'writable_roots': [],
                'daemon_version': daemon, 'workspace_policy': {}})
            context = replace(context, executor=executor)
        adapter = CodexAdapter.configured('custom-codex', settings)
        if mode == 'resume':
            return adapter.resume(SessionHandle('target', 'custom-codex', self.values['workspace'],
                native_id=self.values['session_id']), context)
        return adapter.spawn(context)

    def detail(self, command, name='demo'):
        return command.plugins['plugins'][name]['codex'][0]

    def config(self, command):
        return codex.config_tree(codex.overrides(command.argv[1:]))

    def disable(self, *names):
        for name in names:
            self.enable(name, False)
        loader.reset()

    def test_secrets_and_harness_env_reach_codex_only_through_env_or_config(self):
        """Harness and plugin env, secrets included, reaches Codex tools only through the process env or the relay's
        thread config (shell_environment_policy, MCP env), on bridge, plain and auto's fallback, never argv or the
        saved plugin record; a break goes unnoticed because the variable is just missing in tool calls, or the secret
        shows in ps and state files."""
        with self.subTest('plain record'):
            directory = self.add_plugin(fields={'config': {'model_verbosity': 'low',
                'tui.notifications': ['agent-turn-complete']}, 'instructions': 'notes.md',
                'mcp_servers': {'lookup': {'command': 'python3',
                    'args': ['{plugin_dir}/server.py', '$(touch /never)', ''], 'env': {'LABEL': 'literal'},
                    'env_vars': ['MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID', 'MV2_CLI', 'MV2_PYTHON']}}},
                files={'notes.md': 'Use lookup for facts.'})
            command = self.command()
            config = self.config(command)
            server = config['mcp_servers']['mv2_demo_lookup']
            self.assertEqual(server['args'], [str(directory / 'server.py'), '$(touch /never)', ''])
            self.assertEqual(server['env'], {'LABEL': 'literal'})
            self.assertIn('MV2_AGENT_ID', server['env_vars'])
            self.assertIn('MV2_PYTHON', server['env_vars'])
            self.assertFalse(server['required'])
            self.assertIn('<plugin_instructions name="demo">\nUse lookup for facts.', config['developer_instructions'])
            self.assertEqual(self.detail(command)['instructions']['sha256'],
                             hashlib.sha256(b'Use lookup for facts.').hexdigest())
            record = json.dumps(command.plugins)
            self.assertNotIn('Use lookup for facts.', record)
            self.assertNotIn('$(touch', record)
            self.assertNotIn('literal', record)
            self.assertEqual(command.argv[-2:], ('--', '--literal prompt'))
            self.assertIn('check_for_update_on_startup=false', command.argv)
        self.add_plugin(fields={'mcp_servers': {
            'one': {'command': 'python3', 'env': {'LABEL': 'one'}},
            'two': {'command': 'python3', 'env_vars': ['DECLARED_TOKEN']}}})
        context = replace(self.context, environment={**self.context.environment,
            'CODEX_HOME': str(self.work / 'native-home'), 'UNRELATED_TOKEN': 'unrelated-secret'})
        for bridge in (False, True):
            with self.subTest('launch environment', bridge=bridge):
                command = self.command({'inbox_bridge': bridge, 'env': {
                    'DECLARED_TOKEN': 'declared-secret', 'OTHER_API_KEY': 'harness-secret'}}, context=context)
                argv = '\n'.join(command.argv)
                for value in ('declared-secret', 'harness-secret', 'unrelated-secret', str(self.work / 'native-home')):
                    self.assertNotIn(value, argv)
                servers = self.config(command)['mcp_servers']
                self.assertEqual(servers['mv2_demo_one'].get('env', {}), {'LABEL': 'one'})
                self.assertEqual(servers['mv2_demo_two'].get('env', {}), {})
                self.assertEqual(servers['mv2_demo_two']['env_vars'], ['DECLARED_TOKEN'])
                if bridge:
                    config = LaunchProtocol(command.rpc_proxy.settings).outgoing(
                        {'id': 1, 'method': 'thread/start', 'params': {}})['params']['config']
                    self.assertEqual(config['mcp_servers']['mv2_demo_two']['env']['DECLARED_TOKEN'], 'declared-secret')
                    self.assertNotIn('OTHER_API_KEY', config['mcp_servers']['mv2_demo_two']['env'])
                    self.assertEqual(config['shell_environment_policy']['set']['OTHER_API_KEY'], 'harness-secret')
                self.assertEqual(command.environment['OTHER_API_KEY'], 'harness-secret')
        self.disable('demo')
        with self.subTest('bridge resume'):
            command = self.command({'inbox_bridge': True, 'env': {'APP_TOKEN': 'resume-secret'},
                'resume_args': ['--sandbox', 'read-only', '--ask-for-approval', 'never']}, mode='resume')
            self.assertNotIn('resume-secret', '\n'.join(command.argv))
            frame = {'id': 1, 'method': 'thread/resume', 'params': {'threadId': self.values['session_id']}}
            outgoing = LaunchProtocol(command.rpc_proxy.settings).outgoing(frame)
            self.assertEqual(outgoing['params']['config']['shell_environment_policy']['set']['APP_TOKEN'],
                             'resume-secret')
            self.assertEqual(outgoing['params']['sandbox'], 'read-only')
            self.assertEqual(outgoing['params']['approvalPolicy'], 'never')
            self.assertEqual(outgoing['params']['threadId'], self.values['session_id'])
            self.assertEqual(frame['params'], {'threadId': self.values['session_id']})
        with self.subTest('auto bridge and its fallback'):
            env = {'IS_SANDBOX': '1', 'LITERAL': 'a "quoted" é'}
            environment = {'CODEX_HOME': str(self.work / 'native home'), 'MV2_AGENT_ID': 'current', 'PATH': '/tools'}
            command = self.command({'inbox_bridge': None, 'env': env},
                                   context=replace(self.context, environment=environment))
            self.assertIn('--remote', command.argv)
            self.assertEqual(command.environment, env)
            outgoing = LaunchProtocol(command.rpc_proxy.settings).outgoing(
                {'id': 1, 'method': 'thread/start', 'params': {}})
            forwarded = outgoing['params']['config']['shell_environment_policy']['set']
            self.assertEqual({key: forwarded[key] for key in env}, env)
            self.assertEqual(forwarded['MV2_IDENTITY_MODE'], 'codex-agent-v1')
            self.assertEqual(forwarded['CODEX_HOME'], environment['CODEX_HOME'])
            self.assertFalse(any(arg.startswith('shell_environment_policy.set.IS_SANDBOX') for arg in command.argv))
            self.assertNotIn('a "quoted" é', '\n'.join(command.argv))
            self.assertEqual(command.fallback.environment, {**env, 'CODEX_HOME': environment['CODEX_HOME']})
            self.assertFalse(any(arg.startswith('shell_environment_policy.set.IS_SANDBOX')
                                 for arg in command.fallback.argv))

    def test_mcp_servers_get_the_target_agents_identity(self):
        """A plugin MCP server gets the target agent's stable identity, never the sender's or per-launch values; a
        break goes unnoticed because the MCP server works, acting as the wrong agent."""
        for index, key in enumerate(('MV2_LAUNCH_ID', 'MV2_CONTROL_SOCKET', 'MV2_TMUX_SOCKET')):
            with self.subTest('bridge launch identity', key=key):
                self.add_plugin(name='volatile-' + str(index),
                    fields={'mcp_servers': {'tools': {'command': 'python3', 'env_vars': [key]}}})
                command = self.command({'inbox_bridge': True})
                self.assertNotIn('mcp_servers', command.rpc_proxy.settings['plugin_config'])
                self.assertIn('launch identity', json.dumps(command.plugins))
            self.disable('volatile-' + str(index))
        with self.subTest('ambient sender'):
            self.add_plugin(fields={'mcp_servers': {'tools': {'command': 'python3'}}})
            context = replace(self.context, environment={'PATH': '/bin', 'MV2_AGENT_ID': 'sender',
                'MV2_AGENT_DIR': '/sender', 'MV2_CLI': '/sender/mv2', 'MV2_TMUX_SOCKET': '/sender/tmux'})
            env = self.config(self.command(context=context))['mcp_servers']['mv2_demo_tools'].get('env', {})
            self.assertFalse(any(key.startswith('MV2_') for key in env))
        from multiverse import harnesses
        from multiverse.runtime import lifecycle
        from multiverse.store import write
        # The pure adapter rows use an empty, non-record target placeholder.
        # Here lifecycle publishes the real child record with its own UUID.
        self.agent.rmdir()
        # A pre-update agent has launch proof and a saved terminal, but no
        # agent-stable policy. Its child must still reach target preparation.
        parent, properties, _, _ = lifecycle._record(self.root, 'sender', '', 'codex', None, None)
        self.assertNotIn('agent_identity', properties)
        write(parent / 'properties.json', properties)
        parent_launch = str(uuid4())
        write(parent / 'runtime.json', {'launch_id': parent_launch, 'tmux': {'socket': '/sender/tmux'}})
        identity_keys = {'MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID',
                         'MV2_IDENTITY_MODE', 'MV2_CONTROL_SOCKET', 'MV2_TMUX_SOCKET'}
        environment = {key: value for key, value in os.environ.items()
                       if key not in identity_keys and not key.startswith('CODEX_')}
        environment.update(MV2_UNIVERSE=str(self.root), MV2_AGENT_ID=properties['id'],
            MV2_AGENT_DIR=str(parent), MV2_LAUNCH_ID=parent_launch,
            MV2_CLI='/sender/mv2', MV2_TMUX_SOCKET='/stale-sender/tmux')
        self.add_plugin(fields={'mcp_servers': {'tools': {'command': 'python3', 'env_vars': [
            'MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID', 'MV2_CONTROL_SOCKET',
            'MV2_CLI', 'MV2_PYTHON', 'PATH']}}})
        for setting in (False, None):
            settings = {} if setting is None else {'inbox_bridge': setting}
            adapter = CodexAdapter.configured('codex', settings)
            captured = []
            spawn = adapter.spawn
            def prepare_and_stop(context):
                captured.append((context, spawn(context)))
                raise ValueError('fixture stops before native preparation')
            with self.subTest('initial core context', setting=setting), \
                 patch.dict(os.environ, environment, clear=True), \
                 patch.object(harnesses, 'load', return_value=adapter), \
                 patch.object(lifecycle, '_cli_path', return_value='/target/bin/mv2'), \
                 patch.object(lifecycle.shutil, 'which', return_value='/fixture/tmux'), \
                 patch.object(adapter, 'spawn', side_effect=prepare_and_stop):
                with self.assertRaisesRegex(ValueError, 'fixture stops'):
                    lifecycle.start(self.root, name='initial-plain' if setting is False else 'initial-fallback')
                context, command = captured[0]
                candidate = command.fallback if command.fallback else command
                server = self.config(candidate)['mcp_servers']['mv2_demo_tools']
                self.assertEqual(server.get('env', {}), {})
                self.assertEqual(set(server['env_vars']), {'MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR',
                    'MV2_LAUNCH_ID', 'MV2_CONTROL_SOCKET', 'MV2_CLI', 'MV2_PYTHON', 'PATH'})
                self.assertEqual(context.environment['MV2_CLI'], '/target/bin/mv2')
                self.assertEqual(context.environment['MV2_PYTHON'], sys.executable)
                self.assertNotEqual(context.environment['MV2_AGENT_ID'], properties['id'])
                self.assertNotIn('MV2_TMUX_SOCKET', context.environment)

    def test_refused_plugin_entries_never_reach_codex(self):
        """A plugin cannot set approval_policy, notify, MCP commands through dotted config, inline credentials or
        CODEX_THREAD_ID, and the launch still starts; a break is a silent privilege escalation."""
        bad = [{'config': {'notify': ['command']}}, {'config': {'approval_policy': 'never'}},
            {'config': {'mcp_servers.foo.command': 'evil'}}, {'config': {'tui.animations': 1}},
            {'config': {'model_verbosity': 'verbose'}}, {'config': {'tui.status_line': ['invented']}},
            {'instructions': '../outside'}, {'hooks': {}}, {'instructions': 'nul.md'}]
        with self.subTest('reserved keys and bad types'):
            for number, fields in enumerate(bad):
                self.add_plugin('config-' + str(number), fields=fields, files={'nul.md': '\0'})
            self.add_plugin('good')
            command = self.command()
            self.assertEqual(self.config(command)['model_verbosity'], 'low')
            for number in range(len(bad)):
                detail = self.detail(command, 'config-' + str(number))
                self.assertEqual(detail['state'], 'skipped')
                self.assertTrue(detail['reason'])
        self.disable('good', *('config-' + str(number) for number in range(len(bad))))
        definitions = [({'command': 'python3'}, ['harness.codex']),
            ({'command': 'python3', 'env': {'API_KEY': 'secret'}}, None),
            ({'command': 'python3', 'env': {'MV2_AGENT_ID': 'wrong'}}, None),
            ({'url': 'https://user:pass@example.org/mcp'}, None),
            ({'url': 'https://example.org/mcp?token=secret'}, None),
            ({'url': 'https://example.org/mcp', 'http_headers': {'Authorization': 'secret'}}, None),
            ({'url': 'https://example.org/mcp', 'env_http_headers': {'X.Bad': 'ENV'}}, None),
            ({'command': 'python3', 'required': True}, None),
            ({'command': 'python3', 'env_vars': ['CODEX_THREAD_ID']}, None),
            ({'command': 'python3', 'args': ['{workspace}']}, None)]
        with self.subTest('MCP credentials, permissions and names'):
            for number, (definition, permissions) in enumerate(definitions):
                self.add_plugin('mcp-' + str(number), {'mcp_servers': {'tools': definition}}, permissions=permissions)
            command = self.command()
            self.assertNotIn('mcp_servers', self.config(command))
            for number in range(len(definitions)):
                self.assertEqual(self.detail(command, 'mcp-' + str(number))['state'], 'skipped')
        self.disable(*('mcp-' + str(number) for number in range(len(definitions))))
        with self.subTest('conflicting config and normalized MCP names'):
            self.add_plugin('a', {'config': {'model_verbosity': 'low'}})
            self.add_plugin('b', {'config': {'model_verbosity': 'high'}})
            self.add_plugin('demo', {'mcp_servers': {'a-b': {'command': 'python3'}, 'a_b': {'command': 'python3'}}})
            command = self.command()
            config = self.config(command)
            self.assertNotIn('model_verbosity', config)
            self.assertNotIn('mcp_servers', config)
            self.assertIn('conflicting', self.detail(command, 'a')['reason'])
            self.assertIn('collision', self.detail(command)['reason'])

    def test_explicit_user_config_beats_plugin_defaults(self):
        """Explicit user config wins over plugin defaults across five -c spellings, TOML dotted parents and children,
        and leaves the bridge TUI drops, without the relay mutating frames; a break silently overrides the user's
        settings."""
        self.add_plugin(fields={'config': {'model_verbosity': 'low', 'tui.animations': True}})
        for flags in (['-c', 'model_verbosity="high"'], ['--config', 'model_verbosity="high"'],
                      ['--config=model_verbosity="high"'], ['-cmodel_verbosity="high"'], ['-c=model_verbosity="high"']):
            for mode in ('spawn', 'resume'):
                with self.subTest('override forms', flags=flags, mode=mode):
                    command = self.command({mode + '_args': flags}, mode=mode)
                    self.assertEqual(self.config(command)['model_verbosity'], 'high')
                    self.assertIn('model_verbosity', self.detail(command)['suppressed'])
                    position = command.argv.index(flags[-1]) - len(flags) + 1
                    self.assertEqual(command.argv[position:position + len(flags)], tuple(flags))
        self.add_plugin(fields={'instructions': 'notes.md',
                                'config': {'tui.animations': True, 'tui.notifications': True}},
                        files={'notes.md': 'plugin text'})
        for key in ('tui', 'tui.animations.child'):
            with self.subTest('parent and descendant', key=key):
                command = self.command({'args': ['-c', key + '=false', '-c', 'developer_instructions="user text"']})
                self.assertIn('tui.animations', self.detail(command)['suppressed'])
                self.assertIn('instructions', self.detail(command)['suppressed'])
                self.assertEqual(self.config(command)['developer_instructions'], 'user text')
                self.assertNotIn('plugin text', ' '.join(command.argv))
        with self.subTest('no alt screen'):
            self.add_plugin(fields={'config': {'tui.alternate_screen': 'always'}})
            command = self.command({'args': ['--no-alt-screen']})
            self.assertIn('--no-alt-screen', command.argv)
            self.assertNotIn('tui', self.config(command))
            self.assertEqual(self.detail(command)['state'], 'suppressed')
        with self.subTest('bridge MCP leaf the TUI drops'):
            self.add_plugin(fields={'mcp_servers': {'tools': {'command': 'python3', 'env': {'LABEL': 'default'},
                                                              'args': ['default']}}})
            command = self.command({'inbox_bridge': True, 'args': ['-c', 'mcp_servers.mv2_demo_tools.args=["user"]',
                '-c', 'mcp_servers.mv2_demo_tools.env.LABEL="user"']})
            relay = command.rpc_proxy.settings
            self.assertNotIn('args', relay['plugin_config']['mcp_servers']['mv2_demo_tools'])
            self.assertNotIn('LABEL', relay['plugin_config']['mcp_servers']['mv2_demo_tools'].get('env', {}))
            protocol = LaunchProtocol(relay)
            request = protocol.outgoing({'id': 1, 'method': 'thread/start', 'params': {
                'cwd': self.values['workspace'], 'config': {'mcp_servers.mv2_demo_tools.args': ['native']}}})
            server = request['params']['config']['mcp_servers']['mv2_demo_tools']
            self.assertEqual(server['args'], ['user'])
            self.assertEqual(server['env']['LABEL'], 'user')
            self.assertEqual(server['command'], 'python3')
            self.assertEqual(self.detail(command)['mcp_state'], 'requested')
        with self.subTest('user MCP parent'):
            self.add_plugin(fields={'mcp_servers': {'tools': {'command': 'python3'}}})
            command = self.command({'inbox_bridge': True, 'args': ['-c', 'mcp_servers.mv2_demo_tools=false']})
            self.assertNotIn('mcp_servers', command.rpc_proxy.settings['plugin_config'])
            self.assertFalse(command.rpc_proxy.settings['plugin_user_config']['mcp_servers']['mv2_demo_tools'])
            self.assertEqual(self.detail(command)['mcp_servers'], [])
            self.assertEqual(self.detail(command)['state'], 'suppressed')
        with self.subTest('relay precedence'):
            settings = dict(bootstrap='core', workspace='/workspace', roots=['/agent'],
                plugin_config={'model_verbosity': 'low', 'mcp_servers': {'tool': {'command': 'default', 'args': []}}},
                plugin_user_config={'mcp_servers': {'tool': {'args': ['explicit']}}})
            frame = {'id': 1, 'method': 'thread/start', 'params': {'config': {'model_verbosity': 'high',
                'mcp_servers.tool.command': 'native', 'sandbox_workspace_write.writable_roots': ['/user']}}}
            before = deepcopy(frame)
            out = LaunchProtocol(settings).outgoing(frame)
            self.assertEqual(out['params']['config']['model_verbosity'], 'high')
            self.assertEqual(out['params']['config']['mcp_servers']['tool'],
                             {'command': 'native', 'args': ['explicit']})
            self.assertEqual(out['params']['config']['sandbox_workspace_write']['writable_roots'], ['/agent', '/user'])
            self.assertEqual(frame, before)
            unrelated = {'id': 2, 'method': 'other/request', 'params': {'config': {}}}
            self.assertIs(LaunchProtocol(settings).outgoing(unrelated), unrelated)

    def test_plugin_entries_apply_only_within_tested_versions(self):
        """A plugin entry applies only within the tested versions of whichever component reads it, CLI or shared
        daemon, read from the initialize reply alone; fragile because the two versions drift independently, and a break
        hands config to a Codex that misreads it."""
        with self.subTest('CLI and daemon bounds are independent'):
            self.add_plugin(entries=[{'min_version': '0.162.1', 'max_version': '0.162.1',
                'config': {'model_verbosity': 'low', 'tui.animations': True}}])
            command = self.command({'inbox_bridge': None}, daemon=(0, 163, 0))
            self.assertNotIn('model_verbosity', command.rpc_proxy.settings['plugin_config'])
            self.assertNotIn('tui', self.config(command))
            self.assertEqual(command.plugins['plugins']['demo']['state'], 'skipped')
            self.assertEqual(self.config(command.fallback)['model_verbosity'], 'low')
            self.assertIsNot(command.plugins, command.fallback.plugins)
            self.assertEqual(self.detail(command.fallback)['cli_version'], '0.162.1')
            self.assertIn('0.162.1', str(command.plugins))
        with self.subTest('daemon entry fits when the CLI is outside'):
            self.add_plugin(entries=[{'min_version': '0.163.0', 'max_version': '0.163.0',
                'config': {'model_verbosity': 'low'}}])
            command = self.command({'inbox_bridge': None}, daemon=(0, 163, 0))
            self.assertEqual(command.rpc_proxy.settings['plugin_config']['model_verbosity'], 'low')
            self.assertNotIn('model_verbosity', self.config(command.fallback))
            self.assertEqual(self.detail(command)['daemon_version'], '0.163.0')
        with self.subTest('unknown daemon'):
            self.add_plugin(fields={'config': {'model_verbosity': 'low', 'tui.animations': True},
                'instructions': 'notes.md', 'mcp_servers': {'tools': {'command': 'python3'}}},
                files={'notes.md': 'plugin'})
            command = self.command({'inbox_bridge': True}, daemon=None)
            self.assertEqual(command.rpc_proxy.settings['plugin_config'], {})
            self.assertEqual(command.rpc_proxy.settings['bootstrap'], 'core bootstrap')
            self.assertIn('version could not be read', str(self.detail(command)['skipped']))
            self.assertTrue(self.config(command)['tui']['animations'])
            initial = self.command({'inbox_bridge': True}, final=False)
            self.assertEqual(initial.plugins['plugins'], {})
            self.assertEqual(initial.rpc_proxy.settings['plugin_config'], {})
        with self.subTest('old or unknown CLI'):
            self.add_plugin(fields={'mcp_servers': {'tools': {'command': 'python3'}},
                                    'config': {'model_verbosity': 'low'}})
            with patch.object(codex, 'codex_version', return_value=(0, 159, 3)):
                command = self.command()
                self.assertNotIn('mcp_servers', self.config(command))
                self.assertIn('0.162.1', str(self.detail(command)['skipped']))
            with patch.object(codex, 'codex_version', return_value=None):
                command = self.command()
                self.assertEqual(self.detail(command)['state'], 'skipped')
        self.disable('demo')
        with self.subTest('failed bridge contributor'):
            self.write_plugin('broken', {'module': 'plugin', 'extends': ['launch'], 'permissions': ['harness.codex'],
                'harnesses': {'codex': [{'config': {'tui.animations': True}}]}}, files={'plugin.py': """
from multiverse.plugins import api
def register(mv):
    @mv.launch.contribute('codex')
    def contribute(info):
        if info.bridge:
            raise RuntimeError('bridge contribution failed')
        return api.Contribution()
"""})
            self.enable('broken')
            command = self.command({'inbox_bridge': None})
            record = command.plugins['plugins']['broken']
            self.assertEqual(record['state'], 'skipped')
            self.assertIn('bridge contribution failed', record['reason'])
            self.assertNotIn('tui', self.config(command))
            self.assertTrue(self.config(command.fallback)['tui']['animations'])
        with self.subTest('server release parser'):
            self.assertEqual(codex.server_version('codex_cli_rs/0.162.1 (Mac OS; arm64)'), (0, 162, 1))
            for value in (None, {}, '0.162.1', 'codex/0.162.1-alpha', 'codex/0.162.1junk'):
                self.assertIsNone(codex.server_version(value))
        with self.subTest('daemon version from initialize'):
            plan = launch_settings('/workspace')
            self.assertEqual(next(plan).method, 'initialize')
            self.assertEqual(plan.send({'result': {'userAgent': 'codex_cli_rs/0.163.0 (Linux)'}}).method, 'config/read')
            with self.assertRaises(StopIteration) as stop:
                plan.send({'result': {'config': {}}})
            self.assertEqual(stop.exception.value.value['daemon_version'], (0, 163, 0))

    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_generated_config_is_honoured_by_a_private_codex(self):
        """On private app-servers the generated MCP server connects with only its declared env, and plain argv and
        shell_environment_policy.set bring harness env to shell commands, also across a bridge resume; fragile because
        it depends on Codex's config schema and idle/unsubscribed reload boundary, so drift silently leaves stale env."""
        from native_safety import isolated_home, install
        import subprocess
        install()
        executable = shutil.which('codex')
        version = subprocess.run([executable, '--version'], capture_output=True, text=True, check=True).stdout.strip()
        if version != 'codex-cli 0.164.0':
            self.skipTest('native evidence requires checked Codex 0.164.0')
        mcp_output = self.work / 'mcp-env.json'
        helper = """import json, os, pathlib, sys
keys = ('APP_TOKEN', 'OTHER_API_KEY', 'MV2_AGENT_ID')
pathlib.Path(sys.argv[1]).write_text(json.dumps({key: os.environ.get(key) for key in keys}))
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    result = ({'protocolVersion': request['params']['protocolVersion'], 'capabilities': {'tools': {}},
               'serverInfo': {'name': 'fixture', 'version': '1'}} if method == 'initialize' else
              {'tools': [{'name': 'ping', 'description': 'offline fixture', 'inputSchema': {'type': 'object'}}]}
              if method == 'tools/list' else {})
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
"""

        async def probe(bridge):
            home = isolated_home(self.work / ('shell-bridge-home' if bridge else 'shell-plain-home'))
            self.context = replace(self.context, environment={**self.context.environment, 'CODEX_HOME': str(home)})
            if bridge:
                self.add_plugin(fields={'mcp_servers': {'tools': {'command': sys.executable,
                    'args': ['{plugin_dir}/server.py', str(mcp_output)], 'env_vars': ['APP_TOKEN', 'MV2_AGENT_ID'],
                    'startup_timeout_sec': 5}}}, files={'server.py': helper})
            command = self.command({'inbox_bridge': bridge, 'env': {
                'APP_TOKEN': 'shell-secret', 'OTHER_API_KEY': 'not-for-mcp'}})
            self.assertNotIn('shell-secret', '\n'.join(command.argv))
            argv = [executable]
            if not bridge:
                for key, value in codex.overrides(command.argv[1:]):
                    argv.extend(['-c', key + '=' + codex.toml_value(value)])
            argv.append('app-server')
            socket = self.work / 'shell-private.sock'
            if bridge:
                argv.extend(['--listen', 'unix://' + str(socket)])
            environment = {**os.environ, 'CODEX_HOME': str(home), 'APP_TOKEN': 'daemon-stale'}
            if not bridge:
                environment.update(command.environment)
            process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=environment,
                start_new_session=True)
            group = PrivateProcessGroup(process.pid)
            connection = None
            async def rpc(frame):
                if connection is not None:
                    await connection.send(json.dumps(frame))
                else:
                    process.stdin.write((json.dumps(frame) + '\n').encode())
                    await process.stdin.drain()
                while True:
                    line = await asyncio.wait_for(
                        connection.recv() if connection is not None else process.stdout.readline(), 15)
                    self.assertTrue(line, 'private app-server exited')
                    result = json.loads(line)
                    if result.get('id') == frame['id']:
                        self.assertNotIn('error', result, result)
                        return result['result']
            try:
                if bridge:
                    from websockets.asyncio.client import unix_connect
                    for _ in range(100):
                        if socket.exists():
                            break
                        await asyncio.sleep(.05)
                    connection = await unix_connect(str(socket))
                await rpc({'id': 1, 'method': 'initialize', 'params': {
                    'clientInfo': {'name': 'mv2-test', 'version': '1'}, 'capabilities': {'experimentalApi': True}}})
                if connection is not None:
                    await connection.send(json.dumps({'method': 'initialized', 'params': {}}))
                else:
                    process.stdin.write(b'{"method":"initialized","params":{}}\n')
                frame = {'id': 2, 'method': 'thread/start', 'params': {
                    'cwd': self.values['workspace'], 'ephemeral': False, 'approvalPolicy': 'never'}}
                if bridge:
                    frame = LaunchProtocol(command.rpc_proxy.settings).outgoing(frame)
                thread = await rpc(frame)
                output = self.work / ('bridge-shell-env.txt' if bridge else 'plain-shell-env.txt')
                script = ("import os,pathlib;pathlib.Path(" + repr(str(output))
                          + ").write_text(os.environ.get('APP_TOKEN','missing'))")
                shell = shlex.join([sys.executable, '-c', script])
                await rpc({'id': 3, 'method': 'thread/shellCommand', 'params': {
                    'threadId': thread['thread']['id'], 'command': shell, 'timeoutMs': 5000}})
                for _ in range(100):
                    if output.exists():
                        break
                    await asyncio.sleep(.05)
                self.assertTrue(output.exists(), 'native shell command did not produce fixture output')
                self.assertEqual(output.read_text(), 'shell-secret')
                if bridge:
                    inventory = await rpc({'id': 20, 'method': 'mcpServerStatus/list',
                        'params': {'threadId': thread['thread']['id']}})
                    server = {item['name']: item for item in inventory['data']}['mv2_demo_tools']
                    self.assertEqual(server['runtimeStatus'], 'connected')
                    self.assertIn('ping', server['tools'])
                    self.assertEqual(json.loads(mcp_output.read_text()), {
                        'APP_TOKEN': 'shell-secret', 'MV2_AGENT_ID': 'target', 'OTHER_API_KEY': None})
                    await asyncio.sleep(.1)
                    self.values = {**self.values, 'session_id': thread['thread']['id']}
                    self.context = replace(self.context, values=self.values)
                    resumed = self.command({'inbox_bridge': True, 'env': {'APP_TOKEN': 'resumed-secret'}},
                                           mode='resume')
                    self.assertNotIn('resumed-secret', '\n'.join(resumed.argv))
                    frame = LaunchProtocol(resumed.rpc_proxy.settings).outgoing({'id': 4,
                        'method': 'thread/resume', 'params': {'threadId': thread['thread']['id']}})
                    result = await rpc(frame)
                    self.assertEqual(result['thread']['id'], thread['thread']['id'])
                    resumed_output = self.work / 'resumed-shell-env.txt'
                    script = ("import os,pathlib;pathlib.Path(" + repr(str(resumed_output))
                              + ").write_text(os.environ.get('APP_TOKEN','missing'))")
                    # Exercise the gap between observable output and shell completion.
                    await rpc({'id': 5, 'method': 'thread/shellCommand', 'params': {'threadId': thread['thread']['id'],
                        'command': shlex.join([sys.executable, '-c', script + ';import time;time.sleep(1)']),
                        'timeoutMs': 5000}})
                    for _ in range(100):
                        if resumed_output.exists():
                            break
                        await asyncio.sleep(.05)
                    self.assertTrue(resumed_output.exists(), 'resumed shell command did not produce output')
                    # Native rejoin preserves a still-subscribed thread's old config.
                    self.assertEqual(resumed_output.read_text(), 'shell-secret')
                    # An output file can precede shell completion. Native reload
                    # requires idle work and no subscribers, not a timed pause.
                    deadline = asyncio.get_running_loop().time() + 15
                    while True:
                        observed = await rpc({'id': 21, 'method': 'thread/read', 'params': {
                            'threadId': thread['thread']['id'], 'includeTurns': False}})
                        self.assertEqual(observed['thread']['id'], thread['thread']['id'])
                        if (observed['thread']['status']['type'] == 'idle'
                                and observed['thread'].get('canAcceptDirectInput') is True):
                            break
                        self.assertLess(asyncio.get_running_loop().time(), deadline,
                                        'native shell work did not become idle before resume')
                        await asyncio.sleep(.05)
                    detached = await rpc({'id': 22, 'method': 'thread/unsubscribe',
                        'params': {'threadId': thread['thread']['id']}})
                    self.assertEqual(detached['status'], 'unsubscribed')
                    await connection.close()
                    connection = await unix_connect(str(socket))
                    await rpc({'id': 10, 'method': 'initialize', 'params': {
                        'clientInfo': {'name': 'mv2-test', 'version': '1'}, 'capabilities': {'experimentalApi': True}}})
                    await connection.send(json.dumps({'method': 'initialized', 'params': {}}))
                    frame['id'] = 11
                    result = await rpc(frame)
                    self.assertEqual(result['thread']['id'], thread['thread']['id'])
                    resumed_output.unlink()
                    await rpc({'id': 12, 'method': 'thread/shellCommand', 'params': {'threadId': thread['thread']['id'],
                        'command': shlex.join([sys.executable, '-c', script]), 'timeoutMs': 5000}})
                    for _ in range(100):
                        if resumed_output.exists():
                            break
                        await asyncio.sleep(.05)
                    self.assertTrue(resumed_output.exists(), 'exclusive resume produced no output')
                    self.assertEqual(resumed_output.read_text(), 'resumed-secret')
            finally:
                if connection is not None:
                    await connection.close()
                process.stdin.close()
                # Keep the event loop running so asyncio can reap the leader.
                await asyncio.to_thread(group.close)
                await asyncio.wait_for(process.wait(), 5)
        for bridge in (False, True):
            with self.subTest(bridge=bridge):
                asyncio.run(probe(bridge))


if __name__ == '__main__':
    unittest.main()
