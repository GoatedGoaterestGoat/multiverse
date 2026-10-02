"""Report stays a record-returning Python API with concise shell output."""
import os
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from multiverse.cli import parser, run
from multiverse.store import read, write
from multiverse.tools import report
from test_multiverse import BaseTest


class ReportOutputTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        write(self.directory / 'properties.json', {'id': self.agent, 'name': 'reporter',
            'task': 'PRIVATE FULL TASK PROMPT', 'phase': 'starting', 'summary': ''})
        write(self.directory / 'runtime.json', {'launch_id': 'current'})
        self.env = {**os.environ, 'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': self.agent,
                    'MV2_LAUNCH_ID': 'current'}

    def test_python_tool_and_cli_dispatch_still_return_full_record(self):
        with patch.dict(os.environ, self.env):
            result = report.run(agent=self.agent, phase='working', summary='First line\nSecond line')
            dispatched = run(parser().parse_args(['report', '--phase', 'done']))
        self.assertEqual(result['task'], 'PRIVATE FULL TASK PROMPT')
        self.assertEqual(result['summary'], 'First line\nSecond line')
        self.assertEqual(dispatched['phase'], 'done')
        self.assertEqual(dispatched, read(self.directory / 'properties.json'))

    def test_all_cli_entrypoints_print_one_line_without_task_or_properties(self):
        for command in (['multiverse.cli', 'report'], ['multiverse.cli', 'status'], ['multiverse.tools.report']):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, '-m', *command, '--phase', 'working',
                    '--summary', 'Checking launch\npermissions'], env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'Reported working: Checking launch permissions\n')
                self.assertNotIn('PRIVATE FULL TASK', result.stdout)
                self.assertEqual(read(self.directory / 'properties.json')['summary'], 'Checking launch\npermissions')

    def test_empty_summary_omits_colon_and_stale_launch_still_fails(self):
        result = subprocess.run([sys.executable, '-m', 'multiverse.cli', 'report', '--phase', 'done'],
            env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout, 'Reported done\n')
        failed = subprocess.run([sys.executable, '-m', 'multiverse.tools.report', '--phase', 'working'],
            env={**self.env, 'MV2_LAUNCH_ID': 'old'}, capture_output=True, text=True, timeout=10)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, '')
        self.assertIn('Outdated launch', failed.stderr)
        self.assertEqual(read(self.directory / 'properties.json')['phase'], 'done')
