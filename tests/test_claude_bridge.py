"""Claude inbox bridge: launch options, steering frames and receipts, with no Claude session."""
from dataclasses import replace
import hashlib
import json
import re
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses
from multiverse.harnesses.claude import BRIDGE, MOD
from multiverse.plugins import loader as plugin_loader, manifest as plugin_manifest
from multiverse.harnesses.types import LaunchContext, Message, NativeEndpoint, NativeResult, SessionHandle
from multiverse.store import read, write
from multiverse.tools import capabilities
from test_multiverse import BaseTest

# The typeahead is the bundled `typeahead` plugin; its manifest gates it on the Claude version.
TYPEAHEAD = plugin_loader.BUNDLED / 'typeahead' / 'claude'
TYPEAHEAD_VERSION = plugin_manifest.load(TYPEAHEAD.parent).harnesses['claude'][0].min_version

TOKEN = '0123456789abcdef0123456789abcdef'


def mod_tag():
    """The mod's own tag regex (hooks/index.ts TAG): Python's frames must be what it reads, since each side keeps its copy."""
    found = re.search(r'^const TAG = /(.+)/$', (MOD / 'hooks/index.ts').read_text(), re.M)
    return re.compile(found.group(1).replace('\\/', '/'))


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

    def configure(self, setting):
        """Set the harness's `inbox_bridge` setting; None leaves it out."""
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings'].pop('inbox_bridge', None)
        if setting is not None:
            config['settings']['inbox_bridge'] = setting
        write(path, config)

    def launch(self, setting, version, mode='spawn'):
        """A Claude launch under this `inbox_bridge` setting (None leaves it out) and installed version."""
        self.configure(setting)
        values = self.values()
        adapter = harnesses.load(self.root, 'claude')
        with patch('multiverse.harnesses.claude.claude_version', return_value=version) as probe:
            if mode == 'spawn':
                command = adapter.spawn(LaunchContext('spawn', values))
            else:
                session = SessionHandle(values['agent_id'], 'claude', str(self.work), native_id=values['session_id'])
                command = adapter.resume(session, LaunchContext('resume', values))
        return command, probe

    def harness(self, setting, version=(2, 1, 302), probe=False):
        """What capabilities --harness says with no agent, under this setting and installed version.

        --probe looks the executable up as spawn does, so the harness points at a stand-in that never runs (the
        version read is patched): the answer must not depend on a real claude on PATH, which CI doesn't have.
        """
        self.configure(setting)
        stand_in = self.work / 'stand-in' / 'claude'
        if not stand_in.exists():
            stand_in.parent.mkdir(parents=True)
            stand_in.write_text('#!/bin/sh\nexit 1\n')
            stand_in.chmod(0o755)
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings']['executable'] = str(stand_in)
        write(path, config)
        with patch('multiverse.harnesses.claude.claude_version', return_value=version) as asked:
            found = capabilities.run(self.root, harness='claude', probe=probe)
        return found, asked

    def test_launch_mode_and_hidden_flags_per_setting_and_version(self):
        """Unset, true and false inbox_bridge settings against new, old and unreadable Claude versions pick bridge or
        plain with the exact hidden flags (--plugin-dir, --messaging-socket-path, crossSessionInbound), and
        capabilities --harness says the same of bridge and usage with no launch; fragile because it hinges on a version
        gate and undocumented Claude flags, and a wrong pick quietly makes every agent launch-only or has agents plan
        around abilities their launches lack (CLAUDE-2, CLAUDE-9, 7c6412f)."""
        with self.subTest('the bridge launches the mod on the launch socket'):
            plain = self.bridge(False).spawn(LaunchContext('spawn', self.values()))
            self.assertNotIn('--plugin-dir', plain.argv)
            self.assertIsNone(plain.control_endpoint)
            self.assertEqual(dict(plain.environment), {})
            adapter = self.bridge()
            values = self.values()
            saved = SessionHandle(values['agent_id'], 'claude', str(self.work), native_id=values['session_id'])
            for command in (adapter.spawn(LaunchContext('spawn', values)),
                            adapter.resume(saved, LaunchContext('resume', values))):
                with self.subTest(argv=command.argv[:2]):
                    self.assertEqual(list(command.argv[1:7]), ['--plugin-dir', str(MOD), '--messaging-socket-path',
                                     self.socket, '--settings', '{"crossSessionInbound": "accept"}'])
                    self.assertEqual(command.control_endpoint, NativeEndpoint('unix-lines', self.socket))
                    self.assertEqual(command.environment['CLAUDE_CODE_ENABLE_FUNCTION_HOOKS'], '1')
                    self.assertIn(values['session_id'], command.argv)
            self.assertEqual(adapter.spawn(LaunchContext('spawn', values)).argv[-1], values['task'])
            self.assertTrue((MOD / '.claude-plugin/plugin.json').is_file())
            self.assertTrue((MOD / 'hooks/index.ts').is_file())
        with self.subTest('unset is auto'):
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
        with self.subTest('true requires the bridge and false never loads it'):
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
        with self.subTest('a launch that fell back to plain says so'):
            adapter = self.bridge(None)
            running = dict(state='running', native_id=str(uuid4()), bridge='claude-mod-v1',
                           endpoint=NativeEndpoint('unix-lines', self.socket), session_file=str(self.work / 'agent' / 'session.jsonl'))
            session = SessionHandle('agent', 'claude', str(self.work), native_mode={'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None}, **running)
            offered = adapter.capabilities(session)
            self.assertEqual((offered['send_steer'].state, offered['get_delivery'].state, offered['ask'].state),
                             ('supported', 'supported', 'supported'))
            reason = 'Claude Code 2.1.270 is older than 2.1.280, which the multiverse mod needs'
            fell_back = SessionHandle('agent', 'claude', str(self.work),
                                      native_mode={'policy': 'auto', 'mode': 'plain', 'fallback_reason': reason},
                                      **{**running, 'endpoint': None, 'bridge': None})
            # Launch-only by the harness's own setting is final: nothing a launch does connects it, so core advises no
            # restart (CLAUDE-1) and ledger repair may close its messages (CLAUDE-13). An auto launch that fell back
            # only because this Claude was too old connects once Claude is updated and the agent resumed, so its
            # refusals stay retryable and carry no launch-only marker (Shao, a8925af).
            off = SessionHandle('agent', 'claude', str(self.work),
                                native_mode={'policy': 'plain', 'mode': 'plain', 'fallback_reason': None},
                                **{**running, 'endpoint': None, 'bridge': None})
            for case, harness, launch, cause, final in (
                    ('fell back', adapter, fell_back, reason, False),
                    ('set to false', self.bridge(False), off, 'its harness sets inbox_bridge to false', True)):
                with self.subTest(case):
                    offered = harness.capabilities(launch)
                    for name in ('send_steer', 'send_follow_up'):
                        self.assertEqual((offered[name].state, offered[name].reason, offered[name].mechanism),
                                         ('unavailable', 'This agent is launch-only: ' + cause,
                                          'launch-only' if final else None))
                    self.assertNotEqual(offered['get_delivery'].state, 'supported')
                    self.assertNotEqual(offered['ask'].state, 'supported')
                    executor = Mock()
                    for delivery in ('steer', 'follow-up'):
                        message = Message(str(uuid4()), 'body', delivery=delivery, framed='Message from reviewer:\nbody')
                        result = harness.send(launch, message, executor)
                        self.assertEqual((result.state, result.value), ('unavailable', {'launch_only': True, 'final': final}))
                    result = harness.ask(launch, 'Why?', executor)
                    self.assertEqual((result.state, result.value), ('unavailable', {'launch_only': True, 'final': final}))
                    executor.write_lines.assert_not_called()
            # Launched while the setting was off, with it on now: a resume connects it, so it is a restart case.
            for state, cause in (('running', 'The multiverse mod has not registered for this launch'),
                                 ('stopped', "This agent isn't running: resume it first")):
                with self.subTest('off then, on now', state=state):
                    then = replace(off, state=state)
                    offered = adapter.capabilities(then)
                    self.assertEqual((offered['send_steer'].reason, offered['send_steer'].mechanism), (cause, None))
                    result = adapter.send(then, Message(str(uuid4()), 'body', delivery='steer', framed='x'), Mock())
                    self.assertEqual((result.state, result.reason, result.value), ('unavailable', cause, None))
        with self.subTest('the installed version is read once per binary and is never fatal'):
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
        with self.subTest('capabilities --harness with no launch: messaging (CLAUDE-2)'):
            plain, _ = self.harness(False)
            auto, asked = self.harness(None)
            self.assertNotEqual(plain['send_steer'], auto['send_steer'])  # qa-claude's CLAUDE-2 check
            for name in ('send_steer', 'send_follow_up', 'ask'):
                self.assertEqual(plain[name]['state'], 'unavailable')
                self.assertIn('inbox_bridge', plain[name]['reason'])
                self.assertEqual(auto[name]['state'], 'supported')
                self.assertNotIn('registered', auto[name]['reason'] + plain[name]['reason'])  # there is no launch
                self.assertNotIn('No native implementation', auto[name]['reason'] + plain[name]['reason'])
            self.assertEqual(auto['ask']['mechanism'], 'claude-model-fork')
            self.assertIn('--probe', auto['send_steer']['reason'])
            asked.assert_not_called()  # reading the version runs claude: only --probe does that
            # --probe reads the installed version and says what a launch would do with it.
            new, _ = self.harness(None, probe=True)
            self.assertEqual(new['send_steer']['state'], 'supported')
            unreadable, _ = self.harness(None, version=None, probe=True)
            self.assertEqual(unreadable['send_steer']['state'], 'supported')  # as launches treat it
            old, _ = self.harness(None, version=(2, 1, 279), probe=True)
            refused, _ = self.harness(True, version=(2, 1, 279), probe=True)
            for found, how in ((old, 'launch without the bridge'), (refused, 'refuse to start')):
                for name in ('send_steer', 'send_follow_up', 'ask'):
                    self.assertEqual(found[name]['state'], 'unavailable')
                    self.assertIn(how, found[name]['reason'])
                    self.assertIn('2.1.279 is older than 2.1.280', found[name]['reason'])
        with self.subTest("capabilities --harness with no launch: usage from Claude's transcript (CLAUDE-9)"):
            for setting in (None, True, False):
                for probe in (False, True):
                    with self.subTest(setting=setting, probe=probe):
                        found, _ = self.harness(setting, probe=probe)
                        self.assertEqual((found['get_usage']['state'], found['get_usage']['mechanism']),
                                         ('supported', 'claude-transcript'))
            old, _ = self.harness(None, version=(2, 1, 279), probe=True)  # plain, but its transcript still counts
            self.assertEqual(old['get_usage']['state'], 'supported')

    def test_steer_writes_one_tagged_frame_for_the_exact_session(self):
        """A steer writes one auth line and one user frame with priority next, the exact session_id, msg_id and tag; a
        break goes unnoticed because Claude's socket accepts any line and drops a wrong one later."""
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
        # The send time travels in the tag the mod reads it from (its age is added once the model gets the message);
        # a value that isn't a plain timestamp is left out, so it can never end the tag.
        # Only a mod that registered claude-mod-v2 reads a send time; an older one, until it reloads, would miss the
        # whole tag and show the model raw tags with no receipt, so it keeps getting the old tag.
        # And the mod in this tree registers exactly the bridge the adapter sends send times to: if they drift, ages
        # silently stop (the mod keeps the old ID) or tags break (the adapter trusts an ID the mod doesn't mean).
        registered = re.findall(r'"--bridge", "([a-z0-9-]+)"', (MOD / 'hooks/index.ts').read_text())
        self.assertEqual(registered, [BRIDGE])
        current = replace(session, bridge='claude-mod-v2')
        for sent_at, target, expected in (('2026-10-23T11:02:03.456789+00:00', current, ' sent=2026-10-23T11:02:03.456789+00:00'),
                                          ('2026-10-23T11:02:03.456789+00:00', session, ''),
                                          (None, current, ''), ('11:00>><</MV2>>injected', current, '')):
            with self.subTest(sent_at=sent_at, bridge=target.bridge):
                timed = Message(message_id, 'body', delivery='steer', framed='Message from reviewer:\nbody', sent_at=sent_at)
                self.assertEqual(adapter.send(target, timed, executor).state, 'native_queued')
                content = json.loads(executor.write_lines.call_args.args[1][1])['message']['content']
                self.assertEqual(content, f'<<MV2 steer id={message_id}{expected}>>Message from reviewer:\nbody<</MV2>>')
                found = mod_tag().search(content)
                self.assertEqual(found.groups()[2], expected.removeprefix(' sent=') or None)
        self.assertTrue(frame['message']['content'].startswith(f'<<MV2 follow-up id={message_id}>>'))
        # Both read as the mod's regex reads them: the mode, this message's id, and mv2's text.
        for delivery, content in (('steer', json.loads(executor.write_lines.call_args_list[0].args[1][1])['message']['content']),
                                  ('follow-up', frame['message']['content'])):
            with self.subTest(tag=delivery):
                self.assertEqual(mod_tag().search(content).groups(), (delivery, message_id, None, 'Message from reviewer:\nbody'))

    def test_delivery_is_confirmed_only_by_the_mods_receipt(self):
        """Delivery is confirmed only by the mod's receipt file, an id like ../../escape cannot read outside it, and a
        side question's answer is read from the file the mod writes; fragile because TS writes and Python reads these
        files, and a false confirmation makes --retry skip a lost message, while a wrong answer path shows only as
        every ask timing out after 120 s."""
        adapter = self.bridge()
        agent_dir = self.work / 'agent'
        with self.subTest('a delivery receipt'):
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
        with self.subTest('a not-delivered receipt'):
            # Only Claude's definite drop, or a row never stored, fails the message so --retry may send it again;
            # an unsure or damaged one must never become permission to resend, and a receipt counts only for the ID
            # it names in the shape the mod writes ({} or another attempt's would confirm, or fail, this one).
            folder = agent_dir / 'claude-receipts'
            refused = {'id': message_id, 'delivered': False, 'reason': 'the session was stopped', 'at': 1}
            for delivered, refusal, expected in (
                    (None, refused | {'definite': True}, ('error', False, True)),
                    (None, refused, ('unknown', None, None)),
                    (None, '{"id": ', ('unknown', None, None)),
                    (None, refused | {'definite': True, 'id': str(uuid4())}, ('unknown', None, None)),
                    ({}, None, ('unknown', None, None)),
                    ({'id': str(uuid4()), 'route': 'prompt', 'at': 1}, None, ('unknown', None, None)),
                    ({'id': message_id, 'at': 1}, None, ('unknown', None, None)),
                    ({'id': message_id, 'delivered': 'false', 'route': 'prompt', 'at': 1}, None, ('unknown', None, None)),
                    ({'id': message_id.upper(), 'route': 'peer-prompt', 'at': 1}, None, ('native_queued', True, None)),
                    # The refusal's write can finish after the row was stored: the delivery wins.
                    ({'id': message_id, 'route': 'prompt', 'at': 2}, refused | {'definite': True}, ('native_queued', True, None))):
                with self.subTest(delivered=delivered, refusal=refusal):
                    for name, record in ((f'{message_id}.json', delivered), (f'{message_id}.not-delivered.json', refusal)):
                        (folder / name).unlink(missing_ok=True)
                        if record is not None:
                            (folder / name).write_text(record if isinstance(record, str) else json.dumps(record))
                    result = adapter.get_delivery(session, message_id)
                    value = result.value or {}
                    self.assertEqual((result.state, value.get('transcript_confirmed'), value.get('definite_failure')), expected)
        with self.subTest("a side question's answer"):
            # The mod's answer() writes claude-asks/<question id>.json beside the agent's session file.
            source = (MOD / 'hooks/index.ts').read_text()
            for written in ('`${directory}/claude-asks/${message.id}.json`', 'answered: true, text: result.text',
                            'answered: false, reason: result.reason'):
                self.assertTrue(written in source, f'the mod no longer writes {written}')
            self.publish_key()
            session = SessionHandle('agent', 'claude', str(self.work), native_id=str(uuid4()), state='running',
                                    endpoint=NativeEndpoint('unix-lines', self.socket), bridge='claude-mod-v1',
                                    session_file=str(agent_dir / 'session.jsonl'))

            frames = []

            def answered(target, lines):
                frames.append(json.loads(lines[1]))
                question = frames[-1]['msg_id']
                (agent_dir / 'claude-asks').mkdir(parents=True, exist_ok=True)
                (agent_dir / 'claude-asks' / f'{question}.json').write_text(json.dumps(
                    {'id': question, 'answered': True, 'text': 'About an hour.', 'usage': {'input_tokens': 3}}) + '\n')
                return NativeResult('ok')
            executor = Mock()
            executor.write_lines.side_effect = answered
            with patch('multiverse.harnesses.claude.ASK_TIMEOUT', 2):
                result = adapter.ask(session, 'How long will this take?', executor)
            self.assertEqual(result.state, 'ok', result.reason)
            self.assertEqual(result.value['answer'], 'About an hour.')
            self.assertEqual(list((agent_dir / 'claude-asks').iterdir()), [])  # the answer is consumed
            # The question goes out with the tag the mod's own regex reads as a side question (Shao, a8925af).
            found = mod_tag().search(frames[0]['message']['content'])
            self.assertEqual(found.groups() if found else None, ('ask', frames[0]['msg_id'], None, 'How long will this take?'))

    def test_resume_renders_the_system_prompt_fresh(self):
        """Resume adds --system-prompt-snapshot off right after the bootstrap, only for Claude 2.1.267 and later, unless
        the harness setting or the user's own flag says otherwise; a break goes unnoticed because a resumed agent
        silently keeps its old bootstrap (96ea3d3, efbbb2b)."""
        with self.subTest('off on every resume of a Claude that knows the flag'):
            # With Claude's snapshot on, a resumed conversation keeps the system prompt of its first request.
            for setting in (None, True, False):
                for version in ((2, 1, 300), (2, 1, 280), None):
                    with self.subTest(setting=setting, version=version):
                        if setting is True and version is not None and version < (2, 1, 280):
                            continue
                        resumed, probe = self.launch(setting, version, 'resume')
                        argv = list(resumed.argv)
                        flag = argv.index('--system-prompt-snapshot')
                        self.assertEqual(argv[flag + 1], 'off')
                        self.assertEqual(argv[flag - 2], '--append-system-prompt')  # right after the bootstrap
                        spawned, _ = self.launch(setting, version)
                        # A new conversation records it fresh.
                        self.assertNotIn('--system-prompt-snapshot', spawned.argv)
                        if setting is False:
                            probe.assert_not_called()  # plain still never asks which version is installed
            # A Claude known to predate the flag (an auto launch that fell back to plain) does not get it.
            old, _ = self.launch(None, (2, 1, 266), 'resume')
            self.assertNotIn('--system-prompt-snapshot', old.argv)
            self.assertIn('--append-system-prompt', old.argv)  # the rest of the command is unchanged
            older, _ = self.launch(None, (2, 1, 267), 'resume')
            self.assertIn('--system-prompt-snapshot', older.argv)
        with self.subTest('a harness setting keeps the snapshot'):
            path = self.root / 'harnesses/claude/harness.json'
            config = read(path)
            config['settings']['system_prompt_snapshot'] = 'on'
            write(path, config)
            for setting in (None, True, False):
                with self.subTest(setting=setting):
                    resumed, _ = self.launch(setting, (2, 1, 300), 'resume')
                    self.assertNotIn('--system-prompt-snapshot', resumed.argv)  # Claude keeps its recorded prompt
                    self.assertIn('--append-system-prompt', resumed.argv)
            config['settings']['system_prompt_snapshot'] = 'off'
            write(path, config)
            self.assertIn('--system-prompt-snapshot', self.launch(None, (2, 1, 300), 'resume')[0].argv)
            for bad in ('yes', True, None, ''):
                with self.subTest(value=bad):
                    config['settings']['system_prompt_snapshot'] = bad
                    write(path, config)
                    with self.assertRaisesRegex(ValueError, 'system_prompt_snapshot must be "off" or "on"'):
                        harnesses.load(self.root, 'claude')
        with self.subTest("the user's own flag wins and is never removed"):
            path = self.root / 'harnesses/claude/harness.json'
            def flags(argv):
                argv = list(argv)
                end = argv.index('--') if '--' in argv else len(argv)
                return [(argv[i], argv[i + 1]) if argv[i] == '--system-prompt-snapshot' else (argv[i], None)
                        for i in range(end) if argv[i].startswith('--system-prompt-snapshot')]
            for snapshot in ('off', 'on'):
                for version in ((2, 1, 300), (2, 1, 266), None):
                    for key, user in (('resume_args', ['--system-prompt-snapshot', 'on']), ('args', ['--system-prompt-snapshot=on']),
                                      ('resume_args', ['--system-prompt-snapshot', 'off'])):
                        with self.subTest(snapshot=snapshot, version=version, key=key, user=user):
                            config = read(path)
                            config['settings'].update({'system_prompt_snapshot': snapshot, key: user})
                            config['settings'].pop('resume_args' if key == 'args' else 'args', None)
                            write(path, config)
                            resumed, _ = self.launch(None, version, 'resume')
                            # Exactly the user's own flag, as they wrote it; mv2 adds none beside it.
                            expected = [tuple(user)] if len(user) == 2 else [(user[0], None)]
                            self.assertEqual(flags(resumed.argv), expected)
            # Without a flag of the user's, mv2's own follows its settings as before.
            config = read(path)
            config['settings'].pop('resume_args', None); config['settings'].pop('args', None)
            config['settings']['system_prompt_snapshot'] = 'off'
            write(path, config)
            self.assertEqual(flags(self.launch(None, (2, 1, 300), 'resume')[0].argv),
                             [('--system-prompt-snapshot', 'off')])
            self.assertEqual(flags(self.launch(None, (2, 1, 266), 'resume')[0].argv), [])
            # A user's own bootstrap text and flag before mv2's: still only mv2's own flag is the one dropped.
            config['settings']['args'] = ['--append-system-prompt', 'mine', '--system-prompt-snapshot', 'off']
            write(path, config)
            resumed = self.launch(None, (2, 1, 300), 'resume')[0]
            self.assertEqual(flags(resumed.argv), [('--system-prompt-snapshot', 'off')])
            self.assertEqual(list(resumed.argv).count('--append-system-prompt'), 2)

    def test_bundled_claude_plugins_pass_claudes_own_checks(self):
        """The mod's TS hooks and the typeahead pass claude plugin validate and their own claude plugin test on the
        installed Claude; fragile because the hooks use Claude's private hook API, and this is the only check before
        live agents fail to register."""
        if not shutil.which('claude'):
            self.skipTest('Claude Code is not installed')
        for name, plugin in (('the mod', MOD), ('the typeahead', TYPEAHEAD)):
            with self.subTest(name):
                if plugin == TYPEAHEAD:
                    version = self.real_claude_version('claude')
                    if version is None or version < TYPEAHEAD_VERSION:
                        self.skipTest('The installed Claude Code is older than the typeahead needs')
                for command, timeout in ((['claude', 'plugin', 'validate', str(plugin)], 60),
                                         (['claude', 'plugin', 'test', str(plugin)], 180)):
                    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
