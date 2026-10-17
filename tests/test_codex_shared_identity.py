"""Model-free native evidence for one agent across shared Codex attachments."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock
from uuid import uuid4

from native_safety import install, isolated_home
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.types import LaunchContext, NativeEndpoint, NativeResult, ServiceIdentity, SessionHandle
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write


@unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
class NativeSharedIdentityTest(PluginTestCase):
    def test_one_agent_coordinates_with_another_client_across_attachment_resume(self):
        install()
        executable = shutil.which('codex')
        version = subprocess.check_output([executable, '--version'], text=True).strip()
        if version != 'codex-cli 0.163.0':
            self.skipTest('native shared identity evidence pinned to 0.163.0')
        asyncio.run(self.probe(executable))

    async def probe(self, executable):
        from websockets.asyncio.client import unix_connect
        home = isolated_home(self.work / 'shared-home')
        socket = self.work / 'native.sock'
        agent_id = str(uuid4())
        directory = self.root / 'state' / 'agents' / agent_id
        directory.mkdir(parents=True)
        cli = str(Path(__file__).resolve().parents[1] / 'bin' / 'mv2')
        stable = {'MV2_IDENTITY_MODE': 'codex-agent-v1', 'MV2_UNIVERSE': str(self.root),
                  'MV2_AGENT_ID': agent_id, 'MV2_AGENT_DIR': str(directory),
                  'MV2_CLI': cli, 'MV2_PYTHON': sys.executable, 'PATH': os.environ['PATH']}
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('MV2_') and key not in ('CODEX_THREAD_ID', 'CODEX_SQLITE_HOME', 'TMUX', 'TMUX_PANE')}
        environment['CODEX_HOME'] = str(home)
        process = await asyncio.create_subprocess_exec(executable, 'app-server', '--listen', 'unix://' + str(socket),
            cwd=self.work, env=environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        clients = []
        identifier = 0
        native_id = None
        adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        async def rpc(client, method, params):
            nonlocal identifier
            identifier += 1
            request_id = identifier
            await client.send(json.dumps({'id': request_id, 'method': method, 'params': params}))
            while True:
                reply = json.loads(await asyncio.wait_for(client.recv(), 15))
                if reply.get('id') == request_id:
                    self.assertNotIn('error', reply, reply)
                    return reply['result']

        async def connect():
            client = await unix_connect(str(socket))
            clients.append(client)
            await rpc(client, 'initialize', {'clientInfo': {'name': 'mv2-native-test', 'version': '1'},
                'capabilities': {'experimentalApi': True}})
            await client.send(json.dumps({'method': 'initialized', 'params': {}}))
            return client

        def command(launch, resume=False):
            values = {'name': 'worker', 'agent_id': agent_id, 'agent_dir': str(directory),
                      'workspace': str(self.work / 'project'), 'bootstrap_json': json.dumps('fixture bootstrap'),
                      'task': '', 'session_id': native_id or str(uuid4()),
                      'control_socket': str(self.work / (launch + '-relay.sock'))}
            executor = Mock()
            executor.rpc.return_value = NativeResult('ok', value={'exclude': [], 'writable_roots': [],
                'daemon_version': (0, 163, 0), 'workspace_policy': {}})
            context = LaunchContext('resume' if resume else 'spawn', values,
                environment={**stable, 'MV2_LAUNCH_ID': launch,
                    'MV2_CONTROL_SOCKET': values['control_socket'], 'CODEX_HOME': str(home)},
                agent_environment=stable, endpoint=NativeEndpoint('unix-websocket', str(socket)), executor=executor)
            return (adapter.resume(SessionHandle(agent_id, 'codex', values['workspace'], native_id=native_id), context)
                    if resume else adapter.spawn(context))

        async def shell(client, label):
            # This is fixture-only native execution, never a launch/readiness probe.
            output = self.work / (label + '.json')
            script = '''import json,os,pathlib,subprocess
result={'identity':{key:value for key,value in os.environ.items() if key.startswith('MV2_')}}
for name,args in [('report',['report','--phase','working','--summary','native identity fixture']),('inbox',['inbox','--peek'])]:
 p=subprocess.run([os.environ['MV2_CLI'],*args],capture_output=True,text=True)
 result[name]={'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
result['invalid_proofs']={}
for name,key,value in [('missing-mode','MV2_IDENTITY_MODE',None),('missing-marker','CODEX_THREAD_ID',None),('wrong-marker','CODEX_THREAD_ID','00000000-0000-0000-0000-000000000000'),('wrong-dir','MV2_AGENT_DIR','/not-the-agent'),('wrong-storage','CODEX_HOME',os.environ['CODEX_HOME']+'/wrong'),('old-perlaunch','MV2_IDENTITY_MODE',None)]:
 env=dict(os.environ)
 if value is None:env.pop(key,None)
 else:env[key]=value
 if name=='old-perlaunch':env['MV2_LAUNCH_ID']='00000000-0000-0000-0000-000000000000'
 result['invalid_proofs'][name]={}
 for operation,args in [('report',['report','--phase','blocked']),('inbox',['inbox','--peek'])]:
  p=subprocess.run([os.environ['MV2_CLI'],*args],env=env,capture_output=True,text=True)
  result['invalid_proofs'][name][operation]={'exit':p.returncode,'stderr':p.stderr}
pathlib.Path(OUTPUT).write_text(json.dumps(result))
'''.replace('OUTPUT', repr(str(output)))
            await rpc(client, 'thread/shellCommand', {'threadId': native_id,
                'command': shlex.join([sys.executable, '-c', script]), 'timeoutMs': 10000})
            for _ in range(200):
                if output.exists():
                    break
                await asyncio.sleep(.05)
            result = json.loads(output.read_text())
            self.assertEqual(result['report']['exit'], 0, result)
            self.assertEqual(result['inbox']['exit'], 0, result)
            for name, operations in result['invalid_proofs'].items():
                for operation, failure in operations.items():
                    self.assertNotEqual(failure['exit'], 0, (name, operation, failure))
                    if name == 'old-perlaunch':
                        self.assertIn('with no other client attached', failure['stderr'])
            for key in ('MV2_LAUNCH_ID', 'MV2_CONTROL_SOCKET', 'MV2_TMUX_SOCKET'):
                self.assertNotIn(key, result['identity'])
            self.assertEqual(result['identity']['MV2_AGENT_ID'], agent_id)
            return result['identity']

        try:
            for _ in range(150):
                if socket.exists():
                    break
                await asyncio.sleep(.05)
            first = await connect()
            initial = command('first')
            protocol = LaunchProtocol(initial.rpc_proxy.settings)
            frame = protocol.outgoing({'id': 900, 'method': 'thread/start', 'params': {
                'cwd': str(self.work / 'project'), 'ephemeral': False, 'approvalPolicy': 'never'}})
            reply = await rpc(first, 'thread/start', frame['params'])
            native_id = reply['thread']['id']
            self.assertEqual(protocol.incoming({'id': 900, 'result': reply}), native_id)
            service = asdict(ServiceIdentity('codex', {'CODEX_HOME': str(home)}))
            write(directory / 'properties.json', {'id': agent_id, 'name': 'worker', 'harness': 'codex',
                'universe_id': read(self.root / 'universe.json')['id'],
                'workspace': str(self.work / 'project'), 'native_session_id': native_id,
                'native_service_identity': service, 'native_environment': service['scope'],
                'agent_identity': {'schema': 1, 'spec': asdict(initial.agent_identity), 'service': service,
                    'native_id': native_id, 'workspace': str(self.work / 'project')}})
            write(directory / 'runtime.json', {'launch_id': 'first', 'state': 'running', 'native_mode': {'mode': 'bridge'}})
            first_identity = await shell(first, 'initial')
            outside = await connect()
            await rpc(outside, 'thread/resume', {'threadId': native_id})
            await first.close()  # Only our old attachment; do not cancel the conversation.
            write(directory / 'runtime.json', {'launch_id': 'second', 'state': 'running', 'native_mode': {'mode': 'bridge'}})
            second = await connect()
            resumed = command('second', resume=True)
            protocol = LaunchProtocol(resumed.rpc_proxy.settings)
            frame = protocol.outgoing({'id': 901, 'method': 'thread/resume', 'params': {'threadId': native_id}})
            reply = await rpc(second, 'thread/resume', frame['params'])
            self.assertEqual(protocol.incoming({'id': 901, 'result': reply}), native_id)
            self.assertEqual(await shell(second, 'resumed'), first_identity)
            # Driving the same conversation from the user's client is the same agent.
            self.assertEqual(await shell(outside, 'outside-client'), first_identity)
            self.assertEqual(read(directory / 'properties.json')['phase'], 'working')
            self.assertEqual((await rpc(outside, 'thread/read', {'threadId': native_id, 'includeTurns': False}))['thread']['id'], native_id)
        finally:
            for client in clients:
                await client.close()
            # Native startup can spawn background registry/git helpers. Waiting
            # only for app-server leaves them racing TemporaryDirectory cleanup.
            # This session/group belongs solely to this disposable test server.
            for ending in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, ending)
                except ProcessLookupError:
                    break
                for _ in range(100):
                    await asyncio.sleep(.02)
                    groups = subprocess.check_output(['ps', '-axo', 'pgid='], text=True).split()
                    if str(process.pid) not in groups:
                        break
                else:
                    continue
                break
            else:
                self.fail('Private native test helpers did not exit')
            await asyncio.wait_for(process.wait(), 5)
