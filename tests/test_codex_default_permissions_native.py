"""Default plain launch permissions, with a private native home and no model turn."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import unittest

from native_processes import PrivateProcessGroup
from native_safety import isolated_home
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from multiverse.universe import agents
from test_multiverse import TerminalBase, ROOT, until


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class DefaultPermissionsTest(TerminalBase):
    def test_default_plain_spawn_and_resume_keep_native_permissions(self):
        if subprocess.check_output(['codex', '--version'], text=True,
                                   stdin=subprocess.DEVNULL).strip() != 'codex-cli 0.163.0':
            self.skipTest('native default permission evidence pinned to 0.163.0')
        for name, trusted, sandbox, auto in (('untrusted-spawn', False, None, False),
                                             ('untrusted-fallback', False, None, True),
                                             ('trusted-spawn', True, None, False),
                                             ('trusted-read-only-resume', True, 'read-only', False),
                                             ('untrusted-read-only-resume', False, 'read-only', False),
                                             ('trusted-writable-resume', True, 'workspace-write', False)):
            with self.subTest(case=name):
                home = isolated_home(self.work / name, config=
                    'model="fixture"\nmodel_provider="fixture"\n'
                    '[model_providers.fixture]\nname="Private permission fixture"\n'
                    'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
                    + (f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n' if trusted else ''))
                environment = {**os.environ, 'CODEX_HOME': str(home)}
                identifier = None
                if sandbox:
                    thread = asyncio.run(self.seed_thread(environment, sandbox))
                    directory, properties, _, _ = lifecycle._record(
                        self.root, name, '', 'codex', None, None)
                    properties.update(workspace=str(self.work), native_session_id=thread,
                                      native_environment={'CODEX_HOME': str(home)})
                    write(directory / 'properties.json', properties)
                    identifier = properties['id']
                settings = {'args': ['--no-daemon']}
                if not auto:
                    settings['inbox_bridge'] = False
                write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex',
                      'settings': settings})
                state = {'id': identifier, 'group': None, 'closed': False}
                def cleanup(state=state):
                    if state['closed']:
                        return
                    try:
                        if state['id']:
                            lifecycle.kill(self.root, state['id'])
                    finally:
                        if state['group']:
                            state['group'].close()
                    state['closed'] = True
                self.addCleanup(cleanup)
                arguments = (['resume', identifier] if sandbox else
                             ['spawn', '--name', name, '--harness', 'codex'])
                launched = subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root),
                    *arguments, '--json'], env={**environment, 'MV2_TMUX_SOCKET': self.socket},
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, cwd=self.work)
                self.assertEqual(launched.returncode, 0, launched.stderr)
                result = json.loads(launched.stdout)
                state['id'] = identifier = result['id']
                directory = agents.directory(self.root, identifier)
                pane = agents.runtime(directory)['tmux']['pane']
                state['group'] = PrivateProcessGroup(int(self.backend.run('display-message', '-p', '-t', pane, '#{pane_pid}')))
                try:
                    self.assertEqual(len(result['notices']), 1)
                    self.assertIn('omitted its extra writable agent folder', result['notices'][0])
                    self.assertIn(json.dumps(name), result['notices'][0])
                    self.assertIn(json.dumps(str(directory)), result['notices'][0])
                    self.assertEqual(result['runtime']['launch_notices'], result['notices'])
                    request = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
                    self.assertEqual(request['notices'], result['notices'])
                    def settled():
                        frame = self.backend.run('capture-pane', '-p', '-t', pane)
                        current = agents.runtime(directory)
                        return frame if ('exit_code' in current
                            or current.get('native_startup_observation', {}).get('state') == 'started'
                            or 'Error adding directories' in ' '.join(frame.split())
                            or any(text in ' '.join(frame.casefold().split()) for text in (
                                'trust this folder', 'trust the contents'))) else None
                    screen = until(settled, seconds=15)
                    self.assertNotIn('Error adding directories', ' '.join(screen.split()))
                    self.assertNotIn('exit_code', agents.runtime(directory), screen)
                    if sandbox:
                        self.assertEqual(read(directory / 'properties.json')['native_session_id'], thread)
                finally:
                    cleanup()

    async def seed_thread(self, environment, sandbox):
        process = await asyncio.create_subprocess_exec('codex', 'app-server', env=environment,
            cwd=self.work, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        group = PrivateProcessGroup(process.pid)
        async def request(identifier, method, params):
            process.stdin.write((json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n').encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 10)
                self.assertTrue(line, 'private seed server exited before its response')
                value = json.loads(line)
                if value.get('id') == identifier:
                    self.assertNotIn('error', value, value)
                    return value['result']
        try:
            await request(1, 'initialize', {'clientInfo': {'name': 'private-permission-fixture', 'version': '1'}})
            process.stdin.write(b'{"method":"initialized","params":{}}\n')
            await process.stdin.drain()
            result = await request(2, 'thread/start', {'cwd': str(self.work), 'ephemeral': False,
                'sandbox': sandbox, 'approvalPolicy': 'never'})
            thread = result['thread']['id']
            await request(3, 'thread/name/set', {'threadId': thread, 'name': 'private permission fixture'})
            return thread
        finally:
            process.stdin.close()
            await asyncio.to_thread(group.close)
            await asyncio.wait_for(process.wait(), 5)
