"""Native extension transport checks identity, byte framing and total deadlines."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from multiverse.harnesses.types import NativeEndpoint, SessionHandle
from multiverse.native import NativeExecutor


class ExtensionTransportTest(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix='mv2-socket-', dir='/tmp'))
        self.path = str(Path(temporary) / 'control.sock')
        self.session = SessionHandle('agent', 'pi', temporary, 'launch', 'native',
                                     endpoint=NativeEndpoint('unix-json', self.path))
        self.executor = NativeExecutor(temporary)
        self.enterContext(patch.object(self.executor, 'check_session'))

    def serve(self, handler):
        server = socket.socket(socket.AF_UNIX)
        server.bind(self.path)
        server.listen()
        def run():
            with server:
                client, _ = server.accept()
                with client:
                    data = b''
                    while b'\n' not in data:
                        data += client.recv(4096)
                    try:
                        handler(client, json.loads(data))
                    except (BrokenPipeError, ConnectionResetError):
                        pass
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)

    def test_fragmented_unicode_and_exact_receipt_identity(self):
        def handler(client, request):
            self.assertEqual(request['text'], '日本語')
            wire = (json.dumps({'state': 'native_queued', 'native_id': 'native', 'launch_id': 'launch',
                'message_id': 'message', 'reason': '日本語'}, ensure_ascii=False) + '\n').encode()
            for byte in wire:
                client.sendall(bytes([byte]))
        self.serve(handler)
        result = self.executor.request(self.session, {'operation': 'send', 'message_id': 'message', 'text': '日本語'})
        self.assertEqual(result.reason, '日本語')
        self.assertEqual(result.state, 'native_queued')

    def test_wrong_native_receipt_is_not_accepted(self):
        self.serve(lambda client, _: client.sendall(b'{"state":"native_queued","native_id":"other","launch_id":"launch","message_id":"message"}\n'))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.executor.request(self.session, {'message_id': 'message'})

    def test_dribbling_peer_cannot_extend_total_deadline(self):
        def handler(client, _):
            for _ in range(30):
                client.sendall(b' ')
                time.sleep(.02)
        self.serve(handler)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.executor.request(self.session, {'message_id': 'message'}, timeout=.1)
        self.assertLess(time.monotonic() - start, .3)

    def test_missing_endpoint_is_definite_pre_submission_failure(self):
        result = self.executor.request(self.session, {'message_id': 'message'})
        self.assertEqual(result.state, 'error')
        self.assertIn('before submission', result.reason)
