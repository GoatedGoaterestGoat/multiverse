"""Multiple real Codex terminals/universes on one disposable native daemon.

The provider is a local deterministic HTTP fixture; no credentials or paid calls.
"""
from concurrent.futures import ThreadPoolExecutor
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

from multiverse import agents, native, rpc, universe
from multiverse.harnesses.codex.protocol import _thread, result as rpc_result
from multiverse.harnesses.codex.launch import name_session
from multiverse.harnesses.types import NativeResult, RpcSpec, RpcRequest
from multiverse.store import inbox, read, write
from multiverse.tools import send
from test_multiverse import TerminalBase, until


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class CodexDaemonTest(TerminalBase):
    def test_concurrent_universes_share_daemon_and_keep_native_identity(self):
        seen_requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                title = 'title' in value.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
                if not title:
                    seen_requests.append(value)
                if title:
                    text = '{"title":"Offline daemon test"}'
                else:
                    text = 'Offline test finished.'
                index = len(seen_requests)
                item = {'type': 'message', 'role': 'assistant', 'id': f'msg-{index}',
                        'content': [{'type': 'output_text', 'text': text}]}
                if not title and not any(x.get('type') == 'function_call_output' for x in value.get('input', [])):
                    code = ('import os,json,pathlib; '
                            'pathlib.Path("identity.json").write_text(json.dumps({"cwd":os.getcwd(),'
                            '"env":{k:v for k,v in os.environ.items() if k.startswith("MV2_") or k in ("CODEX_THREAD_ID","CLAUDECODE","USER_EXCLUDED")}}))')
                    command = ('"$MV2_CLI" session-bind --session-id "$CODEX_THREAD_ID"; '
                               + shlex.quote(sys.executable) + ' -c ' + shlex.quote(code) + '; sleep 3')
                    item = {'type': 'function_call', 'call_id': f'call-{index}', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': command, 'yield_time_ms': 10000, 'max_output_tokens': 200})}
                last = value.get('input', [{}])[-1]
                if not title and last.get('role') == 'user' and 'STOP_ACTIVE_WORK' in json.dumps(last):
                    item = {'type': 'function_call', 'call_id': f'call-{index}', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': 'touch active-start; sleep 30; touch active-end',
                                                     'yield_time_ms': 10000, 'max_output_tokens': 50})}
                events = [{'type': 'response.created' , 'response': {'id': f'resp-{index}'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': f'resp-{index}',
                           'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

        provider = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        home = self.work / 'codex-home'
        managed = home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        workspaces = [self.work / 'project-a', self.work / 'project-b']
        for work in workspaces:
            work.mkdir()
        (home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="offline"\n'
            '[shell_environment_policy]\nexclude=["USER_EXCLUDED"]\n'
            '[model_providers.offline]\nname="Offline"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'base_url="http://127.0.0.1:{provider.server_port}/v1"\n' +
            ''.join(f'[projects.{json.dumps(str(work))}]\ntrust_level="trusted"\n' for work in workspaces))
        env = {**os.environ, 'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': str(home),
               'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '', 'http_proxy': '', 'https_proxy': '', 'all_proxy': '',
               'NO_PROXY': '127.0.0.1,localhost', 'USER_EXCLUDED': 'must-remain-excluded', 'MV2_FOREIGN_AGENT': 'must-not-leak', 'CLAUDECODE': 'foreign'}
        def daemon(command):
            result = subprocess.run(['codex', 'app-server', 'daemon', command], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.addCleanup(lambda: daemon('stop'))
        with ThreadPoolExecutor(max_workers=4) as pool:
            started = list(pool.map(lambda _: daemon('start'), range(4)))
        self.assertEqual(sum(x['status'] == 'started' for x in started), 1, started)
        self.assertEqual(len({x['socketPath'] for x in started}), 1)
        endpoint = started[0]['socketPath']
        def thread_name(root, agent_id):
            _, _, session, _ = native.context(root, agent_id)
            def plan():
                thread = yield from _thread(session)
                return thread if isinstance(thread, NativeResult) else NativeResult('ok', value=thread.get('name'))
            return rpc.execute(RpcSpec(endpoint, plan)).value
        roots = [universe.create(work / '.multiverse', 'project', workdir=work) for work in workspaces]
        for root in roots:
            path = root / 'harnesses/codex/harness.json'
            spec = read(path)
            spec['settings'] = {'spawn_args': ['--sandbox', 'workspace-write', '--ask-for-approval', 'never']}
            write(path, spec)
        with patch.dict(os.environ, env), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda root: agents.start(root, 'same-name', 'Record identity then finish.',
                                                            'codex', socket_path=self.socket,
                                                            wait=True, wait_timeout=30), roots))
        self.assertTrue(all(result['ready'] for result in results), results)
        self.assertTrue(all(result['runtime']['native_mode'] == {
            'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None} for result in results))
        self.backend.run('set-option', '-g', 'remain-on-exit', 'on')
        pairs = list(zip(roots, results))
        def stop_terminals():
            pids = []
            for root, result in pairs:
                pid = agents.runtime(agents.directory(root, result["id"])).get("pid")
                if pid:
                    pids.append(pid)
                try:
                    agents.terminal_action(root, result['id'], 'stop')
                except (ValueError, OSError, RuntimeError):
                    pass
            self.kill_server()
            def exited(pid):
                try:
                    os.kill(pid, 0)
                    return False
                except ProcessLookupError:
                    return True
            until(lambda: all(exited(pid) for pid in pids), seconds=10)
        self.addCleanup(stop_terminals)
        try:
            for work, root, launched in zip(workspaces, roots, results):
                until(lambda: (work / 'identity.json').exists(), seconds=30)
                identity = read(work / 'identity.json')
                directory = agents.directory(root, launched['id'])
                properties = read(directory / 'properties.json')
                self.assertEqual(identity['cwd'], str(work))
                self.assertEqual(identity['env']['MV2_AGENT_ID'], launched['id'])
                self.assertEqual(identity['env']['MV2_UNIVERSE'], str(root))
                self.assertEqual(identity['env']['MV2_LAUNCH_ID'], launched['runtime']['launch_id'])
                self.assertEqual(identity['env']['CODEX_THREAD_ID'], properties['native_session_id'])
                self.assertNotIn('MV2_FOREIGN_AGENT', identity['env'])
                self.assertNotIn('CLAUDECODE', identity['env'])
                self.assertNotIn('USER_EXCLUDED', identity['env'])
                self.assertEqual(agents.runtime(directory)['control_endpoint']['address'], endpoint)
                self.assertEqual(thread_name(root, launched['id']), 'same-name')
            # Two independent shared-tool callers target distinct native threads.
            with ThreadPoolExecutor(max_workers=2) as pool:
                sent = list(pool.map(lambda pair: send.run(pair[0], agent=pair[1]['id'],
                           text='ONLY_FOR_' + pair[1]['id'], delivery='follow-up'), pairs))
            for message in sent:
                self.assertEqual(message['state'], 'native_queued', message)
            until(lambda: len(seen_requests) >= 6, seconds=20)
            for request in seen_requests:
                self.assertFalse(all('ONLY_FOR_' + r['id'] in json.dumps(request) for r in results),
                                 'Messages from separate universes must never mix')
            # Ending one client cannot kill the server or affect its sibling.
            message = send.run(roots[0], agent=results[0]['id'], text='STOP_ACTIVE_WORK', delivery='follow-up')
            self.assertEqual(message['state'], 'native_queued', message)
            until(lambda: (workspaces[0] / 'active-start').exists())
            queued = send.run(roots[0], agent=results[0]['id'], text='CANCEL_ON_STOP', delivery='follow-up')
            self.assertEqual(queued['state'], 'native_queued')
            _, _, first_session, _ = native.context(roots[0], results[0]['id'])
            agents.terminal_action(roots[0], results[0]['id'], 'stop')
            def check_stopped(session=first_session):
                def plan():
                    result = yield from _thread(session)
                    if isinstance(result, NativeResult):
                        return result
                    terminals = rpc_result((yield RpcRequest('thread/backgroundTerminals/list', {'threadId': session.native_id})))
                    return NativeResult('ok', value={'thread': result, 'terminals': terminals})
                observed = rpc.execute(RpcSpec(endpoint, plan))
                return observed.state == 'unavailable' or (observed.state == 'ok' and observed.value['thread']['status']['type'] != 'active' and not observed.value['terminals'].get('data'))
            until(check_stopped, seconds=5)
            self.assertFalse((workspaces[0] / 'active-end').exists())
            saved = inbox(agents.directory(roots[0], results[0]['id']), all_messages=True)['messages']
            queued_id = next(m['id'] for m in saved if m['text'] == 'CANCEL_ON_STOP')
            retried = native.deliver(roots[0], results[0]['id'], queued_id, retry=True)
            self.assertFalse(retried['ok'])
            self.assertIn('Cancelled', retried['reason'])
            self.assertFalse(any('CANCEL_ON_STOP' in json.dumps(r) for r in seen_requests))
            self.assertEqual(daemon('version')['status'], 'running')
            self.assertEqual(native.capabilities(roots[1], results[1]['id'], run_probe=True)['send_steer']['state'], 'supported')
            # Resume reapplies the mv2 name even if the native session was renamed.
            renamed = rpc.execute(RpcSpec(endpoint, lambda: name_session(first_session.native_id, 'renamed-native')))
            self.assertEqual(renamed.state, 'ok')
            self.assertEqual(thread_name(roots[0], results[0]['id']), 'renamed-native')
            # Resume from a monitor with a DIFFERENT home must retain saved storage.
            with patch.dict(os.environ, {**env, 'CODEX_HOME': str(self.work / 'wrong-home')}):
                resumed = agents.start(roots[0], resume=results[0]['id'], socket_path=self.socket)
            self.assertEqual(resumed['native_environment']['CODEX_HOME'], str(home))
            self.assertEqual(resumed['native_session_id'], read(agents.directory(roots[0], resumed['id']) / 'properties.json')['native_session_id'])
            until(lambda: native.capabilities(roots[0], resumed['id'], run_probe=True)['send_steer']['state'] == 'supported')
            until(lambda: thread_name(roots[0], resumed['id']) == 'same-name')
            self.assertEqual(agents.runtime(agents.directory(roots[0], resumed['id']))['control_endpoint']['address'], endpoint)
            # Work on the shared daemon survives losing its TUI. Stop still
            # cancels only this exact thread after its pane has disappeared.
            message = send.run(roots[1], agent=results[1]['id'], text='STOP_ACTIVE_WORK_ORPHAN', delivery='follow-up')
            self.assertEqual(message['state'], 'native_queued', message)
            until(lambda: (workspaces[1] / 'active-start').exists())
            message = send.run(roots[1], agent=results[1]['id'], text='ORPHAN_CANCEL_ON_STOP', delivery='follow-up')
            self.assertEqual(message['state'], 'native_queued', message)
            _, _, second_session, _ = native.context(roots[1], results[1]['id'])
            self.backend.run('kill-pane', '-t', results[1]['runtime']['tmux']['pane'])
            until(lambda: agents.listing(roots[1])[0]['live'] == 'dormant')
            cancelled = agents.terminal_action(roots[1], results[1]['id'], 'stop')['native_cancellation']
            self.assertEqual(cancelled['cancelled_submissions'], 1)
            self.assertEqual(cancelled['interrupted_turns'], 1)
            self.assertTrue(cancelled['background_terminal_cleanup_requested'])
            self.assertNotIn('cancelled_message_ids', cancelled)
            until(lambda: check_stopped(second_session), seconds=5)
            self.assertFalse((workspaces[1] / 'active-end').exists())
            saved = inbox(agents.directory(roots[1], results[1]['id']), all_messages=True)['messages']
            self.assertEqual(next(m for m in saved if m['text'] == 'ORPHAN_CANCEL_ON_STOP')['tracking']['submission_state'], 'cancelled')
            self.assertEqual(daemon('version')['status'], 'running')
            self.assertEqual(native.capabilities(roots[0], resumed['id'], run_probe=True)['send_steer']['state'], 'supported')
            # Daemon outage must not trigger a hidden restart/resume on send.
            daemon('stop')
            failed = send.run(roots[0], agent=resumed['id'], text='OUTAGE_MESSAGE', delivery='steer')
            self.assertTrue(failed['saved'])
            self.assertNotEqual(failed['state'], 'native_queued')
            self.assertFalse(Path(endpoint).exists(), 'Failed send must not restart the daemon')
            print('Shared daemon evidence:', json.dumps({'startup_statuses': [s['status'] for s in started],
                  'universes': len(roots), 'unique_agent_ids': len({r['id'] for r in results}),
                  'socket': endpoint, 'outage_delivery': failed['state']}))
        except Exception:
            for root, result in pairs:
                directory = agents.directory(root, result['id'])
                facts = agents.runtime(directory)
                print('RUNTIME', facts)
                try:
                    print(self.backend.run('capture-pane', '-p', '-S', '-160', '-t', facts['tmux']['pane']))
                except Exception:
                    pass
            raise
