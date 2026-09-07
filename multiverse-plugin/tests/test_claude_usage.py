"""``claude_usage.context_window_cached``: exact recorded capacity or None.

The reader never infers a capacity — a bare model id in a transcript cannot
tell a 200k session from a ``[1m]`` one — so every test here starts from a
recorded statusline payload (or the absence of one) and checks that the
dashboard sees exactly that number, only for that session, only while the
record exists.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import claude_statusline, claude_usage
from src.full_jsonl import cwd_slug, live_jsonl_path

WORKSPACE = "/Users/someone/Documents/Project"
SESSION = "11111111-2222-3333-4444-555555555555"


def _payload(session_id: str = SESSION, size: object = 1_000_000, *, cwd: str = WORKSPACE) -> bytes:
    return json.dumps({
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": f"/home/x/.claude/projects/{cwd_slug(WORKSPACE)}/{session_id}.jsonl",
        "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
        "context_window": {"context_window_size": size, "used_percentage": 3},
    }).encode()


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"MULTIVERSE_ROOT": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MULTIVERSE_SESSION_CWD", None)
        claude_usage._CACHE.clear()
        self.addCleanup(claude_usage._CACHE.clear)

    def read(self, uuid: str | None = SESSION, cwd: str = WORKSPACE) -> int | None:
        return claude_usage.context_window_cached(uuid, cwd)

    def test_unknown_without_a_record_or_session(self) -> None:
        self.assertIsNone(self.read())
        self.assertIsNone(self.read(None))
        self.assertIsNone(self.read(""))

    def test_reads_the_exact_recorded_capacity(self) -> None:
        claude_statusline.record(_payload(size=1_000_000))
        self.assertEqual(self.read(), 1_000_000)
        claude_statusline.record(_payload(size=200_000))
        self.assertEqual(self.read(), 200_000)

    def test_record_and_live_transcript_share_the_workspace_key(self) -> None:
        # The recorder only sees transcript_path; the dashboard only has the
        # workspace. Both must land on the same slug, even after the session
        # has cd'ed into a subdirectory.
        self.assertEqual(
            claude_statusline.record_path(SESSION, WORKSPACE).parent.name,
            live_jsonl_path(SESSION, WORKSPACE).parent.name,
        )
        path = claude_statusline.record(_payload(cwd=WORKSPACE + "/deep/inside"))
        self.assertEqual(path, claude_statusline.record_path(SESSION, WORKSPACE))
        self.assertEqual(self.read(), 1_000_000)
        self.assertIsNone(self.read(cwd="/Users/someone/Elsewhere"))

    def test_never_infers_from_another_session_or_model(self) -> None:
        claude_statusline.record(_payload("99999999-2222-3333-4444-555555555555"))
        self.assertIsNone(self.read())
        # A record whose body names a different session is ignored even at
        # this session's path.
        path = claude_statusline.record_path(SESSION, WORKSPACE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_id": "other", "context_window_size": 1_000_000,
        }))
        self.assertIsNone(self.read())

    def test_invalid_sizes_and_malformed_records_are_unknown(self) -> None:
        path = claude_statusline.record_path(SESSION, WORKSPACE)
        path.parent.mkdir(parents=True, exist_ok=True)
        for body in (
            {"session_id": SESSION, "context_window_size": True},
            {"session_id": SESSION, "context_window_size": 0},
            {"session_id": SESSION, "context_window_size": -5},
            {"session_id": SESSION, "context_window_size": "200000"},
            {"session_id": SESSION, "context_window_size": 200000.0},
            {"session_id": SESSION},
            [SESSION, 200000],
        ):
            path.write_text(json.dumps(body))
            self.assertIsNone(self.read(), body)
        path.write_bytes(b'{"session_id": "' + SESSION.encode() + b'", "context_window_size": 1')
        self.assertIsNone(self.read())
        path.write_bytes(b"\xff\xfe")
        self.assertIsNone(self.read())

    def test_malformed_ids_and_workspaces_never_raise(self) -> None:
        for uuid in ("../escape", "..", ".", "a/b", "id\x00", "x" * 201, SESSION + "\n"):
            self.assertIsNone(self.read(uuid), uuid)
        self.assertIsNone(self.read(SESSION, None))  # type: ignore[arg-type]
        self.assertIsNone(self.read(SESSION, ""))

    def test_unchanged_record_costs_a_stat_not_a_parse(self) -> None:
        claude_statusline.record(_payload(size=1_000_000))
        self.assertEqual(self.read(), 1_000_000)
        with patch.object(claude_usage, "parse_record", side_effect=AssertionError("re-parsed")):
            self.assertEqual(self.read(), 1_000_000)
            self.assertEqual(self.read(), 1_000_000)
        claude_statusline.record(_payload(size=200_000))
        self.assertEqual(self.read(), 200_000)

    def test_deleted_record_reads_unknown_then_recovers(self) -> None:
        path = claude_statusline.record(_payload())
        self.assertEqual(self.read(), 1_000_000)
        path.unlink()
        self.assertIsNone(self.read())
        claude_statusline.record(_payload(size=200_000))
        self.assertEqual(self.read(), 200_000)

    def test_cache_is_bounded(self) -> None:
        with patch.object(claude_usage, "_CACHE_LIMIT", 2):
            for i in range(5):
                self.read(f"{i:08d}-2222-3333-4444-555555555555")
            self.assertEqual(len(claude_usage._CACHE), 2)

    def test_assumes_one_million_until_measured(self) -> None:
        os.environ.pop("MULTIVERSE_CLAUDE_CONTEXT_WINDOW", None)
        assumed = lambda: claude_usage.context_window_assumed(SESSION, WORKSPACE)
        self.assertEqual(assumed(), 1_000_000)
        self.assertEqual(claude_usage.context_window_assumed(None, WORKSPACE), 1_000_000)
        claude_statusline.record(_payload(size=200_000))
        self.assertEqual(assumed(), 200_000)
        claude_statusline.record_path(SESSION, WORKSPACE).unlink()
        self.assertEqual(assumed(), 1_000_000)
        with patch.dict(os.environ, {"MULTIVERSE_CLAUDE_CONTEXT_WINDOW": "200000"}):
            self.assertEqual(assumed(), 200_000)
            claude_statusline.record(_payload(size=1_000_000))
            self.assertEqual(assumed(), 1_000_000)
            claude_statusline.record_path(SESSION, WORKSPACE).unlink()
        for bad in ("", "0", "-1", "1e6", "lots", "²", "9" * 5000):
            with patch.dict(os.environ, {"MULTIVERSE_CLAUDE_CONTEXT_WINDOW": bad}):
                self.assertEqual(assumed(), 1_000_000, bad)
        # The exact reader keeps its contract: unknown stays None.
        self.assertIsNone(self.read())

    def test_honors_multiverse_root_for_dormant_rows(self) -> None:
        # Records are durable files keyed by workspace + uuid, so a monitor
        # started later (a different process, an ended session) still sees
        # the measured capacity.
        claude_statusline.record(_payload())
        claude_usage._CACHE.clear()
        self.assertEqual(self.read(), 1_000_000)
        expected = self.root / "claude_statusline" / cwd_slug(WORKSPACE) / f"{SESSION}.json"
        self.assertTrue(expected.is_file())


if __name__ == "__main__":
    unittest.main()
