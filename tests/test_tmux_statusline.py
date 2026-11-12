"""Agent-local status rendering and real isolated tmux wrappers; no native daemon."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import signal
import sys
import termios
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.universe import templates
from multiverse.terminal import statusline
from multiverse.universe.mailbox import append_message, inbox, update_message
from multiverse.store import lock, read, write, now as store_now
from multiverse.universe import records
from multiverse.tools import inbox as inbox_tool
from test_multiverse import BaseTest, TerminalBase


class StatusRendererTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.launch = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.properties = {'id': self.agent, 'universe_id': read(self.root / 'universe.json')['id'],
                           'name': 'worker', 'harness': 'codex', 'phase': 'working', 'summary': 'Read the patch',
                           'native_session_id': str(uuid4())}
        self.runtime = {'launch_id': self.launch, 'state': 'running', 'status': 'waiting',
                        'usage': {'source': 'codex-app-server', 'native_session_id': self.properties['native_session_id'],
                                  'context': {'tokens': 82000, 'window': 200000, 'percent': 37}}}
        self.save()

    def save(self):
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', self.runtime)

    def render(self, family='codex'):
        return statusline.render(self.root, self.agent, self.launch, family)

    def configure(self, **tmux):
        write(self.root / 'statusline.json', {'schema': 1, 'tmux': tmux})

    def test_subverse_identity_and_control_and_format_escaping(self):
        """Agent names, summaries and subverse names reach the status line with # doubled and escape, OSC and bidi
        characters removed; fragile because tmux expands #() and #{} and one new output path can skip the single
        replace, so an agent's summary silently runs a shell command."""
        child = templates.create(self.root / 'subverses/review', 'review', workdir=self.work)
        self.root = child
        self.directory = child / 'state/agents' / self.agent
        self.properties.update(universe_id=read(child / 'universe.json')['id'],
                               name='\x1b[31mworker\x1b[0m\n#{pane_id}',
                               summary='\x1b]0;bad-title\x07A summary #[fg=red] #(touch BAD)\u202e followed by more words ' * 2)
        self.save()
        self.configure(items={'summary': True})
        rendered = self.render()
        self.assertIn('worker ##{pane_id} (review)', rendered)
        self.assertIn('##[fg=red] ##(touch BAD)', rendered)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)
        self.assertNotIn('bad-title', rendered)
        self.assertEqual(statusline.clean('hello ' + 'world ' * 20, 20), 'hello world world…')

    def test_the_status_script_only_reads(self):
        """The standalone renderer must share core's unread result without consuming messages or importing UI/native code."""
        append_message(self.directory, 'already read', {})
        inbox(self.directory)
        append_message(self.directory, 'steer', {}, 'steer')
        append_message(self.directory, 'follow-up', {}, 'follow-up')
        append_message(self.directory, 'manual', {})
        with (self.directory / 'inbox.jsonl').open('ab') as out:
            out.write(b'{"id":"legacy","text":"old format"}\n{"partial":')
        paths = {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        code = ('import runpy,sys; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__"); '
                'assert not any(n.startswith(("multiverse.universe.agents","multiverse.cli","textual","websockets")) for n in sys.modules)')
        result = subprocess.run([sys.executable, '-S', '-c', code, str(Path(statusline.__file__).resolve()),
                                 str(self.root), self.agent, self.launch, 'codex'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), self.render())
        self.assertIn('inbox 2', result.stdout)
        self.assertEqual(statusline.unread(self.directory), len(inbox(self.directory, peek=True)['messages']))
        self.assertEqual(statusline.unread(self.directory), 2)
        self.assertEqual(paths, {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()})

    def test_upgrade_copy_count_follows_confirmation_and_consumption_even_behind_cursor(self):
        """The shared status peek must preserve upgrade copies and their confirmation/read marks behind the cursor."""
        message = append_message(self.directory, 'native notice', {}, 'follow-up', inbox_only_text='original')
        self.assertEqual(statusline.unread(self.directory), 1)
        write(self.directory / 'inbox.cursor', (self.directory / 'inbox.jsonl').stat().st_size)
        self.assertEqual(statusline.unread(self.directory), 1)
        update_message(self.directory, message['id'], native_receipt={'transcript_confirmed': True})
        self.assertEqual(statusline.unread(self.directory), 0)
        update_message(self.directory, message['id'], native_receipt={}, transcript_confirmed_at=store_now())
        self.assertEqual(statusline.unread(self.directory), 0)
        update_message(self.directory, message['id'], native_receipt={}, transcript_confirmed_at=None,
                       submission_state='failed')
        self.assertEqual(statusline.unread(self.directory), 1)
        self.assertEqual([m['text'] for m in inbox(self.directory)['messages']], ['original'])
        self.assertEqual(statusline.unread(self.directory), 0)
        states = read(self.directory / 'inbox-state.json')
        for field in ('transcript_confirmed_at', 'inbox_copy_read_at'):
            for damaged in ({'bad': True}, True, 7, 'not a timestamp', '2026-11-07', '2026-11-07T12:00:00'):
                with self.subTest(field=field, damaged=damaged):
                    state = {**states[message['id']], 'inbox_copy_read_at': None, 'transcript_confirmed_at': None,
                             field: damaged}
                    write(self.directory / 'inbox-state.json', {message['id']: state})
                    self.assertEqual(statusline.unread(self.directory), 1)
        # A damaged ledger cannot be mistaken for no confirmation/read marker.
        for data in ('not json', '[]'):
            (self.directory / 'inbox-state.json').write_text(data)
            self.assertIsNone(statusline.unread(self.directory))

    def test_large_inboxes_share_the_dashboard_count_without_a_byte_or_count_cap(self):
        """Healthy history and ledgers beyond both former caps must still show zero or the exact unread count."""
        from multiverse.ui.backend import LocalBackend
        log, ledger = self.directory / 'inbox.jsonl', self.directory / 'inbox-state.json'
        history = {'id': 'consumed', 'schema': 2, 'delivery': 'inbox-only', 'text': 'x' * (1024 * 1024 + 1)}
        data = (json.dumps(history) + '\n').encode()
        for ledger_mebibytes in (2, 9):
            with self.subTest(ledger_mebibytes=ledger_mebibytes):
                log.write_bytes(data)
                write(self.directory / 'inbox.cursor', len(data))
                # Inbox ledgers grow with messages and attempts; they must not inherit the runtime's 8 MiB record cap.
                write(ledger, {'consumed': {'reason': 'x' * (ledger_mebibytes * 1024 * 1024)}})
                (self.directory / '.inbox.lock').touch()
                backend, row = LocalBackend(), {'universe': str(self.root), 'id': self.agent}
                self.assertGreater(log.stat().st_size, 1024 * 1024)
                self.assertGreater(ledger.stat().st_size, 1024 * 1024)
                before = {path.name: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
                self.assertEqual(backend.unread_count(row), 0)
                self.assertNotIn('inbox', self.render())
                self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir() if path.is_file()})
                with log.open('ab') as stream:
                    for index in range(73):
                        stream.write((json.dumps({'id': str(index), 'schema': 2, 'delivery': 'inbox-only',
                                                 'text': 'unread'}) + '\n').encode())
                self.assertEqual(backend.unread_count(row), 73)
                self.assertIn('inbox 73', self.render())
                self.assertNotIn('inbox 73+', self.render())
                self.assertEqual(len(inbox_tool.unread(self.root, self.agent, blocking=False)), 73)
                # Identity and runtime still have their own small-record bound.
                if ledger_mebibytes == 9:
                    with self.assertRaises(records.RecordError):
                        records.runtime(ledger)
                from multiverse.universe import mailbox
                with patch.object(mailbox, 'unread', wraps=mailbox.unread) as shared:
                    self.assertEqual(statusline._read_unread(self.directory), 73)
                shared.assert_called_once_with(self.directory, blocking=False)

    def test_missing_history_is_unknown_with_saved_evidence_but_a_fresh_inbox_needs_no_lock(self):
        """A lost message log must not hide unread damage, while an inbox with no saved data stays zero without a write."""
        self.assertEqual(statusline.unread(self.directory), 0)
        self.assertFalse((self.directory / '.inbox.lock').exists())
        from multiverse import store
        from multiverse.universe import mailbox
        observed = store.missing_records(self.directory / name for name in mailbox.INBOX_EVIDENCE)
        with patch.object(store.time, 'sleep', side_effect=AssertionError('directory absence must not wait')):
            self.assertIsNone(mailbox.inbox_problem(self.directory, observed_missing=observed))
        # A lock alone can come from doctor or a peek, and is not message/read-state evidence.
        (self.directory / '.inbox.lock').touch()
        self.assertEqual(statusline.unread(self.directory), 0)
        append_message(self.directory, 'consumed', {})
        inbox(self.directory)
        log = self.directory / 'inbox.jsonl'
        saved = log.read_bytes()
        log.unlink()
        with self.assertRaises(ValueError):
            inbox_tool.unread(self.root, self.agent, blocking=False)
        self.assertIsNone(statusline.unread(self.directory))
        self.assertIn('inbox ?', self.render())
        log.write_bytes(saved)
        self.assertEqual(statusline.unread(self.directory), 0)
        log.unlink()
        (self.directory / 'inbox.cursor').unlink()
        for name, value in (('inbox.cursor', 0), ('inbox.cursor', 1), ('inbox-read.json', {'one': store_now()}),
                            ('inbox-state.json', {'one': {}}), ('native-delivery.json', {'one': []})):
            with self.subTest(evidence=name):
                write(self.directory / name, value)
                self.assertIsNone(statusline.unread(self.directory))
                with self.assertRaisesRegex(ValueError, 'inbox history is missing'):
                    inbox_tool.run(self.root, agent=self.agent, peek=True)
                from multiverse.ui.backend import LocalBackend
                self.assertIsNone(LocalBackend().unread_count({'universe': str(self.root), 'id': self.agent}))
                (self.directory / name).unlink()
        from multiverse.ui.backend import LocalBackend
        backend = LocalBackend()
        row = {'universe': str(self.root), 'id': self.agent}
        self.assertEqual(backend.unread_count(row), 0)
        (self.directory / 'inbox-unsubmitted').mkdir()
        self.assertIsNone(backend.unread_count(row))
        self.assertIsNone(statusline.unread(self.directory))
        with self.assertRaisesRegex(ValueError, 'inbox history is missing'):
            inbox_tool.run(self.root, agent=self.agent, peek=True)

    def test_unread_never_waits_for_a_lock_and_stops_a_slow_helper(self):
        """A held inbox lock or slow reader must not stall tmux or leave a reader process behind."""
        append_message(self.directory, 'pending', {})
        started = time.monotonic()
        with lock(self.directory / '.inbox.lock'):
            self.assertIsNone(statusline.unread(self.directory))
        self.assertLess(time.monotonic() - started, statusline.INBOX_TIMEOUT)
        self.assertEqual(statusline.unread(self.directory), 1)
        self.enterContext(patch.object(statusline.threading, 'active_count', return_value=1))
        children, original = [], os.fork
        def fork():
            child = original()
            if child:
                children.append(child)
            return child
        started = time.monotonic()
        with patch.object(statusline, '_read_unread', side_effect=lambda path: time.sleep(10)), \
                patch.object(statusline, 'INBOX_TIMEOUT', .08), patch.object(os, 'fork', side_effect=fork):
            self.assertIsNone(statusline.unread(self.directory))
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(len(children), 1)
        with self.assertRaises(ChildProcessError):
            os.waitpid(children[0], os.WNOHANG)
        self.assertEqual(len(inbox(self.directory, peek=True)['messages']), 1)
        for reaper in (signal.SIG_IGN, lambda *_: None):
            with self.subTest(reaper=reaper), patch.object(signal, 'getsignal', return_value=reaper), \
                    patch.object(os, 'fork', side_effect=AssertionError('incompatible child reaper')):
                self.assertIsNone(statusline.unread(self.directory))
        with patch.object(statusline.threading, 'active_count', return_value=2), \
                patch.object(os, 'fork', side_effect=AssertionError('fork from a threaded caller')):
            self.assertEqual(statusline.unread(self.directory), 1)
        opened, closed = [], []
        real_pipe, real_close = os.pipe, os.close
        def pipe():
            pair = real_pipe()
            opened.extend(pair)
            return pair
        def close(descriptor):
            closed.append(descriptor)
            real_close(descriptor)
        with patch.object(os, 'pipe', side_effect=pipe), patch.object(os, 'close', side_effect=close), \
                patch.object(os, 'fork', return_value=12345), \
                patch.object(statusline.select, 'select', return_value=([], [], [])), \
                patch.object(os, 'waitpid', side_effect=ChildProcessError('ownership lost')), \
                patch.object(os, 'kill', side_effect=AssertionError('no signal without child ownership')):
            self.assertIsNone(statusline.unread(self.directory))
        self.assertCountEqual(opened, closed)





class StatusWrapperTest(TerminalBase):
    def attach(self, session):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 20, 130, 0, 0))
        env = {**os.environ, 'TERM': 'xterm-256color'}
        env.pop('TMUX', None)
        process = subprocess.Popen([*self.backend.prefix, 'attach-session', '-t', session],
                                   stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            os.close(master)
        self.addCleanup(cleanup)
        return master, process

    def output_until(self, master, text, timeout=6):
        data = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                data += os.read(master, 65536)
                if text.encode() in data:
                    return data
        self.fail(f'Status text {text!r} missing from PTY: {data!r}')

    def test_real_tmux_keeps_agent_text_and_odd_root_paths_literal(self):
        """Through real tmux and a PTY, a summary with #[..] #(touch BAD) #{pane_id} and a root path with quotes and #()
        stay literal and no BAD file appears; fragile because the result depends on tmux's own format parser and
        re-expansion of option values, which a unit test cannot prove."""
        root = templates.create(self.work / "odd ' ) #(touch BAD) #{pane_id}" / '.multiverse', 'odd', workdir=self.work)
        # Use the existing fixture's stand-in harness without creating another native process type.
        shutil.copytree(self.root / 'harnesses/fake', root / 'harnesses/fake')
        write(root / 'statusline.json', {'tmux': {'refresh_seconds': 1}})
        result = lifecycle.start(root, 'display', harness='fake', socket_path=self.socket, wait=True, wait_timeout=5)
        directory = agents.directory(root, result['id'])
        master, _ = self.attach(result['runtime']['tmux']['session'])
        self.output_until(master, 'display · fake · phase starting')
        agents.report(root, result['id'], 'blocked', 'Waiting')
        append_message(directory, 'hello', {})
        self.output_until(master, 'inbox 1')
        # Configuration is read on each refresh, not just when a wrapper starts.
        write(root / 'statusline.json', {'tmux': {'refresh_seconds': 1,
                                                  'items': {'summary': True, 'phase': False, 'inbox': False}}})
        agents.report(root, result['id'], 'done', 'LITERAL #[fg=red] #(touch BAD) #{pane_id}\x1b[31m')
        output = self.output_until(master, 'LITERAL #[fg=red] #(touch BAD) #{pane_id}')
        self.assertNotIn(b'\x1b[31m', output)
        self.assertFalse((self.work / 'BAD').exists())
        self.assertEqual(len(inbox(directory, peek=True)['messages']), 1)


if __name__ == '__main__':
    unittest.main()
