"""Focused reproduction of a journal created between a host's generation and event reads."""
from pathlib import Path
import importlib
import json
import shutil
import sys
import threading
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins, store
from multiverse.plugins import api, events, host
from multiverse.plugins.testing import PluginTestCase

SOURCE = '''
SEEN = []
STOP = None
def register(mv):
    def first(event):
        SEEN.append(("first", event.seq))
        if STOP is not None:
            STOP.set()
    def second(event):
        SEEN.append(("second", event.seq))
    mv.events.on("agent.reported", first)
    mv.events.on("agent.reported", second)
'''


class BirthTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.tables = (host._finished, host._timeouts, host._generations, host._journals, host._saved)
        for table in self.tables:
            table.clear()
            self.addCleanup(table.clear)
        self.write_plugin("probe", {"module": "plugin", "extends": ["events"], "events": ["agent.reported"]},
                          {"plugin.py": SOURCE})
        self.enable("probe")
        plugins.anchor_events(self.root, "probe")
        self.loaded_plugin = self.loaded("probe")
        self.module = importlib.import_module(self.loaded_plugin.package + ".plugin")
        self.mv = api.Plugin(self.loaded_plugin)

    def test_a_first_event_published_after_the_generation_read_survives_a_drain(self):
        """A draining worker must persist its completed handler even if the journal appeared after its first read."""
        stopped = self.module.STOP = threading.Event()
        original = events.journal_id
        published = []
        def journal_id(root):
            result = original(root)
            if not published:
                published.append(True)
                self.assertIsNone(result)
                self.assertIsNotNone(events.emit(root, "agent.reported", data={}))
            return result
        with mock.patch.object(events, "journal_id", journal_id):
            host._deliver(self.loaded_plugin, self.mv, self.root, "probe", stopped)
        self.assertEqual(self.module.SEEN, [], "a new journal's batch ran under the missing generation")
        host._deliver(self.loaded_plugin, self.mv, self.root, "probe", stopped)
        self.assertEqual(self.module.SEEN, [("first", 1)])
        progress = self.root / "state" / "plugins" / "probe" / host.HANDLERS
        self.assertEqual(store.read(progress)["journal"], events.journal_id(self.root))
        for table in self.tables:
            table.clear()  # The next worker has no in-memory completions.
        self.module.STOP = None
        host._deliver(self.loaded_plugin, self.mv, self.root, "probe")
        self.assertEqual(self.module.SEEN, [("first", 1), ("second", 1)])

    def test_a_legacy_journal_without_an_id_still_delivers(self):
        """Stable missing journal IDs must retain legacy delivery rather than waiting forever for a new event."""
        directory = events.directory(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        event = {"type": "agent.reported", "seq": 1, "at": store.now(), "data": {}}
        (directory / "000000000001.jsonl").write_text(json.dumps(event) + "\n")
        host._deliver(self.loaded_plugin, self.mv, self.root, "probe")
        self.assertEqual(self.module.SEEN, [("first", 1), ("second", 1)])
        self.assertIsNone(events.journal_id(self.root))
        self.assertEqual(host._read_cursor(self.root, "probe"), 1)

    def test_a_replaced_journal_defers_its_batch_and_keeps_previous_page_progress(self):
        """A generation change must not relabel a new batch or discard handlers completed on earlier pages."""
        events.emit(self.root, "agent.reported", data={})
        previous = events.journal_id(self.root)
        original, reads = events.read, []
        def read(root, after=0, limit=500):
            reads.append(after)
            if len(reads) == 2:
                shutil.rmtree(events.directory(root))
                events.emit(root, "agent.reported", data={})
                events.emit(root, "agent.reported", data={})
            return original(root, after, limit)
        with mock.patch.object(events, "read", read), mock.patch.object(host, "READ_LIMIT", 1):
            host._deliver(self.loaded_plugin, self.mv, self.root, "probe")
        self.assertEqual(self.module.SEEN, [("first", 1), ("second", 1)])
        progress = store.read(self.root / "state" / "plugins" / "probe" / host.HANDLERS)
        self.assertEqual(progress["journal"], previous)
        self.assertEqual(list(progress["finished"].values()), [1, 1])
        self.assertNotEqual(events.journal_id(self.root), previous)
        host._deliver(self.loaded_plugin, self.mv, self.root, "probe")
        self.assertEqual(self.module.SEEN, [("first", 1), ("second", 1),
                                           ("first", 1), ("second", 1), ("first", 2), ("second", 2)])
