"""Native failures retain bounded terminal evidence across lifecycle surfaces."""
import subprocess
import sys
import threading
from unittest.mock import patch

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.runtime import native, runner
from multiverse.universe.mailbox import inbox
from multiverse.store import read, write
from multiverse.tools import deliver, doctor, inspect, list_agents, send
from test_multiverse import BaseTest, TerminalBase, until


ERROR = 'No conversation found with session ID: exact-saved-session'


class LaunchDiagnosticTerminalTest(TerminalBase):
    def test_teardown_waits_for_detached_cleanup_after_panes_are_gone(self):
        launch = self.root / 'state/agents/cleanup-test/launches/old'
        launch.mkdir(parents=True)
        output = launch / 'late-write'
        # Use the cleanup helper's trailing arguments, but make its final write
        # slow and deterministic without changing the production runner.
        helper = subprocess.Popen([sys.executable, '-c',
            'import sys,time; from pathlib import Path; time.sleep(.5); '
            'Path(sys.argv[-1], "late-write").write_text("finished")',
            str(runner.__file__), '--cleanup-wrapper', str(launch)])
        self.addCleanup(lambda: helper.wait(timeout=5))
        self.addCleanup(lambda: helper.terminate() if helper.poll() is None else None)
        # Real helpers are reaped by their runner/init; reap this test child too.
        reaper = threading.Thread(target=helper.wait, daemon=True)
        reaper.start()
        self.addCleanup(lambda: reaper.join(timeout=5))
        self.assertEqual(self.backend.snapshot()[0], {})
        self.kill_server()
        self.assertIsNotNone(helper.poll())
        self.assertEqual(output.read_text(), 'finished')

    def failing_harness(self, code=1, delay=.15):
        script = self.work / 'failure.py'
        script.write_text('import sys,time\n'
                          'print("Native stdout is still a terminal: " + str(sys.stdout.isatty()), flush=True)\n'
                          f'time.sleep({delay})\nprint({ERROR!r}, file=sys.stderr, flush=True)\n'
                          f'sys.exit({code})\n')
        write(self.root / 'harnesses/fake/harness.json', {'schema': 1,
            'spawn': [sys.executable, str(script)], 'resume': [sys.executable, str(script)]})

    def test_failed_wait_retains_output_for_list_inspect_doctor_and_gone_wrapper(self):
        self.failing_harness()
        result = lifecycle.start(self.root, 'bad-session', harness='fake', socket_path=self.socket,
                              wait=True, wait_timeout=5)
        self.assertFalse(result['ready'], result)
        self.assertIn(ERROR, result['error'])
        directory = agents.directory(self.root, result['id'])
        facts = agents.runtime(directory)
        self.assertEqual(facts['state'], 'failed')
        self.assertEqual(facts['exit_code'], 1)
        output = directory / facts['output_tail_file']
        self.assertIn(ERROR, output.read_text())
        self.assertIn('terminal: True', output.read_text())
        self.assertLessEqual(output.stat().st_size, 8192)
        self.assertLessEqual(len(output.read_text().splitlines()), 40)
        for removed in (False, True):
            if removed:
                self.backend.run('kill-pane', '-t', facts['tmux']['pane'])
            self.assertIn(ERROR, list_agents.run(self.root))
            self.assertIn(ERROR, list_agents.run(self.root, json=True)[0]['reason'])
            self.assertIn(ERROR, inspect.run(self.root, agent=result['id']))
            self.assertIn(ERROR, '\n'.join(doctor.run(self.root)['problems']))

    def test_early_clean_exit_has_a_reason_but_explicit_kill_does_not(self):
        self.failing_harness(code=0)
        ended = lifecycle.start(self.root, 'early', harness='fake', socket_path=self.socket, wait=True, wait_timeout=5)
        self.assertFalse(ended['ready'])
        self.assertEqual(ended['runtime']['state'], 'stopped')
        self.assertIn('code 0', ended['error'])
        self.assertIn(ERROR, ended['error'])
        self.failing_harness(delay=30)
        live = lifecycle.start(self.root, 'normal', harness='fake', socket_path=self.socket, wait=True, wait_timeout=5)
        self.assertTrue(live['ready'], live)
        lifecycle.kill(self.root, live['id'])
        self.assertNotIn('output_tail_file', agents.runtime(agents.directory(self.root, live['id'])))

    def test_failed_wake_reports_native_output_without_replaying_uncertain_prompt(self):
        executable = self.work / 'fake-claude'
        executable.write_text(f'#!{sys.executable}\nimport sys,time\ntime.sleep(.15)\n'
                              f'print({ERROR!r}, file=sys.stderr, flush=True)\nsys.exit(1)\n')
        executable.chmod(0o755)
        spec = read(self.root / 'harnesses/claude/harness.json')
        spec['settings'] = {'executable': str(executable), 'inbox_bridge': False}
        write(self.root / 'harnesses/claude/harness.json', spec)
        first = lifecycle.start(self.root, 'wake-error', harness='claude', socket_path=self.socket,
                             wait=True, wait_timeout=5)
        self.assertFalse(first['ready'])
        directory = agents.directory(self.root, first['id'])
        sent = send.run(self.root, agent=first['id'], text='Do this exactly once')
        self.assertTrue(sent['saved'])
        message = inbox(directory, all_messages=True)['messages'][-1]
        until(lambda: agents.runtime(directory).get('state') == 'failed')
        until(lambda: inbox(directory, all_messages=True)['messages'][-1]['tracking']['submission_state'] == 'unknown')
        checked = native.deliver(self.root, first['id'], message['id'], check=True)
        self.assertFalse(checked['ok'])
        self.assertIn(ERROR, checked['reason'])
        self.assertIn('unconfirmed', checked['reason'])
        self.assertIn(ERROR, deliver.run(self.root, agent=first['id'], check=True)['messages'][0]['reason'])
        with patch.object(lifecycle, 'start', side_effect=AssertionError('Uncertain prompt must not be replayed')):
            retried = native.send_or_wake(self.root, first['id'], message['id'], retry=True)
        self.assertIn(ERROR, retried['reason'])

    def test_pending_bridge_message_receives_the_launch_diagnostic(self):
        executable = self.work / 'fake-claude-bridge'
        executable.write_text(f'#!{sys.executable}\nimport sys,time\ntime.sleep(.8)\n'
                              f'print({ERROR!r}, file=sys.stderr, flush=True)\nsys.exit(1)\n')
        executable.chmod(0o755)
        spec = read(self.root / 'harnesses/claude/harness.json')
        spec['settings'] = {'executable': str(executable), 'inbox_bridge': True}
        write(self.root / 'harnesses/claude/harness.json', spec)
        result = lifecycle.start(self.root, 'queue-error', harness='claude', socket_path=self.socket)
        directory = agents.directory(self.root, result['id'])
        sent = send.run(self.root, agent=result['id'], text='Wait for registration')
        self.assertEqual(sent['state'], 'pending', sent)
        until(lambda: agents.runtime(directory).get('startup_delivery') == 'failed')
        saved = inbox(directory, all_messages=True)['messages'][0]
        self.assertEqual(saved['tracking']['submission_state'], 'failed')
        self.assertIn(ERROR, saved['tracking']['attempts'][-1]['reason'])

    def test_later_failure_is_saved_but_later_clean_exit_is_not(self):
        for code in (1, 0):
            with self.subTest(code=code):
                self.failing_harness(code=code, delay=3)
                result = lifecycle.start(self.root, f'later-{code}', harness='fake', socket_path=self.socket,
                                      wait=True, wait_timeout=5)
                self.assertTrue(result['ready'], result)
                directory = agents.directory(self.root, result['id'])
                until(lambda: 'exit_code' in agents.runtime(directory))
                facts = agents.runtime(directory)
                if code:
                    self.assertIn(ERROR, facts['reason'])
                    self.assertNotIn('during startup', facts['reason'])
                else:
                    self.assertNotIn('reason', facts)
                    self.assertNotIn('output_tail_file', facts)


class LaunchDiagnosticBoundTest(BaseTest):
    def test_tail_limits_lines_bytes_and_removes_controls(self):
        source = '\n'.join(f'{index} ' + '🪐' * 200 for index in range(100)) + '\nLAST\x00\x1b\n\n'
        tail = runner.bounded_output(source)
        self.assertLessEqual(len(tail.encode()), 8192)
        self.assertLessEqual(len(tail.splitlines()), 40)
        self.assertTrue(tail.endswith('LAST'))
        self.assertNotIn('\x1b', tail)
        self.assertNotIn('\x00', tail)

    def test_capture_never_reads_a_replacement_pane(self):
        directory = self.root / 'state/agents/agent'
        write(directory / 'runtime.json', {'launch_id': 'old', 'tmux': {'socket': '/fake', 'pane': '%1'}})
        write(directory / 'properties.json', {'id': 'agent', 'universe_id': 'universe'})
        with patch('multiverse.terminal.tmux.Tmux.snapshot', return_value=({'%1': {'@mv2_launch_id': 'replacement'}}, None)), \
             patch('multiverse.terminal.tmux.Tmux.run', side_effect=AssertionError('Must not capture another launch')):
            result = runner.launch_diagnostic(directory, 'old', 'Native process exited with code 1.')
        self.assertIn('output unavailable', result['reason'])
        self.assertEqual((directory / result['output_tail_file']).read_text(), '')

    def test_stale_runner_cannot_capture_or_publish_a_replacement_launch(self):
        directory = self.root / 'state/agents/agent'
        write(directory / 'runtime.json', {'launch_id': 'replacement'})
        with patch('multiverse.terminal.tmux.owned', side_effect=AssertionError('Must not capture')):
            self.assertEqual(runner.launch_diagnostic(directory, 'old', 'Exited'), {})
        self.assertFalse((directory / 'launches/old/output-tail.txt').exists())
