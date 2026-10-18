"""Connection-local relay policy cannot change primary launch bookkeeping."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

from multiverse.harnesses import types
from multiverse.runtime import rpc


class LocalProtocolErrorTest(unittest.TestCase):
    def test_typed_destination_correlation_and_drop(self):
        error = types.LocalProtocolError('local', request_id=7, destination='upstream', code=-32000)
        self.assertEqual((str(error), error.request_id, error.destination, error.code),
                         ('local', 7, 'upstream', -32000))
        self.assertIsNone(types.LocalProtocolError('drop').request_id)
        for options in ({'request_id': True}, {'request_id': []}, {'destination': 'both'},
                        {'code': True}, {'code': 2 ** 31}, {'code': -2 ** 31 - 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                types.LocalProtocolError('invalid', **options)


def reject(reason, identifier=None, destination='client'):
    error = getattr(types, 'LocalProtocolError', None)
    if error is None:
        raise ValueError(reason)
    raise error(reason, request_id=identifier, destination=destination)


class View:
    def __init__(self, shared, token, owns):
        self.shared, self.token, self.owns = shared, token, owns
        self.pending = {}
        self.closed_count = 0
        self.launch_refusal = None

    @property
    def bound(self):
        return self.shared.bound

    def outgoing(self, value):
        if value.get('method') == 'reject':
            reject('local rejection', value.get('id'))
        if value.get('method') == 'wrong-id':
            reject('must not reflect another ID', 'unrelated')
        if value.get('method') == 'wrong-destination':
            reject('client request cannot be answered upstream', value.get('id'), 'upstream')
        if value.get('method') == 'bool-id':
            reject('boolean frame ID cannot be reflected', 1)
        if 'method' in value and 'id' in value:
            self.pending[value['id']] = value.get('params', {}).get('label')
        return value

    def incoming(self, value):
        self.launch_refusal = None
        if value.get('method') == 'local-notification':
            reject('notification must drop')
        if value.get('method') == 'host/request':
            reject('side host request refused', value['id'], 'upstream')
        result = value.get('result', {})
        if result.get('local'):
            self.launch_refusal = {'message': 'must not become primary refusal'}
            reject('bad correlated side response', value.get('id') if result['local'] == 'correlated' else None)
        if result.get('primary-error'):
            raise ValueError('ordinary primary validation failed')
        identifier = value.get('id')
        if identifier in self.pending:
            if self.pending.pop(identifier) != result.get('label'):
                raise ValueError('request ID collided across connections')
        if result.get('bind'):
            self.shared.bound = True
            return 'primary'
        return None

    def usage(self, value):
        return value.get('usage')

    def closed(self):
        self.closed_count += 1
        self.pending.clear()
        if self.shared.close_error:
            raise ValueError('local cleanup failure')


class Protocol(View):
    def __init__(self):
        self.bound = False
        self.views = []
        self.close_error = False
        self.pending = {}
        self.launch_refusal = None
        self.closed_count = 0
        self.shared = self

    def for_connection(self, token, owns):
        view = View(self, token, owns)
        self.views.append(view)
        return view


# Root has writable bound state; View's property deliberately reads only it.
Protocol.bound = False


class RelayConnectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mv2-relay-view-', dir='/tmp')
        self.folder = Path(self.temporary.name)
        self.spec = {'socket_path': str(self.folder / 'relay'), 'upstream_path': str(self.folder / 'native')}
        self.received = []
        async def upstream(connection):
            async for payload in connection:
                value = json.loads(payload)
                self.received.append(value)
                method = value.get('method')
                if method is None:
                    continue
                params = value.get('params', {})
                if method == 'notification':
                    await connection.send(json.dumps({'method': 'local-notification'}))
                if method == 'host':
                    await connection.send(json.dumps({'id': 'server-call', 'method': 'host/request'}))
                    self.received.append(json.loads(await connection.recv()))
                if 'id' not in value:
                    continue
                result = {'label': params.get('label')}
                if method == 'bind':
                    result['bind'] = True
                if method == 'bad-side':
                    await connection.send(json.dumps({'id': value['id'], 'result': {'local': 'correlated'}, 'usage': 'bad'}))
                    continue
                if method == 'uncorrelated':
                    await connection.send(json.dumps({'id': value['id'], 'result': {'local': 'uncorrelated'}, 'usage': 'bad'}))
                if method == 'bad-primary':
                    result['primary-error'] = True
                if method == 'delay':
                    await asyncio.sleep(.05)
                await connection.send(json.dumps({'id': value['id'], 'result': result}))
        self.server = await unix_serve(upstream, self.spec['upstream_path'])
        self.protocol = Protocol()
        self.bound, self.errors, self.losses, self.usage, self.refusals = [], [], [], [], []
        self.proxy = rpc.LaunchProxy(self.spec, self.protocol, self.bound.append, self.errors.append,
                                    self.usage.append, self.losses.append, self.refusals.append)
        await asyncio.to_thread(self.proxy.start)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await asyncio.to_thread(self.proxy.close)
        self.server.close()
        await self.server.wait_closed()
        self.temporary.cleanup()

    async def connect(self):
        client = await unix_connect(self.spec['socket_path'])
        self.clients.append(client)
        return client

    async def request(self, client, identifier, method, **params):
        await client.send(json.dumps({'id': identifier, 'method': method, 'params': params}))
        return json.loads(await asyncio.wait_for(client.recv(), 2))

    async def test_views_scope_colliding_ids_and_owner_is_dynamic(self):
        first, second = await self.connect(), await self.connect()
        replies = await asyncio.gather(self.request(first, 7, 'delay', label='first'),
                                       self.request(second, 7, 'delay', label='second'))
        self.assertEqual([reply['result']['label'] for reply in replies], ['first', 'second'])
        self.assertEqual(len(self.protocol.views), 2)
        self.assertEqual(len({view.token for view in self.protocol.views}), 2)
        self.assertFalse(any(view.owns() for view in self.protocol.views))
        await self.request(first, 9, 'bind')
        self.assertEqual(sum(view.owns() for view in self.protocol.views), 1)
        self.assertEqual(self.bound, ['primary'])

    async def test_local_rejections_skip_all_primary_callbacks_and_continue(self):
        client = await self.connect()
        await self.request(client, 1, 'bind')
        self.assertIn('error', await self.request(client, 2, 'reject'))
        self.assertIn('error', await self.request(client, 3, 'bad-side'))
        self.assertEqual((await self.request(client, 4, 'ping'))['id'], 4)
        self.assertEqual(self.bound, ['primary'])
        self.assertEqual((self.errors, self.losses, self.usage, self.refusals), ([], [], [], []))

    async def test_drop_uncorrelated_frames_and_reply_server_errors_upstream(self):
        client = await self.connect()
        await client.send(json.dumps({'method': 'reject'}))
        self.assertEqual((await self.request(client, 3, 'uncorrelated'))['id'], 3)
        self.assertEqual((await self.request(client, 4, 'notification'))['id'], 4)
        self.assertEqual((await self.request(client, 5, 'host'))['id'], 5)
        server_reply = next(value for value in self.received if value.get('id') == 'server-call')
        self.assertIn('error', server_reply)
        self.assertFalse(any(value.get('method') == 'reject' for value in self.received))
        self.assertEqual((self.errors, self.losses, self.usage, self.refusals), ([], [], [], []))

    async def test_view_cleanup_is_once_and_local_cleanup_failure_is_harmless(self):
        client = await self.connect()
        await self.request(client, 1, 'bind')
        self.protocol.close_error = True
        await client.close()
        for _ in range(40):
            if self.protocol.views and self.protocol.views[0].closed_count:
                break
            await asyncio.sleep(.02)
        self.assertEqual(len(self.protocol.views), 1)
        self.assertEqual(self.protocol.views[0].closed_count, 1)
        self.assertEqual(self.protocol.views[0].pending, {})
        self.assertEqual((self.errors, self.losses), ([], []))

    async def test_primary_validation_errors_are_still_observable(self):
        client = await self.connect()
        await self.request(client, 1, 'bind')
        await client.send(json.dumps({'id': 2, 'method': 'bad-primary'}))
        await asyncio.wait_for(client.wait_closed(), 2)
        self.assertTrue(any('ordinary primary' in error for error in self.errors))
        self.assertTrue(self.losses)

    async def test_connect_failure_still_closes_view(self):
        self.server.close()
        await self.server.wait_closed()
        client = await self.connect()
        await asyncio.wait_for(client.wait_closed(), 2)
        self.assertEqual(len(self.protocol.views), 1)
        self.assertEqual(self.protocol.views[0].closed_count, 1)
        self.assertTrue(self.errors)
        self.assertTrue(self.losses)

    async def test_invalid_correlation_or_direction_is_dropped(self):
        client = await self.connect()
        await client.send(json.dumps({'id': 2, 'method': 'wrong-id'}))
        await client.send(json.dumps({'id': 3, 'method': 'wrong-destination'}))
        await client.send(json.dumps({'id': True, 'method': 'bool-id'}))
        self.assertEqual((await self.request(client, 4, 'ping'))['id'], 4)
        self.assertEqual([value.get('method') for value in self.received], ['ping'])
        self.assertEqual((self.errors, self.losses, self.refusals), ([], [], []))

    async def test_legacy_protocol_without_factory_keeps_primary_behavior(self):
        self.protocol.for_connection = None
        client = await self.connect()
        self.assertEqual((await self.request(client, 1, 'bind'))['id'], 1)
        self.assertEqual(self.bound, ['primary'])
        await client.close()
        self.assertEqual(self.protocol.closed_count, 0)
        self.assertEqual(self.protocol.views, [])
