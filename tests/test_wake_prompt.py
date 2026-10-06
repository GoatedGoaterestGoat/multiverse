"""Real interactive resume prompts, using only local scripted providers."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from multiverse.universe.mailbox import inbox
from multiverse.tools import send
from test_multiverse import TerminalBase, until


@unittest.skipUnless(shutil.which('pi') and shutil.which('tmux'), 'Pi and tmux required')
class PiWakePromptTest(TerminalBase):
    def test_plain_pi_resume_submits_literal_prompt_and_stays_interactive(self):
        path = self.root / 'harnesses/pi/harness.json'
        spec = read(path)
        spec['settings']['args'] = ['--offline', '--no-extensions', '--no-skills', '--no-context-files',
            '--no-prompt-templates', '--no-approve', '--extension', str(Path(__file__).with_name('mock-provider.ts')),
            '--provider', 'mv2-test', '--model', 'mv2-test']
        write(path, spec)
        env = {'PI_CODING_AGENT_DIR': str(self.work / 'pi-home'), 'PI_OFFLINE': '1'}
        with patch.dict(os.environ, env):
            first = lifecycle.start(self.root, 'pi-wake', 'Seed conversation', 'pi', socket_path=self.socket)
        directory = agents.directory(self.root, first['id'])
        requests = directory / 'mock-requests.jsonl'
        until(requests.exists, seconds=25)
        native_id = json.loads((directory / 'session.jsonl').read_text().splitlines()[0])['id']
        lifecycle.kill(self.root, first['id'])
        prompt = 'Message from reviewer:\nPI_WAKE_SENTINEL'
        with patch.dict(os.environ, env):
            sent = send.run(self.root, agent=first['id'], text='PI_WAKE_SENTINEL', sender={'name': 'reviewer'})
        self.assertTrue(sent['ok'], sent)
        resumed = lifecycle.launch_result(self.root, directory)
        until(lambda: 'PI_WAKE_SENTINEL' in requests.read_text(), seconds=25)
        last = json.loads(requests.read_text().splitlines()[-1])
        self.assertEqual(last['messages'][-1]['content'][0]['text'], prompt)
        self.assertEqual(json.loads((directory / 'session.jsonl').read_text().splitlines()[0])['id'], native_id)
        self.assertEqual(read(directory / 'properties.json')['task'], 'Seed conversation')
        self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'alive')
        self.assertNotEqual(resumed['runtime']['launch_id'], first['runtime']['launch_id'])
        saved = inbox(directory, all_messages=True)['messages'][-1]
        self.assertEqual(saved['tracking']['submission_state'], 'accepted')
        self.assertEqual(saved['tracking']['delivery_method'], 'resume_prompt')
        self.assertIsNone(saved['tracking']['native_receipt']['transcript_confirmed'])
        # A bridged wake holds a concurrent second message until registration.
        lifecycle.kill(self.root, first['id'])
        spec['settings']['inbox_bridge'] = True
        write(path, spec)
        with patch.dict(os.environ, env):
            sent = send.run(self.root, agent=first['id'], text='PI_BRIDGE_WAKE', sender={'name': 'reviewer'})
            second = send.run(self.root, agent=first['id'], text='PI_AFTER_WAKE', delivery='follow-up')
        self.assertTrue(sent['ok'], sent)
        self.assertTrue(second['ok'], second)
        until(lambda: 'PI_AFTER_WAKE' in requests.read_text(), seconds=25)
        submitted = [json.loads(line) for line in requests.read_text().splitlines()]
        wake_index = next(i for i, request in enumerate(submitted) if 'PI_BRIDGE_WAKE' in json.dumps(request))
        next_index = next(i for i, request in enumerate(submitted) if 'PI_AFTER_WAKE' in json.dumps(request))
        self.assertGreater(next_index, wake_index)
        self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'alive')


@unittest.skipUnless(shutil.which('claude') and shutil.which('tmux'), 'Claude and tmux required')
class ClaudeWakePromptTest(TerminalBase):
    def test_plain_claude_resume_submits_prompt_and_stays_interactive(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'count_tokens' in self.path:
                    payload = json.dumps({'input_tokens': 1}).encode()
                    content_type = 'application/json'
                else:
                    seen.append(request)
                    message = {'id': f'msg_{len(seen)}', 'type': 'message', 'role': 'assistant',
                        'model': request.get('model', 'claude-sonnet-4-6'), 'content': [], 'stop_reason': None,
                        'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 0}}
                    events = [
                        {'type': 'message_start', 'message': message},
                        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Local test completed.'}},
                        {'type': 'content_block_stop', 'index': 0},
                        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 1}},
                        {'type': 'message_stop'}]
                    payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                    content_type = 'text/event-stream'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        provider = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        config = self.work / 'claude-home'
        config.mkdir()
        write(config / '.claude.json', {'hasCompletedOnboarding': True, 'theme': 'dark',
            'projects': {str(self.work): {'hasTrustDialogAccepted': True, 'allowedTools': []}}})
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC_', 'CLAUDE', 'MV2_', 'OPENAI_', 'CODEX_'))}
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_API_KEY='local-test-key',
                   ANTHROPIC_BASE_URL=f'http://127.0.0.1:{provider.server_port}',
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_TELEMETRY='1',
                   HTTP_PROXY='', HTTPS_PROXY='', ALL_PROXY='', http_proxy='', https_proxy='', all_proxy='',
                   NO_PROXY='127.0.0.1,localhost', MV2_TMUX_SOCKET=self.socket)
        native_id = str(uuid4())
        # Print mode is used only to seed disposable native history. The resumed
        # process below is interactive and must remain alive after its prompt.
        seeded = subprocess.run(['claude', '--bare', '--model', 'claude-sonnet-4-6', '--session-id', native_id,
            '--print', 'Seed conversation'], cwd=self.work, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        path = self.root / 'harnesses/claude/harness.json'
        spec = read(path)
        spec['settings']['args'] = ['--bare']
        write(path, spec)
        agent_id = str(uuid4())
        directory = self.root / 'state/agents' / agent_id
        write(directory / 'properties.json', {'id': agent_id, 'universe_id': read(self.root / 'universe.json')['id'],
            'name': 'claude-wake', 'task': 'Seed conversation', 'workspace': str(self.work),
            'harness': 'claude', 'model': 'claude-sonnet-4-6', 'native_session_id': native_id})
        prompt = 'Message from reviewer:\nCLAUDE_WAKE_SENTINEL'
        with patch.dict(os.environ, env, clear=True):
            sent = send.run(self.root, agent=agent_id, text='CLAUDE_WAKE_SENTINEL', sender={'name': 'reviewer'})
        self.assertTrue(sent['ok'], sent)
        resumed = lifecycle.launch_result(self.root, directory)
        try:
            consented = False
            def submitted():
                nonlocal consented
                if any('CLAUDE_WAKE_SENTINEL' in json.dumps(request) for request in seen):
                    return True
                screen = self.backend.run('capture-pane', '-p', '-t', resumed['runtime']['tmux']['pane'])
                if not consented and 'Detected a custom API key' in screen and 'local-test-key' in screen:
                    # Only this disposable terminal and its loopback test key.
                    self.backend.run('send-keys', '-t', resumed['runtime']['tmux']['pane'], 'Up', 'Enter')
                    consented = True
                return False
            until(submitted, seconds=25)
            self.assertEqual(resumed['native_session_id'], native_id)
            self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'alive')
            self.assertTrue(any(message.get('role') == 'user' and any(block.get('text') == prompt
                for block in message.get('content', []) if isinstance(block, dict))
                for request in seen for message in request.get('messages', [])))
        except Exception:
            print(self.backend.run('capture-pane', '-p', '-S', '-100', '-t', resumed['runtime']['tmux']['pane']))
            raise
