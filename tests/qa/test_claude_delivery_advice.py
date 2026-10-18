"""The ClaudeLaunchOnly fixture from qa-claude f3df071: a private universe, tmux server and stand-in claude, driven
through this checkout's mv2 CLI. It has no tests of its own, so a module that subclasses it runs only its own."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys
from isolation import track

class ClaudeLaunchOnly(unittest.TestCase):

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix='qacl-', dir='/tmp')).resolve()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.work = self.scratch / 'work'
        self.work.mkdir()
        home = self.scratch / 'home'
        home.mkdir()
        self.env = {k: v for (k, v) in os.environ.items() if not k.startswith(('MV2_', 'CLAUDE', 'CODEX_')) and k not in ('TMUX', 'TMUX_PANE')}
        self.env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / '.claude'), CODEX_HOME=str(home / '.codex'), MV2_TMUX_SOCKET=str(self.scratch / 't.sock'))
        track(tmux_socket=self.env['MV2_TMUX_SOCKET'])
        self.mv2('init', str(self.work))
        self.addCleanup(subprocess.run, ['tmux', '-S', self.env['MV2_TMUX_SOCKET'], 'kill-server'], capture_output=True)
        fake = self.scratch / 'claude'
        fake.write_text('#!/bin/sh\n[ "$1" = --version ] && { echo "2.1.302 (Claude Code)"; exit 0; }\nexec sleep 600\n')
        fake.chmod(493)
        for (name, settings) in (('claude-plain', {'executable': str(fake), 'inbox_bridge': False}), ('claude-auto', {'executable': str(fake)})):
            directory = self.work / '.multiverse/harnesses' / name
            directory.mkdir(parents=True)
            (directory / 'harness.json').write_text(json.dumps({'schema': 2, 'adapter': 'claude', 'settings': settings}))

    def mv2(self, *args, code=0):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / 'bin/mv2'), *args], cwd=self.work, env=self.env, capture_output=True, text=True, timeout=90)
        if code is not None:
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def agent_dir(self, name):
        for properties in (self.work / '.multiverse/state/agents').glob('*/properties.json'):
            if json.loads(properties.read_text())['name'] == name:
                return properties.parent
        self.fail('no agent named ' + name)
