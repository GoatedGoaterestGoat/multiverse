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
from multiverse.runtime.native import NativeExecutor


class ExtensionTransportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(prefix='mv2-socket-', dir='/tmp'))
        self.executor = NativeExecutor(self.temporary)
        self.enterContext(patch.object(self.executor, 'check_session'))

    def endpoint(self, name):
        self.path = str(Path(self.temporary) / f'{name}.sock')
        self.session = SessionHandle('agent', 'pi', self.temporary, 'launch', 'native',
                                     endpoint=NativeEndpoint('unix-json', self.path))

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

    def test_pi_socket_client_receipts_and_deadlines(self):
        """A Pi receipt counts only for the exact native, launch and message ids even when UTF-8 splits byte by byte, a
        slow peer cannot stretch the total deadline, and a missing socket is a definite pre-submission failure;
        fragile because socket reads split anywhere, and a misattributed receipt marks a lost message delivered."""
        with self.subTest('a receipt split inside a character'):
            self.endpoint('split')
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
        with self.subTest('a receipt for another native session'):
            self.endpoint('other')
            self.serve(lambda client, _: client.sendall(b'{"state":"native_queued","native_id":"other","launch_id":"launch","message_id":"message"}\n'))
            with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                self.executor.request(self.session, {'message_id': 'message'})
        with self.subTest('a peer that dribbles bytes'):
            self.endpoint('dribble')
            def handler(client, _):
                for _ in range(30):
                    client.sendall(b' ')
                    time.sleep(.02)
            self.serve(handler)
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                self.executor.request(self.session, {'message_id': 'message'}, timeout=.1)
            self.assertLess(time.monotonic() - start, .3)
        with self.subTest('no socket'):
            self.endpoint('missing')
            result = self.executor.request(self.session, {'message_id': 'message'})
            self.assertEqual(result.state, 'error')
            self.assertIn('before submission', result.reason)
