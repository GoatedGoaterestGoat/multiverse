"""Only explicit native writable grants retain mv2's extra plain agent root."""
import json
import unittest

from multiverse.harnesses.base import ConfiguredAdapter
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.launch import plain_arguments
from multiverse.harnesses.types import LaunchContext, NativeEndpoint, SessionHandle


class PlainPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.values = {'workspace': '/workspace', 'agent_dir': '/outside/agent', 'name': 'worker',
                       'control_socket': '/tmp/private/relay.sock',
                       'bootstrap_json': json.dumps('bootstrap'), 'task': 'literal task',
                       'session_id': '0b9925e8-814b-474d-b813-2dc9478ba0c9'}

    def render(self, args=(), mode='spawn', source='args', task=None):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False, source: list(args)})
        context = LaunchContext(mode, {**self.values, 'task': task or self.values['task']})
        before = ConfiguredAdapter._launch_command(adapter, context)
        argv, omitted = plain_arguments(before.argv, adapter.spec[mode], context.values)
        return before.argv, argv, omitted

    def test_default_and_read_only_never_add_the_generated_root(self):
        for mode in ('spawn', 'resume'):
            for flags in ([], ['--sandbox', 'read-only'], ['-sread-only'],
                          ['-c', 'sandbox_mode="workspace-write"'], ['--profile', 'trusted']):
                with self.subTest(mode=mode, flags=flags):
                    before, argv, omitted = self.render(flags, mode)
                    self.assertTrue(omitted)
                    index = before.index('--add-dir')
                    self.assertEqual(argv, list(before[:index] + before[index + 2:]))

    def test_native_writable_switch_forms_retain_the_generated_pair(self):
        forms = (['--sandbox', 'workspace-write'], ['--sandbox=danger-full-access'],
                 ['-s', 'workspace-write'], ['-sworkspace-write'], ['-s=danger-full-access'],
                 ['--dangerously-bypass-approvals-and-sandbox'], ['--approve-for-me'],
                 ['--yolo'], ['--not-so-yolo'])
        for mode in ('spawn', 'resume'):
            for source in ('args', 'spawn_args', 'resume_args'):
                for flags in forms:
                    with self.subTest(mode=mode, source=source, flags=flags):
                        before, argv, omitted = self.render(flags, mode, source)
                        self.assertEqual(omitted, source not in ('args', mode + '_args'))
                        if not omitted:
                            self.assertEqual(argv, list(before))

    def test_every_user_directory_is_preserved_even_when_it_matches_the_agent(self):
        for mode in ('spawn', 'resume'):
            flags = ['--add-dir', self.values['agent_dir'], '--add-dir=/user/root',
                     '--sandbox', 'read-only']
            before, argv, omitted = self.render(flags, mode)
            self.assertTrue(omitted)
            self.assertEqual(argv.count('--add-dir'), 1)
            self.assertIn('--add-dir=/user/root', argv)
            self.assertEqual(argv[-(len(flags) + 2):-2], flags)

    def test_opaque_values_and_prompt_text_never_select_permissions(self):
        for flags in (['--model', '--sandbox=workspace-write'],
                      ['--add-dir', '--dangerously-bypass-approvals-and-sandbox'],
                      ['-c', 'developer_instructions="--approve-for-me"']):
            with self.subTest(flags=flags):
                self.assertTrue(self.render(flags)[2])
        self.assertTrue(self.render(task='--sandbox workspace-write --approve-for-me')[2])

    def test_malformed_or_conflicting_native_args_are_preserved_for_native_errors(self):
        for flags in (['--sandbox'], ['--sandbox=unknown'],
                      ['-sread-only', '--sandbox=workspace-write'],
                      ['--approve-for-me', '-sread-only']):
            with self.subTest(flags=flags):
                before, argv, omitted = self.render(flags)
                self.assertTrue(omitted)
                self.assertEqual(argv[argv.index('-c') + 2:-2], flags)

    def test_a_custom_recipe_without_the_generated_pair_is_unchanged(self):
        argv = ['codex', '--add-dir', '/user/root', '--', 'task']
        actual, omitted = plain_arguments(argv, ['codex', '--add-dir', '/user/root', '--', '{task}'], self.values)
        self.assertEqual(actual, argv)
        self.assertFalse(omitted)

    def test_plain_and_auto_fallback_own_the_notice_but_bridge_is_unchanged(self):
        context = LaunchContext('spawn', self.values,
            environment={'CODEX_HOME': '/private/native-home'},
            endpoint=NativeEndpoint('unix-websocket', '/tmp/private/native.sock'))
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.values['session_id'])
        for mode in ('spawn', 'resume'):
            for settings in ({'inbox_bridge': False}, {'args': ['--no-daemon']}, {}, {'inbox_bridge': True}):
                with self.subTest(mode=mode, settings=settings):
                    adapter = CodexAdapter.configured('codex', settings)
                    command = adapter.spawn(context) if mode == 'spawn' else adapter.resume(session, context)
                    if command.native_mode['mode'] == 'plain':
                        self.assertNotIn('--add-dir', command.argv)
                        self.assertEqual(len(command.notices), 1)
                        self.assertIn('"worker"', command.notices[0])
                        self.assertIn('"/outside/agent"', command.notices[0])
                    else:
                        self.assertEqual(command.notices, ())
                        self.assertEqual(command.rpc_proxy.settings['roots'],
                                         ['/outside/agent'] if mode == 'spawn' else [])
                        if command.fallback:
                            self.assertNotIn('--add-dir', command.fallback.argv)
                            self.assertEqual(len(command.fallback.notices), 1)

    def test_note_is_one_line_and_names_the_exact_agent_and_directory(self):
        values = {**self.values, 'name': 'quoted"\nagent', 'agent_dir': '/private/path\nwith"quotes'}
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        note, = adapter.spawn(LaunchContext('spawn', values)).notices
        self.assertNotIn('\n', note)
        self.assertIn(json.dumps(values['name']), note)
        self.assertIn(json.dumps(values['agent_dir']), note)

    def test_explicit_writable_plain_launch_has_no_omission_notice(self):
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False, 'args': ['-sworkspace-write']})
        command = adapter.spawn(LaunchContext('spawn', self.values))
        self.assertIn('--add-dir', command.argv)
        self.assertEqual(command.notices, ())
