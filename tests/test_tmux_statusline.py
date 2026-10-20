"""Agent-local status rendering and real isolated tmux wrappers; no native daemon."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import unittest
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.universe import templates
from multiverse.terminal import statusline
from multiverse.universe.mailbox import append_message, inbox
from multiverse.store import read, write
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
        """The script tmux runs every few seconds, run as python -S, imports no cli, harness or textual module, writes
        no agent file (inbox.cursor included) and counts unread exactly like inbox(peek=True); fragile because its
        unread count is a stdlib-only copy of inbox reading that drifts, and a write silently consumes messages."""
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
