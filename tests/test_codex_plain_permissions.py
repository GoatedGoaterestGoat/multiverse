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

    def test_generated_add_dir_is_kept_only_for_explicit_writable_grants(self):
        """A plain launch keeps mv2's generated --add-dir only with an explicit writable grant, whatever the flag
        spelling, and never touches the user's own arguments; fragile because Codex has many writable spellings and
        opaque values that look like flags, and a miss brings back Error adding directories or drops the agent's
        writable folder (CODEX-03, fe2fb65)."""
        context = LaunchContext('spawn', self.values,
            endpoint=NativeEndpoint('unix-websocket', '/tmp/private/native.sock'))
        session = SessionHandle('agent', 'codex', '/workspace', native_id=self.values['session_id'])
        for mode in ('spawn', 'resume'):
            for settings in ({'inbox_bridge': False}, {'args': ['--no-daemon']}, {}):
                with self.subTest('default permissions', mode=mode, settings=settings):
                    adapter = CodexAdapter.configured('codex', settings)
                    command = adapter.spawn(context) if mode == 'spawn' else adapter.resume(session, context)
                    plain = command.fallback if command.native_mode['mode'] == 'bridge' else command
                    self.assertEqual(plain.native_mode['mode'], 'plain')
                    self.assertNotIn('--add-dir', plain.argv)
                    note, = plain.notices
                    self.assertIn(self.values['agent_dir'], note)
                    if mode == 'spawn':
                        self.assertEqual(plain.argv[-2:], ('--', self.values['task']))
                    else:
                        self.assertIn(self.values['session_id'], plain.argv)
                    if plain is command:
                        self.assertIsNone(command.rpc_proxy)
        for mode in ('spawn', 'resume'):
            for flags in ([], ['--sandbox', 'read-only'], ['-sread-only'],
                          ['-c', 'sandbox_mode="workspace-write"'], ['--profile', 'trusted']):
                with self.subTest(mode=mode, flags=flags):
                    before, argv, omitted = self.render(flags, mode)
                    self.assertTrue(omitted)
                    index = before.index('--add-dir')
                    self.assertEqual(argv, list(before[:index] + before[index + 2:]))
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
        for mode in ('spawn', 'resume'):
            with self.subTest('user directories', mode=mode):
                flags = ['--add-dir', self.values['agent_dir'], '--add-dir=/user/root',
                         '--sandbox', 'read-only']
                before, argv, omitted = self.render(flags, mode)
                self.assertTrue(omitted)
                self.assertEqual(argv.count('--add-dir'), 1)
                self.assertIn('--add-dir=/user/root', argv)
                self.assertEqual(argv[-(len(flags) + 2):-2], flags)
        for flags in (['--model', '--sandbox=workspace-write'],
                      ['--add-dir', '--dangerously-bypass-approvals-and-sandbox'],
                      ['-c', 'developer_instructions="--approve-for-me"']):
            with self.subTest('opaque values', flags=flags):
                self.assertTrue(self.render(flags)[2])
        with self.subTest('prompt text'):
            self.assertTrue(self.render(task='--sandbox workspace-write --approve-for-me')[2])
        for flags in (['--sandbox'], ['--sandbox=unknown'],
                      ['-sread-only', '--sandbox=workspace-write'],
                      ['--approve-for-me', '-sread-only']):
            with self.subTest('malformed or conflicting', flags=flags):
                before, argv, omitted = self.render(flags)
                self.assertTrue(omitted)
                self.assertEqual(argv[argv.index('-c') + 2:-2], flags)
        with self.subTest('custom recipe'):
            argv = ['codex', '--add-dir', '/user/root', '--', 'task']
            actual, omitted = plain_arguments(argv, ['codex', '--add-dir', '/user/root', '--', '{task}'], self.values)
            self.assertEqual(actual, argv)
            self.assertFalse(omitted)
