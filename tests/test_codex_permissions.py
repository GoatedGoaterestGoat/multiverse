"""Real Codex permission checks using a disposable home and local scripted model."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from multiverse import agents, harnesses
from multiverse.harnesses.codex import CodexAdapter
from multiverse.store import read, write, inbox
from multiverse.tools import send
from native_safety import isolated_home
from test_multiverse import TerminalBase, until


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class CodexPermissionTest(TerminalBase):
    def test_plain_full_access_survives_repeated_explicit_bridge_resume(self):
        completed, requests = set(), []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(value)
                index = len(requests)
                inputs = value.get('input', [])
                last_user = next((item for item in reversed(inputs) if item.get('role') == 'user'), {})
                stage = next((name for name in ('plain-wake', 'bridge-wake', 'extra', 'spawn', 'bridge', 'repeat')
                              if 'permission-probe-' + name in json.dumps(last_user)), 'spawn')
                title = 'title' in value.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
                item = {'type': 'message', 'role': 'assistant', 'id': f'msg-{index}',
                        'content': [{'type': 'output_text', 'text': '{"title":"Permission probe"}' if title else 'Done.'}]}
                if not title and (not inputs or inputs[-1].get('type') != 'function_call_output'):
                    code = ('import json,os,pathlib,socket; '
                            f'pathlib.Path(".git/mv2-{stage}").write_text("writable"); '
                            'sock=socket.socket(socket.AF_UNIX); sock.bind("permission-probe.sock"); '
                            'sock.close(); pathlib.Path("permission-probe.sock").unlink(); '
                            f'pathlib.Path("permission-{stage}.json").write_text(json.dumps('
                            '{"native_id":os.environ["CODEX_THREAD_ID"],"cwd":os.getcwd()}))')
                    command = ('"$MV2_CLI" session-bind --session-id "$CODEX_THREAD_ID"; '
                               + shlex.quote(sys.executable) + ' -c ' + shlex.quote(code))
                    item = {'type': 'function_call', 'call_id': f'call-{index}', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': command, 'yield_time_ms': 10000, 'max_output_tokens': 200})}
                elif not title:
                    completed.add(stage)
                events = [{'type': 'response.created', 'response': {'id': f'resp-{index}'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': f'resp-{index}',
                           'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        home = isolated_home(self.work / 'native-home')
        managed = home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        (home / 'config.toml').write_text(
            'model="gpt-6-sol"\nmodel_provider="offline"\n'
            '[model_providers.offline]\nname="Offline"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n')
        subprocess.run(['git', 'init', '-q', str(self.work)], check=True, timeout=10)
        env = {**os.environ, 'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': str(home),
               'NO_PROXY': '127.0.0.1,localhost', 'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '',
               'http_proxy': '', 'https_proxy': '', 'all_proxy': ''}
        self.addCleanup(lambda: subprocess.run(['codex', 'app-server', 'daemon', 'stop'],
                                               env=env, capture_output=True, timeout=20))
        config = self.root / 'harnesses/codex/harness.json'
        spec = read(config)
        spec['settings'] = {'args': ['--dangerously-bypass-approvals-and-sandbox'], 'spawn_args': ['--no-daemon']}
        write(config, spec)
        with patch.dict(os.environ, env):
            launched = agents.start(self.root, 'permissions', 'permission-probe-spawn', socket_path=self.socket)
        directory = agents.directory(self.root, launched['id'])

        def stop_owned():
            try:
                agents.terminal_action(self.root, launched['id'], 'stop')
            except (ValueError, RuntimeError):
                pass
            self.kill_server()
        self.addCleanup(stop_owned)

        try:
            until(lambda: (self.work / 'permission-spawn.json').exists() and 'spawn' in completed, seconds=30)
            native_id = read(directory / 'properties.json')['native_session_id']
            self.assertEqual(read(self.work / 'permission-spawn.json')['native_id'], native_id)
            for stage in ('plain-wake', 'bridge-wake', 'bridge', 'repeat'):
                agents.terminal_action(self.root, launched['id'], 'stop')
                spec['settings'] = {'args': ['--dangerously-bypass-approvals-and-sandbox']}
                if stage == 'plain-wake':
                    spec['settings']['inbox_bridge'] = False
                    spec['settings']['args'].append('--no-daemon')
                elif stage != 'bridge-wake':
                    spec['settings']['inbox_bridge'] = True
                write(config, spec)
                with patch.dict(os.environ, env):
                    if stage.endswith('-wake'):
                        with patch.object(CodexAdapter, 'send', side_effect=AssertionError('Wake must use the CLI prompt')):
                            delivered = send.run(self.root, agent=launched['id'], text='permission-probe-' + stage,
                                                 sender={'name': 'reviewer'}, delivery='follow-up')
                        self.assertTrue(delivered['ok'], delivered)
                        # Arrives before the TUI has bound/submitted its CLI prompt.
                        additional = send.run(self.root, agent=launched['id'],
                            text='permission-probe-extra ADDITIONAL_WAKE_INPUT',
                            sender={'name': 'reviewer'}, delivery='follow-up') if stage == 'bridge-wake' else None
                        if additional:
                            self.assertEqual(additional['state'], 'pending', additional)
                        resumed = agents.wait_ready(self.root, directory, agents.runtime(directory)['launch_id'],
                                                    harnesses.load(self.root, 'codex'), 30)
                        request = read(directory / 'launches' / resumed['runtime']['launch_id'] / 'request.json')
                        self.assertEqual(request['argv'][-1], 'Message from reviewer:\npermission-probe-' + stage)
                    else:
                        resumed = agents.start(self.root, resume=launched['id'], socket_path=self.socket,
                                               wait=True, wait_timeout=30)
                self.assertTrue(resumed['ready'], resumed)
                self.assertEqual(resumed['runtime']['state'], 'running')
                self.assertEqual(resumed['native_session_id'], native_id)
                if stage == 'bridge-wake':
                    self.assertEqual(resumed['runtime']['native_mode']['policy'], 'auto')
                    self.assertEqual(resumed['runtime']['native_mode']['mode'], 'bridge')
                if not stage.endswith('-wake'):
                    delivered = send.run(self.root, agent=launched['id'], text='permission-probe-' + stage, delivery='follow-up')
                    self.assertEqual(delivered['state'], 'native_queued', delivered)
                until(lambda: (self.work / f'permission-{stage}.json').exists() and stage in completed, seconds=30)
                self.assertEqual(read(self.work / f'permission-{stage}.json')['native_id'], native_id)
                self.assertEqual((self.work / f'.git/mv2-{stage}').read_text(), 'writable')
                self.assertEqual(agents.listing(self.root)[0]['live'], 'alive')
                self.assertEqual(read(directory / 'properties.json')['task'], 'permission-probe-spawn')
                if stage.endswith('-wake'):
                    receipt = next(m['tracking'] for m in inbox(directory, all_messages=True)['messages']
                                   if m['text'] == 'permission-probe-' + stage)
                    self.assertEqual(receipt['submission_state'], 'accepted')
                    self.assertEqual(receipt['delivery_method'], 'resume_prompt')
                    self.assertIsNone(receipt['native_receipt']['transcript_confirmed'])
                if stage == 'bridge-wake':
                    until(lambda: 'extra' in completed and (self.work / 'permission-extra.json').exists(), seconds=20)
                    original_requests = [r for r in requests if 'permission-probe-bridge-wake' in json.dumps(r)]
                    self.assertNotIn('ADDITIONAL_WAKE_INPUT', json.dumps(original_requests[0]))
            print('Codex permission evidence: plain -> plain wake -> bridge wake -> bridge resume twice; '
                  '.git writes and Unix sockets succeed in the same native thread')
        except Exception:
            print('PERMISSION RUNTIME', agents.runtime(directory))
            try:
                print(agents.terminal_action(self.root, launched['id'], 'inspect'))
            except Exception:
                pass
            raise


if __name__ == '__main__':
    unittest.main()
