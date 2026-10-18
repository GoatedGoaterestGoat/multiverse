"""Real PTYs verify stock detach returns to the exact launching dashboard."""
import os
import pty
import select
import subprocess
import sys
import time
from uuid import uuid4

from test_multiverse import ROOT, TerminalBase, until as base_until
from multiverse.universe import agents
from multiverse.terminal.tmux import Tmux
from multiverse.store import read


PATIENCE = 30  # seconds: waits return as soon as their condition holds; a loaded runner needs the room


def until(predicate, seconds=PATIENCE):
    return base_until(predicate, seconds)


class AttachReturnTest(TerminalBase):
    def setUp(self):
        super().setUp()
        self.terminals = {}
        # Never inherit personal bindings; all sessions and keys belong to this test.
        subprocess.run([*self.backend.prefix, '-f', '/dev/null', 'new-session', '-d',
                        '-s', 'unrelated', 'sleep 120'], check=True)
        self.agent, self.agent_dir = self.spawn()
        self.target = self.agent['runtime']['tmux']
        self.native_id = str(uuid4())
        agents.bind_session(self.root, self.agent['id'], self.native_id,
                            self.agent['runtime']['launch_id'])
        self.worker_pid = until(lambda: agents.runtime(self.agent_dir).get('pid'))
        self.pane_pid = self.backend.run('display-message', '-p', '-t', self.target['pane'], '#{pane_pid}')

    def client(self, argv, inherited=None):
        master, slave = pty.openpty()
        self.terminals[master] = os.ttyname(slave)
        env = {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE')}
        env['TERM'] = 'xterm-256color'
        env.update(inherited or {})
        process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)

        def close():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)
        self.addCleanup(close)
        return master, process

    def read_until(self, master, marker):
        output = b''
        deadline = time.monotonic() + PATIENCE
        while marker not in output and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                output += os.read(master, 65536)
        self.assertIn(marker, output, repr(output[-2000:]))
        return output

    def tui_command(self):
        return [sys.executable, str(ROOT / 'bin' / 'mv2'), '-u', str(self.root), 'tui']

    def wait_exit(self, master, process):
        # Drain final redraws so the PTY's output buffer cannot block client exit.
        deadline = time.monotonic() + PATIENCE
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        return process.wait(timeout=5)

    def clients_equal(self, expected, backend=None):
        """tmux's client list settles a moment after a client exits or detaches: wait for it, then assert."""
        try:
            until(lambda: self.clients(backend) == expected)
        except AssertionError:
            pass
        self.assertEqual(self.clients(backend), expected)

    def client_count(self, expected):
        try:
            until(lambda: len(self.clients().splitlines()) == expected)
        except AssertionError:
            pass
        self.assertEqual(len(self.clients().splitlines()), expected)

    def clients(self, backend=None):
        return (backend or self.backend).run('list-clients', '-F',
                                             '#{client_session}|#{client_last_session}')

    def bindings(self, backend=None):
        return (backend or self.backend).run('list-keys', '-T', 'prefix')

    def assert_agent_alive(self):
        self.assertIn(self.target['pane'], self.backend.snapshot()[0])
        runtime = agents.runtime(self.agent_dir)
        self.assertEqual(runtime['pid'], self.worker_pid)
        self.assertEqual(runtime['state'], 'running')
        self.assertNotIn('exit_code', runtime)
        self.assertEqual(read(self.agent_dir / 'properties.json')['native_session_id'], self.native_id)
        os.kill(self.worker_pid, 0)  # Existence check only; no signal is delivered.
        self.assertEqual(self.backend.run('display-message', '-p', '-t', self.target['pane'],
                                          '#{pane_pid}|#{pane_dead}'), self.pane_pid + '|0')

    def open_agent(self, master, pane=None):
        """Press Enter on the selected agent until this exact PTY has an agent client.

        The dashboard drops an Enter that arrives while it is still finishing the previous attach and return
        (a person never notices; a test can be that quick, most of all on a loaded runner), so press it again.
        A repeat is harmless: while the client is still on the dashboard it is dropped the same way.
        The dashboard's inspector can also print FAKE READY, so confirm the client, not only the text.
        """
        expected = self.terminals[master] + '|' + (pane or self.target['pane'])
        output = b''
        for _ in range(6):
            os.write(master, b'\r')
            end = time.monotonic() + 10
            while time.monotonic() < end:
                if select.select([master], [], [], .1)[0]:
                    output += os.read(master, 65536)
                if b'FAKE READY' in output and expected in self.backend.run(
                        'list-clients', '-F', '#{client_tty}|#{pane_id}').splitlines():
                    return
        self.fail(f'No agent client for {expected} after six Enters: {output[-2000:]!r}')

    def check_host_return(self, host, name):
        dashboard = host.create(name, self.work, self.tui_command())
        pid = host.run('display-message', '-p', '-t', dashboard['pane'], '#{pane_pid}')
        before = self.bindings(host)
        master, process = self.client([*host.prefix, 'attach-session', '-t', dashboard['pane']])
        self.read_until(master, b'tester')
        self.open_agent(master)
        # Only the agent client owns the host tty: no nested client intercepts d.
        self.client_count(1)
        if host is not self.backend:
            self.clients_equal('', host)
        os.write(master, b'\x02d')
        self.read_until(master, b'tester')
        until(lambda: self.clients(host).startswith(name + '|'))
        self.assertEqual(host.run('display-message', '-p', '-t', dashboard['pane'], '#{pane_pid}'), pid)
        self.assertIsNone(process.poll())
        self.assertEqual(self.bindings(host), before)
        self.assert_agent_alive()
        # Ordinary detach on the dashboard still leaves the host.
        os.write(master, b'\x02d')
        until(lambda: self.clients(host) == '')
        self.wait_exit(master, process)
        # Reattach and open an agent again; no stale per-client return state.
        master, process = self.client([*host.prefix, 'attach-session', '-t', dashboard['pane']])
        self.read_until(master, b'tester')
        self.open_agent(master)
        os.write(master, b'\x02d')
        self.read_until(master, b'tester')
        until(lambda: self.clients(host).startswith(name + '|'))
        os.write(master, b'\x02d')
        until(lambda: self.clients(host) == '')
        self.wait_exit(master, process)

    def test_prefix_d_returns_to_the_originating_dashboard(self):
        """Prefix-d from an agent returns to the dashboard that opened it, on the same server or from a nested server
        with a quoted socket path, and a custom prefix and d binding stay the user's; fragile because it depends on
        tmux client semantics and nested prefix forwarding that vary by version."""
        with self.subTest('same server'):
            self.check_host_return(self.backend, 'dashboard')
        with self.subTest('nested server with a quoted socket path'):
            outer = Tmux(str(self.work / "outer 'quoted'.sock"))
            subprocess.run([*outer.prefix, '-f', '/dev/null', 'new-session', '-d',
                            '-s', 'unrelated', 'sleep 120'], check=True)
            self.addCleanup(outer.run, 'kill-server')
            self.check_host_return(outer, 'dashboard')
        with self.subTest('custom prefix and d binding'):
            self.backend.run('set-option', '-g', 'prefix', 'C-a')
            self.backend.run('bind-key', '-T', 'prefix', 'd', 'display-message', 'CUSTOM DETACH')
            before = self.bindings()
            dashboard = self.backend.create('custom', self.work, self.tui_command())
            master, _ = self.client([*self.backend.prefix, 'attach-session', '-t', dashboard['pane']])
            self.read_until(master, b'tester')
            self.open_agent(master)
            os.write(master, b'\x01d')
            self.read_until(master, b'CUSTOM DETACH')
            self.assertEqual(self.bindings(), before)
            self.assertTrue(self.clients().startswith('mv2-'))
            # Use the exact owned client to detach without overriding the user's binding.
            client = self.backend.run('list-clients', '-F', '#{client_name}')
            self.backend.run('detach-client', '-t', client)
            self.read_until(master, b'tester')
            until(lambda: self.clients().startswith('custom|'))

    def test_attach_never_hijacks_another_terminals_client(self):
        """Attach never takes over another terminal's tmux client, neither from a detached origin nor from a separate
        terminal that inherited TMUX and TMUX_PANE; a break goes unnoticed in single-terminal development while it
        hijacks the user's other terminal."""
        self.client([*self.backend.prefix, 'attach-session', '-t', 'unrelated'])
        until(lambda: self.clients().startswith('unrelated|'))
        with self.subTest('detached origin'):
            output = self.work / 'detached-result.txt'
            script = (
                'import subprocess; from pathlib import Path; '
                f'r = subprocess.run({self.tui_command()[:-1] + ["attach", self.agent["id"]]!r}, '
                'capture_output=True, text=True); '
                f'Path({str(output)!r}).write_text(r.stdout + r.stderr)'
            )
            self.backend.create('detached', self.work, [sys.executable, '-c', script])
            until(output.exists)
            self.assertIn('No tmux client is attached to the originating dashboard', output.read_text())
            self.assertTrue(self.clients().startswith('unrelated|'))
        with self.subTest('separate terminal with inherited TMUX'):
            pane, server, session = self.backend.run('display-message', '-p', '-t', 'unrelated',
                                                    '#{pane_id}\t#{pid}\t#{session_id}').split('\t')
            master, _ = self.client(self.tui_command(), {
                'TMUX': f'{self.socket},{server},{session[1:]}', 'TMUX_PANE': pane,
            })
            self.read_until(master, b'tester')
            self.open_agent(master)
            self.client_count(2)
            os.write(master, b'\x02d')
            self.read_until(master, b'tester')
            self.clients_equal('unrelated|')
            self.assert_agent_alive()

    def test_detach_after_refreshes_resume_terminal_and_clean_shutdown(self):
        """A dashboard outside tmux returns from an agent after refreshing while suspended, leaves stock tmux keys as
        they were, and quits without stopping the agent; fragile because it depends on Textual's writer-queue
        internals, which once filled while suspended and hung the dashboard."""
        before = self.bindings()
        # Accelerate the real dashboard timer: short attach tests never filled
        # Textual's stopped writer queue when suspend incorrectly yielded.
        code = (
            'import sys; from pathlib import Path; '
            f'sys.path.insert(0, {str(ROOT / "src")!r}); '
            'from multiverse.ui.app import MultiverseApp; '
            f'MultiverseApp(Path({str(self.root)!r}), refresh_interval=.05).run()'
        )
        master, process = self.client([sys.executable, '-c', code])
        self.read_until(master, b'tester')
        # Report synchronized-output support, matching the user's terminal
        # and exercising the _begin_update path from the reported stack.
        os.write(master, b'\x1b[?2026;1$y')
        self.open_agent(master)
        os.write(master, b'\x02L')
        self.read_until(master, b"Can't find last session")
        end = time.monotonic() + 3  # Also lets tmux's error message expire before the next prefix.
        while time.monotonic() < end:
            if select.select([master], [], [], .1)[0]:
                os.read(master, 65536)
        os.write(master, b'\x02d')
        self.read_until(master, b'tester')
        self.clients_equal('')
        self.assertIsNone(process.poll())
        self.assertEqual(self.bindings(), before)
        self.assert_agent_alive()
        # Input must be handled by the resumed dashboard, not echoed in cooked
        # mode. Exit normally rather than using the repeated Ctrl-C workaround.
        os.write(master, b'q')
        self.assertEqual(self.wait_exit(master, process), 0)
        self.assert_agent_alive()
