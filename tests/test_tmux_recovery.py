"""A lost tmux address is not evidence that its recorded processes exited."""
import os
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import signal
import subprocess
import sys
import threading
from unittest.mock import patch

from isolation import track
from multiverse.runtime import lifecycle, native
from multiverse.runtime.process import process_started
from multiverse.harnesses.types import NativeResult
from test_message_delivery import DeliveryFixture
from multiverse.store import read, write, uid
from multiverse.universe.mailbox import message_record
from multiverse.terminal import recovery
from multiverse.terminal.tmux import Tmux
from multiverse.tools import send
from test_multiverse import BaseTest, TerminalBase, until


class SocketRecoveryTest(TerminalBase):
    def setUp(self):
        super().setUp()
        folder = self.work / 'terminal'
        folder.mkdir(mode=0o700)
        self.socket = str(folder / 'tmux.sock')
        self.backend = Tmux(self.socket)
        track(tmux_socket=self.socket)

    def test_a_missing_socket_and_folder_do_not_hide_a_live_agent(self):
        """A vanished socket must reconnect to the verified original tmux process, not hide a live agent or create another server."""
        result, directory = self.spawn()
        until(lambda: read(directory / 'runtime.json').get('runner_started'))
        server = int(self.backend.run('display-message', '-p', '#{pid}'))
        path = Path(self.socket)
        def listed():
            rows = lifecycle.listing(self.root, only=directory.name)
            self.assertEqual(rows[0]['live'], 'alive', rows)
            self.assertNotIn('damaged', rows[0])

        def resume():
            with self.assertRaisesRegex(ValueError, 'running'):
                lifecycle.start(self.root, resume='tester', socket_path=self.socket)

        def simultaneous():
            signals = []
            original = os.kill
            def signal_once(pid, number):
                if number == signal.SIGUSR1:
                    signals.append(pid)
                return original(pid, number)
            with patch.object(recovery.os, 'kill', side_effect=signal_once), ThreadPoolExecutor(4) as pool:
                futures = [pool.submit(listed) for _ in range(4)]
                for future in futures:
                    future.result(timeout=15)
            self.assertEqual(signals, [server])

        with self.subTest('a healthy listing does not wait behind another creator'), \
                patch.object(recovery, 'serialized', side_effect=AssertionError('healthy observation took the recovery lock')):
            listed()
        with self.subTest('joining a healthy server does not wait behind another creator'), \
                patch.object(recovery, 'serialized', side_effect=AssertionError('healthy join took the recovery lock')):
            extra = self.backend.create('healthy-join', self.work, [sys.executable, '-c', 'import time; time.sleep(60)'])
            self.backend._run('kill-pane', '-t', extra['pane'])
        with self.subTest('a socket lost between healthy probe and join recovers the same server'):
            original_run = self.backend._run
            removed = []
            def disappear(*args):
                if args[:2] == ('-N', 'new-session') and not removed:
                    path.unlink()
                    removed.append(True)
                return original_run(*args)
            try:
                with patch.object(self.backend, '_run', side_effect=disappear):
                    extra = self.backend.create('recovered-join', self.work, [sys.executable, '-c', 'import time; time.sleep(60)'])
                self.assertEqual(removed, [True], 'An optimistic join must use tmux -N, which cannot create another server')
                self.assertEqual(int(original_run('display-message', '-p', '#{pid}')), server)
                original_run('kill-pane', '-t', extra['pane'])
            finally:
                if not path.exists():
                    os.kill(server, signal.SIGUSR1)
                    until(path.exists)

        for known, vanished in ((True, False), (False, False), (True, True)):
            with self.subTest(old_tmux_without_join_only=True, known=known, vanished=vanished):
                if not known:
                    recovery.registry(path.resolve()).unlink()
                serial, original_run = recovery.serialized, self.backend._run
                held, fallback, removed = [], [], []
                @contextmanager
                def locked(socket):
                    with serial(socket):
                        held.append(True)
                        try:
                            yield
                        finally:
                            held.pop()
                def old_tmux(*args):
                    if args[0] == '-N':
                        raise RuntimeError('tmux: unknown option -- N')
                    if args[0] in ('new-session', 'if-shell'):
                        self.assertTrue(held, 'Old tmux must join under the recovery lock')
                        fallback.append(args[0])
                        if vanished and not removed:
                            path.unlink()
                            removed.append(True)
                    return original_run(*args)
                second = None
                try:
                    with patch.object(recovery, 'serialized', side_effect=locked), \
                            patch.object(self.backend, '_run', side_effect=old_tmux):
                        extra = self.backend.create('old-tmux', self.work, [sys.executable, '-c', 'import time; time.sleep(60)'])
                    second = int(original_run('display-message', '-p', '#{pid}'))
                    self.assertEqual(second, server, 'The old-tmux join started a second server after an external unlink')
                    self.assertTrue(fallback)
                    self.assertEqual(set(fallback), {'if-shell'})
                    original_run('kill-pane', '-t', extra['pane'])
                finally:
                    if second is not None and second != server:
                        original_run('kill-server')
                    if not path.exists():
                        os.kill(server, signal.SIGUSR1)
                        until(path.exists)
        with self.subTest('old-tmux command words are literal, including quotes and shell syntax'):
            cwd = self.work / "a'; kill-server; ' $HOME ~ # back\\slash"
            cwd.mkdir()
            captured = self.work / 'literal.json'
            words = ["a'; kill-server; '", '$HOME', '~', '#comment', 'a\\b', 'a\tb', '', '`touch WRONG`']
            script = 'import json,os,sys,time; open(sys.argv[1],"w").write(json.dumps([os.getcwd(),sys.argv[2:]])); time.sleep(60)'
            original_run = self.backend._run
            def legacy(*args):
                if args[0] == '-N':
                    raise RuntimeError('tmux: unknown option -- N')
                return original_run(*args)
            with patch.object(self.backend, '_run', side_effect=legacy):
                extra = self.backend.create('quoted-join', cwd, [sys.executable, '-c', script, str(captured), *words])
                until(lambda: captured.exists() and captured.stat().st_size)
                self.assertEqual(json.loads(captured.read_text()), [str(cwd), words])
                with self.assertRaisesRegex(RuntimeError, 'duplicate session'):
                    self.backend.create('quoted-join', cwd, ['/stand-in'])
                for word in ('line\nbreak', 'line\rbreak', 'trailing;', ';'):
                    with self.subTest(refused_literal=repr(word)), self.assertRaisesRegex(ValueError, 'tmux 3.2 or newer'):
                        self.backend.create('unrepresentable', cwd, ['/stand-in', word])
            original_run('kill-pane', '-t', extra['pane'])
            self.assertFalse((cwd / 'WRONG').exists())

        for fast in (False, True):
            with self.subTest('a proven post-creation conflict discards only the new session', fast=fast):
                original_run, connected, observe = self.backend._run, recovery.connected, recovery.observe
                created = []
                def joined(*args):
                    result = original_run(*args)
                    if 'new-session' in args[:2]:
                        created.append(result.split('\t')[1])
                    return result
                def checked(*args, **kwargs):
                    if created:
                        raise recovery._conflict(path, [('saved', {'pid': server}), ('answer', {'pid': server + 1})])
                    return connected(*args, **kwargs)
                with patch.object(self.backend, '_run', side_effect=joined), \
                        patch.object(recovery, 'healthy', wraps=recovery.healthy if fast else lambda *a: None), \
                        patch.object(recovery, 'connected', side_effect=checked), \
                        patch.object(recovery, 'observe', side_effect=lambda *a, **kw: checked(*a, **kw) if created else observe(*a, **kw)):
                    with self.assertRaisesRegex(recovery.ServerConflict, 'conflicting server identities'):
                        self.backend.create('conflicted-join', self.work, [sys.executable, '-c', 'import time; time.sleep(60)'])
                self.assertEqual(len(created), 1)
                self.assertNotIn(created[0], original_run('list-sessions', '-F', '#{session_id}').splitlines())
                self.assertEqual(int(original_run('display-message', '-p', '#{pid}')), server)
                self.assertIn(result['runtime']['tmux']['session'], original_run('list-sessions', '-F', '#{session_id}').splitlines())
        with self.subTest('conflict cleanup never removes a session on another server identity'):
            extra = self.backend.create('guarded-cleanup', self.work, [sys.executable, '-c', 'import time; time.sleep(60)'])
            epoch = self.backend._run('display-message', '-p', '#{start_time}')
            identity = [self.socket, extra['session'], extra['pane'], str(server), epoch]
            for pid, started in ((str(server + 1), epoch), (str(server), str(int(epoch) + 1))):
                self.backend._discard_created([*identity[:3], pid, started])
                self.assertIn(extra['session'], self.backend._run('list-sessions', '-F', '#{session_id}').splitlines())
            self.backend._discard_created(identity)
            self.assertNotIn(extra['session'], self.backend._run('list-sessions', '-F', '#{session_id}').splitlines())

        with self.subTest(unrelated_join_error=True):
            original_run = self.backend._run
            def duplicate(*args):
                if args[0] == '-N':
                    raise RuntimeError('duplicate session: existing')
                return original_run(*args)
            with patch.object(self.backend, '_run', side_effect=duplicate), \
                    patch.object(recovery, 'serialized', side_effect=AssertionError('Unrelated failure retried')):
                with self.assertRaisesRegex(RuntimeError, 'duplicate session'):
                    self.backend.create('duplicate', self.work, ['/stand-in'])

        with self.subTest('a healthy legacy pane proves its server without process queries'):
            facts = read(directory / 'runtime.json')
            legacy = {**facts, 'tmux': {key: value for key, value in facts['tmux'].items() if key != 'server'}}
            with patch.object(recovery, 'process_snapshot', side_effect=AssertionError('Healthy legacy pane inspected a process')):
                panes, error = self.backend.snapshot(facts=legacy)
            self.assertIsNone(error)
            self.assertIn(legacy['tmux']['pane'], panes)

        with self.subTest('an unreadable live legacy runner never permits a second server'):
            until(lambda: read(directory / 'runtime.json').get('pid_started'))
            saved = read(directory / 'runtime.json')
            legacy = {**saved, 'tmux': {key: value for key, value in saved['tmux'].items() if key != 'server'}}
            write(directory / 'runtime.json', legacy)
            recovery.registry(path.resolve()).unlink()
            path.unlink()
            observed = recovery.process_snapshot
            def unreadable(pid):
                return None if pid == legacy['runner_pid'] else observed(pid)
            refused = None
            second = None
            try:
                with patch.object(recovery, 'process_snapshot', side_effect=unreadable), \
                        patch.object(recovery.os, 'kill', wraps=os.kill) as signals:
                    _, error = self.backend.snapshot(facts=legacy)
                    try:
                        lifecycle.start(self.root, name='unreadable-new', harness='fake', socket_path=self.socket)
                    except recovery.Unreachable as caught:
                        refused = caught
                    else:
                        second = int(self.backend._run('display-message', '-p', '#{pid}'))
                    self.assertFalse(any(call.args[1] == signal.SIGUSR1 for call in signals.call_args_list))
                self.assertIsNone(second, 'Unknown runner inspection allowed a duplicate server')
                self.assertIsNotNone(error)
                self.assertIsNotNone(refused)
                self.assertIn(str(legacy['runner_pid']), str(refused))
            finally:
                if second is not None and second != server:
                    self.backend._run('kill-server')
                if not path.exists():
                    os.kill(server, signal.SIGUSR1)
                    until(path.exists)
                write(directory / 'runtime.json', saved)
                self.backend.snapshot(facts=saved)

        with self.subTest('a file blocking the socket directory is named and left untouched'):
            path.unlink()
            path.parent.rmdir()
            path.parent.write_text('keep this obstruction')
            try:
                _, error = self.backend.snapshot(facts=read(directory / 'runtime.json'))
                self.assertIn('regular file', str(error))
                self.assertIn(str(path.parent), str(error))
                self.assertIn(f'PID {server}', str(error))
                self.assertIn('Move it aside', str(error))
                self.assertIn('next mv2 command', str(error))
                self.assertEqual(path.parent.read_text(), 'keep this obstruction')
                lines = []
                result = send.run(self.root, agent='tester', text='save behind obstruction', _present=lines.append)
                self.assertTrue(result['saved'])
                self.assertFalse(result['ok'])
                self.assertIn('Move it aside', lines[0])
                self.assertIn(str(path.parent), lines[0])
                self.assertIn('mv2 deliver tester --retry', lines[0])
                self.assertNotIn('resume', lines[0])
                self.assertNotIn('mv2 kill', lines[0])
            finally:
                path.parent.unlink()
                path.parent.mkdir(mode=0o700)
                os.kill(server, signal.SIGUSR1)
                until(path.exists)

        actions = [('list', listed), ('environment', lambda: self.backend.environment('PATH')),
                   ('attach', lambda: lifecycle.terminal_action(self.root, 'tester', 'attach')),
                   ('send', lambda: send.run(self.root, agent='tester', text='recover before delivery', delivery='steer')),
                   ('spawn', lambda: lifecycle.start(self.root, name='another', harness='fake', socket_path=self.socket)),
                   ('resume', resume), ('simultaneous callers', simultaneous), ('legacy runner parent', listed)]
        for label, action in actions:
            with self.subTest(label=label):
                if label == 'legacy runner parent':
                    facts = read(directory / 'runtime.json')
                    facts['tmux'].pop('server')
                    write(directory / 'runtime.json', facts)
                    recovery.registry(path.resolve()).unlink()
                try:
                    path.unlink()
                    path.parent.rmdir()
                    action()
                    self.assertEqual(int(self.backend.run('display-message', '-p', '#{pid}')), server)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                finally:
                    # Restore this test's own still-running server even on the
                    # old code, so teardown ends its runner and child normally.
                    path.parent.mkdir(mode=0o700, exist_ok=True)
                    if not path.exists():
                        os.kill(server, signal.SIGUSR1)
                        until(path.exists)

    def test_an_answering_server_is_never_displaced_by_recovery(self):
        """A cleaner and a later tmux can leave two servers; reconnecting the old one must not steal the new one's live address."""
        _, directory = self.spawn()
        until(lambda: read(directory / 'runtime.json').get('runner_started'))
        old = int(self.backend.run('display-message', '-p', '#{pid}'))
        Path(self.socket).unlink()
        # Deliberately bypass mv2's protected creation to reproduce a second
        # server started by older code or by hand, on this fixture's path only.
        subprocess.run([*self.backend.prefix, 'new-session', '-d', '-s', 'other-server',
                        sys.executable, '-c', 'import time; time.sleep(60)'], check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)
        other = int(self.backend._run('display-message', '-p', '#{pid}'))
        self.assertNotEqual(old, other)
        try:
            with patch.object(recovery.os, 'kill', wraps=os.kill) as signals:
                panes, error = self.backend.snapshot(facts=read(directory / 'runtime.json'))
                self.assertEqual(panes, {})
                self.assertIn(str(old), error)
                self.assertIn(str(other), error)
                self.assertIn('No signal sent', error)
                with self.assertRaisesRegex(RuntimeError, 'No signal sent'):
                    lifecycle.start(self.root, name='refused', harness='fake', socket_path=self.socket)
                self.assertFalse(any(call.args[1] == signal.SIGUSR1 for call in signals.call_args_list))
            lines = []
            result = send.run(self.root, agent='tester', text='save behind conflict', _present=lines.append)
            self.assertTrue(result['saved'])
            self.assertFalse(result['ok'])
            self.assertIn(f'PID {old}', lines[0])
            self.assertIn(f'PID {other}', lines[0])
            self.assertIn('No signal sent', lines[0])
            self.assertIn('mv2 deliver tester --retry', lines[0])
            self.assertNotIn('resume', lines[0])
            self.assertNotIn('mv2 kill', lines[0])
            self.assertEqual(int(self.backend._run('display-message', '-p', '#{pid}')), other)
            original = read(directory / 'runtime.json')
            for legacy in (True, False):
                with self.subTest(registry_warmed_by_replacement=True, legacy=legacy):
                    facts = {**original, 'tmux': {key: value for key, value in original['tmux'].items()
                                                if not legacy or key != 'server'}}
                    write(directory / 'runtime.json', facts)
                    recovery.registry(recovery.address(self.backend)).unlink()
                    _, error = self.backend.snapshot()  # A different observer learns the replacement.
                    self.assertIsNone(error)
                    self.assertEqual(read(recovery.registry(recovery.address(self.backend)))['pid'], other)
                    with patch.object(recovery.os, 'kill', wraps=os.kill) as signals:
                        _, error = self.backend.snapshot(facts=facts)
                        self.assertIn(str(old), error)
                        self.assertIn(str(other), error)
                        with self.assertRaisesRegex(recovery.Unreachable, 'conflicting server identities'):
                            self.backend.ensure(self.root)
                        self.assertFalse(any(call.args[1] == signal.SIGUSR1 for call in signals.call_args_list))
            write(directory / 'runtime.json', original)
        finally:
            self.backend._run('kill-server')
            os.kill(old, signal.SIGUSR1)
            def restored():
                try:
                    return self.backend._run('display-message', '-p', '#{pid}') == str(old)
                except RuntimeError:
                    return False
            until(restored)


class RecoveryProofTest(BaseTest):
    def test_only_a_current_tmux_identity_authorizes_a_recovery_signal(self):
        """SIGUSR1 kills unrelated processes, so reuse, unreadable identity and conflicting proofs must never authorize it."""
        backend = Tmux(str(self.work / 'address' / 'tmux.sock'))
        socket = recovery.address(backend)
        token = 'Thu Oct 29 20:00:00 2026'
        later = 'Thu Oct 29 20:00:01 2026'
        proof = {'pid': 12345, 'started': token, 'socket': str(socket), 'epoch': 100}
        answer = ('panes', {'pid': 12345, 'epoch': 100})
        for case in ('verified', 'unreadable', 'wrong executable', 'reused PID', 'changed before signal',
                     'registry conflict', 'new answering server', 'different PID with unreadable start', 'healthy fast path', 'same PID new epoch',
                     'old tmux without epoch', 'stopped old record'):
            with self.subTest(case=case):
                write(recovery.registry(socket), proof)
                facts = {'tmux': {'server': proof}}
                observations = (token, 'S')
                probes = [None, None, answer]
                command = 'tmux: server'
                if case == 'unreadable':
                    observations = None
                elif case == 'wrong executable':
                    command = '/bin/sleep'
                elif case == 'reused PID':
                    observations = (later, 'S')
                elif case == 'registry conflict':
                    facts = {'tmux': {'server': {**proof, 'pid': 99999}}}
                elif case == 'different PID with unreadable start':
                    observations = None
                    probes = [('panes', {'pid': 99999, 'epoch': 101})]
                elif case == 'new answering server':
                    probes = [('panes', {'pid': 99999, 'epoch': 101})]
                elif case == 'healthy fast path':
                    probes = [answer]
                elif case == 'same PID new epoch':
                    observations = (later, 'S')
                    probes = [('panes', {'pid': proof['pid'], 'epoch': 101})]
                elif case == 'stopped old record':
                    facts = {'state': 'stopped', 'tmux': {'server': {**proof, 'pid': 99999}}}
                    probes = [answer]
                elif case == 'old tmux without epoch':
                    probes = [('panes', {'pid': proof['pid'], 'epoch': None})]
                with patch.object(recovery, 'process_snapshot', return_value=observations) as snapshot, \
                        patch.object(recovery, 'process_command', return_value=command), \
                        patch.object(recovery.os, 'kill') as signals, \
                        patch.object(recovery, '_probe', side_effect=probes):
                    if case == 'changed before signal':
                        snapshot.side_effect = [(token, 'S'), (later, 'S')]
                    if case in ('unreadable', 'wrong executable', 'changed before signal', 'registry conflict', 'new answering server', 'different PID with unreadable start'):
                        with self.assertRaises(recovery.Unreachable) as caught:
                            recovery.connected(backend, facts=facts)
                        if case in ('registry conflict', 'new answering server', 'different PID with unreadable start'):
                            self.assertIn('12345', str(caught.exception))
                            self.assertIn('99999', str(caught.exception))
                            self.assertNotIn('kill -USR1', str(caught.exception))
                        else:
                            self.assertIn(str(socket), str(caught.exception))
                            self.assertIn('PID 12345', str(caught.exception))
                    else:
                        result = recovery.connected(backend, facts=facts)
                        self.assertEqual(result[0] if result is not None else None, None if case == 'reused PID' else 'panes')
                    signalled = [call.args for call in signals.call_args_list if call.args[1] == signal.SIGUSR1]
                    self.assertEqual(signalled, [(12345, signal.SIGUSR1)] if case == 'verified' else [])
                    if case in ('healthy fast path', 'stopped old record'):
                        snapshot.assert_not_called()
                    elif case in ('same PID new epoch', 'old tmux without epoch', 'stopped old record'):
                        self.assertTrue(snapshot.called)
                        self.assertEqual(read(recovery.registry(socket))['started'], observations[0])
        # A responsive channel needs no signal authority. A failed OS lookup
        # must neither reject a new pane nor borrow a dead server's identity.
        for unavailable in ('snapshot', 'executable'):
            for stale in (False, True):
                with self.subTest(answering=True, unavailable=unavailable, stale=stale):
                    recovery.registry(socket).unlink(missing_ok=True)
                    if stale:
                        write(recovery.registry(socket), {**proof, 'pid': 99999})
                    created = []
                    def create(*args):
                        created.append(True)
                        return f'{socket}\t$1\t%1\t12345\t100'
                    with patch.object(recovery, '_probe', side_effect=lambda *args:
                            answer if created else ('', {'pid': 99999, 'epoch': 100}) if stale else None), \
                            patch.object(recovery, 'process_snapshot', return_value=None if unavailable == 'snapshot' else (token, 'S')), \
                            patch.object(recovery, 'process_command', return_value=None if unavailable == 'executable' else 'tmux'), \
                            patch.object(recovery.os, 'kill', side_effect=ProcessLookupError) as signals, \
                            patch.object(backend, '_run', side_effect=create):
                        terminal = backend.create('new', self.work, ['/stand-in'])
                    self.assertEqual(terminal['pane'], '%1')
                    self.assertIsNone(terminal['server'])
                    self.assertFalse(any(call.args[1] == signal.SIGUSR1 for call in signals.call_args_list))

        for gone in (False, True):
            with self.subTest(answer_disappears=gone):
                recovery.registry(socket).unlink(missing_ok=True)
                with patch.object(recovery, '_probe', side_effect=[answer, None if gone else answer]), \
                        patch.object(recovery, 'process_snapshot', return_value=None), \
                        patch.object(recovery.os, 'kill', side_effect=ProcessLookupError if gone else None):
                    found = recovery.connected(backend)
                self.assertEqual(found, None if gone else ('panes', None))
        write(recovery.registry(socket), {**proof, 'epoch': None})
        with self.subTest(known_answer_unreadable=True), \
                patch.object(recovery, '_probe', return_value=('panes', {**answer[1], 'epoch': None})), \
                patch.object(recovery, 'process_snapshot', return_value=None), \
                patch.object(recovery.os, 'kill'):
            self.assertEqual(recovery.connected(backend), ('panes', None))

        with self.subTest(second_answer_has_different_pid=True), \
                patch.object(recovery, '_probe', side_effect=[('panes', {**answer[1], 'epoch': None}),
                    ('other panes', {'pid': 99999, 'epoch': None})]), \
                patch.object(recovery, 'process_snapshot', return_value=None), \
                patch.object(recovery.os, 'kill') as signals:
            with self.assertRaisesRegex(recovery.Unreachable, 'conflicting server identities'):
                recovery.connected(backend)
            self.assertFalse(any(call.args[1] == signal.SIGUSR1 for call in signals.call_args_list))

        for fast in (False, True):
            for problem in (recovery.Unreachable('proof changed'), OSError('process unreadable'),
                            subprocess.TimeoutExpired('tmux', 10)):
                with self.subTest(post_creation_proof=type(problem).__name__, fast=fast), \
                        patch.object(recovery, 'healthy', return_value=('panes', proof) if fast else None), \
                        patch.object(recovery, 'observe', side_effect=problem), \
                        patch.object(recovery, 'connected', side_effect=[None, problem]), \
                        patch.object(backend, '_run', return_value=f'{socket}\t$1\t%1\t12345\t100'):
                    terminal = backend.create('created', self.work, ['/stand-in'])
                    self.assertEqual(terminal['pane'], '%1')
                    self.assertIsNone(terminal['server'])

        for ended in ('stopped', 'dormant', 'failed'):
            with self.subTest(ended_legacy_runner_pid_reused=ended):
                recovery.registry(socket).unlink(missing_ok=True)
                directory = self.root / 'state/agents' / uid()
                facts = {'state': ended, 'launch_id': uid(), 'runner_pid': os.getpid(),
                         'tmux': {'socket': str(socket), 'session': '$1', 'pane': '%1'}}
                write(directory / 'runtime.json', facts)
                try:
                    with patch.object(recovery, '_probe', return_value=None), \
                            patch.object(recovery, 'process_snapshot', side_effect=AssertionError('Ended runner inspected')):
                        backend.ensure(self.root)
                finally:
                    (directory / 'runtime.json').unlink()

        launch = uid()
        own = {'state': 'running', 'launch_id': launch, 'tmux': {'pane': '%1', 'session': '$1'}}
        row = ['%1', '$1', '0', 'agent', 'universe', launch, '123']
        for case in ('own', 'dead', 'other pane', 'other session', 'other launch', 'malformed launch'):
            with self.subTest(pane_server_proof=case):
                selected, facts = list(row), own
                if case == 'dead':
                    selected[2] = '1'
                elif case == 'other pane':
                    selected[0] = '%2'
                elif case == 'other session':
                    selected[1] = '$2'
                elif case == 'other launch':
                    selected[5] = uid()
                elif case == 'malformed launch':
                    selected[5] = 'not-a-launch'
                    facts = {**own, 'launch_id': selected[5]}
                self.assertEqual(recovery._matches_answer(('\t'.join(selected), answer[1]), proof, None, facts),
                                 case in ('own', 'dead'))

        # The legacy parent proof must verify both runner observations too.
        for state, saved in (('Zs', None), ('X', 'damaged'), ('S', None), (None, None)):
            with self.subTest(legacy_runner_state=state, saved_start=saved), \
                    patch.object(recovery.os, 'kill') as signals, \
                    patch.object(recovery, 'process_snapshot', return_value=(token, state) if state else None), \
                    patch.object(recovery, 'process_parent', side_effect=AssertionError('Unproven runner parent inspected')):
                legacy = {'state': 'running', 'runner_pid': 23456, 'runner_started': saved}
                if state in ('Zs', 'X'):
                    self.assertIsNone(recovery.runner_server(legacy))
                else:
                    with self.assertRaises(recovery.Unreachable):
                        recovery.runner_server(legacy)
                self.assertEqual([call.args for call in signals.call_args_list], [(23456, 0)])
        facts = {'runner_pid': 23456, 'runner_started': token}
        for case in ('verified parent', 'runner reused', 'parent changed', 'orphan', 'non-tmux parent'):
            with self.subTest(case=case), patch.object(recovery.os, 'kill'), \
                    patch.object(recovery, 'process_command', return_value='launchd' if case == 'non-tmux parent' else 'tmux'), \
                    patch.object(recovery, 'process_snapshot', return_value=(token, 'S')), \
                    patch.object(recovery, 'process_parent', side_effect=[12345, 99999] if case == 'parent changed' else [1] if case == 'orphan' else [12345, 12345]), \
                    patch.object(recovery, 'start_matches', side_effect=[False] if case == 'runner reused' else [True, True, True]):
                if case == 'parent changed':
                    with self.assertRaises(recovery.Unreachable):
                        recovery.runner_server(facts)
                else:
                    found = recovery.runner_server(facts)
                    self.assertEqual(found, {'pid': 12345, 'started': token} if case == 'verified parent' else None)

    def test_recovery_preserves_existing_directories_and_never_waits_on_record_fifos(self):
        """Recovery must not change user folders, hang on non-regular records, or admit concurrent owners after pruning a lock."""
        backend = Tmux(str(self.work / 'tmux.sock'))
        path = recovery.address(backend)
        self.work.chmod(0o750)
        proof = {'pid': 12345, 'started': 'Thu Oct 29 20:00:00 2026', 'socket': str(path)}
        write(recovery.registry(path), proof)
        with patch.object(recovery, '_state', return_value='live'), \
                patch.object(recovery, '_probe', side_effect=[None, None, ('panes', {'pid': 12345, 'epoch': 100})]), \
                patch.object(recovery, 'process_snapshot', return_value=(proof['started'], 'S')), \
                patch.object(recovery, 'process_command', return_value='tmux'), patch.object(recovery.os, 'kill'):
            self.assertEqual(recovery.connected(backend)[0], 'panes')
        self.assertEqual(self.work.stat().st_mode & 0o777, 0o750)
        for kind in ('record', 'lock'):
            with self.subTest(kind=kind):
                target = recovery.registry(path).with_suffix('.lock') if kind == 'lock' else recovery.registry(path)
                target.unlink(missing_ok=True)
                os.mkfifo(target)
                try:
                    with self.assertRaises((recovery.Unreachable, OSError)):
                        if kind == 'record':
                            recovery._saved(path)
                        else:
                            with recovery.serialized(path):
                                self.fail('FIFO became a lock')
                finally:
                    target.unlink()
        for answers in (False, True):
            with self.subTest(corrupt_registry=True, answering_server=answers):
                write(recovery.registry(path), {'damaged': True})
                with patch.object(recovery, '_probe', return_value=('', {'pid': 12345, 'epoch': 100}) if answers else None), \
                        patch.object(recovery, 'process_snapshot', return_value=(proof['started'], 'S')), \
                        patch.object(recovery, 'process_command', return_value='tmux'):
                    if answers:
                        self.assertEqual(recovery.connected(backend)[0], '')
                        self.assertEqual(read(recovery.registry(path))['pid'], 12345)
                    else:
                        with self.assertRaises(recovery.Unreachable):
                            recovery.connected(backend)
        # Startup contention is allowed beyond the old five-second recovery
        # window, but a stuck owner still has a bounded, actionable failure.
        with self.subTest(lock_released_after_six_seconds=True), \
                patch.object(recovery.time, 'monotonic', side_effect=[0, 6]), \
                patch.object(recovery.time, 'sleep'), \
                patch.object(recovery.fcntl, 'flock', side_effect=[BlockingIOError, None, None]):
            with recovery.serialized(path):
                pass
        with self.subTest(lock_never_released=True), \
                patch.object(recovery.time, 'monotonic', side_effect=[0, 31]), \
                patch.object(recovery.fcntl, 'flock', side_effect=BlockingIOError):
            with self.assertRaisesRegex(recovery.Unreachable, 'Another mv2 process is starting or recovering'):
                with recovery.serialized(path):
                    self.fail('A stuck lock was acquired')
        # Empty older servers still answer identity separately from list-panes.
        with patch.object(backend, '_run', side_effect=[RuntimeError('no current target'), '12345\t100']):
            self.assertEqual(recovery._probe(backend, path), ('', {'pid': 12345, 'epoch': 100}))
        for state in ('gone', 'live', 'unknown', 'damaged', 'absent'):
            with self.subTest(prune_touched_entry=state):
                record, held = recovery.registry(path), recovery.registry(path).with_suffix('.lock')
                record.unlink(missing_ok=True)
                if state != 'absent':
                    write(record, {'damaged': True} if state == 'damaged' else proof)
                with patch.object(recovery, '_state', return_value=state):
                    with recovery.serialized(path):
                        self.assertTrue(held.exists())
                        self.assertEqual(record.exists(), state != 'absent', 'Never prune before the section can publish')
                self.assertEqual(record.exists(), state not in ('gone', 'absent'))
                self.assertEqual(held.exists(), state not in ('gone', 'absent'))

        for has_record in (False, True):
            with self.subTest(detached_lock_waiter=True, has_record=has_record):
                record, held = recovery.registry(path), recovery.registry(path).with_suffix('.lock')
                record.unlink(missing_ok=True)
                if has_record:
                    write(record, proof)
                opened, release, reopened, entered = (threading.Event() for _ in range(4))
                errors, opens = [], []
                regular = recovery._regular
                @contextmanager
                def delayed_open(target, **kwargs):
                    with regular(target, **kwargs) as stream:
                        if target == held and threading.current_thread() is waiter:
                            opens.append(True)
                            if len(opens) == 1:
                                opened.set()
                                if not release.wait(5):
                                    raise TimeoutError('Old-inode waiter was not released')
                            else:
                                reopened.set()
                        yield stream
                def wait_on_old_inode():
                    try:
                        with recovery.serialized(path):
                            entered.set()
                    except Exception as error:
                        errors.append(error)
                waiter = threading.Thread(target=wait_on_old_inode)
                try:
                    with patch.object(recovery, '_regular', side_effect=delayed_open), \
                            patch.object(recovery, '_state', return_value='gone'):
                        with recovery.serialized(path):
                            waiter.start()
                            self.assertTrue(opened.wait(5))
                        self.assertFalse(record.exists())
                        self.assertFalse(held.exists())
                        with recovery.serialized(path):
                            release.set()
                            self.assertTrue(reopened.wait(5), 'A detached lock waiter must reopen the current inode')
                            self.assertFalse(entered.is_set(), 'Old and new lock inodes admitted concurrent owners')
                        waiter.join(5)
                        self.assertFalse(waiter.is_alive())
                        self.assertEqual(errors, [])
                        self.assertTrue(entered.is_set())
                        self.assertFalse(held.exists())
                finally:
                    release.set()
                    if waiter.ident is not None:
                        waiter.join(5)



class NativeSocketNoticeTest(DeliveryFixture):
    def test_a_lost_local_socket_names_the_live_process_without_preventing_a_working_fallback(self):
        """Missing frontend sockets need a resume remedy, but cannot disable another native delivery path or trigger replay."""
        for case in ('direct', 'proxy', 'stale launch', 'PID reused', 'present', 'fallback succeeds', 'uncertain',
                     'starting', 'startup survived', 'startup failed', 'relay ready', 'relay lost after binding',
                     'relay lost before binding', 'relay lost without binding contract'):
            with self.subTest(case=case):
                self.recipient()
                properties = read(self.directory / 'properties.json')
                facts = {**read(self.directory / 'runtime.json'), 'pid': os.getpid(),
                         'pid_started': process_started(os.getpid()), 'startup_delivery': 'ready'}
                if case.startswith(('starting', 'startup', 'relay')):
                    facts['startup_delivery'] = 'failed' if case == 'startup failed' else 'waiting'
                    facts['startup_checked'] = case == 'startup survived'
                if case.startswith('relay'):
                    facts['native_relay'] = {'state': 'ready' if case == 'relay ready' else 'lost'}
                    facts['attachment'] = {'readiness': 'process' if case == 'relay lost without binding contract' else 'binding'}
                    if case == 'relay lost before binding':
                        facts['state'] = 'starting'
                local = str(self.work / 'missing-local.sock')
                shared = str(self.work / 'missing-daemon.sock')
                env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': self.agent,
                       'MV2_AGENT_DIR': str(self.directory), 'MV2_LAUNCH_ID': self.launch,
                       'MV2_CONTROL_SOCKET': local}
                request = {'env': env, 'control_endpoint': {'transport': 'unix-lines', 'address': local}}
                if case == 'proxy':
                    request.update(rpc_proxy={'socket_path': local},
                                   control_endpoint={'transport': 'unix-websocket', 'address': shared})
                if case == 'stale launch':
                    env['MV2_LAUNCH_ID'] = 'another-launch'
                if case == 'PID reused':
                    facts['pid_started'] = 'Thu Oct 29 20:00:00 2026'
                if case == 'present':
                    Path(local).touch()
                write(self.directory / 'runtime.json', facts)
                write(self.directory / 'launches' / self.launch / 'request.json', request)
                expected = case not in ('stale launch', 'PID reused', 'present', 'starting', 'startup survived',
                                       'startup failed', 'relay lost before binding', 'relay lost without binding contract')
                try:
                    problem = native.connection_problem(self.root, properties, facts)
                    if expected:
                        self.assertIn(local, problem)
                        self.assertIn(f'PID {os.getpid()}', problem)
                        self.assertIn('mv2 resume', problem)
                        self.assertNotIn(shared, problem)
                        self.assertNotIn('damaged', problem)
                    else:
                        self.assertIsNone(problem)
                    if expected:
                        lost = {**facts, 'native_relay': {'state': 'lost', 'reason': 'Relay disappeared'}}
                        write(self.directory / 'runtime.json', lost)
                        with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                            row = lifecycle.listing(self.root, only=self.agent)[0]
                        self.assertIn(local, row['connection_error'])
                        write(self.directory / 'runtime.json', facts)
                    self.behavior = NativeResult('native_queued' if case == 'fallback succeeds' else
                                                 'unknown' if case == 'uncertain' else 'unavailable',
                                                 reason='Adapter result')
                    # Startup queue behavior has its own table; here ask the
                    # adapter directly enough to check only the loss diagnostic.
                    facts['startup_delivery'] = None if facts['startup_delivery'] == 'waiting' else facts['startup_delivery']
                    write(self.directory / 'runtime.json', facts)
                    result = self.send('one message', delivery='steer')
                    self.assertEqual(len(self.calls), 1)
                    _, tracking = message_record(self.directory, result['id'])
                    receipt = tracking['attempts'][-1]
                    if expected and case != 'fallback succeeds':
                        self.assertIn(local, receipt['reason'])
                    else:
                        self.assertNotIn(local, receipt.get('reason', ''))
                    if case in ('fallback succeeds', 'uncertain'):
                        native.deliver(self.root, self.agent, result['id'], retry=True)
                        self.assertEqual(len(self.calls), 1)
                finally:
                    Path(local).unlink(missing_ok=True)
