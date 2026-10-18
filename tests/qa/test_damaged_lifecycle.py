"""LifecycleFixture for outside-in lifecycle checks: the mv2 CLI, documented files and tmux only. It has no tests of
its own; qa.test_missing_agent_records and qa.test_lifecycle_spec use it.

Agents are schema-1 command harnesses (docs/reference/files.md), so no model runs.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from isolation import track
import tempfile
import time
import unittest

# From qa-lifecycle 279e4e8; use this checkout and test interpreter.
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
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('MV2_') and k not in (
                        'TMUX', 'TMUX_PANE', 'CODEX_THREAD_ID', 'CLAUDECODE',
                        'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CONFIG_DIR')}
        self.env.update(HOME=str(home), CODEX_HOME=str(home / '.codex'),
                        CLAUDE_CONFIG_DIR=str(home / '.claude'),
                        MV2_TMUX_SOCKET=str(self.base / 'agents.sock'))
        track(tmux_socket=self.env['MV2_TMUX_SOCKET'])
        self.root = self.work / '.multiverse'
        self.addCleanup(self.stop_agents)
        self.mv2('init', str(self.work))
        self.harness('sleeper', ['/bin/sleep', '600'])

    def harness(self, name, argv, **fields):
        directory = self.root / 'harnesses' / name
        directory.mkdir()
        (directory / 'harness.json').write_text(json.dumps(
            {'schema': 1, 'spawn': argv, 'resume': argv, **fields}))

    def mv2(self, *args, check=True, timeout=60):
        result = subprocess.run([sys.executable, MV2, *args], cwd=self.work, env=self.env, text=True,
                                capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if check:
            self.assertEqual(result.returncode, 0, f'mv2 {" ".join(args)}\n{result.stdout}{result.stderr}')
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
        time.sleep(3)  # well past startup
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
        subprocess.run(['tmux', '-S', self.env['MV2_TMUX_SOCKET'], 'kill-server'],
                       capture_output=True, check=False)

    def remove_scratch(self):
        # Runners may finish writing their documented cleanup records after tmux exits.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.scratch.cleanup()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)
