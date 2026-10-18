"""What Claude's records tell about a conversation mv2 can't see: whether a resume that starts fresh lost one, whether a
failed wake's prompt arrived, and that an unreadable folder never counts as absence."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses
from multiverse.harnesses.claude import CONVERSATION_MISSING, NEVER_USED
from multiverse.harnesses.types import LaunchContext, SessionHandle
from multiverse.store import read, write
from test_multiverse import BaseTest


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

    def forget(self):
        """No record of an earlier message or context for this agent, and no prompt history in Claude's storage."""
        for name in ('inbox-state.json', 'runtime.json', 'sessions.jsonl'):
            (self.agent_dir / name).unlink(missing_ok=True)
        (self.storage / 'history.jsonl').unlink(missing_ok=True)

    def test_fresh_resume_reports_lost_or_unused_conversation(self):
        """A fresh resume says whether the saved conversation is missing or was never used, from receipts, Claude's
        history.jsonl and context reported after the latest /clear, and damaged files never block it; a break goes
        unnoticed because the resume succeeds either way and only the note tells the user their work was lost
        (74887d9)."""
        with self.subTest('a saved conversation'):
            native = str(uuid4())
            path = self.storage / 'projects' / '-w' / f'{native}.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text('{"type": "user"}\n')
            self.assertIsNone(self.notice(self.session(native)))
            self.assertIn('--resume', self.resumed(self.session(native)).argv)
        with self.subTest('no evidence of a message'):
            self.forget()
            native = str(uuid4())
            self.deliver(str(uuid4()), str(uuid4()))  # confirmed, but in an earlier conversation of this agent
            self.deliver(str(uuid4()), native, receipt=False)  # sent to this one, never stored
            self.assertEqual(self.notice(self.session(native)), NEVER_USED)
            self.assertFalse(self.resumed(self.session(native)).native_mode['fallback_reason'])
        with self.subTest('evidence of a conversation'):
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
                    self.forget()
                    native = str(uuid4())
                    evidence(native)
                    self.assertEqual(self.notice(self.session(native)), CONVERSATION_MISSING)
                    self.assertFalse(self.resumed(self.session(native)).native_mode['fallback_reason'])
        with self.subTest('context reported before the agent moved to this session'):
            self.forget()
            native = str(uuid4())
            write(self.agent_dir / 'runtime.json', {'usage': {'context': {'tokens': 1200}, 'reported_at': '2026-10-17T10:00:00+00:00'}})
            (self.agent_dir / 'sessions.jsonl').write_text(json.dumps(
                {'schema': 1, 'at': '2026-10-17T11:00:00+00:00', 'from': str(uuid4()), 'to': native, 'reason': 'clear'}) + '\n')
            self.assertEqual(self.notice(self.session(native)), NEVER_USED)  # /clear, then stopped before a message
            write(self.agent_dir / 'runtime.json', {'usage': {'context': {'tokens': 1200}, 'reported_at': '2026-10-17T12:00:00+00:00'}})
            self.assertEqual(self.notice(self.session(native)), CONVERSATION_MISSING)
        with self.subTest('damaged records'):
            self.forget()
            native = str(uuid4())
            (self.agent_dir / 'inbox-state.json').write_text('{"trunc\n')
            (self.agent_dir / 'runtime.json').write_text('[]')
            (self.agent_dir / 'sessions.jsonl').write_text('not json\n[]\n')
            (self.storage / 'history.jsonl').mkdir(parents=True)  # not a regular file
            self.assertEqual(self.notice(self.session(native)), NEVER_USED)
            self.assertIn('--session-id', self.resumed(self.session(native)).argv)


class ClaudeStartupPromptEvidenceTest(BaseTest):
    """qa-claude CLAUDE-10: when a launch carrying a wake prompt exits during startup, core asks whether Claude
    recorded the prompt. True keeps the message uncertain, False makes it a definite failure --retry resends,
    None (can't tell) keeps it uncertain."""

    SINCE = '2026-10-18T10:00:00.000000+00:00'
    PROMPT = 'Message from lead:\nPlease rebase.'

    def setUp(self):
        super().setUp()
        self.storage = self.work / 'claude-config'
        self.environment = {'CLAUDE_CONFIG_DIR': str(self.storage)}
        self.adapter = harnesses.load(self.root, 'claude')
        self.native = str(uuid4())

    def session(self, native=None):
        return SessionHandle(str(uuid4()), 'claude', str(self.work), launch_id='launch', native_id=native or self.native,
                             state='failed')

    def transcript(self, *rows, raw=None, native=None):
        path = self.storage / 'projects' / '-w' / f'{native or self.native}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        text = ''.join(json.dumps(row) + '\n' for row in rows)
        path.write_text(text + (raw or ''))
        return path

    def row(self, text, timestamp='2026-10-18T10:00:05.000Z', blocks=False):
        content = [{'type': 'text', 'text': text}] if blocks else text
        return {'type': 'user', 'message': {'role': 'user', 'content': content}, 'timestamp': timestamp,
                'sessionId': self.native, 'uuid': str(uuid4())}

    def evidence(self, session=None, since=SINCE, prompt=PROMPT):
        return self.adapter.startup_prompt_evidence(session or self.session(), prompt, since, self.environment)

    def test_startup_prompt_evidence_from_claudes_transcript(self):
        """Claude's transcript tells whether a woken launch that died recorded its prompt (True, False or None); fragile
        because it parses Claude's private JSONL with partial and damaged lines, and a wrong False resends a message
        while a wrong True strands it (7d4d043, CLAUDE-10)."""
        with self.subTest('a row this launch recorded'):
            self.native = str(uuid4())
            for blocks in (False, True):
                with self.subTest(blocks=blocks):
                    self.transcript({'type': 'system', 'timestamp': '2026-10-18T10:00:01.000Z'}, self.row(self.PROMPT, blocks=blocks))
                    self.assertIs(self.evidence(), True)
        with self.subTest('no transcript, or a complete one without the prompt'):
            self.native = str(uuid4())
            self.assertIs(self.evidence(), False)  # Claude exited before writing anything for this session
            self.transcript(self.row('Message from lead:\nPlease rebase.', timestamp='2026-10-18T09:59:00.000Z'),  # an older launch
                            self.row('something else'), {'type': 'assistant', 'message': {'content': 'ok'}})
            self.assertIs(self.evidence(), False)
            # Another session's transcript, and a subagent's, never count for this one.
            self.transcript(self.row(self.PROMPT), native=str(uuid4()))
            sub = self.storage / 'projects' / '-w' / self.native / 'subagents' / 'agent-1.jsonl'
            sub.parent.mkdir(parents=True)
            sub.write_text(json.dumps(self.row(self.PROMPT)) + '\n')
            self.assertIs(self.evidence(), False)
        with self.subTest('anything that cannot be told'):
            self.native = str(uuid4())
            self.transcript(self.row('something else'), raw='{"type": "user", "message": {"content": "Message from le')  # cut short
            self.assertIsNone(self.evidence())
            self.transcript(self.row('something else'), raw='not json\n')  # damaged
            self.assertIsNone(self.evidence())
            self.transcript(self.row('something else'))
            for since in (None, 'not a time', '2026-10-18'):  # missing, invalid, or without a time zone
                with self.subTest(since=since):
                    self.assertIsNone(self.evidence(since=since))
            self.assertIsNone(self.evidence(session=replace(self.session(), native_id=None)))
            self.assertIsNone(self.evidence(prompt=''))
            path = self.storage / 'projects' / '-w' / f'{self.native}.jsonl'
            path.unlink()
            path.mkdir()  # unreadable
            self.assertIsNone(self.evidence())
        with self.subTest('a damaged line beside the row'):
            self.native = str(uuid4())
            self.transcript(self.row(self.PROMPT), raw='not json\n')
            self.assertIs(self.evidence(), True)


class ClaudeUnreadableStorageTest(BaseTest):
    """pathlib's glob silently skips folders it can't read, so a scan that found nothing proved nothing. Every
    transcript lookup now says it can't tell (reviewer finding on 78113fb): a failed wake stays uncertain, a
    resume tries the saved conversation, and usage reports the folder instead of a wrong or missing number."""

    SINCE = '2026-10-18T10:00:00.000000+00:00'
    PROMPT = 'Message from lead:\nPlease rebase.'

    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest('root reads folders whatever their mode')
        self.storage = self.work / 'claude-config'
        self.environment = {'CLAUDE_CONFIG_DIR': str(self.storage)}
        self.enterContext(patch.dict(os.environ, self.environment))
        self.project = self.storage / 'projects' / '-w'

    def saved(self):
        """A new session whose transcript, with the prompt and one response, is in a readable project folder."""
        self.native = str(uuid4())
        self.project.mkdir(parents=True, exist_ok=True)
        row = {'type': 'user', 'message': {'role': 'user', 'content': self.PROMPT}, 'timestamp': '2026-10-18T10:00:05.000Z'}
        response = {'type': 'assistant', 'message': {'id': 'msg_1', 'usage': {'input_tokens': 10, 'output_tokens': 5}},
                    'timestamp': '2026-10-18T10:00:06.000Z'}
        (self.project / f'{self.native}.jsonl').write_text(json.dumps(row) + '\n' + json.dumps(response) + '\n')

    def lock(self, path):
        path.chmod(0)
        self.addCleanup(path.chmod, 0o700)

    def test_an_unreadable_storage_folder_never_proves_absence(self):
        """A Claude storage folder that cannot be read never counts as proof that a transcript is absent, for wake
        evidence, resume and usage; fragile because pathlib.glob silently skips unreadable folders, so any new scan
        brings back a resend, an abandoned conversation or a wrong number (152f9c9)."""
        with self.subTest('wake evidence'):
            self.saved()
            from multiverse.harnesses.claude import prompt_recorded
            # A readable control.
            self.assertIs(prompt_recorded(self.native, self.PROMPT, self.SINCE, self.environment), True)
            self.lock(self.project)  # the transcript's own folder
            self.assertIsNone(prompt_recorded(self.native, self.PROMPT, self.SINCE, self.environment))
            self.project.chmod(0o700)
            other = self.storage / 'projects' / '-elsewhere'
            other.mkdir()
            (self.project / f'{self.native}.jsonl').unlink()
            self.lock(other)  # another project folder, which could hold it
            self.assertIsNone(prompt_recorded(self.native, self.PROMPT, self.SINCE, self.environment))
            other.chmod(0o700)
            self.lock(self.storage / 'projects')  # the projects folder itself
            self.assertIsNone(prompt_recorded(self.native, self.PROMPT, self.SINCE, self.environment))
            (self.storage / 'projects').chmod(0o700)
            self.assertIs(prompt_recorded(self.native, self.PROMPT, self.SINCE, self.environment), False)  # all read: absent
        with self.subTest('resume'):
            self.saved()
            from multiverse.harnesses.claude import fresh_start
            self.lock(self.project)
            session = SessionHandle(str(uuid4()), 'claude', str(self.work), native_id=self.native)
            # No fresh start and no note: Claude says if it is gone.
            self.assertIsNone(fresh_start(session, self.environment))
            self.project.chmod(0o700)
        with self.subTest('usage'):
            self.saved()
            adapter = harnesses.load(self.root, 'claude')
            agent = self.work / 'agent'
            agent.mkdir()
            session = SessionHandle(str(uuid4()), 'claude', str(self.work), native_id=self.native,
                                    session_file=str(agent / 'session.jsonl'))
            self.assertEqual(adapter.get_usage(session).value['total_tokens'], 15)  # readable control
            subagents = self.project / self.native / 'subagents'
            subagents.mkdir(parents=True)
            self.lock(subagents)  # a subagent's usage would be missing from the total
            found = adapter.get_usage(session)
            self.assertEqual(found.state, 'error')
            self.assertIn(str(subagents), found.reason)
            subagents.chmod(0o700)
            self.lock(self.project)
            found = adapter.get_usage(session)
            self.assertEqual(found.state, 'error')  # not "has not written a transcript yet"
            self.assertIn("can't be read", found.reason)
