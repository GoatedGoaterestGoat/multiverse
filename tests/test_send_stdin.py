"""Send preserves multiline stdin and never reads terminal input implicitly."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from multiverse import cli
from multiverse.universe import templates
from multiverse.store import read, write
from multiverse.tools import send
from isolation import isolate


class TerminalInput(io.StringIO):
    def isatty(self):
        return True

    def read(self, *args):
        raise AssertionError('Terminal input must not be read implicitly')


class SendStdinTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = templates.create(self.work / 'universe', 'stdin-tests', workdir=self.work)
        self.agent = str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        write(self.directory / 'properties.json', {
            'id': self.agent, 'name': 'worker',
            'universe_id': read(self.root / 'universe.json')['id'],
        })
        write(self.directory / 'runtime.json', {'state': 'stopped'})
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
        self.env['PYTHONPATH'] = str(PROJECT / 'src')
        self.message = "first line\nuser's \"quotes\", $variables, $(false), `false`\nlast line\n"

    def messages(self):
        path = self.directory / 'inbox.jsonl'
        return [json.loads(line)['text'] for line in path.read_text().splitlines()] if path.exists() else []

    def argv(self, standalone=False):
        entry = ['-m', 'multiverse.tools.send'] if standalone else [str(PROJECT / 'bin/mv2')]
        return [sys.executable, *entry, '-u', str(self.root),
                *([] if standalone else ['send']), self.agent, '--queue-only']

    def test_pipe_reads_stdin_automatically_for_cli_and_standalone(self):
        for standalone in (False, True):
            with self.subTest(standalone=standalone):
                result = subprocess.run(self.argv(standalone), input=self.message, text=True,
                                        capture_output=True, env=self.env, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.messages()[-1], self.message)

    def test_quoted_heredoc_preserves_shell_metacharacters(self):
        command = shlex.join(self.argv()) + " <<'EOF'\n" + self.message + 'EOF\n'
        result = subprocess.run(['/bin/sh', '-c', command], text=True, capture_output=True,
                                env=self.env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.messages(), [self.message])

    def test_explicit_stdin_still_reads_input(self):
        stream = io.StringIO(self.message)
        with patch.object(stream, 'isatty', return_value=True), patch.object(send.sys, 'stdin', stream):
            result = send.run(self.root, agent=self.agent, stdin=True, queue_only=True)
        self.assertEqual(result['text'], self.message)
        self.assertEqual(self.messages(), [self.message])

    def test_explicit_text_and_file_take_precedence_over_stdin(self):
        path = self.work / 'message.txt'
        path.write_text(self.message)
        for options in ({'text': self.message}, {'file': path}):
            with self.subTest(options=options), patch.object(send.sys, 'stdin', TerminalInput()):
                send.run(self.root, agent=self.agent, queue_only=True, **options)
        self.assertEqual(self.messages(), [self.message, self.message])

    def test_terminal_without_message_fails_without_reading_or_saving(self):
        args = cli.parser().parse_args(['-u', str(self.root), 'send', self.agent, '--queue-only'])
        with patch.object(send.sys, 'stdin', TerminalInput()), self.assertRaisesRegex(ValueError, 'message|text'):
            cli.run(args)
        self.assertEqual(self.messages(), [])

    def test_empty_inputs_fail_before_saving(self):
        empty_file = self.work / 'empty.txt'
        empty_file.write_text('')
        for options in ({}, {'stdin': True}, {'text': ''}, {'text': ' \n\t'}, {'file': empty_file}):
            with self.subTest(options=options), patch.object(send.sys, 'stdin', io.StringIO('')):
                with self.assertRaisesRegex(ValueError, '[Ee]mpty|message|text'):
                    send.run(self.root, agent=self.agent, queue_only=True, **options)
        self.assertEqual(self.messages(), [])
        result = subprocess.run(self.argv(), input='', text=True, capture_output=True,
                                env=self.env, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.messages(), [])


if __name__ == '__main__':
    unittest.main()
