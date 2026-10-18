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
from unittest.mock import patch

from native_safety import install, isolated_home
from native_processes import PrivateDaemonGroups
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from multiverse.terminal.tmux import owned
from multiverse.tools import deliver, send
from multiverse.universe import agents
from multiverse.universe.mailbox import inbox
from test_multiverse import TerminalBase, until

install()


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class NativeQueueContinuationTest(TerminalBase):
    def test_native_escape_continues_owned_accepted_follow_up_without_resending(self):
        """On live Codex an accepted follow-up still runs after a native Escape in the launch's own pane and stays
        checkable until confirmed; fragile because it relies on Codex pausing queue draining after an interrupt, which
        is undocumented, and a break strands messages with no error (60c3cbf, 1dbe82c)."""
        if subprocess.check_output(['codex', '--version'], stdin=subprocess.DEVNULL,
                                   text=True).strip() != 'codex-cli 0.163.0':
            self.skipTest('native queue continuation evidence pinned to 0.163.0')
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
                if not title and index == 1:
                    item = {'type': 'function_call', 'call_id': 'initial-tool', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': 'touch initial-tool-started; sleep 30',
                                                     'yield_time_ms': 30000, 'max_output_tokens': 200})}
                elif not title and 'FOLLOW_UP_AFTER_NATIVE_ESCAPE' in json.dumps(body):
                    # A function result in later requests means the marker already exists.
                    if not (work / 'follow-up-ran').exists():
                        item = {'type': 'function_call', 'call_id': 'follow-up-tool', 'name': 'exec_command',
                                'arguments': json.dumps({'cmd': 'touch follow-up-ran',
                                                         'yield_time_ms': 1000, 'max_output_tokens': 200})}
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
        queued = send.run(self.root, agent=result['id'], text='FOLLOW_UP_AFTER_NATIVE_ESCAPE', delivery='follow-up')
        self.assertEqual(queued['state'], 'native_queued', queued)
        message_id = next(message['id'] for message in inbox(directory, all_messages=True)['messages']
                          if message['text'] == 'FOLLOW_UP_AFTER_NATIVE_ESCAPE')
        waiting = deliver.run(self.root, agent=result['id'], check=True)
        self.assertEqual(len(waiting['messages']), 1,
                         'Accepted queue input must remain visible to --check while the turn is active')
        observed = native.deliver(self.root, result['id'], message_id, check=True)
        self.assertIs(observed['value']['transcript_confirmed'], False)
        facts = agents.runtime(directory)
        backend, pane = owned(read(directory / 'properties.json'), facts)
        self.assertEqual(facts['launch_id'], result['runtime']['launch_id'])
        backend.run('send-keys', '-t', pane, 'Escape')
        marker = work / 'follow-up-ran'
        deadline = time.monotonic() + 12
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        checked = deliver.run(self.root, agent=result['id'], check=True)
        receipt = native.deliver(self.root, result['id'], message_id, check=True)
        print('Native Escape queue evidence:', json.dumps({'marker': marker.exists(),
            'native_status': agents.runtime(directory).get('status'),
            'checked_messages': len(checked['messages']), 'receipt': receipt.get('value'),
            'model_requests': len(requests)}))
        with self.subTest(behavior='continuation'):
            self.assertTrue(marker.exists(), 'Accepted follow-up stayed in the idle native queue after Escape')
        with self.subTest(behavior='check'):
            self.assertEqual(len(checked['messages']), 1, 'Accepted input remains checkable until transcript confirmation')
