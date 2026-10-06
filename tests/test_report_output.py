"""Report stays a record-returning Python API with concise shell output."""
import os
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from multiverse.cli import parser, run
from multiverse.universe.mailbox import append_message, inbox
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

    def report(self, *arguments, **environment):
        return subprocess.run([sys.executable, '-m', 'multiverse.cli', 'report', *arguments], env={**self.env, **environment},
                              capture_output=True, text=True, timeout=10)

    def mail(self, delivery, count=1):
        for number in range(count):
            append_message(self.directory, f'{delivery} {number}', {'agent_id': None, 'name': 'user'}, delivery)

    def test_all_cli_entrypoints_print_one_line_without_task_or_properties(self):
        for command in (['multiverse.cli', 'report'], ['multiverse.cli', 'status'], ['multiverse.tools.report']):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, '-m', *command, '--phase', 'working',
                    '--summary', 'Checking launch\npermissions'], env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'Reported working.\n')  # the summary is saved, never echoed
                self.assertNotIn('PRIVATE FULL TASK', result.stdout)
                self.assertEqual(read(self.directory / 'properties.json')['summary'], 'Checking launch\npermissions')

    def test_a_report_without_a_summary_and_a_stale_launch(self):
        result = self.report('--phase', 'done')
        self.assertEqual(result.stdout, 'Reported done.\n')
        failed = subprocess.run([sys.executable, '-m', 'multiverse.tools.report', '--phase', 'working'],
            env={**self.env, 'MV2_LAUNCH_ID': 'old'}, capture_output=True, text=True, timeout=10)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, '')
        self.assertIn('Outdated launch', failed.stderr)
        self.assertEqual(read(self.directory / 'properties.json')['phase'], 'done')

    def test_unread_inbox_only_messages_are_counted_and_nothing_is_marked_read(self):
        self.assertEqual(self.report('--phase', 'working').stdout, 'Reported working.\n')  # nothing unread: only that
        self.mail('inbox-only')
        self.assertEqual(self.report('--phase', 'done').stdout, 'Reported done. You have 1 unread inbox message.\n')
        self.mail('inbox-only', 2)
        self.mail('steer')      # delivered natively, so not an unread inbox message
        self.mail('follow-up')
        for command in (['multiverse.cli', 'report'], ['multiverse.cli', 'status'], ['multiverse.tools.report']):
            result = subprocess.run([sys.executable, '-m', *command, '--phase', 'blocked'], env=self.env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.stdout, 'Reported blocked. You have 3 unread inbox messages.\n')
        # Reporting is not reading: the same three are still unread, and a real read still gets them all.
        self.assertFalse((self.directory / 'inbox.cursor').exists())
        self.assertEqual(len(inbox(self.directory, peek=True)['messages']), 3)
        self.assertEqual(len(inbox(self.directory)['messages']), 3)
        self.assertEqual(self.report('--phase', 'done').stdout, 'Reported done.\n')

    def test_the_count_is_taken_in_process_and_never_fails_the_report(self):
        self.mail('inbox-only', 2)
        with patch.dict(os.environ, self.env), patch('subprocess.Popen') as spawned, patch('subprocess.run') as ran:
            self.assertEqual(report.cli_run(agent=self.agent, phase='working', summary='x'),
                             'Reported working. You have 2 unread inbox messages.')
        spawned.assert_not_called()
        ran.assert_not_called()
        # A cursor past the end of the log makes the inbox unreadable: the report still succeeds, without a count.
        (self.directory / 'inbox.cursor').write_text('999999')
        result = self.report('--phase', 'done')
        self.assertEqual((result.returncode, result.stdout), (0, 'Reported done.\n'))
        self.assertEqual(read(self.directory / 'properties.json')['phase'], 'done')

    def test_a_summary_only_report_names_the_current_phase(self):
        self.assertEqual(self.report('--summary', 'Still reading').stdout, 'Reported starting.\n')
        self.assertEqual(read(self.directory / 'properties.json')['summary'], 'Still reading')
