"""Native control protocol plans, with no provider or terminal assumptions."""
from dataclasses import replace
from pathlib import Path
import unittest
from uuid import uuid4

from multiverse.harnesses.codex.protocol import check_connection, send_message, get_delivery
from multiverse.harnesses.types import SessionHandle, Message


class CodexControlTest(unittest.TestCase):
    def setUp(self):
        self.ctx = SessionHandle('agent', 'codex', '/tmp', native_id=str(uuid4()))
        self.message = Message(str(uuid4()), 'literal `false` $(false)')
        self.requests = []

    def run_plan(self, operation, *, state='active', overrides=None):
        overrides = overrides or {}
        answers = {
            'initialize': {'userAgent': 'test/0.154.0'},
            'thread/read': {'thread': {'id': self.ctx.native_id, 'cwd': '/tmp', 'status': {'type': state}}},
            'thread/turns/list': {'data': [{'id': 'exact-turn', 'status': 'inProgress'}]},
            'thread/queue/list': {'data': []},
            'thread/queue/add': {'queuedSubmission': {'id': 'queue-item', 'clientUserMessageId': self.message.id}},
            'turn/steer': {'turnId': 'exact-turn'},
        }
        generator = check_connection(self.ctx) if operation is None else send_message(self.ctx, self.message, operation)
        request = next(generator)
        while True:
            self.requests.append(request)
            response = overrides.get(request.method, {'result': answers[request.method]})
            try:
                request = generator.send(response)
            except StopIteration as done:
                return done.value

    def test_urgent_requires_exact_active_turn_and_stable_message(self):
        result = self.run_plan('steer')
        self.assertEqual(result.state, 'native_queued')
        last = self.requests[-1]
        self.assertEqual(last.method, 'turn/steer')
        self.assertEqual(last.params['expectedTurnId'], 'exact-turn')
        self.assertEqual(last.params['threadId'], self.ctx.native_id)
        self.assertEqual(last.params['clientUserMessageId'], self.message.id)
        self.assertIn('literal `false` $(false)', last.params['input'][0]['text'])

    def test_normal_and_idle_urgent_use_native_queue_without_steering(self):
        for operation, state in [('follow-up', 'active'), ('follow-up', 'idle'), ('steer', 'idle')]:
            with self.subTest(operation=operation, state=state):
                self.requests.clear()
                self.assertEqual(self.run_plan(operation, state=state).state, 'native_queued')
                self.assertEqual(self.requests[-1].method, 'thread/queue/add')
                self.assertNotIn('turn/steer', [r.method for r in self.requests])

    def test_unloaded_wrong_identity_and_workspace_never_receive(self):
        cases = [({'id': self.ctx.native_id, 'cwd': '/tmp', 'status': {'type': 'notLoaded'}}, 'unavailable'),
                 ({'id': str(uuid4()), 'cwd': '/tmp', 'status': {'type': 'active'}}, 'error'),
                 ({'id': self.ctx.native_id, 'cwd': '/somewhere-else', 'status': {'type': 'active'}}, 'error')]
        for thread, expected in cases:
            self.requests.clear()
            result = self.run_plan('steer', overrides={'thread/read': {'result': {'thread': thread}}})
            self.assertEqual(result.state, expected)
            self.assertEqual([r.method for r in self.requests], ['initialize', 'thread/read'])

    def test_steering_race_is_not_retried_or_submitted_as_fresh_turn(self):
        result = self.run_plan('steer', overrides={'turn/steer': {'error': {'code': -32600, 'message': 'no active turn to steer'}}})
        self.assertEqual(result.state, 'error')
        self.assertIn('no active turn', result.reason)
        self.assertEqual(len([r for r in self.requests if r.method == 'turn/steer']), 1)
        self.assertNotIn('turn/start', [r.method for r in self.requests])
        self.assertNotIn('thread/resume', [r.method for r in self.requests])

    def test_unknown_protocol_and_runtime_failure_are_distinct(self):
        for code, expected in [(-32601, 'unavailable'), (-32603, 'error')]:
            result = self.run_plan(None, overrides={'thread/queue/list': {'error': {'code': code, 'message': 'test failure'}}})
            self.assertEqual(result.state, expected)

    def test_wrong_receipt_is_ambiguous_and_missing_message_id_never_sends(self):
        result = self.run_plan('follow-up', overrides={'thread/queue/add': {'result': {'queuedSubmission': {'id': 'item', 'clientUserMessageId': str(uuid4())}}}})
        self.assertEqual(result.state, 'unknown')
        self.message = replace(self.message, id=None)
        self.requests.clear()
        self.assertEqual(self.run_plan('steer').state, 'error')
        self.assertEqual([r.method for r in self.requests], ['initialize', 'thread/read'])

    def test_receipt_lookup_correlates_queue_and_history_and_never_resends(self):
        for queue, items, expected in (
            ([{'id': 'receipt', 'clientUserMessageId': self.message.id}], [], False),
            ([], [{'type': 'userMessage', 'clientId': self.message.id}], True),
            ([], [{'type': 'userMessage', 'clientId': 'another-message'}], None),
        ):
            replies = {'initialize': {}, 'thread/read': {'thread': {'id': self.ctx.native_id,
                'cwd': '/tmp', 'status': {'type': 'active'}}}, 'thread/queue/list': {'data': queue},
                'thread/turns/list': {'data': [{'id': 'turn', 'items': items}]}}
            generator = get_delivery(self.ctx, self.message.id)
            request = next(generator)
            while True:
                self.assertNotIn(request.method, ('turn/steer', 'thread/queue/add'))
                try:
                    request = generator.send({'result': replies[request.method]})
                except StopIteration as done:
                    result = done.value
                    break
            if expected is None:
                self.assertEqual(result.state, 'unknown')
            else:
                self.assertEqual(result.state, 'native_queued')
                self.assertIs(result.value['transcript_confirmed'], expected)

    def test_malformed_native_shapes_are_diagnosable(self):
        thread = {'id': self.ctx.native_id, 'cwd': '/tmp', 'status': None}
        cases = [(None, 'thread/read', {'thread': None}),
                 (None, 'thread/read', {'thread': thread}),
                 (None, 'thread/queue/list', {'data': None}),
                 ('steer', 'thread/turns/list', {'data': [None]}),
                 ('follow-up', 'thread/queue/add', {'queuedSubmission': None})]
        for operation, method, value in cases:
            with self.subTest(method=method, value=value), self.assertRaisesRegex(ValueError, 'Native RPC'):
                self.run_plan(operation, overrides={method: {'result': value}})
        for error in [None, [], 'bad', {'code': [], 'message': 'bad'}, {'code': 0, 'message': None}]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, 'Native RPC'):
                self.run_plan(None, overrides={'initialize': {'error': error}})


if __name__ == '__main__':
    unittest.main()
