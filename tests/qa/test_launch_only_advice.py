"""LC-1 public scenarios, pinned to this checkout with a private tmux server."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import sys
from isolation import track

MV2 = str(Path(__file__).resolve().parents[2] / 'bin/mv2')

class LifecycleFixture(unittest.TestCase):

    def setUp(self):
        if not shutil.which('tmux') or not MV2:
            self.skipTest('mv2 and tmux are required')
        self.scratch = tempfile.TemporaryDirectory(prefix='ql-bb-', dir='/tmp')
        self.addCleanup(self.remove_scratch)
        self.base = Path(self.scratch.name).resolve()
        self.work = self.base / 'work'
        self.work.mkdir()
        home = self.base / 'home'
        home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('MV2_') and k not in ('TMUX', 'TMUX_PANE', 'CODEX_THREAD_ID', 'CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CONFIG_DIR')}
        self.env.update(HOME=str(home), CODEX_HOME=str(home / '.codex'), CLAUDE_CONFIG_DIR=str(home / '.claude'), MV2_TMUX_SOCKET=str(self.base / 'agents.sock'))
        track(tmux_socket=self.env['MV2_TMUX_SOCKET'])
        self.root = self.work / '.multiverse'
        self.addCleanup(self.stop_agents)
        self.mv2('init', str(self.work))
        self.harness('sleeper', ['/bin/sleep', '600'])

    def harness(self, name, argv, **fields):
        directory = self.root / 'harnesses' / name
        directory.mkdir()
        (directory / 'harness.json').write_text(json.dumps({'schema': 1, 'spawn': argv, 'resume': argv, **fields}))

    def mv2(self, *args, check=True, timeout=60):
        result = subprocess.run([sys.executable, MV2, *args], cwd=self.work, env=self.env, text=True, capture_output=True, timeout=timeout)
        if check:
            self.assertEqual(result.returncode, 0, f"mv2 {' '.join(args)}\n{result.stdout}{result.stderr}")
        return result

    def agents(self):
        return {row['name']: row for row in json.loads(self.mv2('list', '--json').stdout)}

    def until(self, predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        self.fail('Timed out waiting for condition')

    def launch_only_agent_with_failed_steer(self, name='a'):
        self.mv2('spawn', '--name', name, '--harness', 'sleeper', '--wait')
        time.sleep(3)
        result = self.mv2('send', name, '--text', 'a steer it cannot receive', check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def stop_agents(self):
        for properties in self.root.glob('state/agents/*/properties.json'):
            try:
                identity = json.loads(properties.read_text())['id']
            except (OSError, ValueError, TypeError, KeyError):
                continue
            self.mv2('kill', identity, check=False)
        subprocess.run(['tmux', '-S', self.env['MV2_TMUX_SOCKET'], 'kill-server'], capture_output=True, check=False)

    def remove_scratch(self):
        deadline = time.monotonic() + 5
        while True:
            try:
                self.scratch.cleanup()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

class LaunchOnlySendAdvice(LifecycleFixture):
    """docs/use/troubleshooting.md "A launch-only agent can't receive messages"; docs/cli/messaging.md."""

    def test_send_to_running_launch_only_agent_does_not_say_starting(self):
        """LC-1: a launch-only agent that has run for seconds is not 'still starting'."""
        result = self.launch_only_agent_with_failed_steer()
        self.assertIn('Not delivered to a', result.stderr)
        self.assertNotIn('still starting', result.stderr)

    def test_printed_advice_for_launch_only_agent_can_be_followed(self):
        """LC-1: each printed next step is a command that can succeed, never a refused resume."""
        self.launch_only_agent_with_failed_steer()
        self.mv2('kill', 'a')
        self.mv2('resume', 'a', '--wait')
        retry = self.mv2('deliver', 'a', '--retry', check=False)
        advice = retry.stdout + retry.stderr
        if 'resume it' in advice:
            resume = self.mv2('resume', 'a', check=False)
            self.assertEqual(resume.returncode, 0, f'deliver --retry said to resume a running agent:\n{advice}\nresume answered: {resume.stderr}')

    def test_stopped_custom_agent_advice_does_not_loop(self):
        """LC-1: a harness that cannot wake should not be told to resume and retry."""
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        self.mv2('kill', 'a')
        result = self.mv2('send', 'a', '--text', 'hello', check=False)
        self.assertIn('cannot resume with a message', result.stderr)
        self.assertNotIn('resume it, then mv2 deliver a --retry', result.stderr)
