"""Child identity survives exec, but never substitutes a later reused PID."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from multiverse.runtime import lifecycle
from multiverse.universe import agents
from multiverse.harnesses.types import SessionHandle
from multiverse.store import write
from test_multiverse import TerminalBase, until


class ProcessIdentityTest(unittest.TestCase):
    def test_recorded_start_is_optional_and_part_of_the_exact_runtime_snapshot(self):
        session = SessionHandle.from_records({'id': 'a', 'harness': 'fixture', 'workspace': '/tmp'},
                                             {'pid': 123, 'pid_started': 'Fri Oct 16 22:47:45 2026'}, Path('/tmp/a'))
        self.assertEqual(session.pid_started, 'Fri Oct 16 22:47:45 2026')
        self.assertIsNone(SessionHandle('a', 'fixture', '/tmp').pid_started)

    def test_start_probe_has_a_fixed_timezone_locale_and_bounded_timeout(self):
        from multiverse.runtime.process import process_started
        result = subprocess.CompletedProcess([], 0, 'Fri Oct 16  22:47:45 2026\n', '')
        with patch('multiverse.runtime.process.subprocess.run', return_value=result) as run:
            self.assertEqual(process_started(123), 'Fri Oct 16 22:47:45 2026')
        self.assertEqual(run.call_args.args[0], ['ps', '-p', '123', '-o', 'lstart='])
        self.assertEqual(run.call_args.kwargs['env']['TZ'], 'UTC')
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
        self.assertLessEqual(run.call_args.kwargs['timeout'], 2)

    def test_unknown_process_or_unreadable_start_is_never_guessed(self):
        from multiverse.runtime.process import process_started
        for pid in (None, True, -1, 0, '123'):
            with patch('multiverse.runtime.process.subprocess.run') as run:
                self.assertIsNone(process_started(pid))
                run.assert_not_called()
        for result in (subprocess.CompletedProcess([], 1, '', 'missing'),
                       subprocess.CompletedProcess([], 0, 'not a start time', ''),
                       OSError('no ps'), subprocess.TimeoutExpired('ps', 2)):
            with patch('multiverse.runtime.process.subprocess.run',
                       **({'side_effect': result} if isinstance(result, Exception) else {'return_value': result})):
                self.assertIsNone(process_started(123))


class RunnerProcessIdentityTest(TerminalBase):
    def test_runner_records_start_and_exec_keeps_that_child_identity(self):
        from multiverse.runtime.process import process_started
        script = self.work / 'exec-child.py'
        script.write_text('import os,sys,time\n'
                          'if len(sys.argv) > 1:\n'
                          ' print("RESTARTED CHILD", os.getpid(), flush=True)\n'
                          ' while True: time.sleep(1)\n'
                          'print("INITIAL CHILD", os.getpid(), flush=True)\n'
                          'for line in sys.stdin:\n'
                          ' if line.strip() == "restart":\n'
                          '  os.execv(sys.executable, [sys.executable, __file__, "again"])\n')
        write(self.root / 'harnesses/fixture/harness.json', {'schema': 1,
              'spawn': [sys.executable, str(script)], 'resume': [sys.executable, str(script)]})
        launched = lifecycle.start(self.root, 'process-child', harness='fixture', socket_path=self.socket,
                                   wait=True, wait_timeout=8)
        self.assertTrue(launched['ready'], launched)
        directory = agents.directory(self.root, launched['id'])
        first = agents.runtime(directory)
        self.assertEqual(first['pid_started'], process_started(first['pid']))
        self.assertIsNotNone(first['pid_started'])
        self.backend.run('send-keys', '-t', first['tmux']['pane'], 'restart', 'Enter')
        until(lambda: 'RESTARTED CHILD' in self.backend.run('capture-pane', '-p', '-t', first['tmux']['pane']))
        after = agents.runtime(directory)
        self.assertEqual((after['pid'], after['pid_started']), (first['pid'], process_started(first['pid'])))
        lifecycle.kill(self.root, launched['id'])
