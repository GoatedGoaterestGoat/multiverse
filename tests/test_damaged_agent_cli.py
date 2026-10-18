"""Actual CLI reads keep a damaged record local and name its file."""
import json
import os
import subprocess
import sys

from multiverse.runtime import lifecycle
from multiverse.store import write
from multiverse.universe import agents, records
from test_multiverse import BaseTest, ROOT


class DamagedAgentCliTest(BaseTest):
    def setUp(self):
        super().setUp()
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
        return subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root), *arguments],
                              env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def test_actual_cli_reads_healthy_and_damaged_agents_without_tracebacks(self):
        for file, kinds in (('properties.json', ('list', 'truncated', 'empty', 'name', 'harness', 'unreadable')),
                            ('runtime.json', ('list', 'truncated', 'empty', 'unreadable'))):
            for kind in kinds:
                with self.subTest(file=file, kind=kind):
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
                    self.addCleanup(path.chmod, 0o600)
                    for command in (('list', '--json'), ('inspect', self.entries['healthy'].name, '--json'),
                                    ('inspect', self.bad.name, '--json'), ('doctor', '--json')):
                        result = self.invoke(*command)
                        self.assertEqual(result.returncode, 1 if command[0] == 'doctor' else 0, result.stderr)
                        self.assertNotIn('Traceback', result.stdout + result.stderr)
                        value = json.loads(result.stdout)
                        if command[0] == 'list':
                            rows = {row['id']: row for row in value}
                            self.assertEqual(rows[self.entries['healthy'].name]['name'], 'healthy')
                            self.assertIn(file, rows[self.bad.name]['damaged']['file'])
                        elif command[0] == 'doctor':
                            self.assertTrue(any(file in problem for problem in value['problems']), value)
                        elif command[1] == self.bad.name:
                            self.assertIn(file, value['damaged']['file'])
                            self.assertIn(file, value['error'])
                        else:
                            self.assertEqual(value['name'], 'healthy')
                    with self.assertRaisesRegex(records.RecordError, file):
                        agents.directory(self.root, self.bad.name)
                    self.assertEqual(agents.directory(self.root, 'healthy'), self.entries['healthy'])
                    path.chmod(0o600)

    def test_null_identity_fields_are_named_damage_and_nullable_reports_stay_printable(self):
        for field in ('id', 'name', 'harness'):
            value = json.loads(self.original)
            value[field] = None
            write(self.bad / 'properties.json', value)
            result = self.invoke('list', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            row = next(item for item in json.loads(result.stdout) if item.get('id') == self.bad.name)
            self.assertIn(field, row['damaged']['reason'])
        value = json.loads(self.original)
        value.update(phase=None, summary=None)
        write(self.bad / 'properties.json', value)
        result = self.invoke('list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('damaged', result.stdout)
        self.assertIn('healthy', result.stdout)

    def test_doctor_names_an_agents_missing_workspace(self):
        path = self.entries['healthy'] / 'properties.json'
        value = json.loads(path.read_text())
        value['workspace'] = str(self.work / 'gone-workspace')
        write(path, value)
        result = self.invoke('doctor', '--json')
        self.assertEqual(result.returncode, 1)
        problems = json.loads(result.stdout)['problems']
        self.assertTrue(any('healthy' in problem and value['workspace'] in problem for problem in problems), problems)

    def test_send_to_damaged_target_preserves_the_named_record_error(self):
        (self.bad / 'properties.json').write_text('[]')
        result = self.invoke('send', self.bad.name, '--text', 'do not deliver')
        self.assertEqual(result.returncode, 1)
        self.assertIn('properties.json', result.stderr)
        self.assertFalse((self.bad / 'inbox.jsonl').exists())

    def test_one_damaged_recipient_does_not_block_a_healthy_recipient(self):
        (self.bad / 'properties.json').write_text('[]')
        result = self.invoke('send', self.bad.name + ',healthy', '--delivery', 'inbox-only', '--text', 'hello', '--json')
        self.assertEqual(result.returncode, 1)
        value = json.loads(result.stdout)
        self.assertEqual(len(value['recipients']), 2)
        self.assertFalse(value['recipients'][0]['saved'])
        self.assertIn('properties.json', value['recipients'][0]['reason'])
        self.assertTrue(value['recipients'][1]['saved'])

    def test_bad_peer_does_not_hide_healthy_name_or_create_a_record(self):
        (self.bad / 'properties.json').write_text('[]')
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        self.assertEqual(agents.directory(self.root, 'healthy'), self.entries['healthy'])
        with self.assertRaisesRegex(ValueError, 'properties.json'):
            agents.directory(self.root, 'lost-name')
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        self.assertEqual(before, after)
