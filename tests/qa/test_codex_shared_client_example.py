"""Opt-in, real-Codex black-box check of the documented shared-client example.

MV2_QA_REAL_CODEX=1 <python> tests/run.py qa.test_codex_shared_client_example -v
Optional: MV2_QA_AUTH_FILE, MV2_QA_EVIDENCE (persistent output directory).
For the storage-only scenario, MV2_QA_MODEL_FREE=1 uses an unauthenticated,
unreachable localhost provider; it tests launch/resume without a model call.
Uses only public CLIs, tmux, and documented properties/runtime files.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get('MV2_QA_REAL_CODEX') == '1', 'requires real Codex opt-in')
class CodexSharedClientExample(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix='qacxs-', dir='/tmp')).resolve()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.work = self.scratch / 'work'
        self.work.mkdir()
        home = self.scratch / 'home'
        self.codex_home = home / '.codex'
        self.codex_home.mkdir(parents=True)
        model_free = os.environ.get('MV2_QA_MODEL_FREE') == '1'
        if not model_free:
            auth = Path(os.environ.get('MV2_QA_AUTH_FILE', str(Path.home() / '.codex/auth.json')))
            if not auth.is_file():
                self.skipTest('no Codex authentication fixture')
            shutil.copyfile(auth, self.codex_home / 'auth.json')
        self.env = {k:v for k,v in os.environ.items()
                    if not k.startswith(('MV2_', 'CODEX_')) and k not in ('TMUX', 'TMUX_PANE')}
        self.env.update(HOME=str(home), CODEX_HOME=str(self.codex_home),
                        CLAUDE_CONFIG_DIR=str(home / '.claude'),
                        MV2_TMUX_SOCKET=str(self.scratch / 'a.sock'))
        self.evidence = Path(os.environ.get('MV2_QA_EVIDENCE', str(self.scratch / 'evidence')))
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.log = self.evidence / (self.scratch.name + '.jsonl')
        self.run_cli('mv2', 'init', str(self.work))
        self.addCleanup(self.cleanup_native)
        (self.work / '.multiverse/bootstrap.md').write_text('Disposable QA fixture. Await a task. Never inspect product implementation.\n')
        (self.work / '.multiverse/harnesses/codex/harness.json').write_text(json.dumps({
            'schema': 2, 'adapter': 'codex', 'settings': {
                'args': ['--dangerously-bypass-approvals-and-sandbox']}}))
        model = ('model="fixture"\nmodel_provider="fixture"\n'
                 '[model_providers.fixture]\nname="Private QA fixture"\n'
                 'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
                 if model_free else 'model="gpt-6-luna"\nmodel_reasoning_effort="low"\n')
        (self.codex_home / 'config.toml').write_text(
            'check_for_update_on_startup=false\n' + model +
            '[projects.' + json.dumps(str(self.work)) + ']\ntrust_level="trusted"\n')
        daemon_settings = self.codex_home / 'app-server-daemon/settings.json'
        daemon_settings.parent.mkdir(parents=True, exist_ok=True)
        daemon_settings.write_text(json.dumps({'updater': {'autoUpdateEnabled': False}}))

    def run_cli(self, *argv, env=None, timeout=65):
        start = time.monotonic()
        result = subprocess.run(argv, cwd=self.work, env=env or self.env, capture_output=True,
                                text=True, timeout=timeout)
        with self.log.open('a') as f:
            f.write(json.dumps(dict(argv=argv, rc=result.returncode, stdout=result.stdout,
                                    stderr=result.stderr, seconds=time.monotonic()-start))+'\n')
        return result

    def cleanup_native(self):
        for socket in ('plain.sock', 'shared.sock'):
            self.run_cli('tmux', '-S', str(self.scratch/socket), 'kill-server')
        self.run_cli('mv2', 'kill', 'fixture')
        self.run_cli('tmux', '-S', self.env['MV2_TMUX_SOCKET'], 'kill-server')
        # Codex 0.163.0 may leave its private pid-update-loop after `daemon stop`.
        # Remember only helpers under this test's unique home, then verify exact
        # PID/start time/command again before ending them. Never use global kill patterns.
        processes = subprocess.run(['ps', '-axo', 'pid=,lstart=,command='],
                                   capture_output=True, text=True, check=True).stdout.splitlines()
        owned = [line.strip() for line in processes
                 if str(self.codex_home)+'/packages/app-server-daemon/' in line
                 and line.rstrip().endswith('codex app-server daemon pid-update-loop')]
        self.run_cli('codex', 'app-server', 'daemon', 'stop')
        ended = []
        for identity in owned:
            pid = int(identity.split()[0])
            current = subprocess.run(['ps', '-p', str(pid), '-o', 'pid=,lstart=,command='],
                                     capture_output=True, text=True).stdout.strip()
            if current == identity:
                os.kill(pid, signal.SIGTERM)
                ended.append(pid)
        with self.log.open('a') as f:
            f.write(json.dumps({'cleanup_owned_native_helper_pids': ended})+'\n')

    def client_screen(self, session_id, remote):
        socket = self.scratch / ('shared.sock' if remote else 'plain.sock')
        command = ['codex', 'resume']
        if remote:
            command += ['--remote', 'unix://']
        command += [session_id]
        # New server inherits only the fixture environment. No launch identity is copied.
        result = self.run_cli('tmux', '-S', str(socket), 'new-session', '-d', '-s', 'client',
                              '-x', '120', '-y', '36', shlex.join(command))
        self.assertEqual(result.returncode, 0, result.stderr)
        end = time.monotonic() + 20
        screen = ''
        while time.monotonic() < end:
            result = self.run_cli('tmux', '-S', str(socket), 'capture-pane', '-p', '-t', 'client')
            screen = result.stdout
            if 'This conversation is open in another app' in screen:
                break
            if ('Ask Codex to do anything' in screen and 'fixture' in screen
                    and 'Resuming session' not in screen and 'loading' not in screen):
                break
            time.sleep(.25)
        (self.evidence/(self.scratch.name + ('-shared.txt' if remote else '-plain.txt'))).write_text(screen)
        self.run_cli('tmux', '-S', str(socket), 'kill-server')
        return screen

    def test_documented_codex_resume_example_shares_same_home_conversation(self):
        """docs/use/harnesses.md, Codex/Shared conversations: `codex resume` in another window."""
        result = self.run_cli('mv2', 'spawn', '--name', 'fixture', '--task', 'Reply SAVED_THREAD_READY only.',
                              '--wait', '--wait-timeout', '45', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads(result.stdout)
        self.assertTrue(launch['ready'])
        self.assertEqual(launch['runtime']['native_mode']['mode'], 'bridge')
        props = json.loads((self.work / '.multiverse/state/agents' / launch['id'] / 'properties.json').read_text())
        sid = props['native_session_id']
        self.assertTrue(sid)
        end = time.monotonic() + 30
        while time.monotonic() < end:
            listing = self.run_cli('mv2', 'list', '--json')
            if json.loads(listing.stdout)[0]['status'] == 'idle':
                break
            time.sleep(.5)
        plain = self.client_screen(sid, remote=False)
        shared = self.client_screen(sid, remote=True)
        self.assertNotIn('Resuming session', shared)
        self.assertNotIn('loading', shared)
        self.assertIn('Ask Codex to do anything', shared, 'explicit same-daemon control did not attach')
        self.assertNotIn('This conversation is open in another app', shared)
        self.assertNotIn('This conversation is open in another app', plain,
                         'documented codex resume example opens a different server; explicit --remote unix:// succeeds')
        self.assertIn('Ask Codex to do anything', plain)

    def test_resume_under_different_codex_home_keeps_saved_home_and_explains_it(self):
        """The saved conversation wins over a caller's different CODEX_HOME."""
        result = self.run_cli('mv2', 'spawn', '--name', 'fixture', '--wait', '--wait-timeout', '45', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads(result.stdout)
        properties = self.work / '.multiverse/state/agents' / launch['id'] / 'properties.json'
        original = json.loads(properties.read_text())
        result = self.run_cli('mv2', 'kill', 'fixture')
        self.assertEqual(result.returncode, 0, result.stderr)
        other = self.scratch / 'different-codex-home'
        other.mkdir()
        result = self.run_cli('mv2', 'resume', 'fixture', '--wait', '--wait-timeout', '30',
                              env={**self.env, 'CODEX_HOME': str(other)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ready', result.stdout)
        self.assertIn('Kept its saved Codex home ' + json.dumps(str(self.codex_home)), result.stdout)
        self.assertIn('ignored CODEX_HOME=' + json.dumps(str(other)), result.stdout)
        resumed = json.loads(properties.read_text())
        self.assertEqual(resumed['native_session_id'], original['native_session_id'])
        self.assertEqual(resumed['native_environment']['CODEX_HOME'], str(self.codex_home))
        self.assertEqual(resumed['native_service_identity'], original['native_service_identity'])
        self.assertFalse((other / 'app-server-daemon').exists())

    def test_follow_up_runs_after_native_escape_interrupt(self):
        """docs/cli/messaging.md: follow-up arrives after the current turn ends (including interrupt)."""
        result = self.run_cli('mv2', 'spawn', '--name', 'fixture', '--task',
                              'Run exactly one shell command: sleep 10. Wait for it, then reply FIRST_DONE.',
                              '--wait', '--wait-timeout', '45', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads(result.stdout)
        runtime = json.loads((self.work / '.multiverse/state/agents' / launch['id'] / 'runtime.json').read_text())
        socket, pane = runtime['tmux']['socket'], runtime['tmux']['pane']
        end = time.monotonic() + 20
        active = False
        while time.monotonic() < end:
            result = self.run_cli('tmux', '-S', socket, 'capture-pane', '-p', '-t', pane)
            if 'background terminal running' in result.stdout or 'Running sleep 10' in result.stdout:
                active = True
                break
            time.sleep(.2)
        self.assertTrue(active, result.stdout)
        result = self.run_cli('mv2', 'send', 'fixture', '--delivery', 'follow-up', '--text',
                              'Use a shell command to create follow-up-marker.txt containing FOLLOWED. Do it now, then reply DONE.')
        self.assertEqual(result.returncode, 0, result.stderr)
        tags = self.run_cli('tmux', '-S', socket, 'show-options', '-p', '-t', pane).stdout
        self.assertIn(launch['id'], tags)
        self.assertIn(runtime['launch_id'], tags)
        self.run_cli('tmux', '-S', socket, 'send-keys', '-t', pane, 'Escape')
        end = time.monotonic() + 30
        marker = self.work / 'follow-up-marker.txt'
        while time.monotonic() < end and not marker.exists():
            time.sleep(.25)
        screen = self.run_cli('tmux', '-S', socket, 'capture-pane', '-p', '-t', pane).stdout
        (self.evidence/(self.scratch.name+'-interrupted.txt')).write_text(screen)
        self.run_cli('mv2', 'list', '--json')
        self.run_cli('mv2', 'inbox', '--agent', 'fixture', '--all', '--json')
        self.run_cli('mv2', 'deliver', 'fixture', '--check')
        self.run_cli('mv2', 'deliver', 'fixture', '--retry')
        self.assertTrue(marker.exists(), 'accepted follow-up did not run within 30s after Escape:\n'+screen)


if __name__ == '__main__':
    unittest.main()
