"""Claude inbox bridge: launch options, steering frames and core framing, with no Claude process."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses, native, universe
from multiverse.harnesses.claude import MOD
from multiverse.harnesses.types import LaunchContext, Message, NativeEndpoint, NativeResult, SessionHandle
from multiverse.store import read, write
from multiverse.tools import send
from test_multiverse import BaseTest

TOKEN = '0123456789abcdef0123456789abcdef'


class Inbox:
    """A local inbox like Claude's: reads JSON lines until EOF and sends no reply."""

    def __init__(self, path):
        self.lines = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        connection, _ = self.server.accept()
        data = b''
        with connection:
            while chunk := connection.recv(65536):
                data += chunk
        self.lines = [json.loads(line) for line in data.decode().splitlines()]

    def close(self):
        self.thread.join(5)
        self.server.close()


class ClaudeBridgeTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.socket = str(self.work / 'inbox.sock')
        self.config = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))

    def bridge(self, enabled=True):
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings']['inbox_bridge'] = enabled
        write(path, config)
        return harnesses.load(self.root, 'claude')

    def publish_key(self, token=TOKEN):
        digest = hashlib.sha256(os.path.abspath(self.socket).encode()).hexdigest()
        (self.config / 'sessions').mkdir(parents=True, exist_ok=True)
        (self.config / 'sessions' / f'4242.{digest}.key').write_text(json.dumps({'peerToken': token}))

    def values(self):
        return dict(name='worker', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello',
                    bootstrap_json='"Hello"', task='--literal task', agent_dir=str(self.work / 'agent'),
                    harness_dir='/local/harness', session_file='', bootstrap_file='', task_file='',
                    workspace=str(self.work), model='', control_socket=self.socket)

    def session(self, state='running', endpoint=True):
        return SessionHandle('agent', 'claude', str(self.work), native_id=str(uuid4()), state=state,
                             endpoint=NativeEndpoint('unix-lines', self.socket) if endpoint else None)

    def test_bridge_is_opt_in_and_launches_the_mod_on_the_launch_socket(self):
        plain = self.bridge(False).spawn(LaunchContext('spawn', self.values()))
        self.assertNotIn('--plugin-dir', plain.argv)
        self.assertIsNone(plain.control_endpoint)
        self.assertEqual(dict(plain.environment), {})
        adapter = self.bridge()
        values = self.values()
        for command in (adapter.spawn(LaunchContext('spawn', values)),
                        adapter.resume(SessionHandle(values['agent_id'], 'claude', str(self.work),
                                                     native_id=values['session_id']), LaunchContext('resume', values))):
            with self.subTest(argv=command.argv[:2]):
                self.assertEqual(list(command.argv[1:7]), ['--plugin-dir', str(MOD), '--messaging-socket-path',
                                 self.socket, '--settings', '{"crossSessionInbound": "accept"}'])
                self.assertEqual(command.control_endpoint, NativeEndpoint('unix-lines', self.socket))
                self.assertEqual(command.environment['CLAUDE_CODE_ENABLE_FUNCTION_HOOKS'], '1')
                self.assertIn(values['session_id'], command.argv)
        self.assertEqual(adapter.spawn(LaunchContext('spawn', values)).argv[-1], values['task'])
        self.assertTrue((MOD / '.claude-plugin/plugin.json').is_file())
        self.assertTrue((MOD / 'hooks/index.ts').is_file())

    def test_steer_writes_one_tagged_frame_for_the_exact_session(self):
        adapter = self.bridge()
        self.publish_key()
        session = self.session()
        executor = Mock()
        executor.write_lines.return_value = NativeResult('ok')
        message = Message('message-id', 'body', {}, None, 'steer', 'Message from reviewer:\nbody')
        self.assertEqual(adapter.capabilities(session)['send_steer'].state, 'supported')
        result = adapter.send(session, message, executor)
        self.assertEqual(result.state, 'native_queued')
        called_session, lines = executor.write_lines.call_args.args
        self.assertIs(called_session, session)
        auth, frame = (json.loads(line) for line in lines)
        self.assertEqual(auth, {'type': 'auth', 'token': TOKEN})
        self.assertEqual(frame, {'type': 'user', 'priority': 'next', 'session_id': session.native_id,
                                 'msg_id': 'message-id',
                                 'message': {'role': 'user', 'content': '<<MV2>>Message from reviewer:\nbody<</MV2>>'}})

    def test_follow_up_and_unready_sessions_are_unavailable_without_writing(self):
        adapter = self.bridge()
        executor = Mock()
        self.assertEqual(adapter.capabilities(self.session())['send_follow_up'].state, 'unavailable')
        follow_up = Message('id', 'body', delivery='follow-up', framed='Message from user:\nbody')
        self.assertEqual(adapter.send(self.session(), follow_up, executor).state, 'unavailable')
        steer = Message('id', 'body', framed='Message from user:\nbody')
        for session in (self.session(state='stopped'), self.session(endpoint=False)):
            self.assertEqual(adapter.capabilities(session)['send_steer'].state, 'unavailable')
            self.assertEqual(adapter.send(session, steer, executor).state, 'unavailable')
        # Claude has not bound the inbox and published its key yet.
        self.assertEqual(adapter.send(self.session(), steer, executor).state, 'unavailable')
        self.assertEqual(self.bridge(False).capabilities(self.session())['send_steer'].state, 'unavailable')
        executor.write_lines.assert_not_called()

    def test_send_frames_the_sender_in_core_and_reaches_the_inbox(self):
        self.bridge()
        self.publish_key()
        recipient, sender, native_id = str(uuid4()), str(uuid4()), str(uuid4())
        directory = self.root / 'state/agents' / recipient
        write(directory / 'properties.json', {'id': recipient, 'name': 'worker', 'harness': 'claude',
                                              'workspace': str(self.work), 'native_session_id': native_id})
        write(directory / 'runtime.json', {'launch_id': 'current', 'state': 'running',
                                           'control_endpoint': {'transport': 'unix-lines', 'address': self.socket}})
        write(self.root / 'state/agents' / sender / 'properties.json', {'id': sender, 'name': 'reviewer'})
        inbox = Inbox(self.socket)
        with patch.object(native, 'owned'):
            result = send.run(self.root, agent=recipient, text='Please rebase',
                              sender={'agent_id': sender, 'universe': str(self.root), 'name': 'agent'})
        inbox.close()
        self.assertEqual(result['state'], 'native_queued')
        self.assertTrue(result['ok'])
        auth, frame = inbox.lines
        self.assertEqual(auth['token'], TOKEN)
        self.assertEqual(frame['session_id'], native_id)
        self.assertEqual(frame['msg_id'], result['id'])
        self.assertEqual(frame['message']['content'], '<<MV2>>Message from reviewer:\nPlease rebase<</MV2>>')

    def test_executor_writes_only_to_a_line_inbox(self):
        executor = native.NativeExecutor(self.work, {}, {})
        with patch.object(executor, 'check_session'):
            session = SessionHandle('agent', 'claude', str(self.work), endpoint=NativeEndpoint('unix-json', self.socket))
            self.assertEqual(executor.write_lines(session, ['{}']).state, 'unavailable')
            self.assertEqual(executor.write_lines(self.session(), ['{}']).state, 'error')  # nothing listening

    def test_core_names_the_sender_and_a_different_universe(self):
        other = universe.create(self.work / 'other' / '.multiverse', 'pr-review', workdir=self.work)
        lead, local = str(uuid4()), str(uuid4())
        write(other / 'state/agents' / lead / 'properties.json', {'id': lead, 'name': 'lead'})
        write(self.root / 'state/agents' / local / 'properties.json', {'id': local, 'name': 'reviewer'})
        cases = [({'agent_id': lead, 'universe': str(other), 'name': 'agent'}, 'Message from lead (in universe pr-review):\nhi'),
                 ({'agent_id': local, 'universe': str(self.root), 'name': 'agent'}, 'Message from reviewer:\nhi'),
                 ({'name': 'user', 'agent_id': None}, 'Message from user:\nhi'),
                 ({}, 'Message from user:\nhi')]
        for sender, expected in cases:
            with self.subTest(sender=sender):
                self.assertEqual(native.framed_text(self.root, {'from': sender, 'text': 'hi'}), expected)

    @unittest.skipUnless(shutil.which('claude'), 'Claude Code is not installed')
    def test_packaged_mod_passes_claude_validation(self):
        result = subprocess.run(['claude', 'plugin', 'validate', str(MOD)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
