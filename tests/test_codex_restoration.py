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

    async def test_restoration_check_after_daemon_restart(self):
        """After a daemon restart the conversation counts as restored only after every thread/loaded/list page and a
        fresh exact read; fragile because it reads Codex's paging and status shapes, and a false ok restarts a busy or
        unrestored agent (59108e5)."""
        native_id = self.session.native_id
        pages = [self.page(cursor=None if n == 0 else str(n),
                           next_cursor=str(n + 1) if n < 31 else None) for n in range(32)]
        rows = [('restored at the first read', self.start(self.thread()) + self.qualify(), 'ok', 6),
                ('found on a later page', self.start() + [self.page(['other'], next_cursor='page2'),
                    self.page([native_id], cursor='page2'), self.read(self.thread())] + self.qualify(), 'ok', None),
                ('32 pages', self.start() + pages + [self.read(self.thread())] + self.qualify(), 'ok', 39),
                ('not restored', self.start() + [self.page(), self.read(self.thread('notLoaded'))], 'pending', None),
                ('absent from every page', self.start() + [self.page(['other'], next_cursor='page2'),
                    self.page(['another'], cursor='page2'), self.read(self.thread('notLoaded'))], 'pending', None)]
        for response in (self.thread('active'),
                         self.thread(status={'type': 'active', 'activeFlags': ['waitingOnApproval']}),
                         self.thread(status={'type': 'active', 'activeFlags': ['waitingOnUserInput']}),
                         self.thread('systemError'), self.thread('future'),
                         self.thread(id='different'), self.thread(cwd='/elsewhere'),
                         self.thread('notLoaded', id='different'), self.thread('notLoaded', cwd='/elsewhere'),
                         self.thread(canAcceptDirectInput=False)):
            script = self.start() + [self.page([native_id]), self.read(response)]
            rows.append((('loaded but not idle', response), script, 'refused', None))
        for response in (self.thread('active'), self.thread('systemError'), self.thread('future'),
                         self.thread('notLoaded', id='different'), self.thread('notLoaded', cwd='/elsewhere')):
            rows.append((('refused at the first read', response), self.start(response), 'refused', None))
        prefix = self.start() + [self.page(), self.read(self.thread())]
        for index, data in ((0, [{'status': 'inProgress'}]), (0, [{'status': 'future'}]),
                            (1, [{'id': 'queued'}]), (2, [{'id': 'background'}])):
            suffix = self.qualify()[:index + 1]
            method, params, _ = suffix[-1]
            suffix[-1] = (method, params, {'data': data})
            rows.append((('busy inventory', method, data), prefix + suffix, 'unavailable', None))
        for final in (self.thread('active'), self.thread('notLoaded'), self.thread(id='different'),
                      self.thread(cwd='/elsewhere')):
            rows.append((('final read not idle', final), prefix + self.qualify(final), 'unavailable', None))
        for case, script, expected, requests in rows:
            with self.subTest(case=case):
                result = await self.execute(script)
                if expected == 'refused':
                    self.assertNotIn(result.state, ('ok', 'pending'))
                else:
                    self.assertEqual(result.state, expected)
                if expected == 'pending':
                    self.assertEqual(result.reason, 'Codex did not restore this conversation after its restart')
                    self.assertIsNone(result.value)
                if requests is not None:
                    self.assertEqual(len(self.requests), requests)
        failures = [(('malformed page', page), self.start() + [('thread/loaded/list', {}, page)], '')
                    for page in ({'data': {}}, {'data': [None]}, {'data': ['']},
                                 {'data': [], 'nextCursor': ''}, {'data': [], 'nextCursor': 7})]
        failures.append(('cyclic cursor', self.start() + [self.page(next_cursor='loop'),
                         self.page(cursor='loop', next_cursor='loop')], 'cursor'))
        failures.append(('33 pages', self.start() + pages[:-1] + [self.page(cursor='31', next_cursor='32')], 'page'))
        for case, script, pattern in failures:
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, pattern):
                await self.execute(script)
