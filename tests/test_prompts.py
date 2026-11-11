"""Caller authority, exact prompt admission, private answers and outcomes that never resend."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses, store
from multiverse.harnesses.types import NativeResult, PromptAnswer, PromptChoice, PromptQuestion, WaitingPrompt
from multiverse.plugins import access
from multiverse.runtime import native, prompts
from multiverse.universe import agents
from test_multiverse import BaseTest


class PromptTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.launch, self.thread, self.prompt_id = (str(uuid4()) for _ in range(4))
        self.directory = self.root / 'state/agents' / self.agent
        store.write(self.directory / 'properties.json', {'id': self.agent, 'name': 'worker', 'harness': 'claude',
                    'workspace': str(self.work), 'native_session_id': self.thread})
        store.write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'waiting'})
        self.prompt = WaitingPrompt(id=self.prompt_id, kind='permission', tool='Bash', summary='do something',
                                    detail={'command': 'PRIVATE COMMAND TEXT'}, choices=(
                                        PromptChoice(id='allow', label='Allow'),
                                        PromptChoice(id='always', label='Allow always', saves_rule=True),
                                        PromptChoice(id='deny', label='Deny', takes_message=True)))
        self.catalog, self.coverage = [self.prompt], 'complete'
        self.sent, self.reads, self.receipts = [], [], {}
        self.immediate = None
        owner = self
        class Adapter:
            name = family = 'claude'
            def waiting_prompts(self, session, executor):
                return NativeResult('ok', value={'prompts': owner.catalog, 'coverage': owner.coverage})
            def answer_prompt(self, session, answer, executor):
                written = executor.write_prompt_answer(session, answer)
                owner.sent.append((session, answer))
                if isinstance(owner.immediate, Exception):
                    raise owner.immediate
                return owner.immediate or written
            def prompt_outcome(self, session, answer, executor):
                owner.reads.append((session, answer))
                return owner.receipts.get(answer.id, NativeResult('unknown'))
        self.adapter = Adapter()
        self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(native, 'session_snapshot', side_effect=lambda root, directory, props, facts, **kw: (props, facts)))
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', side_effect=lambda props, facts: {
            'live': 'alive' if facts.get('state') == 'running' else 'stopped'}))
        self.ended = self.enterContext(patch.object(prompts.delivery_evidence, 'launch_ended', return_value=False))
        self.events = self.enterContext(patch.object(agents, '_event'))

    def answer(self, **kwargs):
        return prompts.answer(self.root, self.agent, self.prompt_id, wait=False, **kwargs)

    def fresh(self, **kwargs):
        self.prompt_id = str(uuid4())
        self.prompt = replace(self.prompt, id=self.prompt_id, **kwargs)
        self.catalog = [self.prompt]

    def test_refusals_precede_any_answer_and_focus_does_not_change_authority(self):
        """Permission choices and stale caller identity must be refused before a private answer can exist."""
        plugin = {'kind': 'plugin', 'name': 'helper'}
        for environment in ({'MV2_AGENT_ID': 'an agent'}, {'MV2_LAUNCH_ID': 'stale'}, {'MV2_IDENTITY_MODE': 'conversation'}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                self.assertEqual(prompts.waiting(self.root, self.agent).state, 'refused')
                self.assertEqual(self.answer(choice='allow').cause, 'agent_caller')
                self.assertEqual(prompts.result(self.root, self.agent, self.prompt_id, caller=plugin).cause, 'agent_caller')
        for parameters, cause in (({}, 'not_offered'), ({'choice': 'missing'}, 'not_offered'),
                                  ({'choice': 'allow', 'message': 'secret'}, 'not_offered'),
                                  ({'choice': 'always', 'caller': plugin}, 'person_only')):
            with self.subTest(parameters=parameters):
                self.assertEqual(self.answer(**parameters).cause, cause)
        for name, message, reason in (
                ('long message', 'x' * 70000, 'The message is longer than 64 KiB; shorten it. Nothing was sent.'),
                ('UTF-8 message', 'é' * 32769, 'The message is longer than 64 KiB; shorten it. Nothing was sent.'),
                ('combined answer', 'x' * prompts.LIMIT,
                 'The answer exceeds 64 KiB; shorten it and answer the same prompt again. Nothing was sent.'),
                ('NUL message', 'x\0y', 'Choice and message must be bounded text without NUL'),
                ('long NUL message', 'x' * 70000 + '\0', 'Choice and message must be bounded text without NUL'),
                ('nontext message', 123, 'Choice and message must be bounded text without NUL')):
            with self.subTest(size_refusal=name):
                outcome = self.answer(choice='deny', message=message)
                self.assertEqual((outcome.state, outcome.reason), ('refused', reason))
        store.write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'idle'})
        self.assertEqual(self.answer(choice='allow').cause, 'not_waiting')
        self.assertFalse(self.sent)
        self.assertFalse((self.directory / 'prompts').exists())
        store.write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'waiting'})
        store.write(self.directory / 'focus.json', {'id': str(uuid4()), 'since': store.now(), 'by': 'user'})
        found = prompts.waiting(self.root, self.agent, caller=plugin)
        self.assertEqual([c.id for c in found.prompts[0].choices], ['allow', 'deny'])
        self.assertEqual(self.answer(choice='allow', caller=plugin).state, 'submitted')
        self.assertEqual(len(self.sent), 1)
        self.assertTrue((self.directory / 'focus.json').exists())
        for caller, expected in ((None, {'kind': 'person'}), (plugin, plugin)):
            with self.subTest(answer_caller=expected):
                self.fresh()
                self.assertEqual(self.answer(choice='deny', message='Please change the command', caller=caller).state,
                                 'submitted')
                wire = store.read(self.directory / 'prompts' / (self.prompt_id + '.answer.json'))
                self.assertEqual(wire.get('caller'), expected)
                self.assertIs(wire['allow_rule'], expected['kind'] == 'person')
                self.assertEqual(wire['message'], 'Please change the command')
        self.fresh()
        self.immediate = NativeResult('ok', value={'state': 'answered'})
        self.assertTrue(self.answer(choice='always').saved_rule)
        with access.within('observe', 'reader'), self.assertRaises(access.Denied):
            self.answer(choice='allow')

    def test_question_validation_and_safe_records_do_not_keep_secret_or_free_text(self):
        """Submitted text stays private; a person's reported text must be typed, bounded and from a nonsecret question."""
        self.fresh(kind='question', choices=(), questions=(
            PromptQuestion(id='one', question='Pick one', options=(('A', ''), ('B', '')), free_text=False),
            PromptQuestion(id='many', question='Pick several', options=(('C', ''), ('D', '')), multiple=True, free_text=False),
            PromptQuestion(id='secret', question='Password', secret=True, options=(('PRIVATE SECRET', ''),)),
            PromptQuestion(id='text', question='Some text', required=False)))
        valid = {'one': 'A', 'many': ['C', 'D'], 'secret': 'PRIVATE SECRET', 'text': 'PRIVATE FREE TEXT'}
        for answers in ({}, {**valid, 'unknown': 'PRIVATE SECRET'}, {**valid, 'one': ['A', 'B']},
                        {**valid, 'one': 'PRIVATE SECRET'}, {**valid, 'many': ['C', 'C']},
                        {**valid, 'secret': object()}):
            with self.subTest(answers=list(answers)):
                result = self.answer(answers=answers)
                self.assertEqual(result.state, 'refused')
                self.assertNotIn('PRIVATE', str(result))
        self.assertFalse(self.sent)
        self.assertFalse((self.directory / 'prompts').exists())
        result = self.answer(answers=valid)
        self.assertEqual(result.state, 'submitted')
        record = self.directory / 'prompts' / (self.prompt_id + '.json')
        wire = self.directory / 'prompts' / (self.prompt_id + '.answer.json')
        self.assertEqual(wire.stat().st_mode & 0o777, 0o600)
        answer = store.read(wire)
        self.assertEqual(answer['answers']['secret'], ['PRIVATE SECRET'])
        self.assertEqual((answer['launch_id'], answer['native_id']), (self.launch, self.thread))
        self.assertNotIn('PRIVATE', record.read_text())
        self.assertEqual(store.read(record)['answers'], {'one': ['A'], 'many': ['C', 'D']})
        self.receipts[self.prompt_id] = NativeResult('ok', 'PRIVATE SECRET', {'state': 'person', 'reason': 'PRIVATE SECRET',
                             'person_answer': {'answers': {'one': ['B'], 'secret': ['PRIVATE SECRET'], 'text': ['PERSON TYPED TEXT']},
                                               'message': 'PRIVATE REFUSAL MESSAGE'}})
        result = prompts.result(self.root, self.agent, self.prompt_id)
        self.assertEqual(result.person_answer, {'answers': {'one': ['B']}, 'typed': {'text': ['PERSON TYPED TEXT']}})
        self.assertEqual((result.state, result.cause), ('person', 'person'))
        self.assertEqual(result.reason, 'This prompt was answered in its terminal first; mv2 did not send its answer.')
        self.assertEqual(store.read(record)['result']['reason'], result.reason)
        self.assertNotIn('PRIVATE', str(result))
        self.assertNotIn('PRIVATE', record.read_text())
        for call in self.events.call_args_list:
            self.assertEqual(set(call.args[3]), {'kind', 'tool', 'caller', 'outcome'})
            self.assertNotIn('PRIVATE', json.dumps(call.args[3]))
        self.assertFalse(wire.exists())
        for given in ({'answers': {'pick': 'A'}}, {'choice': 'cancel'},
                      {'choice': 'accept', 'answers': {'pick': 'A'}}):
            with self.subTest(form=given):
                self.fresh(questions=(PromptQuestion(id='pick', question='Pick', options=(('A', ''),), free_text=False),),
                           choices=(PromptChoice(id='accept', label='Accept'), PromptChoice(id='cancel', label='Cancel')))
                self.assertEqual(self.answer(**given).state, 'submitted')
        self.fresh()
        self.assertEqual(self.answer(answers={}).cause, 'missing_answer')

    def test_answer_file_budget_includes_metadata_before_reserving_the_prompt(self):
        """Framing bytes can exceed the reader's limit after a permanent reservation, stranding a correctable answer."""
        for caller in (None, {'kind': 'plugin', 'name': 'helper'}):
            for field in ('answers', 'notes'):
                with self.subTest(caller=caller, field=field):
                    self.fresh(kind='question', choices=(), questions=(
                        PromptQuestion(id='secret', question='Value?', secret=True, takes_note=True),))
                    who = caller or {'kind': 'person'}
                    prefix = 'PRIVATE_ENVELOPE_VALUE_é"\\'
                    def payload(text):
                        values = {'secret': (text,)} if field == 'answers' else {}
                        notes = {'secret': text} if field == 'notes' else {}
                        request = PromptAnswer(id=self.prompt_id, answers=values, notes=notes,
                                               allow_rule=caller is None)
                        return {'schema': 1, **asdict(request), 'launch_id': self.launch,
                                'native_id': self.thread, 'caller': who}
                    overhead = len(json.dumps(payload(prefix), ensure_ascii=False).encode())
                    text = prefix + 'x' * (prompts.LIMIT + 1 - overhead)
                    before = len(self.sent), self.events.call_count
                    refused = self.answer(**{field: {'secret': text}}, caller=caller)
                    self.assertEqual(refused.state, 'refused')
                    self.assertIn('64 KiB', refused.reason)
                    self.assertNotIn('PRIVATE_ENVELOPE_VALUE', str(refused))
                    self.assertEqual((len(self.sent), self.events.call_count), before)
                    record = self.directory / 'prompts' / (self.prompt_id + '.json')
                    wire = record.with_name(self.prompt_id + '.answer.json')
                    self.assertFalse(record.exists())
                    self.assertFalse(wire.exists())
                    accepted = self.answer(**{field: {'secret': text[:-1]}}, caller=caller)
                    self.assertEqual(accepted.state, 'submitted', accepted)
                    self.assertEqual(wire.stat().st_size, prompts.LIMIT)
                    self.assertTrue(json.loads(wire.read_bytes()) == json.loads(json.dumps(payload(text[:-1]))),
                                    'The published answer must equal the complete admitted envelope')
                    self.assertNotIn('PRIVATE_ENVELOPE_VALUE', record.read_text())
                    self.assertEqual(len(self.sent), before[0] + 1)

    def test_one_submission_timeout_and_every_outcome_remain_read_only(self):
        """Timeouts, duplicate callers, late receipts and a changed conversation must never replay an answer."""
        for state in ('answered', 'sent_first', 'person', 'gone', 'unknown', 'refused'):
            with self.subTest(state=state):
                self.fresh()
                submitted = prompts.answer(self.root, self.agent, self.prompt_id, choice='allow', timeout=0)
                self.assertEqual(submitted.state, 'submitted')
                before = len(self.sent)
                wire = self.directory / 'prompts' / (self.prompt_id + '.answer.json')
                original = wire.read_bytes()
                self.assertEqual(self.answer(choice='deny').cause, 'already_answered')
                self.assertEqual(wire.read_bytes(), original)
                self.receipts[self.prompt_id] = NativeResult('ok', value={'state': state, 'cause': 'turn_ended' if state == 'gone' else None,
                                                     'reason': 'The native effect is unconfirmed' if state == 'sent_first' else None,
                                                     'person_answer': {'choice': 'deny'}})
                reads = None
                for _ in range(2):
                    result = prompts.result(self.root, self.agent, self.prompt_id)
                    self.assertEqual(result.state, state)
                    if state != 'answered':
                        self.assertTrue(result.reason)
                    if state == 'sent_first':
                        self.assertEqual(result.reason, 'The native effect is unconfirmed')
                    if reads is not None and state in prompts.FINAL - {'sent_first'}:
                        self.assertEqual(len(self.reads), reads)
                    reads = len(self.reads)
                self.assertEqual(len(self.sent), before)
        self.fresh()
        self.immediate = NativeResult('ok', value={'state': 'sent_first'})
        self.assertEqual(self.answer(choice='allow').state, 'sent_first')
        before = len(self.sent)
        for late in (NativeResult('unknown'), NativeResult('ok', value={'state': 'submitted'}),
                     NativeResult('ok', value={'state': 'unknown'}),
                     NativeResult('ok', value={'state': 'unknown', 'cause': 'unconfirmed'})):
            self.receipts[self.prompt_id] = late
            self.assertEqual(prompts.result(self.root, self.agent, self.prompt_id).state, 'sent_first')
        self.receipts[self.prompt_id] = NativeResult('ok', value={'state': 'unknown', 'cause': 'contradicted',
                                                     'reason': 'A later native effect contradicts this answer.'})
        contradicted = prompts.result(self.root, self.agent, self.prompt_id)
        self.assertEqual((contradicted.state, contradicted.cause), ('unknown', 'contradicted'))
        self.assertEqual(self.answer(choice='allow').cause, 'already_answered')
        self.assertEqual(len(self.sent), before)
        for cause in ('interrupted', 'turn_ended', 'switched', 'connection_lost'):
            for schema in ('public', 'secret', 'missing'):
                with self.subTest(observed_cancellation=cause, schema=schema):
                    self.fresh(questions=(PromptQuestion(id='secret', question='Password', secret=True, required=False),)
                               if schema == 'secret' else ())
                    self.assertEqual(self.answer(choice='allow').state, 'sent_first')
                    before = len(self.sent)
                    path = self.directory / 'prompts' / (self.prompt_id + '.json')
                    if schema == 'missing':
                        record = store.read(path)
                        record.pop('has_secret')  # An older record has no trustworthy secrecy marker.
                        store.write(path, record)
                    self.receipts[self.prompt_id] = NativeResult('ok', value={
                        'state': 'unknown', 'cause': cause,
                        'reason': 'An observed cancellation leaves acceptance unknown.' if schema == 'public'
                                  else 'PRIVATE_NATIVE_REASON_MUST_NOT_ESCAPE'})
                    settled = prompts.result(self.root, self.agent, self.prompt_id)
                    self.assertEqual((settled.state, settled.cause), ('unknown', cause))
                    if schema != 'public':
                        self.assertNotIn('PRIVATE_NATIVE_REASON', str(settled))
                        self.assertNotIn('PRIVATE_NATIVE_REASON', path.read_text())
                        self.assertNotIn('PRIVATE_NATIVE_REASON', str(self.events.call_args_list))
                        self.assertNotIn('before this answer took', settled.reason)
                        for word in ('unconfirmed', 'may have', 'do not send'):
                            self.assertIn(word, settled.reason)
                        observed = {'interrupted': 'interruption', 'turn_ended': 'turn',
                                    'switched': 'conversation', 'connection_lost': 'lost'}
                        self.assertIn(observed[cause], settled.reason.lower())
                    self.assertEqual(self.answer(choice='allow').cause, 'already_answered')
                    self.assertEqual(len(self.sent), before)
        self.immediate = None
        for cause in ('switched', 'stopped'):
            for taken in (False, 'unlinked', 'renamed'):
                with self.subTest(end=cause, taken=taken):
                    store.write(self.directory / 'properties.json', {**store.read(self.directory / 'properties.json'),
                                'native_session_id': self.thread})
                    self.ended.return_value = False
                    self.fresh()
                    self.answer(choice='allow')
                    wire = self.directory / 'prompts' / (self.prompt_id + '.answer.json')
                    claimed = wire.with_name(self.prompt_id + '.answer.taken.json')
                    if taken == 'renamed':
                        wire.rename(claimed)  # A native process can die before cleaning its claimed secret file.
                    elif taken:
                        wire.unlink()  # The in-session part claimed it; absence alone cannot prove what took.
                    if cause == 'switched':
                        store.write(self.directory / 'properties.json', {**store.read(self.directory / 'properties.json'),
                                    'native_session_id': str(uuid4())})
                    else:
                        self.ended.return_value = True
                    result = prompts.result(self.root, self.agent, self.prompt_id)
                    self.assertEqual((result.state, result.cause), ('unknown' if taken else 'gone', cause))
                    self.assertEqual(self.reads[-1][0].native_id, self.thread)
                    self.assertEqual(self.reads[-1][1], PromptAnswer(id=self.prompt_id))
                    self.assertFalse(wire.exists())
                    self.assertFalse(claimed.exists())
        self.fresh()
        self.ended.return_value = False
        self.answer(choice='allow')
        self.ended.return_value = True
        unlink = os.unlink
        raced = []
        def native_takes_first(path, **kwargs):
            if path == self.prompt_id + '.answer.json' and not raced:
                raced.append(True)
                unlink(path, **kwargs)
            return unlink(path, **kwargs)
        with patch.object(prompts.os, 'unlink', side_effect=native_takes_first):
            result = prompts.result(self.root, self.agent, self.prompt_id)
        self.assertTrue(raced)
        self.assertEqual(result.state, 'unknown')

    def test_question_notes_are_offered_separate_private_values(self):
        """Notes beside fixed choices must not widen answers or leak secret notes into records or person-first receipts."""
        self.fresh(kind='question', choices=(), questions=(
            PromptQuestion(id='fixed', question='Pick', options=(('A', ''), ('B', '')), free_text=False, takes_note=True),
            PromptQuestion(id='secret', question='Secret pick', options=(('Y', ''),), free_text=False,
                           secret=True, takes_note=True),
            PromptQuestion(id='free', question='Draft', takes_note=True),
            PromptQuestion(id='plain', question='No note', required=False),
            PromptQuestion(id='other', question='Other', options=(('A', ''), ('None of the above', '')),
                           takes_note=True, required=False)))
        answers = {'fixed': 'A', 'secret': 'Y'}
        notes = {'fixed': '  NONSECRET NOTE  ', 'secret': 'PRIVATE NOTE', 'free': 'FREE NOTE'}
        for given, added in ((answers, {**notes, 'missing': 'text'}), (answers, {**notes, 'plain': 'text'}),
                             (answers, {**notes, 'fixed': 'bad\0note'}), (answers, {**notes, 'fixed': object()}),
                             (answers, {**notes, 'fixed': 'x' * prompts.LIMIT}),
                             (answers, {**notes, 'free': '  '}),
                             ({'secret': 'Y'}, notes),
                             ({**answers, 'free': 'another draft'}, notes),
                             ({**answers, 'other': 'custom'}, {**notes, 'other': 'another draft'})):
            with self.subTest(answers=tuple(given), notes=tuple(added)):
                result = self.answer(answers=given, notes=added)
                self.assertEqual(result.state, 'refused')
                self.assertNotIn('PRIVATE', str(result))
        self.assertFalse(self.sent)
        self.assertFalse((self.directory / 'prompts').exists())
        self.assertEqual(self.answer(answers={**answers, 'other': 'None of the above'},
                                     notes={**notes, 'other': '  another idea  '}).state, 'submitted')
        request = self.sent[-1][1]
        self.assertEqual(dict(request.notes), {**notes, 'fixed': 'NONSECRET NOTE', 'other': 'another idea'})
        wire = self.directory / 'prompts' / (self.prompt_id + '.answer.json')
        self.assertEqual(store.read(wire)['notes']['secret'], 'PRIVATE NOTE')
        record = self.directory / 'prompts' / (self.prompt_id + '.json')
        self.assertEqual(set(store.read(record)['question_ids']), {'fixed', 'secret', 'free', 'other'})
        for text in ('NONSECRET NOTE', 'PRIVATE NOTE', 'FREE NOTE', 'another idea'):
            self.assertNotIn(text, record.read_text())
        self.receipts[self.prompt_id] = NativeResult('ok', value={'state': 'person', 'person_answer': {
            'answers': {'fixed': ['B']}, 'notes': {'fixed': 'B', 'free': 'PERSON TYPED', 'secret': 'PRIVATE PERSON NOTE',
                                                 'plain': 'UNOFFered NOTE'}, 'message': 'PRIVATE MESSAGE'}})
        result = prompts.result(self.root, self.agent, self.prompt_id)
        self.assertEqual(result.person_answer, {'answers': {'fixed': ['B']},
                                              'typed': {'fixed': ['B'], 'free': ['PERSON TYPED']}})
        self.assertNotIn('PRIVATE', record.read_text())
        self.assertFalse(wire.exists())
        for call in self.events.call_args_list:
            self.assertEqual(set(call.args[3]), {'kind', 'tool', 'caller', 'outcome'})
        old = self.prompt_id
        schema = {'choices': [], 'questions': [asdict(q) for q in self.prompt.questions]}
        self.fresh()
        self.receipts[old] = NativeResult('ok', value={'state': 'person', **schema, 'person_answer': {
            'answers': {'fixed': ['A']}, 'notes': {'fixed': 'x' * prompts.LIMIT, 'secret': 'PRIVATE NOTE'}}})
        # A late answer with no core attempt still gets the original offered-note schema.
        unattempted = str(uuid4())
        self.receipts[unattempted] = self.receipts[old]
        result = prompts.answer(self.root, self.agent, unattempted, answers=answers, notes=notes, wait=False)
        self.assertEqual(result.person_answer, {'answers': {'fixed': ['A']}, 'truncated': True})
        self.fresh(choices=(PromptChoice(id='cancel', label='Cancel'),))
        self.assertEqual(self.answer(choice='cancel', notes={'fixed': 'text'}).state, 'refused')
        self.fresh()
        self.assertEqual(self.answer(answers={**answers, 'free': 'one draft'}, notes={'fixed': ' \n '}).state, 'submitted')
        self.assertEqual(dict(self.sent[-1][1].notes), {})
        self.fresh(choices=(PromptChoice(id='submit', label='Submit'), PromptChoice(id='cancel', label='Cancel')),
                   questions=(PromptQuestion(id='pick', question='Pick', options=(('A', ''),),
                                             free_text=False, takes_note=True, required=False),))
        for values in ({}, {'pick': []}):
            with self.subTest(choice_without_answers=values):
                self.assertEqual(self.answer(choice='cancel', answers=values, notes={'pick': 'No answer'}).state,
                                 'refused')
        self.assertEqual(self.answer(choice='submit', answers={'pick': 'A'}, notes={'pick': 'Why A fits'}).state,
                         'submitted')
        request = self.sent[-1][1]
        self.assertEqual((request.choice, dict(request.answers), dict(request.notes)),
                         ('submit', {'pick': ('A',)}, {'pick': 'Why A fits'}))

    def test_unanswered_result_reads_closure_without_creating_or_consuming_an_answer(self):
        """A missing core attempt must still explain closure, without sending, repairing state or hiding damaged records."""
        for name in ('.launch.lock', '.delivery.lock', '.prompts.lock'):
            with store.lock(self.directory / name):
                pass
        before = {p.relative_to(self.directory): p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        def result():
            try:
                return prompts.result(self.root, self.agent, self.prompt_id)
            except ValueError as error:
                self.fail(f'An unattempted prompt should have a readable outcome: {error}')
        with patch.object(native, 'session_snapshot', side_effect=AssertionError('A result read must not repair state')), \
                patch.object(prompts, '_exclusive', side_effect=AssertionError('A result read must not reserve an answer')), \
                patch.object(prompts, '_save', side_effect=AssertionError('No attempt exists to save')), \
                patch.object(prompts, '_discard_answer', side_effect=AssertionError('No answer may be consumed')):
            found = result()
            self.assertEqual((found.state, found.cause), ('refused', 'not_answered'))
            self.assertIn('still open', found.reason)
            self.assertIn(f'mv2 answer worker {self.prompt_id}', found.reason)
            with self.subTest('open prompt before assumed submission'), patch.object(
                    self.adapter, 'prompt_outcome', return_value=NativeResult('ok', value={'state': 'submitted'})) as read:
                found = result()
                self.assertEqual((found.state, found.cause), ('refused', 'not_answered'))
                read.assert_not_called()
            self.catalog = []
            schema = {'choices': [], 'questions': [asdict(PromptQuestion(id='pick', question='Pick', options=(('A', ''),))),
                      asdict(PromptQuestion(id='secret', question='Secret', secret=True))]}
            self.receipts[self.prompt_id] = NativeResult('ok', value={'state': 'person', **schema,
                'person_answer': {'answers': {'pick': ['A'], 'secret': ['PRIVATE SECRET']}}})
            found = result()
            self.assertEqual((found.state, found.person_answer), ('person', {'answers': {'pick': ['A']}}))
            self.assertIn('Nothing was sent', found.reason)
            self.assertNotIn('PRIVATE', str(found))
            for state, cause in (('gone', 'interrupted'), ('refused', 'person')):
                self.receipts[self.prompt_id] = NativeResult('ok', value={'state': state, 'cause': cause})
                found = result()
                self.assertEqual((found.state, found.cause), (state, cause))
                self.assertIn('nothing was sent', found.reason.lower())
                if cause == 'person':
                    self.assertIn('person refused', found.reason)
            for cause in (None, 'closed', 'unclassified'):
                with self.subTest(unproven_closure=cause):
                    self.receipts[self.prompt_id] = NativeResult('ok', value={
                        'state': 'gone', 'cause': cause, 'choices': [], 'questions': [],
                        'reason': 'Most likely answered in the terminal before the answer reached it.'})
                    found = result()
                    self.assertEqual(found.reason, 'That prompt is no longer open. Nothing was sent.')
            self.receipts[self.prompt_id] = NativeResult('ok', value={
                'state': 'gone', 'cause': 'stopped', 'choices': [], 'questions': [],
                'reason': 'Its original launch stopped before this prompt was answered.'})
            stopped_reason = 'The agent stopped while this prompt was open; mv2 sent no answer to it.'
            with self.subTest('no answer to stopped prompt'):
                self.assertEqual(result().reason, stopped_reason)
            for state in ('answered', 'sent_first', 'submitted'):
                self.receipts[self.prompt_id] = NativeResult('ok', value={'state': state})
                self.assertEqual((result().state, result().cause), ('refused', 'unconfirmed'))
            self.receipts.clear()
            self.assertEqual((result().state, result().cause), ('gone', 'closed'))
            self.assertIn(f'No prompt with ID {self.prompt_id} is known for this agent', result().reason)
            replacement = replace(self.prompt, id=str(uuid4()))
            self.catalog = [replacement]
            found = result()
            self.assertEqual((found.state, found.cause), ('gone', 'replaced'))
            self.assertIn(replacement.id, found.reason)
            self.assertIn(f'No prompt with ID {self.prompt_id} is known for this agent', found.reason)
            self.catalog, self.coverage = [], 'unknown'
            self.assertEqual((result().state, result().cause), ('refused', 'unconfirmed'))
            connected = prompts._connect(self.root, self.directory, reconcile=False)
            for status in ('waiting', 'idle'):
                with self.subTest(unknown_id_status=status), patch.object(prompts, '_connect', return_value=(
                        connected[0], replace(connected[1], status=status), connected[2])):
                    self.assertIn(f'No prompt with ID {self.prompt_id} is known for this agent', result().reason)
            for name in ('.launch.lock', '.prompts.lock'):
                with store.lock(self.directory / name):
                    self.assertEqual((result().state, result().cause), ('refused', 'busy'))
            with patch.object(prompts, '_connect', return_value=None):
                unknown = result()
                self.assertEqual((unknown.state, unknown.cause), ('gone', 'stopped'))
                with self.subTest('unknown prompt on stopped agent'):
                    self.assertEqual(unknown.reason, f'The agent is not running, and no prompt with ID '
                                                    f'{self.prompt_id} is known for it. Nothing was sent.')
                    self.assertEqual(self.answer(choice='allow').reason, unknown.reason)
                for receipt in ({'state': 'unknown', 'cause': 'open'},
                                {'state': 'gone', 'cause': 'stopped'},
                                {'state': 'unknown', 'choices': [], 'questions': []}):
                    with self.subTest(stopped_known_prompt=receipt):
                        self.receipts[self.prompt_id] = NativeResult('ok', value=receipt)
                        self.assertEqual(result().reason, stopped_reason if receipt.get('cause') in ('open', 'stopped')
                                         else 'The agent is not running. Nothing was sent.')
        self.receipts[self.prompt_id] = NativeResult('ok', value={'state': 'gone', 'cause': 'stopped'})
        self.assertEqual(self.answer(choice='allow').reason, stopped_reason)
        after = {p.relative_to(self.directory): p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.sent)
        self.events.assert_not_called()
        # Present but unreadable is not the same as absent, even under contention.
        path = self.directory / 'prompts' / (self.prompt_id + '.json')
        path.parent.mkdir()
        for damaged in ('[]', '{'):
            path.write_text(damaged)
            with self.assertRaises(ValueError):
                prompts.result(self.root, self.agent, self.prompt_id)
        path.unlink()
        path.symlink_to(self.directory / 'missing-record')
        with self.assertRaises(ValueError):
            prompts.result(self.root, self.agent, self.prompt_id)

    def test_closed_a_cannot_become_an_answer_to_b_even_when_waiting_again(self):
        """A fresh waiting status cannot retarget a delayed answer from the person's closed prompt A to prompt B."""
        old = prompts.waiting(self.root, self.agent).prompts[0]
        self.fresh()
        outcome = prompts.answer(self.root, self.agent, old.id, choice='allow', wait=False)
        self.assertEqual((outcome.state, outcome.cause), ('gone', 'replaced'))
        self.assertIn(self.prompt_id, outcome.reason)
        self.assertFalse(self.sent)
        self.assertFalse((self.directory / 'prompts').exists())
        self.receipts[old.id] = NativeResult('ok', value={'state': 'person', 'person_answer': {'choice': 'deny'}})
        outcome = prompts.answer(self.root, self.agent, old.id, choice='allow', wait=False)
        self.assertEqual((outcome.state, outcome.person_answer), ('person', {'choice': 'deny'}))
        self.assertFalse(self.sent)
        self.receipts[old.id] = NativeResult('ok', value={'state': 'person', 'choices': [], 'questions': [
            asdict(PromptQuestion(id='pick', question='Pick one', options=(('A', ''), ('B', '')))),
            asdict(PromptQuestion(id='secret', question='Secret', secret=True, options=(('PRIVATE SECRET', ''),))),
            asdict(PromptQuestion(id='text', question='Type text'))],
            'person_answer': {'answers': {'pick': ['B'], 'secret': ['PRIVATE SECRET'], 'text': ['PERSON TYPED TEXT']}}})
        outcome = prompts.answer(self.root, self.agent, old.id, answers={'pick': 'A'}, wait=False)
        self.assertEqual(outcome.person_answer, {'answers': {'pick': ['B']}, 'typed': {'text': ['PERSON TYPED TEXT']}})
        self.assertNotIn('PRIVATE', str(outcome))
        self.assertFalse(self.sent)
        self.assertFalse((self.directory / 'prompts').exists())
        self.receipts[old.id].value['person_answer']['answers']['text'] = ['x' * prompts.LIMIT]
        outcome = prompts.answer(self.root, self.agent, old.id, answers={'pick': 'A'}, wait=False)
        self.assertTrue(outcome.person_answer['truncated'])
        self.assertLessEqual(len(json.dumps(outcome.person_answer).encode()), prompts.LIMIT)
        self.assertEqual(outcome.person_answer.get('answers'), {'pick': ['B']})
        # An incomplete or malformed closed schema cannot authorize exposing a person's answers.
        for fields in ({'questions': self.receipts[old.id].value['questions']},
                       {'choices': [], 'questions': [{'id': 'pick'}]}):
            self.receipts[old.id] = NativeResult('ok', value={'state': 'person', **fields,
                                                       'person_answer': {'answers': {'pick': ['B']}}})
            self.assertIsNone(prompts.answer(self.root, self.agent, old.id, choice='allow', wait=False).person_answer)
        self.catalog, self.coverage = [], 'unknown'
        unknown = self.answer(choice='allow')
        self.assertEqual((unknown.state, unknown.cause), ('refused', 'unconfirmed'))
        self.assertIn('may have', unknown.reason)
        self.assertIn('Nothing was sent', unknown.reason)

    def test_exclusive_publication_and_uncertain_submission_fail_closed(self):
        """Filesystem races and an adapter failing after writing must not overwrite an answer or claim nothing was sent."""
        for name in ('.launch.lock', '.prompts.lock'):
            with self.subTest(lock=name), store.lock(self.directory / name):
                self.assertEqual(self.answer(choice='allow').cause, 'busy')
                try:
                    outcome = prompts.result(self.root, self.agent, self.prompt_id)
                except ValueError as error:
                    self.fail(f'A busy unattempted prompt needs a qualified result: {error}')
                self.assertEqual(outcome.cause, 'busy')
        self.assertFalse(self.sent)
        outside = self.work / 'outside'
        outside.mkdir()
        (self.directory / 'prompts').symlink_to(outside)
        self.assertEqual(self.answer(choice='allow').cause, 'storage')
        self.assertFalse(list(outside.iterdir()))
        (self.directory / 'prompts').unlink()
        for value in (RuntimeError('PRIVATE INPUT'), NativeResult('error', 'PRIVATE INPUT')):
            with self.subTest(failure=type(value).__name__):
                self.fresh()
                self.immediate = value
                outcome = self.answer(choice='allow')
                self.assertEqual(outcome.state, 'unknown')
                self.assertNotIn('PRIVATE', str(outcome))
                self.assertNotIn('nothing was sent', outcome.reason.lower())
                self.assertEqual(self.answer(choice='allow').cause, 'already_answered')
                with store.lock(self.directory / '.launch.lock'):
                    self.assertEqual(prompts.result(self.root, self.agent, self.prompt_id).state, 'unknown')
        session, answer = self.sent[-1]
        answer = replace(answer, id=str(uuid4()))
        executor = native.NativeExecutor(str(self.work), store.read(self.directory / 'properties.json'),
                                         store.read(self.directory / 'runtime.json'))
        with self.assertRaisesRegex(ValueError, 'No matching prompt answer'):
            executor.write_prompt_answer(session, answer)
        self.assertFalse((self.directory / 'prompts' / (answer.id + '.answer.json')).exists())
        self.fresh()
        wire = self.directory / 'prompts' / (self.prompt_id + '.answer.json')
        failed = []
        fsync = os.fsync
        def fail_after_publish(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and wire.exists() and not failed:
                failed.append(True)
                raise OSError('PRIVATE FSYNC ERROR')
            return fsync(fd)
        def adapter_handles_write_error(session, answer, executor):
            try:
                return executor.write_prompt_answer(session, answer)
            except OSError:
                return NativeResult('error', 'PRIVATE FSYNC ERROR')
        with patch.object(prompts.os, 'fsync', side_effect=fail_after_publish), \
                patch.object(self.adapter, 'answer_prompt', side_effect=adapter_handles_write_error):
            outcome = self.answer(choice='allow')
        self.assertTrue(failed)
        self.assertEqual(outcome.state, 'unknown')
        self.assertNotIn('PRIVATE', str(outcome))
        self.assertTrue(wire.exists())
        self.assertEqual(self.answer(choice='allow').cause, 'already_answered')

    def test_wait_reads_until_a_receipt_and_never_holds_a_lock_during_sleep(self):
        """A bounded answer wait must allow the native writer and another reader to progress without submitting again."""
        ticks = [0]
        def sleep(seconds):
            with store.lock(self.directory / '.launch.lock', blocking=False), \
                    store.lock(self.directory / '.prompts.lock', blocking=False):
                self.receipts[self.prompt_id] = NativeResult('ok', value={'state': 'answered'})
            ticks[0] += seconds
        with patch.object(prompts, 'time', SimpleNamespace(monotonic=lambda: ticks[0], sleep=sleep)):
            result = prompts.answer(self.root, self.agent, self.prompt_id, choice='allow', timeout=1)
        self.assertEqual(result.state, 'answered')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(ticks, [prompts.POLL])
