"""A native Escape must not strand mv2's accepted after-turn input."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from native_safety import install, isolated_home
from native_processes import PrivateDaemonGroups
from multiverse.runtime import lifecycle, native, rpc
from multiverse.store import read, write
from multiverse.harnesses.types import RpcRequest, RpcNotification, RpcSpec, NativeResult
from multiverse.harnesses.codex.protocol import result as native_result
from multiverse.terminal.tmux import owned
from multiverse.tools import deliver, send
from multiverse.universe import agents
from multiverse.universe.mailbox import inbox
from test_multiverse import TerminalBase, until

install()


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class NativeQueueContinuationTest(TerminalBase):
    def test_native_interrupt_pauses_across_resume_until_new_input_then_drains_fifo(self):
        """Native interrupt pause must survive attachment resume and restart FIFO exactly once for typed or mv2 input; queue-state races otherwise lose or reorder accepted messages silently."""
        if subprocess.check_output(['codex', '--version'], stdin=subprocess.DEVNULL,
                                   text=True).strip() != 'codex-cli 0.164.0':
            self.skipTest('native queue continuation evidence pinned to 0.164.0')
        work = self.work
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if not self.path.endswith('/responses'):
                    self.send_error(404)
                    return
                title = 'title' in body.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
                if not title:
                    requests.append(body)
                index = len(requests)
                item = {'type': 'message', 'role': 'assistant', 'id': 'msg-' + str(index),
                        'content': [{'type': 'output_text', 'text': '{"title":"Private queue test"}' if title else 'Done.'}]}
                inputs = body.get('input', [])
                latest = next((json.dumps(value) for value in reversed(inputs) if value.get('role') == 'user'), '')
                pending_tool = not inputs or inputs[-1].get('type') != 'function_call_output'
                if not title and pending_tool:
                    if index == 1:
                        command = 'touch initial-tool-started; sleep 30'
                    elif 'HOLD_AGAIN' in latest:
                        command = 'touch second-tool-started; sleep 30'
                    else:
                        label = next((key for key in ('QUEUED_FOREIGN', 'FOLLOW_UP_AFTER_NATIVE_ESCAPE',
                                     'NEW_INPUT_RESUMES_FIFO', 'TYPED_RESTART', 'AFTER_TYPED') if key in latest), None)
                        command = f'echo {label} >> order.log; touch {label}' if label else None
                    if command:
                        item = {'type': 'function_call', 'call_id': 'tool-' + str(index), 'name': 'exec_command',
                                'arguments': json.dumps({'cmd': command, 'yield_time_ms': 30000, 'max_output_tokens': 200})}
                events = [{'type': 'response.created', 'response': {'id': 'resp-' + str(index)}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': 'resp-' + str(index),
                           'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
                payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:
                    pass  # Escape can cancel the original HTTP request.

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        home = isolated_home(work / 'native-home', config=
            'model="gpt-6-sol"\nmodel_provider="fixture"\n'
            '[model_providers.fixture]\nname="Private queue fixture"\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api="responses"\nrequires_openai_auth=false\n'
            f'[projects.{json.dumps(str(work))}]\ntrust_level="trusted"\n')
        managed = home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex', 'settings': {
            'inbox_bridge': True, 'spawn_args': ['--sandbox', 'workspace-write', '--ask-for-approval', 'never']}})
        test_env = {'CODEX_HOME': str(home), 'NO_PROXY': '127.0.0.1,localhost',
                    **{key: '' for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                          'http_proxy', 'https_proxy', 'all_proxy')}}
        daemon_env = {**os.environ, **test_env}
        groups = PrivateDaemonGroups()
        self.enterContext(groups.observe_starts())
        def stop_daemon():
            try:
                subprocess.run(['codex', 'app-server', 'daemon', 'stop'], env=daemon_env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=True)
            finally:
                groups.close()
        self.addCleanup(stop_daemon)
        result = None
        def stop_agent():
            if result is not None:
                lifecycle.kill(self.root, result['id'])
        self.addCleanup(stop_agent)
        with patch.dict(os.environ, test_env):
            result = lifecycle.start(self.root, 'native-escape', 'Run the private initial tool.',
                                     'codex', socket_path=self.socket, wait=True, wait_timeout=30)
        self.assertTrue(result['ready'], result)
        directory = agents.directory(self.root, result['id'])
        until(lambda: (work / 'initial-tool-started').exists(), seconds=30)
        primary = read(directory / 'properties.json')['native_session_id']
        # One native-owned older item must not make a new mv2 message skip it.
        def native_queue():
            response = native_result((yield RpcRequest('initialize', {
                'clientInfo': {'name': 'private-queue-proof', 'version': '1'},
                'capabilities': {'experimentalApi': True}},
                on_success=(RpcNotification('initialized', {}),))))
            self.assertIsInstance(response, dict)
            response = native_result((yield RpcRequest('thread/queue/add', {
                'threadId': primary, 'clientUserMessageId': str(uuid4()),
                'input': [{'type': 'text', 'text': 'QUEUED_FOREIGN', 'text_elements': []}]})))
            self.assertIsInstance(response, dict)
            return NativeResult('ok', value=response)
        endpoint = native.context(self.root, result['id'])[2].endpoint.address
        self.assertEqual(rpc.execute(RpcSpec(endpoint, native_queue)).state, 'ok')
        queued = send.run(self.root, agent=result['id'], text='FOLLOW_UP_AFTER_NATIVE_ESCAPE', delivery='follow-up')
        self.assertEqual(queued['state'], 'native_queued', queued)
        def message_id(text):
            return next(message['id'] for message in inbox(directory, all_messages=True)['messages']
                        if message['text'] == text)
        waiting_id = message_id('FOLLOW_UP_AFTER_NATIVE_ESCAPE')
        def interrupt():
            facts = agents.runtime(directory)
            backend, pane = owned(read(directory / 'properties.json'), facts)
            backend.run('send-keys', '-t', pane, 'Escape')
            until(lambda: (agents.runtime(directory).get('queue_state') or {}).get('state') == 'paused', seconds=10)
            until(lambda: agents.runtime(directory).get('status') == 'idle', seconds=10)
        def paused():
            checked = deliver.run(self.root, agent=result['id'], check=True)
            receipt = native.deliver(self.root, result['id'], waiting_id, check=True)
            self.assertTrue(receipt['value'].get('paused'), (checked, receipt))
            self.assertFalse((work / 'FOLLOW_UP_AFTER_NATIVE_ESCAPE').exists())
        interrupt()
        paused()
        # Read-only checks, retries and inbox-only must never restart native work.
        send.run(self.root, agent=result['id'], text='INBOX_ONLY_DOES_NOT_RESTART', delivery='inbox-only')
        deliver.run(self.root, agent=result['id'], retry=True)
        time.sleep(.5)
        paused()
        lifecycle.kill(self.root, result['id'], attachment_only=True)
        with patch.dict(os.environ, test_env):
            result = lifecycle.start(self.root, resume=result['id'], socket_path=self.socket,
                                     wait=True, wait_timeout=30)
        self.assertTrue(result['ready'], result)
        self.assertEqual(read(directory / 'properties.json')['native_session_id'], primary)
        paused()
        # A stopped paused bridge must queue its wake text after binding,
        # rather than submit a CLI prompt ahead of older accepted native input.
        lifecycle.kill(self.root, result['id'], attachment_only=True)
        with patch.dict(os.environ, test_env):
            resumed = send.run(self.root, agent=result['id'], text='NEW_INPUT_RESUMES_FIFO', delivery='follow-up')
        self.assertIn(resumed['state'], ('pending', 'native_queued'), resumed)
        until(lambda: (work / 'NEW_INPUT_RESUMES_FIFO').exists(), seconds=25)
        facts = agents.runtime(directory)
        wake_request = read(directory / 'launches' / facts['launch_id'] / 'request.json')
        self.assertNotIn('NEW_INPUT_RESUMES_FIFO', json.dumps(wake_request['argv']))
        self.assertEqual(read(directory / 'properties.json')['native_session_id'], primary)
        self.assertEqual((work / 'order.log').read_text().splitlines(),
                         ['QUEUED_FOREIGN', 'FOLLOW_UP_AFTER_NATIVE_ESCAPE', 'NEW_INPUT_RESUMES_FIFO'])
        for text in ('FOLLOW_UP_AFTER_NATIVE_ESCAPE', 'NEW_INPUT_RESUMES_FIFO'):
            until(lambda: (native.deliver(self.root, result['id'], message_id(text), check=True).get('value') or {})
                  .get('transcript_confirmed') is True, seconds=10)
        # A terminal prompt starts its own turn, then lets the paused queue drain.
        until(lambda: agents.runtime(directory).get('status') == 'idle', seconds=10)
        facts = agents.runtime(directory)
        backend, pane = owned(read(directory / 'properties.json'), facts)
        backend.run('send-keys', '-t', pane, '-l', 'HOLD_AGAIN')
        until(lambda: 'HOLD_AGAIN' in backend.run('capture-pane', '-p', '-t', pane), seconds=5)
        backend.run('send-keys', '-t', pane, 'Enter')
        until(lambda: (work / 'second-tool-started').exists(), seconds=15)
        accepted = send.run(self.root, agent=result['id'], text='AFTER_TYPED', delivery='follow-up')
        self.assertEqual(accepted['state'], 'native_queued', accepted)
        interrupt()
        self.assertFalse((work / 'AFTER_TYPED').exists())
        backend.run('send-keys', '-t', pane, '-l', 'TYPED_RESTART')
        until(lambda: 'TYPED_RESTART' in backend.run('capture-pane', '-p', '-t', pane), seconds=5)
        backend.run('send-keys', '-t', pane, 'Enter')
        until(lambda: (work / 'AFTER_TYPED').exists(), seconds=20)
        self.assertEqual((work / 'order.log').read_text().splitlines()[-2:], ['TYPED_RESTART', 'AFTER_TYPED'])
        until(lambda: (native.deliver(self.root, result['id'], message_id('AFTER_TYPED'), check=True).get('value') or {})
              .get('transcript_confirmed') is True, seconds=10)
        self.assertEqual(deliver.run(self.root, agent=result['id'], check=True)['messages'], [])
        for message in inbox(directory, all_messages=True)['messages']:
            if message['text'] in ('FOLLOW_UP_AFTER_NATIVE_ESCAPE', 'NEW_INPUT_RESUMES_FIFO', 'AFTER_TYPED'):
                self.assertEqual(len(message['tracking']['attempts']), 1)
        self.assertEqual(read(directory / 'properties.json')['native_session_id'], primary)
        print('Native queue proof: pause across attachment resume; mixed FIFO; inbox/check/retry inert; typed restart; one attempt.')
