from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from native_processes import PrivateDaemonGroups, PrivateProcessGroup


class PrivateProcessGroupTest(unittest.TestCase):
    def test_failed_process_observation_never_signals_or_claims_closed(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        group = PrivateProcessGroup(process.pid)
        try:
            with patch('native_processes.subprocess.check_output',
                       side_effect=subprocess.CalledProcessError(1, ['ps'])), \
                    patch('native_processes.os.killpg') as signal_group:
                with self.assertRaises(subprocess.CalledProcessError):
                    group.close()
                signal_group.assert_not_called()
                self.assertFalse(group.closed)
        finally:
            group.close(reap=process.poll)
            process.wait(timeout=5)

    @unittest.skipUnless(hasattr(os, 'fork'), 'POSIX process states required')
    def test_zombie_only_group_needs_no_signal_or_reap_by_pid_one(self):
        with tempfile.TemporaryDirectory(prefix='mv2-native-zombie-') as directory:
            ready, release = Path(directory) / 'ready', Path(directory) / 'release'
            supervisor = subprocess.Popen([sys.executable, '-c',
                'import os,pathlib,sys,time\n'
                'child=os.fork()\n'
                'if child == 0:\n'
                ' os.setsid()\n'
                ' pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n'
                ' while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n'
                ' os._exit(0)\n'
                'sys.stdin.read()\n'
                'os.waitpid(child,0)\n', str(ready), str(release)],
                stdin=subprocess.PIPE, start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), 'Private child did not start')
                child = int(ready.read_text())
                group = PrivateProcessGroup(child)
                release.touch()
                while time.monotonic() < deadline:
                    state = subprocess.check_output(['ps', '-p', str(child), '-o', 'stat='],
                        stdin=subprocess.DEVNULL, text=True, timeout=2).strip()
                    if state.startswith('Z'):
                        break
                    time.sleep(.01)
                else:
                    self.fail('Private child did not become a zombie')
                with patch('native_processes.os.killpg', side_effect=AssertionError('must not signal a zombie')):
                    group.close(timeout=.1)
                self.assertEqual(group._members(), [])
            finally:
                release.touch()
                supervisor.stdin.close()
                supervisor.wait(timeout=5)  # Its own parent reaps it, independent of PID 1.

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


    def test_start_observer_remembers_owned_daemon_before_validation_failure_escapes(self):
        from multiverse.runtime import native
        with tempfile.TemporaryDirectory(prefix='mv2-native-start-') as directory:
            log = Path(directory) / 'native-service.log'
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            groups = PrivateDaemonGroups()
            def invalid_endpoint(specification, workspace, log_path, unset_env, observe_versions=None):
                Path(log_path).write_text(json.dumps({'pid': process.pid}) + '\nNative warning')
                raise RuntimeError('native endpoint validation failed')
            try:
                with patch.object(native, 'start_shared_service', invalid_endpoint):
                    with groups.observe_starts():
                        with self.assertRaisesRegex(RuntimeError, 'endpoint validation failed'):
                            native.start_shared_service(None, None, log, ())
                self.assertEqual(list(groups.groups), [process.pid])
                self.assertIsNone(process.poll(), 'Observation must not stop the server early')
                groups.remember({'pid': process.pid})  # Concurrent/reused start responses share one group.
                self.assertEqual(len(groups.groups), 1)
            finally:
                groups.close(reap=process.poll)
                process.wait(timeout=5)

    def test_concurrent_start_responses_remember_one_owned_group(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        groups = PrivateDaemonGroups()
        try:
            with patch('native_processes.PrivateProcessGroup', wraps=PrivateProcessGroup) as capture:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(groups.remember, [{'pid': process.pid}] * 8))
                self.assertEqual(capture.call_count, 1)
            self.assertEqual(list(groups.groups), [process.pid])
            self.assertIsNone(process.poll())
        finally:
            groups.close(reap=process.poll)
            process.wait(timeout=5)
