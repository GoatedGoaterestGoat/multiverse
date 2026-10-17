"""Scripted app-server restoration checks; no native process or model calls."""
from collections import deque
import json
from pathlib import Path
import tempfile
import unittest

from websockets.asyncio.server import unix_serve

from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import (AttachmentSpec, NativeEndpoint, NativeServiceHandle,
                                        ServiceIdentity, SessionHandle)
from multiverse.runtime import rpc


class RestorationProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mv2-restoration-', dir='/tmp')
        self.root = Path(self.temp.name)
        endpoint = NativeEndpoint('unix-websocket', str(self.root / 'rpc.sock'))
        self.session = SessionHandle('agent', 'codex', str(self.root), native_id='saved-thread',
            endpoint=endpoint, service=NativeServiceHandle(ServiceIdentity('codex', {}), endpoint),
            attachment=AttachmentSpec('service', 'binding', 'idle-existing-service', True, True))
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        self.script, self.requests, self.errors = deque(), [], []
        self.server = await unix_serve(self.handle, endpoint.address)

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.temp.cleanup()

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
            await connection.send(json.dumps({'id': request['id'], 'result': response}))

    def thread(self, state='idle', **changes):
        return {'thread': {'id': self.session.native_id, 'cwd': str(self.root),
                           'status': {'type': state}, **changes}}

    def read(self, response):
        return ('thread/read', {'threadId': self.session.native_id, 'includeTurns': False}, response)

    def start(self, response=None):
        return [('initialize', None, {}), self.read(response or self.thread('notLoaded'))]

    def page(self, data=(), cursor=None, next_cursor=None):
        return ('thread/loaded/list', {} if cursor is None else {'cursor': cursor},
                {'data': list(data), 'nextCursor': next_cursor})

    def qualify(self, final=None):
        return [('thread/turns/list', {'threadId': self.session.native_id, 'limit': 1,
                    'sortDirection': 'desc', 'itemsView': 'notLoaded'}, {'data': [{'status': 'completed'}]}),
                ('thread/queue/list', {'threadId': self.session.native_id, 'limit': 1}, {'data': []}),
                ('thread/backgroundTerminals/list', {'threadId': self.session.native_id, 'limit': 1}, {'data': []}),
                self.read(final or self.thread())]

    async def execute(self, script):
        self.script = deque(script)
        self.requests, self.errors = [], []
        try:
            result = await rpc._execute(self.adapter.attachment_recovery(self.session), 3)
        finally:
            if self.errors:
                raise self.errors[0]
        self.assertFalse(self.script, 'Native check stopped before consuming required evidence')
        return result

    async def test_unloaded_then_restored_requires_complete_fresh_qualification(self):
        pending = await self.execute(self.start() + [self.page(), self.read(self.thread('notLoaded'))])
        self.assertEqual(pending.state, 'pending')
        self.assertEqual(pending.reason, 'Codex did not restore this conversation after its restart')
        self.assertIsNone(pending.value)
        restored = await self.execute(self.start(self.thread()) + self.qualify())
        self.assertEqual(restored.state, 'ok')
        self.assertEqual(len(self.requests), 6)

    async def test_pagination_finds_exact_thread_on_later_page_then_rechecks(self):
        result = await self.execute(self.start() + [self.page(['other'], next_cursor='page2'),
            self.page([self.session.native_id], cursor='page2'), self.read(self.thread())] + self.qualify())
        self.assertEqual(result.state, 'ok')

    async def test_absence_requires_all_pages_and_fresh_exact_read(self):
        result = await self.execute(self.start() + [self.page(['other'], next_cursor='page2'),
            self.page(['another'], cursor='page2'), self.read(self.thread('notLoaded'))])
        self.assertEqual(result.state, 'pending')

    async def test_loaded_inventory_is_not_proof_of_idle(self):
        for response in (self.thread('active'),
                         self.thread(status={'type': 'active', 'activeFlags': ['waitingOnApproval']}),
                         self.thread(status={'type': 'active', 'activeFlags': ['waitingOnUserInput']}),
                         self.thread('systemError'), self.thread('future'),
                         self.thread(id='different'), self.thread(cwd='/elsewhere'),
                         self.thread('notLoaded', id='different'), self.thread('notLoaded', cwd='/elsewhere'),
                         self.thread(canAcceptDirectInput=False)):
            with self.subTest(response=response):
                result = await self.execute(self.start() + [self.page([self.session.native_id]), self.read(response)])
                self.assertNotIn(result.state, ('ok', 'pending'))

    async def test_initial_busy_unknown_and_mismatched_unloaded_stop_immediately(self):
        for response in (self.thread('active'), self.thread('systemError'), self.thread('future'),
                         self.thread('notLoaded', id='different'), self.thread('notLoaded', cwd='/elsewhere')):
            with self.subTest(response=response):
                result = await self.execute(self.start(response))
                self.assertNotIn(result.state, ('ok', 'pending'))

    async def test_restoration_still_requires_inventories_and_final_idle_read(self):
        prefix = self.start() + [self.page(), self.read(self.thread())]
        for index, data in ((0, [{'status': 'inProgress'}]), (0, [{'status': 'future'}]),
                            (1, [{'id': 'queued'}]), (2, [{'id': 'background'}])):
            with self.subTest(index=index, data=data):
                suffix = self.qualify()[:index + 1]
                method, params, _ = suffix[-1]
                suffix[-1] = (method, params, {'data': data})
                self.assertEqual((await self.execute(prefix + suffix)).state, 'unavailable')
        for final in (self.thread('active'), self.thread('notLoaded'), self.thread(id='different'),
                      self.thread(cwd='/elsewhere')):
            with self.subTest(final=final):
                self.assertEqual((await self.execute(prefix + self.qualify(final))).state, 'unavailable')

    async def test_malformed_and_cyclic_pages_fail_closed(self):
        for page in ({'data': {}}, {'data': [None]}, {'data': ['']},
                     {'data': [], 'nextCursor': ''}, {'data': [], 'nextCursor': 7}):
            with self.subTest(page=page):
                with self.assertRaises(ValueError):
                    await self.execute(self.start() + [('thread/loaded/list', {}, page)])
        with self.assertRaisesRegex(ValueError, 'cursor'):
            await self.execute(self.start() + [self.page(next_cursor='loop'),
                self.page(cursor='loop', next_cursor='loop')])

    async def test_page_cap_allows_full_qualification_but_not_unbounded_inventory(self):
        pages = [self.page(cursor=None if n == 0 else str(n),
                           next_cursor=str(n + 1) if n < 31 else None) for n in range(32)]
        result = await self.execute(self.start() + pages + [self.read(self.thread())] + self.qualify())
        self.assertEqual(result.state, 'ok')
        self.assertEqual(len(self.requests), 39)
        pages[-1] = self.page(cursor='31', next_cursor='32')
        with self.assertRaisesRegex(ValueError, 'page'):
            await self.execute(self.start() + pages)
