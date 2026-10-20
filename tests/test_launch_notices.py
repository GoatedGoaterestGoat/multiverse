"""A resume through the real CLI keeps the saved native storage and explains the overrides it ignored."""
import os
import json
import subprocess
import sys
from uuid import uuid4

from multiverse.runtime import lifecycle
from multiverse.store import read, write
from test_multiverse import TerminalBase, ROOT, until


class LaunchNoticeCliTest(TerminalBase):
    def test_real_cli_resume_prints_and_serializes_saved_home_notice(self):
        """Through the real CLI a waited resume runs the native child with the saved CODEX_HOME and CODEX_SQLITE_HOME,
        not the caller's, keeps the conversation and names both ignored overrides in its result; the caller's
        override looks legitimate, and a break silently resumes into an empty home as a new conversation (0fe24aa,
        473392e, e22da69, CODEX-01)."""
        executable = self.work / 'codex-stand-in'
        executable.write_text(f'#!{sys.executable}\n'
            'import os,sys,time,json\nfrom pathlib import Path\n'
            'if sys.argv[1:] == ["--version"]: print("codex-cli 0.164.0"); sys.exit()\n'
            'Path(os.environ["MV2_AGENT_DIR"], "seen.json").write_text(json.dumps(\n'
            '    {k: os.environ.get(k) for k in ("CODEX_HOME", "CODEX_SQLITE_HOME")}))\n'
            'print("STAND-IN READY", flush=True)\nwhile True: time.sleep(1)\n')
        executable.chmod(0o755)
        config = read(self.root / 'harnesses/codex/harness.json')
        config['settings'].update(executable=str(executable), inbox_bridge=False)
        write(self.root / 'harnesses/codex/harness.json', config)
        directory, properties, _, _ = lifecycle._record(self.root, 'notice-worker', '', 'codex', None, None)
        saved = self.work / 'saved-home'
        saved.mkdir()
        saved_environment = {'CODEX_HOME': str(saved), 'CODEX_SQLITE_HOME': str(self.work / 'saved-database')}
        properties.update(native_session_id=str(uuid4()), native_session_provider='codex',
                          workspace=str(self.work), native_environment=saved_environment)
        write(directory / 'properties.json', properties)
        requested = {key: str(self.work / ('requested-' + key)) for key in saved_environment}
        environment = {**os.environ, **requested, 'MV2_TMUX_SOCKET': self.socket, 'PYTHONPATH': str(ROOT / 'src')}
        process = subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root),
                                  'resume', properties['id'], '--wait', '--json'], env=environment,
                                 capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['native_session_id'], properties['native_session_id'])
        notice = next((notice for notice in result['notices'] if requested['CODEX_HOME'] in notice), '')
        for key in saved_environment:
            self.assertIn(json.dumps(saved_environment[key]), notice)
            self.assertIn(f'{key}={json.dumps(requested[key])}', notice)
        until(lambda: (directory / 'seen.json').exists())
        self.assertEqual(read(directory / 'seen.json'), saved_environment)
        self.assertEqual(read(directory / 'properties.json')['native_environment'], saved_environment)
