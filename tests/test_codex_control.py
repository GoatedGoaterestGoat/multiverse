"""Native control protocol plans, with no provider or terminal assumptions."""
from dataclasses import replace
from pathlib import Path
import unittest
from uuid import uuid4

from multiverse.harnesses.codex.control import plan
from multiverse.harnesses.types import NativeContext


class CodexControlTest(unittest.TestCase):
    def setUp(self):
        self.ctx = NativeContext({'native_session_id': str(uuid4()), 'workspace': '/tmp'}, message_id=str(uuid4()))
        self.requests = []

    def run_plan(self, operation, *, state='active', overrides=None):
        overrides = overrides or {}
        answers = {
            'initialize': {'userAgent': 'test/0.154.0'},
            'thread/read': {'thread': {'id': self.ctx.properties['native_session_id'], 'cwd': '/tmp', 'status': {'type': state}}},
            'thread/turns/list': {'data': [{'id': 'exact-turn', 'status': 'inProgress'}]},
            'thread/queue/list': {'data': []},
            'thread/queue/add': {'queuedSubmission': {'id': 'queue-item', 'clientUserMessageId': self.ctx.message_id}},
            'turn/steer': {'turnId': 'exact-turn'},
        }
        generator = plan(self.ctx, operation, 'literal `false` $(false)')
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
        self.assertEqual(last.params['threadId'], self.ctx.properties['native_session_id'])
        self.assertEqual(last.params['clientUserMessageId'], self.ctx.message_id)
        self.assertEqual(last.params['input'][0]['text'], 'literal `false` $(false)')

    def test_normal_and_idle_urgent_use_native_queue_without_steering(self):
        for operation, state in [('follow_up', 'active'), ('follow_up', 'idle'), ('steer', 'idle')]:
            with self.subTest(operation=operation, state=state):
                self.requests.clear()
                self.assertEqual(self.run_plan(operation, state=state).state, 'native_queued')
                self.assertEqual(self.requests[-1].method, 'thread/queue/add')
                self.assertNotIn('turn/steer', [r.method for r in self.requests])

    def test_unloaded_wrong_identity_and_workspace_never_receive(self):
        cases = [({'id': self.ctx.properties['native_session_id'], 'cwd': '/tmp', 'status': {'type': 'notLoaded'}}, 'unavailable'),
                 ({'id': str(uuid4()), 'cwd': '/tmp', 'status': {'type': 'active'}}, 'error'),
                 ({'id': self.ctx.properties['native_session_id'], 'cwd': '/somewhere-else', 'status': {'type': 'active'}}, 'error')]
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
        result = self.run_plan('follow_up', overrides={'thread/queue/add': {'result': {'queuedSubmission': {'id': 'item', 'clientUserMessageId': str(uuid4())}}}})
        self.assertEqual(result.state, 'error')
        self.assertIn('unknown', result.reason)
        self.ctx = replace(self.ctx, message_id=None)
        self.requests.clear()
        self.assertEqual(self.run_plan('steer').state, 'error')
        self.assertEqual([r.method for r in self.requests], ['initialize', 'thread/read'])


if __name__ == '__main__':
    unittest.main()
