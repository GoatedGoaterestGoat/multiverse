"""CLI commands keep a damaged record local and name its file."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from multiverse import cli
from multiverse.runtime import lifecycle
from multiverse.store import write
from multiverse.universe import agents, records
from test_multiverse import BaseTest, ROOT

REAL = (('properties.json', 'list'), ('runtime.json', 'truncated'))  # also through bin/mv2 in its own process


class DamagedAgentCliTest(BaseTest):
    def setUp(self):
        super().setUp()
        # Commands that look for a terminal ask the default server too: make it this test's own, absent one.
        self.enterContext(patch.dict(os.environ, {'TMUX_TMPDIR': str(self.work)}))
        harness = self.root / 'harnesses/fake'
        harness.mkdir()
        write(harness / 'harness.json', {'schema': 1, 'spawn': ['/bin/cat'], 'resume': ['/bin/cat']})
        self.entries = {}
        for name in ('healthy', 'damaged'):
            directory, properties, _, _ = lifecycle._record(self.root, name, '', 'fake', None, None)
            properties['workspace'] = str(self.work)
            write(directory / 'properties.json', properties)
            self.entries[name] = directory
        self.bad = self.entries['damaged']
        self.original = (self.bad / 'properties.json').read_bytes()

    def invoke(self, *arguments):
        """mv2 run in this process: an uncaught exception fails the test as a traceback would."""
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *arguments]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                cli.main()
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else int(exit.code is not None)
        return SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())

    def real(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root), *arguments],
                              env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def test_each_command_keeps_a_damaged_record_local(self):
        """list, inspect, doctor, send and kill, as text and as JSON, keep each form of a damaged record local and
        name its file and distinguish readable damage from denied access, also through the real CLI; fragile
        because each command has its own read path, and one
        uncaught RecordError breaks that command for every agent (c61c4c0, LC-9)."""
        healthy = self.entries['healthy'].name
        for file, kinds in (('properties.json', ('list', 'truncated', 'empty', 'name', 'harness', 'unreadable')),
                            ('runtime.json', ('list', 'truncated', 'empty', 'unreadable'))):
            for kind in kinds:
                (self.bad / 'properties.json').chmod(0o600)
                (self.bad / 'properties.json').write_bytes(self.original)
                (self.bad / 'runtime.json').unlink(missing_ok=True)
                path = self.bad / file
                if kind in ('name', 'harness'):
                    value = json.loads(self.original)
                    value[kind] = []
                    write(path, value)
                else:
                    path.write_text({'list': '[]', 'truncated': '{"id":', 'empty': '', 'unreadable': '{}'}[kind])
                if kind == 'unreadable':
                    path.chmod(0)
                    self.addCleanup(lambda path=path: path.exists() and path.chmod(0o600))
                for invoke in (self.invoke, self.real) if (file, kind) in REAL else (self.invoke,):
                    for command in (('list', '--json'), ('inspect', healthy, '--json'),
                                    ('inspect', self.bad.name, '--json'), ('doctor', '--json'),
                                    ('list',), ('inspect', healthy), ('doctor',)):
                        with self.subTest(file=file, kind=kind, command=command, real=invoke == self.real):
                            result = invoke(*command)
                            self.assertEqual(result.returncode, 1 if command[0] == 'doctor' else 0, result.stderr)
                            self.assertNotIn('Traceback', result.stdout + result.stderr)
                            if '--json' not in command:
                                self.assertIn('healthy' if command[0] in ('list', 'inspect') else file, result.stdout)
                                continue
                            value = json.loads(result.stdout)
                            if command[0] == 'list':
                                rows = {row['id']: row for row in value}
                                self.assertEqual(rows[healthy]['name'], 'healthy')
                                self.assertIn(file, rows[self.bad.name]['damaged']['file'])
                                if file == 'runtime.json':
                                    reason = rows[self.bad.name]['damaged']['reason']
                                    self.assertEqual('restore read access' in reason, kind == 'unreadable')
                                    if kind != 'unreadable':
                                        self.assertIn('repair or restore', reason)
                            elif command[0] == 'doctor':
                                self.assertTrue(any(file in problem for problem in value['problems']), value)
                            elif command[1] == self.bad.name:
                                self.assertIn(file, value['damaged']['file'])
                                self.assertIn(file, value['error'])
                            else:
                                self.assertEqual(value['name'], 'healthy')
                with self.subTest(file=file, kind=kind, command='lookup'):
                    with self.assertRaisesRegex(records.RecordError, file):
                        agents.directory(self.root, self.bad.name)
                    self.assertEqual(agents.directory(self.root, 'healthy'), self.entries['healthy'])
                path.chmod(0o600)
        (self.bad / 'runtime.json').unlink()
        (self.bad / 'properties.json').write_text('[]')
        with self.subTest(command='send to the damaged agent'):
            result = self.invoke('send', self.bad.name, '--text', 'do not deliver')
            self.assertEqual(result.returncode, 1)
            self.assertIn('properties.json', result.stderr)
            self.assertFalse((self.bad / 'inbox.jsonl').exists())
        with self.subTest(command='send to the damaged and the healthy agent'):
            result = self.invoke('send', self.bad.name + ',healthy', '--delivery', 'inbox-only', '--text', 'hello',
                                 '--json')
            self.assertEqual(result.returncode, 1)
            value = json.loads(result.stdout)
            self.assertEqual(len(value['recipients']), 2)
            self.assertFalse(value['recipients'][0]['saved'])
            self.assertIn('properties.json', value['recipients'][0]['reason'])
            self.assertTrue(value['recipients'][1]['saved'])
        for command in (('send', 'healthy', '--delivery', 'inbox-only', '--text', 'still reachable'),
                        ('kill', 'healthy')):
            with self.subTest(command=command[0] + ' the healthy agent'):
                result = self.invoke(*command)
                self.assertEqual(result.returncode, 0, result.stderr)
