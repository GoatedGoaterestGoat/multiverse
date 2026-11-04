"""Codex rollout metadata, exact storage, native history and snapshot body reads."""
from dataclasses import replace
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.codex import transcripts as t
from multiverse.harnesses.types import SessionHandle, TranscriptRowsChanged
from test_multiverse import BaseTest


def event(kind, **fields):
    return {'type': 'event_msg', 'timestamp': '2026-10-25T10:00:01Z', 'payload': {'type': kind, **fields}}


def message(text, role='user', **fields):
    return {'type': 'response_item', 'timestamp': '2026-10-25T10:00:00Z',
            'payload': {'type': 'message', 'role': role, 'content': [{'type': 'output_text' if role == 'assistant' else 'input_text', 'text': text}], **fields}}


class CodexTranscriptReadingTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.home = self.work / 'codex-home'
        self.native = str(uuid4())
        self.path = self.home / 'sessions/2026/10/25' / f'rollout-2026-10-25T10-00-00-{self.native}.jsonl'
        self.path.parent.mkdir(parents=True)
        self.adapter = harnesses.load(self.root, 'codex')
        self.session = SessionHandle(str(uuid4()), 'codex', str(self.work), native_id=self.native,
                                     home=str(self.home), session_file=str(self.work / 'agent/session.jsonl'))
        t._indexes.clear()
        self.addCleanup(t._indexes.clear)

    def write(self, rows, path=None):
        header = {'type': 'session_meta', 'timestamp': '2026-10-25T09:59:00Z',
                  'payload': {'id': self.native, 'cli_version': '0.164.0'}}
        (path or self.path).write_text(''.join(json.dumps(row) + '\n' for row in [header, *rows]))

    def test_native_inputs_are_not_counted_twice_or_confused_with_context_and_late_ends(self):
        """Native events authenticate prompts and ends; counting their echoes or trusting final text silently corrupts turns."""
        cases = [
            ('event after input', [message('go'), event('user_message', message='go')], ['prompt', 'meta']),
            ('event before input', [event('user_message', message='go'), message('go')], ['meta', 'prompt']),
            ('context has no input event', [message('instructions'), message('go'), event('user_message', message='go')],
             ['injected', 'prompt', 'meta']),
            ('repeated body is one steer', [message('go'), event('user_message', message='go'), message('go'),
                                          event('user_message', message='go')], ['prompt', 'meta', 'mid_turn', 'meta']),
            ('ambiguous legacy input stays context', [message('same'), message('same'),
                                                       event('user_message', message='same')],
             ['injected', 'injected', 'meta']),
            ('native media provenance', [
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,first'}],
                 'internal_chat_message_metadata_passthrough': {
                     'content_item_metadata': [{'provenance': {'type': 'user'}}]}}}], ['prompt']),
            ('media-only legacy echo is not exact proof', [
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,first'}]}},
                event('user_message', message='', images=['data:image/png;base64,other'])], ['injected', 'meta']),
            ('mixed media text is not exact proof', [
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': 'look'}, {'type': 'input_image', 'image_url': 'first'}]}},
                event('user_message', message='look', images=['other'])], ['injected', 'meta']),
            ('two echo formats cannot authenticate two inputs', [message('same'), message('same'),
                event('user_message', message='same'), event('item_completed', item={
                    'type': 'UserMessage', 'content': [{'type': 'text', 'text': 'same'}]})],
                ['injected', 'injected', 'meta', 'meta']),
            ('framing is not provenance', [message('Message from lead:\ngo'), event('user_message', message='Message from lead:\ngo')],
             ['prompt', 'meta']),
            ('assistant final is not an end', [message('done', 'assistant', phase='final_answer')], ['response']),
            ('tool result', [{'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call', 'output': 'ok'}}],
             ['tool_result']),
            ('compaction content stays nested', [{'type': 'compacted', 'payload': {'message': 'summary',
                                                                                'replacement_history': [message('go')['payload']]}}],
             ['compaction']),
            ('inherited user stays context', [{**message('old'), 'metadata': {'inherited_user_message': True}},
                                             event('user_message', message='old')], ['injected', 'meta']),
            ('compaction output', [{**message('summary', 'assistant'), 'metadata': {'compaction_output': True}}], ['compaction']),
            ('known harness context cannot match input', [message('same', internal_chat_message_metadata_passthrough={
                'content_item_metadata': [{'provenance': {'type': 'harness'}}]}),
                event('user_message', message='same')], ['injected', 'meta']),
            ('additional tools are context', [{'type': 'response_item', 'payload': {'type': 'additional_tools'}}], ['injected']),
            ('unknown event', [event('future')], ['meta']),
        ]
        for name, rows, labels in cases:
            with self.subTest(name):
                self.write([event('task_started', turn_id='t'), *rows])
                view = self.adapter.transcript_view(self.session)
                self.assertEqual(view.source.state, 'supported')
                self.assertEqual([fact.label for fact in view.rows[2:]], labels)
                self.assertTrue(all(fact.source is None for fact in view.rows))
                self.assertFalse(any(fact.ends_turn for fact in view.rows))
                for fact in view.rows:
                    if fact.call_id is not None:
                        self.assertIsNone(fact.ref)
        for name, row, expected in [
            ('session identity is not an item', {'type': 'session_meta', 'payload': {'id': self.native}}, None),
            ('turn identity is not an item', {'type': 'turn_context', 'payload': {'id': 'turn'}}, None),
            ('event identity is not an item', event('future', id='event'), None),
            ('response item identity', message('answer', 'assistant', id='item'), 'item'),
            ('peer item identity', {'type': 'inter_agent_communication', 'payload': {
                'id': 'peer-item', 'content': 'hello', 'trigger_turn': False}}, 'peer-item'),
            ('missing item identity', message('answer', 'assistant'), None),
        ]:
            with self.subTest(native_item_ref=name):
                self.write([row])
                view = self.adapter.transcript_view(self.session)
                self.assertEqual(view.source.state, 'supported')
                self.assertIsNone(view.rows[0].ref)
                self.assertEqual(view.rows[1].ref, expected)
        for name, ending, end in [('complete', event('task_complete', turn_id='t'), True),
                                  ('abort', event('turn_aborted', turn_id='t'), True),
                                  ('late completion', event('task_complete', turn_id='old'), False),
                                  ('unattributed completion', event('task_complete'), False)]:
            with self.subTest(name):
                self.write([event('task_started', turn_id='t'), message('go'), event('user_message', message='go'), ending])
                self.assertEqual(self.adapter.transcript_view(self.session).rows[-1].ends_turn, end)

    def test_checked_native_rollout_labels_steering_followups_and_interruptions(self):
        """The checked release uses provenance and item_completed echoes, so legacy-only parsing silently loses every prompt."""
        for version, count in (('0.164.0', 111), ('0.166.0', 110)):
            with self.subTest(version=version):
                raw = (Path(__file__).parent / f'fixtures/codex/rollout-{version}.jsonl').read_bytes()
                native_rows = [json.loads(line) for line in raw.splitlines()]
                self.native = native_rows[0]['payload']['id']
                self.session = replace(self.session, native_id=self.native)
                self.path = self.path.with_name(f'rollout-2026-10-25T10-00-00-{self.native}.jsonl')
                self.path.write_bytes(raw)
                view = self.adapter.transcript_view(self.session)
                self.assertEqual((view.source.state, view.source.version, len(view.rows)), ('supported', version, count))
                expected = {8: 'injected', 12: 'prompt', 13: 'meta', 14: 'response', 17: 'tool_result',
                            19: 'mid_turn', 20: 'meta', 27: 'response', 30: 'meta', 33: 'prompt',
                            43: 'prompt', 52: 'prompt', 58: 'injected', 59: 'injected', 63: 'prompt'}
                for number, label in expected.items():
                    with self.subTest(number):
                        self.assertEqual(view.rows[number].label, label)
                self.assertEqual(sum(fact.label == 'prompt' for fact in view.rows), 9)
                self.assertEqual(sum(fact.label == 'mid_turn' for fact in view.rows), 1)
                self.assertEqual(sum(fact.ends_turn for fact in view.rows), 9)
                self.assertTrue(all(fact.source is None for fact in view.rows))
                self.assertEqual(view.rows[12].preview, 'Run the offline tool sequence.')
                self.assertEqual(view.rows[17].call_id, native_rows[17]['payload']['call_id'])
                # Provenance is sufficient even before the UI echo is written.
                self.path.write_bytes(b'\n'.join(raw.splitlines()[:13]) + b'\n')
                self.assertEqual(self.adapter.transcript_view(self.session).rows[12].label, 'prompt')

    def test_rollbacks_abandon_native_turns_and_restore_the_prior_context_boundary(self):
        """Native rollback counts every ordinary input, including steers, while uncertain old boundaries must never delete a proven prompt."""
        rows = [event('task_started', turn_id='a'), message('first', id='first'), event('user_message', message='first'),
                {'type': 'compacted', 'payload': {'message': 'kept summary'}}, event('task_complete', turn_id='a'),
                event('task_started', turn_id='b'), message('second', id='second'), event('user_message', message='second'),
                {'type': 'compacted', 'payload': {'message': 'discarded summary'}}, event('task_complete', turn_id='b'),
                event('thread_rolled_back', num_turns=1)]
        self.write(rows)
        view = self.adapter.transcript_view(self.session)
        self.assertEqual([view.rows[i].ref for i in view.history if view.rows[i].label == 'prompt'], ['first'])
        self.assertNotIn(7, view.history)
        self.assertEqual(view.rows[view.history[view.context_from]].label, 'compaction')
        self.assertEqual(view.history[view.context_from], 4)
        self.assertEqual(view.history_version, 1)
        unknown = self.adapter.transcript_view(self.session, tip='not-stored')
        self.assertEqual((unknown.history, unknown.context_from, unknown.tip, unknown.gap),
                         (view.history, view.context_from, view.tip, None))
        old = self.adapter.transcript_view(self.session, tip='second')
        self.assertIn(7, old.history)
        self.assertNotIn(11, old.history)

        # Checked-release steer boundary and uncertain older input probes from review.
        captured = [json.loads(line) for line in (Path(__file__).parent / 'fixtures/codex/rollout-0.164.0.jsonl').read_text().splitlines()][:31]
        captured[0]['payload']['id'] = self.native
        for count in (1, 2):
            with self.subTest(native_inputs_removed=count):
                value = [*captured, event('thread_rolled_back', num_turns=count)]
                self.path.write_text(''.join(json.dumps(row) + '\n' for row in value))
                result = self.adapter.transcript_view(self.session)
                self.assertEqual(result.source.state, 'supported')
                self.assertNotIn(19, result.history)
                self.assertEqual(12 in result.history, count == 1)
        provenance = {'content_item_metadata': [{'provenance': {'type': 'user'}}]}
        for name, injected in [
                ('inherited ordinary input', {**message('copied', id='copied', internal_chat_message_metadata_passthrough=provenance),
                                              'metadata': {'inherited_user_message': True}}),
                ('non-triggering peer input', {'type': 'inter_agent_communication', 'payload': {
                    'id': 'peer', 'author': '/sender', 'recipient': '/receiver', 'content': 'continue', 'trigger_turn': False}})]:
            with self.subTest(native_boundary=name):
                self.write([event('task_started', turn_id='one'), message('keep', id='keep',
                            internal_chat_message_metadata_passthrough=provenance), injected,
                            event('thread_rolled_back', num_turns=1)])
                result = self.adapter.transcript_view(self.session)
                self.assertEqual(result.source.state, 'supported')
                self.assertIn(2, result.history)
                self.assertNotIn(3, result.history)
        context_cases = [
            ('managed developer context', message('<managed_developer_instructions>ROLLED_BACK</managed_developer_instructions>', 'developer', id='context'), False, 'supported'),
            ('case-insensitive developer prefix', message(' \n<MANAGED_DEVELOPER_INSTRUCTIONS>ROLLED_BACK', 'developer', id='context'), False, 'supported'),
            ('mixed developer message', message('', 'developer', id='context', content=[
                {'type': 'input_text', 'text': 'persistent developer text'},
                {'type': 'input_text', 'text': '<model_switch>changed</model_switch>'}]), False, 'supported'),
            ('environment user context', message('<environment_context>ROLLED_BACK</environment_context>', id='context',
                internal_chat_message_metadata_passthrough={'content_item_metadata': [{'provenance': {'type': 'harness'}}]}), False, 'supported'),
            ('external user context', message('<external_context>ROLLED_BACK</external_context>', id='context'), False, 'supported'),
            ('legacy environment user context', message('<environment_context>ROLLED_BACK</environment_context>', id='context'), False, 'supported'),
            ('opaque checkpoint ending in context', {'type': 'compacted', 'payload': {'message': 'summary',
                'replacement_history': [message('<managed_developer_instructions>context</managed_developer_instructions>', 'developer')['payload']]}}, None, 'error'),
            ('opaque checkpoint filtered system tail', {'type': 'compacted', 'payload': {'message': 'summary',
                'replacement_history': [message('<managed_developer_instructions>context</managed_developer_instructions>', 'developer')['payload'],
                                        message('filtered', 'system')['payload']]}}, None, 'error'),
            ('unknown model membership', {'type': 'response_item', 'payload': {}}, None, 'error'),
            ('checkpoint with unknown message shape', {'type': 'compacted', 'payload': {'message': 'summary',
                'replacement_history': [{'type': 'message'}]}}, None, 'error'),
            ('checkpoint ending in a response', {'type': 'compacted', 'payload': {'message': 'summary',
                'replacement_history': [message('summary response', 'assistant')['payload']]}}, True, 'supported'),
            ('persistent developer text', message('keep persistent developer instructions', 'developer', id='context'), True, 'supported'),
            ('unknown user context', message('<opaque_context>unknown</opaque_context>', id='context',
                internal_chat_message_metadata_passthrough={'content_item_metadata': [{'provenance': {'type': 'harness'}}]}), None, 'error'),
            ('malformed developer content', message('', 'developer', id='context', content={'unknown': 'shape'}), None, 'error'),
        ]
        for name, context_row, retained, state in context_cases:
            with self.subTest(pre_input_context=name):
                self.write([event('task_started', turn_id='a'), message('A', id='a',
                            internal_chat_message_metadata_passthrough=provenance), message('answer A', 'assistant', id='answer'),
                            context_row, event('task_started', turn_id='b'), message('B', id='b',
                            internal_chat_message_metadata_passthrough=provenance), message('answer B', 'assistant'),
                            event('thread_rolled_back', num_turns=1)])
                result = self.adapter.transcript_view(self.session)
                self.assertEqual(result.source.state, state)
                if state == 'supported':
                    self.assertEqual(4 in result.history, retained)
                    self.assertIn(2, result.history)
                    self.assertNotIn(6, result.history)
                else:
                    self.assertEqual(result.rows, ())
        for name, filtered, retained in [
                ('system message', message('filtered system record', 'system'), False),
                ('unowned configuration', {'type': 'response_item', 'payload': {'type': 'configuration_update', 'reasoning': {'effort': 'low'}}}, False),
                ('compaction trigger', {'type': 'response_item', 'payload': {'type': 'compaction_trigger'}}, False),
                ('native Other', {'type': 'response_item', 'payload': {'type': 'other'}}, False),
                ('owned configuration barrier', {'type': 'response_item', 'payload': {'type': 'configuration_update', 'reasoning': {'effort': 'low'}},
                                                 'metadata': {'harness_authored_configuration': True}}, True)]:
            with self.subTest(native_model_membership=name):
                self.write([message('A', id='a', internal_chat_message_metadata_passthrough=provenance),
                            message('answer A', 'assistant'),
                            message('<managed_developer_instructions>ROLLED_BACK</managed_developer_instructions>', 'developer', id='context'),
                            filtered, message('B', id='b', internal_chat_message_metadata_passthrough=provenance),
                            message('answer B', 'assistant'), event('thread_rolled_back', num_turns=1)])
                result = self.adapter.transcript_view(self.session)
                self.assertEqual(result.source.state, 'supported')
                self.assertEqual(3 in result.history, retained)
        with self.subTest(ambiguous_model_membership=True):
            self.write([message('A', id='a', internal_chat_message_metadata_passthrough=provenance),
                        message('answer A', 'assistant'),
                        message('<managed_developer_instructions>context</managed_developer_instructions>', 'developer'),
                        {'type': 'response_item', 'payload': {'type': 'configuration_update', 'reasoning': {'effort': 'low'}},
                         'metadata': {'harness_authored_configuration': False}},
                        message('B', id='b', internal_chat_message_metadata_passthrough=provenance),
                        event('thread_rolled_back', num_turns=1)])
            self.path.write_text(self.path.read_text().replace('"harness_authored_configuration": false',
                '"harness_authored_configuration": false, "harness_authored_configuration": true'))
            self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        with self.subTest(unknown_legacy_context_echo=True):
            opaque = '<opaque_context>unknown</opaque_context>'
            self.write([message('A', id='a', internal_chat_message_metadata_passthrough=provenance),
                        message('answer A', 'assistant'), message(opaque, id='unknown'),
                        event('user_message', message=opaque),
                        message('B', id='b', internal_chat_message_metadata_passthrough=provenance),
                        event('thread_rolled_back', num_turns=1)])
            self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        for barrier in (False, True):
            with self.subTest(context_prefix_or_barrier=barrier):
                context_row = message('<managed_developer_instructions>context</managed_developer_instructions>', 'developer', id='context')
                if barrier:
                    self.write([message('A', id='a', internal_chat_message_metadata_passthrough=provenance),
                                context_row, {'type': 'response_item', 'payload': {'type': 'function_call_output', 'id': 'barrier', 'call_id': 'call', 'output': 'ok'}},
                                message('B', id='b', internal_chat_message_metadata_passthrough=provenance),
                                event('thread_rolled_back', num_turns=1)])
                    context_index = 2
                else:
                    self.write([context_row, message('A', id='a', internal_chat_message_metadata_passthrough=provenance),
                                event('thread_rolled_back', num_turns=1)])
                    context_index = 1
                result = self.adapter.transcript_view(self.session)
                self.assertEqual(result.source.state, 'supported')
                self.assertIn(context_index, result.history)
        self.write([event('task_started', turn_id='a'), message('first', id='first'),
                    event('user_message', message='first'), event('task_complete', turn_id='a'),
                    event('task_started', turn_id='b'), message('uncertain', id='unknown'),
                    event('thread_rolled_back', num_turns=1)])
        uncertain = self.adapter.transcript_view(self.session)
        self.assertEqual(uncertain.source.state, 'error')
        self.assertEqual(uncertain.rows, ())
        self.assertIsNone(self.adapter.transcript_readable(self.session))

    def test_body_reads_refuse_rewrites_and_copies_must_agree_on_every_preceding_byte(self):
        """An earlier same-length edit or divergent archive must never mix bodies with an old cursor's facts."""
        self.write([message('first', id='one'), message('second', 'assistant', id='two')])
        original = self.path.read_bytes()
        view = self.adapter.transcript_view(self.session)
        self.path.write_bytes(original + (json.dumps(event('task_complete', turn_id='t')) + '\n').encode())
        appended = self.adapter.transcript_view(self.session)
        self.assertEqual([fact.position for fact in appended.rows[:3]], [fact.position for fact in view.rows])
        result = self.adapter.transcript_bodies(self.session, view, [1], max_bytes=1 << 20)
        result[0]['payload']['role'] = 'changed'
        self.assertEqual(self.adapter.transcript_bodies(self.session, view, [1], max_bytes=1 << 20)[0]['payload']['role'], 'user')
        self.assertEqual(self.adapter.transcript_bodies(self.session, view, [1], max_bytes=1), [None])
        real_open = t.open_regular
        for phase, changed in [('replace before body open', True), ('truncate after selected read', True),
                               ('change selected bytes before body open', True), ('append before body open', False),
                               ('append after selected read', False), ('rewrite plus growth before body open', True),
                               ('rewrite plus growth after selected read', True)]:
            with self.subTest(phase):
                tail = (json.dumps(event('future')) + '\n').encode() if phase == 'truncate after selected read' else b''
                self.path.write_bytes(original + tail)
                calls = []
                def append_row():
                    with self.path.open('ab') as handle:
                        handle.write((json.dumps(event('future')) + '\n').encode())
                class ChangingBody:
                    def __init__(inner, handle):
                        inner.handle = handle
                        inner.changed = False
                    def __enter__(inner):
                        return inner
                    def __exit__(inner, *args):
                        inner.handle.close()
                    def fileno(inner):
                        return inner.handle.fileno()
                    def seek(inner, offset):
                        return inner.handle.seek(offset)
                    def read(inner, size):
                        line = inner.handle.read(size)
                        if not inner.changed:
                            inner.changed = True
                            if phase == 'truncate after selected read':
                                self.path.write_bytes(original)
                            elif phase == 'rewrite plus growth after selected read':
                                self.path.write_bytes(original.replace(b'first', b'other'))
                                append_row()
                            else:
                                append_row()
                        return line
                def change_during_read(path):
                    calls.append(path)
                    if len(calls) == 2:
                        if phase == 'replace before body open':
                            self.path.rename(self.path.with_name('replaced-kept'))
                            self.path.write_bytes(original)
                        elif phase == 'change selected bytes before body open':
                            self.path.write_bytes(original.replace(b'second', b'other!'))
                        elif phase == 'rewrite plus growth before body open':
                            self.path.write_bytes(original.replace(b'first', b'other'))
                            append_row()
                        elif phase == 'append before body open':
                            append_row()
                        else:
                            return ChangingBody(real_open(path))
                    return real_open(path)
                with patch.object(t, 'open_regular', change_during_read):
                    if changed:
                        with self.assertRaises(TranscriptRowsChanged):
                            self.adapter.transcript_bodies(self.session, view, [2], max_bytes=1 << 20)
                    else:
                        result = self.adapter.transcript_bodies(self.session, view, [2], max_bytes=1 << 20)
                        self.assertEqual(result[0]['payload']['id'], 'two')
                self.path.with_name('replaced-kept').unlink(missing_ok=True)
        self.path.write_bytes(original)
        self.path.write_bytes(self.path.read_bytes().replace(b'first', b'other'))
        with self.assertRaises(TranscriptRowsChanged):
            self.adapter.transcript_bodies(self.session, view, [2], max_bytes=1 << 20)
        archive = self.home / 'archived_sessions' / self.path.name
        archive.parent.mkdir()
        archive.write_bytes(original)
        os.utime(archive, (1, 1))
        self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        self.assertIsNone(self.adapter.transcript_readable(self.session))
        self.path.write_bytes(original + b'{"unfinished":')
        view = self.adapter.transcript_view(self.session)
        self.assertEqual((view.source.state, len(view.rows)), ('supported', 3))
        self.assertEqual(self.adapter.transcript_bodies(self.session, view, [2], max_bytes=1 << 20)[0]['payload']['id'], 'two')

        for name, damaged in [
                ('duplicate output', b'{"type":"response_item","payload":{"type":"function_call_output","call_id":"call","output":"first","output":"second"}}\n'),
                ('missing record type', b'{"unexpected":"object"}\n')]:
            with self.subTest(unreadable_body=name):
                self.path.write_bytes(original + damaged)
                damaged_view = self.adapter.transcript_view(self.session)
                self.assertEqual(damaged_view.source.state, 'supported')
                self.assertEqual((damaged_view.rows[-1].kind, damaged_view.rows[-1].label), ('unreadable', 'meta'))
                self.assertEqual(self.adapter.transcript_bodies(self.session, damaged_view, [3], max_bytes=1 << 20), [None])

    def test_compressed_copies_preserve_logical_rows_bodies_and_plain_precedence(self):
        """Cold compressed history must preserve exact identity/cursors without writing a sibling or selecting stale compressed bytes over plain data."""
        from codex_compression import compress
        compressed = self.path.with_suffix('.jsonl.zst')
        for shape in ('single frame', 'multiple frames', 'empty frame', 'unknown size', 'native window', 'checksum', 'skippable', 'plain sibling'):
            with self.subTest(shape=shape):
                self.write([message('compressed prompt'), event('user_message', message='compressed prompt')])
                if shape == 'native window':
                    with self.path.open('a') as handle:
                        handle.write(json.dumps(event('padding', content='p' * (3 << 20))) + '\n')
                data = self.path.read_bytes()
                plain_view = self.adapter.transcript_view(self.session)
                if shape == 'multiple frames':
                    header, rest = data.split(b'\n', 1)
                    encoded = compress(header + b'\n') + compress(rest)
                else:
                    encoded = compress(data, content_size=shape != 'unknown size', checksum=shape == 'checksum')
                    if shape == 'skippable':
                        encoded = b'\x50\x2a\x4d\x18\x04\x00\x00\x00note' + encoded
                    if shape == 'empty frame':
                        encoded = compress(b'') + encoded
                compressed.write_bytes(encoded)
                if shape == 'plain sibling':
                    self.write([message('plain wins'), event('user_message', message='plain wins')])
                else:
                    self.path.unlink()
                before = {p.name: p.read_bytes() for p in self.path.parent.iterdir()}
                view = self.adapter.transcript_view(self.session)
                self.assertEqual(view.source.state, 'supported')
                if shape != 'plain sibling':
                    self.assertEqual(view.rows, plain_view.rows, 'Compression cannot change logical positions or row proofs')
                self.assertTrue(self.adapter.transcript_readable(self.session))
                self.assertEqual(self.adapter.navigation_target(self.session, self.native).state, 'ok')
                bodies = self.adapter.transcript_bodies(self.session, view, [1, 0, 2], max_bytes=1 << 20)
                self.assertEqual(bodies[0]['payload']['content'][0]['text'],
                                 'plain wins' if shape == 'plain sibling' else 'compressed prompt')
                self.assertEqual(bodies[1]['payload']['id'], self.native)
                self.assertEqual(view.rows[1].label, 'prompt')
                self.assertEqual({p.name: p.read_bytes() for p in self.path.parent.iterdir()}, before)
                compressed.unlink()

        self.write([message('old compressed')])
        compressed.write_bytes(compress(self.path.read_bytes()))
        self.path.unlink()
        real_open = t.open_regular
        def publish_plain(path):
            handle = real_open(path)
            self.write([message('new plain')])
            return handle
        with self.subTest(concurrent='plain winner appears'):
            with patch.object(t, 'open_regular', side_effect=publish_plain):
                self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
            latest = self.adapter.transcript_view(self.session)
            self.assertEqual(latest.source.state, 'supported')
            self.assertEqual(self.adapter.transcript_bodies(self.session, latest, [1], max_bytes=1 << 20)[0]
                             ['payload']['content'][0]['text'], 'new plain')
        self.path.unlink(missing_ok=True)
        class Expired(ValueError):
            pass
        class Budget:
            inside_header = False
            def check(self):
                if self.inside_header:
                    raise Expired('cancelled while checking compressed header')
        budget, real_header = Budget(), t._header
        def header_budget(*args, **kwargs):
            budget.inside_header = True
            try:
                return real_header(*args, **kwargs)
            finally:
                budget.inside_header = False
        with self.subTest(cancellation='compressed header'), patch.object(t, '_header', side_effect=header_budget):
            with self.assertRaises(Expired):
                self.adapter.transcript_view(self.session, reader=budget)

        self.write([message('keep')])
        encoded = compress(self.path.read_bytes())
        self.path.unlink()
        for name, data in (('truncated header', encoded[:5]), ('truncated data', encoded[:-1]),
                           ('corrupt header', b'bad!' + encoded[4:]),
                           ('unfinished trailing header', encoded + b'\x28\xb5'),
                           ('incomplete second frame', encoded + encoded[:-1])):
            with self.subTest(damaged_compression=name):
                compressed.write_bytes(data)
                self.assertEqual(self.adapter.session_storage_evidence(self.session, str(self.home)).state, 'unknown')
                self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
                self.assertIsNone(self.adapter.transcript_readable(self.session))
        compressed.write_bytes(encoded)
        with patch('multiverse.harnesses.codex.rollout.import_module', side_effect=ImportError('missing')):
            view = self.adapter.transcript_view(self.session)
            self.assertEqual(view.source.state, 'error')
            self.assertIn('Zstandard support is missing', view.source.reason)
            self.assertIn('Zstandard support is missing',
                          self.adapter.session_storage_evidence(self.session, str(self.home)).reason)
            self.assertIn('Zstandard support is missing', self.adapter.navigation_target(self.session, self.native).reason)
            self.write([message('plain needs no codec')])
            self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'supported')
            self.path.unlink()
        for setting in ('MAX_BLOCKS', 'MAX_LINE', 'MAX_BYTES', 'MAX_INPUT'):
            with self.subTest(bounded=setting), patch('multiverse.harnesses.codex.rollout.' + setting, 0):
                self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        with patch.object(t, 'MAX_ROWS', 0):
            t._indexes.clear()
            self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        from multiverse.harnesses.codex import rollout
        with self.assertRaises(rollout.RolloutError):
            with rollout.open_rollout(compressed) as handle:
                handle.read(0)  # Finish frame-header validation before exercising the encoded-read budget.
                with patch.object(rollout, 'MAX_INPUT', 0):
                    handle.read(1)
        self.write([message('x' * 2048)])
        compressed.write_bytes(compress(self.path.read_bytes()))
        self.path.unlink()
        with patch('multiverse.harnesses.codex.rollout.MAX_LINE', 1024):
            t._indexes.clear()
            self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        # A valid unknown-size frame can ask for an unreasonable decoder window.
        self.write([message('window')])
        oversized_window = bytearray(compress(self.path.read_bytes(), content_size=False))
        self.path.unlink()
        self.assertFalse(oversized_window[4] & 32)
        oversized_window[5] = (27 - 10) << 3  # 128 MiB, above the bounded reader's 64 MiB.
        compressed.write_bytes(oversized_window)
        self.assertEqual(self.adapter.session_storage_evidence(self.session, str(self.home)).state, 'unknown')
        compressed.write_bytes(encoded)
        self.path.symlink_to(self.path.with_name('missing-plain-target'))
        # Native preference cannot depend on which sibling scandir happens to yield first.
        scandir = os.scandir
        class CompressedFirst:
            def __init__(self, path):
                self.entries = scandir(path)
            def __enter__(self):
                self.entries.__enter__()
                return self
            def __iter__(self):
                return iter(sorted(self.entries, key=lambda item: not item.name.endswith('.zst')))
            def __exit__(self, *args):
                return self.entries.__exit__(*args)
        with patch('multiverse.harnesses.codex.storage.os.scandir', side_effect=CompressedFirst):
            self.assertEqual(self.adapter.session_storage_evidence(self.session, str(self.home)).state, 'unknown')
        self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')

    def test_scope_is_recorded_storage_and_uncertainty_never_becomes_absence(self):
        """A missing, linked, damaged or foreign rollout cannot be read through another caller's home or thread."""
        self.write([message('go')])
        for name, session, environment, state in [
                ('recorded', self.session, {'CODEX_HOME': '/elsewhere'}, 'supported'),
                ('unrecorded outside', replace(self.session, home=None), {'CODEX_HOME': str(self.home)}, 'unavailable'),
                ('unrecorded inside', replace(self.session, home=None), {'MV2_AGENT_DIR': str(self.work / 'agent'), 'CODEX_HOME': str(self.home)}, 'supported'),
                ('another thread', replace(self.session, native_id=str(uuid4())), {}, 'unavailable')]:
            with self.subTest(name), patch.dict(os.environ, environment):
                self.assertEqual(self.adapter.transcript_view(session).source.state, state)
                navigation = self.adapter.navigation_target(session, session.native_id)
                self.assertEqual(navigation.state, 'ok' if state == 'supported' and session.home else 'unavailable')
        self.assertIs(self.adapter.transcript_readable(self.session), True)
        self.assertIs(self.adapter.transcript_readable(replace(self.session, native_id=str(uuid4()))), False)
        # Reviewer probes use actual line bytes and schedule writes between reads.
        proven = message('own', internal_chat_message_metadata_passthrough={
            'content_item_metadata': [{'provenance': {'type': 'user'}}]})
        self.write([proven])
        own_view = self.adapter.transcript_view(self.session)
        foreign_header = {'type': 'session_meta', 'payload': {'id': str(uuid4())}}
        with self.path.open('a') as handle:
            handle.write(json.dumps(foreign_header) + '\n')
            handle.write(json.dumps(message('foreign', internal_chat_message_metadata_passthrough={
                'content_item_metadata': [{'provenance': {'type': 'user'}}]})) + '\n')
        self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        self.assertIsNone(self.adapter.transcript_readable(self.session))
        self.assertEqual(self.adapter.navigation_target(self.session, self.native).state, 'unavailable')
        with self.assertRaises(TranscriptRowsChanged):
            self.adapter.transcript_bodies(self.session, own_view, [1], max_bytes=1 << 20)
        for malformed in ('duplicate identity', 'duplicate record type'):
            with self.subTest(late_header=malformed):
                self.write([proven])
                own_view = self.adapter.transcript_view(self.session)
                foreign = str(uuid4())
                if malformed == 'duplicate identity':
                    late = ('{"type":"session_meta","payload":{"id":"' + foreign
                            + '","id":"' + self.native + '"}}\n')
                else:
                    late = ('{"type":"session_meta","type":"response_item","payload":'
                            '{"type":"message","role":"user","id":"' + self.native + '"}}\n')
                with self.path.open('a') as handle:
                    handle.write(late)
                    handle.write(json.dumps(proven) + '\n')
                self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
                self.assertIsNone(self.adapter.transcript_readable(self.session))
                self.assertEqual(self.adapter.navigation_target(self.session, self.native).state, 'unavailable')
                with self.assertRaises(TranscriptRowsChanged):
                    self.adapter.transcript_bodies(self.session, own_view, [1], max_bytes=1 << 20)
        real_open = t.open_regular
        for change in ('foreign rewrite', 'same-thread mixed rewrite', 'append', 'append after scan'):
            with self.subTest(change):
                header = {'type': 'session_meta', 'payload': {'id': self.native, 'marker': 'old'}}
                raw_header = json.dumps(header).encode()
                raw_header += b' ' * (8191 - len(raw_header)) + b'\n'
                own_body = (json.dumps(proven) + '\n').encode()
                original_scan = raw_header + own_body
                if change.startswith('append'):
                    replacement = original_scan + (json.dumps(event('future')) + '\n').encode()
                else:
                    header['payload']['id'] = str(uuid4()) if change == 'foreign rewrite' else self.native
                    header['payload']['marker'] = 'new'
                    replacement = json.dumps(header).encode()
                    replacement += b' ' * (8191 - len(replacement)) + b'\n'
                    replacement += (json.dumps(message('foreign', internal_chat_message_metadata_passthrough={
                        'content_item_metadata': [{'provenance': {'type': 'user'}}]})) + '\n').encode()
                self.path.write_bytes(original_scan)
                class ChangingScan:
                    def __init__(inner, handle):
                        inner.handle = handle
                    def __enter__(inner):
                        return inner
                    def __exit__(inner, *args):
                        inner.handle.close()
                    def fileno(inner):
                        return inner.handle.fileno()
                    def __iter__(inner):
                        yield inner.handle.readline()
                        if change != 'append after scan':
                            self.path.write_bytes(replacement)
                        yield from inner.handle
                        if change == 'append after scan':
                            self.path.write_bytes(replacement)
                with patch.object(t, 'open_regular', side_effect=lambda path: ChangingScan(real_open(path))):
                    view = self.adapter.transcript_view(self.session)
                self.assertEqual(view.source.state, 'supported' if change.startswith('append') else 'error')
                if change.startswith('append'):
                    self.assertEqual(len(self.adapter.transcript_view(self.session).rows), 3)
                else:
                    self.assertEqual(view.rows, ())
        self.write([message('go')])
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(self.native.encode(), str(uuid4()).encode()))
        self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        header, rest = original.split(b'\n', 1)
        duplicate = ('{"type":"session_meta","payload":{"id":"' + self.native
                     + '","id":"' + self.native + '"}}\n').encode()
        self.path.write_bytes(duplicate + rest)
        self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        self.path.write_bytes(original)
        real_header = t._header
        def moved_after_header(path, native, **kwargs):
            result = real_header(path, native, **kwargs)
            self.path.write_bytes(original.replace(self.native.encode(), str(uuid4()).encode()))
            return result
        with patch.object(t, '_header', moved_after_header):
            self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        self.path.write_bytes(original)
        header, rest = original.split(b'\n', 1)
        value = json.loads(header)
        value['payload']['history_base'] = {'thread_id': str(uuid4()), 'end_ordinal_exclusive': 12}
        self.path.write_bytes(json.dumps(value).encode() + b'\n' + rest)
        # A lone child is a segment, not a complete transcript even without a conflicting copy.
        self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        self.assertIsNone(self.adapter.transcript_readable(self.session))
        self.path.write_bytes(original)
        compressed = self.path.with_suffix('.jsonl.zst')
        self.path.rename(compressed)
        self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        self.assertIsNone(self.adapter.transcript_readable(self.session))
        compressed.rename(self.path)
        other = self.path.with_name('other')
        self.path.rename(other)
        self.path.symlink_to(other)
        self.assertEqual(self.adapter.transcript_view(self.session).source.state, 'error')
        self.path.unlink()
        other.rename(self.path)
        # Reader cancellation must propagate, including core's ValueError subclass.
        class Expired(ValueError):
            pass
        class Reader:
            calls = 0
            def check(self):
                self.calls += 1
                if self.calls > 1:
                    raise Expired()
        with self.assertRaises(Expired):
            self.adapter.transcript_view(self.session, reader=Reader())
