"""Installed Codex TUI + native server, backed only by a local scripted HTTP provider."""
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

from multiverse.tools import send
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.runtime import native, rpc
from multiverse.harnesses.types import NativeResult, RpcRequest, RpcSpec
from multiverse.universe.mailbox import inbox, append_message
from multiverse.store import read, write
from native_safety import isolated_home
from native_cli import mv2_launcher
from native_processes import PrivateDaemonGroups
from test_multiverse import TerminalBase, until


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class CodexTimingTest(TerminalBase):
    def test_native_tui_steering_and_after_turn_queue(self):
        """On live Codex a steer reaches the next model request, a follow-up waits for the turn's end, receipts become
        transcript-confirmed, and after an interrupt mv2 recovers the accepted item without resending; fragile because
        every step depends on app-server queue and turn semantics, and timing bugs show only as late or doubled
        messages."""
        self.enterContext(patch.object(lifecycle, '_cli_path', return_value=mv2_launcher(self.work)))
        records = []
        work = self.work

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                value = json.loads(body)
                if not self.path.endswith('/responses'):
                    self.send_error(404)
                    return
                is_title = 'title' in value.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
                if not is_title:
                    records.append({'at': time.time() * 1000, 'body': value})
                index = len(records)
                item = {'type': 'message', 'role': 'assistant', 'id': f'msg-{index}',
                        'content': [{'type': 'output_text', 'text': 'Offline sequence complete.'}]}
                if is_title:
                    item['content'][0]['text'] = '{"title":"Run offline test"}'
                elif index <= 2 or index in (6, 8, 10):
                    duration = 30 if index > 2 else 4
                    command = f'touch tool-{index}-start; sleep {duration}; touch tool-{index}-end'
                    if index == 1:
                        command = '"$MV2_CLI" session-bind --session-id "$CODEX_THREAD_ID"; ' + command
                    item = {'type': 'function_call', 'call_id': f'call-{index}', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': command, 'yield_time_ms': 10000, 'max_output_tokens': 200})}
                events = [
                    {'type': 'response.created', 'response': {'id': f'resp-{index}'}},
                    {'type': 'response.output_item.done', 'item': item},
                    {'type': 'response.completed', 'response': {'id': f'resp-{index}',
                     'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}},
                ]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        native_home = isolated_home(self.work / 'native-home', config=
            'model = "gpt-6-sol"\nmodel_provider = "mv2-test"\n'
            '[model_providers.mv2-test]\nname = "Offline test"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            f'[projects.{json.dumps(str(work))}]\ntrust_level = "trusted"\n')
        managed = native_home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        path = self.root / 'harnesses/codex/harness.json'
        spec = read(path)
        spec['settings'] = {'inbox_bridge': True, 'spawn_args': ['--sandbox', 'workspace-write', '--ask-for-approval', 'never']}
        write(path, spec)
        test_env = dict(CODEX_HOME=str(native_home), NO_COLOR='1', NO_PROXY='127.0.0.1,localhost',
                        HTTP_PROXY='', HTTPS_PROXY='', ALL_PROXY='', http_proxy='', https_proxy='', all_proxy='')
        daemon_env = {**os.environ, **test_env}
        daemon_groups = PrivateDaemonGroups()
        self.enterContext(daemon_groups.observe_starts())
        def stop_owned_daemon():
            try:
                subprocess.run(['codex', 'app-server', 'daemon', 'stop'],
                               env=daemon_env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=20, check=True)
            finally:
                daemon_groups.close()
        self.addCleanup(stop_owned_daemon)
        with patch.dict(os.environ, test_env):
            result = lifecycle.start(self.root, 'timing', 'Run the offline tool sequence.', 'codex', socket_path=self.socket,
                                  wait=True, wait_timeout=30)
            self.assertTrue(result['ready'], result)
        self.backend.run('set-option', '-g', 'remain-on-exit', 'on')
        directory = agents.directory(self.root, result['id'])
        endpoint = Path(until(lambda: agents.runtime(directory).get('control_endpoint'), seconds=30)['address'])

        def stop_owned():
            try:
                if lifecycle.listing(self.root)[0]['live'] == 'alive':
                    lifecycle.terminal_action(self.root, result['id'], 'stop')
            finally:
                until(lambda: lifecycle.listing(self.root)[0]['live'] == 'dormant', seconds=10)
                self.assertTrue(endpoint.exists(), 'Terminal exit must leave shared daemon running')

        self.addCleanup(stop_owned)
        try:
            until(lambda: (work / 'tool-1-start').exists(), seconds=30)
            sent = time.time() * 1000
            urgent = send.run(self.root, agent=result['id'], text='URGENT_SAFE_BOUNDARY', delivery='steer')
            normal = send.run(self.root, agent=result['id'], text='NORMAL_AFTER_TURN', delivery='follow-up')
            for message in (urgent, normal):
                self.assertEqual(message['state'], 'native_queued', message)
            until(lambda: len(records) >= 4, seconds=30)
            self.assertTrue('URGENT_SAFE_BOUNDARY' in json.dumps(records[1]['body']), 'Urgent input must reach the next request')
            self.assertFalse('NORMAL_AFTER_TURN' in json.dumps(records[1]['body']), 'Normal input must wait for turn completion')
            self.assertFalse('NORMAL_AFTER_TURN' in json.dumps(records[2]['body']), 'Normal input must wait for turn completion')
            self.assertTrue('NORMAL_AFTER_TURN' in json.dumps(records[3]['body']), 'Normal input must wake after the turn')
            for index in (1, 2):
                self.assertTrue((work / f'tool-{index}-end').exists())
            self.assertTrue(str(self.root) in json.dumps(records[0]['body']), 'Bootstrap must reach the native conversation')
            saved = inbox(directory, all_messages=True)['messages']
            for message in saved:
                self.assertNotIn(message['id'], json.dumps(records))
                receipt = native.deliver(self.root, result['id'], message['id'], check=True)
                self.assertEqual(receipt['state'], 'native_queued', receipt)
                self.assertTrue(receipt['value']['transcript_confirmed'], receipt)
            print('Codex timing evidence:', json.dumps({'urgent_ms': records[1]['at'] - sent,
                  'normal_ms': records[3]['at'] - sent, 'tool_duration_ms': 4000}))
            original_native = read(directory / 'properties.json')['native_session_id']
            old_endpoint = endpoint
            stop_owned()
            with patch.dict(os.environ, test_env):
                resumed = lifecycle.start(self.root, resume=result['id'], socket_path=self.socket,
                                       wait=True, wait_timeout=30)
            self.assertTrue(resumed['ready'], resumed)
            self.backend.run('set-option', '-g', 'remain-on-exit', 'on')
            endpoint = Path(until(lambda: agents.runtime(directory).get('control_endpoint'), seconds=30)['address'])
            self.assertEqual(endpoint, old_endpoint)
            self.assertEqual(resumed['native_session_id'], original_native)
            self.assertEqual(resumed['workspace'], str(work))
            until(lambda: native.capabilities(self.root, result['id'], run_probe=True)['send_follow_up']['state'] == 'supported', seconds=20)
            idle = append_message(directory, 'IDLE_AFTER_EXACT_RESUME', {}, delivery='follow-up')
            resumed_sent = time.time() * 1000
            receipt = native.deliver(self.root, result['id'], idle['id'])
            self.assertEqual(receipt['state'], 'native_queued', receipt)
            self.assertEqual(receipt['native_session_id'], original_native)
            until(lambda: len(records) >= 5, seconds=20)
            self.assertTrue('IDLE_AFTER_EXACT_RESUME' in json.dumps(records[4]['body']))
            metadata = json.loads(records[4]['body']['client_metadata']['x-codex-turn-metadata'])
            self.assertEqual(metadata['sandbox_mode'], 'workspace-write', 'Saved native permissions must survive resume')
            print('Codex resumed idle queue evidence:', json.dumps({'delay_ms': records[4]['at'] - resumed_sent}))
            def raw(method, params):
                def plan():
                    yield RpcRequest('initialize', {'clientInfo': {'name': 'timing-investigation', 'version': '1'},
                        'capabilities': {'experimentalApi': True}})
                    response = yield RpcRequest(method, {'threadId': original_native, **params})
                    return NativeResult('ok', value=response)
                return rpc.execute(RpcSpec(str(endpoint), plan)).value
            until(lambda: agents.runtime(directory).get('status') == 'idle', seconds=10)
            trigger = send.run(self.root, agent=result['id'], text='RUN_TOOL_TO_INTERRUPT', delivery='follow-up')
            self.assertEqual(trigger['state'], 'native_queued', trigger)
            until(lambda: (work / 'tool-6-start').exists(), seconds=15)
            waiting = raw('thread/queue/add', {'clientUserMessageId': 'during-active-turn',
                'input': [{'type': 'text', 'text': 'MUST_WAIT_FOR_ACTIVE_TURN'}]})['result']['queuedSubmission']
            active_start = raw('thread/queue/start', {'queuedSubmissionId': waiting['id']})
            print('Start while active:', json.dumps(active_start))
            self.assertIn('error', active_start, 'Queue start must refuse an active thread')
            self.assertEqual(len(records), 6)
            cancelled = native.stop(self.root, result['id'])
            self.assertEqual(cancelled.value['interrupted_turns'], 1, cancelled)
            self.assertEqual(cancelled.value['cancelled_submissions'], 1, cancelled)
            # Reproduce native behavior independently: interrupt pauses automatic
            # draining, even when the next queue/add happens after it is idle.
            until(lambda: raw('thread/read', {'includeTurns': False})['result']['thread']['status']['type'] == 'idle')
            raw_receipt = raw('thread/queue/add', {'clientUserMessageId': 'direct-after-interrupt',
                'input': [{'type': 'text', 'text': 'RAW_FOLLOW_UP_AFTER_INTERRUPT'}]})
            raw_item = raw_receipt['result']['queuedSubmission']
            time.sleep(.3)
            if len(records) == 6:
                self.assertEqual(raw('thread/queue/list', {'limit': 2})['result']['data'][0]['id'], raw_item['id'])
                self.assertIn('result', raw('thread/queue/start', {'queuedSubmissionId': raw_item['id']}))
            until(lambda: len(records) >= 7, seconds=5)
            self.assertIn('RAW_FOLLOW_UP_AFTER_INTERRUPT', json.dumps(records[6]['body']))

            # mv2 recovers the sole newly accepted item, including after killing
            # and resuming the exact saved conversation. It never resends it.
            for index, shutdown in ((8, False), (10, True)):
                until(lambda: agents.runtime(directory).get('status') == 'idle', seconds=10)
                trigger = send.run(self.root, agent=result['id'], text=f'INTERRUPT_CYCLE_{index}', delivery='follow-up')
                self.assertEqual(trigger['state'], 'native_queued', trigger)
                until(lambda: (work / f'tool-{index}-start').exists(), seconds=10)
                if shutdown:
                    stop_owned()
                    with patch.dict(os.environ, test_env):
                        resumed = lifecycle.start(self.root, resume=result['id'], socket_path=self.socket,
                                               wait=True, wait_timeout=30)
                    self.assertTrue(resumed['ready'], resumed)
                    self.assertEqual(resumed['native_session_id'], original_native)
                else:
                    self.assertEqual(native.stop(self.root, result['id']).value['interrupted_turns'], 1)
                sent_after_interrupt = time.time() * 1000
                text = f'FOLLOW_UP_AFTER_INTERRUPT_{index}'
                followup = send.run(self.root, agent=result['id'], text=text, delivery='follow-up')
                self.assertEqual(followup['state'], 'native_queued', followup)
                until(lambda: len(records) >= index + 1, seconds=5)
                self.assertIn(text, json.dumps(records[index]['body']))
                saved = inbox(directory, all_messages=True)['messages'][-1]
                receipt = native.deliver(self.root, result['id'], saved['id'], check=True)
                self.assertTrue(receipt['value']['transcript_confirmed'], receipt)
                print('Codex interrupted queue recovery:', json.dumps({'stop_resume': shutdown,
                    'delay_ms': records[index]['at'] - sent_after_interrupt}))
        except Exception:
            print('REQUESTS', [{'at': r['at'], 'urgent': 'URGENT_SAFE_BOUNDARY' in json.dumps(r['body']),
                                 'normal': 'NORMAL_AFTER_TURN' in json.dumps(r['body'])} for r in records])
            print('RUNTIME', agents.runtime(directory))
            try:
                facts = agents.runtime(directory)
                print(self.backend.run('capture-pane', '-p', '-S', '-200', '-t', facts['tmux']['pane']))
            except Exception as error:
                print(error)
            print((directory / 'launches' / agents.runtime(directory)['launch_id'] / 'native-service.log').read_text()[-3000:])
            raise
