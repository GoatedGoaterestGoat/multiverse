"""Read-only Codex queue inventories, with synthetic native replies only."""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import unix_serve

from isolation import isolate
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import NativeEndpoint, NativeResult, SessionHandle
from multiverse.runtime import native


class QueueReadTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.temp = tempfile.TemporaryDirectory(prefix='mv2-queue-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.session = SessionHandle('agent', 'codex', self.temp.name,
            native_id=str(uuid4()), state='running', launch_id=str(uuid4()),
            session_file=str(Path(self.temp.name) / 'runtime.json'),
            endpoint=NativeEndpoint('unix-websocket', str(Path(self.temp.name) / 'queue.sock')),
            native_mode={'mode': 'bridge', 'policy': 'auto'}, native_relay={'state': 'ready'},
            native_versions={'cli': '0.167.1', 'daemon': '0.167.1'})
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        self.requests, self.specs = [], []

    def read(self, *, session=None, overrides=None):
        owner = self
        session = session or self.session
        replies = {
            'initialize': {'userAgent': 'custom-originator/0.167.1 (native build)'},
            'thread/read': {'thread': {'id': session.native_id, 'cwd': session.workspace,
                                       'status': {'type': 'notLoaded'}, 'canAcceptDirectInput': False}},
            'thread/queue/list': {'data': [], 'nextCursor': None},
        }
        class Executor:
            def check_session(self):
                pass

            def rpc(self, spec, timeout):
                owner.specs.append((spec, timeout))
                plan = spec.plan()
                request = next(plan)
                while True:
                    owner.requests.append(request)
                    reply = (overrides or {}).get(request.method, {'result': replies[request.method]})
                    try:
                        request = plan.send(reply)
                    except StopIteration as done:
                        return done.value
        self.assertTrue(callable(getattr(self.adapter, 'queued_prompts', None)), 'Codex has no queue provider')
        return self.adapter.queued_prompts(session, Executor())

    @staticmethod
    def entry(id='message', native='queue-item', **extra):
        return {'id': native, 'clientUserMessageId': id,
                'input': [{'type': 'text', 'text': 'PRIVATE QUEUED BODY'}], **extra}

    def test_exact_read_projects_ids_without_input_or_queue_mutation(self):
        """Queue presence must be exact and read-only or read-first can block the wrong sender or start work."""
        data = [self.entry('second', 'q2'), self.entry('first', 'q1', extra='PRIVATE UNKNOWN')]
        observed = self.read(overrides={'thread/queue/list': {'result': {'data': data, 'nextCursor': None}}})
        self.assertEqual(observed.state, 'ok', observed)
        self.assertEqual(observed.value['coverage'], 'complete')
        self.assertEqual(observed.value['entries'], [
            {'message_id': 'second', 'native_id': 'q2', 'since': None},
            {'message_id': 'first', 'native_id': 'q1', 'since': None}])
        self.assertNotIn('PRIVATE', json.dumps(observed.to_dict()))
        self.assertEqual([r.method for r in self.requests], ['initialize', 'thread/read', 'thread/queue/list'])
        self.assertEqual(self.requests[1].params, {'threadId': self.session.native_id, 'includeTurns': False})
        self.assertEqual(self.requests[2].params, {'threadId': self.session.native_id, 'limit': 100})
        self.assertEqual(self.requests[0].params['capabilities'], {'experimentalApi': True})
        spec, timeout = self.specs[0]
        self.assertEqual((timeout, spec.max_requests, spec.max_response_bytes), (2, 3, 64 << 20))
        self.assertEqual(spec.socket_path, self.session.endpoint.address)
        self.assertIsNotNone(observed.value['observed_at'])
        self.assertIsNotNone(datetime.fromisoformat(observed.value['observed_at']).tzinfo)
        self.assertIn('after-turn', observed.reason)
        self.assertIn('active-turn', observed.reason)
        self.assertEqual(self.adapter.capabilities(self.session)['queued'].state, 'supported')
        for state in ('idle', 'active', 'notLoaded'):
            with self.subTest(state=state):
                self.requests.clear()
                observed = self.read(session=replace(self.session, queue_state={'state': 'paused'}),
                    overrides={'thread/read': {'result': {'thread': {'id': self.session.native_id,
                        'cwd': self.session.workspace, 'status': {'type': state}, 'canAcceptDirectInput': False}}}})
                self.assertEqual(observed.state, 'ok')
                self.assertEqual(observed.value['coverage'], 'complete')
                self.assertEqual([r.method for r in self.requests], ['initialize', 'thread/read', 'thread/queue/list'])

    def test_scope_and_checked_pair_are_required_before_queue_read(self):
        """Recorded and actual native versions and exact thread scope must agree before any queue IDs are trusted."""
        for changes in ({'native_mode': {'mode': 'plain'}}, {'native_mode': None},
                {'native_relay': None}, {'native_relay': {'state': 'lost'}}, {'state': 'stopped'},
                {'native_id': None}, {'launch_id': None}, {'session_file': None}, {'endpoint': None},
                {'endpoint': NativeEndpoint('unix-lines', 'unused')},
                {'native_versions': None}, {'native_versions': ['bad']},
                {'native_versions': {'cli': [], 'daemon': []}},
                {'native_versions': {'cli': '0.168.0', 'daemon': '0.168.0'}},
                {'native_versions': {'cli': '0.166.0', 'daemon': '0.167.1'}},
                {'native_versions': {'cli': '0.167.1', 'daemon': '0.167.2'}},
                {'native_versions': {'cli': '0.167.2', 'daemon': '0.167.1'}},
                {'native_versions': {'cli': '0.169.0', 'daemon': '0.167.2'}},
                {'native_versions': {'cli': '0.167.2', 'daemon': '0.169.0'}},
                {'native_versions': {'cli': '0.168.0', 'daemon': '0.168.0'}},
                {'native_versions': {'cli': '0.169.1', 'daemon': '0.169.1'}}):
            with self.subTest(changes=changes):
                self.specs.clear()
                observed = self.read(session=replace(self.session, **changes))
                self.assertEqual(observed.state, 'unavailable')
                self.assertEqual(self.specs, [])
        for version in ('0.166.0', '0.167.0', '0.167.1', '0.167.2', '0.169.0'):
            with self.subTest(version=version):
                observed = self.read(session=replace(self.session, native_versions={'cli': version, 'daemon': version}),
                    overrides={'initialize': {'result': {'userAgent': 'origin/' + version}}})
                self.assertEqual(observed.state, 'ok')
                self.assertEqual(observed.value['coverage'], 'complete')
        for greeting in (None, '', 'codex/0.167.0', 'codex/0.168.0', 'codex/0.167.1-beta',
                         'unverified prefix codex/0.167.1', 'codex/0.167.10'):
            with self.subTest(greeting=greeting):
                self.requests.clear()
                observed = self.read(overrides={'initialize': {'result': {'userAgent': greeting}}})
                self.assertEqual(observed.state, 'unavailable')
                self.assertEqual([r.method for r in self.requests], ['initialize'])
        for thread in ({'id': 'foreign', 'cwd': self.session.workspace},
                       {'id': self.session.native_id, 'cwd': '/'}, {'id': self.session.native_id}):
            with self.subTest(thread=thread):
                self.requests.clear()
                observed = self.read(overrides={'thread/read': {'result': {'thread': thread}}})
                self.assertEqual(observed.state, 'unavailable')
                self.assertEqual([r.method for r in self.requests], ['initialize', 'thread/read'])

    def test_incomplete_pages_keep_only_valid_positive_ids(self):
        """Partial or malformed queue pages must not become complete or erase independently valid presence."""
        good = self.entry()
        for label, page, ids, coverage in (
                ('empty', {'data': [], 'nextCursor': None}, [], 'complete'),
                ('missing cursor', {'data': [good]}, ['message'], 'unknown'),
                ('more', {'data': [good], 'nextCursor': '100'}, ['message'], 'unknown'),
                ('invalid cursor', {'data': [good], 'nextCursor': False}, ['message'], 'unknown'),
                ('missing rows', {'nextCursor': None}, [], 'unknown'),
                ('bad row', {'data': [None, good], 'nextCursor': None}, ['message'], 'unknown'),
                ('missing id', {'data': [good, {'id': 'q'}], 'nextCursor': None}, ['message'], 'unknown'),
                ('nul id', {'data': [good, self.entry('bad\0id')], 'nextCursor': None}, ['message'], 'unknown'),
                ('invalid utf8', {'data': [good, self.entry(chr(0xd800))], 'nextCursor': None}, ['message'], 'unknown'),
                ('duplicate', {'data': [good, self.entry(native='other')], 'nextCursor': None}, ['message'], 'unknown'),
                ('excess', {'data': [self.entry(str(n), str(n)) for n in range(101)], 'nextCursor': None},
                 [str(n) for n in range(100)], 'unknown')):
            with self.subTest(label=label):
                observed = self.read(overrides={'thread/queue/list': {'result': page}})
                self.assertEqual(observed.state, 'ok', observed)
                self.assertEqual(observed.value['coverage'], coverage)
                self.assertEqual([row['message_id'] for row in observed.value['entries']], ids)
                if coverage == 'unknown':
                    self.assertTrue(observed.value['reason'])
        for method in ('initialize', 'thread/read', 'thread/queue/list'):
            with self.subTest(error=method):
                observed = self.read(overrides={method: {'error': {'code': -32600, 'message': 'PRIVATE ERROR'}}})
                self.assertEqual(observed.state, 'unavailable')
                self.assertNotIn('PRIVATE', json.dumps(observed.to_dict()))

    def test_real_transport_bounds_large_frames_and_total_deadline(self):
        """A large queue body must not break metadata reads, while runaway or slow frames remain bounded and private."""
        for case in ('large', 'oversize', 'slow'):
            with self.subTest(case=case):
                endpoint = str(Path(self.temp.name) / (case + '.sock'))
                session = replace(self.session, endpoint=NativeEndpoint('unix-websocket', endpoint))
                methods, errors, stop = [], [], threading.Event()
                def handler(connection):
                    try:
                        for frame in connection:
                            request = json.loads(frame)
                            methods.append(request['method'])
                            if request['method'] == 'initialized':
                                continue
                            if request['method'] == 'initialize':
                                value = {'userAgent': 'codex/0.167.1'}
                            elif request['method'] == 'thread/read':
                                value = {'thread': {'id': session.native_id, 'cwd': session.workspace}}
                            elif request['method'] == 'thread/queue/list':
                                if case == 'slow':
                                    stop.wait(4)
                                    return
                                value = {'data': [self.entry(input=[{'type': 'text',
                                    'text': 'Z' * ((65 if case == 'oversize' else 2) << 20)}])], 'nextCursor': None}
                            else:
                                raise AssertionError(request['method'])
                            connection.send(json.dumps({'id': request['id'], 'result': value}))
                    except ConnectionClosed:
                        pass
                    except Exception as error:
                        errors.append(error)
                server = unix_serve(handler, path=endpoint, close_timeout=.1)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    executor = native.NativeExecutor(self.temp.name)
                    started = time.monotonic()
                    with patch.object(executor, 'check_session'):
                        observed = self.adapter.queued_prompts(session, executor)
                    elapsed = time.monotonic() - started
                    if case == 'large':
                        self.assertEqual(observed.state, 'ok', observed)
                        self.assertEqual(observed.value['coverage'], 'complete', observed)
                        self.assertEqual(len(observed.value['entries']), 1)
                        self.assertLess(len(json.dumps(observed.to_dict())), 1500)
                    else:
                        self.assertEqual(observed.state, 'unavailable', observed)
                        self.assertLess(elapsed, 3.5)
                    self.assertEqual(methods, ['initialize', 'initialized', 'thread/read', 'thread/queue/list'])
                    self.assertEqual(errors, [])
                finally:
                    stop.set()
                    server.shutdown()
                    thread.join(2)
                self.assertFalse(thread.is_alive())
