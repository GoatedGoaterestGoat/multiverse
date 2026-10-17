"""Exact-thread recovery safety, bounded outages and real idle TUI recovery."""
from copy import deepcopy
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.runtime import recovery
from multiverse.harnesses.codex.protocol import recovery_check
from multiverse.harnesses.types import NativeEndpoint, NativeResult, SessionHandle, NativeServiceHandle, ServiceIdentity, AttachmentSpec
from multiverse.runtime.runner import RelayPublisher, StatusWatcher
from multiverse.universe.mailbox import append_message, update_message
from multiverse.store import now, read, write
from multiverse.terminal.tmux import Tmux
from native_safety import isolated_home
from test_multiverse import TerminalBase, until


class RecoveryProtocolTest(unittest.TestCase):
    def setUp(self):
        self.session = SessionHandle('agent', 'codex', '/tmp/work', native_id='exact-thread',
                                     endpoint=NativeEndpoint('unix-websocket', '/tmp/native.sock'))
        self.thread = {'id': 'exact-thread', 'cwd': '/tmp/work', 'status': {'type': 'idle'}}
        self.replies = {'initialize': {}, 'thread/read': {'thread': self.thread},
                        'thread/turns/list': {'data': [{'status': 'completed'}]},
                        'thread/queue/list': {'data': []}, 'thread/backgroundTerminals/list': {'data': []}}

    def execute(self, transform=None):
        plan, self.requests = recovery_check(self.session), []
        request = next(plan)
        while True:
            self.requests.append(request)
            response = {'result': deepcopy(self.replies[request.method])}
            if transform:
                response = transform(request, response, len(self.requests))
            try:
                request = plan.send(response)
            except StopIteration as done:
                return done.value

    def test_qualifies_only_read_only_exact_idle_thread(self):
        self.assertEqual(self.execute().state, 'ok')
        self.assertEqual([r.method for r in self.requests], ['initialize', 'thread/read',
            'thread/turns/list', 'thread/queue/list', 'thread/backgroundTerminals/list', 'thread/read'])
        self.assertTrue(all(r.params.get('threadId') == 'exact-thread' for r in self.requests[1:]))

    def test_active_waiting_unloaded_wrong_thread_and_workspace_never_qualify(self):
        for change in ({'id': 'other'}, {'cwd': '/tmp/other'}, {'status': {'type': 'notLoaded'}},
                       {'status': {'type': 'active', 'activeFlags': []}},
                       {'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']}},
                       {'status': {'type': 'active', 'activeFlags': ['waitingOnUserInput']}},
                       {'canAcceptDirectInput': False}):
            with self.subTest(change=change):
                self.replies['thread/read']['thread'] = {**self.thread, **change}
                self.assertNotEqual(self.execute().state, 'ok')

    def test_background_queue_and_in_progress_or_unknown_turn_block_recovery(self):
        for method, data in (('thread/backgroundTerminals/list', [{'processId': 'running'}]),
                             ('thread/queue/list', [{'id': 'queued'}]),
                             ('thread/turns/list', [{'status': 'inProgress'}]),
                             ('thread/turns/list', [{'status': 'future'}])):
            with self.subTest(method=method, data=data):
                original = self.replies[method]
                self.replies[method] = {'data': data}
                self.assertNotEqual(self.execute().state, 'ok')
                self.replies[method] = original

    def test_unsupported_malformed_and_racing_native_activity_fail_closed(self):
        def unsupported(request, response, index):
            return {'error': {'code': -32601, 'message': 'unsupported'}} if index == 5 else response
        self.assertNotEqual(self.execute(unsupported).state, 'ok')
        def malformed(request, response, index):
            return {'result': {'data': 'wrong'}} if index == 5 else response
        with self.assertRaises(ValueError):
            self.execute(malformed)
        def active(request, response, index):
            if index == 6:
                response['result']['thread']['status'] = {'type': 'active', 'activeFlags': []}
            return response
        self.assertNotEqual(self.execute(active).state, 'ok')


class RecoveryWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mv2-recovery-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'state/agents' / str(uuid4())
        self.launch, self.native_id = str(uuid4()), str(uuid4())
        self.properties = {'id': self.directory.name, 'name': 'idle', 'universe_id': str(uuid4()),
                           'workspace': str(self.root), 'harness': 'codex', 'native_session_id': self.native_id}
        self.facts = {'launch_id': self.launch, 'state': 'running', 'status': 'idle', 'pid': 123,
                      'startup_checked': True, 'startup_delivery': 'ready',
                      'control_endpoint': {'transport': 'unix-websocket', 'address': '/tmp/unavailable.sock'},
                      'tmux': {'socket': '/fake/tmux.sock', 'session': '$1', 'pane': '%1'},
                      'recovery_snapshot': {'safe': True, 'at': now(), 'native_id': self.native_id,
                                            'status_version': 0}}
        self.facts['native_service'] = asdict(NativeServiceHandle(
            ServiceIdentity('codex', {'CODEX_HOME': '/isolated'}), NativeEndpoint(**self.facts['control_endpoint'])))
        self.facts['attachment'] = asdict(AttachmentSpec('service', 'binding', 'idle-existing-service', True, True))
        self.facts['recovery_snapshot']['service'] = self.facts['native_service']
        self.facts['recovery_snapshot']['workspace'] = self.properties['workspace']
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', self.facts)
        (self.directory / '.launch.lock').touch()
        self.popen = self.enterContext(patch.object(recovery.subprocess, 'Popen'))
        self.enterContext(patch.object(recovery, 'process_started', return_value=None))

    def publish_loss(self):
        RelayPublisher(self.directory, self.launch)('upstream disappeared')
        return read(self.directory / 'runtime.json')

    def test_only_qualified_primary_loss_starts_one_worker(self):
        facts = self.publish_loss()
        self.assertEqual(facts['auto_recovery']['state'], 'checking')
        self.assertEqual(facts['native_relay']['state'], 'lost')
        self.assertEqual(self.popen.call_count, 1)
        recovery.schedule(self.directory, self.launch)
        self.assertEqual(self.popen.call_count, 1)

    def test_stale_missing_wrong_thread_and_active_proof_leave_sticky_loss(self):
        for proof in (None, {'safe': False, 'reason': 'Background processes are running'},
                      {**self.facts['recovery_snapshot'], 'native_id': 'other'},
                      {**self.facts['recovery_snapshot'], 'at': '2000-01-01T00:00:00+00:00'},
                      {**self.facts['recovery_snapshot'], 'status_version': 1}):
            with self.subTest(proof=proof):
                write(self.directory / 'runtime.json', {**self.facts, 'recovery_snapshot': proof})
                self.assertEqual(self.publish_loss()['auto_recovery']['state'], 'skipped')
        self.popen.assert_not_called()

    def test_native_activity_invalidates_idle_proof(self):
        StatusWatcher(self.directory, self.launch, self.root)._publish('waiting')
        self.assertEqual(self.publish_loss()['auto_recovery']['state'], 'skipped')
        self.popen.assert_not_called()

    def test_expired_safe_proof_has_an_actionable_reason(self):
        proof = {**self.facts['recovery_snapshot'], 'at': '2000-01-01T00:00:00+00:00',
                 'reason': 'Exact native thread is idle'}
        write(self.directory / 'runtime.json', {**self.facts, 'recovery_snapshot': proof})
        self.assertIn('expired', self.publish_loss()['auto_recovery']['reason'])

    def test_manual_terminal_exit_is_never_automatically_resumed(self):
        RelayPublisher(self.directory, self.launch)('terminal connection closed (code 1006): no close frame')
        self.assertEqual(read(self.directory / 'runtime.json')['auto_recovery']['state'], 'skipped')
        self.popen.assert_not_called()

    def test_removed_agent_is_not_recreated_by_delayed_worker(self):
        self.publish_loss()
        shutil.rmtree(self.directory)
        recovery.run(self.directory, self.launch)
        self.assertFalse(self.directory.exists())

    def test_optional_status_loss_keeps_recent_idle_proof_for_primary_loss(self):
        StatusWatcher(self.directory, self.launch, self.root)._publish('unknown')
        self.assertEqual(self.publish_loss()['auto_recovery']['state'], 'checking')

    def test_startup_and_uncertain_messages_block_without_receipt_mutation(self):
        message = append_message(self.directory, 'uncertain', {}, delivery='steer', target_session=self.native_id)
        update_message(self.directory, message['id'], submission_state='unknown', target_session=self.native_id)
        before = (self.directory / 'inbox-state.json').read_bytes()
        self.assertEqual(self.publish_loss()['auto_recovery']['state'], 'skipped')
        self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), before)
        self.popen.assert_not_called()

    def test_saved_message_waiting_to_acquire_launch_lock_blocks_recovery(self):
        append_message(self.directory, 'pending input', {}, delivery='steer', target_session=self.native_id)
        self.assertEqual(self.publish_loss()['auto_recovery']['state'], 'skipped')
        self.popen.assert_not_called()

    def test_outage_retries_are_bounded_and_never_start_resume_or_submit(self):
        self.publish_loss()
        with patch.object(recovery, 'BACKOFF', (0, 0, 0)), \
                patch.object(recovery, 'check', side_effect=ConnectionRefusedError('daemon down')) as checked, \
                patch.object(lifecycle, 'start') as start:
            recovery.run(self.directory, self.launch)
        self.assertEqual(checked.call_count, 3)
        start.assert_not_called()
        facts = read(self.directory / 'runtime.json')
        self.assertEqual(facts['auto_recovery']['state'], 'failed')
        self.assertEqual(facts['auto_recovery']['attempts'], 3)
        self.assertIn('timed out', facts['native_relay']['reason'])

    def test_acceptance_after_proof_blocks_even_with_a_transcript_receipt(self):
        message = append_message(self.directory, 'new input', {}, delivery='steer', target_session=self.native_id)
        update_message(self.directory, message['id'], submission_state='accepted', target_session=self.native_id,
                       attempts=[{'at': now()}], native_receipt={'transcript_confirmed': True})
        before = (self.directory / 'inbox-state.json').read_bytes()
        self.assertEqual(self.publish_loss()['auto_recovery']['state'], 'skipped')
        self.assertEqual((self.directory / 'inbox-state.json').read_bytes(), before)
        self.popen.assert_not_called()

    def test_completed_acceptance_before_fresh_proof_does_not_block_forever(self):
        message = append_message(self.directory, 'completed input', {}, delivery='follow-up', target_session=self.native_id)
        update_message(self.directory, message['id'], submission_state='accepted', target_session=self.native_id,
                       attempts=[{'at': '2000-01-01T00:00:00+00:00'}], native_receipt={'transcript_confirmed': False})
        self.assertIsNone(recovery.in_flight(self.directory, self.facts, self.native_id, since=now()))
        self.assertEqual(self.publish_loss()['auto_recovery']['state'], 'checking')

    def test_unknown_acceptance_time_fails_closed(self):
        message = append_message(self.directory, 'input', {}, delivery='steer', target_session=self.native_id)
        for attempts in ([], [{'at': 'invalid'}], [{'at': '2000-01-01'}]):
            update_message(self.directory, message['id'], submission_state='accepted',
                           target_session=self.native_id, attempts=attempts)
            self.assertIsNotNone(recovery.in_flight(self.directory, self.facts, self.native_id, since=now()))

    def test_message_accepted_during_native_check_invalidates_sample(self):
        class Clock:
            def __init__(self):
                self.calls = 0
            def wait(self, seconds):
                self.calls += 1
                return self.calls > 2
        monitor = recovery.RecoveryMonitor(self.directory, self.launch, self.root)
        monitor.stopped = Clock()
        def check(*args):
            message = append_message(self.directory, 'racing input', {}, delivery='steer', target_session=self.native_id)
            update_message(self.directory, message['id'], submission_state='accepted', target_session=self.native_id,
                           attempts=[{'at': now()}])
            return NativeResult('ok')
        with patch.object(recovery, 'check', side_effect=check):
            monitor._run()
        proof = read(self.directory / 'runtime.json')['recovery_snapshot']
        self.assertFalse(proof['safe'])
        self.assertIn('accepted after', proof['reason'])

    def test_fresh_native_background_work_and_frontend_children_prevent_shutdown(self):
        self.publish_loss()
        with patch.object(recovery, 'check', return_value=NativeResult('unavailable', 'Background processes running')), \
                patch.object(lifecycle, 'start') as start, patch.object(recovery, 'owned') as owned:
            recovery.run(self.directory, self.launch)
        start.assert_not_called(); owned.assert_not_called()
        write(self.directory / 'runtime.json', self.facts)
        self.publish_loss()
        backend = Mock()
        with patch.object(recovery, 'check', return_value=NativeResult('ok')), \
                patch.object(recovery, 'owned', return_value=(backend, '%1')), \
                patch.object(recovery, 'frontend_children', return_value=True), patch.object(lifecycle, 'start') as start:
            recovery.run(self.directory, self.launch)
        backend.signal_child.assert_not_called(); start.assert_not_called()

    def test_manual_stop_or_replacement_cancels_delayed_recovery(self):
        for changes in ({'ending_at': now(), 'auto_recovery': {'state': 'cancelled'}},
                        {'launch_id': 'replacement'}):
            with self.subTest(changes=changes):
                write(self.directory / 'runtime.json', self.facts)
                self.publish_loss()
                def racing_check(*args):
                    write(self.directory / 'runtime.json', {**read(self.directory / 'runtime.json'), **changes})
                    return NativeResult('ok')
                with patch.object(recovery, 'check', side_effect=racing_check), patch.object(lifecycle, 'start') as start:
                    recovery.run(self.directory, self.launch)
                start.assert_not_called()
                facts = read(self.directory / 'runtime.json')
                self.assertEqual(facts['launch_id'], changes.get('launch_id', self.launch))
                if 'auto_recovery' in changes:
                    self.assertEqual(facts['auto_recovery']['state'], 'cancelled')

    def test_recent_restart_limit_prevents_crash_loop(self):
        write(self.directory / 'runtime.json', {**self.facts, 'automatic_restarts': [{'at': now()}, {'at': now()}]})
        facts = self.publish_loss()
        self.assertEqual(facts['auto_recovery']['state'], 'skipped')
        self.assertIn('limit', facts['auto_recovery']['reason'])
        self.popen.assert_not_called()

    def test_respawn_requires_dead_pane_and_exact_ownership(self):
        pane = {'pane_id': '%1', 'pane_dead': '1', '@mv2_agent_id': self.properties['id'],
                '@mv2_universe_id': self.properties['universe_id'], '@mv2_launch_id': self.launch}
        backend = Tmux('/fake/tmux.sock')
        with patch.object(backend, 'run') as command:
            for change in ({'pane_dead': '0'}, {'@mv2_launch_id': 'replacement'},
                           {'@mv2_agent_id': 'other'}, {'@mv2_universe_id': 'other'}):
                with self.subTest(change=change), patch.object(backend, 'snapshot',
                        return_value=({'%1': {**pane, **change}}, None)), self.assertRaises(ValueError):
                    backend.respawn(self.properties, self.facts, self.root, ['runner'])
            command.assert_not_called()
            with patch.object(backend, 'snapshot', return_value=({'%1': pane}, None)):
                self.assertEqual(backend.respawn(self.properties, self.facts, self.root, ['runner']), self.facts['tmux'])
            command.assert_called_once_with('respawn-pane', '-t', '%1', '-c', str(self.root), 'runner')


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class RecoveryLiveTest(TerminalBase):
    def setUp(self):
        super().setUp()
        self.requests = []
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                title = 'title' in value.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
                inputs = value.get('input', [])
                if not title:
                    parent.requests.append(value)
                item = {'type': 'message', 'role': 'assistant', 'id': 'message',
                        'content': [{'type': 'output_text', 'text': '{"title":"Recovery test"}' if title else 'Done.'}]}
                if (not title and 'make-background' in json.dumps(inputs)
                        and (not inputs or inputs[-1].get('type') != 'function_call_output')):
                    item = {'type': 'function_call', 'call_id': 'background', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': 'sleep 90', 'yield_time_ms': 10, 'max_output_tokens': 100})}
                events = [{'type': 'response.created', 'response': {'id': 'response'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': 'response',
                           'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.home = isolated_home(self.work / 'home', config=
            'model="gpt-6-sol"\nmodel_provider="offline"\n[features]\nplugins=false\n'
            '[model_providers.offline]\nname="Offline"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'base_url="http://127.0.0.1:{self.server.server_port}/v1"\n'
            f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n')
        real = str(Path(shutil.which('codex')).resolve())
        managed = self.home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True); managed.symlink_to(real)
        self.commands = self.work / 'codex-commands.jsonl'
        shim = self.work / 'codex-shim'
        shim.write_text(f'#!{sys.executable}\nimport json,os,sys\n'
                        f'with open({str(self.commands)!r}, "a") as out: out.write(json.dumps(sys.argv[1:4])+"\\n")\n'
                        f'os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n')
        shim.chmod(0o700)
        self.env = {**os.environ, 'CODEX_HOME': str(self.home), 'CODEX_SQLITE_HOME': str(self.home),
                    'NO_PROXY': '127.0.0.1,localhost', 'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '',
                    'http_proxy': '', 'https_proxy': '', 'all_proxy': ''}
        self.addCleanup(lambda: self.daemon('stop'))
        spec = read(self.root / 'harnesses/codex/harness.json')
        spec['settings'] = {'executable': str(shim), 'inbox_bridge': True,
                            'args': ['--dangerously-bypass-approvals-and-sandbox']}
        write(self.root / 'harnesses/codex/harness.json', spec)

    def daemon(self, command):
        process = subprocess.run(['codex', 'app-server', 'daemon', command], env=self.env,
                                 cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def launch(self, task='idle-recovery-probe'):
        with patch.dict(os.environ, self.env):
            result = lifecycle.start(self.root, 'idle', task, socket_path=self.socket, wait=True, wait_timeout=30)
        self.assertTrue(result['ready'], result)
        directory = agents.directory(self.root, result['id'])
        def cleanup():
            try:
                lifecycle.kill(self.root, result['id'])
            except (OSError, ValueError, RuntimeError):
                pass
        self.addCleanup(cleanup)
        return result, directory

    def test_idle_restarts_in_same_attached_pane_without_replaying_or_starting_daemon(self):
        result, directory = self.launch()
        until(lambda: (agents.runtime(directory).get('recovery_snapshot') or {}).get('safe'), seconds=30)
        native_id = read(directory / 'properties.json')['native_session_id']
        old = agents.runtime(directory)
        from multiverse.runtime.runner import UsagePublisher
        UsagePublisher(directory, old['launch_id'])({'native_session_id': native_id,
                                                   'context': {'tokens': 17457}, 'source': 'codex-app-server'})
        measurement = agents.runtime(directory)['usage']
        count = len(self.requests)
        fd, slave = pty.openpty()
        client = subprocess.Popen([*self.backend.prefix, 'attach-session', '-t', old['tmux']['session']],
            stdin=slave, stdout=slave, stderr=slave,
            env={**{key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')},
                 'TERM': 'xterm-256color'})
        os.close(slave)
        def close_client():
            if client.poll() is None:
                client.terminate()
                client.wait(timeout=5)
            os.close(fd)
        self.addCleanup(close_client)
        until(lambda: self.backend.run('list-clients', '-t', old['tmux']['pane']), seconds=5)
        clients = self.backend.run('list-clients', '-t', old['tmux']['pane'], '-F', '#{client_name}')
        self.daemon('restart')  # Only this fixture's isolated daemon.
        def ready():
            facts = agents.runtime(directory)
            return (facts.get('launch_id') != old['launch_id'] and facts.get('startup_checked')
                    and (facts.get('auto_recovery') or {}).get('state') == 'reconnected')
        until(ready, seconds=60)
        current = agents.runtime(directory)
        self.assertEqual(read(directory / 'properties.json')['native_session_id'], native_id)
        self.assertEqual(current['tmux'], old['tmux'])
        self.assertEqual(self.backend.run('list-clients', '-t', current['tmux']['pane'], '-F', '#{client_name}'), clients)
        self.assertIsNone(client.poll())
        self.assertEqual(current['last_usage'], measurement)
        self.assertEqual(current['automatic_restarts'][-1]['from_launch_id'], old['launch_id'])
        request = read(directory / 'launches' / current['launch_id'] / 'request.json')
        self.assertEqual(request['rpc_proxy']['settings']['resume_permissions'],
                         {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'})
        self.assertIsNone(request['fallback'])
        self.assertEqual(len(self.requests), count, 'Recovery must never start a model turn or replay the task')
        commands = [json.loads(line) for line in self.commands.read_text().splitlines()]
        self.assertEqual(commands.count(['app-server', 'daemon', 'start']), 1)
        self.assertFalse(any(command[:2] == ['app-server', 'daemon'] and command[2] != 'start' for command in commands))
        self.assertIn(current['launch_id'], self.backend.run('show-option', '-v', '-t', current['tmux']['session'],
                                                            '@mv2_status_command'))

    def test_native_background_work_stays_disconnected_without_restart(self):
        _, directory = self.launch('make-background')
        def background():
            proof = agents.runtime(directory).get('recovery_snapshot') or {}
            return not proof.get('safe') and 'background' in proof.get('reason', '').lower()
        until(background, seconds=30)
        old = agents.runtime(directory)
        self.daemon('restart')
        until(lambda: (agents.runtime(directory).get('native_relay') or {}).get('state') == 'lost', seconds=10)
        current = agents.runtime(directory)
        self.assertEqual(current['launch_id'], old['launch_id'])
        self.assertEqual(current['auto_recovery']['state'], 'skipped')
        self.assertIn('background', current['auto_recovery']['reason'].lower())
        self.assertNotIn('automatic_restarts', current)
