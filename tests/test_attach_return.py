"""Real PTYs verify stock detach returns to the exact launching dashboard."""
import os
import pty
import select
import subprocess
import sys
import time
from uuid import uuid4

from test_multiverse import ROOT, TerminalBase, until
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.terminal.tmux import Tmux
from multiverse.store import read


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
        deadline = time.monotonic() + 10
        while marker not in output and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                output += os.read(master, 65536)
        self.assertIn(marker, output, repr(output[-2000:]))
        return output

    def tui_command(self):
        return [sys.executable, str(ROOT / 'bin' / 'mv2'), '-u', str(self.root), 'tui']

    def wait_exit(self, master, process):
        # Drain final redraws so the PTY's output buffer cannot block client exit.
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        return process.wait(timeout=1)

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
        os.write(master, b'\r')
        self.read_until(master, b'FAKE READY')
        # The dashboard's inspector can also print FAKE READY. Confirm that this
        # exact PTY now has an agent client, rather than accepting preview text.
        expected = self.terminals[master] + '|' + (pane or self.target['pane'])
        until(lambda: expected in self.backend.run('list-clients', '-F',
                                                   '#{client_tty}|#{pane_id}').splitlines())

    def test_external_dashboard_detach_returns_and_stock_l_is_unchanged(self):
        before = self.bindings()
        master, _ = self.client(self.tui_command())
        self.read_until(master, b'tester')
        self.open_agent(master)
        os.write(master, b'\x02L')
        self.read_until(master, b"Can't find last session")
        time.sleep(1)  # Let tmux's error message expire before the next prefix.
        os.write(master, b'\x02d')
        self.read_until(master, b'tester')
        self.assertEqual(self.clients(), '')
        self.assertEqual(self.bindings(), before)
        self.assert_agent_alive()
        for _ in range(3):
            self.open_agent(master)
            os.write(master, b'\x02d')
            self.read_until(master, b'tester')
            self.assert_agent_alive()

    def check_host_return(self, host):
        dashboard = host.create('dashboard', self.work, self.tui_command())
        pid = host.run('display-message', '-p', '-t', dashboard['pane'], '#{pane_pid}')
        before = self.bindings(host)
        master, process = self.client([*host.prefix, 'attach-session', '-t', dashboard['pane']])
        self.read_until(master, b'tester')
        for _ in range(3):
            self.open_agent(master)
            # Only the agent client owns the host tty: no nested client intercepts d.
            self.assertEqual(len(self.clients().splitlines()), 1)
            if host is not self.backend:
                self.assertEqual(self.clients(host), '')
            os.write(master, b'\x02d')
            self.read_until(master, b'tester')
            until(lambda: self.clients(host).startswith('dashboard|'))
            self.assertEqual(host.run('display-message', '-p', '-t', dashboard['pane'],
                                      '#{pane_pid}'), pid)
            self.assertIsNone(process.poll())
            self.assert_agent_alive()
        self.assertEqual(self.bindings(host), before)
        self.assert_agent_alive()
        # Ordinary detach on the dashboard still leaves the host.
        os.write(master, b'\x02d')
        until(lambda: self.clients(host) == '')
        self.wait_exit(master, process)
        # Reattach and open an agent again; no stale per-client return state.
        master, _ = self.client([*host.prefix, 'attach-session', '-t', dashboard['pane']])
        self.read_until(master, b'tester')
        self.open_agent(master)
        os.write(master, b'\x02d')
        self.read_until(master, b'tester')
        self.assertTrue(self.clients(host).startswith('dashboard|'))

    def test_same_server_d_returns_to_original_dashboard(self):
        self.check_host_return(self.backend)

    def test_cross_server_d_returns_without_nested_prefix_forwarding(self):
        outer = Tmux(str(self.work / "outer 'quoted'.sock"))
        subprocess.run([*outer.prefix, '-f', '/dev/null', 'new-session', '-d',
                        '-s', 'unrelated', 'sleep 120'], check=True)
        self.addCleanup(outer.run, 'kill-server')
        self.check_host_return(outer)

    def test_custom_d_binding_and_prefix_are_preserved(self):
        self.backend.run('set-option', '-g', 'prefix', 'C-a')
        self.backend.run('bind-key', '-T', 'prefix', 'd', 'display-message', 'CUSTOM DETACH')
        before = self.bindings()
        dashboard = self.backend.create('dashboard', self.work, self.tui_command())
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
        self.assertTrue(self.clients().startswith('dashboard|'))

    def test_two_dashboards_return_to_their_own_origin(self):
        clients = []
        for name in ('first', 'second'):
            dashboard = self.backend.create(name, self.work, self.tui_command())
            master, _ = self.client([*self.backend.prefix, 'attach-session', '-t', dashboard['pane']])
            self.read_until(master, b'tester')
            self.open_agent(master)
            clients.append((name, master))
        self.assertEqual(len(self.clients().splitlines()), 2)
        for name, master in clients:
            os.write(master, b'\x02d')
            self.read_until(master, b'tester')
            until(lambda: name + '|' in self.clients().splitlines())
            self.assertIn(name + '|', self.clients().splitlines())
        self.assert_agent_alive()

    def test_missing_origin_does_not_select_an_arbitrary_session(self):
        dashboard = self.backend.create('dashboard', self.work, self.tui_command())
        master, process = self.client([*self.backend.prefix, 'attach-session', '-t', dashboard['pane']])
        self.read_until(master, b'tester')
        self.open_agent(master)
        self.backend.run('kill-session', '-t', dashboard['session'])
        os.write(master, b'\x02d')
        self.wait_exit(master, process)
        self.assertEqual(self.clients(), '')
        self.assert_agent_alive()

    def test_raw_agent_client_detach_has_no_dashboard_route(self):
        master, process = self.client([*self.backend.prefix, 'attach-session', '-t', self.target['pane']])
        self.read_until(master, b'FAKE READY')
        os.write(master, b'\x02d')
        self.wait_exit(master, process)
        self.assertEqual(self.clients(), '')
        self.assert_agent_alive()

    def test_dashboard_selection_survives_attach_and_return(self):
        other = lifecycle.start(self.root, 'z-selected', 'second agent', 'fake', socket_path=self.socket)
        agents.report(self.root, other['id'], summary='second agent')
        dashboard = self.backend.create('dashboard', self.work, self.tui_command())
        master, _ = self.client([*self.backend.prefix, 'attach-session', '-t', dashboard['pane']])
        self.read_until(master, b'z-selected')
        # Agent ordering follows generated identities; select explicitly via search.
        os.write(master, b'/z-selected\r')
        def filtered():
            screen = self.backend.run('capture-pane', '-p', '-t', dashboard['pane'])
            return 'z-selected' in screen and 'tester' not in screen
        until(filtered)
        # Filtering may select its group row; Down chooses the sole remaining agent.
        os.write(master, b'\x1b[B')
        self.read_until(master, 'test · second agent'.encode())
        for _ in range(2):
            self.open_agent(master, other['runtime']['tmux']['pane'])
            self.assertEqual(self.backend.run('list-clients', '-F', '#{pane_id}'),
                             other['runtime']['tmux']['pane'])
            os.write(master, b'\x02d')
            self.read_until(master, 'test · second agent'.encode())
            self.assertTrue(filtered(), 'Dashboard filter changed after returning')

    def test_detached_origin_cannot_replace_an_unrelated_client(self):
        master, _ = self.client([*self.backend.prefix, 'attach-session', '-t', 'unrelated'])
        until(lambda: self.clients().startswith('unrelated|'))
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

    def test_separate_terminal_with_inherited_tmux_does_not_replace_parent_client(self):
        self.client([*self.backend.prefix, 'attach-session', '-t', 'unrelated'])
        until(lambda: self.clients().startswith('unrelated|'))
        pane, server, session = self.backend.run('display-message', '-p', '-t', 'unrelated',
                                                '#{pane_id}\t#{pid}\t#{session_id}').split('\t')
        master, _ = self.client(self.tui_command(), {
            'TMUX': f'{self.socket},{server},{session[1:]}', 'TMUX_PANE': pane,
        })
        self.read_until(master, b'tester')
        self.open_agent(master)
        self.assertEqual(len(self.clients().splitlines()), 2)
        os.write(master, b'\x02d')
        self.read_until(master, b'tester')
        self.assertEqual(self.clients(), 'unrelated|')
        self.assert_agent_alive()

    def test_detach_after_refreshes_resume_terminal_and_clean_shutdown(self):
        # Accelerate the real dashboard timer: short attach tests never filled
        # Textual's stopped writer queue when suspend incorrectly yielded.
        code = (
            'import sys; from pathlib import Path; '
            f'sys.path.insert(0, {str(ROOT / "src")!r}); '
            'from multiverse.tui import MultiverseApp; '
            f'MultiverseApp(Path({str(self.root)!r}), refresh_interval=.05).run()'
        )
        master, process = self.client([sys.executable, '-c', code])
        self.read_until(master, b'tester')
        for _ in range(2):
            # Report synchronized-output support, matching the user's terminal
            # and exercising the _begin_update path from the reported stack.
            os.write(master, b'\x1b[?2026;1$y')
            self.open_agent(master)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                if select.select([master], [], [], .1)[0]:
                    os.read(master, 65536)
            os.write(master, b'\x02d')
            self.read_until(master, b'tester')
            self.assertEqual(self.clients(), '')
            self.assertIsNone(process.poll())
            self.assert_agent_alive()
        # Input must be handled by the resumed dashboard, not echoed in cooked
        # mode. Exit normally rather than using the repeated Ctrl-C workaround.
        os.write(master, b'q')
        self.assertEqual(self.wait_exit(master, process), 0)
