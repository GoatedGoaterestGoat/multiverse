"""A Claude agent with no live connection by design, what capabilities say without a launch, and what a resume
that starts fresh says.

A launch-only agent (inbox_bridge false, or a Claude Code too old for the mod) can't be connected by restarting
it, so its unavailable results carry a typed `launch_only` flag that core's advice reads. A bridged launch whose
mod hasn't registered gets no flag: that connection should exist.
"""
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses
from multiverse.harnesses.claude import (CONVERSATION_MISSING, FRESH_SESSION, NEVER_USED, NOT_REGISTERED)
from multiverse.harnesses.types import LaunchContext, Message, NativeEndpoint, SessionHandle
from multiverse.store import read, write
from multiverse.tools import capabilities
from test_multiverse import BaseTest

TOO_OLD = 'Claude Code 2.1.279 is older than 2.1.280, which the multiverse mod needs'


class ClaudeLaunchOnlyTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.storage = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.storage)}))
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 302)))

    def adapter(self, setting=None):
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings'] = {key: value for key, value in config['settings'].items() if key != 'inbox_bridge'}
        if setting is not None:
            config['settings']['inbox_bridge'] = setting
        write(path, config)
        return harnesses.load(self.root, 'claude')

    def plain(self, policy, fallback_reason=None):
        return SessionHandle('agent', 'claude', str(self.work), launch_id='launch', native_id=str(uuid4()),
                             state='running', native_mode={'policy': policy, 'mode': 'plain',
                                                           'fallback_reason': fallback_reason})

    def test_a_launch_only_agent_says_why_and_is_flagged_so_no_restart_is_advised(self):
        executor = Mock()
        message = Message(str(uuid4()), 'hi', framed='Message from lead:\nhi')
        cases = (
            # inbox_bridge false: the setting is the cause, also after a fresh start added its note.
            ('plain', False, None, 'its harness sets inbox_bridge to false'),
            ('plain', False, NEVER_USED, 'its harness sets inbox_bridge to false'),
            # Unset (auto) on a Claude too old for the mod: the version is the cause, never the fresh-start note.
            ('auto', None, TOO_OLD, TOO_OLD),
            ('auto', None, f'{TOO_OLD}; {CONVERSATION_MISSING}', TOO_OLD),
            ('auto', None, f'{TOO_OLD}; {FRESH_SESSION}', TOO_OLD),  # a record from before this change
        )
        for policy, setting, fallback, cause in cases:
            with self.subTest(policy=policy, fallback=fallback):
                adapter, session = self.adapter(setting), self.plain(policy, fallback)
                for result in (adapter.send(session, message, executor), adapter.ask(session, 'why?', executor)):
                    self.assertEqual(result.state, 'unavailable')
                    self.assertEqual(result.value, {'launch_only': True})
                    self.assertEqual(result.reason, f'This agent is launch-only: {cause}')
                found = adapter.capabilities(session)
                for name in ('send_steer', 'send_follow_up', 'ask'):
                    self.assertEqual((found[name].state, found[name].reason), ('unavailable', f'This agent is launch-only: {cause}'))
                self.assertNotIn('get_delivery', {name for name, value in found.items() if value.state == 'supported'})
        executor.write_lines.assert_not_called()

    def test_a_bridged_launch_whose_mod_has_not_registered_keeps_its_restart_case(self):
        adapter = self.adapter()
        session = SessionHandle('agent', 'claude', str(self.work), launch_id='launch', native_id=str(uuid4()),
                                state='running', endpoint=NativeEndpoint('unix-lines', str(self.work / 'in.sock')),
                                native_mode={'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None})
        message = Message(str(uuid4()), 'hi', framed='Message from lead:\nhi')
        for result in (adapter.send(session, message, Mock()), adapter.ask(session, 'why?', Mock())):
            self.assertEqual((result.state, result.reason, result.value), ('unavailable', NOT_REGISTERED, None))
        # Claude does implement ask: until the mod registers, it says that, not "No native implementation".
        self.assertEqual(adapter.capabilities(session)['ask'].reason, NOT_REGISTERED)

    def harness(self, setting, version=(2, 1, 302), probe=False):
        self.adapter(setting)
        with patch('multiverse.harnesses.claude.claude_version', return_value=version) as asked:
            found = capabilities.run(self.root, harness='claude', probe=probe)
        return found, asked

    def test_capabilities_without_a_launch_say_what_this_harness_would_do(self):
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


    def test_usage_without_a_launch_matches_what_every_claude_launch_has(self):
        # qa-claude CLAUDE-9: usage is measured from Claude's transcript, with or without the bridge.
        for setting in (None, True, False):
            for probe in (False, True):
                with self.subTest(setting=setting, probe=probe):
                    found, _ = self.harness(setting, probe=probe)
                    self.assertEqual((found['get_usage']['state'], found['get_usage']['mechanism']),
                                     ('supported', 'claude-transcript'))
        old, _ = self.harness(None, version=(2, 1, 279), probe=True)  # plain, but its transcript still counts
        self.assertEqual(old['get_usage']['state'], 'supported')

class ClaudeFreshResumeNoticeTest(BaseTest):
    """Resume keeps the fresh start of a session Claude has no conversation for (PR #3), and now says so in the
    resume result: "stopped before its first message" unless mv2 or Claude has evidence that one existed."""

    def setUp(self):
        super().setUp()
        self.storage = self.work / 'claude-config'
        self.environment = {'CLAUDE_CONFIG_DIR': str(self.storage)}
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 302)))
        self.agent_dir = self.work / 'agent'
        self.agent_dir.mkdir()
        self.adapter = harnesses.load(self.root, 'claude')

    def session(self, native_id):
        return SessionHandle(str(uuid4()), 'claude', str(self.work), native_id=native_id,
                             session_file=str(self.agent_dir / 'session.jsonl'))

    def notice(self, session):
        return self.adapter.resume_notice(session, {}, self.environment)

    def resumed(self, session):
        values = dict(name='worker', agent_id=session.agent_id, session_id=session.native_id, bootstrap='Hello',
                      bootstrap_json='"Hello"', task='', agent_dir=str(self.agent_dir), harness_dir='/h',
                      session_file='', bootstrap_file='', task_file='', workspace=str(self.work), model='',
                      control_socket=str(self.work / 'in.sock'))
        return self.adapter.resume(session, LaunchContext('resume', values, environment=self.environment))

    def deliver(self, message_id, target, receipt=True):
        """A message as core records it, and the mod's receipt once its row was stored in a conversation."""
        path = self.agent_dir / 'inbox-state.json'
        state = read(path) if path.exists() else {}
        state[message_id] = {'submission_state': 'accepted', 'target_session': target,
                             'attempts': [{'state': 'native_queued', 'native_session_id': target}]}
        write(path, state)
        if receipt:
            (self.agent_dir / 'claude-receipts').mkdir(exist_ok=True)
            (self.agent_dir / 'claude-receipts' / f'{message_id}.json').write_text(json.dumps({'route': 'mid-turn'}))

    def test_a_saved_conversation_resumes_with_no_notice(self):
        native = str(uuid4())
        path = self.storage / 'projects' / '-w' / f'{native}.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text('{"type": "user"}\n')
        self.assertIsNone(self.notice(self.session(native)))
        self.assertIn('--resume', self.resumed(self.session(native)).argv)

    def test_with_no_evidence_of_a_message_it_was_stopped_before_its_first_one(self):
        native = str(uuid4())
        self.deliver(str(uuid4()), str(uuid4()))  # confirmed, but in an earlier conversation of this agent
        self.deliver(str(uuid4()), native, receipt=False)  # sent to this one, never stored
        self.assertEqual(self.notice(self.session(native)), NEVER_USED)
        self.assertEqual(self.resumed(self.session(native)).native_mode['fallback_reason'], NEVER_USED)

    def test_evidence_of_a_conversation_says_the_saved_one_is_missing(self):
        def receipt(native):
            self.deliver(str(uuid4()), native)

        def typed_prompt(native):
            # Claude's own prompt history names the session of every prompt typed into it.
            history = self.storage / 'history.jsonl'
            history.parent.mkdir(parents=True, exist_ok=True)
            with history.open('a') as stream:
                stream.write(json.dumps({'display': 'fix it', 'pastedContents': {}, 'timestamp': 1, 'project': str(self.work),
                                         'sessionId': native}, separators=(',', ':')) + '\n')

        def reported_context(native):
            write(self.agent_dir / 'runtime.json', {'usage': {'context': {'tokens': 1200}, 'reported_at': '2026-10-17T10:00:00+00:00'}})

        for evidence in (receipt, typed_prompt, reported_context):
            with self.subTest(evidence=evidence.__name__):
                for name in ('inbox-state.json', 'runtime.json', 'sessions.jsonl'):
                    (self.agent_dir / name).unlink(missing_ok=True)
                (self.storage / 'history.jsonl').unlink(missing_ok=True)
                native = str(uuid4())
                evidence(native)
                self.assertEqual(self.notice(self.session(native)), CONVERSATION_MISSING)
                self.assertEqual(self.resumed(self.session(native)).native_mode['fallback_reason'], CONVERSATION_MISSING)

    def test_context_reported_before_the_agent_moved_to_this_session_is_not_its_evidence(self):
        native = str(uuid4())
        write(self.agent_dir / 'runtime.json', {'usage': {'context': {'tokens': 1200}, 'reported_at': '2026-10-17T10:00:00+00:00'}})
        (self.agent_dir / 'sessions.jsonl').write_text(json.dumps(
            {'schema': 1, 'at': '2026-10-17T11:00:00+00:00', 'from': str(uuid4()), 'to': native, 'reason': 'clear'}) + '\n')
        self.assertEqual(self.notice(self.session(native)), NEVER_USED)  # /clear, then stopped before a message
        write(self.agent_dir / 'runtime.json', {'usage': {'context': {'tokens': 1200}, 'reported_at': '2026-10-17T12:00:00+00:00'}})
        self.assertEqual(self.notice(self.session(native)), CONVERSATION_MISSING)

    def test_damaged_records_are_no_evidence_and_never_stop_a_resume(self):
        native = str(uuid4())
        (self.agent_dir / 'inbox-state.json').write_text('{"trunc\n')
        (self.agent_dir / 'runtime.json').write_text('[]')
        (self.agent_dir / 'sessions.jsonl').write_text('not json\n[]\n')
        (self.storage / 'history.jsonl').mkdir(parents=True)  # not a regular file
        self.assertEqual(self.notice(self.session(native)), NEVER_USED)
        self.assertIn('--session-id', self.resumed(self.session(native)).argv)


class ClaudeInheritedVariablesTest(BaseTest):
    def test_a_parent_claudes_ide_port_and_inbox_never_reach_an_agent(self):
        # A Claude agent spawned from inside Claude inherits the parent's IDE port and its inbox path and secret.
        from multiverse.runtime import runner
        names = ('CLAUDE_CODE_SSE_PORT', 'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN')
        inherited = {name: 'parent' for name in names}
        path = self.root / 'harnesses/claude/harness.json'
        for settings, kept in (({}, {}), ({'unset_env': ['NO_COLOR']}, {}),
                               ({'env': {'CLAUDE_CODE_SSE_PORT': '7777'}}, {'CLAUDE_CODE_SSE_PORT': '7777'})):
            with self.subTest(settings=settings):
                config = read(path)
                config['settings'] = {**{k: v for k, v in config['settings'].items() if k not in ('unset_env', 'env')}, **settings}
                write(path, config)
                with patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 302)):
                    command = harnesses.load(self.root, 'claude').spawn(LaunchContext('spawn', dict(
                        name='w', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='', bootstrap_json='""',
                        task='', agent_dir=str(self.work / 'a'), harness_dir='/h', session_file='', bootstrap_file='',
                        task_file='', workspace=str(self.work), model='', control_socket=str(self.work / 's.sock'))))
                with patch.dict(os.environ, {**inherited, 'PATH': '/usr/bin'}):
                    env = runner.launch_environment({'unset_env': list(command.unset_env),
                                                     'env': {**command.environment, 'MV2_CLI': '/mv2/bin/mv2'}})
                self.assertEqual({name: env[name] for name in names if name in env}, kept)
