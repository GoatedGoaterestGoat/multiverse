"""QA lifecycle decided contracts from 540cb4e, pinned to this checkout: the mv2 CLI, documented files and tmux only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from isolation import track

MV2 = str(Path(__file__).resolve().parents[2] / 'bin/mv2')


class DecidedSpecQuestions(unittest.TestCase):
    """Behaviour mv2-lead decided for this pass's spec questions (Q4)."""

    def setUp(self):
        if not shutil.which('tmux'):
            self.skipTest('tmux is required')
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

    def test_missing_executable_says_name_did_not_start(self):
        """A missing executable releases the name before native preparation; stale advice or a retained reservation otherwise traps CLI retries (Q4)."""
        self.harness('missing', ['/nonexistent/qa-agent'])
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.mv2('spawn', '--name', 'm', '--harness', 'missing', '--wait', check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('m did not start: Harness executable not found', result.stderr)
                self.assertNotIn('mv2 remove m', result.stderr)
                self.assertEqual(json.loads(self.mv2('list', '--json').stdout), [])
