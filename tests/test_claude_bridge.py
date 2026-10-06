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
from multiverse.harnesses.claude import MOD, measured_usage
from multiverse.harnesses.types import LaunchContext, Message, NativeEndpoint, NativeResult, SessionHandle
from multiverse.store import read, write
from multiverse.tools import runtime_update, send
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
        from multiverse.harnesses import claude as claude_module
        self.real_claude_version = claude_module.claude_version
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 289)))

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

    def session(self, state='running', endpoint=True, bridge='claude-mod-v1'):
        return SessionHandle('agent', 'claude', str(self.work), native_id=str(uuid4()), state=state,
                             endpoint=NativeEndpoint('unix-lines', self.socket) if endpoint else None, bridge=bridge)

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

    def launch(self, setting, version, mode='spawn'):
        """A Claude launch under this `inbox_bridge` setting (None leaves it out) and installed version."""
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings'].pop('inbox_bridge', None)
        if setting is not None:
            config['settings']['inbox_bridge'] = setting
        write(path, config)
        values = self.values()
        adapter = harnesses.load(self.root, 'claude')
        with patch('multiverse.harnesses.claude.claude_version', return_value=version) as probe:
            if mode == 'spawn':
                command = adapter.spawn(LaunchContext('spawn', values))
            else:
                session = SessionHandle(values['agent_id'], 'claude', str(self.work), native_id=values['session_id'])
                command = adapter.resume(session, LaunchContext('resume', values))
        return command, probe

    def test_unset_is_auto_and_uses_the_bridge_unless_this_claude_cannot_run_it(self):
        command, _ = self.launch(None, (2, 1, 289))
        self.assertEqual(list(command.argv[1:3]), ['--plugin-dir', str(MOD)])
        self.assertEqual(command.control_endpoint, NativeEndpoint('unix-lines', self.socket))
        self.assertEqual(command.native_mode, {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None})
        for mode in ('spawn', 'resume'):
            with self.subTest(mode=mode):
                old, _ = self.launch(None, (2, 1, 270), mode)
                self.assertNotIn('--plugin-dir', old.argv)
                self.assertIsNone(old.control_endpoint)
                self.assertNotIn('CLAUDE_CODE_ENABLE_FUNCTION_HOOKS', old.environment)
                self.assertEqual((old.native_mode['policy'], old.native_mode['mode']), ('auto', 'plain'))
                self.assertIn('Claude Code 2.1.270 is older than 2.1.280', old.native_mode['fallback_reason'])
        # A version that cannot be read is no reason to give up the bridge: the launch shows whether the mod loads.
        unknown, _ = self.launch(None, None)
        self.assertEqual(unknown.native_mode['mode'], 'bridge')

    def test_true_requires_the_bridge_and_false_never_loads_it(self):
        required, _ = self.launch(True, (2, 1, 289))
        self.assertEqual(required.native_mode, {'policy': 'required', 'mode': 'bridge', 'fallback_reason': None})
        with self.assertRaisesRegex(ValueError, 'older than 2.1.280'):
            self.launch(True, (2, 1, 270))
        for version in ((2, 1, 289), (2, 1, 270)):
            plain, probe = self.launch(False, version)
            self.assertNotIn('--plugin-dir', plain.argv)
            self.assertIsNone(plain.control_endpoint)
            self.assertEqual(plain.native_mode, {'policy': 'plain', 'mode': 'plain', 'fallback_reason': None})
            probe.assert_not_called()  # plain never asks which version is installed
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings']['inbox_bridge'] = 'yes'  # still strictly validated
        write(path, config)
        with self.assertRaises(ValueError):
            harnesses.load(self.root, 'claude')

    def test_a_launch_that_fell_back_to_plain_says_so_in_its_capabilities(self):
        adapter = self.bridge(None)
        running = dict(state='running', native_id=str(uuid4()), bridge='claude-mod-v1',
                       endpoint=NativeEndpoint('unix-lines', self.socket), session_file=str(self.work / 'agent' / 'session.jsonl'))
        session = SessionHandle('agent', 'claude', str(self.work), native_mode={'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None}, **running)
        capabilities = adapter.capabilities(session)
        self.assertEqual((capabilities['send_steer'].state, capabilities['get_delivery'].state, capabilities['ask'].state),
                         ('supported', 'supported', 'supported'))
        reason = 'Claude Code 2.1.270 is older than 2.1.280, which the multiverse mod needs'
        fell_back = SessionHandle('agent', 'claude', str(self.work),
                                  native_mode={'policy': 'auto', 'mode': 'plain', 'fallback_reason': reason},
                                  **{**running, 'endpoint': None, 'bridge': None})
        capabilities = adapter.capabilities(fell_back)
        for name in ('send_steer', 'send_follow_up'):
            self.assertEqual(capabilities[name].state, 'unavailable')
            self.assertEqual(capabilities[name].reason, 'Launched without the bridge: ' + reason)
        self.assertNotEqual(capabilities['get_delivery'].state, 'supported')
        self.assertNotEqual(capabilities['ask'].state, 'supported')

    def test_what_reads_the_setting_sees_the_effective_value(self):
        # auto and true both mean the bridge is on for code that asks adapter.settings (compaction does).
        for setting, expected in ((None, True), (True, True), (False, False)):
            self.launch(setting, (2, 1, 289))
            self.assertEqual(harnesses.load(self.root, 'claude').settings['inbox_bridge'], expected, setting)

    def test_the_installed_version_is_read_once_per_binary_and_is_never_fatal(self):
        from multiverse.harnesses import claude as module
        fake = self.work / 'bin' / 'claude-fake'
        fake.parent.mkdir()
        fake.write_text('#!/bin/sh\necho "2.1.289 (Claude Code)"\n')
        fake.chmod(0o755)
        with patch('multiverse.harnesses.claude.claude_version', self.real_claude_version):
            module._versions.clear()
            self.assertEqual(module.claude_version(str(fake)), (2, 1, 289))
            fake.write_text('#!/bin/sh\necho "something else"\n')
            self.assertEqual(module.claude_version(str(fake)), (2, 1, 289))  # cached by the binary's real path
            module._versions.clear()
            self.assertIsNone(module.claude_version(str(fake)))               # no version in the output
            self.assertIsNone(module.claude_version(str(self.work / 'nothing-here')))  # not installed
            self.assertIsNone(module.bridge_problem(str(self.work / 'nothing-here')))
            module._versions.clear()

    def test_steer_writes_one_tagged_frame_for_the_exact_session(self):
        adapter = self.bridge()
        self.publish_key()
        session = self.session()
        executor = Mock()
        executor.write_lines.return_value = NativeResult('ok')
        message_id = str(uuid4())
        message = Message(message_id, 'body', delivery='steer', framed='Message from reviewer:\nbody')
        self.assertEqual(adapter.capabilities(session)['send_steer'].state, 'supported')
        result = adapter.send(session, message, executor)
        self.assertEqual(result.state, 'native_queued')
        called_session, lines = executor.write_lines.call_args.args
        self.assertIs(called_session, session)
        auth, frame = (json.loads(line) for line in lines)
        self.assertEqual(auth, {'type': 'auth', 'token': TOKEN})
        self.assertEqual(frame, {'type': 'user', 'priority': 'next', 'session_id': session.native_id,
                                 'msg_id': message_id,
                                 'message': {'role': 'user', 'content': f'<<MV2 steer id={message_id}>>'
                                                                        'Message from reviewer:\nbody<</MV2>>'}})
        # Follow-up travels at `next` too; the tag tells the mod to wait for the turn to end.
        follow_up = Message(message_id, 'body', delivery='follow-up', framed='Message from reviewer:\nbody')
        self.assertEqual(adapter.capabilities(session)['send_follow_up'].state, 'supported')
        self.assertEqual(adapter.send(session, follow_up, executor).state, 'native_queued')
        frame = json.loads(executor.write_lines.call_args.args[1][1])
        self.assertEqual(frame['priority'], 'next')
        self.assertTrue(frame['message']['content'].startswith(f'<<MV2 follow-up id={message_id}>>'))

    def test_unready_sessions_are_unavailable_without_writing(self):
        adapter = self.bridge()
        executor = Mock()
        steer = Message(str(uuid4()), 'body', framed='Message from user:\nbody')
        # A stopped launch, one without an inbox, and one whose mod never registered.
        for session in (self.session(state='stopped'), self.session(endpoint=False), self.session(bridge=None)):
            for name in ('send_steer', 'send_follow_up'):
                self.assertEqual(adapter.capabilities(session)[name].state, 'unavailable')
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
                                           'native_bridge': 'claude-mod-v1',
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
        self.assertNotIn('id', result)
        message_id = frame['msg_id']
        self.assertNotIn(message_id, json.dumps(result))
        self.assertEqual(frame['message']['content'],
                         f'<<MV2 steer id={message_id}>>Message from reviewer:\nPlease rebase<</MV2>>')

    def test_mod_registers_its_bridge_and_reports_waiting_for_the_current_launch(self):
        agent = str(uuid4())
        directory = self.root / 'state/agents' / agent
        write(directory / 'properties.json', {'id': agent, 'name': 'worker', 'harness': 'claude'})
        write(directory / 'runtime.json', {'launch_id': 'current', 'state': 'running'})
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}):
            runtime_update.run(self.root, agent=agent, status='idle', bridge='claude-mod-v1')
            runtime_update.run(self.root, agent=agent, status='waiting')
        facts = read(directory / 'runtime.json')
        self.assertEqual((facts['native_bridge'], facts['status']), ('claude-mod-v1', 'waiting'))
        with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'previous'}), self.assertRaisesRegex(ValueError, 'Outdated launch'):
            runtime_update.run(self.root, agent=agent, status='working')
        self.assertEqual(read(directory / 'runtime.json')['status'], 'waiting')

    def response(self, id, input_tokens, output_tokens, cache_read=0, cache_write=0, block='text'):
        usage = {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                 'cache_read_input_tokens': cache_read, 'cache_creation_input_tokens': cache_write}
        return json.dumps({'type': 'assistant', 'timestamp': f'2026-09-30T00:00:0{len(id)}Z',
                           'message': {'id': id, 'usage': usage, 'content': [{'type': block}]}}) + '\n'

    def test_usage_is_measured_once_per_response_across_the_session_and_its_subagents(self):
        adapter = self.bridge(False)
        native_id = str(uuid4())
        project = self.config / 'projects' / '-work-project'
        project.mkdir(parents=True)
        main = project / f'{native_id}.jsonl'
        # One response is recorded once per content block, each with the same usage.
        main.write_text(json.dumps({'type': 'user', 'message': {'content': 'hi'}}) + '\n'
                        + self.response('msg_1', 10, 5, cache_read=100, cache_write=20, block='thinking')
                        + self.response('msg_1', 10, 5, cache_read=100, cache_write=20) + 'not json\n')
        session = SessionHandle('agent', 'claude', str(self.work), native_id=native_id)
        result = adapter.get_usage(session)
        self.assertEqual(result.state, 'ok')
        self.assertEqual({key: result.value[key] for key in ('input_tokens', 'cached_input_tokens', 'cache_write_tokens',
                                                             'output_tokens', 'total_tokens')},
                         {'input_tokens': 130, 'cached_input_tokens': 100, 'cache_write_tokens': 20,
                          'output_tokens': 5, 'total_tokens': 135})
        self.assertEqual(adapter.capabilities(session)['get_usage'].state, 'supported')
        # Growth is read incrementally; an unfinished line waits for its newline.
        partial = self.response('msg_2', 1, 2)
        with main.open('a') as handle:
            handle.write(partial[:20])
        self.assertEqual(measured_usage(native_id).total_tokens, 135)
        with main.open('a') as handle:
            handle.write(partial[20:])
        subagents = project / native_id / 'subagents'
        subagents.mkdir(parents=True)
        (subagents / 'agent-a.jsonl').write_text(self.response('msg_3', 40, 60))
        self.assertEqual(measured_usage(native_id).total_tokens, 135 + 3 + 100)
        self.assertEqual(adapter.get_usage(SessionHandle('agent', 'claude', str(self.work), native_id=str(uuid4()))).state,
                         'unavailable')
        self.assertEqual(adapter.get_usage(SessionHandle('agent', 'claude', str(self.work))).state, 'unavailable')

    def test_delivery_is_confirmed_only_by_the_mods_receipt(self):
        adapter = self.bridge()
        agent_dir = self.work / 'agent'
        session = SessionHandle('agent', 'claude', str(self.work), native_id=str(uuid4()),
                                session_file=str(agent_dir / 'session.jsonl'))
        self.assertEqual(adapter.capabilities(session)['get_delivery'].state, 'supported')
        message_id = str(uuid4())
        self.assertEqual(adapter.get_delivery(session, message_id).state, 'unknown')
        (agent_dir / 'claude-receipts').mkdir(parents=True)
        (agent_dir / 'claude-receipts' / f'{message_id}.json').write_text(
            json.dumps({'id': message_id, 'route': 'mid-turn', 'at': 1790000000000}))
        result = adapter.get_delivery(session, message_id)
        self.assertEqual((result.state, result.value['transcript_confirmed'], result.value['route']),
                         ('native_queued', True, 'mid-turn'))
        self.assertEqual(adapter.get_delivery(session, '../../escape').state, 'error')

    def answer_when_asked(self, agent_dir, record):
        """Play the mod: answer the question the adapter wrote, as a file."""
        executor = Mock()

        def write_lines(session, lines):
            question_id = json.loads(lines[1])['msg_id']
            (agent_dir / 'claude-asks').mkdir(parents=True, exist_ok=True)
            if record is not None:
                (agent_dir / 'claude-asks' / f'{question_id}.json').write_text(json.dumps({'id': question_id, **record}))
            return NativeResult('ok')
        executor.write_lines.side_effect = write_lines
        return executor

    def test_ask_answers_from_the_mods_fork_without_touching_the_conversation(self):
        adapter = self.bridge()
        self.publish_key()
        agent_dir = self.work / 'agent'
        session = SessionHandle('agent', 'claude', str(self.work), native_id=str(uuid4()), state='running',
                                endpoint=NativeEndpoint('unix-lines', self.socket), bridge='claude-mod-v1',
                                session_file=str(agent_dir / 'session.jsonl'))
        self.assertEqual(adapter.capabilities(session)['ask'].state, 'supported')
        executor = self.answer_when_asked(agent_dir, {'answered': True, 'text': 'About an hour.',
                                                      'usage': {'input_tokens': 3, 'output_tokens': 4}})
        result = adapter.ask(session, 'How long will this take?', executor)
        self.assertEqual((result.state, result.value['answer']), ('ok', 'About an hour.'))
        frame = json.loads(executor.write_lines.call_args.args[1][1])
        self.assertEqual(frame['priority'], 'next')
        self.assertEqual(frame['message']['content'], f'<<MV2 ask id={frame["msg_id"]}>>How long will this take?<</MV2>>')
        self.assertEqual(list((agent_dir / 'claude-asks').iterdir()), [])  # the answer is consumed
        nothing = adapter.ask(session, 'Anything yet?', self.answer_when_asked(agent_dir, {'answered': False, 'reason': 'nothing-to-fork'}))
        self.assertEqual(nothing.state, 'unavailable')
        failed = adapter.ask(session, 'Anything yet?', self.answer_when_asked(agent_dir, {'answered': False, 'reason': 'api-error'}))
        self.assertEqual(failed.state, 'error')
        with patch('multiverse.harnesses.claude.ASK_TIMEOUT', 0.3):
            self.assertEqual(adapter.ask(session, 'Still there?', self.answer_when_asked(agent_dir, None)).state, 'unknown')
        self.assertEqual(adapter.ask(self.session(bridge=None), 'Hello?', Mock()).state, 'unavailable')

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

    @unittest.skipUnless(shutil.which('claude'), 'Claude Code is not installed')
    def test_packaged_mod_passes_its_plugin_tests(self):
        result = subprocess.run(['claude', 'plugin', 'test', str(MOD)], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
