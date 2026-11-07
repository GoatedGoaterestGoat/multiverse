"""Claude's part of `mv2 answer`: the prompts the mod publishes, answer checks and outcomes from Claude's own stored
rows, with no Claude session."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses
from multiverse.harnesses.claude import BRIDGE, checked_version
from multiverse.harnesses.types import NativeEndpoint, NativeResult, PromptAnswer, SessionHandle
from multiverse.store import read, write
from test_multiverse import BaseTest

TOKEN = '0123456789abcdef0123456789abcdef'
CALL = 'toolu_01Abc'
QUESTIONS = [
    {'id': 'Which colour?', 'header': 'Colour', 'question': 'Which colour?', 'options': [['Blue', ''], ['Red', '']], 'multiple': False, 'free_text': True},
    {'id': 'Which fruits?', 'header': 'Fruit', 'question': 'Which fruits?', 'options': [['Apple', ''], ['Plum', '']], 'multiple': True, 'free_text': True}]


def iso(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat().replace('+00:00', 'Z')


class ClaudePromptsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.socket = str(self.work / 'inbox.sock')
        self.config = self.work / 'claude-config'
        self.agent_dir = self.work / 'agent'
        (self.agent_dir / 'claude-prompts').mkdir(parents=True)
        (self.agent_dir / 'properties.json').write_text(json.dumps({'name': 'a1'}))
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 319)))
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings']['inbox_bridge'] = True
        write(path, config)
        self.adapter = harnesses.load(self.root, 'claude')
        digest = hashlib.sha256(os.path.abspath(self.socket).encode()).hexdigest()
        (self.config / 'sessions').mkdir(parents=True)
        (self.config / 'sessions' / f'4242.{digest}.key').write_text(json.dumps({'peerToken': TOKEN}))
        self.native = str(uuid4())
        self.transcript = self.config / 'projects' / 'p' / f'{self.native}.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text('')

    def session(self, bridge=BRIDGE, status='waiting', launch='launch-1'):
        return SessionHandle('agent', 'claude', str(self.work), launch_id=launch, native_id=self.native, state='running',
                             endpoint=NativeEndpoint('unix-lines', self.socket), bridge=bridge, status=status,
                             session_file=str(self.agent_dir / 'session.jsonl'), home=str(self.config))

    def publish(self, **fields):
        """A prompt as the mod publishes it (hooks/prompts.ts)."""
        id = fields.pop('id', str(uuid4()))
        record = {'schema': 1, 'id': id, 'instance': 'i', 'launch': 'launch-1', 'session': self.native, 'claude': checked_version(),
                  'tool_use_id': CALL, 'kind': 'permission', 'tool': 'Bash', 'asked_at': iso(), 'summary': 'touch /tmp/x/a',
                  'detail': {'input': {'command': 'touch /tmp/x/a'}, 'reason': 'needs approval'},
                  'choices': [{'id': 'allow', 'label': 'Allow once', 'saves_rule': False, 'takes_message': False},
                              {'id': 'allow_always', 'label': 'Allow, and also allow Bash(touch:*), saved', 'saves_rule': True, 'takes_message': False},
                              {'id': 'deny', 'label': 'Deny; the agent carries on', 'saves_rule': False, 'takes_message': True}],
                  'questions': [], 'answerable': True, 'reason': None, 'subagent': None,
                  'transcript': {'path': str(self.transcript), 'offset': 0}, **fields}
        (self.agent_dir / 'claude-prompts' / f'{id}.json').write_text(json.dumps(record))
        return id

    def end(self, id, **fields):
        record = {'schema': 1, 'id': id, 'kind': 'permission', 'tool_use_id': CALL,
                  'transcript': {'path': str(self.transcript), 'offset': 0}, 'at': iso(), **fields}
        (self.agent_dir / 'claude-prompts' / f'{id}.end.json').write_text(json.dumps(record))

    def rows(self, *rows):
        self.transcript.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_only_the_open_prompts_of_this_launch_and_conversation_are_listed(self):
        """A prompt from an earlier launch, another conversation (after /clear) or one that ended must never be
        answerable: an answer to it would wait for a hook that no longer holds it. An unchecked Claude Code version
        is listed for the person but not answerable, since its answer path hasn't been checked live."""
        current = self.publish()
        older = self.publish(launch='launch-0')
        cleared = self.publish(session=str(uuid4()))
        ended = self.publish()
        self.end(ended, how='closed', cause='person')
        newer = self.publish(claude='2.1.999', asked_at=iso(-1))
        listed = self.adapter.waiting_prompts(self.session())
        self.assertEqual(listed.state, 'ok')
        prompts = {p['id']: p for p in listed.value['prompts']}
        self.assertEqual(set(prompts), {current, newer})
        self.assertNotIn(older, prompts)
        self.assertNotIn(cleared, prompts)
        self.assertTrue(prompts[current]['answerable'])
        self.assertEqual(prompts[current]['choices'][1], {'id': 'allow_always', 'label': 'Allow, and also allow Bash(touch:*), saved',
                                                         'saves_rule': True, 'takes_message': False})
        self.assertFalse(prompts[newer]['answerable'])
        self.assertIn("aren't checked on Claude Code 2.1.999", prompts[newer]['reason'])
        self.assertEqual(listed.value['coverage'], 'complete')

    def test_coverage_and_which_launches_publish_prompts(self):
        """A waiting agent with nothing published must read as unknown, not as 'no prompt': the person would look
        elsewhere while it waits. A mod older than v4 publishes nothing, and says how to get the current one."""
        empty = self.adapter.waiting_prompts(self.session())
        self.assertEqual((empty.value['coverage'], empty.value['prompts']), ('unknown', []))
        self.assertIn('published no prompt', empty.value['reason'])
        self.assertEqual(self.adapter.waiting_prompts(self.session(status='working')).value['coverage'], 'complete')
        older = self.adapter.waiting_prompts(self.session(bridge='claude-mod-v3'))
        self.assertEqual(older.state, 'unavailable')
        self.assertIn('mv2 kill a1, then mv2 resume a1', older.reason)
        self.assertEqual(self.adapter.capabilities(self.session(bridge='claude-mod-v3'))['answer'].state, 'unavailable')
        self.assertEqual(self.adapter.capabilities(self.session())['answer'].state, 'supported')
        # A v3 mod still runs mv2 slash: the new bridge name must not take it away.
        self.assertEqual(self.adapter.capabilities(self.session(bridge='claude-mod-v3'))['slash'].state, 'supported')

    def test_an_answer_is_checked_before_core_writes_it(self):
        """An answer the mod would refuse must be refused before it is written: core writes one answer per prompt,
        so a bad one would use up the prompt's only answer. A rule saved to the person's settings is the person's."""
        permission = self.publish()
        question = self.publish(kind='question', tool='AskUserQuestion', choices=[], questions=QUESTIONS)
        both = {'Which colour?': ('Blue',), 'Which fruits?': ('Apple', 'Plum')}
        cases = [
            ('an unoffered choice', PromptAnswer(id=permission, choice='allow_session'), 'not offered'),
            ("a plugin's saved rule", PromptAnswer(id=permission, choice='allow_always'), 'only the person'),
            ('a message where none is taken', PromptAnswer(id=permission, choice='allow', message='hi'), 'takes no message'),
            ('a question left out', PromptAnswer(id=question, answers={'Which colour?': ('Blue',)}), 'every question'),
            ('two answers for one', PromptAnswer(id=question, answers={**both, 'Which colour?': ('Blue', 'Red')}), 'one answer'),
            ('a choice for questions', PromptAnswer(id=question, choice='allow', answers=both), 'per question'),
            ('a closed prompt', PromptAnswer(id=str(uuid4()), choice='allow'), 'no longer open'),
        ]
        executor = Mock()
        for label, answer, reason in cases:
            with self.subTest(label):
                reply = self.adapter.answer_prompt(self.session(), answer, executor)
                self.assertNotEqual(reply.state, 'ok')
                self.assertIn(reason, reply.reason)
        executor.write_prompt_answer.assert_not_called()
        executor.write_prompt_answer.return_value = NativeResult('pending', value={'state': 'submitted'})
        for answer in (PromptAnswer(id=permission, choice='allow_always', allow_rule=True), PromptAnswer(id=question, answers=both)):
            self.assertEqual(self.adapter.answer_prompt(self.session(), answer, executor).state, 'pending')
        self.assertEqual(executor.write_prompt_answer.call_count, 2)

    def test_the_outcome_comes_from_what_claude_stored(self):
        """Claude runs the hook beside its dialog and the first answer wins, so only its stored rows say which took:
        its decision row when mv2's answer did, the person's accept or rejection on the call's result when they
        answered first. Reporting 'answered' on the mod's word alone would be wrong whenever the person was quicker, and
        a decision row proves only which call: rows that disagree with the answer mv2 sent, or with each other, stay
        unknown."""
        result = lambda **extra: {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': CALL, 'content': 'ok'}]}, **extra}
        decision = lambda verdict, **extra: {'type': 'attachment', 'attachment': {'type': 'hook_permission_decision', 'decision': verdict,
                                                                                  'toolUseID': CALL}, **extra}
        decided = decision('allow')
        cases = [
            ('mv2 answered', {'how': 'sent', 'choice': 'allow_always', 'saved_rule': True},
             [decided, result(permissionDecision={'decision': 'accept', 'source': 'hook'})], {'state': 'answered', 'saved_rule': True}),
            ("mv2's refusal took", {'how': 'sent', 'choice': 'deny'}, [decision('deny')], {'state': 'answered'}),
            ("mv2's answers to questions took", {'how': 'sent', 'choice': None, 'kind': 'question'}, [decided], {'state': 'answered'}),
            ('a decision other than the one sent', {'how': 'sent', 'choice': 'deny', 'at': iso(60)}, [decided],
             {'state': 'unknown', 'cause': 'contradicted'}),
            ('two decisions for one call', {'how': 'sent', 'choice': 'allow'},
             [decided, result(permissionDecision={'decision': 'reject', 'source': 'hook'})], {'state': 'unknown', 'cause': 'contradicted'}),
            ("a decision outside Claude's words", {'how': 'sent', 'choice': 'allow'}, [decision('maybe')],
             {'state': 'unknown', 'cause': 'contradicted'}),
            ("another conversation's row in the file", {'how': 'sent', 'choice': 'allow'}, [decision('allow', sessionId=str(uuid4()))],
             {'state': 'unknown', 'cause': 'contradicted'}),
            # An explicit null names no conversation; only a row without the field is taken as this one's.
            ('a row naming no conversation', {'how': 'sent', 'choice': 'allow'}, [decision('allow', sessionId=None)],
             {'state': 'unknown', 'cause': 'contradicted'}),
            ("a row naming this conversation", {'how': 'sent', 'choice': 'allow'}, [decision('allow', sessionId=self.native)],
             {'state': 'answered'}),
            ('the person allowed it first', {'how': 'sent'}, [result(permissionDecision={'decision': 'accept', 'source': 'user_temporary', 'grantScope': 'once'})],
             {'state': 'person', 'person_answer': {'choice': 'allow'}}),
            ('the person allowed it for the session', {'how': 'closed', 'cause': 'resolved'},
             [result(permissionDecision={'decision': 'accept', 'source': 'user_temporary', 'grantScope': 'session'})],
             {'state': 'person', 'person_answer': {'choice': 'allow_session'}}),
            ('the person refused it', {'how': 'closed', 'cause': 'resolved'},
             [result(permissionDecision={'decision': 'reject', 'source': 'user_reject'}, toolDenialKind='user-rejected')],
             {'state': 'person', 'person_answer': {'choice': 'deny'}}),
            # Esc on Claude's dialog, as 2.1.319 stores it: no permission decision, only the rejection.
            ('Esc on the dialog', {'how': 'closed', 'cause': 'resolved'}, [result(toolDenialKind='user-rejected')],
             {'state': 'person', 'person_answer': {'choice': 'deny'}}),
            # Claude joins a multi-select's labels with ", "; typed text with a comma in it must stay one answer.
            ("the person's answers to questions", {'how': 'closed', 'cause': 'resolved', 'kind': 'question', 'questions': QUESTIONS},
             [result(permissionDecision={'decision': 'accept', 'source': 'user_temporary'},
                     toolUseResult={'answers': {'Which fruits?': 'Apple, Plum', 'Which colour?': 'Green, please'}})],
             {'state': 'person', 'person_answer': {'answers': {'Which fruits?': ['Apple', 'Plum'], 'Which colour?': ['Green, please']}},
              'questions': [{**q, 'secret': False, 'required': True} for q in QUESTIONS]}),
            ('Esc', {'how': 'closed', 'cause': 'interrupted'}, [], {'state': 'gone', 'cause': 'interrupted'}),
            # Stopping an agent ends its calls as it goes: the old mod's last cause isn't the reason, the stop is.
            ('closed by a stop', {'how': 'closed', 'cause': 'resolved', 'launch': 'launch-0'}, [], {'state': 'gone', 'cause': 'stopped'}),
            ('closed in its own launch, nobody known', {'how': 'closed', 'cause': 'resolved', 'launch': 'launch-1'}, [],
             {'state': 'gone', 'cause': 'closed'}),
            # A restart since never hides what Claude stored: a --result after it still reads the evidence first.
            ("mv2's answer, settled after a restart", {'how': 'sent', 'choice': 'allow', 'launch': 'launch-0'}, [decided],
             {'state': 'answered'}),
            ("the person's answer, then a restart", {'how': 'closed', 'cause': 'resolved', 'launch': 'launch-0'},
             [result(permissionDecision={'decision': 'accept', 'source': 'user_temporary', 'grantScope': 'once'})],
             {'state': 'person', 'person_answer': {'choice': 'allow'}}),
            ('the turn ended', {'how': 'closed', 'cause': 'turn_ended'}, [], {'state': 'gone', 'cause': 'turn_ended'}),
            # Long option descriptions make an end record larger than a receipt; it must still read.
            ('a large question prompt ended', {'how': 'closed', 'cause': 'turn_ended', 'kind': 'question', 'questions': [
                {**QUESTIONS[0], 'options': [['Blue', 'x' * 40000], ['Red', 'y' * 40000]]}]}, [],
             {'state': 'gone', 'cause': 'turn_ended'}),
            ('the mod refused it', {'how': 'refused', 'reason': 'x'}, [], {'state': 'refused'}),
            ('just sent', {'how': 'sent'}, [], {'state': 'submitted'}),
            ('sent, never taken', {'how': 'sent', 'at': iso(60)}, [], {'state': 'unknown', 'cause': 'not_taken'}),
        ]
        for label, end, rows, expected in cases:
            with self.subTest(label):
                id = self.publish()
                (self.agent_dir / 'claude-prompts' / f'{id}.json').unlink()
                self.end(id, **end)
                self.rows(*rows)
                reply = self.adapter.prompt_outcome(self.session(), PromptAnswer(id=id))
                self.assertEqual(reply.state, 'ok')
                self.assertEqual({k: reply.value.get(k) for k in expected}, expected)
        # The agent stopped and wasn't resumed: its launch is still the recorded one, so only the missing result row
        # tells the stop (the call never finished) from a person who answered (their answer always leaves one).
        stopped = replace(self.session(), state='dormant')
        for label, rows, expected in (
                ('closed by the stop, before a resume', [], {'state': 'gone', 'cause': 'stopped'}),
                ("the person's answer, then the stop",
                 [result(permissionDecision={'decision': 'accept', 'source': 'user_temporary', 'grantScope': 'once'})],
                 {'state': 'person', 'person_answer': {'choice': 'allow'}}),
                # Claude stored a result that isn't the person's (a rule allowed the call): it finished, so the stop
                # didn't close the prompt, and nothing better than closed is known.
                ('a result from a rule, then the stop', [result(permissionDecision={'decision': 'accept', 'source': 'config'})],
                 {'state': 'gone', 'cause': 'closed'})):
            with self.subTest(label):
                id = self.publish()
                (self.agent_dir / 'claude-prompts' / f'{id}.json').unlink()
                self.end(id, how='closed', cause='resolved', launch='launch-1')
                self.rows(*rows)
                reply = self.adapter.prompt_outcome(stopped, PromptAnswer(id=id))
                self.assertEqual({k: reply.value.get(k) for k in expected}, expected)
        # An open prompt is submitted only once core's answer for it exists (waiting, or already taken by the mod):
        # one mv2 sent nothing to is just open, never claimed as sent.
        open_one = self.publish()
        reply = self.adapter.prompt_outcome(self.session(), PromptAnswer(id=open_one)).value
        self.assertEqual((reply['state'], reply.get('cause')), ('unknown', 'open'))
        answers = self.agent_dir / 'prompts'
        answers.mkdir(exist_ok=True)
        for suffix in ('.answer.json', '.answer.taken.json'):
            with self.subTest(suffix):
                (answers / f'{open_one}{suffix}').write_text('{}')
                self.assertEqual(self.adapter.prompt_outcome(self.session(), PromptAnswer(id=open_one)).value['state'], 'submitted')
                (answers / f'{open_one}{suffix}').unlink()
        self.assertEqual(self.adapter.prompt_outcome(self.session(), PromptAnswer(id=str(uuid4()))).value['state'], 'unknown')

    def test_an_outcome_reads_only_this_conversations_transcript(self):
        """The end record names the transcript; another conversation's file, or a file of the same name in another
        Claude storage (a session ID can exist in two homes), must not be read as this call's evidence, and with no
        recorded storage nothing is read at all, never this process's default storage."""
        row = json.dumps({'type': 'attachment', 'attachment': {'type': 'hook_permission_decision', 'decision': 'allow',
                                                               'toolUseID': CALL}}) + '\n'
        another = self.config / 'projects' / 'p' / f'{uuid4()}.jsonl'
        foreign = self.work / 'other-claude-home' / 'projects' / 'p' / f'{self.native}.jsonl'
        foreign.parent.mkdir(parents=True)
        for path in (another, foreign):
            path.write_text(row)
        self.transcript.write_text(row)
        for label, path, session in (('another conversation', another, self.session()), ('another storage', foreign, self.session()),
                                     ('no recorded storage', self.transcript, replace(self.session(), home=None))):
            with self.subTest(label):
                id = self.publish()
                (self.agent_dir / 'claude-prompts' / f'{id}.json').unlink()
                self.end(id, how='sent', choice='allow', transcript={'path': str(path), 'offset': 0}, at=iso(60))
                self.assertEqual(self.adapter.prompt_outcome(session, PromptAnswer(id=id)).value['state'], 'unknown')
        # Its own transcript in its recorded storage does prove it.
        id = self.publish()
        (self.agent_dir / 'claude-prompts' / f'{id}.json').unlink()
        self.end(id, how='sent', choice='allow', at=iso(60))
        self.assertEqual(self.adapter.prompt_outcome(self.session(), PromptAnswer(id=id)).value['state'], 'answered')


if __name__ == '__main__':
    unittest.main()
