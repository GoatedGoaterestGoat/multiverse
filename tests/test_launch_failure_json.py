"""Preflight refusals keep the requested JSON result and nonzero exit status."""
import contextlib
import io
import json
import sys
from unittest.mock import patch
from multiverse import cli
from multiverse.store import write, uid
from multiverse.tools._launch import failure_record
from test_multiverse import BaseTest


class LaunchFailureJsonTest(BaseTest):
    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *args]):
            try:
                cli.main()
            except SystemExit as error:
                code = error.code or 0
        return out.getvalue(), err.getvalue(), code

    def assert_failure(self, result, name, reason):
        out, err, code = result
        self.assertEqual(code, 1)
        self.assertEqual(err, '')
        record = json.loads(out)
        self.assertIs(record['ok'], False)
        self.assertIs(record['ready'], False)
        self.assertEqual(record['name'], name)
        self.assertIn(reason, record['error'])
        return record

    def test_missing_executable_spawn_and_resume_are_json_records(self):
        write(self.root / 'harnesses' / 'missing' / 'harness.json', {'schema': 1,
            'spawn': ['/nonexistent/mv2-test-executable'], 'resume': ['/nonexistent/mv2-test-executable']})
        first = self.assert_failure(self.command('spawn', '--name', 'worker', '--harness', 'missing', '--json'),
                                   'worker', 'Harness executable not found')
        self.assertTrue(first['id'])
        self.assert_failure(self.command('resume', 'worker', '--json'), 'worker', 'Harness executable not found')

    def test_required_old_claude_refusal_is_json_and_record_is_retained(self):
        executable = self.work / 'old-claude'
        executable.write_text('#!/bin/sh\n[ "$1" = --version ] && { echo "2.1.279 (Claude Code)"; exit 0; }\nexit 73\n')
        executable.chmod(0o755)
        write(self.root / 'harnesses' / 'old' / 'harness.json', {'schema': 2, 'adapter': 'claude',
            'settings': {'executable': str(executable), 'inbox_bridge': True}})
        record = self.assert_failure(self.command('spawn', '--name', 'old-agent', '--harness', 'old', '--json'),
                                    'old-agent', '2.1.279 is older')
        self.assertTrue((self.root / 'state' / 'agents' / record['id'] / 'properties.json').is_file())

    def test_enrichment_never_mutates_existing_records_or_masks_damage(self):
        directory = self.root / 'state' / 'agents' / uid()
        write(directory / 'properties.json', {'id': directory.name, 'name': 'existing', 'harness': 'claude',
            'workspace': str(self.work)})
        write(directory / 'runtime.json', {'state': 'running', 'launch_id': uid(), 'pid': 123})
        before = (directory / 'runtime.json').read_bytes()
        result = failure_record(self.root, 'existing', ValueError('name already reserved'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['runtime']['state'], 'running')
        self.assertEqual((directory / 'runtime.json').read_bytes(), before)
        (directory / 'properties.json').write_text('[]')
        result = failure_record(self.root, directory.name, ValueError('original refusal'))
        self.assertEqual(result['reason'], 'original refusal')
        self.assertEqual(result['name'], directory.name)
