"""Connection-local relay policy cannot change primary launch bookkeeping."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

from multiverse.harnesses import types
from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.runtime import rpc


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


def codex_launch(native_id):
    grants = {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}
    return LaunchProtocol({'native_id': native_id, 'workspace': '/fixture/workspace', 'bootstrap': 'primary bootstrap',
                           'roots': [], 'resume_permissions': grants})


def codex_view(protocol, name, owner):
    view = protocol.for_connection(name, owner)
    view.outgoing({'id': 1, 'method': 'initialize', 'params': {}})
    view.incoming({'id': 1, 'result': {'userAgent': 'codex/0.163.0'}})
    return view


def codex_state(protocol):
    return deepcopy({key: getattr(protocol, key) for key in (
        'native_id', 'bound', 'pending', 'pending_method', 'launch_refusal',
        'prompt_request', 'prompt_submitted', 'prompt_error')})


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
        self.loss_recorded = threading.Event()
        def record_loss(reason):
            self.losses.append(reason)
            self.loss_recorded.set()
        self.proxy = rpc.LaunchProxy(self.spec, self.protocol, self.bound.append, self.errors.append,
                                    self.usage.append, record_loss, self.refusals.append)
        await asyncio.to_thread(self.proxy.start)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await asyncio.to_thread(self.proxy.close)
        self.server.close()
        await self.server.wait_closed()
        self.temporary.cleanup()

    async def fresh(self):
        """Start the next row on a new relay and upstream."""
        await self.asyncTearDown()
        await self.asyncSetUp()

    async def connect(self):
        client = await unix_connect(self.spec['socket_path'])
        self.clients.append(client)
        return client

    async def request(self, client, identifier, method, **params):
        await client.send(json.dumps({'id': identifier, 'method': method, 'params': params}))
        return json.loads(await asyncio.wait_for(client.recv(), 2))

    async def test_request_ids_are_scoped_per_connection(self):
        """Clients reusing small JSON-RPC ids on one relay, through the relay and Codex's connection views, each get
        their own replies, and ownership follows the binding client; a break goes unnoticed because a crossed reply
        binds or confirms the wrong request."""
        with self.subTest('relay views'):
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
        main, side = str(uuid4()), str(uuid4())
        fork = {'id': 10, 'method': 'thread/fork',
                'params': {'threadId': main, 'ephemeral': True, 'excludeTurns': True}}
        forked = {'id': 10, 'result': {'thread': {'id': side, 'forkedFromId': main, 'ephemeral': True,
                                                  'cwd': '/fixture/workspace', 'path': None}}}
        with self.subTest('a reply on another connection does not confirm the primary prompt'):
            protocol = codex_launch(main)
            protocol.bound = True
            view, other = codex_view(protocol, 'tui', lambda: True), codex_view(protocol, 'control', lambda: False)
            view.outgoing({'id': 10, 'method': 'turn/start', 'params': {'threadId': main, 'input': []}})
            other.incoming({'id': 10, 'result': {'turn': {'id': 'unrelated'}}})
            self.assertFalse(protocol.prompt_submitted)
            view.incoming({'id': 10, 'result': {'turn': {'id': 'primary'}}})
            self.assertTrue(protocol.prompt_submitted)
        with self.subTest("another connection's primary requests neither bind nor reconfigure"):
            protocol = codex_launch(main)
            protocol.bound = True
            view, other = codex_view(protocol, 'tui', lambda: True), codex_view(protocol, 'other', lambda: False)
            view.outgoing({'id': 12, 'method': 'turn/start', 'params': {'threadId': main}})
            before = codex_state(protocol)
            request = {'id': 12, 'method': 'thread/read', 'params': {'threadId': main}}
            self.assertEqual(other.outgoing(request), request)
            thread = {'id': main, 'cwd': '/fixture/workspace'}
            self.assertIsNone(other.incoming({'id': 12, 'result': {'thread': thread}}))
            self.assertEqual(codex_state(protocol), before)
            with self.assertRaises(types.LocalProtocolError):
                other.outgoing({'id': 13, 'method': 'thread/resume', 'params': {'threadId': main}})
            self.assertEqual(codex_state(protocol), before)
        with self.subTest('a startup binding reply binds only on its own connection'):
            protocol = codex_launch(None)
            owner, other = codex_view(protocol, 'first', lambda: False), codex_view(protocol, 'second', lambda: False)
            outgoing = owner.outgoing({'id': 5, 'method': 'thread/start', 'params': {}})
            self.assertEqual(outgoing['params']['developerInstructions'], 'primary bootstrap')
            reply = {'id': 5, 'result': {'thread': {'id': main, 'cwd': '/fixture/workspace'},
                                        'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never'}}
            self.assertIsNone(other.incoming(reply))
            self.assertFalse(protocol.bound)
            self.assertEqual(owner.incoming(reply), main)
            self.assertTrue(protocol.bound)
            self.assertEqual(protocol.native_id, main)
        with self.subTest('another connection cannot consume a side reply or publish a binding'):
            protocol = codex_launch(main)
            protocol.bound = True
            view, other = codex_view(protocol, 'tui', lambda: True), codex_view(protocol, 'control', lambda: False)
            view.outgoing(fork)
            self.assertIsNone(other.incoming(forked))
            self.assertIsNone(other.side_id)
            self.assertEqual(view.fork_request, 10)
            self.assertIsNone(view.incoming(forked))
            self.assertEqual(view.side_id, side)
            self.assertTrue(protocol.bound)
        with self.subTest('host replies have their own namespace, and a closed view forgets its side'):
            protocol = codex_launch(main)
            protocol.bound = True
            view = codex_view(protocol, 'tui', lambda: True)
            view.outgoing(fork)
            with self.assertRaises(types.LocalProtocolError):
                view.outgoing({**fork, 'id': 11})
            # A native server request may use the same ID as a client request.
            reply = {'id': 10, 'result': {'content': 'host response'}}
            self.assertEqual(view.outgoing(reply), reply)
            view.incoming(forked)
            view.outgoing({'id': 100, 'method': 'thread/read', 'params': {'threadId': side}})
            before = codex_state(protocol)
            view.closed()
            self.assertEqual(view.side_requests, {})
            self.assertIsNone(view.side_id)
            self.assertIsNone(view.incoming(forked))
            self.assertEqual(codex_state(protocol), before)

    async def test_side_rejections_never_reach_the_launch(self):
        """Side-connection rejections and drops never reach the launch's bound, loss or refusal callbacks, and the
        connection keeps working; fragile because those callbacks mark a working agent lost and start its
        recovery."""
        with self.subTest('invalid correlation or direction is dropped'):
            client, start = await self.connect(), len(self.received)
            await client.send(json.dumps({'id': 2, 'method': 'wrong-id'}))
            await client.send(json.dumps({'id': 3, 'method': 'wrong-destination'}))
            await client.send(json.dumps({'id': True, 'method': 'bool-id'}))
            self.assertEqual((await self.request(client, 4, 'ping'))['id'], 4)
            self.assertEqual([value.get('method') for value in self.received[start:]], ['ping'])
            self.assertEqual((self.errors, self.losses, self.usage, self.refusals), ([], [], [], []))
        with self.subTest('uncorrelated frames drop and server requests are answered upstream'):
            client = await self.connect()
            await client.send(json.dumps({'method': 'reject'}))
            self.assertEqual((await self.request(client, 3, 'uncorrelated'))['id'], 3)
            self.assertEqual((await self.request(client, 4, 'notification'))['id'], 4)
            self.assertEqual((await self.request(client, 5, 'host'))['id'], 5)
            server_reply = next(value for value in self.received if value.get('id') == 'server-call')
            self.assertIn('error', server_reply)
            self.assertFalse(any(value.get('method') == 'reject' for value in self.received))
            self.assertEqual((self.errors, self.losses, self.usage, self.refusals), ([], [], [], []))
        with self.subTest('local rejections after binding'):
            client = await self.connect()
            await self.request(client, 1, 'bind')
            self.assertIn('error', await self.request(client, 2, 'reject'))
            self.assertIn('error', await self.request(client, 3, 'bad-side'))
            self.assertEqual((await self.request(client, 4, 'ping'))['id'], 4)
            self.assertEqual(self.bound, ['primary'])
            self.assertEqual((self.errors, self.losses, self.usage, self.refusals), ([], [], [], []))

    async def test_real_primary_failures_still_report_loss(self):
        """Real primary failures still report loss and each view cleans up once; a break leaves a dead agent shown
        as running."""
        with self.subTest('cleanup runs once and its failure is harmless'):
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
        with self.subTest('a primary validation error'):
            await self.fresh()
            client = await self.connect()
            await self.request(client, 1, 'bind')
            await client.send(json.dumps({'id': 2, 'method': 'bad-primary'}))
            await asyncio.wait_for(client.wait_closed(), 2)
            await self.wait_for_loss()
            self.assertTrue(any('ordinary primary' in error for error in self.errors))
            self.assertTrue(self.losses)
        with self.subTest('an upstream that cannot be reached'):
            await self.fresh()
            self.server.close()
            await self.server.wait_closed()
            client = await self.connect()
            await asyncio.wait_for(client.wait_closed(), 2)
            await self.wait_for_loss()
            self.assertEqual(len(self.protocol.views), 1)
            self.assertEqual(self.protocol.views[0].closed_count, 1)
            self.assertTrue(self.errors)
            self.assertTrue(self.losses)

    async def wait_for_loss(self):
        # The peer sees the close handshake before the relay's handler finally
        # runs. Wait for its last callback without stopping the proxy, which
        # would intentionally suppress connection-loss reporting.
        self.assertTrue(await asyncio.to_thread(self.loss_recorded.wait, 2),
                        'relay loss callback did not finish')
