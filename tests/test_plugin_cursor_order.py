"""PR #23 independent review (a): a crash between the cursor's two writes never makes a host skip a new journal's
events. The cursor number is written before the journal it belongs to, so an interrupted write can only mean
delivering again (at least once), never skipping."""
from pathlib import Path
import shutil
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import events, host
from test_plugin_delivery import Fixture


class CursorWriteOrderTest(Fixture):
    def restarted_journal(self):
        """Cursor 1000 in an old journal, then the journal removed and started again, now at seq 3."""
        self.emit(self.root, 1)
        old = events.journal_id(self.root)
        host._write_cursor(self.root, "pair", 1000, old)
        shutil.rmtree(events.directory(self.root))
        for _ in range(3):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"})
        new = events.journal_id(self.root)
        self.assertNotEqual(old, new)
        host._cursor_journals.clear()  # a fresh host process: what it last wrote is not remembered
        return new

    def interrupted(self, target_name, journal):
        """_write_cursor(3, journal) with the replace of `target_name` failing, as a crash there would."""
        real = Path.replace

        def replace(path, target):
            if Path(target).name == target_name:
                raise OSError("crashed here")
            return real(path, target)
        with mock.patch.object(Path, "replace", replace), self.assertRaises(OSError):
            host._write_cursor(self.root, "pair", 3, journal)
        host._cursor_journals.clear()

    def test_a_crash_at_either_write_never_skips_the_new_journal(self):
        for target in ("events.cursor", "events.cursor.journal"):
            with self.subTest(crash_at=target):
                journal = self.restarted_journal()
                self.interrupted(target, journal)
                after = host._read_cursor(self.root, "pair", journal)
                self.assertLessEqual(after, 3, f"a host would skip the new journal's events up to {after}")

    def test_an_older_host_still_reads_the_cursor(self):
        journal = self.restarted_journal()
        host._write_cursor(self.root, "pair", 3, journal)
        self.assertEqual(int(host._cursor_path(self.root, "pair").read_text().strip()), 3)
        self.assertEqual(host._read_cursor(self.root, "pair", journal), 3)
