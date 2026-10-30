"""A helper must not change the terminal file description shared with a native TUI."""
import ast
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import unittest

from isolation import isolate


PROBE = r'''
import fcntl, json, os, sys, time
from multiverse.terminal.tmux import Tmux

fd = os.dup(0)  # Codex's terminal probe duplicates stdin, then makes that description nonblocking.
original = fcntl.fcntl(fd, fcntl.F_GETFL)
fcntl.fcntl(fd, fcntl.F_SETFL, original | os.O_NONBLOCK)
try:
    server = Tmux(sys.argv[1])
    pid, epoch = server.run('display-message', '-p', '#{pid}\t#{start_time}').split('\t')
    Tmux(sys.argv[1], identity={'pid': int(pid), 'epoch': int(epoch)}).run(*json.loads(sys.argv[2]))
except RuntimeError:
    pass  # Failed tmux commands must be equally safe.
nonblocking = bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK)
print(json.dumps({'nonblocking': nonblocking}), flush=True)
deadline = time.monotonic() + .25
try:
    # A deadline checked before read cannot help if another process made the fd blocking.
    while time.monotonic() < deadline:
        os.read(fd, 256)
except BlockingIOError:
    pass
print('read completed', flush=True)
'''


class StdinOwnershipTest(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_core_subprocesses_explicitly_choose_whether_they_own_the_terminal(self):
        """Every helper subprocess in src/ sets stdin explicitly, only the runner and attach inherit it, and tmux
        commands, failing or not, leave a native terminal probe nonblocking; fragile because any new
        subprocess.run silently inherits the TUI's terminal and strands Codex's terminal probe (0b7aaef)."""
        source = Path(__file__).resolve().parents[1] / 'src' / 'multiverse'
        interactive = {('runtime/runner.py', 'Popen'), ('tools/attach.py', 'run')}
        inherited = set()
        for path in source.rglob('*.py'):
            for call in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == 'subprocess'
                        and call.func.attr in ('run', 'Popen', 'check_call', 'check_output')):
                    continue
                location = (str(path.relative_to(source)), call.func.attr)
                with self.subTest(path=location[0], line=call.lineno):
                    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
                    self.assertIn('stdin', keywords, 'a helper must not implicitly inherit the native terminal')
                    if isinstance(keywords['stdin'], ast.Constant) and keywords['stdin'].value is None:
                        self.assertIn(location, interactive, 'only native UI and interactive attach own the terminal')
                        inherited.add(location)
        self.assertEqual(inherited, interactive)

        if not shutil.which('tmux'):
            self.skipTest('tmux is required for the terminal probe')
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix='mv2-stdin-', dir='/tmp'))
        socket = str(Path(temporary) / 'tmux.sock')
        tmux = ['tmux', '-f', '/dev/null', '-S', socket]
        subprocess.run([*tmux, 'new-session', '-d', '-s', 'probe', 'sleep', '60'],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=5)
        self.addCleanup(lambda: subprocess.run([*tmux, 'kill-server'], stdin=subprocess.DEVNULL,
                                               capture_output=True, timeout=5))
        commands = [
            ['capture-pane', '-p', '-t', 'probe'],
            ['display-message', '-p', '-t', 'probe', '#{cursor_flag}'],
            ['list-panes', '-a'],
            ['capture-pane', '-p', '-t', 'missing'],
            ['display-message', '-Z'],
            ['list-panes', '-t', 'missing'],
        ]
        environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        for command in commands:
            with self.subTest(command=command):
                master, slave = pty.openpty()
                process = None
                try:
                    process = subprocess.Popen([sys.executable, '-c', PROBE, socket, json.dumps(command)],
                                               stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               bufsize=0, env=environment)
                    self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'probe did not start')
                    facts = json.loads(process.stdout.readline())
                    blocked = False
                    try:
                        output, errors = process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        blocked = True
                        os.write(master, b'release\n')  # Unblock only our disposable terminal, then reap.
                        output, errors = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 0, errors)
                    self.assertFalse(blocked, 'native read blocked past its deadline after a helper ran')
                    self.assertTrue(facts['nonblocking'], 'tmux changed the native terminal to blocking mode')
                    self.assertIn(b'read completed', output)
                finally:
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.communicate()
                    os.close(master)
                    os.close(slave)
