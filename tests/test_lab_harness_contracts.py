"""Independent offline lab regressions: no tmux server or model calls."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import agents, cli, universe
from multiverse.store import append_message, inbox, read, write


class LabHarnessContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mv2-offline-')
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = universe.create(self.work / 'universe', 'offline', workdir=self.work)
        self.socket = '/offline-test/tmux.sock'
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.backend = self.patches.enter_context(patch('multiverse.agents.Tmux'))
        self.backend.return_value.create.return_value = {
            'socket': self.socket, 'session': '$offline', 'pane': '%offline'}
        self.patches.enter_context(patch('multiverse.agents.shutil.which', return_value=sys.executable))
        self.patches.enter_context(patch.dict(os.environ, MV2_TMUX_SOCKET=self.socket))

    def start(self, **kwargs):
        return agents.start(self.root, name='offline', task='literal $(false) `false`\nquoted',
                            model='gpt-6-astra', socket_path=self.socket, **kwargs)

    def request(self, row):
        return read(self.root / 'state/agents' / row['id'] / 'launches' /
                    row['runtime']['launch_id'] / 'request.json')

    def test_h001_socket_survives_real_runner_and_parent_identity_is_removed(self):
        row = self.start()
        request = self.request(row)
        self.assertEqual(request['env'].get('MV2_TMUX_SOCKET'), self.socket)
        directory = self.root / 'state/agents' / row['id']
        launch = directory / 'launches' / row['runtime']['launch_id']
        output = self.work / 'seen.json'
        request['argv'] = [sys.executable, '-c',
            'import json,os,pathlib; pathlib.Path(' + repr(str(output)) + ').write_text(json.dumps('
            '{k:v for k,v in os.environ.items() if k.startswith("MV2_") or k=="CODEX_THREAD_ID"}))']
        write(launch / 'request.json', request)
        runner = Path(agents.__file__).with_name('runner.py')
        result = subprocess.run([sys.executable, str(runner), str(launch)],
            env={**os.environ, 'CODEX_THREAD_ID': 'parent-session', 'MV2_PARENT_SENTINEL': 'must-strip'},
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = read(output)
        self.assertEqual(seen['MV2_TMUX_SOCKET'], self.socket)
        self.assertEqual(seen['MV2_AGENT_ID'], row['id'])
        self.assertNotIn('CODEX_THREAD_ID', seen)
        self.assertNotIn('MV2_PARENT_SENTINEL', seen)

    def test_all_configured_adapters_exact_resume(self):
        for harness in ('codex', 'claude', 'pi'):
            with self.subTest(harness=harness):
                row = self.start(harness=harness)
                directory = self.root / 'state/agents' / row['id']
                native = row.get('native_session_id')
                if harness == 'codex':
                    native = str(uuid4())
                    agents.bind_session(self.root, row['id'], native, row['runtime']['launch_id'])
                if harness == 'pi':
                    (directory / 'session.jsonl').write_text(__import__('json').dumps({'type': 'session', 'id': str(uuid4())}) + '\n')
                with patch('multiverse.agents.observe', return_value={'live': 'stopped'}):
                    resumed = agents.start(self.root, resume=row['id'], socket_path=self.socket)
                argv = self.request(resumed)['argv']
                self.assertEqual(resumed['id'], row['id'])
                self.assertNotEqual(resumed['runtime']['launch_id'], row['runtime']['launch_id'])
                self.assertNotIn('--last', argv)
                self.assertNotIn(row['task'], argv)
                self.assertIn(native if harness != 'pi' else str(directory / 'session.jsonl'), argv)
                self.assertEqual(argv[argv.index('--model') + 1], 'gpt-6-astra')
                with self.assertRaisesRegex(ValueError, 'Outdated'):
                    agents.update_runtime(directory, row['runtime']['launch_id'], status='idle')

    def test_unbound_codex_cannot_resume(self):
        row = self.start()
        with patch('multiverse.agents.observe', return_value={'live': 'stopped'}), \
                self.assertRaisesRegex(ValueError, 'No native session ID recorded'):
            agents.start(self.root, resume=row['id'], socket_path=self.socket)

    def test_h002_empty_read_and_existing_message_boundaries(self):
        row = self.start()
        directory = self.root / 'state/agents' / row['id']
        self.assertEqual(inbox(directory), {'messages': [], 'cursor': 0})
        first = append_message(directory, 'nonce one', {})
        second = append_message(directory, 'nonce two', {})
        batch = inbox(directory, limit=1)
        self.assertEqual(batch['messages'][0]['id'], first['id'])
        self.assertEqual(inbox(directory)['messages'][0]['id'], second['id'])
        with self.assertRaises(ValueError):
            inbox(directory, after=batch['cursor'] + 1)
        self.assertEqual(inbox(directory)['messages'], [])
        self.assertNotEqual(first['id'], second['id'])

    def test_unknown_agent_inbox_does_not_create_state(self):
        before = set((self.root / 'state/agents').iterdir())
        args = cli.parser().parse_args(['-u', str(self.root), 'inbox', '--agent', 'absent'])
        with self.assertRaisesRegex(ValueError, 'found 0'):
            cli.run(args)
        self.assertEqual(set((self.root / 'state/agents').iterdir()), before)

    def test_agent_facing_templates_describe_current_messaging(self):
        for template in ('base', 'development', 'testing'):
            root = universe.TEMPLATES / template
            for path in root.rglob('*.md'):
                with self.subTest(path=path):
                    text = path.read_text()
                    self.assertNotRegex(text.lower(), r'\back\b|acknowledg|--reply-to|--message-id')
            self.assertIn('marks them read automatically', (root / 'bootstrap.md').read_text())
            skill = (root / 'skills/coordination/SKILL.md').read_text()
            self.assertIn('deliver AGENT --retry', skill)
            self.assertIn('deliver AGENT --check', skill)


if __name__ == '__main__':
    unittest.main()
