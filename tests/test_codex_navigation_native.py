"""Checked native navigation must keep mv2's current thread and callback authority together."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve
from websockets.exceptions import WebSocketException

from native_cli import mv2_launcher
from native_processes import PrivateDaemonGroups
from native_safety import install, isolated_home
from multiverse.harnesses.codex.plugins import CHECKED_VERSION, version_text
from multiverse.harnesses.types import NativeResult, RpcNotification, RpcRequest, RpcSpec
from multiverse.runtime import lifecycle, native, recovery, transcripts
from multiverse.store import existing_lock, read, write
from multiverse.terminal.tmux import owned
from multiverse.tools import deliver, send
from multiverse.universe import agents, sessions
from multiverse.universe.mailbox import inbox
from test_multiverse import TerminalBase, until
from test_message_delivery import DeliveryFixture

install()


def committed_navigation(directory, *, launch, previous, generation, request_id, reason):
    try:
        with existing_lock(directory / '.launch.lock', blocking=False):
            properties, facts = read(directory / 'properties.json'), agents.runtime(directory)
    except BlockingIOError:
        return None
    marker = facts.get('navigation') or {}
    target = properties.get('native_session_id')
    correlation = marker.get('correlation') or ''
    if (facts.get('launch_id') != launch or facts.get('state') != 'running' or facts.get('ending_at')
            or facts.get('status') != 'idle' or facts.get('session_generation') != generation + 1
            or marker.get('state') != 'committed' or marker.get('launch_id') != launch
            or marker.get('previous') != previous or marker.get('reason') != reason
            or not target or target == previous or marker.get('committed_target') != target
            or not correlation.rpartition(':')[0] or correlation.rpartition(':')[2] != str(request_id)):
        return None
    return properties, facts


class NavigationCommitTest(DeliveryFixture):
    def test_switch_snapshot_waits_for_the_durable_commit_not_just_new_properties(self):
        """The pointer is published before usage/reset metadata; polling it alone reads a half-committed switch."""
        from multiverse.runtime import navigation
        from uuid import uuid4
        self.recipient(native_session_home=str(self.work), native_session_provider='claude')
        (self.directory / '.launch.lock').touch()
        agents.update_runtime(self.directory, self.launch, status='idle', startup_checked=True,
                              native_relay={'state': 'ready'}, session_generation=0,
                              usage={'native_session_id': self.native_id})
        with patch.object(recovery, 'check', return_value=NativeResult('ok')):
            ticket = navigation.begin(self.root, self.directory, self.launch,
                {'previous': self.native_id, 'target': None, 'reason': 'new', 'correlation': 'private:12'})
        target = str(uuid4())
        advanced, release = threading.Event(), threading.Event()
        rebind = agents._rebind_session
        def pause_after_pointer(*args, **kwargs):
            value = rebind(*args, **kwargs)
            advanced.set()
            if not release.wait(10):
                raise AssertionError('Paused commit was not released')
            return value
        def snapshot(**changes):
            return committed_navigation(self.directory, **{'launch': self.launch, 'previous': self.native_id,
                                        'generation': 0, 'request_id': 12, 'reason': 'new', **changes})
        with patch.object(agents, '_rebind_session', side_effect=pause_after_pointer), \
                ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(navigation.commit, self.root, self.directory, self.launch,
                                  {**ticket, 'target': target, 'home': str(self.work)})
            try:
                self.assertTrue(advanced.wait(10), 'Commit did not publish its pointer')
                self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], target)
                self.assertEqual(agents.runtime(self.directory)['usage']['native_session_id'], self.native_id)
                self.assertIsNone(snapshot())
            finally:
                release.set()
            pending.result(timeout=10)
        agents.update_runtime(self.directory, self.launch, status='idle')
        properties, facts = snapshot()
        self.assertEqual(properties['native_session_id'], target)
        self.assertIsNone(facts['usage'])
        self.assertEqual(facts['session_generation'], 1)
        for changes in ({'launch': str(uuid4())}, {'generation': 1}, {'request_id': 13},
                        {'reason': 'clear'}, {'previous': target}):
            with self.subTest(unrelated_commit=changes):
                self.assertIsNone(snapshot(**changes))


def read_transcript(operation, *, timeout=30):
    """Retry transient identity contention without changing the requested cursor."""
    deadline = time.monotonic() + timeout
    last_retry = None
    while True:
        if last_retry is not None and time.monotonic() >= deadline:
            raise last_retry
        try:
            value = operation()
            if isinstance(value, transcripts.TranscriptInfo) and value.state != 'supported':
                error = (transcripts.Retry if value.state == 'unavailable' and getattr(value, 'retry', False) is True
                         else transcripts.Unavailable)
                raise error(value.reason)
            return value
        except transcripts.Retry as error:
            last_retry = error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(.05, remaining))


class NavigationReadTest(unittest.TestCase):
    def test_info_contention_retries_the_exact_requested_generation(self):
        """A normalized transient info failure must not look like missing native identity or lineage."""
        root, directory, generation = object(), object(), object()
        expected = transcripts.TranscriptInfo(state='supported', native_id='original')
        snapshot = SimpleNamespace(earlier=(), session=SimpleNamespace(native_id='original'))
        for boundary in ('snapshot', 'load'):
            for failure in (transcripts.Retry('contention'), transcripts.Unavailable('damaged'),
                            transcripts.Unavailable('provider error', state='error'),
                            transcripts.Unavailable('provider error', state='error', retry=True)):
                with self.subTest(boundary=boundary, retry=failure.retry, state=failure.state):
                    attempts = []
                    transient = failure.state == 'unavailable' and failure.retry is True
                    def operation():
                        attempts.append((root, directory, generation))
                        if len(attempts) != 1:
                            self.assertTrue(transient, 'Permanent info failures must propagate immediately')
                            return expected
                        with patch.object(transcripts, '_snapshot', side_effect=failure if boundary == 'snapshot' else None,
                                          return_value=snapshot), \
                                patch.object(transcripts, '_load', side_effect=failure):
                            value = transcripts.info(root, directory, generation=generation)
                        self.assertIs(getattr(value, 'retry', False), failure.retry)
                        self.assertEqual((value.state, value.reason), (failure.state, str(failure)))
                        return value
                    with patch('test_codex_navigation_native.time',
                               SimpleNamespace(monotonic=lambda: 0, sleep=lambda seconds: None)):
                        if transient:
                            self.assertIs(read_transcript(operation), expected)
                        else:
                            with self.assertRaises(transcripts.Unavailable) as caught:
                                read_transcript(operation)
                            self.assertNotIsInstance(caught.exception, transcripts.Retry)
                            self.assertEqual(str(caught.exception), str(failure))
                    self.assertEqual(attempts, [(root, directory, generation)] * (2 if transient else 1))

    def test_retry_keeps_the_original_cursor_and_does_not_hide_replacement(self):
        """A transient identity lock must not flake the native proof or refresh its old cursor and hide replacement."""
        for outcome in ('replaced', 'busy', 'late-success', 'unavailable', 'busy-info', 'late-success-info'):
            with self.subTest(outcome=outcome):
                root, directory, cursor = object(), object(), object()
                changed = transcripts.Changed('replaced', 'new-start', 'new-generation')
                error = transcripts.Unavailable('damaged') if outcome == 'unavailable' else transcripts.Retry('identity lock busy')
                clock, starts = [0.0], []
                def sleep(seconds):
                    clock[0] += seconds
                def read(*args, **kwargs):
                    starts.append(clock[0])
                    if outcome == 'replaced':
                        self.assertLessEqual(rows.call_count, 2, 'Replacement must propagate')
                        raise error if rows.call_count == 1 else changed
                    if outcome == 'unavailable':
                        self.assertEqual(rows.call_count, 1, 'Permanent unavailability must propagate')
                    self.assertLessEqual(rows.call_count, 5, 'Transcript retry deadline was reset')
                    if outcome.startswith('late-success') and clock[0] >= .2:
                        return 'a read started after the deadline'
                    if outcome in ('busy-info', 'late-success-info'):
                        return transcripts.TranscriptInfo(state='unavailable', reason=str(error), retry=True)
                    raise error
                with patch.object(transcripts, 'rows', side_effect=read) as rows, \
                        patch('test_codex_navigation_native.time',
                              SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)):
                    with self.assertRaises(type(changed) if outcome == 'replaced' else type(error)) as raised:
                        read_transcript(lambda: transcripts.rows(root, directory, after=cursor), timeout=.2)
                if outcome == 'replaced':
                    self.assertIs(raised.exception, changed)
                    self.assertEqual(rows.call_count, 2)
                elif outcome in ('busy', 'late-success', 'busy-info', 'late-success-info'):
                    self.assertGreater(rows.call_count, 1)
                    self.assertLessEqual(rows.call_count, 5)
                    self.assertLessEqual(clock[0], .2)
                    self.assertTrue(all(start < .2 for start in starts), starts)
                else:
                    self.assertEqual(rows.call_count, 1)
                self.assertEqual(rows.call_args_list, [call(root, directory, after=cursor)] * rows.call_count)


class PrivateWire:
    """Retain bounded navigation metadata; optionally fail one post-resume history read."""
    def __init__(self, path, upstream):
        self.path, self.upstream = str(path), upstream
        self.ready = threading.Event()
        self.records = []
        self.error = None
        self.connections = 0
        self.fail_history_target = None
        self.history_failed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            raise AssertionError(f'Private wire did not start: {self.error}')

    def _run(self):
        async def main():
            self.loop = asyncio.get_running_loop()
            self.stop = asyncio.Event()
            async with unix_serve(self.handle, self.path, compression=None, max_size=16 << 20,
                                  close_timeout=.2, ping_interval=None):
                self.ready.set()
                await self.stop.wait()
        try:
            asyncio.run(main())
        except Exception as error:
            self.error = repr(error)
            self.ready.set()

    async def handle(self, client):
        self.connections += 1
        connection = self.connections
        pending = {}
        fail_history = False
        async def forward(source, target, outgoing):
            nonlocal fail_history
            async for payload in source:
                frame = json.loads(payload)
                method, identifier = frame.get('method'), frame.get('id')
                if outgoing and method:
                    params = frame.get('params') or {}
                    if method in ('thread/start', 'thread/resume', 'thread/read', 'thread/fork',
                                  'thread/shellCommand', 'thread/turns/list', 'thread/queue/list',
                                  'turn/start', 'turn/interrupt', 'thread/queue/add', 'thread/queue/start',
                                  'thread/queue/delete', 'thread/backgroundTerminals/clean'):
                        pending[identifier] = (method, params.get('threadId'))
                        self.records.append({'connection': connection, 'direction': 'request',
                            'id': identifier, 'method': method, 'params': {
                                key: params[key] for key in ('threadId', 'cwd', 'threadSource',
                                    'ephemeral', 'historyMode', 'sessionStartSource', 'excludeTurns',
                                    'approvalPolicy', 'approvalsReviewer', 'sandbox', 'permissions',
                                    'runtimeWorkspaceRoots', 'clientUserMessageId', 'turnId')
                                if key in params},
                            'parameter_keys': sorted(params),
                            'config_keys': sorted((params.get('config') or {}).keys())})
                    if (fail_history and not self.history_failed.is_set()
                            and method == 'thread/turns/list'
                            and params.get('threadId') == self.fail_history_target):
                        await client.send(json.dumps({'id': identifier, 'error': {
                            'code': -32602, 'message': 'Private navigation history failure'}}))
                        pending.pop(identifier, None)
                        self.records.append({'connection': connection, 'direction': 'injected_error',
                                             'id': identifier, 'method': method, 'thread_id': self.fail_history_target,
                                             'code': -32602})
                        self.history_failed.set()
                        continue
                elif not outgoing and method == 'thread/settings/updated':
                    params = frame.get('params') or {}
                    settings = params.get('threadSettings')
                    self.records.append({'connection': connection, 'direction': 'notification',
                        'method': method, 'thread_id': params.get('threadId'),
                        'settings': {key: settings[key] for key in ('cwd', 'approvalPolicy', 'approvalsReviewer',
                            'sandboxPolicy', 'activePermissionProfile', 'runtimeWorkspaceRoots')
                            if key in settings} if isinstance(settings, dict) else None})
                elif not outgoing and not method and identifier in pending:
                    request, target_id = pending.pop(identifier)
                    result = frame.get('result') or {}
                    thread = result.get('thread') or {}
                    self.records.append({'connection': connection, 'direction': 'response',
                        'id': identifier, 'method': request,
                        'thread': {key: thread[key] for key in ('id', 'cwd', 'status', 'ephemeral', 'forkedFromId',
                                                              'path', 'parentThreadId', 'canAcceptDirectInput')
                                   if key in thread},
                        'cwd': result.get('cwd'),
                        'grants': {key: result[key] for key in ('approvalPolicy', 'approvalsReviewer', 'sandbox',
                                                              'activePermissionProfile', 'runtimeWorkspaceRoots')
                                   if key in result},
                        **({'error': frame['error']} if 'error' in frame else {})})
                    if (request == 'thread/resume' and target_id == self.fail_history_target
                            and thread.get('id') == target_id and 'error' not in frame):
                        fail_history = True
                if len(self.records) > 8192:
                    raise AssertionError('Private wire exceeded its metadata budget')
                await target.send(payload)
        try:
            async with unix_connect(self.upstream, compression=None, max_size=16 << 20,
                                    close_timeout=.2, ping_interval=None) as upstream:
                tasks = [asyncio.create_task(forward(client, upstream, True)),
                         asyncio.create_task(forward(upstream, client, False))]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except WebSocketException:
            pass  # Closing an owned frontend is normal fixture cleanup.
        except Exception as error:
            self.error = repr(error)
            raise

    def close(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stop.set)
            self.thread.join(5)
        if self.thread.is_alive() or self.error:
            raise AssertionError(f'Private wire cleanup failed: {self.error}')


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class NativeNavigationTest(TerminalBase):
    def test_native_new_clear_and_owned_resume_follow_exact_conversation(self):
        """Real navigation can succeed while records, callbacks or queued receipts silently stay on the old thread; fake frames cannot prove the checked TUI's selection sequence."""
        if subprocess.check_output(['codex', '--version'], stdin=subprocess.DEVNULL,
                                   text=True).strip() != 'codex-cli ' + version_text(CHECKED_VERSION):
            self.skipTest(f'native navigation evidence pinned to {version_text(CHECKED_VERSION)}')
        self.enterContext(patch.object(lifecycle, '_cli_path', return_value=mv2_launcher(self.work)))
        requests = []
        work = self.work / 'project'
        work.mkdir()
        write(self.root / 'universe.json', {**read(self.root / 'universe.json'), 'workspace': str(work)})
        subprocess.run(['git', 'init', '-q', str(work)], check=True, timeout=10)

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
                inputs = body.get('input') or []
                user = max((i for i, value in enumerate(inputs) if value.get('role') == 'user'), default=-1)
                tags = re.findall(r'NAV_[A-Z_]+', json.dumps(inputs[user]) if user >= 0 else '')
                tag = tags[-1] if tags else 'NAV_UNEXPECTED'
                output = any(value.get('type') == 'function_call_output' for value in inputs[user + 1:])
                if not title:
                    requests.append({'tag': tag, 'tool_result': output,
                                     'history': json.dumps(inputs)})
                index = len(requests)
                text = '{"title":"Private navigation"}' if title else 'NAV_DONE_' + tag
                item = {'type': 'message', 'role': 'assistant', 'id': f'nav-message-{index}',
                        'content': [{'type': 'output_text', 'text': text}]}
                if not title and not output and tag != 'NAV_SIDE':
                    code = ('import json,os,pathlib,subprocess,time; '
                            'root=pathlib.Path.cwd(); '
                            f'tag={tag!r}; '
                            'seen={"thread":os.environ.get("CODEX_THREAD_ID"),"cwd":str(root)}; '
                            'seen["callbacks"]={name:subprocess.run([os.environ["MV2_CLI"],*args],'
                            'capture_output=True,text=True,timeout=10).returncode for name,args in '
                            '[("bind",["session-bind","--session-id",seen["thread"]]),'
                            '("report",["report","--phase","done","--summary",tag]),'
                            '("inbox",["inbox","--json"])]}; '
                            '(root/(tag+".json")).write_text(json.dumps(seen)); '
                            'print(json.dumps(seen),flush=True); '
                            '\nif tag in ("NAV_HOLD", "NAV_PICKER_HOLD"):\n'
                            ' for _ in range(1200):\n'
                            '  if (root/"release-hold").exists(): break\n'
                            '  time.sleep(.05)\n')
                    item = {'type': 'function_call', 'call_id': f'nav-call-{index}', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': shlex.join([sys.executable, '-c', code]),
                                                     'yield_time_ms': 10000, 'max_output_tokens': 300})}
                events = [{'type': 'response.created', 'response': {'id': f'nav-response-{index}'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': f'nav-response-{index}',
                           'usage': {'input_tokens': 100 + index, 'output_tokens': 1,
                                     'total_tokens': 101 + index}}}]
                data = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

        provider = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        home = isolated_home(work / 'codex-home', config=
            'model="gpt-6-sol"\nmodel_provider="fixture"\nthread_unload_delay_secs=1\n'
            '[sandbox_workspace_write]\nexclude_tmpdir_env_var=true\nexclude_slash_tmp=true\n'
            '[features]\nplugins=false\n'
            '[model_providers.fixture]\nname="Private navigation fixture"\n'
            f'base_url="http://127.0.0.1:{provider.server_port}/v1"\n'
            'wire_api="responses"\nrequires_openai_auth=false\n'
            f'[projects.{json.dumps(str(work))}]\ntrust_level="trusted"\n')
        managed = home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        user_home = work / 'user-home'
        user_home.mkdir()
        env = {'HOME': str(user_home), 'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': '',
               'OPENAI_API_KEY': '', 'CODEX_API_KEY': '', 'NO_PROXY': '127.0.0.1,localhost',
               **{key: '' for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                      'http_proxy', 'https_proxy', 'all_proxy')}}
        daemon_env = {**os.environ, **env}
        write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex', 'settings': {
            'inbox_bridge': True, 'args': ['--sandbox', 'workspace-write', '--ask-for-approval', 'never']}})
        groups = PrivateDaemonGroups()
        self.enterContext(groups.observe_starts())
        original_start = native.start_shared_service
        wire = None

        def start_with_wire(*args, **kwargs):
            nonlocal wire
            endpoint = original_start(*args, **kwargs)
            if wire is None:
                wire = PrivateWire(work / 'native-wire.sock', endpoint.address)
            self.assertEqual(wire.upstream, endpoint.address)
            return replace(endpoint, address=wire.path)

        self.enterContext(patch.object(native, 'start_shared_service', side_effect=start_with_wire))
        def stop_daemon():
            try:
                subprocess.run(['codex', 'app-server', 'daemon', 'stop'], env=daemon_env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=True)
            finally:
                groups.close()
                print('Native navigation cleanup:', json.dumps({
                    'private_home': str(home),
                    'groups': {str(pid): group.closed for pid, group in groups.groups.items()}}))
        self.addCleanup(stop_daemon)
        self.addCleanup(lambda: wire.close() if wire else None)
        started = None
        def stop_agent():
            (work / 'release-hold').touch()
            if started is not None:
                lifecycle.kill(self.root, started['id'])
        self.addCleanup(stop_agent)
        with patch.dict(os.environ, env):
            started = lifecycle.start(self.root, 'navigation-test', 'NAV_A_SEED', 'codex',
                                      socket_path=self.socket, wait=True, wait_timeout=30)
        self.assertTrue(started['ready'], started)
        directory = agents.directory(self.root, started['id'])
        properties = lambda: read(directory / 'properties.json')
        current = lambda: properties()['native_session_id']
        original = current()
        launch = agents.runtime(directory)['launch_id']
        self.assertEqual(agents.runtime(directory)['native_versions'],
                         {'cli': version_text(CHECKED_VERSION), 'daemon': version_text(CHECKED_VERSION)})
        backend, pane = owned(properties(), agents.runtime(directory))
        def capture():
            try:
                current_backend, current_pane = owned(properties(), agents.runtime(directory))
                return current_backend.run('capture-pane', '-p', '-S', '-100', '-t', current_pane)
            except (OSError, RuntimeError, ValueError) as error:
                return f'Private pane unavailable: {error}'

        def idle():
            until(lambda: agents.runtime(directory).get('status') == 'idle', seconds=30)

        def command(text):
            backend.run('send-keys', '-t', pane, 'C-u')
            backend.run('send-keys', '-t', pane, '-l', text)
            until(lambda: text in capture(), seconds=10)
            backend.run('send-keys', '-t', pane, 'Enter')

        def callback(tag, target):
            path = work / (tag + '.json')
            until(path.exists, seconds=30)
            value = read(path)
            self.assertEqual(value, {'thread': target, 'cwd': str(work),
                                    'callbacks': {'bind': 0, 'report': 0, 'inbox': 0}})

        def message(tag, target, *, wait=True):
            (work / (tag + '.json')).unlink(missing_ok=True)
            result = send.run(self.root, agent=started['id'], text=tag, delivery='follow-up')
            self.assertEqual(result['state'], 'native_queued', result)
            saved = next(m for m in inbox(directory, all_messages=True)['messages'] if m['text'] == tag)
            if wait:
                callback(tag, target)
                until(lambda: (native.deliver(self.root, started['id'], saved['id'], check=True)
                               .get('value') or {}).get('transcript_confirmed') is True, seconds=30)
                idle()
            return saved['id']

        def switch(text, previous):
            generation = agents.runtime(directory).get('session_generation', 0)
            offset = len(wire.records)
            reason = 'resume' if text.startswith('/resume ') else text.removeprefix('/')
            method = 'thread/resume' if reason == 'resume' else 'thread/start'
            command(text)
            request = until(lambda: next((row for row in wire.records[offset:]
                if row['direction'] == 'request' and row['method'] == method and type(row['id']) is int), None), seconds=20)
            props, facts = until(lambda: committed_navigation(directory, launch=launch, previous=previous,
                generation=generation, request_id=request['id'], reason=reason), seconds=20)
            value = props['native_session_id']
            self.assertEqual(props['agent_identity']['native_id'], value)
            self.assertEqual(facts['launch_id'], launch)
            self.assertFalse(facts.get('native_refusal'))
            self.assertIn((facts.get('usage') or {}).get('native_session_id'), (None, value))
            return value

        try:
            callback('NAV_A_SEED', original)
            idle()
            command('/rename NAV_FIRST')
            initial = read_transcript(lambda: transcripts.info(self.root, directory))
            second = switch('/new', original)
            second_message = message('NAV_B_VERIFY', second)
            second_input = next(r['history'] for r in requests if r['tag'] == 'NAV_B_VERIFY')
            self.assertNotIn('NAV_DONE_NAV_A_SEED', second_input)
            with self.assertRaises(transcripts.Changed) as replaced:
                read_transcript(lambda: transcripts.rows(self.root, directory, after=initial.start))
            self.assertEqual(replaced.exception.reason, 'replaced')
            earlier_info = read_transcript(lambda: transcripts.info(self.root, directory))
            self.assertIn(original, {row.native_id for row in earlier_info.earlier}, earlier_info)
            earlier = next(row for row in earlier_info.earlier if row.native_id == original)
            original_info = read_transcript(lambda: transcripts.info(self.root, directory, generation=earlier.generation))
            self.assertEqual(original_info.native_id, original, original_info)
            hold = message('NAV_HOLD', second, wait=False)
            callback('NAV_HOLD', second)
            queued = message('NAV_QUEUED', second, wait=False)
            backend.run('send-keys', '-t', pane, 'Escape')
            until(lambda: (agents.runtime(directory).get('queue_state') or {}).get('state') == 'paused', seconds=20)
            idle()
            before = len(sessions.journal(directory))
            command('/new')
            until(lambda: 'queue' in capture().lower() or 'paused' in capture().lower(), seconds=10)
            self.assertEqual(current(), second)
            self.assertEqual(len(sessions.journal(directory)), before)
            command('/clear')
            until(lambda: 'queue' in capture().lower() or 'paused' in capture().lower(), seconds=10)
            self.assertEqual(current(), second)
            self.assertEqual(len(sessions.journal(directory)), before)
            self.assertFalse(agents.runtime(directory).get('native_refusal'))
            # Native /clear resets its visible history before RPC. Same-thread
            # native /resume is a local no-op, not a history-replay barrier.
            paused = native.deliver(self.root, started['id'], queued, check=True)
            self.assertTrue((paused.get('value') or {}).get('paused'), paused)
            (work / 'release-hold').touch()
            message('NAV_UNPAUSE', second)
            callback('NAV_QUEUED', second)
            self.assertEqual([r['tag'] for r in requests if not r['tool_result']][-2:],
                             ['NAV_QUEUED', 'NAV_UNPAUSE'])
            for message_id in (hold, queued):
                checked = native.deliver(self.root, started['id'], message_id, check=True)
                self.assertTrue((checked.get('value') or {}).get('transcript_confirmed'), checked)
            until(lambda: recovery.check(properties(), agents.runtime(directory), directory).state == 'ok', seconds=20)
            (work / 'release-hold').unlink()
            third = switch('/clear', second)
            message('NAV_C_VERIFY', third)
            command('/rename NAV_THIRD')
            third_input = next(r['history'] for r in requests if r['tag'] == 'NAV_C_VERIFY')
            self.assertNotIn('NAV_DONE_NAV_B_VERIFY', third_input)
            before_side = agents.runtime(directory).get('usage')
            command('/btw NAV_SIDE')
            until(lambda: 'NAV_DONE_NAV_SIDE' in capture(), seconds=20)
            self.assertEqual(current(), third)
            self.assertEqual(agents.runtime(directory).get('status'), 'idle')
            self.assertEqual(agents.runtime(directory).get('usage'), before_side)
            backend.run('send-keys', '-t', pane, 'C-c')
            until(lambda: 'NAV_DONE_NAV_SIDE' not in capture(), seconds=10)
            self.assertEqual(switch('/resume ' + original, third), original)
            message('NAV_A_RETURN', original)
            returned_input = next(r['history'] for r in requests if r['tag'] == 'NAV_A_RETURN')
            self.assertIn('NAV_DONE_NAV_A_SEED', returned_input)
            self.assertNotIn('NAV_DONE_NAV_B_VERIFY', returned_input)
            self.assertNotIn('NAV_DONE_NAV_C_VERIFY', returned_input)
            # History checks address the departed thread without loading it or
            # giving it current callback authority.
            _, adapter, selected, executor = native.context(self.root, started['id'])
            old = next(m for m in inbox(directory, all_messages=True)['messages'] if m['id'] == second_message)
            attempt_id = (old['tracking'].get('attempts') or [{}])[-1].get('native_message_id', second_message)
            def old_status():
                yield RpcRequest('initialize', {'clientInfo': {'name': 'private-navigation-read', 'version': '1'},
                    'capabilities': {'experimentalApi': True}}, on_success=(RpcNotification('initialized', {}),))
                reply = yield RpcRequest('thread/read', {'threadId': second, 'includeTurns': False})
                return NativeResult('ok', value=(reply.get('result', {}).get('thread', {}).get('status') or {}).get('type'))
            # A short private-only cache delay makes the unloaded historical
            # case deterministic; a read never loads or resumes that thread.
            until(lambda: executor.rpc(RpcSpec(selected.endpoint.address, old_status, max_requests=2)).value == 'notLoaded',
                  seconds=15)
            offset = len(wire.records)
            receipt = adapter.get_delivery(replace(selected, native_id=second), attempt_id, executor)
            self.assertTrue((receipt.value or {}).get('transcript_confirmed'), receipt)
            self.assertFalse(any(row.get('method') in ('thread/resume', 'thread/start')
                                 for row in wire.records[offset:]))
            command('/resume')
            until(lambda: 'Resume a previous session' in capture(), seconds=10)
            backend.run('send-keys', '-t', pane, '-l', 'NAV_THIRD')
            until(lambda: capture().count('NAV_THIRD') >= 2, seconds=10)
            picker_hold = message('NAV_PICKER_HOLD', original, wait=False)
            callback('NAV_PICKER_HOLD', original)
            until(lambda: agents.runtime(directory).get('status') == 'working', seconds=20)
            before = len(sessions.journal(directory))
            backend.run('send-keys', '-t', pane, 'Enter')
            until(lambda: 'Failed to resume session' in capture(), seconds=15)
            self.assertEqual(current(), original)
            self.assertEqual(len(sessions.journal(directory)), before)
            self.assertFalse(agents.runtime(directory).get('native_refusal'))
            (work / 'release-hold').touch()
            idle()
            checked = native.deliver(self.root, started['id'], picker_hold, check=True)
            self.assertTrue((checked.get('value') or {}).get('transcript_confirmed'), checked)
            command('/resume')
            until(lambda: 'Resume a previous session' in capture(), seconds=10)
            backend.run('send-keys', '-t', pane, '-l', 'NAV_THIRD')
            until(lambda: capture().count('NAV_THIRD') >= 2, seconds=10)
            backend.run('send-keys', '-t', pane, 'Enter')
            until(lambda: current() == third, seconds=20)
            idle()
            # This one controlled fault exercises the approved RPC boundary,
            # not a claim that the visible widget has acknowledged selection.
            wire.fail_history_target = original
            command('/resume ' + original)
            until(lambda: current() == original and wire.history_failed.is_set(), seconds=20)
            # The failed history hydration leaves the widget on third. A raw
            # shell request there must fail locally rather than execute as it.
            # The checked TUI treats this RPC error as fatal, not a pane notice.
            stale_shell = work / 'STALE_WIDGET_SHELL_SHOULD_NOT_RUN'
            offset = len(wire.records)
            before_shell_journal = sessions.journal(directory)
            backend.run('send-keys', '-t', pane, 'C-u')
            backend.run('send-keys', '-t', pane, '-l', '!touch ' + shlex.quote(stale_shell.name))
            until(lambda: stale_shell.name in capture(), seconds=10)
            backend.run('send-keys', '-t', pane, 'Enter')
            stale_shell_exit = until(lambda: facts if (
                (facts := agents.runtime(directory)).get('launch_id') == launch
                and facts.get('exit_code') == 1
                and 'thread/shellCommand failed in TUI' in facts.get('reason', '')) else None, seconds=15)
            self.assertFalse(stale_shell.exists())
            self.assertFalse(any(row.get('method') == 'thread/shellCommand' for row in wire.records[offset:]))
            self.assertEqual(current(), original)
            self.assertEqual(properties()['agent_identity']['native_id'], original)
            self.assertEqual(sessions.journal(directory), before_shell_journal)
            lifecycle.kill(self.root, started['id'])
            with patch.dict(os.environ, env):
                resumed = lifecycle.start(self.root, resume=started['id'], socket_path=self.socket,
                                          wait=True, wait_timeout=30)
            self.assertTrue(resumed['ready'], resumed)
            backend, pane = owned(properties(), agents.runtime(directory))
            launch = resumed['runtime']['launch_id']
            self.assertEqual(current(), original)
            message('NAV_A_AFTER_REATTACH', original)
            for message_id in (hold, queued):
                checked = native.deliver(self.root, started['id'], message_id, check=True)
                self.assertTrue((checked.get('value') or {}).get('transcript_confirmed'), checked)
            self.assertEqual(deliver.run(self.root, agent=started['id'], check=True)['messages'], [])
            navigation = [row for row in sessions.journal(directory) if row.get('reason') in ('new', 'clear', 'resume')]
            self.assertEqual([(row['from'], row['to'], row['reason']) for row in navigation], [
                (original, second, 'new'), (second, third, 'clear'),
                (third, original, 'resume'), (original, third, 'resume'), (third, original, 'resume')])
            lineage_info = read_transcript(lambda: transcripts.info(self.root, directory))
            self.assertEqual({row.native_id for row in lineage_info.earlier}, {second, third}, lineage_info)
            # Reattachment keeps native saved grants, which a following /new
            # must inherit even though the resume CLI has no --add-dir flag.
            reattached = switch('/new', original)
            message('NAV_NEW_AFTER_REATTACH', reattached)
            reattached_transition = sessions.journal(directory)[-1]
            before_empty = len(requests)
            empty = switch('/new', reattached)
            self.assertEqual(len(requests), before_empty, 'A fresh conversation must not run a hidden first prompt')
            stop_offset = len(wire.records)
            empty_stop = lifecycle.kill(self.root, started['id'])
            self.assertEqual(empty_stop['native_cancellation']['state'], 'ok', empty_stop)
            self.assertEqual(empty_stop['native_cancellation']['interrupted_turns'], 0)
            self.assertTrue(empty_stop['native_cancellation']['background_terminal_cleanup_requested'])
            self.assertEqual(current(), empty)
            cleanup = next(row for row in wire.records[stop_offset:] if row['direction'] == 'request'
                           and row['method'] == 'thread/backgroundTerminals/clean')
            stopped = [row for row in wire.records[stop_offset:] if row['connection'] == cleanup['connection']]
            self.assertEqual([row['method'] for row in stopped if row['direction'] == 'request'],
                ['thread/read', 'thread/queue/list', 'thread/turns/list', 'thread/read', 'thread/backgroundTerminals/clean'])
            empty_error = next(row['error'] for row in stopped if row.get('error'))
            self.assertEqual(empty_error, {'code': -32600, 'message': f'thread {empty} is not materialized yet; '
                                          'thread/turns/list is unavailable before first user message'})
            primary_replies = [row for row in wire.records if row.get('direction') == 'response'
                and row.get('method') in ('thread/start', 'thread/resume')
                and row.get('thread', {}).get('id') in (original, second, third, reattached, empty)]
            def grant_set(row):
                grants = row['grants']
                return {**grants, 'runtimeWorkspaceRoots': sorted(set(grants['runtimeWorkspaceRoots'])),
                        'sandbox': {**grants['sandbox'],
                                    'writableRoots': sorted(set(grants['sandbox']['writableRoots']))}}
            effective_grants = grant_set(primary_replies[0])
            self.assertEqual(effective_grants['approvalPolicy'], 'never')
            self.assertEqual(effective_grants['sandbox']['type'], 'workspaceWrite')
            self.assertIn(str(directory), effective_grants['sandbox']['writableRoots'])
            self.assertFalse(directory.is_relative_to(work))
            for row in primary_replies:
                self.assertEqual(grant_set(row), effective_grants, row)
            starts = [row for row in wire.records if row.get('direction') == 'request'
                      and row.get('method') == 'thread/start' and type(row.get('id')) is int]
            self.assertEqual([row['params'].get('sessionStartSource') for row in starts], [None, 'clear', None, None])
            for row in starts:
                self.assertEqual(row['params'].get('cwd'), str(work), row)
                self.assertEqual(row['params'].get('threadSource'), 'user', row)
                self.assertIs(row['params'].get('ephemeral'), False, row)
            print('Native navigation evidence:', json.dumps({'threads': [original, second, third],
                  'navigation': navigation, 'requests': wire.records,
                  'historical_receipt': {'native_id': second, 'message_id': attempt_id,
                                         'state': receipt.state, 'value': receipt.value},
                  'provider_turns': [r['tag'] for r in requests if not r['tool_result']],
                  'history_fault': wire.history_failed.is_set(),
                  'reattached_navigation': {'native_id': reattached, 'grants': effective_grants,
                                            'transition': reattached_transition,
                                            'callback': read(work / 'NAV_NEW_AFTER_REATTACH.json')},
                  'empty_thread_stop': {'native_id': empty, 'result': empty_stop, 'error': empty_error,
                                        'transition': sessions.journal(directory)[-1]},
                  'stale_shell_refusal': {key: stale_shell_exit[key] for key in ('exit_code', 'reason')}}))
        except Exception:
            print('Native navigation diagnostic:', json.dumps({'properties': properties(),
                  'runtime': agents.runtime(directory), 'wire': wire.records,
                  'provider_turns': [(r['tag'], r['tool_result']) for r in requests], 'pane': capture()}))
            raise
