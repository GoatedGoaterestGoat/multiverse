"""A saved process identity never substitutes a later reused PID, and never guesses when it cannot tell."""
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from multiverse.runtime import attachments, lifecycle, missing_runtime, process
from multiverse.runtime.process import process_snapshot, process_started
from multiverse.store import read, write
from multiverse.universe import records
from test_multiverse import BaseTest, until


class ProcessEvidenceTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    def attachment(self, finished=True):
        """A stopped launch of an agent folder without records, with the cleanup record its runner wrote and finished
        unless `finished` is False."""
        directory = self.root / 'state/agents' / str(uuid4())
        launch = directory / 'launches' / str(uuid4())
        launch.mkdir(parents=True)
        if finished:
            attachments.begin(launch, process_started(os.getpid()))
            attachments.finish(launch)
        return directory, launch, {'launch_id': launch.name, 'state': 'stopped'}

    def legacy_role(self, launch, name='old role', *, at=None):
        role = attachments.Role(launch, name)
        value = read(role.path)
        write(role.path, {**value, 'pid_started': None, 'at': at if at is not None else self.old})
        return role

    def record(self, name, launched=True):
        """An agent record without runtime.json, with one saved launch request if `launched`."""
        directory, properties, _, _ = lifecycle._record(self.root, name, '', 'codex', None, None)
        write(directory / 'properties.json', properties)
        launch = directory / 'launches' / str(uuid4())
        if launched:
            write(launch / 'request.json', {'env': {'MV2_TMUX_SOCKET': '/recorded.sock'}})
        return directory, properties, launch

    def test_process_evidence_decides_gone_live_or_unknown(self):
        """Boot-scoped Linux ticks or macOS UTC start times decide live (same
        start), gone (reused, zombie, absent) or unknown (unreadable, malformed, out of range, denied), and only gone
        ever releases, for attachment teardown, recovery and require_stopped alike; fragile because start strings are
        written across clock changes, reboots and locales, and both error directions (signalling a reused pid, a
        duplicate launch) are silent (1022b20, 2bcf1af, 49d878c)."""
        proof, lost, orphaned = self.record('proof', launched=False), self.record('lost'), self.record('orphaned')
        with self.subTest('Linux identities use boot-scoped ticks, independent of wall-clock steps'):
            boot = '11111111-1111-4111-8111-111111111111'
            token = f'linux:{boot}:12345'
            stat = '42 (name with ) parentheses) S ' + '0 ' * 18 + '12345 0'
            def proc_text(path, *args, **kwargs):
                return stat if str(path) == '/proc/42/stat' else boot
            with patch.object(sys, 'platform', 'linux'), patch.object(Path, 'read_text', proc_text), \
                    patch.object(process.subprocess, 'run', side_effect=AssertionError('no wall-clock ps identity')):
                self.assertEqual(process_started(42), token)
                self.assertEqual(process_snapshot(42), (token, 'S'))
        with self.subTest('tick identity distinguishes reuse and never opens cleanup after a clock step'):
            token = 'linux:11111111-1111-4111-8111-111111111111:12345'
            for saved, gone in ((token, False), (token[:-5] + '12344', True),
                                (token.replace('11111111-', '22222222-', 1), True),
                                ('linux:damaged:12345', False),
                                ('Thu Jan 1 00:00:00 1970', False)):
                with self.subTest(saved=saved), \
                        patch.object(attachments, '_observation', return_value=(False, (token, 'S'))):
                    self.assertEqual(attachments._gone(42, saved), gone)
            with patch.object(attachments, '_observation', return_value=(False, (token, 'S'))):
                self.assertIsNotNone(attachments.attested_running({'pid': 42, 'pid_started': token}))
            with patch.object(attachments, '_observation', return_value=(False, ('Thu Jan 1 00:00:00 1970', 'S'))):
                self.assertFalse(attachments._gone(42, token))
        for boundary in ('dormant_at', 'started_at'):
            with self.subTest('legacy dormant or previous-boot record releases reused PIDs', boundary=boundary):
                directory, launch, facts = self.attachment(finished=False)
                old = {**facts, 'state': 'running', 'runner_pid': os.getpid(), 'pid': os.getpid(), boundary: self.old}
                # Only the previous-boot row needs a boot boundary; dormant_at
                # compares the actual current process start to the saved exit.
                with (patch.object(attachments, 'boot_time', return_value=datetime.now(timezone.utc).timestamp(), create=True)
                      if boundary == 'started_at' else nullcontext()):
                    self.assertIsNone(attachments.problem(directory, old))
                    self.assertIsNone(attachments.recorded_process_problem(directory, old))
        with self.subTest('a wall-clock step cannot override a current Linux tick identity'):
            directory, _, facts = self.attachment(finished=False)
            current = 'linux:11111111-1111-4111-8111-111111111111:12345'
            with patch.object(attachments, '_observation', return_value=(False, (current, 'S'))), \
                    patch.object(attachments, 'boot_time', return_value=datetime.now(timezone.utc).timestamp()), \
                    patch.object(attachments, '_command', return_value='child'):
                self.assertIsNotNone(attachments.recorded_process_problem(directory,
                    {**facts, 'started_at': self.old, 'pid': 42, 'pid_started': current}))
        with self.subTest('the start probe'):
            result = subprocess.CompletedProcess([], 0, 'Fri Oct 16  22:47:45 2026\n', '')
            with patch.object(sys, 'platform', 'darwin'), \
                    patch('multiverse.runtime.process.subprocess.run', return_value=result) as run:
                self.assertEqual(process_started(123), 'Fri Oct 16 22:47:45 2026')
            self.assertEqual(run.call_args.args[0], ['ps', '-p', '123', '-o', 'lstart='])
            self.assertEqual(run.call_args.kwargs['env']['TZ'], 'UTC')
            self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
            self.assertLessEqual(run.call_args.kwargs['timeout'], 2)
        for pid in (None, True, -1, 0, '123'):
            with self.subTest('no probe for an invalid pid', pid=pid), \
                    patch('multiverse.runtime.process.subprocess.run') as run:
                self.assertIsNone(process_started(pid))
                run.assert_not_called()
        for result in (subprocess.CompletedProcess([], 1, '', 'missing'),
                       subprocess.CompletedProcess([], 0, 'not a start time', ''),
                       OSError('no ps'), subprocess.TimeoutExpired('ps', 2)):
            with self.subTest('an unreadable start is never guessed', result=result), \
                    patch.object(sys, 'platform', 'darwin'), \
                    patch('multiverse.runtime.process.subprocess.run',
                          **({'side_effect': result} if isinstance(result, Exception) else {'return_value': result})):
                self.assertIsNone(process_started(123))
        with self.subTest('a zombie is gone'):
            child = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                until(lambda: (process_snapshot(child.pid) or ('', ''))[1].startswith('Z'), seconds=5)
                os.kill(child.pid, 0)  # A zombie still has a PID, but has no connections to drain.
                self.assertTrue(attachments._gone(child.pid))
            finally:
                child.wait(timeout=5)
        with self.subTest('live, unknown and invalid identities are not gone, and invalid ones are never signalled'):
            snapshot = process_snapshot(os.getpid())
            self.assertIsNotNone(snapshot)
            self.assertFalse(attachments._gone(os.getpid(), snapshot[0]))
            with patch.object(attachments, 'process_snapshot', return_value=None), \
                    patch.object(attachments.os, 'kill', side_effect=PermissionError) as signal:
                self.assertFalse(attachments._gone(123, snapshot[0]))
                signal.assert_called_once_with(123, 0)
            with patch.object(attachments.os, 'kill') as signal:
                for pid in (None, 0, -1, True, '123'):
                    self.assertFalse(attachments._gone(pid))
                signal.assert_not_called()
        with self.subTest('legacy role and connection records started after them are reused'):
            directory, launch, facts = self.attachment()
            role = self.legacy_role(launch)
            logger = attachments.connection_logger(launch)
            item = {'id': str(uuid4()), 'role': 'old connection', 'endpoint': '/private/service.sock',
                    'state': 'opening'}
            logger(item)
            path = launch / 'roles' / ('connection-' + item['id'] + '.json')
            write(path, {**read(path), 'pid_started': None, 'at': self.old})
            self.assertIsNone(attachments.problem(directory, facts))
            self.assertEqual(read(role.path)['state'], 'active', 'Observation must never signal or forge closure')
        with self.subTest('a legacy stopped runner is reused only if it started after the exit'):
            directory, launch, facts = self.attachment()
            (launch / 'cleanup.json').unlink()
            old = {**facts, 'runner_pid': os.getpid(), 'pid': os.getpid(), 'exit_code': 0, 'stopped_at': self.old}
            self.assertIsNone(attachments.problem(directory, old))
            slow = {**old, 'started_at': self.old, 'stopped_at': datetime.now(timezone.utc).isoformat()}
            # This row concerns one boot; a separate row covers an earlier boot.
            with patch.object(attachments, 'boot_time', return_value=0):
                problem = attachments.problem(directory, slow)
            self.assertIsNotNone(problem, 'A legitimate process can start after pre-spawn started_at')
            self.assertIn(str(os.getpid()), problem)
        with self.subTest('a denied probe still sees reuse, and an unknown identity is actionable'):
            directory, launch, facts = self.attachment()
            role = self.legacy_role(launch, at=datetime.now(timezone.utc).isoformat())
            with patch.object(attachments.os, 'kill', side_effect=PermissionError):
                current = process_started(os.getpid())
                reused = current.rsplit(':', 1)[0] + ':0' if current.startswith('linux:') else 'Thu Jan 1 00:00:00 1970'
                self.assertTrue(attachments._gone(os.getpid(), reused))
                problem = attachments.problem(directory, facts)
            self.assertIn(str(os.getpid()), problem)
            self.assertIn('cannot prove', problem)
            self.assertNotIn(process_started(os.getpid()), problem)
            self.assertIn(str(role.path), problem)
            self.assertIn('retry', problem)
        with self.subTest('malformed identity is no proof of reuse, and unknown inspection blocks'):
            directory, launch, facts = self.attachment()
            role = self.legacy_role(launch)
            record = read(role.path)
            for token in ('invalid', [], 1, True):
                with self.subTest(token=token):
                    write(role.path, {**record, 'pid_started': token})
                    self.assertIn(str(role.path), attachments.problem(directory, facts))
            write(role.path, {**record, 'pid': []})
            self.assertIn(str(role.path), attachments.problem(directory, facts))
            write(role.path, record)
            with patch.object(attachments, 'process_snapshot', return_value=None), \
                 patch.object(attachments.os, 'kill', side_effect=PermissionError):
                reason = attachments.problem(directory, facts)
            self.assertIn('cannot prove', reason)
            self.assertIn('inspection unavailable', reason)
            role.path.write_text('{damaged')
            self.assertIn(str(role.path), attachments.problem(directory, facts))
        with self.subTest('an out-of-range saved pid is unknown, not a process control error'):
            directory, launch, facts = self.attachment()
            role = self.legacy_role(launch)
            write(role.path, {**read(role.path), 'pid': 2 ** 90})
            reason = attachments.problem(directory, facts)
            self.assertIn(str(2 ** 90), reason)
            self.assertIn(str(role.path), reason)
            self.assertIn('cannot prove', reason)
        directory, launch, _ = self.attachment(finished=False)
        for value in ([], {'schema': 1, 'launch_id': 'foreign', 'state': 'complete'}):
            with self.subTest('foreign or malformed cleanup records fail closed', cleanup=value), \
                    patch.object(attachments.os, 'kill') as kill:
                write(launch / 'cleanup.json', value)
                self.assertIn('invalid cleanup', attachments.problem(directory, {'launch_id': launch.name}))
                kill.assert_not_called()
        started = 'Fri Oct 16 12:00:00 2026'
        directory, properties, _ = proof
        for pid_key, start_key in (('pid', 'pid_started'), ('runner_pid', 'runner_started')):
            for token, observation in ((started, (False, (started, 'S'))),
                                       (None, (False, (started, 'S'))),
                                       ('damaged', (False, (started, 'S'))),
                                       (started, (False, None))):
                facts = {'launch_id': None, 'state': 'stopped', pid_key: 42, start_key: token}
                with self.subTest('no terminal keeps live or unknown process evidence', pid=pid_key, token=token,
                                  observation=observation), \
                     patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
                     patch.object(attachments, '_observation', return_value=observation), \
                     patch.object(attachments, '_command', return_value='private fixture'):
                    with self.assertRaisesRegex(records.RecordError, 'runtime.json.*PID 42'):
                        missing_runtime.require_stopped(directory, properties, facts=facts)
        facts = {'launch_id': None, 'state': 'stopped', 'pid': 42, 'pid_started': started}
        for observation in ((True, None), (False, (started, 'Z')), (False, ('Sat Oct 17 12:00:00 2026', 'S'))):
            with self.subTest('absent, zombie and reused processes allow recovery', observation=observation), \
                 patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
                 patch.object(attachments, '_observation', return_value=observation):
                notices = missing_runtime.require_stopped(directory, properties, facts=facts)
                self.assertIn('damaged launch authority', notices[0])
        with self.subTest('an observation without an exit is no proof of reuse'), \
             patch.object(attachments, '_observation', return_value=(False, ('Sat Oct 17 12:00:00 2026', 'S'))), \
             patch.object(attachments, '_command', return_value='private fixture'):
            facts = {'launch_id': None, 'state': 'starting', 'pid': 42, 'observed_at': '2026-10-17T00:00:00+00:00'}
            self.assertIsNotNone(attachments.recorded_process_problem(directory, facts))
            self.assertIsNone(attachments.recorded_process_problem(directory, {**facts, 'exit_code': 0}))
        directory, properties, launch = lost
        write(launch / 'cleanup.json', {'schema': 1, 'launch_id': launch.name,
              'state': 'active', 'pid': 42, 'pid_started': started})
        with self.subTest('an active cleanup owner is checked by identity, not by pid'), \
                patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
                patch.object(attachments.os, 'kill', return_value=None), \
                patch.object(attachments, 'process_snapshot', return_value=(started, 'S')), \
                patch.object(attachments, '_command', return_value='runner'):
            with self.assertRaisesRegex(records.RecordError, 'PID 42.*runner'):
                missing_runtime.require_stopped(directory, properties)
        for observation in ((False, (started, 'Z')), (False, ('Sat Oct 17 12:00:00 2026', 'S')), (True, None)):
            with self.subTest('a gone cleanup owner allows recovery', observation=observation), \
                    patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
                    patch.object(attachments, '_observation', return_value=observation):
                self.assertEqual(missing_runtime.require_stopped(directory, properties), [missing_runtime.NOTICE])
        directory, properties, launch = orphaned
        write(launch / 'cleanup.json', {'schema': 1, 'launch_id': launch.name,
              'state': 'active', 'pid': 42, 'pid_started': started,
              'child_pid': 43, 'child_started': 'Fri Oct 16 12:00:01 2026', 'child_at': '2026-10-16T12:00:02+00:00'})

        def observation(pid, observations):
            result = (True, None) if pid == 42 else (False, ('Fri Oct 16 12:00:01 2026', 'S'))
            observations[pid] = result
            return result
        with self.subTest('a recorded child blocks after its runner is gone'), \
                patch.object(missing_runtime.Tmux, 'snapshot', return_value=({}, None)), \
                patch.object(attachments, '_observation', side_effect=observation), \
                patch.object(attachments, '_command', return_value='native-child'):
            with self.assertRaisesRegex(records.RecordError, 'child process.*PID 43'):
                missing_runtime.require_stopped(directory, properties)
