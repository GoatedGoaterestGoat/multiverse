"""Real /btw on a private server keeps mv2 input on the exact primary."""
from copy import deepcopy
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
from multiverse.harnesses.types import NativeResult, RpcNotification, RpcRequest, RpcSpec
from multiverse.runtime import lifecycle, native, rpc
from multiverse.store import read, write
from multiverse.terminal.tmux import owned
from multiverse.tools import deliver, send
from multiverse.universe import agents
from multiverse.universe.mailbox import inbox
from test_multiverse import TerminalBase, until

install()


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class NativeSideTest(TerminalBase):
    def test_btw_turn_close_and_resume_keep_delivery_on_primary(self):
        if subprocess.check_output(['codex', '--version'], stdin=subprocess.DEVNULL,
                                   text=True).strip() != 'codex-cli 0.163.0':
            self.skipTest('native side evidence pinned to 0.163.0')
        work = self.work
        requests, counts = [], {'primary': 0, 'side': 0}

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
                scope = 'side' if 'SIDE_NATIVE_HOLD' in json.dumps(body.get('input')) else 'primary'
                if not title:
                    counts[scope] += 1
                    requests.append({'scope': scope, 'body': body})
                index = counts[scope]
                item = {'type': 'message', 'role': 'assistant', 'id': f'{scope}-msg-{index}',
                        'content': [{'type': 'output_text', 'text': '{"title":"Private side test"}' if title
                                     else scope.upper() + '_DONE'}]}
                if not title and index == 1:
                    command = (f'printf "%s" "$CODEX_THREAD_ID" > {scope}-thread; touch {scope}-active; '
                               f'n=0; while test ! -e release-{scope} && test "$n" -lt 1200; '
                               f'do n=$((n+1)); sleep .05; done; touch {scope}-tool-completed')
                    item = {'type': 'function_call', 'call_id': scope + '-hold', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': command, 'yield_time_ms': 30000, 'max_output_tokens': 200})}
                tokens = 20 if scope == 'primary' else 20000
                events = [{'type': 'response.created', 'response': {'id': f'{scope}-resp-{index}'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': f'{scope}-resp-{index}',
                           'usage': {'input_tokens': tokens, 'output_tokens': 1, 'total_tokens': tokens + 1}}}]
                payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        home = isolated_home(work / 'native-home', config=
            'model="gpt-6-sol"\nmodel_provider="fixture"\n'
            '[model_providers.fixture]\nname="Private side fixture"\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api="responses"\nrequires_openai_auth=false\n'
            f'[projects.{json.dumps(str(work))}]\ntrust_level="trusted"\n')
        managed = home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex', 'settings': {
            'inbox_bridge': True, 'args': ['--sandbox', 'workspace-write', '--ask-for-approval', 'never']}})
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
        started = None
        def stop_agent():
            (work / 'release-primary').touch()
            (work / 'release-side').touch()
            if started is not None:
                lifecycle.kill(self.root, started['id'])
        self.addCleanup(stop_agent)
        with patch.dict(os.environ, test_env):
            started = lifecycle.start(self.root, 'side-test', 'Run the private primary tool.',
                                      'codex', socket_path=self.socket, wait=True, wait_timeout=30)
        self.assertTrue(started['ready'], started)
        directory = agents.directory(self.root, started['id'])
        primary = read(directory / 'properties.json')['native_session_id']
        until(lambda: (work / 'primary-active').exists(), seconds=30)
        self.assertEqual((work / 'primary-thread').read_text(), primary)
        facts = agents.runtime(directory)
        backend, pane = owned(read(directory / 'properties.json'), facts)
        backend.run('send-keys', '-t', pane, '-l', '/btw SIDE_NATIVE_HOLD')
        until(lambda: '/btw SIDE_NATIVE_HOLD' in backend.run('capture-pane', '-p', '-t', pane), seconds=10)
        backend.run('send-keys', '-t', pane, 'Enter')
        try:
            until(lambda: (work / 'side-active').exists(), seconds=20)
            side = (work / 'side-thread').read_text()
            self.assertNotEqual(side, primary)
            before_launch = agents.runtime(directory)['launch_id']
            for delivery, text in (('steer', 'PRIMARY_STEER_ONLY'), ('follow-up', 'PRIMARY_FOLLOW_UP_ONLY')):
                result = send.run(self.root, agent=started['id'], text=text, delivery=delivery)
                self.assertEqual(result['state'], 'native_queued', result)
            messages = inbox(directory, all_messages=True)['messages']
            waiting = deliver.run(self.root, agent=started['id'], check=True)
            self.assertTrue(waiting['messages'], 'Queued primary input must remain checkable')
            (work / 'release-primary').touch()
            receipts = {}
            def confirmed():
                for message in messages:
                    receipts[message['id']] = native.deliver(self.root, started['id'], message['id'], check=True)
                return all((receipt.get('value') or {}).get('transcript_confirmed') is True
                           for receipt in receipts.values())
            until(confirmed, seconds=30)
            def history():
                yield RpcRequest('initialize', {'clientInfo': {'name': 'private-side-evidence', 'version': '1'},
                    'capabilities': {'experimentalApi': True}}, on_success=(RpcNotification('initialized', {}),))
                response = yield RpcRequest('thread/turns/list', {'threadId': primary, 'limit': 20,
                    'itemsView': 'full', 'sortDirection': 'desc'})
                return NativeResult('ok', value=response['result']['data'])
            turns = rpc.execute(RpcSpec(agents.runtime(directory)['control_endpoint']['address'], history)).value
            for message_id, receipt in receipts.items():
                self.assertTrue(any(turn['id'] == receipt['value']['turn_id']
                    and any(item.get('type') == 'userMessage' and item.get('clientId') == message_id
                            for item in turn['items']) for turn in turns))
            until(lambda: agents.runtime(directory).get('status') == 'idle', seconds=20)
            self.assertFalse((work / 'release-side').exists())
            self.assertEqual(counts['side'], 1, 'The side must still be working when the primary is idle')
            self.assertFalse((work / 'side-tool-completed').exists())
            self.assertEqual(deliver.run(self.root, agent=started['id'], check=True)['messages'], [])
            primary_usage = deepcopy(agents.runtime(directory).get('usage'))
            self.assertIsNotNone(primary_usage)
            self.assertTrue(any('PRIMARY_STEER_ONLY' in json.dumps(record['body'])
                                for record in requests if record['scope'] == 'primary'))
            self.assertTrue(any('PRIMARY_FOLLOW_UP_ONLY' in json.dumps(record['body'])
                                for record in requests if record['scope'] == 'primary'))
            (work / 'release-side').touch()
            until(lambda: counts['side'] >= 2, seconds=20)
            until(lambda: 'SIDE_DONE' in backend.run('capture-pane', '-p', '-t', pane), seconds=20)
            self.assertFalse(any('PRIMARY_STEER_ONLY' in json.dumps(record['body'])
                                 or 'PRIMARY_FOLLOW_UP_ONLY' in json.dumps(record['body'])
                                 for record in requests if record['scope'] == 'side'))
            self.assertEqual(agents.runtime(directory).get('usage'), primary_usage)
            self.assertEqual(agents.runtime(directory).get('status'), 'idle')
            self.assertEqual(agents.runtime(directory)['launch_id'], before_launch)
            self.assertFalse((directory / 'launches' / before_launch / 'native-refusal.json').exists())
            self.assertEqual(read(directory / 'properties.json')['native_session_id'], primary)
            self.assertEqual(len(lifecycle.listing(self.root)), 1)
            backend.run('send-keys', '-t', pane, 'C-c')
            until(lambda: 'SIDE_DONE' not in backend.run('capture-pane', '-p', '-t', pane), seconds=20)
            after = send.run(self.root, agent=started['id'], text='PRIMARY_AFTER_SIDE_CLOSE', delivery='follow-up')
            self.assertEqual(after['state'], 'native_queued', after)
            after_message = next(message for message in inbox(directory, all_messages=True)['messages']
                                 if message['text'] == 'PRIMARY_AFTER_SIDE_CLOSE')
            until(lambda: (native.deliver(self.root, started['id'], after_message['id'], check=True)
                           .get('value') or {}).get('transcript_confirmed') is True, seconds=20)
            self.assertEqual(agents.runtime(directory)['launch_id'], before_launch)
            self.assertFalse(any('PRIMARY_AFTER_SIDE_CLOSE' in json.dumps(record['body'])
                                 for record in requests if record['scope'] == 'side'))
            lifecycle.kill(self.root, started['id'])
            with patch.dict(os.environ, test_env):
                resumed = lifecycle.start(self.root, resume=started['id'], socket_path=self.socket,
                                          wait=True, wait_timeout=30)
            self.assertTrue(resumed['ready'], resumed)
            self.assertEqual(resumed['native_session_id'], primary)
            print('Native /btw evidence:', json.dumps({'primary': primary, 'side': side,
                'provider_requests': counts, 'messages_confirmed': len(messages), 'resumed': primary}))
        except Exception:
            print('Native /btw diagnostic:', json.dumps({'requests': [(r['scope'], len(r['body'].get('input', [])))
                  for r in requests], 'runtime': {key: agents.runtime(directory).get(key) for key in
                  ('state', 'status', 'native_relay', 'launch_error')},
                  'receipts': locals().get('receipts'),
                  'pane': backend.run('capture-pane', '-p', '-t', pane)[-5000:]}))
            raise
