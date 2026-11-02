"""Read-only Codex snapshots and the core pre-start generation boundary."""
from dataclasses import replace
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.codex import ask, run_history, transcripts
from multiverse.harnesses.types import NativeResult, ObservedBinding, RunFailure, RunReply, SessionHandle, TranscriptRowsChanged
from multiverse.runtime import native, runs
from multiverse.store import write
from test_codex_transcript_reading import event, message
from test_multiverse import BaseTest
from test_runs import Provider, REQUEST


class CodexAskTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.home = self.work / 'codex-home'
        self.native = str(uuid4())
        self.path = self.home / 'sessions' / f'rollout-2026-11-01T00-00-00-{self.native}.jsonl'
        self.path.parent.mkdir(parents=True)
        self.adapter = harnesses.load(self.root, 'codex')
        self.session = SessionHandle(str(uuid4()), 'codex', str(self.work), native_id=self.native,
                                     home=str(self.home), session_file=str(self.work / 'agent/session.jsonl'))
        transcripts._indexes.clear()
        self.addCleanup(transcripts._indexes.clear)

    def write(self, rows=(), **header):
        values = [{'type': 'session_meta', 'payload': {'id': self.native, 'model_provider': 'openai', **header}},
                  {'type': 'turn_context', 'payload': {'model': 'gpt-6-sol', 'effort': 'low'}}, *rows]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in values))

    def test_snapshot_starts_at_checkpoint_with_provenance_and_no_doubled_echoes(self):
        """Compaction discards old inputs, but the prior model and original instructions must survive the copy."""
        encrypted = {'type': 'reasoning', 'summary': [], 'encrypted_content': 'same-model-opaque'}
        self.write([message('old'), {'type': 'compacted', 'payload': {'message': '',
                    'replacement_history': [encrypted, message('kept', 'assistant')['payload']]}},
                    message('question'), event('user_message', message='question'), message('answer', 'assistant')],
                   base_instructions={'text': 'agent instructions'})
        before = self.path.read_bytes()
        snap = ask.snapshot(self.adapter, self.session)
        self.assertEqual((snap.model, snap.effort, snap.base_instructions), ('gpt-6-sol', 'low', 'agent instructions'))
        self.assertEqual([r.data['type'] for r in snap.messages[:3]], ['session_meta', 'turn_context', 'compacted'])
        copied = run_history.session(snap.messages, model=snap.model, session_id=str(uuid4()), cwd='/private',
                                     at='2026-11-01T00:00:00Z', system=snap.base_instructions, own_conversation=True)
        self.assertNotIn(b'"old"', copied.content)
        self.assertEqual(copied.content.count(b'same-model-opaque'), 1)
        self.assertEqual(copied.content.count(b'"question"'), 1)
        self.assertEqual(before, self.path.read_bytes())

    def test_snapshot_refuses_unknown_provider_gap_unreadable_rows_and_repeated_changes(self):
        """An apparently successful partial snapshot can leak another provider's conversation or answer from missing context."""
        for provider in ('custom', None):
            self.write([message('answer', 'assistant')], model_provider=provider)
            with self.assertRaisesRegex(ask.AskUnavailable, 'provider'):
                ask.snapshot(self.adapter, self.session)
        self.write([message('answer', 'assistant')])
        view = self.adapter.transcript_view(self.session)
        with patch.object(self.adapter, 'transcript_view', return_value=replace(view, gap=True)):
            with self.assertRaisesRegex(ask.AskUnavailable, 'never stored'):
                ask.snapshot(self.adapter, self.session)
        with patch.object(self.adapter, 'transcript_bodies', return_value=[None] * len(view.history)) as bodies:
            with self.assertRaisesRegex(ask.AskUnavailable, 'unreadable'):
                ask.snapshot(self.adapter, self.session)
            self.assertEqual(bodies.call_args.kwargs['max_bytes'], 16 << 20)
        with patch.object(self.adapter, 'transcript_bodies', side_effect=TranscriptRowsChanged()) as bodies:
            with self.assertRaisesRegex(ask.AskUnavailable, 'kept changing'):
                ask.snapshot(self.adapter, self.session)
            self.assertEqual(bodies.call_count, 3)
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': self.native,
                                                                         'model_provider': 'openai'}}) + '\n')
        with self.assertRaisesRegex(ask.AskUnavailable, 'turn model'):
            ask.snapshot(self.adapter, self.session)

    def test_default_and_empty_instructions_are_distinct_and_changed_rows_retry(self):
        """An absent native instruction field means the checked Codex default, while an explicit empty field means none."""
        for header, expected in (({}, None), ({'base_instructions': {'text': ''}}, '')):
            self.write([message('answer', 'assistant')], **header)
            body_read = self.adapter.transcript_bodies
            calls = []
            def changed_once(*args, **kwargs):
                calls.append(True)
                if len(calls) == 1:
                    raise TranscriptRowsChanged()
                return body_read(*args, **kwargs)
            with patch.object(self.adapter, 'transcript_bodies', side_effect=changed_once):
                self.assertEqual(ask.snapshot(self.adapter, self.session).base_instructions, expected)
            self.assertEqual(len(calls), 2)

    def test_latest_owned_settings_choose_the_question_without_rewriting_history(self):
        """Idle model changes can precede compaction; ask must use the tip settings without relabelling old reasoning."""
        settings = event('thread_settings_applied', thread_id=self.native,
                         thread_settings={'model': 'gpt-6-luna', 'model_provider_id': 'openai',
                                          'reasoning_effort': 'high'})
        reasoning = {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': [],
                                                         'encrypted_content': 'old-model-reasoning'}}
        compacted = {'type': 'compacted', 'payload': {'replacement_history': [reasoning['payload']]}}
        for where, effort, expected in (('after-turn', 'high', ('gpt-6-luna', 'high')),
                                        ('before-compaction', 'high', ('gpt-6-luna', 'high')),
                                        ('before-turn', 'high', ('gpt-6-sol', 'medium')),
                                        ('after-turn', None, ('gpt-6-luna', None)),
                                        ('after-turn', 'omitted', ('gpt-6-luna', None))):
            with self.subTest(where=where, effort=effort):
                settings['payload']['thread_settings']['reasoning_effort'] = effort
                if effort == 'omitted':
                    settings['payload']['thread_settings'].pop('reasoning_effort')
                rows = [message('answer', 'assistant'), settings, reasoning]
                if where == 'before-compaction':
                    rows.append(compacted)
                elif where == 'before-turn':
                    rows.append({'type': 'turn_context', 'payload': {'model': 'gpt-6-sol', 'effort': 'medium'}})
                self.write(rows)
                before = self.path.read_bytes()
                snap = ask.snapshot(self.adapter, self.session)
                self.assertEqual((snap.model, snap.effort), expected)
                contexts = [row.data['payload']['model'] for row in snap.messages if row.data['type'] == 'turn_context']
                self.assertTrue(contexts)
                self.assertEqual(set(contexts), {'gpt-6-sol'})
                copied = run_history.session(snap.messages, model=snap.model, session_id=str(uuid4()), cwd='/private',
                                             at='2026-11-01T00:00:00Z', system=None, own_conversation=True)
                self.assertEqual(copied.content.count(b'old-model-reasoning'), 1)
                self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_latest_settings_refuse_instead_of_spending_on_stale_settings(self):
        """Missing or foreign settings evidence must not silently send a question using an older model or effort."""
        settings = {'model': 'gpt-6-luna', 'model_provider_id': 'openai', 'reasoning_effort': 'high'}
        for field, value in (('thread_id', str(uuid4())), ('thread_id', None), ('thread_id', 'omitted'),
                             ('model', None), ('model', ''), ('model', 'omitted'),
                             ('model_provider_id', 'custom'), ('model_provider_id', 'omitted'),
                             ('reasoning_effort', []), ('thread_settings', None)):
            with self.subTest(field=field, value=value):
                body = event('thread_settings_applied', thread_id=self.native, thread_settings=dict(settings))
                target = body['payload'] if field in ('thread_id', 'thread_settings') else body['payload']['thread_settings']
                if value == 'omitted':
                    target.pop(field)
                else:
                    target[field] = value
                self.write([message('answer', 'assistant'), body])
                with self.assertRaises(ask.AskUnavailable):
                    ask.snapshot(self.adapter, self.session)
        body = event('thread_settings_applied', thread_id=self.native, thread_settings=settings)
        for compacted in (False, True):
            with self.subTest(unreadable=True, compacted=compacted):
                self.write([body] + ([{'type': 'compacted', 'payload': {'replacement_history': []}}] if compacted else []))
                body_read = self.adapter.transcript_bodies
                def missing_settings(session, view, indexes, **kwargs):
                    bodies = body_read(session, view, indexes, **kwargs)
                    return [None if view.rows[index].kind == 'event_msg/thread_settings_applied' else value
                            for index, value in zip(indexes, bodies)]
                with patch.object(self.adapter, 'transcript_bodies', side_effect=missing_settings):
                    with self.assertRaises(ask.AskUnavailable):
                        ask.snapshot(self.adapter, self.session)


class AskGenerationTests(BaseTest):
    def test_pending_native_binding_and_journal_transition_are_refused_without_repair(self):
        """Unreconciled identity evidence must stop the snapshot before reading history or spending, without a repair write."""
        identifier, previous, newer, launch = (str(uuid4()) for _ in range(4))
        directory = self.root / 'state/agents' / identifier
        directory.mkdir()
        home = str(self.work / 'recorded-home')
        properties = {'id': identifier, 'harness': 'fake', 'workspace': str(self.work),
                      'native_session_id': previous, 'native_session_home': home}
        facts = {'launch_id': launch}
        write(directory / 'properties.json', properties)
        write(directory / 'runtime.json', facts)
        session = SessionHandle(identifier, 'fake', str(self.work), native_id=previous, home=home,
                                launch_id=launch, session_file=str(directory / 'session.jsonl'))
        executor = native.NativeExecutor(str(self.work), properties, facts)
        class ObservedProvider(Provider):
            def observed_binding(self, session):
                return ObservedBinding(native_id=newer, home=home, evidence='fixture')
        for mode in ('observed', 'journal'):
            if mode == 'journal':
                row = {'schema': 1, 'id': str(uuid4()), 'at': '2026-11-01T00:00:00Z', 'harness': 'fake',
                       'launch': launch, 'reason': 'detected', 'from': previous, 'to': newer, 'home': home}
                (directory / 'sessions.jsonl').write_text(json.dumps(row) + '\n')
            before = {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
            prepare = Mock(return_value=REQUEST)
            with self.subTest(mode=mode), patch.object(runs, '_execute') as execute:
                with self.assertRaises(RunFailure) as caught:
                    executor.run_once(ObservedProvider() if mode == 'observed' else Provider(), session, prepare)
                self.assertEqual(caught.exception.kind, 'unavailable')
                prepare.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(before, {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()})

    def test_codex_question_never_reconciles_or_writes_source_records(self):
        """The ordinary native operation path repairs bindings, but a private question must only read the agent."""
        identifier = str(uuid4())
        directory = self.root / 'state/agents' / identifier
        directory.mkdir()
        write(directory / 'properties.json', {'id': identifier, 'name': 'source', 'harness': 'codex',
                                              'workspace': str(self.work), 'native_session_id': str(uuid4())})
        adapter = harnesses.load(self.root, 'codex')
        before = {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        with patch.object(native.harnesses, 'load', return_value=adapter), \
                patch.object(adapter, 'ask', return_value=NativeResult('ok', value={'answer': 'private'})), \
                patch.object(native, 'session_snapshot', side_effect=AssertionError('reconciled source')), \
                patch.object(native, 'lock', side_effect=AssertionError('locked source')):
            self.assertEqual(native.operation(self.root, identifier, 'ask', 'question')['state'], 'ok')
        self.assertEqual(before, {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()})

    def test_generation_and_storage_changes_during_prepare_or_plan_prevent_native_execution(self):
        """A clear, relaunch or storage change during the snapshot must not spend on the previous generation."""
        identifier, launch, native_id = str(uuid4()), str(uuid4()), str(uuid4())
        directory = self.root / 'state/agents' / identifier
        directory.mkdir()
        home = str(self.work / 'recorded-home')
        properties = {'id': identifier, 'harness': 'fake', 'workspace': str(self.work),
                      'native_session_id': native_id, 'native_session_home': home,
                      'native_storage_environment': {'STORE': home}}
        facts = {'launch_id': launch}
        session = SessionHandle(identifier, 'fake', str(self.work), native_id=native_id,
                                home=home, launch_id=launch, session_file=str(directory / 'session.jsonl'))
        executor = native.NativeExecutor(str(self.work), properties, facts)
        for stage in ('prepare', 'plan'):
            for field in ('launch', 'native_session_id', 'native_session_home', 'native_storage_environment',
                          'session_generation', 'navigation', 'unchanged'):
                with self.subTest(stage=stage, field=field):
                    write(directory / 'properties.json', properties)
                    write(directory / 'runtime.json', facts)
                    def change():
                        if field == 'launch':
                            write(directory / 'runtime.json', {'launch_id': str(uuid4())})
                        elif field in ('session_generation', 'navigation'):
                            write(directory / 'runtime.json', {**facts, field:
                                  2 if field == 'session_generation' else {'state': 'admitted'}})
                        elif field != 'unchanged':
                            value = {'STORE': home + '-changed'} if field == 'native_storage_environment' else str(uuid4())
                            write(directory / 'properties.json', {**properties, field: value})
                    class SelectedProvider(Provider):
                        family, storage_env = 'fake', ('STORE',)
                        def run_plan(inner, *args, **kwargs):
                            if stage == 'plan':
                                change()
                            return super().run_plan(*args, **kwargs)
                    def prepare():
                        if stage == 'prepare':
                            change()
                        return REQUEST
                    with patch.object(runs, '_execute', return_value=RunReply(text='private')) as execute:
                        if field == 'unchanged':
                            self.assertEqual(executor.run_once(SelectedProvider(files=False), session, prepare).text, 'private')
                            self.assertEqual(execute.call_count, 1)
                        else:
                            with self.assertRaises(RunFailure) as caught:
                                executor.run_once(SelectedProvider(files=False), session, prepare)
                            self.assertEqual(caught.exception.kind, 'unavailable')
                            execute.assert_not_called()
