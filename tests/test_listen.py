"""A background listener never consumes messages or survives its launch."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from dataclasses import asdict
import fcntl
import io
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from isolation import track
from test_multiverse import BaseTest, until
from multiverse import cli, store
from multiverse.harnesses.types import AgentIdentitySpec, NativeEndpoint, NativeServiceHandle, ServiceIdentity
from multiverse.runtime import agent_identity
from multiverse.runtime.process import process_started
from multiverse.tools import listen
from multiverse.universe import mailbox


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
from multiverse.tools import listen
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
                    with patch.object(listen.time, 'sleep', side_effect=end) as slept:
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
        """Native sends and inbox readers can hold locks; waiting must still time out or end with its original launch."""
        spec = AgentIdentitySpec('fixture-v1', 'FIXTURE_THREAD', ('FIXTURE_HOME',))
        service = NativeServiceHandle(ServiceIdentity('fixture', {'FIXTURE_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', str(self.work / 'service.sock')))
        properties = {**self.properties, 'native_service_identity': asdict(service.identity)}
        properties['agent_identity'] = agent_identity.record(spec, properties, service)
        store.write(self.directory / 'properties.json', properties)
        env = {**os.environ, 'MV2_IDENTITY_MODE': spec.mode, 'FIXTURE_THREAD': self.native, 'FIXTURE_HOME': str(self.work)}
        env.pop('MV2_LAUNCH_ID', None)
        for lock_name in ('.launch.lock', '.inbox.lock'):
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
