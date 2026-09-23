"""Claude-only PTY relay: preserve interactivity, persist evidence before input.

No raw terminal output is stored. Only fixed, allowlisted failure descriptions
survive the pane, so prompts, credentials and tool output cannot enter the log.
"""
import errno
import fcntl
import struct
import os
import pty
import re
import select
import signal
import termios
import time
import tty

from .agents import update_runtime
from .harnesses.claude import history
from .store import now, write


class Diagnostics:
    def __init__(self, session_id):
        self.session_id = session_id
        self.tail = ''
        self.messages = []
        self.seen = set()

    def feed(self, data):
        self.tail = (self.tail + data.decode('utf-8', errors='replace'))[-8192:]
        plain = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.tail).lower()
        signatures = (
            ('no conversation found with session id:', 'No conversation found with session ID: ' + self.session_id),
            ('invalid api key', 'Claude authentication failed: invalid API key'),
            ('not logged in', 'Claude authentication required'),
            ('session id is already in use', 'Claude session ID is already in use'),
            ('unknown option', 'Claude rejected an unknown command-line option'),
            ('permission denied', 'Claude launch failed: permission denied'),
            ('enoent', 'Claude launch failed: required file or executable not found'),
        )
        before = list(self.messages)
        for needle, message in signatures:
            if needle in plain and message not in self.seen:
                self.seen.add(message)
                self.messages.append(message)
        self.messages = self.messages[-4:]
        return self.messages != before


def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def run(argv, env, launch, evidence):
    agent_dir = launch.parent.parent
    evidence = {**evidence, 'closed': False}
    update_runtime(agent_dir, launch.name, claude_session=evidence, runner_pid=os.getpid())
    diagnostics = Diagnostics(evidence['session_id'])
    child = None
    master = None
    old_terminal = termios.tcgetattr(0) if os.isatty(0) else None
    previous_resize = None
    status = None

    def persist(code=None):
        report = {'launch_id': launch.name, 'messages': diagnostics.messages, 'at': now()}
        if code is not None:
            report['exit_code'] = code
        write(launch / 'diagnostics.json', report)
        update_runtime(agent_dir, launch.name, diagnostics=report)

    def observe_history():
        if not evidence['never_initialized']:
            return
        try:
            history.location(evidence['environment'], evidence['workspace'], evidence['session_id']).lstat()
        except FileNotFoundError:
            if history.fingerprint(evidence['environment']) == evidence['storage_fingerprint']:
                return
        except OSError:
            pass
        # Any transcript artifact or loss of storage visibility ends the proof,
        # including history created by provider-side activity without terminal input.
        evidence['never_initialized'] = False
        update_runtime(agent_dir, launch.name, claude_session=evidence)

    try:
        child, master = pty.fork()
        if child == 0:
            try:
                signal.signal(signal.SIGHUP, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.execvpe(argv[0], argv, env)
            except Exception:
                os.write(2, b'Claude launch failed: ENOENT or executable unavailable\n')
                os._exit(127)
        update_runtime(agent_dir, launch.name, pid=child)

        def resize(*_):
            if old_terminal is not None:
                size = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0))
                fcntl.ioctl(master, termios.TIOCSWINSZ, size)
        previous_resize = signal.signal(signal.SIGWINCH, resize)
        resize()
        if old_terminal is not None:
            tty.setraw(0)
        inputs = [master, 0]
        while True:
            observe_history()
            ready, _, _ = select.select(inputs, [], [], 0.1)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    data = b''
                if not data:
                    break
                if diagnostics.feed(data):
                    persist()
                write_all(1, data)
            if 0 in ready:
                data = os.read(0, 65536)
                if not data:
                    inputs.remove(0)
                else:
                    # Durable BEFORE forwarding even one byte to Claude. Terminal
                    # replies also count as uncertain; false negatives are safe.
                    if evidence['never_initialized']:
                        evidence['never_initialized'] = False
                        update_runtime(agent_dir, launch.name, claude_session=evidence)
                    write_all(master, data)
        _, status = os.waitpid(child, 0)
        code = os.waitstatus_to_exitcode(status)
        persist(code)
        return code
    finally:
        if child and status is None:
            try:
                os.killpg(child, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    waited, status = os.waitpid(child, os.WNOHANG)
                    if waited:
                        break
                    time.sleep(0.02)
                else:
                    os.killpg(child, signal.SIGKILL)
                    _, status = os.waitpid(child, 0)
            except ProcessLookupError:
                pass
        if master is not None:
            os.close(master)
        if previous_resize is not None:
            signal.signal(signal.SIGWINCH, previous_resize)
        if old_terminal is not None:
            try:
                termios.tcsetattr(0, termios.TCSANOW, old_terminal)
            except termios.error:
                pass  # pane already removed
        # Close evidence only after the provider has exited, avoiding stop/resume races.
        evidence['closed'] = True
        try:
            observe_history()
            persist(os.waitstatus_to_exitcode(status) if status is not None else None)
            update_runtime(agent_dir, launch.name, claude_session=evidence)
        except ValueError:
            pass  # a stale runner must never overwrite a newer launch
