"""Bounded decoder ownership for synthetic compressed rollout reads."""
from contextlib import ExitStack
from concurrent.futures import Future
from threading import Thread
import gc
import io
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from codex_compression import compress, BAD_FRAME
from multiverse.harnesses.codex import rollout
from test_multiverse import BaseTest


@unittest.skipIf(sys.version_info >= (3, 14), 'python-zstandard fallback ownership; built-in decoder is separate')
class DecoderPoolTest(BaseTest):
    def setUp(self):
        super().setUp()
        pool = getattr(rollout, '_decompressors', [])
        pool.clear()
        self.addCleanup(pool.clear)
        self.path = self.work / 'rollout.jsonl.zst'
        self.payload = b'line of synthetic rollout data\n' * 10000
        self.path.write_bytes(compress(self.payload, content_size=False))

    def test_repeated_and_overlapping_reads_keep_only_bounded_idle_decoders(self):
        """Repeated dashboard reads must reuse native contexts without sharing an active stream or retaining sources."""
        import zstandard
        constructor = zstandard.ZstdDecompressor
        with patch.object(zstandard, 'ZstdDecompressor', side_effect=constructor) as made:
            for _ in range(12):
                result = Future()
                def read_in_fresh_thread():
                    try:
                        with rollout.open_rollout(self.path) as handle:
                            self.assertEqual(handle.read(len(self.payload)), self.payload)
                            source = weakref.ref(handle.source)
                        result.set_result(source)
                    except BaseException as error:
                        result.set_exception(error)
                thread = Thread(target=read_in_fresh_thread)
                thread.start()
                thread.join(5)
                self.assertFalse(thread.is_alive(), 'The synthetic compressed read must finish')
                source = result.result(timeout=1)
                gc.collect()
                self.assertIsNone(source(), 'An idle decoder must not retain the previous rollout source')
            self.assertEqual(made.call_count, 1)
            with ExitStack() as stack:
                readers = [stack.enter_context(rollout.open_rollout(self.path)) for _ in range(8)]
                for reader in readers:
                    self.assertEqual(reader.read(1), self.payload[:1])
                self.assertEqual(made.call_count, 8, 'Each overlapping stream needs an exclusive context')
                self.assertEqual(len(rollout._decompressors), 0)
                for reader in reversed(readers):
                    self.assertEqual(reader.read(len(self.payload)), self.payload[1:])
            self.assertEqual(len(rollout._decompressors), 4)
            count = made.call_count
            with rollout.open_rollout(self.path) as handle:
                self.assertEqual(handle.read(32), self.payload[:32])
                handle.seek(0)
                self.assertEqual(handle.read(len(self.payload)), self.payload)
            self.assertEqual(made.call_count, count)

    def test_failed_reads_and_close_never_return_a_still_active_context(self):
        """Cancellation, corrupt data and failed stream construction or close must not release a live decoder to another reader."""
        import zstandard
        constructor = zstandard.ZstdDecompressor
        with patch.object(zstandard, 'ZstdDecompressor', side_effect=constructor) as made:
            self.path.write_bytes(BAD_FRAME)
            for _ in range(10):
                with self.assertRaises(rollout.RolloutError):
                    with rollout.open_rollout(self.path) as handle:
                        handle.read(1)
            self.path.write_bytes(compress(self.payload))
            class Cancelled(Exception):
                pass
            with self.assertRaises(Cancelled):
                with rollout.open_rollout(self.path) as handle:
                    handle.read(1)
                    raise Cancelled
            with rollout.open_rollout(self.path) as handle:
                self.assertEqual(handle.read(len(self.payload)), self.payload)
            self.assertEqual(made.call_count, 1)
        rollout._decompressors.clear()
        failures = {'construct': True, 'close': True}
        class Stream(io.BytesIO):
            def close(self):
                if failures['close']:
                    raise OSError('synthetic close failure')
                super().close()
        class Context:
            def __init__(self, **kwargs):
                pass
            def stream_reader(self, source, **kwargs):
                if failures['construct']:
                    raise ValueError('synthetic constructor failure')
                return Stream(b'plain')
        codec = SimpleNamespace(ZstdDecompressor=Context, ZstdError=ValueError)
        with patch.object(rollout, 'import_module', return_value=codec):
            make, _ = rollout.decoder()
            with self.assertRaises(ValueError):
                make(io.BytesIO())
            self.assertEqual(rollout._decompressors, [])
            failures['construct'] = False
            stream = make(io.BytesIO())
            with self.assertRaises(OSError):
                stream.close()
            self.assertEqual(rollout._decompressors, [])
            failures['close'] = False
            stream = make(io.BytesIO())
            self.assertEqual(stream.read(), b'plain')
            stream.close()
            stream.close()
            self.assertEqual(len(rollout._decompressors), 1)
