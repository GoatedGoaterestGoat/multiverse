"""Offline native async-question identity, ordinary-input recipes and relay lifetime."""
import asyncio
from dataclasses import replace
import errno
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from websockets.exceptions import ConnectionClosed

from multiverse.harnesses.codex import prompt_codec as codec, prompt_files, prompts
from multiverse.harnesses.types import PromptAnswer, PromptInput
import test_codex_prompts as fixtures


ASYNC = 'request_user_input_async'
START = '<send_user_message_question_reply>'
END = '</send_user_message_question_reply>'


def native_id(item, index):
    return json.dumps([ASYNC, item, index], ensure_ascii=False, separators=(',', ':'))


def wrapper(item, index, title, value):
    return START + '\n' + json.dumps([{'answer': value, 'question': title,
        'questionItemId': native_id(item, index)}], ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n' + END


class AsyncQuestionTest(unittest.TestCase):
    setUp = fixtures.PromptRelayTest.setUp
    result = fixtures.PromptRelayTest.result

    def flush(self):
        jobs, self.relay.jobs = self.relay.jobs, prompts.Jobs()
        self.relay.applied(self.relay.files.apply(jobs))

    def question_event(self, item='async-one', questions=None, *, target=None, method='item/completed'):
        self.relay.observe('incoming', {'method': method, 'params': {
            'threadId': target or self.native, 'turnId': 'turn-one', 'item': {
                'type': 'agentMessage', 'id': item, 'text': 'Questions', 'delivery': 'async',
                'questions': questions if questions is not None else [{'title': 'Which?', 'options': ['One', 'Two']}]}}})
        self.flush()
        return prompts.catalog(self.session).value['prompts']

    def test_native_question_encoding_keeps_identity_options_and_the_exact_three_field_envelope(self):
        """A normal input without the exact native envelope loses the question identity or changes its answer."""
        params = {'threadId': self.native, 'turnId': 'turn-one', 'itemId': 'async-one',
                  'questionIndex': 1, 'question': {'title': 'Which?', 'options': ['One', 'Two']}}
        offer = codec.describe(ASYNC, params)
        self.assertIsNotNone(offer, 'Native async questions need their own codec, not the callback codec')
        self.assertTrue(offer.answerable)
        public = offer.public('opaque')
        self.assertFalse(public['requires_waiting'])
        self.assertEqual(public['tool'], ASYNC)
        self.assertFalse(public['questions'][0]['secret'])
        self.assertFalse(public['questions'][0]['multiple'])
        self.assertFalse(public['questions'][0]['takes_note'])
        for value in ('One', 'Typed answer', 'quotes " and \\ and \n unicode ☃'):
            self.assertEqual(offer.response(fixtures.answer(answers={'answer': [value]})),
                             wrapper('async-one', 1, 'Which?', value))
        for invalid in (fixtures.answer('allow'), fixtures.answer(answers={'answer': ['One', 'Two']}),
                        fixtures.answer(answers={'other': ['One']}), fixtures.answer(answers={'answer': ['']}),
                        fixtures.answer(answers={'answer': ['One']}, notes=[]),
                        fixtures.answer(answers={'answer': ['One']}, notes={'answer': 'note'})):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                offer.response(invalid)
        for changes in ({'questionIndex': True}, {'questionIndex': -1}, {'itemId': 'x' * 512},
                {'question': {'title': 'Which?', 'options': [str(i) for i in range(33)]}},
                {'question': {'title': 'Which?', 'options': ['x' * 513]}},
                {'question': {'title': 'Which?', 'options': [True]}},
                {'question': {'title': 'Which?', 'options': None, 'isSecret': True}}):
            with self.subTest(changes=changes):
                self.assertFalse(codec.describe(ASYNC, {**params, **changes}).answerable)
        with self.assertRaises(ValueError):
            offer.response(fixtures.answer(answers={'answer': ['x' * (128 << 10)]}))
        title = 'é' * 255 + '☃\nnext'
        unicode_offer = codec.describe(ASYNC, {**params, 'question': {'title': title, 'options': None}})
        self.assertEqual(unicode_offer.response(fixtures.answer(answers={'answer': ['One']})),
                         wrapper('async-one', 1, 'é' * 255, 'One'))

    def test_live_questions_survive_turn_end_and_close_only_the_exact_known_structured_reply(self):
        """Local skip and turn end cannot silently expire a question mv2 is allowed to answer later."""
        questions = [{'title': 'Same title', 'options': None} for _ in range(3)]
        rows = self.question_event(questions=questions)
        self.assertEqual(len(rows), 3, 'Live AgentMessage questions must be discovered')
        ids = [row['id'] for row in rows]
        self.assertTrue(all(row['requires_waiting'] is False for row in rows))
        self.question_event(questions=questions)
        self.assertEqual([r['id'] for r in prompts.catalog(self.session).value['prompts']], ids)
        self.relay.observe('incoming', {'method': 'turn/completed', 'params': {
            'threadId': self.native, 'turn': {'id': 'turn-one', 'status': 'completed'}}})
        self.flush()
        self.assertEqual(len(prompts.catalog(self.session).value['prompts']), 3)
        for text, target in [('Message from person:\n' + wrapper('async-one', 0, 'Same title', 'first'), self.native),
                             (wrapper('async-one', 0, 'Same title', 'first'), 'foreign')]:
            self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': target,
                'turnId': 'later-turn', 'item': {'type': 'userMessage', 'id': 'reply', 'content': [
                    {'type': 'text', 'text': text}]}}})
        self.flush()
        self.assertEqual(len(prompts.catalog(self.session).value['prompts']), 3)
        self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
            'turnId': 'later-turn', 'item': {'type': 'userMessage', 'id': 'reply', 'clientId': 'prompt-unrelated', 'content': [
                {'type': 'text', 'text': wrapper('async-one', 0, 'Same title', 'first')}]}}})
        self.flush()
        self.assertEqual([r['id'] for r in prompts.catalog(self.session).value['prompts']], ids[1:])
        duplicate = wrapper('async-one', 1, 'Same title', 'first').replace(
            '"answer":"first"', '"answer":"first","answer":"second"', 1)
        self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
            'turnId': 'later', 'item': {'type': 'userMessage', 'id': 'duplicate', 'content': [
                {'type': 'text', 'text': duplicate}]}}})
        self.flush()
        self.assertEqual([r['id'] for r in prompts.catalog(self.session).value['prompts']], ids[1:])
        self.assertEqual(self.result(ids[0])['state'], 'person')
        self.question_event(questions=questions)
        self.assertEqual([r['id'] for r in prompts.catalog(self.session).value['prompts']], ids[1:])
        # A bare legacy whole-message ID must not close any indexed question.
        legacy = START + json.dumps({'answer': 'wrong', 'question': 'Same title',
                                     'questionItemId': 'async-one'}) + END
        self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
            'turnId': 'later', 'item': {'type': 'userMessage', 'id': 'legacy', 'content': [
                {'type': 'text', 'text': legacy}]}}})
        self.flush()
        self.assertEqual([r['id'] for r in prompts.catalog(self.session).value['prompts']], ids[1:])
        for index in (1, 2):
            reply = {'answer': 'external answer', 'question': 'Same title',
                     'questionItemId': native_id('async-one', index), 'extra': True}
            raw = json.dumps(reply, ensure_ascii=False, separators=(',', ':'))
            if index == 2:
                # Native ignores unknown object values, including duplicate keys within them.
                raw = raw[:-1] + ',"metadata":{"answer":"ignored one","answer":"ignored two"}}'
            text = START + raw + END
            self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
                'turnId': 'later', 'item': {'type': 'userMessage', 'id': 'extra-' + str(index),
                    'clientId': 'another-client', 'content': [{'type': 'text', 'text': text}]}}})
            self.flush()
            self.assertEqual([r['id'] for r in prompts.catalog(self.session).value['prompts']], ids[index + 1:])
            self.assertEqual(self.result(ids[index])['state'], 'person')
            self.assertFalse('ignored one' in repr(self.relay.records))
            for path in self.directory.rglob('*.json'):
                self.assertFalse('ignored one' in path.read_text())
        self.question_event(questions=questions)
        self.assertEqual(prompts.catalog(self.session).value['prompts'], [])

    def test_terminal_input_closes_the_exact_question_before_codex_echoes_it(self):
        """A terminal answer queued behind a long tool call must prevent a second mv2 answer."""
        self.connection.side_id = 'side'
        for method in ('turn/steer', 'turn/start'):
            with self.subTest(method=method):
                item = 'terminal-' + method
                rows = self.question_event(item=item, questions=[{'title': 'Same?', 'options': None}] * 2)
                ids = [r['id'] for r in rows[-2:]]
                self.assertEqual(len(ids), 2)
                text = wrapper(item, 0, 'Same?', 'Terminal answer')
                frame = {'id': 'input-' + method, 'method': method, 'params': {
                    'threadId': self.native, 'clientUserMessageId': 'terminal-client',
                    'input': [{'type': 'text', 'text': text, 'text_elements': []}]}}
                for target, body, client in ((self.native, 'ordinary prose ' + text, 'terminal-client'),
                        ('foreign', text, 'terminal-client'), ('side', text, 'terminal-client'),
                        (self.native, text, 'prompt-' + ids[0])):
                    ignored = {**frame, 'params': {**frame['params'],
                        'threadId': target, 'clientUserMessageId': client,
                        'input': [{'type': 'text', 'text': body}]}}
                    self.relay.observe('outgoing', ignored)
                    asyncio.run(self.relay.before_send(ignored))
                    self.flush()
                    self.assertTrue(self.relay.records[ids[0]]['open'])
                self.relay.observe('outgoing', frame)
                asyncio.run(self.relay.before_send(frame))
                self.relay.observe('incoming', {'id': frame['id'], 'result': {'turn': {'id': 'next-turn'}}})
                self.flush()
                self.assertFalse(self.relay.records[ids[0]]['open'])
                self.assertTrue(self.relay.records[ids[1]]['open'])
                self.assertNotIn(ids[0], [r['id'] for r in prompts.catalog(self.session).value['prompts']])
                self.assertEqual(self.result(ids[0])['state'], 'person')
                self.assertEqual(self.result(ids[0])['person_answer'],
                                 {'answers': {'answer': ['Terminal answer']}})
                self.assertEqual(self.relay.files.input_claim(ids[0], self.native).disposition, 'person')
                attempted = prompts.submit(replace(self.session, status='working'),
                    PromptAnswer(id=ids[0], answers={'answer': ('Second answer',)}),
                    SimpleNamespace(check_session=lambda: None))
                self.assertNotIsInstance(attempted.value, PromptInput)
                # The delayed conversation item must not reopen or rename the terminal result.
                self.relay.observe('incoming', {'method': 'item/completed', 'params': {
                    'threadId': self.native, 'turnId': 'later', 'item': {'type': 'userMessage',
                        'id': 'echo-' + method, 'clientId': 'terminal-client', 'content': frame['params']['input']}}})
                self.flush()
                self.assertEqual(self.result(ids[0])['state'], 'person')

    def test_disabled_worker_with_failed_cleanup_still_fences_terminal_retries(self):
        """A stale inventory after optional worker failure must not let terminal input bypass its shared winner."""
        rows = self.question_event(item='disabled-worker', questions=[{'title': 'Which?', 'options': None}] * 2)
        row = rows[0]
        frame = {'id': 'first', 'method': 'turn/steer', 'params': {'threadId': self.native,
            'input': [{'type': 'text', 'text': wrapper('disabled-worker', 0, 'Which?', 'One')}]}}
        self.relay.observe('outgoing', frame)
        with patch.object(self.relay.files, 'apply', return_value=[]), self.assertRaises(ValueError):
            asyncio.run(self.relay.before_send(frame))
        with patch.object(self.relay.files, 'apply', side_effect=ValueError('Publication unavailable')):
            asyncio.run(self.relay.close())
        self.assertTrue(self.relay.disabled)
        self.assertIn(row['id'], [r['id'] for r in prompts.catalog(self.session).value['prompts']])
        retry = {**frame, 'id': 'retry'}
        self.relay.observe('outgoing', retry)
        with self.assertRaises(ValueError):
            asyncio.run(self.relay.before_send(retry))
        # The stale disk snapshot can still admit mv2 input, so denying terminal forwarding matters.
        admitted = prompts.submit(self.session, PromptAnswer(id=row['id'], answers={'answer': ('Two',)}),
                                  SimpleNamespace(check_session=lambda: None))
        self.assertIsInstance(admitted.value, PromptInput)
        asyncio.run(self.relay.before_send(retry))  # A verified input-first claim still permits terminal input.
        other = rows[1]['id']
        self.assertTrue(self.relay.files.claim_decision(other, self.relay.envelope(self.native, other), 'closed').won)
        closed = {**frame, 'id': 'closed', 'params': {**frame['params'], 'input': [{'type': 'text',
            'text': wrapper('disabled-worker', 1, 'Which?', 'One')}]}}
        asyncio.run(self.relay.before_send(closed))
        self.assertNotIsInstance(prompts.submit(self.session, PromptAnswer(id=other, answers={'answer': ('Two',)}),
            SimpleNamespace(check_session=lambda: None)).value, PromptInput)

    def test_answer_returns_one_unframed_input_recipe_without_a_callback_response_or_answer_file(self):
        """An async answer must use ordinary delivery once, without framing that native parsing rejects."""
        rows = self.question_event()
        self.assertEqual(len(rows), 1)
        id = rows[0]['id']
        self.session = replace(self.session, status='working')
        checks = []
        def forbid(*args):
            self.fail('Async answers must never write a callback answer file')
        executor = SimpleNamespace(check_session=lambda: checks.append(True), write_prompt_answer=forbid)
        reply = prompts.submit(self.session, PromptAnswer(id=id, answers={'answer': ('One',)}), executor)
        self.assertEqual(reply.state, 'ok')
        self.assertIsInstance(reply.value, PromptInput)
        self.assertEqual(reply.value.id, 'prompt-' + id)
        self.assertEqual(reply.value.text, wrapper('async-one', 0, 'Which?', 'One'))
        self.assertTrue(checks)
        again = prompts.submit(self.session, PromptAnswer(id=id, answers={'answer': ('Two',)}), executor)
        self.assertNotIsInstance(again.value, PromptInput)
        self.assertEqual(again.value['state'], 'unknown')
        self.assertFalse((self.directory / 'prompts' / (id + '.answer.json')).exists())
        self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
            'turnId': 'later', 'item': {'type': 'userMessage', 'id': 'own-reply', 'clientId': reply.value.id,
                'content': [{'type': 'text', 'text': reply.value.text}]}}})
        self.flush()
        self.assertNotEqual(self.result(id)['state'], 'person')
        self.assertEqual(len(prompts.catalog(self.session).value['prompts']), 1)
        async def pickup():
            task = asyncio.create_task(self.relay.run(forbid))
            try:
                for _ in range(30):
                    if not prompts.catalog(self.session).value['prompts']:
                        return
                    await asyncio.sleep(.02)
                self.fail('A claimed ordinary input must disappear from the open registry')
            finally:
                await self.relay.close()
                await task
        asyncio.run(pickup())

    def test_discovery_is_live_primary_only_and_unsupported_views_never_gain_input_authority(self):
        """Replayed history, another thread and review traffic cannot become answerable primary questions."""
        self.question_event(target='foreign')
        self.assertEqual(prompts.catalog(self.session).value['prompts'], [])
        self.relay.observe('incoming', {'id': 'history', 'result': {'thread': {
            'id': self.native, 'items': [{'type': 'agentMessage', 'id': 'history', 'delivery': 'async',
                                        'questions': [{'title': 'History?', 'options': None}]}]}}})
        self.flush()
        self.assertEqual(prompts.catalog(self.session).value['prompts'], [])
        for mode in ('review', 'unknown'):
            self.relay.mode = mode
            rows = self.question_event(item=mode)
            row = rows[-1]
            self.assertFalse(row['answerable'])
            self.assertIn(mode, row['reason'])
        self.relay.mode = 'normal'
        self.connection.side_id = 'side'
        rows = self.question_event(item='side', target='side')
        self.assertFalse(rows[-1]['answerable'])
        self.assertIn('/btw', rows[-1]['reason'])
        rows = self.question_event(item='normal')
        self.assertTrue(rows[-1]['answerable'])
        self.session = replace(self.session, status='working')
        executor = SimpleNamespace(check_session=lambda: None, write_prompt_answer=lambda *a: self.fail('No file'))
        for row in rows[:-1]:
            self.assertNotEqual(prompts.submit(self.session, PromptAnswer(id=row['id'],
                answers={'answer': ('One',)}), executor).state, 'ok')
        self.connection.root.native_id = 'replacement'
        self.relay.synchronize()
        self.flush()
        original = self.relay.files.read(rows[-1]['id'] + '.result.json')
        self.assertEqual((original['state'], original['cause']), ('gone', 'switched'))

    def test_callback_questions_remain_waiting_only_even_with_the_native_countdown_flag(self):
        """The callback flag controls a terminal countdown and cannot grant async-input authority."""
        wire = 'callback'
        self.relay.observe('incoming', {'id': wire, 'method': fixtures.QUESTION, 'params': {
            'threadId': self.native, 'turnId': 'turn-one', 'itemId': wire, 'isBlocking': False,
            'questions': [{'id': 'one', 'header': 'One', 'question': 'Which?', 'options': None}]}})
        self.flush()
        rows = prompts.catalog(self.session).value['prompts']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get('requires_waiting', True))
        executor = SimpleNamespace(check_session=lambda: None,
            write_prompt_answer=lambda *a: self.fail('A working callback question must not be submitted'))
        reply = prompts.submit(replace(self.session, status='working'),
            PromptAnswer(id=rows[0]['id'], answers={'one': ('One',)}), executor)
        self.assertEqual(reply.state, 'unavailable')

    def test_known_reply_or_closure_during_validation_never_authorizes_a_second_input(self):
        """A stale open snapshot must not authorize input after a durable terminal or conversation closure."""
        for state in ('person', 'switched'):
            with self.subTest(state=state):
                item = 'validation-' + state
                id = self.question_event(item=item)[-1]['id']
                def close():
                    if state == 'person':
                        self.relay.observe('incoming', {'method': 'item/completed', 'params': {
                            'threadId': self.native, 'turnId': 'later', 'item': {'type': 'userMessage',
                                'id': 'terminal-' + state, 'clientId': 'terminal-client', 'content': [
                                    {'type': 'text', 'text': wrapper(item, 0, 'Which?', 'One')}]}}})
                    else:
                        self.relay.close_record(self.relay.records[id], 'switched', 'The agent changed conversations.')
                        self.relay.index()
                    self.flush()
                    self.assertFalse([r for r in prompts.catalog(self.session).value['prompts'] if r['id'] == id])
                reply = prompts.submit(replace(self.session, status='working'),
                    PromptAnswer(id=id, answers={'answer': ('Two',)}), SimpleNamespace(check_session=close))
                self.assertNotIsInstance(reply.value, PromptInput)
                self.assertIsInstance(reply.value, dict)
                self.assertEqual(reply.value['state'], 'person' if state == 'person' else 'gone')
                claim = self.relay.files.input_claim(id, self.native)
                self.assertEqual(claim.disposition, 'person' if state == 'person' else 'closed')
                self.assertFalse('Two' in (self.directory / 'codex-prompts' / self.launch / (id + '.input.json')).read_text())

    def test_closure_claim_is_visible_before_its_result_and_input_first_is_not_renamed_person(self):
        """The common claim, rather than a later public result check, must order terminal closure and input admission."""
        id = self.question_event(item='publication')[0]['id']
        self.relay.observe('incoming', {'method': 'item/completed', 'params': {
            'threadId': self.native, 'turnId': 'later', 'item': {'type': 'userMessage', 'id': 'terminal',
                'clientId': 'external', 'content': [{'type': 'text',
                    'text': wrapper('publication', 0, 'Which?', 'One')}]}}})
        jobs, self.relay.jobs = self.relay.jobs, prompts.Jobs()
        started, release = threading.Event(), threading.Event()
        outputs, errors = [], []
        write, remove = self.relay.files.write, self.relay.files.remove
        def delayed(name, value):
            if name == id + '.result.json':
                started.set()
                if not release.wait(3):
                    raise AssertionError('The publication test barrier expired')
            return write(name, value)
        def guarded_remove(name, **kwargs):
            if name == id + '.open.json':
                self.assertEqual(self.relay.files.input_claim(id, self.native).disposition, 'person')
            return remove(name, **kwargs)
        def worker():
            try:
                outputs.append(self.relay.files.apply(jobs))
            except Exception as error:
                errors.append(type(error).__name__)
        with patch.object(self.relay.files, 'write', side_effect=delayed), patch.object(
                self.relay.files, 'remove', side_effect=guarded_remove):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(len(prompts.catalog(self.session).value['prompts']), 1)
                reply = prompts.submit(self.session, PromptAnswer(id=id, answers={'answer': ('Two',)}),
                                       SimpleNamespace(check_session=lambda: None))
                self.assertNotIsInstance(reply.value, PromptInput)
                self.assertEqual(reply.value['state'], 'person')
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.relay.applied(outputs[0])
        self.assertEqual(self.result(id)['state'], 'person')

        id = self.question_event(item='input-first')[0]['id']
        reply = prompts.submit(self.session, PromptAnswer(id=id, answers={'answer': ('Two',)}),
                               SimpleNamespace(check_session=lambda: None))
        self.assertIsInstance(reply.value, PromptInput)
        self.relay.observe('incoming', {'method': 'item/completed', 'params': {
            'threadId': self.native, 'turnId': 'later', 'item': {'type': 'userMessage', 'id': 'late-terminal',
                'clientId': 'external', 'content': [{'type': 'text',
                    'text': wrapper('input-first', 0, 'Which?', 'One')}]}}})
        self.flush()
        self.assertEqual(self.result(id)['state'], 'submitted')
        self.assertIsNone(self.result(id)['person_answer'])
        self.assertEqual(self.relay.records[id]['result']['state'], 'submitted')
        self.assertEqual(self.relay.files.input_claim(id, self.native).disposition, 'input')
        asyncio.run(self.relay.close())
        self.assertEqual(self.result(id)['state'], 'unknown')
        self.assertEqual(self.relay.files.input_claim(id, self.native).disposition, 'input')

    def test_in_progress_or_damaged_claim_never_authorizes_another_input_or_reads_a_hardlink(self):
        """An incomplete winner or corrupt claim cannot safely authorize a retry, even before its publisher returns."""
        id = self.question_event(item='in-progress')[0]['id']
        started, release = threading.Event(), threading.Event()
        replies, errors = [], []
        link, fdopen = os.link, os.fdopen
        def delayed_link(source, target, **kwargs):
            result = link(source, target, **kwargs)
            if target == id + '.input.json':
                started.set()
                if not release.wait(3):
                    raise AssertionError('The claim publication barrier expired')
            return result
        def guarded_fdopen(fd, mode='r', **kwargs):
            if mode == 'rb':
                self.assertEqual(os.fstat(fd).st_nlink, 1, 'Claim contents must not be read through a hardlink')
            return fdopen(fd, mode, **kwargs)
        def attempt():
            try:
                replies.append(prompts.submit(self.session, PromptAnswer(id=id, answers={'answer': ('One',)}),
                                               SimpleNamespace(check_session=lambda: None)))
            except Exception as error:
                errors.append(type(error).__name__)
        with patch.object(prompt_files.os, 'link', side_effect=delayed_link), patch.object(
                prompt_files.os, 'fdopen', side_effect=guarded_fdopen):
            thread = threading.Thread(target=attempt)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                losing = prompts.submit(self.session, PromptAnswer(id=id, answers={'answer': ('Two',)}),
                                         SimpleNamespace(check_session=lambda: None))
                self.assertNotIsInstance(losing.value, PromptInput)
                self.assertEqual(losing.value['state'], 'unknown')
                self.assertIn('do not send', losing.value['reason'])
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(replies), 1)
        self.assertIsInstance(replies[0].value, PromptInput)
        self.assertEqual(self.relay.files.input_claim(id, self.native).disposition, 'input')
        for changes in ({}, {'native_id': 'another-thread'}, {'launch_id': 'another-launch'}, {'schema': True}):
            bad = self.question_event(item='damaged-' + str(len(self.relay.records)))[-1]['id']
            value = {'schema': 1, 'launch_id': self.launch, 'native_id': self.native, 'id': bad,
                     'input_id': 'prompt-' + bad, 'disposition': 'person', 'cause': 'person'} if changes else {}
            value.update(changes)
            path = self.directory / 'codex-prompts' / self.launch / (bad + '.input.json')
            raw = json.dumps(value)
            path.write_text(raw)
            result = prompts.submit(self.session, PromptAnswer(id=bad, answers={'answer': ('Two',)}),
                                    SimpleNamespace(check_session=lambda: None))
            self.assertNotIsInstance(result.value, PromptInput)
            self.assertEqual(result.value['state'], 'unknown')
            self.assertIn('do not send', result.value['reason'])
            self.assertEqual(path.read_text(), raw)

    def test_registry_overflow_retains_all_tracked_questions_and_never_admits_an_overflowed_id_later(self):
        """A full registry must not forget once-only identities and reopen them when a slot becomes free."""
        with patch.object(prompts, 'MAX_REQUESTS', 2):
            rows = self.question_event(questions=[{'title': str(i), 'options': None} for i in range(3)])
            self.assertEqual(len(rows), 2)
            first = [row['id'] for row in rows]
            found = prompts.catalog(self.session).value
            self.assertEqual(found['coverage'], 'unknown')
            self.assertIn('Further async questions', found['reason'])
            self.relay.observe('incoming', {'method': 'item/completed', 'params': {'threadId': self.native,
                'turnId': 'next', 'item': {'type': 'userMessage', 'id': 'one', 'content': [
                    {'type': 'text', 'text': wrapper('async-one', 0, '0', 'Done')}]}}})
            self.flush()
            self.question_event(questions=[{'title': str(i), 'options': None} for i in range(3)])
            self.question_event(item='new-unseen')
            self.assertEqual([r['id'] for r in prompts.catalog(self.session).value['prompts']], first[1:])
            self.assertEqual(len(self.relay.records), 2)
            self.assertEqual(self.result(first[0])['state'], 'person')
        self.relay.async_overflow = False
        with patch.object(prompts, 'MAX_RETAINED', 2):
            self.question_event(item='retained-overflow')
            self.assertEqual(len(self.relay.records), 2)
            self.assertTrue(self.relay.async_overflow)


class AsyncTransportTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PromptTransportTest.asyncSetUp
    exchange = fixtures.PromptTransportTest.exchange
    until = fixtures.PromptTransportTest.until

    async def test_winner_io_failure_refuses_the_frame_without_disconnecting_the_terminal(self):
        """A failed optional winner write must fence retries without breaking ordinary terminal input."""
        await self.upstreams[0].send(json.dumps({'method': 'item/completed', 'params': {
            'threadId': self.native, 'turnId': 'turn-one', 'item': {'type': 'agentMessage',
                'id': 'io-failure', 'text': 'Question', 'delivery': 'async',
                'questions': [{'title': 'Which?', 'options': ['One', 'Two']}]}}}))
        await self.until(lambda: len(prompts.catalog(self.session).value['prompts']) == 1)
        row = prompts.catalog(self.session).value['prompts'][0]
        apply, hits = prompt_files.Files.apply, []
        def failed(files, jobs, **kwargs):
            jobs = list(jobs)
            if len(jobs) == 1 and jobs[0][0] == 'settle' and jobs[0][2].get('id') == row['id']:
                hits.append(True)
                raise OSError(errno.EIO, 'PRIVATE_IO_REASON_SENTINEL')
            return apply(files, jobs, **kwargs)
        with patch.object(prompt_files.Files, 'apply', failed):
            for method in ('turn/steer', 'turn/start'):
                for attempt in ('first', 'retry'):
                    request = method + '-' + attempt
                    try:
                        response = await self.exchange({'id': request, 'method': method, 'params': {
                            'threadId': self.native, 'input': [{'type': 'text',
                                'text': wrapper('io-failure', 0, 'Which?', 'PRIVATE ANSWER')}]}})
                    except ConnectionClosed as error:
                        self.fail('Optional winner I/O disconnected the terminal: ' + str(error))
                    self.assertIn('error', response)
                    self.assertNotIn('PRIVATE', repr(response))
                    self.assertFalse([v for v in self.frames if v.get('id') == request])
                request = 'ordinary-after-' + method
                response = await self.exchange({'id': request, 'method': 'turn/steer', 'params': {
                    'threadId': self.native, 'input': [{'type': 'text', 'text': 'Ordinary unrelated input'}]}})
                self.assertEqual(response['result'], {'echo': request})
        self.assertTrue(hits)

    async def test_terminal_forwarding_waits_for_the_shared_claim_and_keeps_input_first(self):
        """Slow publication must not acknowledge a terminal answer before the shared winner exists."""
        async def question(item, options=None):
            before = {r['id'] for r in prompts.catalog(self.session).value['prompts']}
            await self.upstreams[0].send(json.dumps({'method': 'item/completed', 'params': {
                'threadId': self.native, 'turnId': 'turn-one', 'item': {'type': 'agentMessage',
                    'id': item, 'delivery': 'async', 'text': 'Question',
                    'questions': [{'title': 'Which?', 'options': options}]}}}))
            await self.until(lambda: any(r['id'] not in before for r in prompts.catalog(self.session).value['prompts']))
            added = [r for r in prompts.catalog(self.session).value['prompts'] if r['id'] not in before]
            self.assertEqual(len(added), 1)
            return added[0]
        for input_first in (False, True):
            with self.subTest(input_first=input_first):
                item = 'held-terminal-' + str(input_first)
                row = await question(item, ['One', 'Two'])
                entered, release = threading.Event(), threading.Event()
                settle = prompt_files.Files.settle
                def held(files, name, value, **kwargs):
                    if value.get('id') == row['id'] and value.get('state') == 'person':
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError('The terminal claim barrier expired')
                    return settle(files, name, value, **kwargs)
                frame = {'id': item, 'method': 'turn/steer', 'params': {'threadId': self.native,
                    'expectedTurnId': 'turn-one', 'clientUserMessageId': 'terminal-' + item,
                    'input': [{'type': 'text', 'text': wrapper(item, 0, 'Which?', 'One')}]}}
                with patch.object(prompt_files.Files, 'settle', held):
                    task = asyncio.create_task(self.exchange(frame))
                    recipe = None
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        self.assertFalse([f for f in self.frames if f.get('id') == item])
                        if input_first:
                            recipe = await asyncio.to_thread(prompts.submit, replace(self.session, status='working'),
                                PromptAnswer(id=row['id'], answers={'answer': ('Two',)}),
                                SimpleNamespace(check_session=lambda: None))
                            self.assertIsInstance(recipe.value, PromptInput)
                    finally:
                        release.set()
                        await asyncio.wait_for(task, 3)
                await self.until(lambda: not prompts.catalog(self.session).value['prompts'])
                outcome = prompts.outcome(self.session, PromptAnswer(id=row['id'])).value
                self.assertEqual(outcome['state'], 'submitted' if input_first else 'person')
                if input_first:
                    self.assertIsNone(outcome['person_answer'])
                else:
                    second = await asyncio.to_thread(fixtures.core_prompts.answer, self.root, 'agent', row['id'],
                                                    answers={'answer': 'Two'}, wait=False)
                    self.assertEqual(second.state, 'person')
                self.assertEqual([f for f in self.frames if f.get('id') == item], [frame])
        row = await question('unpublished')
        apply = prompt_files.Files.apply
        def missing(files, jobs, **kwargs):
            jobs = list(jobs)
            if any(action == 'settle' and value.get('id') == row['id'] for action, _, value in jobs):
                return []
            return apply(files, jobs, **kwargs)
        with patch.object(prompt_files.Files, 'apply', missing):
            unpublished = {'id': 'unpublished', 'method': 'turn/steer', 'params': {
                'threadId': self.native, 'input': [{'type': 'text',
                    'text': wrapper('unpublished', 0, 'Which?', 'PRIVATE ANSWER')}]}}
            for request in ('unpublished', 'retried-unpublished'):
                refused = await self.exchange({**unpublished, 'id': request})
                self.assertIn('error', refused)
                self.assertNotIn('PRIVATE ANSWER', repr(refused))
                self.assertFalse([f for f in self.frames if f.get('id') == request])
            ordinary = {'id': 'unrelated', 'method': 'turn/steer', 'params': {
                'threadId': self.native, 'input': [{'type': 'text', 'text': 'Ordinary unrelated input'}]}}
            self.assertEqual((await self.exchange(ordinary))['result'], {'echo': 'unrelated'})
        retried = {**unpublished, 'id': 'published-retry'}
        await self.exchange(retried)
        await self.until(lambda: not prompts.catalog(self.session).value['prompts'])
        self.assertEqual(prompts.outcome(self.session, PromptAnswer(id=row['id'])).value['state'], 'person')
        self.assertEqual([f for f in self.frames if f.get('id') == 'published-retry'], [retried])
        row = await question('unverifiable')
        claim_path = self.directory / 'codex-prompts' / self.launch / (row['id'] + '.input.json')
        claim_path.write_text('{}')
        for request in ('unverifiable', 'retried-unverifiable'):
            refused = await self.exchange({'id': request, 'method': 'turn/steer', 'params': {
                'threadId': self.native, 'input': [{'type': 'text',
                    'text': wrapper('unverifiable', 0, 'Which?', 'One')}]}})
            self.assertIn('error', refused)
            self.assertFalse([f for f in self.frames if f.get('id') == request])
            self.assertEqual(claim_path.read_text(), '{}')

    async def test_actual_relay_forwards_async_items_and_only_the_explicit_ordinary_input(self):
        """Async discovery and answer recipes must never inject an unsolicited callback response on the owning socket."""
        await self.upstreams[0].send(json.dumps({'method': 'item/completed', 'params': {
            'threadId': self.native, 'turnId': 'turn-one', 'item': {'type': 'agentMessage',
                'id': 'transport', 'text': 'Question', 'delivery': 'async',
                'questions': [{'title': 'Which?', 'options': ['One']}]}}}))
        async with asyncio.timeout(5):
            while json.loads(await self.client.recv()).get('method') != 'item/completed':
                pass
        await self.until(lambda: len(prompts.catalog(self.session).value['prompts']) == 1)
        row = prompts.catalog(self.session).value['prompts'][0]
        self.session = replace(self.session, status='working')
        recipe = await asyncio.to_thread(prompts.submit, self.session,
            PromptAnswer(id=row['id'], answers={'answer': ('One',)}), self.executor)
        self.assertEqual(recipe.state, 'ok')
        self.assertEqual(recipe.value.text, wrapper('transport', 0, 'Which?', 'One'))
        # This explicit normal input models core's ordinary send boundary; queue/restart
        # admission itself is covered by the owning core fixture, not this relay row.
        frame = {'id': 'ordinary-input', 'method': 'turn/steer', 'params': {'threadId': self.native,
            'expectedTurnId': 'turn-one', 'clientUserMessageId': recipe.value.id,
            'input': [{'type': 'text', 'text': recipe.value.text, 'text_elements': []}]}}
        await self.exchange(frame)
        await self.until(lambda: not prompts.catalog(self.session).value['prompts'])
        self.assertEqual([f for f in self.frames if f.get('method') == 'turn/steer'], [frame])
        self.assertFalse([f for f in self.frames if 'method' not in f])
        self.assertFalse((self.directory / 'prompts' / (row['id'] + '.answer.json')).exists())
        self.assertTrue(all('One' not in p.read_text() for p in
            (self.directory / 'codex-prompts' / self.launch).glob('*.input.json')))
        await self.upstreams[0].send(json.dumps({'method': 'item/completed', 'params': {
            'threadId': self.native, 'turnId': 'turn-one', 'item': {'type': 'agentMessage',
                'id': 'terminal-transport', 'text': 'Question', 'delivery': 'async',
                'questions': [{'title': 'Which?', 'options': ['One']}]}}}))
        await self.until(lambda: len(prompts.catalog(self.session).value['prompts']) == 1)
        row = prompts.catalog(self.session).value['prompts'][0]
        terminal = {'id': 'terminal-input', 'method': 'turn/steer', 'params': {'threadId': self.native,
            'expectedTurnId': 'turn-one', 'clientUserMessageId': 'terminal-client', 'input': [{'type': 'text',
                'text': wrapper('terminal-transport', 0, 'Which?', 'One'), 'text_elements': []}]}}
        await self.exchange(terminal)  # The fake daemon never emits the input's userMessage item.
        try:
            await self.until(lambda: not prompts.catalog(self.session).value['prompts'])
        except TimeoutError:
            self.fail('The terminal answer stayed answerable while Codex had not echoed the queued input')
        result = await asyncio.to_thread(fixtures.core_prompts.answer, self.root, 'agent', row['id'],
                                        answers={'answer': 'Second answer'}, wait=False)
        self.assertEqual(result.state, 'person')
        self.assertEqual([f for f in self.frames if f.get('method') == 'turn/steer'], [frame, terminal])
