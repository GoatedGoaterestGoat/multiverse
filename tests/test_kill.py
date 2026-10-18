"""Real, isolated tmux shutdown stages and exact-launch process ownership."""
import os
from pathlib import Path
import shutil
import signal
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse.universe import agents, records
from multiverse.runtime import lifecycle
from multiverse.runtime import native
from multiverse.universe import templates
from multiverse.universe import relocate
from multiverse.store import read
from multiverse.terminal.tmux import Tmux
from test_multiverse import TerminalBase, until


class KillTest(TerminalBase):
    def test_attachment_only_describes_service_and_terminal_work_ownership(self):
        for owner in ('service', 'terminal', None, 'damaged'):
            with self.subTest(owner=owner):
                launched = lifecycle.start(self.root, 'attachment-' + str(owner), 'a task', 'fake', socket_path=self.socket)
                directory = agents.directory(self.root, launched['id'])
                until(lambda: (directory / 'seen.json').exists())
                facts = agents.runtime(directory)
                agents.update_runtime(directory, facts['launch_id'],
                                      attachment=[] if owner == 'damaged' else {'work_owner': owner} if owner else None)
                with patch.object(native, 'stop', side_effect=AssertionError('must not request cancellation')):
                    result = lifecycle.kill(self.root, launched['id'], attachment_only=True)
                reason = result['native_cancellation']['reason']
                self.assertEqual(result['state'], 'dormant')
                self.assertNotIn(launched['runtime']['tmux']['pane'], self.backend.snapshot()[0])
                if owner == 'service':
                    self.assertIn('conversation work was not cancelled', reason)
                else:
                    self.assertNotIn('work was not cancelled', reason)
                    self.assertIn('ends work owned by that process', reason)

    def retain_wrapper(self, launched, directory):
        # Simulate an older/interrupted runner: compatibility paths must still
        # handle dead panes even though normal native exits now remove them.
        os.kill(agents.runtime(directory)['runner_pid'], signal.SIGTERM)
        until(lambda: lifecycle.listing(directory.parents[2])[0]['live'] == 'stopped')

    def test_native_exit_precedes_wrapper_teardown_including_older_panes(self):
        launched, directory = self.spawn()
        identifier = str(uuid4())
        agents.bind_session(self.root, launched['id'], identifier)
        pane = launched['runtime']['tmux']['pane']
        self.backend.run('set-option', '-p', '-t', pane, 'remain-on-exit', 'off')
        cancelling, release, errors, stages = threading.Event(), threading.Event(), [], []
        original = Tmux.run

        def native_stop(*args):
            stages.append(lifecycle.listing(self.root)[0]['live'])
            self.assertEqual(lifecycle.listing(self.root)[0]['status'], '-')
            self.assertEqual(self.backend.run('show-options', '-pv', '-t', pane, 'remain-on-exit'), 'on')
            cancelling.set()
            if not release.wait(3):
                raise TimeoutError('test release was not signalled')

        def command(backend, *args):
            if args[:1] == ('kill-pane',):
                row = lifecycle.listing(self.root)[0]
                stages.append(row['live'])
                self.assertEqual(row['status'], '-')
                self.assertIn(pane, self.backend.snapshot()[0])
                with self.assertRaises(ProcessLookupError):
                    os.kill(row['runtime']['pid'], 0)
            return original(backend, *args)

        def kill():
            try:
                lifecycle.kill(self.root, launched['id'])
            except Exception as error:
                errors.append(error)

        with patch.object(native, 'stop', side_effect=native_stop), patch.object(Tmux, 'run', command):
            worker = threading.Thread(target=kill)
            worker.start()
            try:
                self.assertTrue(cancelling.wait(3))
                self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'ending')
            finally:
                release.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(stages, ['ending', 'stopped'])
        row = lifecycle.listing(self.root)[0]
        self.assertEqual((row['live'], row['status']), ('dormant', '-'))
        self.assertNotIn(pane, self.backend.snapshot()[0])
        self.assertEqual(read(directory / 'properties.json')['native_session_id'], identifier)
        resumed = lifecycle.start(self.root, resume=launched['id'], socket_path=self.socket)
        self.assertEqual(resumed['native_session_id'], identifier)
        self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'alive')

    def test_ctrl_c_removes_wrapper_and_resume_preserves_session(self):
        launched, directory = self.spawn()
        identifier = str(uuid4())
        agents.bind_session(self.root, launched['id'], identifier)
        old_pane = launched['runtime']['tmux']['pane']
        self.backend.run('send-keys', '-t', old_pane, 'C-c')
        until(lambda: lifecycle.listing(self.root)[0]['live'] == 'dormant')
        self.assertNotIn(old_pane, self.backend.snapshot()[0])
        self.assertEqual(lifecycle.listing(self.root)[0]['status'], '-')
        resumed = lifecycle.start(self.root, resume=launched['id'], socket_path=self.socket)
        self.assertEqual(resumed['native_session_id'], identifier)
        panes = self.backend.snapshot()[0]
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[resumed['runtime']['tmux']['pane']]['@mv2_launch_id'], resumed['runtime']['launch_id'])
        self.assertNotEqual(resumed['runtime']['launch_id'], launched['runtime']['launch_id'])

    def test_kill_stopped_wrapper_does_not_signal_another_process(self):
        launched, directory = self.spawn()
        pane = launched['runtime']['tmux']['pane']
        self.retain_wrapper(launched, directory)
        with patch.object(Tmux, 'signal_child') as signal_child:
            self.assertEqual(lifecycle.kill(self.root, launched['id'])['state'], 'dormant')
        signal_child.assert_not_called()

    def test_stop_with_absent_or_foreign_pane_never_controls_the_pane(self):
        launched, directory = self.spawn()
        pane = launched['runtime']['tmux']['pane']
        self.backend.run('set-option', '-p', '-t', pane, '@mv2_launch_id', 'foreign')
        with patch.object(Tmux, 'signal_child') as signal_child:
            with patch.object(lifecycle, 'TEARDOWN_TIMEOUT', .1):
                with self.assertRaisesRegex(records.RecordError, 'runtime.json.*tagged terminal.*valid saved launch'):
                    lifecycle.terminal_action(self.root, launched['id'], 'stop')
            self.assertIn(pane, self.backend.snapshot()[0])
            self.backend.run('kill-pane', '-t', pane)
            result = lifecycle.terminal_action(self.root, launched['id'], 'stop')
        signal_child.assert_not_called()
        self.assertEqual(result['state'], 'dormant')
        self.assertEqual(result['native_cancellation']['reason'], 'No native process was running')

    def test_dormant_native_cancellation_failure_preserves_identity(self):
        launched, directory = self.spawn()
        identifier = str(uuid4())
        agents.bind_session(self.root, launched['id'], identifier)
        self.backend.run('kill-pane', '-t', launched['runtime']['tmux']['pane'])
        with patch.object(native, 'stop', side_effect=RuntimeError('native daemon unreachable')):
            result = lifecycle.kill(self.root, launched['id'])
        self.assertEqual(result['native_cancellation']['state'], 'unknown')
        self.assertIn('native daemon unreachable', result['native_cancellation']['reason'])
        self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'dormant')
        self.assertEqual(read(directory / 'properties.json')['native_session_id'], identifier)

    def test_dead_wrapper_removal_does_not_require_native_daemon(self):
        launched, directory = self.spawn()
        identifier = str(uuid4())
        agents.bind_session(self.root, launched['id'], identifier)
        pane = launched['runtime']['tmux']['pane']
        self.retain_wrapper(launched, directory)
        with patch.object(native, 'stop', side_effect=ConnectionRefusedError(61, 'Connection refused')), \
                patch.object(Tmux, 'signal_child') as signal_child:
            result = lifecycle.kill(self.root, launched['id'])
        signal_child.assert_not_called()
        self.assertEqual(result['state'], 'dormant')
        self.assertEqual(result['native_cancellation']['state'], 'unknown')
        self.assertIn('unconfirmed', agents.runtime(directory)['error'])
        self.assertNotIn(pane, self.backend.snapshot()[0])
        resumed = lifecycle.start(self.root, resume=launched['id'], socket_path=self.socket)
        self.assertEqual(resumed['native_session_id'], identifier)

    def test_move_and_remove_clear_retained_wrappers_and_preserve_identity(self):
        launched, directory = self.spawn()
        identifier = str(uuid4())
        agents.bind_session(self.root, launched['id'], identifier)
        destination = templates.create(self.root / 'subverses/review', 'review', workdir=self.work)
        shutil.copytree(self.root / 'harnesses/fake', destination / 'harnesses/fake')
        pane = launched['runtime']['tmux']['pane']
        self.retain_wrapper(launched, directory)
        relocate.move_agent(self.root, launched['id'], destination)
        self.assertNotIn(pane, self.backend.snapshot()[0])
        self.assertEqual(lifecycle.listing(destination)[0]['live'], 'dormant')
        resumed = lifecycle.start(destination, resume=launched['id'], socket_path=self.socket)
        self.assertEqual(resumed['native_session_id'], identifier)
        moved = agents.directory(destination, launched['id'])
        until(lambda: agents.runtime(moved).get('pid'))
        pane = resumed['runtime']['tmux']['pane']
        self.retain_wrapper(resumed, moved)
        removed = relocate.remove_agent(destination, launched['id'])
        self.assertNotIn(pane, self.backend.snapshot()[0])
        self.assertEqual(read(Path(removed['trash']) / 'properties.json')['native_session_id'], identifier)

    def test_native_cancellation_failure_retains_terminal_and_reports_alive(self):
        launched, directory = self.spawn()
        with patch.object(native, 'stop', side_effect=RuntimeError('native daemon unreachable')), \
                patch.object(Tmux, 'signal_child') as signal_child:
            with self.assertRaisesRegex(RuntimeError, 'native daemon unreachable'):
                lifecycle.kill(self.root, launched['id'])
        signal_child.assert_not_called()
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['live'], 'alive')
        self.assertIn('terminal retained', row['runtime']['error'])
        self.assertIn(launched['runtime']['tmux']['pane'], self.backend.snapshot()[0])

    def test_foreign_pid_is_never_signalled_and_launch_can_retry(self):
        launched, directory = self.spawn()
        facts = agents.runtime(directory)
        agents.update_runtime(directory, facts['launch_id'], pid=os.getpid())
        with self.assertRaisesRegex(ValueError, 'no longer belongs'):
            lifecycle.kill(self.root, launched['id'])
        self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'ending')
        agents.update_runtime(directory, facts['launch_id'], pid=facts['pid'])
        lifecycle.kill(self.root, launched['id'])
        self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'dormant')

    def test_native_process_ignoring_term_is_killed_before_wrapper_removal(self):
        script = self.root / 'harnesses/fake/worker.py'
        script.write_text('import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n' + script.read_text())
        launched, directory = self.spawn()
        start = time.monotonic()
        lifecycle.kill(self.root, launched['id'])
        self.assertGreaterEqual(time.monotonic() - start, 2)
        facts = agents.runtime(directory)
        self.assertEqual(facts['exit_code'], -signal.SIGKILL)
        self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'dormant')

    def test_resume_race_never_tears_down_replacement_launch(self):
        launched, directory = self.spawn()
        original_signal = Tmux.signal_child
        replacement = []

        def signal_then_resume(backend, properties, facts, signum):
            original_signal(backend, properties, facts, signum)
            until(lambda: lifecycle.listing(self.root)[0]['live'] == 'stopped')
            replacement.append(lifecycle.start(self.root, resume=launched['id'], socket_path=self.socket))

        with patch.object(Tmux, 'signal_child', signal_then_resume), self.assertRaisesRegex(ValueError, 'Outdated launch'):
            lifecycle.kill(self.root, launched['id'])
        row = lifecycle.listing(self.root)[0]
        self.assertEqual(row['live'], 'alive')
        self.assertEqual(row['runtime']['launch_id'], replacement[0]['runtime']['launch_id'])
        self.assertNotIn('error', row['runtime'])
