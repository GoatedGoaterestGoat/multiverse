import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from native_processes import PrivateProcessGroup


class PrivateProcessGroupTest(unittest.TestCase):
    def test_stopped_server_leaves_writer_until_its_private_group_is_drained(self):
        with tempfile.TemporaryDirectory(prefix='mv2-native-group-') as directory:
            ready = Path(directory) / 'writer-ready'
            helper = (
                'import pathlib, signal, sys, time\n'
                'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                'path = pathlib.Path(sys.argv[1])\n'
                'while True:\n'
                ' path.write_text("still writing")\n'
                ' time.sleep(.01)\n')
            leader = subprocess.Popen([sys.executable, '-c',
                'import subprocess, sys\n'
                'subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])\n'
                'sys.stdin.read()\n', helper, str(ready)],
                stdin=subprocess.PIPE, start_new_session=True)
            group = PrivateProcessGroup(leader.pid)
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                         start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), 'Private writer did not start')
                leader.stdin.close()
                leader.wait(timeout=5)  # Equivalent to a successful native daemon stop.
                self.assertTrue(group._members(), 'The orphaned writer must still be alive')
                group.close(timeout=.1)  # The helper ignores TERM, requiring bounded KILL.
                self.assertEqual(group._members(), [])
                self.assertIsNone(unrelated.poll(), 'Another private server must be untouched')
                with patch('native_processes.os.killpg', side_effect=AssertionError('closed group reused')):
                    group.close()
                # Temporary home removal now happens only after all writers exit.
            finally:
                group.close(reap=leader.poll)
                leader.wait(timeout=5)
                if leader.stdin and not leader.stdin.closed:
                    leader.stdin.close()
                unrelated.kill()
                unrelated.wait(timeout=5)

    def test_rejects_the_callers_group_before_signalling(self):
        with patch('native_processes.os.killpg', side_effect=AssertionError('must not signal')):
            with self.assertRaisesRegex(AssertionError, 'separate process group'):
                PrivateProcessGroup(os.getpid())
