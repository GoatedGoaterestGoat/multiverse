"""What Claude's records tell about a conversation mv2 can't see: whether a resume that starts fresh lost one, whether a
failed wake's prompt arrived, and that an unreadable folder never counts as absence."""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
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
        history.jsonl and context reported after the latest /clear, and damaged files never block it, and an ignored
        CLAUDE_CONFIG_DIR is named; a break goes unnoticed because the resume succeeds either way and only the note
        tells the user their work was lost, or that their storage choice was not used (74887d9, qa-lifecycle 12bf723)."""
        with self.subTest('a saved conversation'):
            native = str(uuid4())
            path = self.storage / 'projects' / '-w' / f'{native}.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text('{"type": "user"}\n')
            self.assertIsNone(self.notice(self.session(native)))
            self.assertIn('--resume', self.resumed(self.session(native)).argv)
        with self.subTest('a CLAUDE_CONFIG_DIR the resume ignored'):
            # A resume keeps the storage recorded at spawn (lifecycle 620e411); the caller's other value is named,
            # as Codex names an ignored CODEX_HOME.
            native = str(uuid4())
            path = self.storage / 'projects' / '-w' / f'{native}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"type": "user"}\n')
            other = str(self.work / 'other-claude')
            alias = self.work / 'storage-alias'
            alias.symlink_to(self.storage)
            kept = str(self.storage.resolve())
            ignored = f'Kept its saved Claude storage "{kept}"; ignored CLAUDE_CONFIG_DIR="{other}".'
            for caller, expected in (({'CLAUDE_CONFIG_DIR': other}, ignored), ({'CLAUDE_CONFIG_DIR': str(alias)}, None),
                                     ({'CLAUDE_CONFIG_DIR': str(self.storage)}, None), ({}, None),
                                     ({'CLAUDE_CONFIG_DIR': ''}, None)):
                with self.subTest(caller=caller):
                    self.assertEqual(self.adapter.resume_notice(self.session(native), caller, self.environment), expected)
            # Launched with the default storage (CLAUDE_CONFIG_DIR absent or empty): the kept storage is the launch's
            # recorded home, else HOME/.claude of the launch environment, and never this process's.
            home = self.work / 'home'
            (home / '.claude').mkdir(parents=True)
            default = str((home / '.claude').resolve())
            named = f'Kept its saved Claude storage "{default}"; ignored CLAUDE_CONFIG_DIR="{other}".'
            pinned = replace(self.session(native), home=default)
            for session, effective, expected in ((pinned, {}, named), (pinned, {'CLAUDE_CONFIG_DIR': ''}, named),
                                                 (self.session(native), {'HOME': str(home)}, named),
                                                 (self.session(native), {}, None),  # nothing says where: no claim
                                                 (pinned, {}, named)):
                with self.subTest(home=session.home is not None, effective=effective):
                    with patch.dict(os.environ, {'HOME': str(self.work / 'checker-home')}):
                        notice = self.adapter.resume_notice(session, {'CLAUDE_CONFIG_DIR': other}, effective) or ''
                    # Only the storage sentence: a fresh-start note may follow it.
                    if expected:
                        self.assertTrue(notice.startswith(expected), notice)
                    else:
                        self.assertNotIn('Kept its saved', notice)
            self.assertNotIn('Kept its saved', self.adapter.resume_notice(pinned, {'CLAUDE_CONFIG_DIR': default}, {}) or '')
            # A fresh start keeps its own note after this one.
            fresh = self.adapter.resume_notice(self.session(str(uuid4())), {'CLAUDE_CONFIG_DIR': other}, self.environment)
            self.assertTrue(fresh.startswith(ignored + ' '), fresh)
            self.assertIn('stopped before its first message', fresh)
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

    def evidence(self, session=None, since=SINCE, prompt=PROMPT, environment=...):
        environment = self.environment if environment is ... else environment
        return self.adapter.startup_prompt_evidence(session or self.session(), prompt, since, environment)

    def reason(self, session=None, since=SINCE, prompt=PROMPT):
        return self.adapter.startup_prompt_evidence_reason(session or self.session(), prompt, since, self.environment)

    def test_startup_prompt_evidence_from_claudes_transcript(self):
        """Claude's transcript tells whether a woken launch that died recorded its prompt (True, False or None); fragile
        because it parses Claude's private JSONL with partial and damaged lines, and reads the launch's own storage
        (pinned in session.home), never the checker's: a wrong False resends a message while a wrong True strands it.
        A None says what can't be read, for deliver --check's advice (7d4d043, CLAUDE-10, qa-claude's 9f0331f
        retest)."""
        with self.subTest('a row this launch recorded'):
            self.native = str(uuid4())
            for blocks in (False, True):
                with self.subTest(blocks=blocks):
                    self.transcript({'type': 'system', 'timestamp': '2026-10-18T10:00:01.000Z'}, self.row(self.PROMPT, blocks=blocks))
                    self.assertIs(self.evidence(), True)
                    self.assertIsNone(self.reason())  # asked only after None; a definite answer has none
        with self.subTest('no transcript, or a complete one without the prompt'):
            self.native = str(uuid4())
            self.assertIs(self.evidence(), False)  # Claude exited before writing anything for this session
            self.assertIsNone(self.reason())
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
            damaged = self.transcript(self.row('something else'), raw='not json\n')  # damaged
            self.assertIsNone(self.evidence())
            self.assertEqual(self.reason(), f"Claude's transcript {damaged} has a damaged or unfinished row: "
                                            'if Claude is still writing it, wait')
            self.transcript(self.row('something else'))
            for since in (None, 'not a time', '2026-10-18'):  # missing, invalid, or without a time zone
                with self.subTest(since=since):
                    self.assertIsNone(self.evidence(since=since))
            self.assertIsNone(self.evidence(session=replace(self.session(), native_id=None)))
            self.assertIsNone(self.evidence(prompt=''))
            # Core's own facts are missing there; core says what, so Claude adds no reason.
            self.assertIsNone(self.reason(since=None))
            self.assertIsNone(self.reason(prompt=''))
            path = self.storage / 'projects' / '-w' / f'{self.native}.jsonl'
            path.unlink()
            path.mkdir()  # unreadable
            self.assertIsNone(self.evidence())
            # A recheck reads the launch's storage, never this process's: where neither the session nor the snapshot
            # says where it is, it can't tell, even though this process's own home has no such transcript.
            checker = self.work / 'checker-home'
            checker.mkdir()
            with patch.dict(os.environ, {'HOME': str(checker)}):
                os.environ.pop('CLAUDE_CONFIG_DIR', None)
                for environment in (None, {}, {'PATH': '/usr/bin'}, {'CLAUDE_CONFIG_DIR': 'relative/claude'}):
                    with self.subTest(environment=environment):
                        self.assertIsNone(self.evidence(environment=environment))
        with self.subTest('the storage the launch was pinned to'):
            # session.home is pinned when the launch starts; a snapshot pointing elsewhere, or none, never redirects it.
            self.native = str(uuid4())
            self.transcript(self.row(self.PROMPT))
            pinned = replace(self.session(), home=str(self.storage))
            for environment in ({'CLAUDE_CONFIG_DIR': str(self.work / 'other-claude')}, {}, None):
                with self.subTest(environment=environment):
                    self.assertIs(self.evidence(session=pinned, environment=environment), True)
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
        # Claude files a session under its launch directory's real path, every non-alphanumeric character a '-'.
        self.project = self.storage / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', os.path.realpath(self.work))

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
        evidence, resume and usage, but only one whose name could be this agent's workspace's blocks; fragile because
        pathlib.glob silently skips unreadable folders, and the folder name copies Claude's private, lossy rule (NFC,
        UTF-16 code units, a hash past 200), shared with GOAT: a wrong name skips the folder holding the session and
        resends a prompt, or lets one unrelated folder stop every agent (152f9c9, qa-compaction's 9f0331f retest)."""
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
        with self.subTest('what the reason says, and a locked transcript read again'):
            adapter = harnesses.load(self.root, 'claude')
            self.saved()
            session = SessionHandle(str(uuid4()), 'claude', str(self.work), native_id=self.native, state='failed')
            ask = lambda method: getattr(adapter, method)(session, 'another prompt', self.SINCE, self.environment)
            for locked in (self.project, self.storage / 'projects'):
                with self.subTest(locked=locked.name):
                    self.lock(locked)
                    found = ask('startup_prompt_evidence'), ask('startup_prompt_evidence_reason')
                    locked.chmod(0o700)
                    self.assertEqual(found, (None, f"Claude's storage folder {locked} can't be read: "
                                                   'restore read access to it'))
            # qa-claude's retest: a stand-in transcript at chmod 000, then 700. Readable again, it shows the prompt
            # never arrived, so the message becomes retryable.
            transcript = self.project / f'{self.native}.jsonl'
            self.lock(transcript)
            found = ask('startup_prompt_evidence'), ask('startup_prompt_evidence_reason')
            transcript.chmod(0o700)
            self.assertEqual(found, (None, f"Claude's transcript {transcript} can't be read: restore read access to it"))
            self.assertIs(ask('startup_prompt_evidence'), False)
            self.assertIsNone(ask('startup_prompt_evidence_reason'))
        with self.subTest("a legacy agent's storage is adopted only on proof"):
            # LC-28: core asks each candidate home whether this session's own transcript is there, and adopts
            # one only when exactly one says present and the others absent; anything unreadable is unknown.
            adapter = harnesses.load(self.root, 'claude')
            self.saved()
            session = SessionHandle(str(uuid4()), 'claude', str(self.work), native_id=self.native)
            empty = self.work / 'other-claude'
            (empty / 'projects' / '-private-tmp-scratch').mkdir(parents=True)
            evidence = lambda home, handle=session: adapter.session_storage_evidence(handle, home)
            self.assertEqual((evidence(str(self.storage)).state, evidence(str(self.storage)).value), ('ok', {'present': True}))
            self.assertEqual((evidence(str(empty)).state, evidence(str(empty)).value), ('ok', {'present': False}))
            self.assertEqual(evidence(str(self.work / 'no-such-home')).value, {'present': False})
            self.lock(empty / 'projects' / '-private-tmp-scratch')  # another workspace's folder proves nothing either way
            self.assertEqual(evidence(str(empty)).value, {'present': False})
            (empty / 'projects' / '-private-tmp-scratch').chmod(0o700)
            self.lock(self.project)  # the folder that could hold it
            found = evidence(str(self.storage))
            self.project.chmod(0o700)
            self.assertEqual(found.state, 'unknown')
            self.assertIn(str(self.project), found.reason)
            for home, handle in (('relative/claude', session), (str(self.storage), replace(session, native_id=None))):
                with self.subTest(home=home, native=handle.native_id):
                    self.assertEqual(evidence(home, handle).state, 'unknown')
        with self.subTest("another workspace's unreadable folder"):
            # Another tester's scratch folder, or a root-owned one a `sudo claude` left, is no place Claude files this
            # agent's sessions, so it must not stop this agent's lookups.
            from multiverse.harnesses.claude import NEVER_USED, fresh_start
            adapter = harnesses.load(self.root, 'claude')
            self.saved()
            agent = self.work / 'agent-elsewhere'
            agent.mkdir()
            session = SessionHandle(str(uuid4()), 'claude', str(self.work), native_id=self.native, state='failed',
                                    session_file=str(agent / 'session.jsonl'))
            unused = replace(session, native_id=str(uuid4()))
            other = self.storage / 'projects' / '-private-tmp-scratch'
            other.mkdir()
            self.lock(other)
            found = (adapter.startup_prompt_evidence(session, self.PROMPT, self.SINCE, self.environment),
                     adapter.startup_prompt_evidence(session, 'another prompt', self.SINCE, self.environment),
                     fresh_start(unused, self.environment) == NEVER_USED, adapter.get_usage(session).state)
            other.chmod(0o700)
            self.assertEqual(found, (True, False, True, 'ok'))
            # A Claude worktree under the workspace is filed under its name plus "-...": that folder still blocks.
            worktree = self.storage / 'projects' / (self.project.name + '--claude-worktrees-a')
            worktree.mkdir()
            self.lock(worktree)
            found = adapter.startup_prompt_evidence(session, 'another prompt', self.SINCE, self.environment)
            worktree.chmod(0o700)
            self.assertIsNone(found)
            # A recheck keeps the failed launch's own handle (f76d7f4): its workspace, not properties.json's, names the
            # folder that can hold the session, even after the agent moved on to another workspace.
            ran = self.work / 'ran-here'
            ran.mkdir()
            write(agent / 'properties.json', {'id': session.agent_id, 'harness': 'claude', 'workspace': str(self.work)})
            snapshot = replace(session, workspace=str(ran))
            for locked, blocks in ((self.project, False),
                                   (self.storage / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', os.path.realpath(ran)), True)):
                with self.subTest(locked=locked.name[-12:]):
                    locked.mkdir(exist_ok=True)
                    self.lock(locked)
                    found = adapter.startup_prompt_evidence(snapshot, 'another prompt', self.SINCE, self.environment)
                    locked.chmod(0o700)
                    self.assertEqual(found, None if blocks else False)
        with self.subTest("Claude's folder names"):
            # The table GOAT shares (agreed with compaction and mv2-lead): Bun replaces UTF-16 code units after NFC,
            # and past 200 units adds a hash only Bun can compute, so a long name is matched by its first 200 plus "-".
            from multiverse.harnesses.claude import could_hold, project_key
            for path, key in (('/Users/x/proj', '-Users-x-proj'), ('/private/tmp/claude-501/-Users-x', '-private-tmp-claude-501--Users-x'),
                              ('/w/run-2.1.289', '-w-run-2-1-289'), ('/w/caf\u00e9', '-w-caf-'), ('/w/cafe\u0301', '-w-caf-'),
                              ('/w/\U0001F600', '-w---')):
                with self.subTest(path=path):
                    self.assertEqual(project_key(path), key)
            long = '/' + 'a' * 229  # 230 code units
            for name, workspaces, holds in (
                    ('-Users-x-proj', ('/Users/x/proj',), True), ('-users-X-PROJ', ('/Users/x/proj',), True),
                    ('-Users-x-proj--claude-worktrees-a', ('/Users/x/proj',), True),
                    ('-Users-x-proj-old', ('/Users/x/proj',), True),  # a sibling over-blocks: rare and safe
                    ('-Users-x-other', ('/Users/x/proj',), False), ('-private-tmp-scratch', ('/Users/x/proj',), False),
                    ('-Users-x-pro', ('/Users/x/proj',), False),
                    (project_key(long)[:200] + '-1x2y3z', (long,), True), ('-' + 'a' * 199 + 'b-1x2y3z', (long,), False),
                    ('-Users-x-other', (), True), ('-Users-x-other', ('relative/proj',), True)):  # no usable workspace
                with self.subTest(name=name[:40], workspaces=[w[:20] for w in workspaces]):
                    self.assertIs(could_hold(name, workspaces), holds)
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


class ClaudeAcceptedMessageEvidenceTest(BaseTest):
    """LC-30: a message Claude accepted waits in its in-memory prompt queue, which dies with the process. Once that
    launch is gone and a later one has come up idle, the session's own transcript says whether it arrived."""

    SINCE = '2026-10-19T10:00:00.000000+00:00'
    TEXT = 'Message from lead:\nPlease rebase.'

    def setUp(self):
        super().setUp()
        self.storage = self.work / 'claude-config'
        self.adapter = harnesses.load(self.root, 'claude')
        self.native, self.message = str(uuid4()), str(uuid4())
        self.project = self.storage / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', os.path.realpath(self.work))

    def session(self, native=...):
        return SessionHandle(str(uuid4()), 'claude', str(self.work), native_id=self.native if native is ... else native)

    def transcript(self, *rows, raw=''):
        self.project.mkdir(parents=True, exist_ok=True)
        path = self.project / f'{self.native}.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows) + raw)
        return path

    def steer(self, message=None, prompt=None, at='2026-10-19T10:00:05.000Z'):
        """How a mid-turn steer is stored once the mod strips Claude's wrapper: a queued_command row with our msg_id."""
        return {'type': 'attachment', 'timestamp': at, 'attachment': {'type': 'queued_command', 'prompt': prompt or self.TEXT,
                'origin': {'kind': 'peer', 'from': 'uds:/x', 'msg_id': message or self.message}}}

    def prompt(self, text=None, at='2026-10-19T10:00:05.000Z'):
        """How a follow-up or idle steer is stored: the mod typed it in as the person's words."""
        return {'type': 'user', 'timestamp': at, 'message': {'role': 'user', 'content': text or self.TEXT},
                'origin': {'kind': 'plugin', 'name': 'multiverse', 'asUser': True}}

    def evidence(self, session=None, text=TEXT, since=SINCE, home=None, framed=True):
        result = self.adapter.accepted_message_evidence(session or self.session(), self.message, text, since,
                                                        home or str(self.storage), framed=framed)
        return result.state, (result.value or {}).get('delivered')

    def test_accepted_message_delivered_lost_or_unknown(self):
        """A message Claude accepted is delivered when the session's transcript holds it, in either stored form, lost
        only when every transcript that could hold it was read whole without it, and unknown otherwise; fragile because
        it parses Claude's private rows, and a wrong False makes someone resend a message the agent already got, while
        a wrong True hides a lost one (LC-30)."""
        rows = [
            ('a mid-turn steer, matched by its id', [self.steer(prompt='other text')], '', {}, ('ok', True)),
            ('a follow-up typed in after the accept', [self.prompt()], '', {}, ('ok', True)),
            ('an older prompt with the same text', [self.prompt(at='2026-10-19T09:59:00.000Z')], '', {}, ('ok', False)),
            ("another message's steer", [self.steer(message=str(uuid4()), prompt='other text')], '', {}, ('ok', False)),
            ('a complete transcript without it', [self.prompt('something else')], '', {}, ('ok', False)),
            ('a damaged row and no match', [self.prompt('something else')], 'not json\n', {}, ('unknown', None)),
            ('a damaged row beside the id', [self.steer()], '{"type": "user", "mess', {}, ('ok', True)),
            ('raw body found: another message could share it', [self.prompt()], '', {'text': 'Please rebase.', 'framed': False},
             ('unknown', None)),
            ('raw body absent', [self.prompt('something else')], '', {'text': 'Please rebase.', 'framed': False}, ('ok', False)),
            ('raw body with the id', [self.steer()], '', {'text': 'Please rebase.', 'framed': False}, ('ok', True)),
            ('a naive accept time', [self.prompt()], '', {'since': '2026-10-19T10:00:00'}, ('unknown', None)),
            ('no session ID', [self.prompt()], '', {'session': self.session(native=None)}, ('unknown', None)),
            ('a relative home', [self.prompt()], '', {'home': 'relative/claude'}, ('unknown', None)),
        ]
        for case, written, raw, options, expected in rows:
            with self.subTest(case):
                self.transcript(*written, raw=raw)
                self.assertEqual(self.evidence(**options), expected)
        with self.subTest('no transcript at all'):
            (self.project / f'{self.native}.jsonl').unlink()
            self.assertEqual(self.evidence(), ('ok', False))
        with self.subTest('a folder that could hold it is unreadable'):
            if os.geteuid() == 0:
                self.skipTest('root reads folders whatever their mode')
            self.transcript(self.prompt('something else'))
            self.project.chmod(0)
            try:
                found = self.adapter.accepted_message_evidence(self.session(), self.message, self.TEXT, self.SINCE,
                                                               str(self.storage))
            finally:
                self.project.chmod(0o700)
            self.assertEqual(found.state, 'unknown')
            self.assertIn(str(self.project), found.reason)
