"""Native control protocol plans, with no provider or terminal assumptions."""
from dataclasses import replace
import unittest
from uuid import uuid4

from multiverse.harnesses.codex.protocol import check_connection, send_message, get_delivery, stop_session
from multiverse.harnesses.types import SessionHandle, Message


class CodexControlTest(unittest.TestCase):
    def setUp(self):
        self.ctx = SessionHandle('agent', 'codex', '/tmp', native_id=str(uuid4()))
        self.message = Message(str(uuid4()), 'literal `false` $(false)', framed='Message from reviewer:\nliteral `false` $(false)')
        self.ctx = replace(self.ctx, queue_state={'native_session_id': self.ctx.native_id,
            'restart': {'id': self.message.id, 'state': 'claimed'}})
        self.requests = []

    def run_plan(self, operation, *, state='active', overrides=None):
        overrides = overrides or {}
        answers = {
            'initialize': {'userAgent': 'test/0.154.0'},
            'thread/read': {'thread': {'id': self.ctx.native_id, 'cwd': '/tmp', 'status': {'type': state}}},
            'thread/turns/list': {'data': [{'id': 'exact-turn', 'status': 'inProgress'}]},
            'thread/queue/list': {'data': []},
            'thread/queue/add': {'queuedSubmission': {'id': 'queue-item', 'clientUserMessageId': self.message.id}},
            'thread/queue/start': {'turn': {'id': 'queue-turn', 'status': 'inProgress'}},
            'turn/steer': {'turnId': 'exact-turn'},
            'turn/interrupt': {}, 'thread/backgroundTerminals/clean': {},
            'thread/queue/delete': {'deleted': True},
        }
        generator = (check_connection(self.ctx) if operation is None else
                     stop_session(self.ctx) if operation == 'stop' else send_message(self.ctx, self.message, operation))
        request = next(generator)
        while True:
            self.requests.append(request)
            response = overrides.get(request.method, {'result': answers[request.method]})
            try:
                request = generator.send(response)
            except StopIteration as done:
                return done.value

    def test_send_plan_per_operation_and_thread_state(self):
        """Each operation, thread state and existing queue maps to the exact Codex RPC sequence (steer, queue, wake);
        fragile because steer needs expectedTurnId and the queue API is experimental, and a wrong plan delivers at the
        wrong time or unpauses a queue on receipt retry with no error."""
        own = {'id': 'queue-item', 'clientUserMessageId': self.message.id}
        other = {'id': 'other', 'clientUserMessageId': 'another-message'}
        read = ['initialize', 'thread/read']
        idle_add = read + ['thread/queue/add', 'thread/queue/list']
        for operation, state, queue, methods, wake in (
                ('steer', 'active', None, read + ['thread/turns/list', 'turn/steer'], None),
                ('follow-up', 'active', None, read + ['thread/queue/add'], None),
                ('follow-up', 'idle', None, idle_add, None),
                ('steer', 'idle', None, idle_add, None),
                ('steer', 'idle', {'data': [own]}, idle_add + ['thread/queue/start'], 'started'),
                ('follow-up', 'idle', {'data': [own]}, idle_add + ['thread/queue/start'], 'started'),
                ('follow-up', 'idle', {'data': [other]}, idle_add + ['thread/queue/start'], 'started'),
                ('follow-up', 'idle', {'data': [own], 'nextCursor': 'more'}, idle_add + ['thread/queue/start'], 'started')):
            with self.subTest(operation=operation, state=state, queue=queue):
                self.requests.clear()
                overrides = {'thread/queue/list': {'result': queue}} if queue else None
                result = self.run_plan(operation, state=state, overrides=overrides)
                self.assertEqual(result.state, 'native_queued')
                self.assertEqual([r.method for r in self.requests], methods)
                submitted = next(r for r in self.requests if r.method in ('turn/steer', 'thread/queue/add'))
                self.assertEqual(submitted.params['threadId'], self.ctx.native_id)
                self.assertEqual(submitted.params['clientUserMessageId'], self.message.id)
                self.assertEqual(submitted.params['input'][0]['text'], self.message.framed)
                self.assertNotIn(self.message.id, submitted.params['input'][0]['text'])
                if submitted.method == 'turn/steer':
                    self.assertEqual(submitted.params['expectedTurnId'], 'exact-turn')
                if wake == 'started':
                    self.assertEqual(result.value['queue_started_turn_id'], 'queue-turn')
                    self.assertEqual(self.requests[-1].params,
                        {'threadId': self.ctx.native_id})
                elif wake == 'other work':
                    self.assertIn('other queued work', result.reason)

        for restart, starts in ((None, 0), ({'id': 'other', 'state': 'claimed'}, 0),
                                ({'id': self.message.id, 'state': 'pending'}, 0),
                                ({'id': self.message.id, 'state': 'claimed'}, 1)):
            with self.subTest(paused_queue_restart=restart):
                state = {'native_session_id': self.ctx.native_id, 'state': 'paused'}
                if restart is not None:
                    state['restart'] = restart
                self.ctx = replace(self.ctx, queue_state=state)
                self.requests.clear()
                self.assertEqual(self.run_plan('follow-up', state='idle', overrides={
                    'thread/queue/list': {'result': {'data': [other]}}}).state, 'native_queued')
                methods = [r.method for r in self.requests]
                self.assertEqual(methods.count('thread/queue/add'), 1)
                self.assertEqual(methods.count('thread/queue/start'), starts)
                if starts:
                    self.assertEqual(self.requests[-1].params, {'threadId': self.ctx.native_id})

    def test_failure_after_acceptance_is_never_resent(self):
        """Every failure after Codex accepted a message is reported without a second submission; a break goes unnoticed
        because the agent just sees the message twice."""
        message = self.message
        queued = {'thread/queue/list': {'result': {'data': [{'id': 'queue-item', 'clientUserMessageId': message.id}]}}}
        for name, operation, state, overrides, message_id, expected, reason, submissions in (
                ('queue start races an active turn', 'follow-up', 'idle', {**queued, 'thread/queue/start': {
                    'error': {'code': -32600, 'message': 'thread already has an active or pending turn'}}},
                 message.id, 'native_queued', 'do not resend', 1),
                ('queue start unknown', 'follow-up', 'idle', {**queued, 'thread/queue/start': {
                    'error': {'code': -32601, 'message': 'unknown method'}}}, message.id, 'native_queued',
                 'do not resend', 1),
                ('queue start without a turn', 'follow-up', 'idle', {**queued, 'thread/queue/start': {'result': {}}},
                 message.id, 'native_queued', 'do not resend', 1),
                ('steering race', 'steer', 'active', {'turn/steer': {
                    'error': {'code': -32600, 'message': 'no active turn to steer'}}}, message.id, 'error',
                 'no active turn', 1),
                ('wrong receipt', 'follow-up', 'active', {'thread/queue/add': {'result': {'queuedSubmission': {
                    'id': 'item', 'clientUserMessageId': str(uuid4())}}}}, message.id, 'unknown', None, 1),
                ('no message id', 'steer', 'active', {}, None, 'error', None, 0)):
            with self.subTest(name):
                self.requests.clear()
                self.message = replace(message, id=message_id)
                result = self.run_plan(operation, state=state, overrides=overrides)
                methods = [r.method for r in self.requests]
                self.assertEqual(result.state, expected)
                if reason:
                    self.assertIn(reason, result.reason)
                if expected == 'native_queued':
                    self.assertEqual(result.value['queued_submission_id'], 'queue-item')
                self.assertEqual(methods.count('thread/queue/add') + methods.count('turn/steer'), submissions)
                self.assertNotIn('turn/start', methods)
                self.assertNotIn('thread/resume', methods)
                if not submissions:
                    self.assertEqual(methods, ['initialize', 'thread/read'])

    def test_wrong_target_or_changed_reply_shape_stops_before_mutation(self):
        """A wrong thread or workspace, an unloaded thread, a failing method or a changed Codex reply shape ends the
        plan at that reply, before any further mutating RPC; fragile because Codex reply shapes change between
        versions."""
        def thread(**fields):
            return {'result': {'thread': {'id': self.ctx.native_id, 'cwd': '/tmp', 'status': {'type': 'active'},
                                          **fields}}}
        rows = [('steer', 'thread/read', thread(status={'type': 'notLoaded'}), 'unavailable'),
                ('steer', 'thread/read', thread(id=str(uuid4())), 'error'),
                ('steer', 'thread/read', thread(cwd='/somewhere-else'), 'error'),
                (None, 'thread/queue/list', {'error': {'code': -32601, 'message': 'test failure'}}, 'unavailable'),
                (None, 'thread/queue/list', {'error': {'code': -32603, 'message': 'test failure'}}, 'error'),
                (None, 'thread/read', {'result': {'thread': None}}, ValueError),
                (None, 'thread/read', thread(status=None), ValueError),
                (None, 'thread/queue/list', {'result': {'data': None}}, ValueError),
                ('steer', 'thread/turns/list', {'result': {'data': [None]}}, ValueError),
                ('follow-up', 'thread/queue/add', {'result': {'queuedSubmission': None}}, ValueError)]
        rows += [(None, 'initialize', {'error': error}, ValueError)
                 for error in [None, [], 'bad', {'code': [], 'message': 'bad'}, {'code': 0, 'message': None}]]
        for operation, method, reply, expected in rows:
            with self.subTest(operation=operation, method=method, reply=reply):
                self.requests.clear()
                if expected is ValueError:
                    with self.assertRaisesRegex(ValueError, 'Native RPC'):
                        self.run_plan(operation, overrides={method: reply})
                else:
                    self.assertEqual(self.run_plan(operation, overrides={method: reply}).state, expected)
                self.assertEqual(self.requests[-1].method, method)

    def test_receipt_lookup_correlates_queue_and_history_and_never_resends(self):
        """A receipt is queued or transcript-confirmed only by clientUserMessageId in Codex's queue and turn history,
        and lookup never resends; fragile because it reads Codex's history item shape, and a wrong answer stays
        invisible until someone asks whether a message arrived."""
        for queue, items, expected, paused in (
            ([{'id': 'receipt', 'clientUserMessageId': self.message.id}], [], False, False),
            ([{'id': 'receipt', 'clientUserMessageId': self.message.id}], [], False, True),
            ([], [{'type': 'userMessage', 'clientId': self.message.id}], True, False),
            ([], [{'type': 'userMessage', 'clientId': 'another-message'}], None, False),
        ):
            replies = {'initialize': {}, 'thread/read': {'thread': {'id': self.ctx.native_id,
                'cwd': '/tmp', 'status': {'type': 'active'}}}, 'thread/queue/list': {'data': queue},
                'thread/turns/list': {'data': [{'id': 'turn', 'items': items}]}}
            session = replace(self.ctx, queue_state={'native_session_id': self.ctx.native_id, 'state': 'paused'}) if paused else self.ctx
            generator = get_delivery(session, self.message.id)
            request = next(generator)
            while True:
                self.assertNotIn(request.method, ('turn/steer', 'thread/queue/add'))
                try:
                    request = generator.send({'result': replies[request.method]})
                except StopIteration as done:
                    result = done.value
                    break
            if expected is False:
                self.assertEqual(bool(result.value.get('paused')), paused)
            if expected is None:
                self.assertEqual(result.state, 'unknown')
            else:
                self.assertEqual(result.state, 'native_queued')
                self.assertIs(result.value['transcript_confirmed'], expected)

    def test_stop_cancels_only_this_threads_queue_and_turn(self):
        """Stop cancels only this thread's queue and turn on a shared daemon and never reloads an unloaded thread; a
        break goes unnoticed because it kills or reloads another client's work, which mv2 never sees."""
        queued = {'thread/queue/list': {'result': {'data': [
            {'id': 'queued-item', 'clientUserMessageId': self.message.id}]}}}
        everything = {'cancelled_message_ids': [self.message.id], 'cancelled_submissions': 1, 'interrupted_turns': 1,
                      'background_terminal_cleanup_requested': True}
        for name, state, overrides, expected, value, last in (
                ('exact queue and turn', 'active', queued, 'ok', everything, 'thread/backgroundTerminals/clean'),
                ('unloaded thread', 'notLoaded', {}, 'ok', None, 'thread/read'),
                ('unbounded queue', 'active', {'thread/queue/list': {'result': {'data': [], 'nextCursor': 'more'}}},
                 'error', None, 'thread/queue/list'),
                ('wrong thread', 'active', {'thread/read': {'result': {'thread': {
                    'id': str(uuid4()), 'cwd': '/tmp', 'status': {'type': 'active'}}}}}, 'error', None, 'thread/read'),
                ('interrupt fails', 'active', {**queued, 'turn/interrupt': {
                    'error': {'code': -32603, 'message': 'native failure'}}}, 'error',
                 {'cancelled_message_ids': [self.message.id]}, 'turn/interrupt')):
            with self.subTest(name):
                self.requests.clear()
                result = self.run_plan('stop', state=state, overrides=overrides)
                self.assertEqual(result.state, expected)
                for key, observed in (value or {}).items():
                    self.assertEqual(result.value[key], observed)
                self.assertEqual(self.requests[-1].method, last)
                for request in self.requests[1:]:
                    self.assertEqual(request.params['threadId'], self.ctx.native_id)
                    if request.method == 'turn/interrupt':
                        self.assertEqual(request.params['turnId'], 'exact-turn')


if __name__ == '__main__':
    unittest.main()
