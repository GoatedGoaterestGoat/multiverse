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
from test_multiverse import BaseTest, TerminalBase


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

    def test_runner_waits_for_a_parent_making_progress_beyond_fifteen_seconds(self):
        def advance(_seconds):
            self.clock += 1
            write(self.launch / 'registration.json', {**self.owner, 'sequence': int(self.clock),
                  'stage': 'preparing attachment'})
            if self.clock == 20:
                write(self.launch / 'request.json', self.request)
                (self.launch / 'ready').touch()

        child = Mock(pid=123)
        child.wait.return_value = child.poll.return_value = 0
        with patch.object(runner.sys, 'argv', ['runner', str(self.launch)]), \
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

    def wait(self, *, advance=None, **limits):
        def tick(_seconds):
            self.clock += 1
            if advance:
                advance()
        with patch.object(registration.time, 'monotonic', side_effect=lambda: self.clock), \
             patch.object(registration.time, 'sleep', side_effect=tick), \
             patch.object(registration, 'process_snapshot', return_value=(self.owner['parent_started'], 'S')):
            registration.wait_for_release(self.launch, **limits)

    def test_dead_parent_and_reused_parent_pid_fail_without_waiting(self):
        for replacement in (None, ('a different process start', 'S')):
            with self.subTest(replacement=replacement), \
                 patch.object(registration, 'process_snapshot', return_value=replacement), \
                 patch.object(registration.os, 'kill', side_effect=ProcessLookupError), \
                 patch.object(registration.time, 'sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError, 'parent exited'):
                    registration.wait_for_release(self.launch)
                sleep.assert_not_called()

    def test_unreaped_dead_parent_is_not_mistaken_for_a_live_process(self):
        with subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
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

    def test_live_parent_without_progress_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError, 'stopped making progress'):
            self.wait(stall_timeout=3)
        self.assertEqual(self.clock, 3)

    def test_continuous_progress_still_has_an_overall_cap(self):
        def advance():
            write(self.launch / 'registration.json', {**self.owner, 'sequence': int(self.clock)})
        with self.assertRaisesRegex(RuntimeError, 'exceeded 5 seconds'):
            self.wait(advance=advance, total_timeout=5, stall_timeout=2)
        self.assertEqual(self.clock, 5)

    def test_parent_completion_record_is_not_permission_to_start_without_ready(self):
        write(self.launch / 'registration.json', {**self.owner, 'state': 'complete'})
        with self.assertRaisesRegex(RuntimeError, 'stopped making progress'):
            self.wait(stall_timeout=2)

    def test_failed_preparation_is_reported_while_parent_is_still_alive(self):
        progress = registration.Progress(self.launch)
        progress.fail()
        with self.assertRaisesRegex(RuntimeError, 'preparation failed'):
            registration.wait_for_release(self.launch)

    def test_foreign_changed_or_disappearing_registration_is_never_released(self):
        for change, message in (({'launch_id': 'other'}, 'Invalid'),
                                ({'parent_pid': os.getpid() + 1}, 'parent changed'),
                                ({'sequence': -1}, 'Invalid'), (None, 'disappeared')):
            self.clock = 0
            write(self.launch / 'registration.json', self.owner)
            def advance():
                if change is None:
                    (self.launch / 'registration.json').unlink()
                else:
                    write(self.launch / 'registration.json', {**self.owner, **change})
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, message):
                self.wait(advance=advance)

    def test_old_requests_keep_their_bounded_wait_and_ready_compatibility(self):
        (self.launch / 'registration.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Spawn did not finish'):
            self.wait()
        self.assertEqual(self.clock, 15)
        (self.launch / 'ready').touch()
        registration.wait_for_release(self.launch)

    def test_malformed_record_is_not_mistaken_for_an_old_request(self):
        for record in (None, [], {}, {**self.owner, 'parent_pid': True}):
            with self.subTest(record=record):
                write(self.launch / 'registration.json', record)
                with self.assertRaisesRegex(RuntimeError, 'Invalid'):
                    registration.wait_for_release(self.launch)


class StatusBatchTest(BaseTest):
    def test_status_setup_uses_one_tmux_client_with_explicit_targets(self):
        from multiverse.terminal.tmux import Tmux
        backend = Tmux('/private/fixture.sock')
        with patch.object(backend, 'run') as run:
            backend.configure_status({'session': '$7', 'pane': '%9'}, self.root,
                                     {'id': 'agent', 'name': 'worker'}, 'launch', 'fixture')
        self.assertEqual(run.call_count, 1, 'status setup must not start a client for each option')
        args = run.call_args.args
        self.assertIn(';', args)
        self.assertNotIn('-g', args)
        self.assertNotIn('-wg', args)

    def test_status_batch_does_not_hide_a_tmux_error(self):
        from multiverse.terminal.tmux import Tmux
        backend = Tmux('/private/fixture.sock')
        with patch.object(backend, 'run', side_effect=RuntimeError('native tmux error')):
            with self.assertRaisesRegex(RuntimeError, 'native tmux error'):
                backend.configure_status({'session': '$7', 'pane': '%9'}, self.root,
                                         {'id': 'agent', 'name': 'worker'}, 'launch', 'fixture')


class RegistrationOrderTest(TerminalBase):
    def test_status_is_prepared_before_pane_and_child_waits_for_finished_registration(self):
        from multiverse.terminal.tmux import Tmux, status_setup
        from multiverse.universe import agents
        original_create = Tmux.create
        original_launched = lifecycle._launched
        stages = []

        def prepare(*args):
            stages.append('prepared')
            return status_setup(*args)

        def create(backend, *args):
            self.assertEqual(stages, ['prepared'])
            stages.append('pane')
            return original_create(backend, *args)

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
