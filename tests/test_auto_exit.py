"""Native exits detach only the agent's clients and remove its owned wrapper."""
import os
import pty
import signal
import subprocess
import sys
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse import agents, runner
from multiverse.store import read, write
from test_multiverse import BaseTest, TerminalBase, until


class AutoExitTest(TerminalBase):
    def start_ready(self):
        result = agents.start(self.root, 'manual-exit', harness='fake', socket_path=self.socket,
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
        self.assertEqual(read(directory / 'properties.json')['native_session_id'], native_id)
        resumed = agents.start(self.root, resume=result['id'], socket_path=self.socket, wait=True, wait_timeout=5)
        self.assertTrue(resumed['ready'], resumed)
        self.assertEqual(resumed['native_session_id'], native_id)

    def test_ctrl_c_that_native_handles_without_exiting_keeps_wrapper_and_client(self):
        script = self.root / 'harnesses/fake/worker.py'
        script.write_text('import signal\nsignal.signal(signal.SIGINT, lambda *_: print("TURN INTERRUPTED", flush=True))\n'
                          + script.read_text())
        result, directory = self.start_ready()
        pane = result['runtime']['tmux']['pane']
        attached, terminal = self.attach_client(pane)
        os.write(terminal, b'\x03')
        until(lambda: 'TURN INTERRUPTED' in self.backend.run('capture-pane', '-p', '-t', pane))
        self.assertEqual(agents.listing(self.root)[0]['live'], 'alive')
        self.assertIsNone(attached.poll())
        os.kill(agents.runtime(directory)['pid'], signal.SIGTERM)
        until(lambda: agents.runtime(directory).get('state') == 'dormant')
        self.assertEqual(attached.wait(timeout=5), 0)

    def test_clean_native_exit_removes_wrapper_without_an_attached_client(self):
        script = self.root / 'harnesses/fake/worker.py'
        script.write_text('import sys,time\nprint("NATIVE READY", flush=True)\ntime.sleep(3)\nsys.exit(0)\n')
        result, directory = self.start_ready()
        until(lambda: agents.runtime(directory).get('state') == 'dormant')
        self.assertEqual(agents.runtime(directory)['exit_code'], 0)
        self.assertEqual(agents.runtime(directory)['state'], 'dormant')
        self.assertNotIn(result['runtime']['tmux']['pane'], self.backend.snapshot()[0])


class AutoExitOwnershipTest(BaseTest):
    def test_old_cleanup_cannot_touch_replacement_launch(self):
        directory = self.root / 'state/agents/agent'
        write(directory / 'runtime.json', {'launch_id': 'replacement'})
        with patch('multiverse.tmux.owned', side_effect=AssertionError('Must not control replacement')):
            runner.cleanup_wrapper(directory / 'launches/old')
        self.assertEqual(read(directory / 'runtime.json'), {'launch_id': 'replacement'})

    def test_detach_precedes_kill_and_only_targets_clients_viewing_owned_pane(self):
        directory = self.root / 'state/agents/agent'
        write(directory / 'properties.json', {'id': 'agent', 'universe_id': 'universe'})
        write(directory / 'runtime.json', {'launch_id': 'old', 'state': 'stopped', 'exit_code': 0,
                                          'tmux': {'socket': '/fake', 'pane': '%1'}})
        backend = Mock()
        backend.run.side_effect = ['own\t%1\nother\t%2', '', '']
        with patch('multiverse.tmux.owned', return_value=(backend, '%1')):
            runner.cleanup_wrapper(directory / 'launches/old')
        self.assertEqual(backend.run.call_args_list[1].args, ('detach-client', '-t', 'own'))
        self.assertEqual(backend.run.call_args_list[2].args, ('kill-pane', '-t', '%1'))
        self.assertEqual(agents.runtime(directory)['state'], 'dormant')

    def test_cleanup_failure_leaves_wrapper_retryable(self):
        directory = self.root / 'state/agents/agent'
        write(directory / 'properties.json', {'id': 'agent', 'universe_id': 'universe'})
        write(directory / 'runtime.json', {'launch_id': 'old', 'state': 'stopped', 'exit_code': 0})
        with patch('multiverse.tmux.owned', side_effect=RuntimeError('tmux unavailable')):
            runner.cleanup_wrapper(directory / 'launches/old')
        self.assertEqual(agents.runtime(directory)['state'], 'stopped')
        self.assertIn('tmux unavailable', agents.runtime(directory)['error'])
