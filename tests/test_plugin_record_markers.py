"""Owned universe and history markers must not disappear during a single filesystem observation."""
from contextlib import contextmanager
import errno
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import store
from multiverse.plugins import api, events, harness, host, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import plugins as tool


class MarkerTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        pause = mock.patch.object(store.time, "sleep")
        pause.start()
        self.addCleanup(pause.stop)
        for table in (host._finished, host._saved, host._cursor_journals, host._journals):
            before = dict(table)
            self.addCleanup(lambda table=table, before=before: (table.clear(), table.update(before)))
            table.clear()

    @contextmanager
    def missing(self, path, lasting=False):
        original, calls = Path.stat, []
        def stat(current, *args, **kwargs):
            if current == path:
                calls.append(current)
                if lasting or len(calls) == 1:
                    raise FileNotFoundError(errno.ENOENT, "missing marker", str(path))
            return original(current, *args, **kwargs)
        with mock.patch.object(Path, "stat", stat):
            yield calls

    def test_universe_markers_preserve_ownership_and_lookup(self):
        """A missed universe marker can drop events, omit launch plugins or bypass a local plugin's trust check."""
        directory = self.agent("worker")
        plugin_dir = self.root / "plugins" / "local"
        marker = self.root / "universe.json"
        for read, present, absent in (
            (lambda: events.root_of(directory), self.root, None),
            (lambda: harness.universe_of(directory), self.root, None),
            (lambda: tool._universe_plugin(plugin_dir), True, False),
        ):
            with self.missing(marker):
                self.assertEqual(read(), present)
            with self.missing(marker, lasting=True):
                self.assertEqual(read(), absent)
        looked_up = object()
        plugin = SimpleNamespace(universe=lambda root: SimpleNamespace(agent=lambda name: looked_up))
        with self.missing(marker):
            self.assertIs(api.Plugin.agent_at(plugin, directory), looked_up)
        with self.missing(marker, lasting=True):
            with self.assertRaises(api.Error):
                api.Plugin.agent_at(plugin, directory)
        for lasting in (False, True):
            with mock.patch.object(sys, "pycache_prefix", None), self.missing(marker, lasting):
                loader._keep_bytecode_out(plugin_dir)
                self.assertEqual(sys.pycache_prefix is None, lasting)

    def test_host_marker_checks_preserve_progress_without_recreating_absent_universes(self):
        """Transient marker misses must not lose cursors or completion records; permanent absence still stops writes."""
        marker = self.root / "universe.json"
        handler = lambda event: None
        loaded = SimpleNamespace(name="probe", registrations=SimpleNamespace(handlers=[("*", handler)]))
        key = ("probe", str(self.root), 0)
        cursor = host._cursor_path(self.root, "probe")
        progress = cursor.with_name(host.HANDLERS)
        for lasting in (False, True):
            cursor.unlink(missing_ok=True)
            with self.missing(marker, lasting):
                host._write_cursor(self.root, "probe", 7, "journal")
            self.assertEqual(cursor.exists(), not lasting)
            progress.unlink(missing_ok=True)
            host._saved.clear()
            host._finished[key] = 7
            with self.missing(marker, lasting):
                host._save_progress(loaded, self.root, "probe", "journal")
            self.assertEqual(progress.exists(), not lasting)
            with self.missing(marker, lasting), mock.patch.object(events, "journal_id", return_value="journal"), \
                    mock.patch.object(host, "_read_cursor", return_value=7), \
                    mock.patch.object(host, "_restore_progress"), mock.patch.object(host, "_save_progress"), \
                    mock.patch.object(events, "read", return_value=([], None)) as read:
                host._deliver(loaded, SimpleNamespace(universe=lambda root: None), self.root, "probe")
            self.assertEqual(read.call_count, 0 if lasting else 1)

    def test_cursor_generation_marker_is_rewritten_only_after_lasting_absence(self):
        """One missing stat must not replace an intact generation marker while its paired cursor is updated."""
        for lasting in (False, True):
            host._write_cursor(self.root, "probe", 1, "journal")
            marker = host._cursor_journal_path(self.root, "probe")
            inode = marker.stat().st_ino
            with self.missing(marker, lasting):
                host._write_cursor(self.root, "probe", 2, "journal")
            self.assertEqual(marker.stat().st_ino == inode, not lasting)

    def test_session_history_presence_reads_append_only_evidence_afresh(self):
        """Append-only absence must not delay transcript reads or hide later journal evidence; replaced runtime gaps still cannot select this HOME."""
        directory = self.root / "state" / "agents" / "worker"
        directory.mkdir(parents=True, exist_ok=True)
        runtime = directory / "runtime.json"
        store.write(runtime, {})
        journal = directory / "sessions.jsonl"
        with mock.patch.object(store.time, "sleep", side_effect=AssertionError("Append-only absence must not wait")):
            self.assertTrue(api._never_launched(directory, {}))
            with journal.open("a") as stream:
                stream.write('{}\n')
            self.assertFalse(api._never_launched(directory, {}))
            journal.unlink()
            self.assertTrue(api._never_launched(directory, {}))
        # A replaced runtime is a different protocol, including when no journal was ever created.
        store.write(runtime, {"launch_id": "previous"})
        with self.missing(runtime) as calls:
            self.assertFalse(api._never_launched(directory, {}))
            self.assertGreater(len(calls), 1)
