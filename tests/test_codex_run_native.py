"""Checked Codex in private homes against a local scripted provider; no real credentials or model spend."""
import base64
from datetime import datetime, timezone
import http.server
import json
import os
from pathlib import Path
import shutil
import threading
from unittest.mock import patch

import native_safety
from multiverse import harnesses
from multiverse.harnesses.codex import run
from multiverse.harnesses.codex.ask import AskRequest
from multiverse.harnesses.codex.plugins import codex_version
from multiverse.harnesses.types import RunRequest, RunRow
from multiverse.runtime import runs
from test_multiverse import BaseTest

native_safety.install()


class CodexPrivateNativeTest(BaseTest):
    def test_native_fresh_and_real_tool_history_requests_declare_zero_tools(self):
        """Configuration and copied native history can restore hidden tools even when the public settings look disabled."""
        binary = shutil.which('codex')
        if not binary or codex_version(binary, str(self.work)) not in run.VERSIONS:
            self.skipTest('Requires a separately checked Codex binary')
        source = native_safety.isolated_home(self.work / 'source-home', config='''
cli_auth_credentials_store = "file"
[mcp_servers.never_start]
command = "this-mcp-command-must-never-start"
''')
        def jwt(claims):
            data = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
            return 'eyJhbGciOiJub25lIn0.' + data + '.test-signature'
        token = jwt({'exp': 4102444800, 'email': 'test@example.invalid', 'https://api.openai.com/auth': {
            'chatgpt_account_id': 'test-account', 'chatgpt_user_id': 'test-user', 'chatgpt_plan_type': 'plus'}})
        (source / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {
            'id_token': token, 'access_token': token, 'refresh_token': 'test-refresh', 'account_id': 'test-account'},
            'last_refresh': datetime.now(timezone.utc).isoformat()}))
        (Path.home() / 'AGENTS.md').write_text('DISCOVERY_MUST_NOT_APPEAR')
        captured = []
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_GET(self):
                if '/accounts/check' in self.path:
                    body = json.dumps({'accounts': [{'id': 'test-account', 'workspace_backend_origin': 'https://chatgpt.com',
                                                     'account_routing_override': 'NO_CONSTRAINT'}]}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(426)
                self.send_header('Content-Length', '0')
                self.end_headers()
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                captured.append(json.loads(raw))
                message = {'id': 'msg_answer', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                           'content': [{'type': 'output_text', 'text': 'PRIVATE_OK', 'annotations': []}]}
                frames = [{'type': 'response.created', 'response': {'id': 'resp_test', 'status': 'in_progress'}},
                          {'type': 'response.output_item.added', 'output_index': 0,
                           'item': {**message, 'status': 'in_progress', 'content': []}},
                          {'type': 'response.output_text.delta', 'item_id': 'msg_answer', 'output_index': 0,
                           'content_index': 0, 'delta': 'PRIVATE_OK'},
                          {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                          {'type': 'response.completed', 'response': {'id': 'resp_test', 'status': 'completed',
                           'output': [message], 'usage': {'input_tokens': 33, 'output_tokens': 2, 'total_tokens': 35,
                           'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}}]
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                self.wfile.write(''.join('event: ' + value['type'] + '\ndata: ' + json.dumps(value) + '\n\n'
                                        for value in frames).encode())
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        configuration = run.configuration
        def local(**kwargs):
            return {**configuration(**kwargs), 'openai_base_url': f'http://127.0.0.1:{server.server_port}',
                    'chatgpt_base_url': f'http://127.0.0.1:{server.server_port}/backend-api/',
                    'features.enable_request_compression': False}
        adapter = harnesses.load(self.root, 'codex')
        fixture = Path(__file__).parent / 'fixtures/codex/rollout-0.166.0.jsonl'
        original = fixture.read_bytes()
        history = [json.loads(line) for line in original.splitlines()]
        history[0]['payload']['dynamic_tools'] = [{'name': 'must_not_be_offered', 'input_schema': {'type': 'object'}}]
        history.append({'type': 'dynamic_tools_updated', 'payload': {'tools': [{'name': 'must_not_be_offered'}]}})
        # The public reader already selects history; retain actual native tool calls/results
        # here while excluding rollback markers (the converter must not apply them again).
        requests = [RunRequest(messages=(), prompt='Reply briefly.', system='Plain text only.', model='gpt-5.5', effort='low'),
                    AskRequest(messages=tuple(RunRow(format=run.FORMAT, data=value) for value in history),
                               prompt='Reply briefly.', system='Plain text only.', model='gpt-6-sol', effort='low'),
                    RunRequest(messages=tuple(RunRow(format=run.FORMAT, data=value) for value in [
                        {'type': 'session_meta', 'payload': {'creator_account_id': 'another-account'}},
                        {'type': 'turn_context', 'payload': {'model': 'gpt-6-sol'}},
                        {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': [],
                                                            'encrypted_content': 'MUST_BE_DROPPED'}},
                        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': 'RETAINED_PLAIN_HISTORY'}]}}]),
                               prompt='Reply briefly.', system='Plain text only.', model='gpt-6-sol', effort='low')]
        with patch.object(run, 'configuration', side_effect=local):
            for request in requests:
                reply = runs.run_once(adapter, request, root=self.root, plugin='native-test', timeout=30,
                                     environment={'HOME': str(Path.home()), 'CODEX_HOME': str(source), 'PATH': os.environ['PATH']})
                self.assertEqual(reply.text, 'PRIVATE_OK')
                self.assertEqual(reply.usage['input'], 33)
                self.assertFalse(list(runs.base().rglob('run.json')))
        self.assertEqual(len(captured), 3)
        for request in captured:
            self.assertFalse(request.get('tools'))
            self.assertFalse(request.get('store'))
            self.assertNotIn('DISCOVERY_MUST_NOT_APPEAR', json.dumps(request))
            for item in request.get('input', []):
                if item.get('type') in ('additional_tools', 'tool_search_output'):
                    self.assertFalse(item.get('tools'))
            self.assertNotIn('must_not_be_offered', json.dumps(request))
        types = [item.get('type') for item in captured[1].get('input', [])]
        self.assertIn('function_call', types)
        self.assertIn('function_call_output', types)
        self.assertNotIn('MUST_BE_DROPPED', json.dumps(captured[2]))
        self.assertIn('RETAINED_PLAIN_HISTORY', json.dumps(captured[2]))
        self.assertTrue(reply.lost)
        self.assertEqual(fixture.read_bytes(), original)
