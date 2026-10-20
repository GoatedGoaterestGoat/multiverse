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
            native_mode={'mode': 'bridge'}, queue_state={'native_session_id': 'thread',
                'state': 'paused', 'restart': {'id': 'new-message', 'state': 'claimed'}})
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

    def start(self):
        return self.response('thread/queue/start', {'threadId': 'thread'},
                             {'turn': {'id': 'turn-1'}})

    async def execute(self, script, accepted=None):
        self.script = deque(script)
        self.requests, self.errors = [], []
        executor = Mock()
        executor.rpc.side_effect = lambda spec, **kwargs: spec
        spec = self.adapter.continue_queued(self.session, self.accepted if accepted is None else accepted, executor)
        self.assertEqual(spec.max_requests, 4)
        self.assertEqual(executor.rpc.call_args.kwargs, {'timeout': 5})
        try:
            observed = await rpc._execute(spec, 3)
        finally:
            if self.errors:
                raise self.errors[0]
        self.assertFalse(self.script, 'Continuation omitted required evidence')
        return observed

    async def test_only_a_claimed_new_message_starts_the_native_fifo_head(self):
        """Only a newly claimed message can restart the FIFO queue, including native entries; replayed claims or selecting a tail silently double-start or reorder work."""
        success = {'continued': True, 'turn_id': 'turn-1'}
        foreign = {'id': 'native-entry', 'clientUserMessageId': None}
        rows = [('owned head', self.prefix() + [self.page(self.items[:1], limit=1), self.start()], success),
                ('native head', self.prefix() + [self.page([foreign], limit=1, next_cursor='more'), self.start()], success),
                ('empty queue', self.prefix() + [self.page([], limit=1)], {'continued': False}),
                ('typed turn wins', self.prefix(status={'type': 'active'}), 'unavailable'),
                ('queue start races typed input', self.prefix() + [self.page([foreign], limit=1),
                    ('thread/queue/start', {'threadId': 'thread'},
                     {'error': {'code': -32600, 'message': 'Thread is busy'}})], 'error')]
        rows += [(str(changes), self.prefix(**changes), 'not ok') for changes in
                 ({'id': 'other'}, {'cwd': '/other'}, {'status': {'type': 'notLoaded'}},
                  {'canAcceptDirectInput': False})]
        for name, script, expected in rows:
            with self.subTest(name):
                observed = await self.execute(script)
                if isinstance(expected, dict):
                    self.assertEqual(observed.state, 'ok')
                    self.assertEqual(observed.value, expected)
                elif expected == 'not ok':
                    self.assertNotEqual(observed.state, 'ok')
                else:
                    self.assertEqual(observed.state, expected)
                methods = [request['method'] for request in self.requests]
                self.assertLessEqual(methods.count('thread/queue/start'), 1)
                self.assertFalse(any(method in methods for method in
                    ('thread/queue/add', 'thread/queue/delete', 'thread/queue/reorder', 'thread/resume')))
        executor = Mock()
        for queue in (None, {}, {'native_session_id': 'other', 'restart': {'id': 'new', 'state': 'claimed'}},
                      {'native_session_id': 'thread', 'restart': {'id': 'new', 'state': 'pending'}},
                      {'native_session_id': 'thread', 'state': 'paused'}):
            with self.subTest(claim=queue):
                result = self.adapter.continue_queued(replace(self.session, queue_state=queue), self.accepted, executor)
                self.assertEqual(result.state, 'ok')
                self.assertFalse(result.value['continued'])
                executor.rpc.assert_not_called()

    async def test_damaged_queue_evidence_cannot_start_or_resubmit(self):
        """Malformed native queue evidence must not mutate work; reply-shape drift otherwise silently starts an unrelated turn."""
        for data, cursor in ((None, None), ({}, None), ([None], None), ([{}], None),
                             ([{'id': ''}], None), (self.items, None), ([], 'more'),
                             (self.items[:1], False)):
            with self.subTest(data=data, cursor=cursor):
                script = [self.response('thread/queue/list', None, {'data': data, 'nextCursor': cursor})]
                with self.assertRaises(ValueError):
                    await self.execute(self.prefix() + script)
                self.assertNotIn('thread/queue/start', [request['method'] for request in self.requests])
