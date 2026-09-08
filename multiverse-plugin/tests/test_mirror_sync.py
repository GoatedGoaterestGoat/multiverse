"""Mirroring must not manufacture changes that invalidate dashboard caches."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import full_jsonl, mirror_sync


def encoded(*rows) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


class MirrorSyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.live = self.root / "live.jsonl"
        self.full = self.root / "mirrors" / "thread.jsonl"
        self.marker = self.full.with_suffix(".marker")
        self.cursor = self.full.with_suffix(".cursor.json")
        self.user = {"uuid": "u1", "type": "user", "parentUuid": None,
                     "message": {"role": "user", "content": "hello"}}
        self.metadata = {"type": "last-prompt", "lastPrompt": "hello"}

    def sync(self):
        return mirror_sync.sync_mirror(self.live, self.full, self.marker)

    def append(self, *rows):
        with self.live.open("ab") as stream:
            stream.write(encoded(*rows))

    def rows(self):
        return [json.loads(line) for line in self.full.read_bytes().splitlines()]

    def test_idle_uuidless_tail_never_grows_mirror_or_changes_mtime(self):
        self.live.write_bytes(encoded(self.user, self.metadata))
        self.assertEqual(self.sync(), 2)
        before = mirror_sync.signature(self.full)
        # The unchanged fast path doesn't even read either JSONL.
        with patch.object(Path, "read_bytes", side_effect=AssertionError("idle read")):
            for _ in range(5):
                self.assertEqual(self.sync(), 0)
        self.assertEqual(mirror_sync.signature(self.full), before)
        self.assertEqual(self.rows(), [self.user, self.metadata])

    def test_repeated_identical_metadata_is_preserved_once_per_occurrence(self):
        self.live.write_bytes(encoded(self.user, self.metadata))
        self.sync()
        for _ in range(3):
            self.append(self.metadata)
            self.assertEqual(self.sync(), 1)
            self.assertEqual(self.sync(), 0)
        self.assertEqual(self.rows(), [self.user] + [self.metadata] * 4)

    def test_legacy_tail_upgrade_is_idempotent_without_rewriting_existing_rows(self):
        for repeated in (1, 5):
            with self.subTest(repeated=repeated):
                self.full.parent.mkdir(exist_ok=True)
                # Historical mirrors may have many duplicate tail occurrences
                # and different serialization from live. Do not delete them.
                self.full.write_bytes(encoded(self.user, *[self.metadata] * repeated))
                self.marker.write_text("u1")
                self.cursor.unlink(missing_ok=True)
                self.live.write_text("\n".join(json.dumps(row, separators=(",", ":"))
                                               for row in (self.user, self.metadata)) + "\n")
                before = mirror_sync.signature(self.full)
                self.assertEqual(self.sync(), 0)
                self.assertEqual(self.sync(), 0)
                self.assertEqual(mirror_sync.signature(self.full), before)
                self.append({"type": "cost-state", "cost": 2})
                self.assertEqual(self.sync(), 1)

    def test_partial_legacy_tail_overlap_only_appends_unmirrored_suffix(self):
        extra = {"type": "cost-state", "cost": 2}
        self.full.parent.mkdir()
        self.full.write_bytes(encoded(self.user, self.metadata))
        self.marker.write_text("u1")
        self.live.write_bytes(encoded(self.user, self.metadata, extra))
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.rows(), [self.user, self.metadata, extra])

    def test_blank_lines_do_not_break_legacy_tail_matching(self):
        self.full.parent.mkdir()
        self.full.write_bytes(encoded(self.user, self.metadata))
        self.marker.write_text("u1")
        self.live.write_bytes(encoded(self.user) + b"\n" + encoded(self.metadata) + b" \n")
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.rows(), [self.user, self.metadata])

    def test_append_during_read_is_not_mistaken_for_already_consumed_bytes(self):
        self.live.write_bytes(encoded(self.user))
        self.sync()
        self.append(self.metadata)
        real_read = Path.read_bytes
        extra = {"type": "cost-state", "cost": 2}
        def read_then_append(path):
            data = real_read(path)
            if path == self.live:
                self.append(extra)
            return data
        with patch.object(Path, "read_bytes", read_then_append):
            self.assertEqual(self.sync(), 1)
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.rows(), [self.user, self.metadata, extra])

    def test_partial_records_wait_for_newline_at_bootstrap_and_append(self):
        self.live.write_bytes(encoded(self.user).rstrip(b"\n"))
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.full.read_bytes(), b"")
        with self.live.open("ab") as stream:
            stream.write(b"\n" + encoded(self.metadata)[:8])
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.sync(), 0)
        with self.live.open("ab") as stream:
            stream.write(encoded(self.metadata)[8:])
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.rows(), [self.user, self.metadata])

    def test_compaction_reanchors_and_heals_unknown_parents(self):
        self.live.write_bytes(encoded(self.user, self.metadata))
        self.sync()
        synthetic = {"uuid": "compact-tip", "type": "user"}
        new = {"uuid": "u2", "type": "assistant", "parentUuid": "compact-tip"}
        self.live.write_bytes(encoded(synthetic, self.metadata, new))
        before = self.full.read_bytes()
        self.assertEqual(self.sync(), 0)  # wait for compaction to set marker
        self.assertEqual(self.full.read_bytes(), before)
        self.marker.write_text("compact-tip")
        self.assertEqual(self.sync(), 2)  # identical metadata is genuinely new
        self.assertEqual(self.rows()[-1]["parentUuid"], "u1")
        self.assertEqual(self.marker.read_text(), "u2")
        self.assertEqual(self.sync(), 0)

    def test_rewritten_prefix_with_same_marker_invalidates_byte_cursor(self):
        self.live.write_bytes(encoded(self.user, self.metadata))
        self.sync()
        new = {"uuid": "u2", "parentUuid": "missing", "type": "assistant"}
        self.live.write_bytes(encoded({"type": "metadata"}, self.user, self.metadata, new))
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.rows()[-1]["parentUuid"], "u1")
        self.assertEqual(len(self.rows()), 3)

    def test_replaced_full_invalidates_cursor_even_with_same_mtime(self):
        self.live.write_bytes(encoded(self.user, self.metadata))
        self.sync()
        old = self.full.stat()
        replacement = self.root / "replacement"
        replacement.write_bytes(encoded(self.user))
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(self.full)
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.rows(), [self.user, self.metadata])

    def test_cursor_write_failure_retries_without_duplicating_metadata(self):
        self.live.write_bytes(encoded(self.user))
        self.sync()
        self.append(self.metadata)
        with patch.object(mirror_sync, "atomic_write_json", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.sync()
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.rows(), [self.user, self.metadata])

    def test_metadata_only_file_and_missing_or_invalid_cursor(self):
        self.live.write_bytes(encoded(self.metadata, self.metadata))
        self.assertEqual(self.sync(), 2)
        for cursor in ("", "null", "[1]", "{broken"):
            self.cursor.write_text(cursor)
            self.assertEqual(self.sync(), 0)
            self.assertEqual(len(self.rows()), 2)

    def test_hook_and_compaction_share_cursor_and_lock(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "mirror-jsonl.py"
        spec = importlib.util.spec_from_file_location("mirror_hook", path)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        self.live.write_bytes(encoded(self.user, self.metadata))
        with (
            patch.object(full_jsonl, "_full_dir", return_value=self.full.parent),
            patch.object(full_jsonl, "live_jsonl_path", return_value=self.live),
            patch.object(hook, "_full_dir", return_value=self.full.parent),
            patch.object(hook, "_live", return_value=self.live),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            futures = [pool.submit(hook._sync if n % 2 else full_jsonl.sync_full,
                                   "thread", "/workspace") for n in range(24)]
            for future in futures:
                future.result()
        self.assertEqual(self.rows(), [self.user, self.metadata])

    def test_overlap_matches_naive_reference_including_repeated_patterns(self):
        sequences = [list(p) for n in range(5) for p in product((b"a", b"b"), repeat=n)]
        for prefix in sequences:
            for tail in sequences:
                expected = max([0] + [n for n in range(1, min(len(prefix), len(tail)) + 1)
                                      if tail[-n:] == prefix[:n]])
                self.assertEqual(mirror_sync._tail_overlap(deque(tail), prefix), expected)


class FullTokenCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.full = self.root / "thread.jsonl"
        self.full.write_bytes(b"{}\n")
        patcher = patch.object(full_jsonl, "_full_dir", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        full_jsonl._FULL_TOTAL_CACHE.clear()
        self.addCleanup(full_jsonl._FULL_TOTAL_CACHE.clear)
        self.forest = [SimpleNamespace(user_prompt=SimpleNamespace(tokens=7),
                                       leaves=[SimpleNamespace(tokens=12)]),
                       SimpleNamespace(user_prompt=None, leaves=[SimpleNamespace(tokens=3)])]

    def count(self):
        return full_jsonl.total_token_count_cached("thread", "/workspace")

    def test_repeated_counts_are_read_only_and_parse_once(self):
        before = mirror_sync.signature(self.full)
        with (
            patch.object(full_jsonl, "sync_full", side_effect=AssertionError("must not sync")),
            patch("compaction.parser.parse_session", return_value=self.forest) as parse,
        ):
            for _ in range(5):
                self.assertEqual(self.count(), 22)
            parse.assert_called_once_with(str(self.full))
        self.assertEqual(mirror_sync.signature(self.full), before)
        self.assertEqual(list(self.root.iterdir()), [self.full])

    def test_changed_or_replaced_mirror_invalidates_cache(self):
        with patch("compaction.parser.parse_session", return_value=self.forest) as parse:
            self.count()
            self.full.write_bytes(b"{}\n{}\n")
            self.count()
            old = self.full.stat()
            other = self.root / "replacement"
            other.write_bytes(self.full.read_bytes())
            os.utime(other, ns=(old.st_atime_ns, old.st_mtime_ns))
            other.replace(self.full)
            self.count()
            self.assertEqual(parse.call_count, 3)

    def test_racing_append_is_not_cached_as_the_new_file(self):
        def parse_and_append(_):
            with self.full.open("ab") as stream:
                stream.write(b"{}\n")
            return self.forest
        with patch("compaction.parser.parse_session", side_effect=parse_and_append) as parse:
            self.count()
            self.count()
            self.assertEqual(parse.call_count, 2)
            self.assertNotIn(str(self.full), full_jsonl._FULL_TOTAL_CACHE)

    def test_missing_mirror_stays_missing(self):
        self.full.unlink()
        with patch.object(full_jsonl, "sync_full") as sync:
            self.assertEqual(self.count(), 0)
            sync.assert_not_called()
        self.assertFalse(self.full.exists())


if __name__ == "__main__":
    unittest.main()
