"""Native failures retain bounded terminal evidence across lifecycle surfaces."""
import contextlib
import io
import os
import subprocess
import sys
import threading
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.runtime import native, runner
from multiverse.universe.mailbox import inbox, message_record
from multiverse.store import read, write
from multiverse.tools import deliver, doctor, inspect, list_agents, send
from test_multiverse import BaseTest, TerminalBase, until


ERROR = 'No conversation found with session ID: exact-saved-session'


class LaunchDiagnosticTerminalTest(TerminalBase):
    def test_teardown_waits_for_detached_cleanup_after_panes_are_gone(self):
        """TerminalBase.kill_server waits for detached cleanup helpers after the panes are gone; a break gives
        intermittent cleanup failures in other modules that are hard to attribute."""
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

    def failing_harness(self, code=1, delay=.15, release=None):
        """A fake harness that prints to its terminal, then ERROR, and exits with `code`: after `delay` seconds,
        or once the test creates `release`."""
        script = self.work / 'failure.py'
        wait = f'while not os.path.exists({str(release)!r}): time.sleep(.01)\n' if release else f'time.sleep({delay})\n'
        script.write_text('import os,sys,time\n'
                          'print("Native stdout is still a terminal: " + str(sys.stdout.isatty()), flush=True)\n'
                          f'{wait}print({ERROR!r}, file=sys.stderr, flush=True)\n'
                          f'sys.exit({code})\n')
        write(self.root / 'harnesses/fake/harness.json', {'schema': 1,
            'spawn': [sys.executable, str(script)], 'resume': [sys.executable, str(script)]})

    def test_a_failed_launch_keeps_its_terminal_output_as_the_reason(self):
        """A native launch failure keeps its terminal output as the reason by exit code, timing and kill, for
        list, inspect and doctor; fragile because the output is captured through tmux after the pane died, and a
        break shows only when a real failure has no reason."""
        # This checks exit evidence, not startup speed. Leave scheduling room under suite load;
        # the release file still guarantees that the later exits happen only after readiness.
        readiness_timeout = 25
        self.failing_harness()
        with self.subTest(exit='failure during startup'):
            result = lifecycle.start(self.root, 'bad-session', harness='fake', socket_path=self.socket,
                                     wait=True, wait_timeout=readiness_timeout)
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

        self.failing_harness(code=0)
        with self.subTest(exit='clean exit during startup'):
            ended = lifecycle.start(self.root, 'early', harness='fake', socket_path=self.socket,
                                    wait=True, wait_timeout=readiness_timeout)
            self.assertFalse(ended['ready'])
            self.assertEqual(ended['runtime']['state'], 'stopped')
            self.assertIn('code 0', ended['error'])
            self.assertIn(ERROR, ended['error'])

        self.failing_harness(code=143)
        with self.subTest(exit='SIGTERM during startup'):
            stopped = lifecycle.start(self.root, 'terminated-early', harness='fake', socket_path=self.socket,
                                      wait=True, wait_timeout=readiness_timeout)
            directory = agents.directory(self.root, stopped['id'])
            facts = agents.runtime(directory)
            self.assertEqual(facts['exit_code'], 143)
            self.assertEqual(facts['state'], 'stopped')
            self.assertFalse(lifecycle.listing(self.root, only=directory.name)[0]['launch_error'])
            self.assertNotIn('terminated-early', '\n'.join(doctor.run(self.root)['problems']))
            self.assertIn(ERROR, inspect.run(self.root, agent=directory.name))  # Native terminal text is untouched.

        for code in (1, 0, 143):
            # The harness exits just after the launch is ready, once this test releases it.
            release = self.work / f'release-{code}'
            self.failing_harness(code=code, release=release)
            with self.subTest(exit='after the launch was ready', code=code):
                result = lifecycle.start(self.root, f'later-{code}', harness='fake', socket_path=self.socket,
                                         wait=True, wait_timeout=readiness_timeout)
                self.assertTrue(result['ready'], result)
                release.touch()
                directory = agents.directory(self.root, result['id'])
                until(lambda: 'exit_code' in agents.runtime(directory))
                facts = agents.runtime(directory)
                if code == 1:
                    self.assertIn(ERROR, facts['reason'])
                    self.assertNotIn('during startup', facts['reason'])
                else:
                    self.assertNotIn('reason', facts)
                    self.assertNotIn('output_tail_file', facts)
                if code == 143:
                    self.assertIn(facts['state'], ('stopped', 'dormant'))
                    until(lambda: agents.runtime(directory).get('wrapper_cleanup', {}).get('state') == 'done', seconds=readiness_timeout)
                    facts = agents.runtime(directory)
                    # Old runner records retain their bytes but no longer surface SIGTERM as a launch failure.
                    old = {**facts, 'state': 'failed', 'reason': ERROR}
                    write(directory / 'runtime.json', old)
                    self.assertFalse(lifecycle.listing(self.root, only=directory.name)[0]['launch_error'])
                    self.assertNotIn('later-143', '\n'.join(doctor.run(self.root)['problems']))
                    self.assertEqual(read(directory / 'runtime.json'), old)

        self.failing_harness(release=self.work / 'never')
        with self.subTest(exit='explicit kill'):
            live = lifecycle.start(self.root, 'normal', harness='fake', socket_path=self.socket,
                                   wait=True, wait_timeout=readiness_timeout)
            self.assertTrue(live['ready'], live)
            lifecycle.kill(self.root, live['id'])
            self.assertNotIn('output_tail_file', agents.runtime(agents.directory(self.root, live['id'])))

    def failing_claude(self, name, delay=.15, bridge=False):
        """A Claude harness whose process sleeps `delay` seconds, prints ERROR and exits 1."""
        executable = self.work / name
        executable.write_text(f'#!{sys.executable}\nimport sys,time\ntime.sleep({delay})\n'
                              f'print({ERROR!r}, file=sys.stderr, flush=True)\nsys.exit(1)\n')
        executable.chmod(0o755)
        spec = read(self.root / 'harnesses/claude/harness.json')
        spec['settings'] = {'executable': str(executable), 'inbox_bridge': bridge}
        write(self.root / 'harnesses/claude/harness.json', spec)
        return executable

    def test_a_failed_wake_never_replays_and_fails_its_queued_input(self):
        """Through the real runner and tmux, a wake whose process cannot start ends failed with its output as the
        reason, never replays an uncertain prompt, turns queued bridge messages into failed ones, and deliver
        --check exits non-zero while it waits; a break goes unnoticed because messages sit in submitting or
        pending forever (qa-claude e75b3b1)."""
        self.failing_claude('fake-claude')
        with self.subTest(wake='uncertain'):
            first = lifecycle.start(self.root, 'wake-error', harness='claude', socket_path=self.socket,
                                    wait=True, wait_timeout=5)
            self.assertFalse(first['ready'])
            directory = agents.directory(self.root, first['id'])
            # A damaged native transcript supplies no negative consumption proof.
            from multiverse.harnesses.claude import claude_home
            transcript = (claude_home() / 'projects' / '-fixture'
                          / (read(directory / 'properties.json')['native_session_id'] + '.jsonl'))
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text('{"type":')
            sent = send.run(self.root, agent=first['id'], text='Do this exactly once')
            self.assertTrue(sent['saved'])
            message = inbox(directory, all_messages=True)['messages'][-1]
            until(lambda: agents.runtime(directory).get('state') == 'failed')
            until(lambda: inbox(directory, all_messages=True)['messages'][-1]['tracking']['submission_state']
                  == 'unknown')
            checked = native.deliver(self.root, first['id'], message['id'], check=True)
            self.assertFalse(checked['ok'])
            self.assertIn(ERROR, checked['reason'])
            self.assertIn('unconfirmed', checked['reason'])
            self.assertIn(ERROR, deliver.run(self.root, agent=first['id'], check=True)['messages'][0]['reason'])
            with patch.object(lifecycle, 'start', side_effect=AssertionError('Uncertain prompt must not be replayed')):
                retried = native.send_or_wake(self.root, first['id'], message['id'], retry=True)
            self.assertIn(ERROR, retried['reason'])

        broken = self.work / 'broken-native'
        broken.write_text('#!/nonexistent/interpreter\n')
        broken.chmod(0o755)
        for executable, reason in ((self.failing_claude('fake-claude'), ERROR), (broken, 'No such file or directory')):
            with self.subTest(wake='definite failure', executable=executable.name):
                spec = read(self.root / 'harnesses/claude/harness.json')
                spec['settings']['executable'] = str(executable)
                write(self.root / 'harnesses/claude/harness.json', spec)
                agent_id = str(uuid4())
                directory = self.root / 'state/agents' / agent_id
                write(directory / 'properties.json', {'id': agent_id,
                      'universe_id': read(self.root / 'universe.json')['id'], 'name': 'failed-' + executable.name,
                      'task': 'original task', 'harness': 'claude', 'workspace': str(self.work),
                      'native_session_id': str(uuid4()),
                      # This current synthetic recipient has a known launch scope;
                      # legacy storage discovery is a separate upgrade boundary.
                      'native_storage_environment': {key: os.environ.get(key) for adapter in harnesses.BUILTINS.values()
                                                     for key in adapter.storage_env}})
                with patch.dict(os.environ, {'MV2_TMUX_SOCKET': self.socket}):
                    result = send.run(self.root, agent=agent_id, text='Preserve this prompt', sender={'name': 'user'})
                self.assertTrue(result['saved'])
                saved = inbox(directory, all_messages=True)['messages'][0]
                until(lambda: message_record(directory, saved['id'])[1].get('submission_state') == 'failed')
                # The mailbox failure and runtime diagnostic are separate writes.
                until(lambda: agents.runtime(directory).get('reason'))
                tracking = message_record(directory, saved['id'])[1]
                self.assertEqual(tracking['delivery_method'], 'resume_prompt')
                self.assertEqual(tracking['attempts'][-1]['state'], 'error')
                self.assertIn(reason, tracking['attempts'][-1]['reason'])
                self.assertIn(agents.runtime(directory)['reason'], tracking['attempts'][-1]['reason'])
                request = read(directory / 'launches' / tracking['wake_launch_id'] / 'request.json')
                self.assertEqual(request['argv'][-1], 'Message from user:\nPreserve this prompt')
                self.assertEqual(read(directory / 'properties.json')['task'], 'original task')
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as checked:
                    deliver.cli_run(self.root, agent=agent_id, check=True)
                self.assertEqual(checked.exception.code, 1)

        self.failing_claude('fake-claude-bridge', delay=.8, bridge=True)
        with self.subTest(wake='queued on the bridge'):
            result = lifecycle.start(self.root, 'queue-error', harness='claude', socket_path=self.socket)
            directory = agents.directory(self.root, result['id'])
            sent = send.run(self.root, agent=result['id'], text='Wait for registration')
            self.assertEqual(sent['state'], 'pending', sent)
            until(lambda: agents.runtime(directory).get('startup_delivery') == 'failed')
            saved = inbox(directory, all_messages=True)['messages'][0]
            self.assertEqual(saved['tracking']['submission_state'], 'failed')
            self.assertIn(ERROR, saved['tracking']['attempts'][-1]['reason'])


class LaunchDiagnosticBoundTest(BaseTest):
    def test_a_stale_runner_cannot_capture_or_publish_for_a_replacement(self):
        """A stale runner never captures a replacement launch's pane or publishes into its record; a break writes
        the wrong launch's output into the new record."""
        directory = self.root / 'state/agents/agent'
        write(directory / 'runtime.json', {'launch_id': 'old', 'tmux': {'socket': '/fake', 'pane': '%1'}})
        write(directory / 'properties.json', {'id': 'agent', 'universe_id': 'universe'})
        with self.subTest(replaced='the pane'), \
             patch('multiverse.terminal.tmux.Tmux.snapshot',
                   return_value=({'%1': {'@mv2_launch_id': 'replacement'}}, None)), \
             patch('multiverse.terminal.tmux.Tmux.run', side_effect=AssertionError('Must not capture another launch')):
            result = runner.launch_diagnostic(directory, 'old', 'Native process exited with code 1.')
            self.assertIn('output unavailable', result['reason'])
            self.assertEqual((directory / result['output_tail_file']).read_text(), '')
        directory = self.root / 'state/agents/replaced'
        write(directory / 'runtime.json', {'launch_id': 'replacement'})
        with self.subTest(replaced='the record'), \
             patch('multiverse.terminal.tmux.owned', side_effect=AssertionError('Must not capture')):
            self.assertEqual(runner.launch_diagnostic(directory, 'old', 'Exited'), {})
            self.assertFalse((directory / 'launches/old/output-tail.txt').exists())
