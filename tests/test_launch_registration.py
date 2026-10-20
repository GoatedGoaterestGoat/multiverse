"""A progressing launch parent must not lose its runner at a fixed 15-second boundary."""
import os
from pathlib import Path
import sys
import subprocess
import time
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.runtime import runner, registration, lifecycle
from multiverse.runtime.process import process_started, process_snapshot
from multiverse.store import now, read, write
from test_multiverse import BaseTest, TerminalBase, until


class RegistrationTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.launch_id = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.launch = self.directory / 'launches' / self.launch_id
        self.launch.mkdir(parents=True)
        write(self.directory / 'properties.json', {'id': self.agent, 'harness': 'fixture',
              'name': 'worker', 'workspace': str(self.work)})
        write(self.directory / 'runtime.json', {'launch_id': self.launch_id, 'state': 'starting'})
        self.request = {'argv': ['fixture'], 'env': {'MV2_UNIVERSE': str(self.root),
                        'MV2_AGENT_DIR': str(self.directory), 'MV2_AGENT_ID': self.agent,
                        'MV2_LAUNCH_ID': self.launch_id,
                        'MV2_CLI': str(Path(__file__).resolve().parents[1] / 'bin/mv2'),
                        'MV2_PYTHON': sys.executable}, 'unset_env': []}
        self.clock = 0.
        self.owner = {'schema': 1, 'launch_id': self.launch_id, 'parent_pid': os.getpid(),
                      'parent_started': process_started(os.getpid()), 'sequence': 0,
                      'stage': 'creating terminal', 'state': 'preparing', 'at': now()}
        write(self.launch / 'registration.json', self.owner)

    def test_parent_can_release_and_exit_after_the_runner_reads_an_earlier_stage(self):
        """A parent may release and exit after the runner read an earlier stage, with a real subprocess; fragile
        because the race shows only under load, as intermittent launch failures (ac89025)."""
        script = self.work / 'release-parent.py'
        script.write_text("""import sys, time
from pathlib import Path
from multiverse.runtime.registration import Progress
launch = Path(sys.argv[1])
progress = Progress(launch)
progress.mark('configuring terminal status')
(launch / 'parent-prepared').touch()
while not (launch / 'release-parent').exists(): time.sleep(.01)
progress.release()
""")
        child = subprocess.Popen([sys.executable, str(script), str(self.launch)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        original = registration._parent_gone
        def after_reading_old_stage(owner):
            self.assertEqual(read(self.launch / 'registration.json')['stage'], 'configuring terminal status')
            (self.launch / 'release-parent').touch()
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertTrue((self.launch / 'ready').exists())
            return original(owner)
        try:
            until(lambda: (self.launch / 'parent-prepared').exists())
            with patch.object(registration, '_parent_gone', side_effect=after_reading_old_stage):
                registration.wait_for_release(self.launch)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stderr.close()

    def wait(self, *, advance=None, **limits):
        def tick(_seconds):
            self.clock += 1
            if advance:
                advance()
        with patch.object(registration.time, 'monotonic', side_effect=lambda: self.clock), \
             patch.object(registration.time, 'sleep', side_effect=tick), \
             patch.object(registration, 'process_snapshot', return_value=(self.owner['parent_started'], 'S')):
            registration.wait_for_release(self.launch, **limits)

    def fresh(self):
        """The launch as its parent left it while preparing: the owner's record, no ready, clock at zero."""
        self.clock = 0.
        write(self.launch / 'registration.json', self.owner)
        (self.launch / 'ready').unlink(missing_ok=True)

    def test_runner_waits_for_a_parent_still_making_progress(self):
        """The runner waits past 15 s for a parent still making progress, gives up on a stalled one, caps the
        total, and keeps the old bounded wait for old requests; fragile because the timeouts interact with a legacy
        path, and a fixed 15 s boundary once abandoned slow launches (1105de8)."""
        def advance(_seconds):
            self.clock += 1
            write(self.launch / 'registration.json', {**self.owner, 'sequence': int(self.clock),
                  'stage': 'preparing attachment'})
            if self.clock == 20:
                write(self.launch / 'request.json', self.request)
                (self.launch / 'ready').touch()

        child = Mock(pid=123)
        child.wait.return_value = child.poll.return_value = 0
        with self.subTest(parent='progressing past 15 s'), \
             patch.object(runner.sys, 'argv', ['runner', str(self.launch)]), \
             patch.object(runner.time, 'monotonic', side_effect=lambda: self.clock), \
             patch.object(runner.time, 'sleep', side_effect=advance), \
             patch.object(runner, 'process_started', return_value=None), \
             patch.object(registration, 'process_snapshot', return_value=(self.owner['parent_started'], 'S')), \
             patch.object(runner.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(runner, 'launch_diagnostic', return_value={}):
            result = runner.main()
            self.assertEqual(result, 0, read(self.directory / 'runtime.json').get('error'))
            self.assertEqual(self.clock, 20)
            popen.assert_called_once()

        self.fresh()
        with self.subTest(parent='stalled'), self.assertRaisesRegex(RuntimeError, 'stopped making progress'):
            self.wait(stall_timeout=3)
        self.assertEqual(self.clock, 3)

        self.fresh()
        def progress():
            write(self.launch / 'registration.json', {**self.owner, 'sequence': int(self.clock)})
        with self.subTest(parent='progressing past the cap'), \
                self.assertRaisesRegex(RuntimeError, 'exceeded 5 seconds'):
            self.wait(advance=progress, total_timeout=5, stall_timeout=2)
        self.assertEqual(self.clock, 5)

        self.fresh()
        (self.launch / 'registration.json').unlink()
        with self.subTest(parent='an old request'):
            with self.assertRaisesRegex(RuntimeError, 'Spawn did not finish'):
                self.wait()
            self.assertEqual(self.clock, 15)
            (self.launch / 'ready').touch()
            registration.wait_for_release(self.launch)

    def test_the_child_starts_only_on_a_valid_ready_from_a_live_matching_parent(self):
        """The runner starts the native child only on a valid ready from a live, matching parent, never for a
        dead, reused, zombie, foreign or malformed one; fragile because of pid reuse and malformed records, and a
        break starts a native child for an abandoned launch."""
        for replacement in (None, ('a different process start', 'S')):
            with self.subTest(parent='dead or reused', replacement=replacement), \
                 patch.object(registration, 'process_snapshot', return_value=replacement), \
                 patch.object(registration.os, 'kill', side_effect=ProcessLookupError), \
                 patch.object(registration.time, 'sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError, 'parent exited'):
                    registration.wait_for_release(self.launch)
                sleep.assert_not_called()

        with self.subTest(parent='unreaped'), subprocess.Popen([sys.executable, '-c', 'pass'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
            deadline = time.monotonic() + 5
            snapshot = process_snapshot(child.pid)
            while (snapshot is None or snapshot[1][0] != 'Z') and time.monotonic() < deadline:
                time.sleep(.01)
                snapshot = process_snapshot(child.pid)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot[1][0], 'Z')
            write(self.launch / 'registration.json', {**self.owner, 'parent_pid': child.pid,
                  'parent_started': snapshot[0]})
            with self.assertRaisesRegex(RuntimeError, 'parent exited'):
                registration.wait_for_release(self.launch)

        self.fresh()
        with self.subTest(parent='completed without ready, then exited'):
            code = ("from pathlib import Path; from multiverse.runtime.registration import Progress; "
                    "Progress(Path(__import__('sys').argv[1])).mark('releasing runner', state='complete')")
            subprocess.run([sys.executable, '-c', code, str(self.launch)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=5, check=True)
            self.assertFalse((self.launch / 'ready').exists())
            with self.assertRaisesRegex(RuntimeError, 'parent exited'):
                registration.wait_for_release(self.launch)

        self.fresh()
        write(self.launch / 'registration.json', {**self.owner, 'state': 'complete'})
        with self.subTest(parent='completed without ready'), \
                self.assertRaisesRegex(RuntimeError, 'stopped making progress'):
            self.wait(stall_timeout=2)

        self.fresh()
        registration.Progress(self.launch).fail()
        with self.subTest(parent='failed its preparation'), self.assertRaisesRegex(RuntimeError, 'preparation failed'):
            registration.wait_for_release(self.launch)

        for change, message in (({'launch_id': 'other'}, 'Invalid'),
                                ({'parent_pid': os.getpid() + 1}, 'parent changed'),
                                ({'sequence': -1}, 'Invalid'), (None, 'disappeared')):
            self.fresh()
            def advance():
                if change is None:
                    (self.launch / 'registration.json').unlink()
                else:
                    write(self.launch / 'registration.json', {**self.owner, **change})
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, message):
                self.wait(advance=advance)

        for record in (None, [], {}, {**self.owner, 'parent_pid': True}):
            with self.subTest(record=record):
                write(self.launch / 'registration.json', record)
                with self.assertRaisesRegex(RuntimeError, 'Invalid'):
                    registration.wait_for_release(self.launch)


class RegistrationOrderTest(TerminalBase):
    def test_status_is_prepared_before_pane_and_child_waits_for_finished_registration(self):
        """On real tmux the status line is prepared before the pane and the native child runs only after the launch
        is fully recorded; missing targets can change other sessions' status, while early release lets the native
        process call mv2 before its identity exists, failing intermittently."""
        from multiverse.terminal.tmux import Tmux, status_setup
        from multiverse.universe import agents
        original_create = Tmux.create
        original_configure = Tmux.configure_status
        original_launched = lifecycle._launched
        stages = []

        def prepare(*args):
            stages.append('prepared')
            return status_setup(*args)

        def create(backend, *args):
            self.assertEqual(stages, ['prepared'])
            stages.append('pane')
            return original_create(backend, *args)

        def configure(backend, terminal, *args, **kwargs):
            with patch.object(backend, 'run', wraps=backend.run) as run:
                original_configure(backend, terminal, *args, **kwargs)
            run.assert_called_once()
            commands = [[]]
            for value in run.call_args.args:
                if value == ';':
                    commands.append([])
                else:
                    commands[-1].append(value)
            for command in commands:
                self.assertNotIn('-g', command)
                self.assertIn('-t', command)
                target = terminal['pane'] if '-w' in command or command[0] == 'rename-window' else terminal['session']
                self.assertEqual(command[command.index('-t') + 1], target)

        def launched(root, directory, launch_id, *args):
            launch = directory / 'launches' / launch_id
            record = read(launch / 'registration.json')
            self.assertEqual(record['launch_id'], launch_id)
            self.assertEqual(record['parent_pid'], os.getpid())
            self.assertEqual(record['parent_started'], process_started(os.getpid()))
            self.assertEqual(record['stage'], 'recording final launch')
            self.assertFalse((launch / 'ready').exists())
            self.assertFalse((directory / 'seen.json').exists(), 'native child ran before release')
            self.assertIsNone(agents.runtime(directory).get('pid'))
            stages.append('recorded')
            return original_launched(root, directory, launch_id, *args)

        with patch.object(lifecycle, 'status_setup', side_effect=prepare), \
             patch.object(Tmux, 'create', new=create), \
             patch.object(Tmux, 'configure_status', new=configure), \
             patch.object(lifecycle, '_launched', side_effect=launched):
            result = lifecycle.start(self.root, 'registered', harness='fake', socket_path=self.socket,
                                     wait=True, wait_timeout=8)
        self.assertTrue(result['ready'], result.get('error'))
        self.assertEqual(stages, ['prepared', 'pane', 'recorded'])
        directory = agents.directory(self.root, result['id'])
        record = read(directory / 'launches' / result['runtime']['launch_id'] / 'registration.json')
        self.assertEqual(record['state'], 'complete')
        self.assertTrue((directory / 'seen.json').exists())
        lifecycle.kill(self.root, result['id'])
