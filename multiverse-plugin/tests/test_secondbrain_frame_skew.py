"""A frame type this build predates is NAMED before it is fatal.

SB and the connector ship independently, so meeting a newly-added frame is routine. It
stays fatal on purpose -- an unknown ``t`` is unclassifiable, so skipping it is a
confident claim that it did not matter, and nothing can tell whether it carried state its
sender now believes we accepted. Skew is prevented at the sender via hello.supports.

What this fixes is the DIAGNOSIS: the connect loop's generic handler logged "connection
lost", so the operator saw a reconnect loop with no cause and a remote deployment looked
like a flaky network. The tests that matter assert the frame type reaches the log, since
a correct-but-anonymous failure is the thing being repaired.
"""

from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from secondbrain import protocol as p  # noqa: E402
from src import sb_connector as C  # noqa: E402

FUTURE = json.dumps({
    "t": "transcript_ack", "workspace_id": "d:/ws", "agent": "Alice",
    "cursor": {"claude_uuid": "u", "turn_id": "t1", "row_uuid": "r1"},
})


class FakeWS:
    def __init__(self, raws):
        self._raws = list(raws)

    async def recv(self):
        if self._raws:
            return self._raws.pop(0)
        raise ConnectionError("no more frames")


def _conn() -> C.SBConnector:
    conn = object.__new__(C.SBConnector)
    conn.channel = None
    return conn


class UnknownFrameTypeTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_frame_type_is_named_in_the_log(self):
        """The repair: an operator must be able to tell this from a network fault."""
        with self.assertLogs("sb_connector", level=logging.ERROR) as logs:
            with self.assertRaises(p.ProtocolError):
                await _conn()._recv(FakeWS([FUTURE]))
        blob = "\n".join(logs.output)
        self.assertIn("transcript_ack", blob)
        self.assertIn("re-vendor", blob.lower())

    async def test_it_stays_fatal(self):
        """Skipping an unclassifiable frame would trade a loud failure for a silent
        divergence -- the sender believing state was accepted that never was."""
        with self.assertLogs("sb_connector", level=logging.ERROR):
            with self.assertRaises(p.ProtocolError):
                await _conn()._recv(FakeWS([FUTURE]))

    async def test_a_known_frame_still_decodes(self):
        frame = await _conn()._recv(FakeWS([json.dumps({"t": "ack", "ack_seq": 7})]))
        self.assertIs(frame.T, p.FrameType.ACK)
        self.assertEqual(frame.ack_seq, 7)

    async def test_a_malformed_frame_raises_without_blaming_skew(self):
        """A body we cannot parse is a real protocol error, not version skew. Naming it
        as skew would send the operator to re-vendor over a corrupt frame."""
        for raw in ('{"t": "ack"', '"not an object"', '{"no_t": 1}', '{"t": 42}',
                    json.dumps({"t": "ack"})):  # known type, missing ack_seq
            with self.subTest(raw=raw):
                with self.assertRaises(p.ProtocolError):
                    with self.assertNoLogs("sb_connector", level=logging.ERROR):
                        await _conn()._recv(FakeWS([raw]))


class KnownTypesAreDerivedTests(unittest.TestCase):
    def test_the_known_set_comes_from_the_vendored_enum(self):
        """Never a hardcoded list: re-vendoring must update it for free, or the skew
        detector becomes the next thing to drift."""
        self.assertEqual(C._KNOWN_FRAME_TYPES, frozenset(ft.value for ft in p.FrameType))
        for expected in ("ack", "query", "action", "lock", "bye"):
            self.assertIn(expected, C._KNOWN_FRAME_TYPES)


if __name__ == "__main__":
    unittest.main()
