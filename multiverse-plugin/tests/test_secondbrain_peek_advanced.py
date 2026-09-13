"""``cursor_advances_marker`` — does committing this peek actually move the marker?

Exists because ``peek_partial_transcript`` refuses to anchor onto an in-progress turn
and therefore returns text it will return again. The event path absorbs that silently
(SecondBrain dedupes on ``event_id``); a PULL has no event id, so SB needs to be told.

The cases that matter are the ones where the answer is False, since a wrong True makes
SB present provisional text as finalized -- the failure this signal exists to prevent.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from src import sb_transcript as T  # noqa: E402

CWD = "/ws/proj"
AGENT = "Alice"


class CursorAdvancesMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.dict(os.environ, {"MULTIVERSE_ROOT": self.temp.name}))

    # -- the False cases: nothing finalized, everything returned will repeat ----

    def test_claude_cursor_equal_to_the_marker_is_not_an_advance(self):
        """Peek's caught-up and single-partial-turn paths both return the marker's own
        position as the cursor, so a commit is a deliberate no-op."""
        T._write_marker(CWD, AGENT, "u", "t1", "r1")
        cursor = {"claude_uuid": "u", "turn_id": "t1", "row_uuid": "r1"}
        self.assertFalse(T.cursor_advances_marker(CWD, AGENT, cursor))

    def test_codex_cursor_equal_to_the_marker_is_not_an_advance(self):
        cursor = {
            "runtime": "codex", "thread_id": "th", "record_offset": 42,
            "record_hash": "h", "turn_id": "ct", "item_id": "it",
        }
        T._write_codex_marker(CWD, AGENT, cursor)
        self.assertFalse(T.cursor_advances_marker(CWD, AGENT, cursor))

    def test_pi_cursor_equal_to_the_marker_is_not_an_advance(self):
        cursor = {"runtime": "pi", "session_id": "s", "entry_id": "e"}
        T._write_pi_marker(CWD, AGENT, cursor)
        self.assertFalse(T.cursor_advances_marker(CWD, AGENT, cursor))

    def test_a_junk_cursor_is_not_an_advance(self):
        """Never report progress for something that cannot be committed."""
        T._write_marker(CWD, AGENT, "u", "t1", "r1")
        self.assertFalse(T.cursor_advances_marker(CWD, AGENT, "not-a-dict"))

    # -- the True cases -------------------------------------------------------

    def test_no_marker_yet_counts_as_an_advance(self):
        """Without a marker the peek delivered from the start of history."""
        self.assertTrue(
            T.cursor_advances_marker(CWD, AGENT, {"claude_uuid": "u", "turn_id": "t1", "row_uuid": "r1"})
        )

    def test_a_later_turn_is_an_advance(self):
        T._write_marker(CWD, AGENT, "u", "t1", "r1")
        cursor = {"claude_uuid": "u", "turn_id": "t2", "row_uuid": "r2"}
        self.assertTrue(T.cursor_advances_marker(CWD, AGENT, cursor))

    def test_the_same_turn_with_a_new_tip_row_is_an_advance(self):
        """Codex advances WITHIN a turn, so turn_id alone cannot decide this -- the tip
        row is what moves. A comparison that only looked at turn_id would report no
        progress for a Codex agent that is making some."""
        T._write_marker(CWD, AGENT, "u", "t1", "r1")
        cursor = {"claude_uuid": "u", "turn_id": "t1", "row_uuid": "r9"}
        self.assertTrue(T.cursor_advances_marker(CWD, AGENT, cursor))

    def test_a_moved_codex_offset_is_an_advance(self):
        cursor = {
            "runtime": "codex", "thread_id": "th", "record_offset": 42,
            "record_hash": "h", "turn_id": "ct", "item_id": "it",
        }
        T._write_codex_marker(CWD, AGENT, cursor)
        self.assertTrue(T.cursor_advances_marker(CWD, AGENT, {**cursor, "record_offset": 99}))

    # -- the property that keeps it working for a runtime that does not exist yet --

    def test_it_interprets_no_cursor_field(self):
        """A runtime minting an unfamiliar cursor shape must still be answered correctly,
        because the marker writers copy the cursor's own keys. Nothing here may hard-code
        claude_uuid / record_offset / entry_id."""
        cursor = {"runtime": "quux", "warp_id": "w1", "frobnicator": 7}
        marker = {"cwd": CWD, "agent_name": AGENT, "updated_at": "now", **cursor}
        with patch.object(T, "read_marker", return_value=marker):
            self.assertFalse(T.cursor_advances_marker(CWD, AGENT, cursor))
            self.assertTrue(T.cursor_advances_marker(CWD, AGENT, {**cursor, "frobnicator": 8}))


if __name__ == "__main__":
    unittest.main()
