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

import asyncio
import json
import logging
import os
import sys
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ed25519
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from secondbrain import protocol as p  # noqa: E402
from src import sb_connector as C  # noqa: E402

# A frame type that will never exist. It MUST be fictional: this file first used
# "transcript_ack", which stopped testing anything the moment that frame was vendored --
# decode began succeeding and three tests went green for the wrong reason. A real
# future frame tests skew only until it arrives, which is exactly when you stop
# noticing. Same shape as testing a vocabulary you do not own with a made-up member.
FUTURE = json.dumps({
    "t": "frobnicate_widget", "workspace_id": "d:/ws", "agent": "Alice",
    "payload": {"whatever": 1},
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
        self.assertIn(json.loads(FUTURE)["t"], blob)  # derived, never a second copy
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
                # assertNoLogs OUTSIDE assertRaises, deliberately. Its __exit__ skips the
                # assertion entirely when an exception is propagating, so the intuitive
                # nesting (raises outside, no-logs inside) asserts NOTHING about logs --
                # it passed against a mutant that logged for every ProtocolError.
                with self.assertNoLogs("sb_connector", level=logging.ERROR):
                    with self.assertRaises(p.ProtocolError):
                        await _conn()._recv(FakeWS([raw]))



class ClosesWith4400Tests(unittest.IsolatedAsyncioTestCase):
    """A bare drop gives SB an abrupt 1006, indistinguishable from a network fault --
    the same complaint that motivated naming the frame locally, mirrored onto the host,
    where the audit log is all they have. §10 documents 4400 as the response.

    This drives the REAL run() loop rather than reconstructing the close. A first draft
    rebuilt the try/except in the test and passed against a mutant that changed the
    production close -- it was exercising a copy, and would have passed with the close
    deleted outright.
    """

    async def test_run_closes_4400_when_the_stream_hits_a_protocol_error(self):
        import tempfile

        closed: list[tuple] = []

        class WS:
            async def close(self, code=None, reason=None):
                closed.append((code, reason))

        class Connecting:
            def __call__(self, *a, **kw):
                return self

            async def __aenter__(self):
                return WS()

            async def __aexit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            self.enterContext(patch.dict(os.environ, {"MULTIVERSE_ROOT": tmp}))
            key = ed25519.Ed25519PrivateKey.generate()
            ident = C.DeviceIdentity(
                device_id="dev", private_key=key, pinned_server_fingerprint="FP",
                granted_workspaces=["/ws"], device_name="t",
            )
            conn = C.SBConnector("ws://127.0.0.1:1", ident)

            async def boom(self, ws, from_seq):
                raise p.ProtocolError("unknown frame type 'frobnicate_widget'")

            async def noop(self, *a, **kw):
                return None

            async def handshake(self, ws):
                conn._stop.set()  # one pass through the loop, then leave
                return 0

            import websockets.asyncio.client as mod  # run() imports it lazily
            with patch.object(mod, "connect", Connecting()), \
                 patch.object(C.SBConnector, "_handshake", handshake), \
                 patch.object(C.SBConnector, "_emit_workspace_connected", noop), \
                 patch.object(C.SBConnector, "_pump", boom):
                await asyncio.wait_for(conn.run(), timeout=5)

        self.assertEqual(closed, [(4400, "undecodable frame")])

    def test_the_code_is_taken_from_the_vendored_enum(self):
        """Never the literal 4400: re-vendoring must carry any change, and a hardcoded
        number is the same drift hazard as a hardcoded frame-type list."""
        self.assertEqual(int(p.CloseCode.PROTOCOL_ERROR), 4400)


class TheFixtureIsStillFictionalTests(unittest.TestCase):
    def test_the_future_frame_type_is_not_one_the_protocol_knows(self):
        """If this ever fails, the fixture has become real and every skew test above is
        passing for the wrong reason. Assert the premise, not just the conclusion."""
        known = frozenset(ft.value for ft in p.FrameType)
        self.assertNotIn(json.loads(FUTURE)["t"], known)


class TheBranchIsStructuralTests(unittest.IsolatedAsyncioTestCase):
    """protocol.py distinguishes the two cases; this client must not re-derive it.

    Before, the branch compared the frame's 't' against a locally-built set -- a second
    source of truth for a distinction the contract already makes, and one more thing that
    could drift out of step with it.
    """

    async def test_unknown_type_raises_the_subclass_and_malformed_raises_the_parent(self):
        with self.assertRaises(p.UnknownFrameType):
            await _conn()._recv(FakeWS([FUTURE]))
        with self.assertRaises(p.ProtocolError) as caught:
            await _conn()._recv(FakeWS(['{"t": "ack"}']))  # known type, missing field
        self.assertNotIsInstance(caught.exception, p.UnknownFrameType)


if __name__ == "__main__":
    unittest.main()
