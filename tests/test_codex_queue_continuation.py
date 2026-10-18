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

    async def test_native_queue_head_starts_only_when_every_item_is_ours(self):
        """mv2 starts Codex's queued head only when every queued item is its own and the thread is still idle; fragile
        because it races user input and Codex's own draining, and a wrong start runs the user's typed input or
        double-submits (60c3cbf)."""
        def started(queued, message):
            return {'continued': True, 'queued_submission_id': queued, 'client_user_message_id': message,
                    'turn_id': 'turn-1'}
        head = [self.read(), self.page(self.items[:1], limit=1)]
        foreign = {'id': 'foreign', 'clientUserMessageId': 'outside'}
        busy = {'error': {'code': -32600, 'message': 'Thread is busy'}}
        rows = [('owned queue', self.prefix() + [self.page(), *head, self.start()], started('queued-1', 'message-1')),
                ('every page owned', self.prefix() + [self.page(self.items[:1], next_cursor='two'),
                    self.page(self.items[1:], cursor='two'), *head, self.start()], started('queued-1', 'message-1')),
                ('native order wins', self.prefix() + [self.page(self.items[::-1]), self.read(),
                    self.page(self.items[1:], limit=1), self.start('queued-2')], started('queued-2', 'message-2')),
                ('empty queue with saved acceptance', self.prefix() + [self.page([])], {'continued': False}),
                ('foreign later page', self.prefix() + [self.page(self.items[:1], next_cursor='two'),
                    self.page([foreign], cursor='two')], 'unavailable'),
                ('start refused', self.prefix() + [self.page(), *head, ('thread/queue/start', None, busy)], 'error'),
                ('start without a turn', self.prefix() + [self.page(), *head,
                    self.response('thread/queue/start', None, {'turn': {}})], ValueError)]
        rows += [(f'initially {changes}', self.prefix(**changes), 'not ok')
                 for changes in ({'status': {'type': 'active'}}, {'status': {'type': 'notLoaded'}},
                                 {'status': {'type': 'future'}}, {'id': 'other'}, {'cwd': '/elsewhere'},
                                 {'canAcceptDirectInput': False})]
        rows += [(f'foreign item {item}', self.prefix() + [self.page([self.items[1], item])], 'unavailable')
                 for item in ({'id': 'foreign', 'clientUserMessageId': 'other'},
                              {'id': 'queued-1', 'clientUserMessageId': 'other'},
                              {'id': 'different', 'clientUserMessageId': 'message-1'})]
        rows += [(f'changed during check {changes}', self.prefix() + [self.page(), self.read(**changes)], 'not ok')
                 for changes in ({'status': {'type': 'active'}}, {'id': 'other'}, {'cwd': '/elsewhere'},
                                 {'canAcceptDirectInput': False})]
        rows += [(f'head changed to {changed}', self.prefix() + [self.page(), self.read(), self.page(changed, limit=1)],
                  None) for changed in ([], self.items[1:], [{'id': 'outside', 'clientUserMessageId': 'foreign'}])]
        for name, script, expected in rows:
            with self.subTest(name):
                if expected is ValueError:
                    with self.assertRaisesRegex(ValueError, 'turn ID'):
                        await self.execute(script)
                else:
                    result = await self.execute(script)
                    if isinstance(expected, dict):
                        self.assertEqual(result.state, 'ok')
                        self.assertEqual(result.value, expected)
                    elif expected == 'not ok':
                        self.assertNotEqual(result.state, 'ok')
                    elif expected:
                        self.assertEqual(result.state, expected)
                    if not isinstance(expected, dict) or not expected['continued']:
                        self.assertFalse((result.value or {}).get('continued'))
                methods = [r['method'] for r in self.requests]
                self.assertLessEqual(methods.count('thread/queue/start'), 1)
                self.assertFalse(any(method in ('thread/queue/add', 'thread/queue/delete', 'thread/queue/reorder',
                    'thread/resume', 'thread/unsubscribe', 'turn/interrupt') for method in methods))

    async def test_damaged_receipts_and_pages_fail_closed(self):
        """Damaged receipts and malformed, cyclic or unbounded Codex pages never start anything; fragile because the
        queue inventory is Codex's paged shape and the receipts are a file other writers touch, and a wrong start
        double-submits silently."""
        executor = Mock()
        rows = [('no receipts', self.session, {}, ('ok', 'unavailable')),
                ('starting', replace(self.session, state='starting'), self.accepted, ('ok', 'unavailable')),
                ('no native id', replace(self.session, native_id=None), self.accepted, ('ok', 'unavailable')),
                ('plain', replace(self.session, native_mode={'mode': 'plain'}), self.accepted, ('ok', 'unavailable')),
                ('relay lost', replace(self.session, native_relay={'state': 'lost', 'reason': 'lost'}), self.accepted,
                 ('ok', 'unavailable'))]
        rows += [(f'receipts {receipts}', self.session, receipts, ('error',))
                 for receipts in (None, [], {'': 'queued'}, {'message': ''}, {'message': None},
                                  {'one': 'same', 'two': 'same'})]
        for name, session, receipts, states in rows:
            with self.subTest(name):
                self.assertIn(self.adapter.continue_queued(session, receipts, executor).state, states)
                executor.rpc.assert_not_called()
        pages = [self.page(self.items if n == 0 else [], cursor=str(n) if n else None,
                          next_cursor=str(n + 1) if n < 7 else None) for n in range(8)]
        rows = [(f'inventory {data}', [self.response('thread/queue/list', None, {'data': data})], '')
                for data in (None, {}, [None], [{'id': ''}], [{'id': 'queued-1'}], [*self.items, self.items[0]])]
        rows += [(f'next cursor {cursor!r}', [self.page([], next_cursor=cursor)], '') for cursor in ('', 7, False)]
        rows += [('cyclic cursor', [self.page([], next_cursor='loop'),
                                    self.page([], cursor='loop', next_cursor='loop')], 'cursor'),
                 ('eight pages', pages + [self.read(), self.page(self.items[:1], limit=1), self.start()], None),
                 ('nine pages', pages[:-1] + [self.page([], cursor='7', next_cursor='8')], 'page'),
                 ('oversized page', [self.page(self.items[:1] * 101)], 'limit')]
        rows += [(f'head cursor {cursor!r}', [self.page(), self.read(),
                  self.page(self.items[:1], limit=1, next_cursor=cursor)], 'cursor') for cursor in ('', 7, False)]
        for name, script, error in rows:
            with self.subTest(name):
                if error is None:
                    self.assertEqual((await self.execute(self.prefix() + script)).state, 'ok')
                    self.assertEqual(len(self.requests), 13)
                else:
                    with self.assertRaisesRegex(ValueError, error):
                        await self.execute(self.prefix() + script)
