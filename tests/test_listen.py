"""A background listener never consumes messages or survives its launch."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import fcntl
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from isolation import track
from test_multiverse import BaseTest, until
from multiverse import cli, store
from multiverse.harnesses.types import AgentIdentitySpec, NativeEndpoint, NativeServiceHandle, ServiceIdentity
from multiverse.runtime import agent_identity, focus_note, native
from multiverse.runtime.process import process_started
from multiverse.tools import listen, send
from multiverse.universe import focus, mailbox


class ListenTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.launch, self.native = store.uid(), store.uid(), store.uid()
        self.directory = self.root / 'state/agents' / self.agent
        self.properties = {'id': self.agent, 'name': 'listener', 'harness': 'pi', 'workspace': str(self.work),
                           'universe_id': store.read(self.root / 'universe.json')['id'],
                           'native_session_id': self.native}
        store.write(self.directory / 'properties.json', self.properties)
        self.facts = {'launch_id': self.launch, 'state': 'running', 'pid': os.getpid(),
                      'pid_started': process_started(os.getpid()), 'runner_pid': os.getpid(),
                      'runner_started': process_started(os.getpid())}
        store.write(self.directory / 'runtime.json', self.facts)
        (self.directory / '.launch.lock').touch()
        self.enterContext(patch.dict(os.environ, {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': self.agent,
            'MV2_AGENT_DIR': str(self.directory), 'MV2_LAUNCH_ID': self.launch}))

    def command(self, *options):
        output, error, code = io.StringIO(), io.StringIO(), 0
        with redirect_stdout(output), redirect_stderr(error), patch.object(sys, 'argv', ['mv2', 'listen', *options]):
            try:
                cli.main()
            except SystemExit as ended:
                code = ended.code
        return code, output.getvalue(), error.getvalue()

    def message(self, delivery='inbox-only'):
        return mailbox.append_message(self.directory, 'quiet work', {'name': 'sender'}, delivery=delivery)

    def note(self, directory):
        """The real turn-end note, saved and committed, with only its native submission left out."""
        with patch.object(native, 'send_or_wake', return_value={'state': 'native_queued', 'ok': True}) as submitted:
            focus_note.run(directory, focus.last(directory)['id'], wait=0)
        self.assertEqual(submitted.call_count, 1)
        self.assertEqual(mailbox.inbox(directory, all_messages=True, limit=None)['messages'][-1]['id'],
                         focus.last(directory)['note'])

    def ledger(self):
        return {name: (self.directory / name).read_bytes() if (self.directory / name).exists() else None
                for name in ('inbox.cursor', 'inbox-state.json', 'native-delivery.json')}

    @contextmanager
    def listener(self, held, *, timeout=None):
        """An owned CLI, with barriers around real lock attempts and the first quiet poll."""
        attempted, polled = self.work / 'attempted', self.work / 'polled'
        attempted.unlink(missing_ok=True)
        polled.unlink(missing_ok=True)
        code = '''import fcntl,os,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from multiverse.tools import listen, send
from multiverse.tools._common import output
original=fcntl.flock
def locking(fd,operation):
 descriptor=fd.fileno() if hasattr(fd,'fileno') else fd
 if operation & fcntl.LOCK_EX and os.fstat(descriptor).st_ino==Path(sys.argv[3]).stat().st_ino:
  Path(sys.argv[4]).touch()
 return original(fd,operation)
fcntl.flock=locking
def sleeping(delay):
 Path(sys.argv[5]).touch()
 time.sleep(delay)
listen.time=SimpleNamespace(monotonic=time.monotonic,sleep=sleeping)
output(listen.run,render=listen.format_output,root=sys.argv[2],timeout=sys.argv[6] or None)
'''
        process = subprocess.Popen([sys.executable, '-I', '-c', code,
            str(Path(listen.__file__).resolve().parents[2]), str(self.root), str(held),
            str(attempted), str(polled), timeout or ''], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        track(pid=process.pid)
        try:
            yield process, attempted, polled
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

    def test_listen_notifies_only_for_unread_manual_messages_and_never_consumes_them(self):
        """A background notification must neither repeat native prompts nor mark quiet work read before the agent sees it."""
        for delivery in ('steer', 'follow-up'):
            self.message(delivery)
        before = self.ledger()
        code, output, _ = self.command('--timeout', '0')
        self.assertEqual(code, 1)
        self.assertIn('Timed out', output)
        self.assertEqual(self.ledger(), before)

        arrived = False
        def arrives(_):
            nonlocal arrived
            # The listener has no default deadline and emits nothing while waiting.
            self.assertEqual(sys.stdout.getvalue(), '')
            if not arrived:
                self.message()
                arrived = True
            else:
                time.sleep(.01)
        with patch.object(listen, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=arrives)):
            code, output, error = self.command()
        self.assertEqual((code, output, error), (0, 'inbox message from sender\n', ''))
        self.assertTrue(arrived)
        self.assertEqual(self.ledger(), before)
        # Immediate success leaves the same message unread too.
        self.assertEqual(self.command('--timeout', '0')[:2], (0, output))
        for _ in range(50):
            self.message()
        self.assertEqual(self.command()[:2], (0, '51 unread inbox messages\n'))
        self.assertEqual(self.ledger(), before)
        self.assertEqual(len(mailbox.inbox(self.directory)['messages']), 50)
        self.assertEqual(self.command()[:2], (0, 'inbox message from sender\n'))
        mailbox.inbox(self.directory)
        before = self.ledger()
        self.assertEqual(self.command('--timeout', '0')[0], 1)
        self.assertEqual(self.ledger(), before)

    def test_listen_stops_when_its_launch_ends_even_with_stable_conversation_identity(self):
        """Cached Codex identity and stale running records must not leave an old background listener watching a replacement."""
        spec = AgentIdentitySpec('fixture-v1', 'FIXTURE_THREAD', ('FIXTURE_HOME',))
        service = NativeServiceHandle(ServiceIdentity('fixture', {'FIXTURE_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', str(self.work / 'service.sock')))
        rows = [('replaced', {'launch_id': store.uid()}), ('stopped', {'state': 'stopped'}),
                ('ending', {'ending_at': store.now()}), ('exited successfully', {'exit_code': 0}),
                ('failed', {'state': 'failed'}), ('missing', None)]
        for stable in (False, True):
            properties = dict(self.properties)
            env = dict(os.environ)
            if stable:
                properties['native_service_identity'] = asdict(service.identity)
                properties['agent_identity'] = agent_identity.record(spec, properties, service)
                env.pop('MV2_LAUNCH_ID', None)
                env.update(MV2_IDENTITY_MODE=spec.mode, FIXTURE_THREAD=self.native, FIXTURE_HOME=str(self.work))
            store.write(self.directory / 'properties.json', properties)
            for name, changes in rows:
                with self.subTest(stable=stable, end=name), patch.dict(os.environ, env, clear=True):
                    store.write(self.directory / 'runtime.json', self.facts)
                    before = self.ledger()
                    def end(_):
                        if changes is None:
                            (self.directory / 'runtime.json').unlink()
                        else:
                            store.write(self.directory / 'runtime.json', {**self.facts, **changes})
                        self.message()  # An unread message never lets an ended launch succeed.
                    slept = Mock(side_effect=end)
                    with patch.object(listen, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=slept)):
                        code, output, error = self.command()
                    self.assertEqual(code, 1, (output, error))
                    self.assertIn('launch', error)
                    self.assertEqual(slept.call_count, 1)
                    self.assertEqual(self.ledger(), before)
                    mailbox.inbox(self.directory, limit=None)
        store.write(self.directory / 'properties.json', self.properties)
        for pid in ('pid', 'runner_pid'):
            with self.subTest(crashed=pid):
                # A reused PID is gone even if runtime.json still says running.
                facts = {**self.facts, 'pid_started' if pid == 'pid' else 'runner_started':
                         'Mon Jan 1 00:00:00 2001'}
                store.write(self.directory / 'runtime.json', facts)
                code, _, error = self.command('--timeout', '0')
                self.assertEqual(code, 1)
                self.assertIn('launch has ended', error)

        for pid in ('pid', 'runner_pid'):
            with self.subTest(crash_between_polls=pid):
                frontend = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])
                track(pid=frontend.pid)
                store.write(self.directory / 'runtime.json', {**self.facts, pid: frontend.pid,
                    'pid_started' if pid == 'pid' else 'runner_started': process_started(frontend.pid)})
                clock = [0.0]
                def crash_and_arrive(delay):
                    frontend.terminate()
                    frontend.wait(timeout=5)
                    self.message()
                    clock[0] += delay
                try:
                    with patch.object(listen, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=crash_and_arrive)):
                        with self.assertRaisesRegex(ValueError, 'launch has ended'):
                            listen.run(self.root)
                finally:
                    if frontend.poll() is None:
                        frontend.terminate()
                        frontend.wait(timeout=5)
                    mailbox.inbox(self.directory, limit=None)

    def test_lock_contention_never_extends_the_deadline_or_adopts_another_launch(self):
        """Native sends, inbox readers and focus changes can hold locks; waiting must still time out or end with its
        original launch."""
        spec = AgentIdentitySpec('fixture-v1', 'FIXTURE_THREAD', ('FIXTURE_HOME',))
        service = NativeServiceHandle(ServiceIdentity('fixture', {'FIXTURE_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', str(self.work / 'service.sock')))
        properties = {**self.properties, 'native_service_identity': asdict(service.identity)}
        properties['agent_identity'] = agent_identity.record(spec, properties, service)
        store.write(self.directory / 'properties.json', properties)
        env = {**os.environ, 'MV2_IDENTITY_MODE': spec.mode, 'FIXTURE_THREAD': self.native, 'FIXTURE_HOME': str(self.work)}
        env.pop('MV2_LAUNCH_ID', None)
        for lock_name in ('.launch.lock', '.inbox.lock', '.focus.lock'):
            held = self.directory / lock_name
            held.touch()
            for case in ('timeout', 'replacement', 'handoff'):
                with self.subTest(lock=lock_name, case=case), patch.dict(os.environ, env, clear=True):
                    store.write(self.directory / 'runtime.json', self.facts)
                    mailbox.inbox(self.directory, limit=None)
                    before = self.ledger()
                    with held.open('r+') as handle:
                        if case != 'replacement':
                            fcntl.flock(handle, fcntl.LOCK_EX)
                        with self.listener(held, timeout='0.2' if case == 'timeout' else None) as (child, attempted, polled):
                            def reached(path):
                                if path.exists():
                                    return True
                                if child.poll() is not None:
                                    self.fail(f'Listener exited before barrier: {child.communicate()}')
                                return False
                            if case == 'replacement':
                                until(lambda: reached(polled))
                                fcntl.flock(handle, fcntl.LOCK_EX)
                                attempted.unlink(missing_ok=True)
                                # Make the next iteration inspect the inbox too.
                                (self.directory / 'inbox.jsonl').touch()
                            until(lambda: reached(attempted))
                            if case == 'timeout' and lock_name == '.focus.lock':
                                # A busy focus lock may be a focus starting: the timeout waits for it to free.
                                freed_after = time.monotonic() + 0.5
                                until(lambda: time.monotonic() >= freed_after)
                                self.assertIsNone(child.poll())
                                fcntl.flock(handle, fcntl.LOCK_UN)
                            if case != 'timeout':
                                store.write(self.directory / 'runtime.json', {**self.facts, 'launch_id': store.uid()})
                            if case == 'handoff':
                                fcntl.flock(handle, fcntl.LOCK_UN)
                                self.message()
                            try:
                                output, error = child.communicate(timeout=2)
                            except subprocess.TimeoutExpired:
                                self.fail(f'Listener remained blocked on {lock_name} during {case}')
                            self.assertNotEqual(child.returncode, 0, output + error)
                            self.assertIn('Timed out' if case == 'timeout' else 'Listening launch', output + error)
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    self.assertEqual(self.ledger(), before)
                    mailbox.inbox(self.directory, limit=None)

    def test_a_focus_holds_the_report_until_it_ends_and_the_listener_claims_it(self):
        """listen is refused while focused, and one already running reports nothing during a focus, not even a
        timeout, then what is unread once it ends, naming the held messages and claiming the report so the turn-end
        note stays quiet, or leaving out what a note that came first announced; a break interrupts the agent while
        the user answers, or tells it twice."""
        focus.start(self.directory)
        with patch.object(listen, 'time', SimpleNamespace(monotonic=time.monotonic,
                                                           sleep=Mock(side_effect=AssertionError('listen waited while focused')))):
            code, _, error = self.command('--timeout', '0')
        self.assertEqual((code, 'end focus mode first' in error), (1, True))
        focus.end(self.directory)

        # Registration never waits: with the turn-end note's probe holding it, the listener still listens,
        # and registers on a later poll.
        probe = (self.directory / focus.LISTENING).open('a')
        fcntl.flock(probe, fcntl.LOCK_EX)
        steps = [lambda: probe.close(), lambda: (self.assertTrue(focus.listener(self.directory)), self.message())]
        # Only listen's own clock: subprocess waits on the real time.sleep for the process checks.
        with patch.object(listen, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=lambda _: steps.pop(0)())):
            self.assertEqual(self.command()[:2], (0, 'inbox message from sender\n'))
        self.assertEqual(steps, [])
        mailbox.inbox(self.directory, limit=None)

        def held():
            focus.hold(self.directory, lambda focus_id: mailbox.append_message(
                self.directory, 'stop', {'name': 'sender'}, 'inbox-only', intended='steer', focus=focus_id))
        def quiet():
            self.assertEqual(sys.stdout.getvalue(), '')
            self.assertTrue(focus.listener(self.directory))
        def note_first(directory):
            # The turn-end note gave up waiting for this held-up listener and told the agent itself.
            focus.end(directory)
            self.note(directory)
        for read_during_focus, timed, noted in ((False, False, False), (True, False, False), (False, True, False),
                                                (False, False, True)):
            with self.subTest(read_during_focus=read_during_focus, timed=timed, noted=noted):
                steps = [focus.start, lambda _: (held(), self.message()), lambda _: quiet(),
                         *([mailbox.inbox] if read_during_focus else []), note_first if noted else focus.end]
                if read_during_focus:
                    steps.append(lambda _: (quiet(), self.message()))   # nothing unread at the end: keep waiting
                clock = [0.0]
                def step(delay):
                    clock[0] += delay   # a timed listener's 0.1 s runs out during the focus
                    steps.pop(0)(self.directory)
                with patch.object(listen, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=step)):
                    code, output, _ = self.command(*(['--timeout', '0.1'] if timed else []))
                self.assertEqual(steps, [])
                latest = focus.last(self.directory)
                if read_during_focus or noted:
                    # Only the inbox-only message, after a note that came first announced the held one.
                    self.assertEqual((code, output, latest['report']),
                                     (0, 'inbox message from sender\n', 'note' if noted else None))
                else:
                    self.assertEqual((code, output, latest['report']), (0, '2 unread inbox messages (including 1 '
                                     'steer/follow-up message held during focus mode)\n', 'listener'))
                    before = len(mailbox.inbox(self.directory, all_messages=True, limit=None)['messages'])
                    self.assertIsNone(focus_note.run(self.directory, latest['id'], wait=0))
                    self.assertEqual(len(mailbox.inbox(self.directory, all_messages=True, limit=None)['messages']), before)
                self.assertFalse(focus.listener(self.directory))
                mailbox.inbox(self.directory, limit=None)

    def test_a_report_or_timeout_never_crosses_a_focus_boundary(self):
        """A focus that starts as listen prints waits for the line, a timeout never prints into a focus that began
        during its checks, a failed print claims nothing, and a focus that began as the listener started counts as
        one it waited through; each break wakes the agent during a focus, or loses or repeats the turn-end note."""
        def held():
            focus.hold(self.directory, lambda focus_id: mailbox.append_message(
                self.directory, 'stop', {'name': 'sender'}, 'inbox-only', intended='steer', focus=focus_id))
        clock = [0.0]
        def run(steps, **options):
            def step(delay):
                clock[0] += delay
                steps.pop(0)(self.directory)
            with patch.object(listen, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=step)):
                result = listen.run(self.root, **options)
            self.assertEqual(steps, [])
            return result

        # A focus that starts while the line is printed waits until it is out.
        self.message()
        waited = []
        def slow_print(result):
            starting = threading.Thread(target=focus.start, args=(self.directory,))
            starting.start()
            starting.join(0.3)
            waited.append(starting.is_alive())
            self.addCleanup(starting.join, 5)
        self.assertTrue(run([], _present=slow_print)['ok'])
        until(lambda: focus.current(self.directory))
        self.assertEqual(waited, [True])
        focus.end(self.directory)
        mailbox.inbox(self.directory, limit=None)

        # A focus that begins during the process check holds back an expired timeout until it ends.
        original, printed, checked = listen._processes, [], []
        def focus_during_check(facts):
            if not checked:
                checked.append(True)
                focus.start(self.directory)
            return original(facts)
        with patch.object(listen, '_processes', side_effect=focus_during_check):
            result = run([focus.end], timeout='0',
                         _present=lambda result: printed.append(focus.current(self.directory)))
        self.assertEqual((result['ok'], printed), (False, [None]))

        # A print that failed claims nothing, so the turn-end note still goes.
        focus.start(self.directory)
        held()
        focus.end(self.directory)
        def broken(result):
            raise BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            run([], _present=broken)
        self.assertIsNone(focus.last(self.directory)['report'])
        with patch.object(send, 'run', return_value={'ok': True}) as sent:
            focus_note.run(self.directory, focus.last(self.directory)['id'], wait=0)
        self.assertEqual(sent.call_count, 1)
        mailbox.inbox(self.directory, limit=None)

        # A focus that begins just after the listener's check: the note that came first is not repeated.
        current, begun = focus.current, []
        def begin_after_check(directory):
            found = current(directory)
            if not begun:
                begun.append(True)
                focus.start(directory)
            return found
        def note_first(directory):
            focus.end(directory)
            self.note(directory)
        lines = []
        with patch.object(focus, 'current', side_effect=begin_after_check):
            run([lambda _: held(), note_first, lambda _: self.message()],
                _present=lambda result: lines.append(result['line']))
        self.assertEqual((lines, focus.last(self.directory)['report']), (['inbox message from sender'], 'note'))
        mailbox.inbox(self.directory, limit=None)

        # A focus that ends during the process check: what it held is read before an expired timeout.
        def end_during_check(facts):
            original(facts)
            focus.end(self.directory)
        def focus_and_hold(directory):
            focus.start(directory)
            held()
            clock[0] += 2   # held up past both the deadline and the next process check
        with patch.object(listen, '_processes', side_effect=end_during_check):
            result = run([focus_and_hold, lambda _: None], timeout='0.1')   # the read, on the next poll
        self.assertEqual(result, {'ok': True, 'line': 'inbox message from sender (a steer/follow-up message held '
                                                      'during focus mode)'})
        mailbox.inbox(self.directory, limit=None)

        # After a focus, a busy launch lock lets an expired timeout finish; the held message stays unread.
        launch_lock = (self.directory / '.launch.lock').open('r+')
        self.addCleanup(launch_lock.close)
        def end_and_hold_the_launch_lock(directory):
            focus.end(directory)
            fcntl.flock(launch_lock, fcntl.LOCK_EX)   # as a native submission might, for as long as it likes
        # One more poll would find no step left: the expired listener must finish on the poll after the focus.
        result = run([focus_and_hold, end_and_hold_the_launch_lock], timeout='0.1')
        self.assertFalse(result['ok'])
        fcntl.flock(launch_lock, fcntl.LOCK_UN)
        self.assertEqual(len(mailbox.inbox(self.directory, limit=None)['messages']), 1)

        # Two listeners that waited through the same focus: the second leaves out what the first reported.
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        focus.start(self.directory)
        held()
        focus.end(self.directory)
        messages = mailbox.inbox(self.directory, limit=None, peek=True)['messages']
        with focus.locked(self.directory):
            first = listen._report(self.directory, messages, before)
            self.assertTrue(focus.claim_locked(self.directory, first[1], 'listener'))
            self.assertIsNone(listen._report(self.directory, messages, before))
            # A listen started after the focus still reports what is unread, as usual.
            self.assertEqual(listen._report(self.directory, messages, datetime.now(timezone.utc))[0]['line'],
                             'inbox message from sender (a steer/follow-up message held during focus mode)')
        mailbox.inbox(self.directory, limit=None)

        # The saved-note lookup meets a busy inbox lock: the report waits for the next poll, not for a new message.
        focus.start(self.directory)
        held()
        focus.end(self.directory)
        inbox_lock = (self.directory / '.inbox.lock').open('r+')
        self.addCleanup(inbox_lock.close)
        peek, peeks = mailbox.inbox, []
        def busy_after_the_peek(*arguments, **options):
            found = peek(*arguments, **options)
            peeks.append(found)
            if len(peeks) == 1:
                fcntl.flock(inbox_lock, fcntl.LOCK_EX)
            return found
        def free_it(delay):
            fcntl.flock(inbox_lock, fcntl.LOCK_UN)
            clock[0] += delay
        with patch.object(mailbox, 'inbox', side_effect=busy_after_the_peek), \
                patch.object(listen, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=free_it)):
            result = listen.run(self.root, timeout='1')
        self.assertEqual(result, {'ok': True, 'line': 'inbox message from sender (a steer/follow-up message held '
                                                      'during focus mode)'})
        self.assertEqual(len(peeks), 2)
        mailbox.inbox(self.directory, limit=None)

        # A quiet listener still checks, on every poll, that its conversation is the agent's.
        spec = AgentIdentitySpec('fixture-v1', 'FIXTURE_THREAD', ('FIXTURE_HOME',))
        service = NativeServiceHandle(ServiceIdentity('fixture', {'FIXTURE_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', str(self.work / 'service.sock')))
        properties = {**self.properties, 'native_service_identity': asdict(service.identity)}
        properties['agent_identity'] = agent_identity.record(spec, properties, service)
        store.write(self.directory / 'properties.json', properties)
        env = {**os.environ, 'MV2_IDENTITY_MODE': spec.mode, 'FIXTURE_THREAD': self.native, 'FIXTURE_HOME': str(self.work)}
        env.pop('MV2_LAUNCH_ID', None)
        def switch_conversation(directory):
            moved = store.uid()
            store.write(directory / 'properties.json', {**properties, 'native_session_id': moved,
                        'agent_identity': {**properties['agent_identity'], 'native_id': moved}})
        with patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(ValueError, 'conversation does not match'):
            run([switch_conversation], timeout='0.5')
