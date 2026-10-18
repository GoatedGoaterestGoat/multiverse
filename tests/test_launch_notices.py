"""Provider resume explanations survive both immediate and waited launch results."""
import os
import json
import io
from contextlib import redirect_stdout
import subprocess
import sys
from uuid import uuid4
import unittest
from unittest.mock import patch

from multiverse import display
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from test_native_backends import NativeBackendTest
from test_multiverse import TerminalBase, ROOT, until
from multiverse.tools import send


class LaunchNoticeTest(NativeBackendTest):
    def test_notice_uses_caller_and_saved_environment_and_stays_one_line(self):
        seen = []
        notice = 'Kept saved fixture home;\nignored caller override.'
        def describe(session, caller, effective):
            seen.append((session, dict(caller), dict(effective)))
            return notice
        with patch.object(self.adapter, 'resume_notice', side_effect=describe, create=True), \
                patch.dict(os.environ, {'TEST_NATIVE_HOME': str(self.work / 'different')}):
            result = lifecycle.start(self.root, resume=self.result['id'], socket_path='/fake')
        self.assertEqual(result['notices'], ['Kept saved fixture home; ignored caller override.'])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1]['TEST_NATIVE_HOME'], str(self.work / 'different'))
        self.assertEqual(seen[0][2]['TEST_NATIVE_HOME'], str(self.adapter.home))
        self.assertEqual(seen[0][0].native_id, self.properties['native_session_id'])
        self.assertEqual(read(self.directory / 'properties.json')['native_service_identity'],
                         self.properties['native_service_identity'])
        request = read(self.directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
        self.assertEqual(request['notices'], result['notices'])
        text = display.outcome({**result, 'ready': True}, 'Resumed', 'did not resume')
        self.assertEqual(text.splitlines()[-1], result['notices'][0])
        self.assertEqual(len(text.splitlines()), 2)
        with patch.object(self.adapter, 'resume_notice', return_value='Waited launch notice', create=True), \
                patch.object(lifecycle, 'wait_ready', side_effect=lambda root, directory, *args:
                             {**lifecycle.launch_result(root, directory), 'ready': True, 'notices': ['Screen unverified.']}):
            waited = lifecycle.start(self.root, resume=self.result['id'], socket_path='/fake', wait=True)
        self.assertEqual(waited['notices'], ['Screen unverified.', 'Waited launch notice'])

    def test_missing_invalid_or_failed_optional_notices_never_change_native_scope(self):
        for answer in (None, '', [], 3, ValueError('optional diagnostic failed')):
            with self.subTest(answer=answer):
                options = {'side_effect': answer} if isinstance(answer, Exception) else {'return_value': answer}
                with patch.object(self.adapter, 'resume_notice', create=True, **options):
                    result = lifecycle.start(self.root, resume=self.result['id'], socket_path='/fake')
                self.assertEqual(result.get('notices', []), [])
                self.assertEqual(read(self.directory / 'properties.json')['native_service_identity'],
                                 self.properties['native_service_identity'])
        self.assertEqual(display.outcome({**result, 'notices': ['x\ny', None]}, 'Resumed', 'did not resume').splitlines()[-1], 'x y')

    def test_message_wake_returns_resume_notice_in_human_and_json_results(self):
        with patch.object(self.adapter, 'resume_notice', return_value='Kept saved fixture storage.', create=True), \
                patch.object(native, 'observe', return_value={'live': 'dormant'}):
            output = io.StringIO()
            with redirect_stdout(output):
                send.cli_run(self.root, agent=self.result['id'], text='wake up')
            self.assertEqual(output.getvalue().splitlines()[-1], 'Kept saved fixture storage.')
            result = send.cli_run(self.root, agent=self.result['id'], text='second wake', json=True)
        self.assertEqual(result['notices'], ['Kept saved fixture storage.'])


class LaunchNoticeCliTest(TerminalBase):
    def test_real_cli_resume_prints_and_serializes_saved_home_notice(self):
        executable = self.work / 'codex-stand-in'
        executable.write_text(f'#!{sys.executable}\n'
            'import os,sys,time,json\nfrom pathlib import Path\n'
            'if sys.argv[1:] == ["--version"]: print("codex-cli 0.164.0"); sys.exit()\n'
            'Path(os.environ["MV2_AGENT_DIR"], "seen.json").write_text(json.dumps({k: os.environ.get(k) for k in ("CODEX_HOME", "CODEX_SQLITE_HOME")}))\n'
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
        environment = {**os.environ, 'CODEX_HOME': str(self.work / 'requested-home'),
                       'MV2_TMUX_SOCKET': self.socket, 'PYTHONPATH': str(ROOT / 'src')}
        for options, keys in (([], ('CODEX_HOME',)), (['--wait'], ('CODEX_SQLITE_HOME',)),
                              (['--json'], ('CODEX_HOME', 'CODEX_SQLITE_HOME')),
                              (['--wait', '--json'], ('CODEX_SQLITE_HOME',))):
            environment.update(saved_environment)
            for key in keys:
                environment[key] = str(self.work / ('requested-' + key))
            labels = {'CODEX_HOME': 'Codex home', 'CODEX_SQLITE_HOME': 'Codex database home'}
            expected = ' '.join(f'Kept its saved {labels[key]} {json.dumps(saved_environment[key])}; '
                                f'ignored {key}={json.dumps(environment[key])}.' for key in keys)
            with self.subTest(options=options):
                process = subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root),
                                          'resume', properties['id'], *options], env=environment,
                                         capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
                self.assertEqual(process.returncode, 0, process.stderr)
                if '--json' in options:
                    result = json.loads(process.stdout)
                    self.assertEqual(result['notices'], [expected])
                    self.assertEqual(result['native_session_id'], properties['native_session_id'])
                else:
                    self.assertEqual(process.stdout.splitlines()[-1], expected)
                    self.assertEqual(len(process.stdout.splitlines()), 2)
                    self.assertIn(': ready.' if '--wait' in options else ').', process.stdout.splitlines()[0])
                until(lambda: (directory / 'seen.json').exists())
                self.assertEqual(read(directory / 'seen.json'), saved_environment)
                self.assertEqual(read(directory / 'properties.json')['native_environment'], saved_environment)
                lifecycle.kill(self.root, properties['id'], attachment_only=True)
                (directory / 'seen.json').unlink()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (LaunchNoticeTest, LaunchNoticeCliTest)
                              for name in cls.__dict__ if name.startswith('test_'))
