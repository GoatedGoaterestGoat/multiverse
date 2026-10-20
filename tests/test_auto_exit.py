"""Native exits detach only the agent's clients and remove its owned wrapper."""
import os
import pty
import subprocess
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.runtime import runner
from multiverse.store import read, write
from test_multiverse import BaseTest, TerminalBase, until


class AutoExitTest(TerminalBase):
    def start_ready(self):
        result = lifecycle.start(self.root, 'manual-exit', harness='fake', socket_path=self.socket,
                              wait=True, wait_timeout=5)
        self.assertTrue(result['ready'], result)
        return result, agents.directory(self.root, result['id'])

    def attach_client(self, pane):
        master, slave = pty.openpty()
        env = {**os.environ, 'TERM': 'xterm-256color'}
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        process = subprocess.Popen([*self.backend.prefix, 'attach-session', '-t', pane],
                                   stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        self.addCleanup(os.close, master)
        self.addCleanup(lambda: process.wait(timeout=5))
        self.addCleanup(lambda: process.terminate() if process.poll() is None else None)
        until(lambda: pane in self.backend.run('list-clients', '-F', '#{pane_id}').splitlines())
        return process, master

    def test_ctrl_c_detaches_attached_client_instead_of_switching_to_another_agent(self):
        """On real tmux a native exit detaches only the clients viewing that agent, removes its pane, and resume keeps
        the session in one new tagged pane; fragile because tmux moves attached clients to another session when a pane
        dies, so a break drops the user into another agent's pane to type there."""
        result, directory = self.start_ready()
        native_id = str(uuid4())
        agents.bind_session(self.root, result['id'], native_id)
        pane = result['runtime']['tmux']['pane']
        other = self.backend.create('other', self.work, ['sleep', '60'])['pane']
        attached, terminal = self.attach_client(pane)
        second_attached, _ = self.attach_client(pane)
        until(lambda: self.backend.run('list-clients', '-F', '#{pane_id}').splitlines().count(pane) == 2)
        other_client, _ = self.attach_client(other)
        os.write(terminal, b'\x03')
        until(lambda: agents.runtime(directory).get('state') == 'dormant')
        self.assertEqual(attached.wait(timeout=5), 0)
        self.assertEqual(second_attached.wait(timeout=5), 0)
        self.assertIsNone(other_client.poll(), 'Another agent client must remain attached')
        self.assertIn(other, self.backend.snapshot()[0])
        self.assertNotIn(pane, self.backend.snapshot()[0])
        self.assertEqual(agents.runtime(directory)['state'], 'dormant')
        self.assertEqual(lifecycle.listing(self.root)[0]['status'], '-')
        self.assertEqual(read(directory / 'properties.json')['native_session_id'], native_id)
        resumed = lifecycle.start(self.root, resume=result['id'], socket_path=self.socket, wait=True, wait_timeout=5)
        self.assertTrue(resumed['ready'], resumed)
        self.assertEqual(resumed['native_session_id'], native_id)
        self.assertNotEqual(resumed['runtime']['launch_id'], result['runtime']['launch_id'])
        panes = self.backend.snapshot()[0]
        self.assertEqual(set(panes), {other, resumed['runtime']['tmux']['pane']})
        self.assertEqual(panes[resumed['runtime']['tmux']['pane']]['@mv2_launch_id'], resumed['runtime']['launch_id'])


class AutoExitOwnershipTest(BaseTest):
    def test_stale_runner_cleanup_never_touches_a_replacement(self):
        """runner.cleanup_wrapper touches nothing once a replacement launch owns the runtime, detaches before it kills,
        and stays retryable when tmux fails; fragile because cleanup runs late and asynchronously, and a break kills a
        freshly resumed agent with nothing to say why."""
        with self.subTest('a replacement owns the runtime'):
            directory = self.root / 'state/agents/replaced'
            write(directory / 'runtime.json', {'launch_id': 'replacement'})
            with patch('multiverse.terminal.tmux.owned', side_effect=AssertionError('Must not control replacement')):
                runner.cleanup_wrapper(directory / 'launches/old')
            self.assertEqual(read(directory / 'runtime.json'), {'launch_id': 'replacement'})
        with self.subTest('detach precedes kill, only for clients viewing the owned pane'):
            directory = self.root / 'state/agents/owned'
            write(directory / 'properties.json', {'id': 'owned', 'universe_id': 'universe'})
            write(directory / 'runtime.json', {'launch_id': 'old', 'state': 'stopped', 'exit_code': 0,
                                              'tmux': {'socket': '/fake', 'pane': '%1'}})
            backend = Mock()
            backend.run.side_effect = ['own\t%1\nother\t%2', '', '']
            with patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
                runner.cleanup_wrapper(directory / 'launches/old')
            self.assertEqual(backend.run.call_args_list[1].args, ('detach-client', '-t', 'own'))
            self.assertEqual(backend.run.call_args_list[2].args, ('kill-pane', '-t', '%1'))
            self.assertEqual(agents.runtime(directory)['state'], 'dormant')
        with self.subTest('a tmux failure leaves the wrapper retryable'):
            directory = self.root / 'state/agents/failing'
            write(directory / 'properties.json', {'id': 'failing', 'universe_id': 'universe'})
            write(directory / 'runtime.json', {'launch_id': 'old', 'state': 'stopped', 'exit_code': 0})
            with patch('multiverse.terminal.tmux.owned', side_effect=RuntimeError('tmux unavailable')):
                runner.cleanup_wrapper(directory / 'launches/old')
            self.assertEqual(agents.runtime(directory)['state'], 'stopped')
            self.assertIn('tmux unavailable', agents.runtime(directory)['error'])
