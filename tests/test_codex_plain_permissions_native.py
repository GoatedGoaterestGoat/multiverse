"""The native permission resolver decides whether the plain extra root is usable."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

from multiverse.harnesses.types import SharedServiceSpec
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from multiverse.terminal.tmux import owned
from multiverse.universe import agents, templates
from native_cli import mv2_launcher
from native_processes import PrivateDaemonGroups
from native_safety import isolated_home
from test_multiverse import TerminalBase, until


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class NativePlainPermissionsTest(TerminalBase):
    def test_native_policy_keeps_writable_agent_roots_and_retries_only_readonly(self):
        """Native trust and named permission defaults can silently remove an outside agent root; real tool writes and one-shot read-only retries must survive spawn and resume."""
        version = subprocess.check_output(['codex', '--version'], stdin=subprocess.DEVNULL, text=True).strip()
        if version != 'codex-cli 0.163.0':
            self.skipTest('native permission resolver evidence requires checked Codex 0.163.0')
        self.enterContext(patch.object(lifecycle, '_cli_path', return_value=mv2_launcher(self.work)))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                title = 'title' in body.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
                inputs = body.get('input', [])
                item = {'type': 'message', 'role': 'assistant', 'id': 'msg',
                        'content': [{'type': 'output_text', 'text': '{"title":"Private root probe"}' if title else 'Done.'}]}
                if not title and (not inputs or inputs[-1].get('type') != 'function_call_output'):
                    command = ('touch "$MV2_AGENT_DIR/native-write" && '
                               'printf %s "$CODEX_THREAD_ID" > "$MV2_AGENT_DIR/native-thread" && '
                               '"$MV2_CLI" report --phase working --summary root-proof && '
                               '"$MV2_CLI" inbox && { "$MV2_CLI" session-bind --session-id "$CODEX_THREAD_ID" '
                               '> "$MV2_AGENT_DIR/bind-result" 2>&1; touch "$MV2_AGENT_DIR/callbacks-complete"; }')
                    item = {'type': 'function_call', 'call_id': 'root-proof', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': command, 'yield_time_ms': 1000, 'max_output_tokens': 200})}
                events = [{'type': 'response.created', 'response': {'id': 'response'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': 'response',
                           'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
                payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'
                                  for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        rows = [('trusted', 'trusted', False, [], True),
                ('recorded-untrusted', 'untrusted', False, [], True),
                ('no-trust-decision-auto', None, True, [], False),
                ('explicit-readonly', 'trusted', False, ['--sandbox', 'read-only'], False),
                ('named-writable', 'trusted', False, [], True)]
        for label, decision, auto, flags, writable in rows:
            with self.subTest(policy=label):
                workspace = self.work / label / 'workspace'
                workspace.mkdir(parents=True)
                if decision is not None:
                    subprocess.run(['git', 'init', '-q', str(workspace)], stdin=subprocess.DEVNULL, check=True)
                root = templates.create(self.work / label / 'universe/.multiverse', label, workdir=workspace)
                permission_config = ('default_permissions="writer"\n' if label == 'named-writable' else '')
                named_config = ('[permissions.writer]\nextends=":workspace"\n[permissions.writer.filesystem]\n'
                                '":tmpdir"="read"\n":slash_tmp"="read"\n' if label == 'named-writable' else '')
                home = isolated_home(self.work / label / 'native-home', config=permission_config +
                    'model="gpt-6-sol"\nmodel_provider="fixture"\n'
                    '[sandbox_workspace_write]\nexclude_tmpdir_env_var=true\nexclude_slash_tmp=true\n'
                    '[features]\nplugins=false\n'
                    '[model_providers.fixture]\nname="Private roots fixture"\n'
                    f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
                    'wire_api="responses"\nrequires_openai_auth=false\n'
                    + (f'[projects.{json.dumps(str(workspace))}]\ntrust_level={json.dumps(decision)}\n' if decision else '')
                    + named_config)
                settings = {'args': [*flags, '--ask-for-approval', 'never']}
                if auto:
                    settings['args'].append('--no-daemon')  # Exercise automatic plain fallback.
                else:
                    settings['inbox_bridge'] = False
                write(root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex', 'settings': settings})
                env = {'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': str(home),
                       'NO_PROXY': '127.0.0.1,localhost', **{key: '' for key in
                        ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')}}
                groups = PrivateDaemonGroups()
                result = None
                try:
                    # Capture the one private service before any frontend starts;
                    # no daemon owned by the caller can be reached by this fixture.
                    with patch.dict(os.environ, env), groups.observe_starts():
                        native.start_shared_service(SharedServiceSpec(
                            ('codex', 'app-server', 'daemon', 'start'), 'socketPath',
                            ('cliVersion', 'appServerVersion'), env, cwd=str(self.work)),
                            workspace, self.work / (label + '-start.log'), ('CODEX_THREAD_ID',))
                        result = lifecycle.start(root, label, 'WRITE_AGENT_ROOT', socket_path=self.socket)
                    directory = agents.directory(root, result['id'])
                    self.assertNotIn(workspace, directory.parents)
                    answered = set()
                    def open_restricted():
                        facts = agents.runtime(directory)
                        backend, pane = owned(read(directory / 'properties.json'), facts)
                        screen = backend.run('capture-pane', '-p', '-t', pane)
                        if 'Folder access' in screen and '1. Open restricted' in screen and facts.get('pid') not in answered:
                            answered.add(facts.get('pid'))
                            backend.run('send-keys', '-t', pane, 'Enter')
                    if writable:
                        def complete():
                            if (directory / 'callbacks-complete').exists():
                                return True
                            open_restricted()
                            return False
                        until(complete, seconds=30)
                        native_id = (directory / 'native-thread').read_text()
                        print('Native bind diagnostic:', label, (directory / 'bind-result').read_text().strip())
                        self.assertEqual(read(directory / 'properties.json').get('native_session_id'), native_id,
                                         (directory / 'bind-result').read_text())
                        self.assertFalse(agents.runtime(directory).get('startup_retry'))
                        self.assertTrue(native_id)
                        if label == 'trusted':
                            lifecycle.kill(root, result['id'])
                            (directory / 'native-write').unlink()
                            (directory / 'callbacks-complete').unlink()
                            with patch.dict(os.environ, env):
                                result = lifecycle.start(root, resume=result['id'], socket_path=self.socket,
                                                         resume_prompt='WRITE_AGENT_ROOT_AGAIN')
                            until(lambda: (directory / 'callbacks-complete').exists(), seconds=30)
                            self.assertEqual(read(directory / 'properties.json')['native_session_id'], native_id)
                            self.assertFalse(agents.runtime(directory).get('startup_retry'))
                    else:
                        def readonly_exit():
                            if agents.runtime(directory).get('startup_retry'):
                                return True
                            open_restricted()
                            return False
                        until(readonly_exit, seconds=20)
                        facts = agents.runtime(directory)
                        self.assertEqual(facts['startup_retry']['exit_code'], 1)
                        until(lambda: agents.runtime(directory).get('startup_checked'), seconds=10)
                        backend, pane = owned(read(directory / 'properties.json'), agents.runtime(directory))
                        screen = backend.run('capture-pane', '-p', '-t', pane)
                        self.assertNotIn('Error adding directories', screen)
                        current = read(directory / 'launches' / facts['launch_id'] / 'request.json')
                        self.assertIsNone(current['startup_retry'], 'The retry is consumed before starting again')
                        self.assertNotIn('--add-dir', current['argv'])
                        self.assertTrue(any('read-only' in note for note in lifecycle.launch_result(root, directory)['notices']))
                    print('Native extra-root evidence:', label, 'writable=', writable,
                          'retried=', bool(agents.runtime(directory).get('startup_retry')))
                except Exception:
                    if result is not None:
                        directory = agents.directory(root, result['id'])
                        facts = agents.runtime(directory)
                        print('Native root failure:', label, {key: facts.get(key) for key in
                              ('state', 'startup_checked', 'startup_retry', 'native_startup_observation')})
                        try:
                            backend, pane = owned(read(directory / 'properties.json'), facts, states=('alive', 'stopped'))
                            print(backend.run('capture-pane', '-p', '-t', pane)[-4096:])
                        except (ValueError, OSError, RuntimeError):
                            pass
                    raise
                finally:
                    if result is not None:
                        lifecycle.kill(root, result['id'])
                    try:
                        subprocess.run(['codex', 'app-server', 'daemon', 'stop'], env={**os.environ, **env},
                                       stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=True)
                    finally:
                        groups.close()
