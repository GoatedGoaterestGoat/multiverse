from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import codex_usage, groups, monitor


def _event(live=250, incoming=1000, cached=600, outgoing=80, context_window=258400) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 220,
                    "cached_input_tokens": 100,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 10,
                    "total_tokens": live,
                },
                "total_token_usage": {
                    "input_tokens": incoming,
                    "cached_input_tokens": cached,
                    "output_tokens": outgoing,
                    "reasoning_output_tokens": 50,
                    "total_tokens": incoming + outgoing,
                },
                "model_context_window": context_window,
            },
        },
    }


class RolloutTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / "sessions/2026/09/06/rollout-2026-09-06T00-00-00-thread.jsonl"
        self.path.parent.mkdir(parents=True)
        codex_usage._CACHE.clear()
        self.addCleanup(codex_usage._CACHE.clear)

    def append(self, record: object) -> None:
        with self.path.open("ab") as fh:
            fh.write(json.dumps(record).encode() + b"\n")

    def read(self) -> codex_usage.CodexTokenUsage:
        return codex_usage.token_usage_cached("thread", self.home)


class CodexUsageTests(RolloutTestCase):
    def test_matches_statusline_non_cached_input_plus_output(self) -> None:
        self.append(_event())
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage(250, 480, 258400))

    def test_latest_snapshot_is_not_summed_or_double_counted(self) -> None:
        self.append(_event())
        self.append(_event(300, 2000, 1200, 180))
        self.append(_event(300, 2000, 1200, 180))
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage(300, 980, 258400))

    def test_compaction_context_estimate_can_shrink_with_zero_breakdown(self) -> None:
        self.append(_event(200000))
        self.assertEqual(self.read().live_tokens, 200000)
        self.append({"type": "compacted", "payload": {"message": "summary"}})
        event = _event(12000)
        event["payload"]["info"]["last_token_usage"] = {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 12000,
        }
        self.append(event)
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage(12000, 480, 258400))

    def test_rate_limits_and_bad_records_preserve_last_measurement(self) -> None:
        self.append(_event())
        for record in (
            {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
            {"type": "response_item", "payload": _event()["payload"]},
            {"type": "event_msg", "payload": {"type": "token_count", "info": []}},
            {"type": "event_msg", "payload": "token_count"},
            ["token_count"],
            _event(True, True, False, True),
        ):
            self.append(record)
        with self.path.open("ab") as fh:
            fh.write(b'{"token_count":invalid}\n\xff"token_count"\n')
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage(250, 480, 258400))

    def test_partial_final_record_is_used_only_after_newline(self) -> None:
        self.append(_event())
        encoded = json.dumps(_event(500, 3000, 2000, 300)).encode()
        with self.path.open("ab") as fh:
            fh.write(encoded[:50])
        self.assertEqual(self.read().live_tokens, 250)
        with self.path.open("ab") as fh:
            fh.write(encoded[50:])
        self.assertEqual(self.read().live_tokens, 250)
        with self.path.open("ab") as fh:
            fh.write(b"\n")
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage(500, 1300, 258400))

    def test_unknown_until_first_usage_and_measured_zero_context(self) -> None:
        self.path.touch()
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage())
        self.append({"type": "session_meta", "payload": {"id": "thread"}})
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage())
        self.append(_event(0, 0, 0, 0))
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage(0, None, 258400))

    def test_statusline_clamps_negative_usage_components(self) -> None:
        self.append(_event(20, 10, 100, -2))
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage(20, None, 258400))
        self.append(_event(20, 10, -2, 4))
        self.assertEqual(self.read().total_tokens, 14)

    def test_unchanged_rollout_does_not_reopen_or_rediscover(self) -> None:
        self.append(_event())
        self.read()
        with (
            patch.object(Path, "open", side_effect=AssertionError("unexpected read")),
            patch.object(codex_usage, "find_rollout_path") as discover,
        ):
            self.assertEqual(self.read(), codex_usage.CodexTokenUsage(250, 480, 258400))
        discover.assert_not_called()

    def test_replacement_same_size_mtime_and_truncation_invalidate_cache(self) -> None:
        self.append(_event())
        self.read()
        st = self.path.stat()
        replacement = self.path.with_suffix(".tmp")
        replacement.write_text(json.dumps(_event(260)) + "\n")
        os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
        replacement.replace(self.path)
        self.assertEqual(self.read().live_tokens, 260)
        self.path.write_bytes(b"")
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage())

    def test_deletion_and_permission_failure_do_not_show_stale_counts(self) -> None:
        self.append(_event())
        self.read()
        self.append(_event(300))
        with patch.object(Path, "open", side_effect=PermissionError):
            self.assertEqual(self.read(), codex_usage.CodexTokenUsage())
        self.assertEqual(self.read().live_tokens, 300)
        self.path.unlink()
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage())

    def test_missing_path_discovery_retries_and_none_id_skips_discovery(self) -> None:
        with patch.object(codex_usage.time, "monotonic", return_value=0):
            self.assertEqual(self.read(), codex_usage.CodexTokenUsage())
        self.append(_event())
        with patch.object(codex_usage.time, "monotonic", return_value=6):
            self.assertEqual(self.read().live_tokens, 250)
        with patch.object(codex_usage, "find_rollout_path") as discover:
            self.assertEqual(codex_usage.token_usage_cached(None), codex_usage.CodexTokenUsage())
        discover.assert_not_called()

    def test_newer_rollout_is_discovered_after_path_cache_expires(self) -> None:
        self.append(_event())
        with patch.object(codex_usage.time, "monotonic", return_value=0):
            self.read()
        newer = self.path.with_name("rollout-2026-09-06T01-00-00-thread.jsonl")
        newer.write_text(json.dumps(_event(600)) + "\n")
        st = self.path.stat()
        os.utime(newer, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        with patch.object(codex_usage.time, "monotonic", return_value=31):
            self.assertEqual(self.read().live_tokens, 600)

    def test_cache_is_scoped_to_codex_home_and_thread(self) -> None:
        self.append(_event())
        self.read()
        other = self.home / "other-home"
        other_path = other / "sessions" / self.path.name
        other_path.parent.mkdir(parents=True)
        other_path.write_text(json.dumps(_event(700)) + "\n")
        with patch.dict(os.environ, {"CODEX_HOME": str(other)}):
            self.assertEqual(codex_usage.token_usage_cached("thread").live_tokens, 700)
            self.assertEqual(codex_usage.token_usage_cached("missing"), codex_usage.CodexTokenUsage())
        self.assertEqual(self.read().live_tokens, 250)

    def test_cache_is_bounded(self) -> None:
        with patch.object(codex_usage, "_CACHE_LIMIT", 2):
            for thread in ("a", "b", "c"):
                codex_usage.token_usage_cached(thread, self.home)
        self.assertEqual(len(codex_usage._CACHE), 2)
        self.assertNotIn((self.home, "a"), codex_usage._CACHE)

    def test_context_capacity_comes_from_latest_snapshot_not_a_model_default(self) -> None:
        self.append(_event(context_window=1_000_000))
        self.assertEqual(self.read().context_window, 1_000_000)
        self.append(_event(context_window=200_000))
        self.assertEqual(self.read().context_window, 200_000)
        # Do not carry an old model's capacity into a snapshot without one.
        event = _event()
        del event["payload"]["info"]["model_context_window"]
        self.append(event)
        self.assertEqual(self.read(), codex_usage.CodexTokenUsage(250, 480, None))

    def test_invalid_capacity_does_not_hide_valid_counters(self) -> None:
        for capacity in (None, 0, -1, True, "258400", 258400.0, {}, []):
            with self.subTest(capacity=capacity):
                self.append(_event(context_window=capacity))
                self.assertEqual(self.read(), codex_usage.CodexTokenUsage(250, 480, None))

    def test_reverse_reader_handles_chunk_boundaries_crlf_and_large_lines(self) -> None:
        for data in (b"", b"a", b"a\n", b"a\nb", b"a\nb\n", b"a\r\nb\r\n",
                     b"a" * 100 + b"\nb\n" + b"c" * 200 + b"\npartial"):
            for chunk_size in (1, 2, 3, 17, 64):
                with self.subTest(data=data[:10], chunk_size=chunk_size):
                    with patch.object(codex_usage, "_CHUNK_SIZE", chunk_size):
                        lines = list(codex_usage._complete_lines_reverse(io.BytesIO(data), len(data)))
                    expected = list(reversed(data.split(b"\n")[:-1])) if b"\n" in data else []
                    self.assertEqual(lines, expected)

    def test_initial_read_stops_at_latest_snapshot_in_large_rollout(self) -> None:
        self.append({"type": "response_item", "payload": {"text": "x" * 1_000_000}})
        self.append(_event())
        with patch.object(codex_usage, "_usage_from_record", wraps=codex_usage._usage_from_record) as parse:
            self.assertEqual(self.read().live_tokens, 250)
        self.assertEqual(parse.call_count, 1)


class CodexDashboardUsageTests(RolloutTestCase):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.app = monitor.MonitorApp([])
        self.row = monitor.DashboardRow(
            name="codex", cwd="/workspace", runtime=monitor.RUNTIME_CODEX,
            status="idle", status_ts=1, session_id="thread",
        )

    def draw(self) -> tuple:
        self.app.entries = [self.row]
        self.app._usage = self.app._collect_usage(self.app.entries)
        table = MagicMock()
        table.cursor_row = 0
        with (
            patch.object(self.app, "query_one", return_value=table),
            patch.object(monitor, "pending_inbox_count", return_value=0),
            patch.object(monitor, "auto_build_enabled") as build,
        ):
            self.app._redraw_table()
        build.assert_not_called()
        return table.add_row.call_args.args

    def test_wrapped_dormant_and_retired_sessions_render_native_counters(self) -> None:
        self.append(_event(125000, 2000000, 1500000, 80000))
        for status in ("idle", "working", "dormant", "retired"):
            with self.subTest(status=status):
                self.row.status = status
                self.row.session = None if status == "dormant" else monitor.Session(
                    id="wrapper-not-thread", cwd=self.row.cwd, created_at=1,
                    runtime=monitor.RUNTIME_CODEX, codex_session_id="thread",
                )
                cells = self.draw()
                self.assertEqual(len(cells), 10)
                self.assertEqual(cells[4], "[dim]-[/]")
                # Total is 3-sig-fig compact; Tokens stays exact.
                self.assertEqual(cells[6:8], ("[green]125,000[/]", "[green]580k[/]"))

    def test_redraw_refreshes_both_counters_after_native_update(self) -> None:
        self.append(_event())
        self.assertEqual(self.draw()[6:8], ("[green]250[/]", "[green]480[/]"))
        self.append(_event(100, 2000, 1500, 100))
        self.assertEqual(self.draw()[6:8], ("[green]100[/]", "[green]600[/]"))

    def test_context_colors_at_exact_thresholds_and_after_capacity_change(self) -> None:
        for live, color in ((0, "green"), (599, "green"), (600, "yellow"),
                            (799, "yellow"), (800, "red"), (1000, "red"), (1100, "red")):
            with self.subTest(live=live):
                self.append(_event(live=live, context_window=1000))
                self.assertEqual(self.draw()[6], f"[{color}]{live:,}[/]")
        self.append(_event(live=1100, context_window=2000))
        self.assertEqual(self.draw()[6], "[green]1,100[/]")
        self.append(_event(live=1100, context_window=None))
        self.assertEqual(self.draw()[6], "1,100")

    def test_total_colors_use_unrounded_native_used_tokens(self) -> None:
        for total, color in ((1_499_999, "green"), (1_500_000, "yellow"),
                             (2_499_999, "yellow"), (2_500_000, "red")):
            with self.subTest(total=total):
                self.append(_event(incoming=total + 1_000_000, cached=1_000_000, outgoing=0))
                self.assertEqual(self.draw()[7], f"[{color}]{monitor._fmt_tokens(total)}[/]")

    def test_unmeasured_threads_show_dashes(self) -> None:
        self.assertEqual(self.draw()[6:8], ("[dim]-[/]", "[dim]-[/]"))
        self.row.session_id = None
        self.assertEqual(self.draw()[6:8], ("[dim]-[/]", "[dim]-[/]"))

    def test_pi_stays_unknown_and_claude_keeps_its_own_sources(self) -> None:
        self.append(_event())
        self.row.runtime = monitor.RUNTIME_PI
        self.assertEqual(self.draw()[6:8], ("[dim]-[/]", "[dim]-[/]"))
        self.row.runtime = monitor.RUNTIME_CLAUDE
        self.row.claude_uuid = "claude-id"
        with (
            patch.object(monitor, "last_real_usage_total_cached", return_value=123) as live,
            patch.object(monitor, "total_token_count_cached", return_value=456) as total,
        ):
            self.assertEqual(self.app._agent_tokens(self.row), 123)
            self.assertEqual(self.app._agent_total(self.row), 456)
        live.assert_called_once_with("claude-id", "/workspace")
        total.assert_called_once_with("claude-id", "/workspace")

    def test_nested_folder_leaves_mixed_runtime_token_columns_blank(self) -> None:
        self.append(_event())
        claude = monitor.DashboardRow(
            name="claude", cwd="/workspace", status="idle", status_ts=1,
            claude_uuid="claude-id", session_id="claude-id",
        )
        pi = monitor.DashboardRow(
            name="pi", cwd="/workspace", runtime=monitor.RUNTIME_PI,
            status="idle", status_ts=1, session_id="pi-id",
        )
        state = groups.GroupState(
            groups={
                "parent": groups.Group("parent", "Mixed", collapsed=True),
                "child": groups.Group("child", "Nested", parent="parent"),
            },
            members={"codex": "child", "claude": "parent", "pi": "child"},
        )
        rows = groups.build_display_rows([self.row, claude, pi], state)
        self.assertEqual(len(rows), 1)
        with (
            patch.object(monitor, "pending_inbox_count", return_value=0),
            patch.object(monitor, "last_real_usage_total_cached") as live,
            patch.object(monitor, "total_token_count_cached") as total,
            patch.object(monitor, "codex_token_usage_cached") as codex,
        ):
            cells = self.app._group_cells(rows[0], 2)
        self.assertEqual(cells[6:8], ("", ""))
        live.assert_not_called()
        total.assert_not_called()
        codex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
