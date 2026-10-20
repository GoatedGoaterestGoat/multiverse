"""Native validation chooses whether mv2's own plain writable root is usable."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from multiverse.harnesses.codex.plugins import Prepared

from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import LaunchContext, NativeEndpoint, SessionHandle


class PlainPermissionsTest(unittest.TestCase):
    def test_plain_retry_removes_only_the_generated_root_after_native_refusal(self):
        """A read-only native exit retries only mv2's root; rendered diagnostics are easy to miss, while removing a user's root silently changes permissions."""
        values = {'workspace': '/workspace', 'agent_dir': '/outside/agent', 'name': 'worker',
                  'control_socket': '/tmp/private/relay.sock', 'bootstrap_json': json.dumps('bootstrap'),
                  'task': 'literal task', 'session_id': '0b9925e8-814b-474d-b813-2dc9478ba0c9'}
        session = SessionHandle('agent', 'codex', '/workspace', native_id=values['session_id'])
        for mode in ('spawn', 'resume'):
            for base_settings in ({'inbox_bridge': False}, {'args': ['--no-daemon']}, {}):
                for flags in ([], ['--sandbox', 'read-only'], ['--sandbox', 'workspace-write'],
                              ['--profile', 'named'], ['--add-dir', values['agent_dir'], '--add-dir=/user/root']):
                    with self.subTest(mode=mode, settings=base_settings, flags=flags):
                        settings = {**base_settings, 'args': [*base_settings.get('args', []), *flags]}
                        adapter = CodexAdapter.configured('codex', settings)
                        context = LaunchContext(mode, values, endpoint=NativeEndpoint('unix-websocket', '/tmp/native'))
                        command = adapter.spawn(context) if mode == 'spawn' else adapter.resume(session, context)
                        plain = command.fallback if command.native_mode['mode'] == 'bridge' else command
                        self.assertEqual(plain.native_mode['mode'], 'plain')
                        index = plain.argv.index('--add-dir')
                        self.assertEqual(plain.argv[index + 1], values['agent_dir'])
                        retry = plain.startup_retry
                        self.assertIsNotNone(retry)
                        self.assertEqual(retry.argv, plain.argv[:index] + plain.argv[index + 2:])
                        self.assertEqual(plain.notices, (), 'Notice applies only when native refusal selects the retry')
                        self.assertIn('read-only', retry.notices[-1])
                        self.assertIn(values['agent_dir'], retry.notices[-1])
                        self.assertEqual(retry.argv[-1], values['task'] if mode == 'spawn' else values['session_id'])
                        if plain is not command:
                            self.assertIsNone(command.startup_retry, 'Bridge grants never use the plain retry')
        with self.subTest('plugin config prefixes preserve generated-root provenance'):
            plan = Mock(environment={})
            plan.record.return_value = {}
            prepared = Prepared(plan, 'bootstrap', defaults={'model_reasoning_effort': 'high'})
            with patch('multiverse.harnesses.codex.plugins.prepare', return_value=prepared):
                plain = CodexAdapter.configured('codex', {'inbox_bridge': False}).spawn(
                    LaunchContext('spawn', values))
            index = plain.argv.index('--add-dir')
            self.assertEqual(plain.startup_retry.argv, plain.argv[:index] + plain.argv[index + 2:])
            self.assertIn('model_reasoning_effort="high"', plain.startup_retry.argv)
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': False})
        captures = json.loads((Path(__file__).parent / 'fixtures/codex/add-dir-readonly.json').read_text())
        capture = captures[0]
        evidence = {'text': capture['text'], 'exit_code': 1, 'agent_dir': capture['agent_dir'],
                    'argv': ['codex', '--add-dir', capture['agent_dir']]}
        for changed, expected in (({}, True), ({'exit_code': 0}, False), ({'exit_code': None}, False),
                                  ({'exit_code': True}, False), ({'text': 'quota exceeded'}, False),
                                  ({'text': capture['text'].replace('Error adding directories:', 'Warning:')}, False),
                                  ({'agent_dir': '/different/agent'}, False),
                                  ({'agent_dir': capture['agent_dir'] + '-sibling'}, False),
                                  ({'agent_dir': None}, False),
                                  ({'text': capture['text'] + '\nDifferent startup failure'}, True)):
            with self.subTest(evidence=changed):
                self.assertIs(adapter.startup_retryable({**evidence, **changed}), expected)
        inline = next(row for row in captures if row['case'] == 'inline-readonly-spawn')
        for text, directory, expected in (
                (inline['text'], inline['agent_dir'], True),
                (inline['text'].replace(inline['workspace'], '~/project'), inline['agent_dir'], True),
                (inline['text'].replace(inline['workspace'], '/long/…/project'), inline['agent_dir'], True),
                (''.join(char + '\n' for char in inline['text']), inline['agent_dir'], True),
                ('Saved native storage retained.\n' + inline['text'], inline['agent_dir'], True),
                (inline['text'], None, False),
                (inline['text'], {'damaged': 'directory'}, False),
                (inline['text'], '/different/agent', False),
                (inline['text'] + '\x1b', inline['agent_dir'], False),
                (inline['text'] + '\0', inline['agent_dir'], False)):
            with self.subTest(inline_agent_dir=directory, text=text):
                self.assertIs(adapter.startup_retryable(
                    {**evidence, 'text': text, 'agent_dir': directory}), expected)
