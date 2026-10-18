"""Continue only already accepted mv2 input; never submit it a second time."""
from collections import deque
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from websockets.asyncio.server import unix_serve

from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import NativeEndpoint, SessionHandle
from multiverse.runtime import rpc


class QueueContinuationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mv2-continue-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.session = SessionHandle('agent', 'codex', str(self.work), state='running',
            native_id='thread', endpoint=NativeEndpoint('unix-websocket', str(self.work / 'rpc.sock')),
            native_mode={'mode': 'bridge'})
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        self.accepted = {'message-1': 'queued-1', 'message-2': 'queued-2'}
        self.items = [{'id': queued, 'clientUserMessageId': message} for message, queued in self.accepted.items()]
        self.script, self.requests, self.errors = deque(), [], []
        self.server = await unix_serve(self.handle, self.session.endpoint.address)

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, connection):
        async for raw in connection:
            request = json.loads(raw)
            if request['method'] == 'initialized':
                continue
            self.requests.append(request)
            try:
                method, params, response = self.script.popleft()
                self.assertEqual(request['method'], method)
                if params is not None:
                    self.assertEqual(request['params'], params)
            except (AssertionError, IndexError) as error:
                self.errors.append(error)
                await connection.close()
                return
            await connection.send(json.dumps({'id': request['id'], **response}))

    def thread(self, **changes):
        return {'thread': {'id': 'thread', 'cwd': str(self.work), 'status': {'type': 'idle'},
                           'canAcceptDirectInput': True, **changes}}

    def response(self, method, params, value):
        return (method, params, {'result': value})

    def read(self, **changes):
        return self.response('thread/read', {'threadId': 'thread', 'includeTurns': False}, self.thread(**changes))

    def prefix(self, **changes):
        return [self.response('initialize', None, {}), self.read(**changes)]

    def page(self, items=None, cursor=None, next_cursor=None, limit=100):
        params = {'threadId': 'thread', 'limit': limit}
        if cursor is not None:
            params['cursor'] = cursor
        return self.response('thread/queue/list', params,
                             {'data': self.items if items is None else items, 'nextCursor': next_cursor})

    def start(self, queued='queued-1'):
        return self.response('thread/queue/start', {'threadId': 'thread', 'queuedSubmissionId': queued},
                             {'turn': {'id': 'turn-1'}})

    async def execute(self, script, accepted=None):
        self.script = deque(script)
        self.requests, self.errors = [], []
        executor = Mock()
        executor.rpc.side_effect = lambda spec, **kwargs: spec
        spec = self.adapter.continue_queued(self.session, self.accepted if accepted is None else accepted, executor)
        self.assertEqual(spec.max_requests, 13)
        self.assertEqual(executor.rpc.call_args.kwargs, {'timeout': 5})
        try:
            observed = await rpc._execute(spec, 3)
        finally:
            if self.errors:
                raise self.errors[0]
        self.assertFalse(self.script, 'Continuation omitted required evidence')
        return observed

    async def test_owned_queue_starts_existing_head_once_without_reordering_or_resubmission(self):
        result = await self.execute(self.prefix() + [self.page(), self.read(),
            self.page(self.items[:1], limit=1), self.start()])
        self.assertEqual(result.state, 'ok')
        self.assertEqual(result.value, {'continued': True, 'queued_submission_id': 'queued-1',
            'client_user_message_id': 'message-1', 'turn_id': 'turn-1'})
        self.assertEqual([r['method'] for r in self.requests].count('thread/queue/start'), 1)
        self.assertFalse(any(r['method'] in ('thread/queue/add', 'thread/queue/delete',
            'thread/queue/reorder', 'thread/resume', 'thread/unsubscribe', 'turn/interrupt') for r in self.requests))

    async def test_empty_native_queue_is_noop_even_when_acceptance_is_saved(self):
        result = await self.execute(self.prefix() + [self.page([])])
        self.assertEqual(result.state, 'ok')
        self.assertEqual(result.value, {'continued': False})

    async def test_initial_busy_unloaded_unknown_or_wrong_thread_never_starts(self):
        for changes in ({'status': {'type': 'active'}}, {'status': {'type': 'notLoaded'}},
                        {'status': {'type': 'future'}}, {'id': 'other'}, {'cwd': '/elsewhere'},
                        {'canAcceptDirectInput': False}):
            with self.subTest(changes=changes):
                result = await self.execute(self.prefix(**changes))
                self.assertNotEqual(result.state, 'ok')
                self.assertNotIn('thread/queue/start', [r['method'] for r in self.requests])

    async def test_foreign_or_uncorrelated_input_preserves_entire_queue(self):
        for item in ({'id': 'foreign', 'clientUserMessageId': 'other'},
                     {'id': 'queued-1', 'clientUserMessageId': 'other'},
                     {'id': 'different', 'clientUserMessageId': 'message-1'}):
            with self.subTest(item=item):
                result = await self.execute(self.prefix() + [self.page([self.items[1], item])])
                self.assertEqual(result.state, 'unavailable')

    async def test_all_pages_must_be_owned_before_starting_native_head(self):
        result = await self.execute(self.prefix() + [self.page(self.items[:1], next_cursor='two'),
            self.page(self.items[1:], cursor='two'), self.read(), self.page(self.items[:1], limit=1), self.start()])
        self.assertEqual(result.state, 'ok')
        foreign = {'id': 'foreign', 'clientUserMessageId': 'outside'}
        result = await self.execute(self.prefix() + [self.page(self.items[:1], next_cursor='two'),
            self.page([foreign], cursor='two')])
        self.assertEqual(result.state, 'unavailable')

    async def test_native_order_wins_over_receipt_order(self):
        result = await self.execute(self.prefix() + [self.page(self.items[::-1]), self.read(),
            self.page(self.items[1:], limit=1), self.start('queued-2')])
        self.assertEqual(result.value['client_user_message_id'], 'message-2')

    async def test_activity_or_head_change_during_check_never_starts(self):
        for changes in ({'status': {'type': 'active'}}, {'id': 'other'}, {'cwd': '/elsewhere'},
                        {'canAcceptDirectInput': False}):
            with self.subTest(changes=changes):
                self.assertNotEqual((await self.execute(self.prefix() + [self.page(), self.read(**changes)])).state, 'ok')
        for head in ([], self.items[1:], [{'id': 'outside', 'clientUserMessageId': 'foreign'}]):
            with self.subTest(head=head):
                result = await self.execute(self.prefix() + [self.page(), self.read(), self.page(head, limit=1)])
                self.assertFalse((result.value or {}).get('continued'))

    async def test_malformed_duplicate_and_cyclic_inventory_fails_closed(self):
        for data in (None, {}, [None], [{'id': ''}], [{'id': 'queued-1'}], [*self.items, self.items[0]]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                await self.execute(self.prefix() + [self.response('thread/queue/list', None, {'data': data})])
        for cursor in ('', 7, False):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                await self.execute(self.prefix() + [self.page([], next_cursor=cursor)])
        with self.assertRaisesRegex(ValueError, 'cursor'):
            await self.execute(self.prefix() + [self.page([], next_cursor='loop'),
                self.page([], cursor='loop', next_cursor='loop')])

    async def test_page_bound_allows_thirteen_requests_but_never_unbounded_scan(self):
        pages = [self.page(self.items if n == 0 else [], cursor=str(n) if n else None,
                          next_cursor=str(n + 1) if n < 7 else None) for n in range(8)]
        result = await self.execute(self.prefix() + pages + [self.read(), self.page(self.items[:1], limit=1), self.start()])
        self.assertEqual(result.state, 'ok')
        self.assertEqual(len(self.requests), 13)
        pages[-1] = self.page([], cursor='7', next_cursor='8')
        with self.assertRaisesRegex(ValueError, 'page'):
            await self.execute(self.prefix() + pages)

    async def test_rpc_error_and_ambiguous_start_do_not_claim_consumption(self):
        error = {'error': {'code': -32600, 'message': 'Thread is busy'}}
        result = await self.execute(self.prefix() + [self.page(), self.read(),
            self.page(self.items[:1], limit=1), ('thread/queue/start', None, error)])
        self.assertEqual(result.state, 'error')
        self.assertFalse((result.value or {}).get('continued'))
        with self.assertRaisesRegex(ValueError, 'turn ID'):
            await self.execute(self.prefix() + [self.page(), self.read(), self.page(self.items[:1], limit=1),
                self.response('thread/queue/start', None, {'turn': {}})])

    async def test_empty_or_unavailable_session_never_opens_rpc(self):
        executor = Mock()
        for session, accepted in ((self.session, {}),
                (replace(self.session, state='starting'), self.accepted),
                (replace(self.session, native_id=None), self.accepted),
                (replace(self.session, native_mode={'mode': 'plain'}), self.accepted),
                (replace(self.session, native_relay={'state': 'lost', 'reason': 'lost'}), self.accepted)):
            with self.subTest(session=session, accepted=accepted):
                result = self.adapter.continue_queued(session, accepted, executor)
                self.assertIn(result.state, ('ok', 'unavailable'))
                executor.rpc.assert_not_called()

    async def test_damaged_receipts_and_oversized_native_page_never_start(self):
        executor = Mock()
        for receipts in (None, [], {'': 'queued'}, {'message': ''}, {'message': None},
                         {'one': 'same', 'two': 'same'}):
            with self.subTest(receipts=receipts):
                result = self.adapter.continue_queued(self.session, receipts, executor)
                self.assertEqual(result.state, 'error')
                executor.rpc.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'limit'):
            await self.execute(self.prefix() + [self.page(self.items[:1] * 101)])
        for cursor in ('', 7, False):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ValueError, 'cursor'):
                await self.execute(self.prefix() + [self.page(), self.read(),
                    self.page(self.items[:1], limit=1, next_cursor=cursor)])
