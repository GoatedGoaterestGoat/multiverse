"""Real, isolated tmux shutdown stages and exact-launch process ownership."""
import os
import signal
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse.universe import agents, records
from multiverse.runtime import lifecycle
from multiverse.runtime import native
from multiverse.store import read
from multiverse.terminal.tmux import Tmux
from test_multiverse import TerminalBase, until


class KillTest(TerminalBase):
    def start(self, name):
        launched = lifecycle.start(self.root, name, 'a task', 'fake', socket_path=self.socket)
        directory = agents.directory(self.root, launched['id'])
        until(lambda: (directory / 'seen.json').exists())
        return launched, directory

    def live(self, launched):
        return lifecycle.listing(self.root, only=launched['id'])[0]['live']

    def test_attachment_only_describes_service_and_terminal_work_ownership(self):
        """kill --attachment-only never requests native cancellation, closes the pane, and says whether the agent's
        work keeps running in a service or ends with the terminal, also for a record naming no owner; a break goes
        unnoticed because an agent trusting a wrong description stops work it meant to keep (5609703)."""
        for owner in ('service', None):
            with self.subTest(owner=owner):
                launched, directory = self.start('attachment-' + str(owner))
                facts = agents.runtime(directory)
                agents.update_runtime(directory, facts['launch_id'],
                                      attachment={'work_owner': owner} if owner else None)
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
        """kill goes native exit, ending, stopped, dormant, removing the pane only after the native process exits and
        keeping the session for resume, and a native process that exits by itself leaves no pane either; fragile
        because the order spans threads, tmux and native stop, and a break silently loses the last turn."""
        with self.subTest('kill'):
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
        with self.subTest('the native process exits by itself'):
            (self.root / 'harnesses/fake/worker.py').write_text(
                'import os, sys, time\nfrom pathlib import Path\nprint("NATIVE READY", flush=True)\n'
                "while not Path(os.environ['MV2_AGENT_DIR'], 'exit').exists(): time.sleep(.05)\nsys.exit(0)\n")
            launched = lifecycle.start(self.root, 'self-exit', 'a task', 'fake', socket_path=self.socket)
            directory = agents.directory(self.root, launched['id'])
            until(lambda: agents.runtime(directory).get('startup_checked'))  # a clean exit, not a startup failure
            (directory / 'exit').touch()
            until(lambda: agents.runtime(directory).get('state') == 'dormant')
            self.assertEqual(agents.runtime(directory)['exit_code'], 0)
            self.assertNotIn(launched['runtime']['tmux']['pane'], self.backend.snapshot()[0])

    def test_a_dead_wrapper_is_removed_without_signals_or_the_daemon(self):
        """A dead wrapper left by an older or interrupted runner is removed without signals or the native daemon, and
        resume then keeps the session; fragile because only older runners create it, and a break leaves undeletable
        panes or signals a reused pid."""
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

    def test_kill_never_controls_a_pane_or_process_it_cannot_prove_is_its_own(self):
        """kill never controls a pane tagged with another launch or signals a pid that is not its child, and both
        agents can still be stopped once the evidence is right; fragile because panes and pids get reused and every
        agent runs as the same user, so a break signals an unrelated process."""
        with self.subTest('a pane tagged with another launch'):
            launched, directory = self.start('foreign-pane')
            pane = launched['runtime']['tmux']['pane']
            self.backend.run('set-option', '-p', '-t', pane, '@mv2_launch_id', 'someone-else')
            row = lifecycle.listing(self.root, only=launched['id'])[0]
            self.assertEqual(row['live'], 'unknown')
            self.assertEqual(row['damaged']['file'], str((directory / 'runtime.json').relative_to(self.root)))
            with self.assertRaises(ValueError):
                lifecycle.terminal_action(self.root, launched['id'], 'inspect')
            # The pane was retagged, not exited: the old owned runner is still live.
            cleanup = directory / 'launches' / launched['runtime']['launch_id'] / 'cleanup.json'
            self.assertEqual(read(cleanup)['state'], 'active')
            with patch.object(Tmux, 'signal_child') as signal_child:
                with patch.object(lifecycle, 'TEARDOWN_TIMEOUT', .1), \
                        self.assertRaisesRegex(records.RecordError, 'runtime.json.*tagged terminal.*valid saved'):
                    lifecycle.terminal_action(self.root, launched['id'], 'stop')
                self.assertIn(pane, self.backend.snapshot()[0])
                self.backend.run('kill-pane', '-t', pane)
                result = lifecycle.terminal_action(self.root, launched['id'], 'stop')
            signal_child.assert_not_called()
            self.assertEqual(result['state'], 'dormant')
        with self.subTest('a pid that is not its child'):
            launched, directory = self.start('foreign-pid')
            facts = agents.runtime(directory)
            agents.update_runtime(directory, facts['launch_id'], pid=os.getpid())
            with self.assertRaisesRegex(ValueError, 'no longer belongs'):
                lifecycle.kill(self.root, launched['id'])
            self.assertEqual(self.live(launched), 'ending')
            agents.update_runtime(directory, facts['launch_id'], pid=facts['pid'])
            lifecycle.kill(self.root, launched['id'])
            self.assertEqual(self.live(launched), 'dormant')

    def test_native_stop_failure_keeps_identity_and_reports_alive(self):
        """An unreachable daemon during kill keeps a live terminal and reports it alive without signalling it, and on
        a dormant agent never loses the session id; a break goes unnoticed until a daemon is down, and then the
        conversation is lost."""
        launched, directory = self.spawn()
        identifier = str(uuid4())
        agents.bind_session(self.root, launched['id'], identifier)
        pane = launched['runtime']['tmux']['pane']
        with self.subTest('a live terminal'):
            with patch.object(native, 'stop', side_effect=RuntimeError('native daemon unreachable')), \
                    patch.object(Tmux, 'signal_child') as signal_child:
                with self.assertRaisesRegex(RuntimeError, 'native daemon unreachable'):
                    lifecycle.kill(self.root, launched['id'])
            signal_child.assert_not_called()
            row = lifecycle.listing(self.root)[0]
            self.assertEqual(row['live'], 'alive')
            self.assertIn('terminal retained', row['runtime']['error'])
            self.assertIn(pane, self.backend.snapshot()[0])
        with self.subTest('a dormant agent'):
            self.backend.run('kill-pane', '-t', pane)
            with patch.object(native, 'stop', side_effect=RuntimeError('native daemon unreachable')):
                result = lifecycle.kill(self.root, launched['id'])
            self.assertEqual(result['native_cancellation']['state'], 'unknown')
            self.assertIn('native daemon unreachable', result['native_cancellation']['reason'])
            self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'dormant')
            self.assertEqual(read(directory / 'properties.json')['native_session_id'], identifier)

    def test_native_process_ignoring_term_is_killed_before_wrapper_removal(self):
        """A native process ignoring SIGTERM is killed before its wrapper is removed; a break leaves orphan native
        processes behind removed panes, unnoticed."""
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
        """A kill racing a resume never tears down the replacement launch; fragile because it is a race, and the
        resumed agent just vanishes."""
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
