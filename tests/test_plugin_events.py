"""The event journal and the plugin host: recording, rolling, pruning, reading, handlers, services and cursors."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import api, events, host, loader
from multiverse.plugins.testing import PluginTestCase
from multiverse.store import write

LISTENER = """
import json, os
from pathlib import Path

def register(mv):
    @mv.events.on("agent.*")
    def seen(event):
        with open(os.environ["EVENTS_OUT"], "a") as out:
            out.write(json.dumps({"type": event.type, "seq": event.seq, "agent": event.agent,
                                  "universe": str(event.universe.path), "data": event.data}) + "\\n")

    @mv.events.on("events.gap")
    def gap(event):
        with open(os.environ["EVENTS_OUT"], "a") as out:
            out.write(json.dumps({"type": event.type, "data": event.data}) + "\\n")
"""

SERVICE = """
import os

def register(mv):
    def start(context):
        with open(os.environ["SERVICE_OUT"], "a") as out:
            out.write("started " + ",".join(sorted(u.name for u in context.universes)) + "\\n")
        context.stopping.wait()
    def stop():
        with open(os.environ["SERVICE_OUT"], "a") as out:
            out.write("stopped\\n")
    mv.service(start, stop)
"""

ONE_TYPE = """
SEEN = []

def register(mv):
    @mv.events.on("agent.status")
    def status(event):
        SEEN.append((event.type, event.seq))
"""


class JournalTest(PluginTestCase):
    def subscribe(self, patterns=("agent.*",), name="listener"):
        self.write_plugin(name, {"extends": ["events"], "events": list(patterns)})
        self.enable(name)

    def record(self, count):
        for index in range(count):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": index})

    def test_sequence_numbers_survive_a_crash_and_a_torn_line(self):
        """Journal sequence numbers survive a stale counter and a torn last line; fragile because it runs only after
        a crash, and a reused number makes cursors skip events silently."""
        self.subscribe()
        for _ in range(3):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"})
        (events.directory(self.root) / "seq").write_text("2\n")  # the counter missed the last write
        with events.segments(self.root)[-1][1].open("ab") as handle:
            handle.write(b'{"schema":1,"seq":4,"type":"agent.st')  # torn mid-write
        events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"after": "crash"})
        found, _ = events.read(self.root, 0)
        self.assertEqual([event["seq"] for event in found], [1, 2, 3, 4])
        self.assertEqual(found[-1]["data"], {"after": "crash"})

    def test_a_segment_pruned_while_it_is_read_becomes_a_gap(self):
        """Segments rolled by size or day and pruned by age, by plugins.json's size limit or between listing and
        reading read back as a gap with the right bounds, and a handler gets events.gap; fragile because rolling,
        pruning and reading race, and events otherwise vanish without an error."""
        self.subscribe()
        with mock.patch.object(events, "SEGMENT_BYTES", 250):
            self.record(12)
        self.assertGreater(len(events.segments(self.root)), 3)  # rolled by size
        self.assertEqual([event["seq"] for event in events.read(self.root, 0)[0]], list(range(1, 13)))
        with self.subTest("pruned between listing and reading"):
            real, calls = events.segments, []

            def racing(root):
                listed = real(root)
                if not calls:
                    calls.append(1)
                    listed[0][1].unlink()  # pruned after listing, before reading
                return listed
            with mock.patch.object(events, "segments", racing):
                found, gap = events.read(self.root, 0)
            self.assertIsNotNone(gap)
            self.assertEqual(found[0]["seq"], gap["to"] + 1)
            self.assertEqual(found[-1]["seq"], 12)
        with self.subTest("pruned by age, and a new day rolls"):
            listed = events.segments(self.root)
            current = listed[-1][1]
            old = time.time() - 9 * 86400
            for _, path in listed:
                os.utime(path, (old, old))
            self.assertEqual(set(events.prune(self.root)), {path for _, path in listed[:-1]})
            self.assertTrue(current.exists())  # the segment being written is never deleted
            self.record(1)
            self.assertNotEqual(events.segments(self.root)[-1][1], current)  # it was written days ago: a new one
            found, gap = events.read(self.root, 0)
            self.assertEqual(gap["from"], 1)
            self.assertEqual(found[0]["seq"], gap["to"] + 1)
        with self.subTest("pruned by the size plugins.json sets"):
            data = json.loads((self.root / "plugins.json").read_text())
            write(self.root / "plugins.json", {**data, "events": {"retain_days": 30, "max_mb": 0.0005}})
            self.assertEqual(events.retention(self.root), (30, 0.0005))
            with mock.patch.object(events, "SEGMENT_BYTES", 200):
                self.record(20)
            closed = events.segments(self.root)[:-1]
            self.assertLessEqual(sum(path.stat().st_size for _, path in closed), 0.0005 * 1024 * 1024)
        with self.subTest("a handler of one type is told what it missed"):
            self.write_plugin("narrow", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                              {"plugin.py": ONE_TYPE})
            self.enable("narrow")
            loaded = loader.load(self.entry("narrow"))
            host._write_cursor(self.root, "narrow", 0)
            found, gap = events.read(self.root, 0)
            host._deliver(loaded, api.Plugin(loaded), self.root, "narrow")
            self.assertEqual(loaded.module.SEEN, [("events.gap", gap["to"])]
                             + [("agent.status", event["seq"]) for event in found])
            self.assertEqual(host._read_cursor(self.root, "narrow"), found[-1]["seq"])

    def test_nothing_new_reads_no_segment(self):
        """A host poll with nothing new opens no journal segment; a break goes unnoticed because every idle host
        then rereads its journal each poll, costing CPU on every agent's machine with no visible symptom (PR #20
        review should-fix)."""
        self.subscribe(["*"])
        self.record(3)
        found, _ = events.read(self.root, 0)
        self.assertEqual(len(found), 3)
        real = Path.open
        opened = []

        def counting(path, *arguments, **options):
            if path.suffix == ".jsonl":
                opened.append(path)
            return real(path, *arguments, **options)
        with mock.patch.object(Path, "open", counting):
            self.assertEqual(events.read(self.root, 3), ([], None))
        self.assertEqual(opened, [])


class HostTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.out = self.work / "events.out"
        os.environ["EVENTS_OUT"] = str(self.out)
        os.environ["SERVICE_OUT"] = str(self.work / "service.out")
        os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        self.patch = mock.patch.object(host, "SUPERVISE_SECONDS", 0.2)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def run_host(self):
        stopping = threading.Event()
        thread = threading.Thread(target=host.supervise, args=(self.root,), kwargs={"stopping": stopping}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stopping.set(), thread.join(30)))
        return stopping, thread

    def until(self, condition, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        self.fail("timed out")

    def lines(self, path=None):
        path = path or self.out
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_handlers_get_events_once_per_cursor_and_the_host_stops_when_unneeded(self):
        """The real supervise loop delivers to one handler in two universes, runs a service, and exits, stopping
        the service cleanly, once no plugin there is on; a break goes unnoticed because the host runs in the
        background, leaving a stray process per universe or going quiet."""
        self.write_plugin("listener", {"module": "plugin", "extends": ["events"], "events": ["agent.*", "events.gap"]},
                          {"plugin.py": LISTENER})
        self.write_plugin("server", {"module": "plugin", "extends": ["service"]}, {"plugin.py": SERVICE})
        self.enable("listener")
        self.enable("server")
        child = self.subverse("child")
        self.enable("listener", root=child)
        events.emit(self.root, "agent.status", {"id": "old", "name": "old"})  # before the worker: not replayed
        stopping, thread = self.run_host()
        service = self.work / "service.out"
        self.until(lambda: (self.root / "state/plugins/listener/events.cursor").exists()
                   and (child / "state/plugins/listener/events.cursor").exists()
                   and service.exists() and service.read_text().startswith("started test"))
        events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": "waiting"})
        events.emit(child, "agent.exited", {"id": "b", "name": "c"}, data={"state": "failed"})
        self.until(lambda: len(self.lines()) == 2)
        seen = {(item["type"], item["universe"]) for item in self.lines()}
        self.assertEqual(seen, {("agent.status", str(self.root)), ("agent.exited", str(child))})
        self.assertTrue(host.running(self.root))
        status = json.loads((host.host_directory(self.root) / "status.json").read_text())
        self.assertEqual(sorted(status["workers"]), ["listener", "server"])
        self.enable("listener", on=False)
        self.enable("listener", on=False, root=child)
        self.enable("server", on=False)
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertFalse(host.running(self.root))
        self.assertTrue(service.read_text().endswith("stopped\n"))


LATE = "SEEN = []\ndef register(mv):\n    mv.events.on('agent.status', lambda e: SEEN.append(e.seq))\n"


class CursorAnchorTest(PluginTestCase):
    def test_a_plugins_cursor_starts_where_its_switch_says(self):
        """Events after enabling reach a host that starts later, and events from while a plugin was off are never
        replayed when it is turned on again; fragile because set_enabled sets the anchor where two opposite rules
        meet, and events are silently dropped or replayed."""
        self.write_plugin("early", {"extends": ["events"], "events": ["*"]})
        self.enable("early")  # someone else subscribes, so the journal is written while late is off
        self.write_plugin("late", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                          {"plugin.py": LATE})

        def status(count):
            for index in range(count):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": index})
        status(3)
        plugins.set_enabled(self.root, "late", True)  # mv2 plugins enable: the cursor starts here
        status(2)  # the host is down
        loaded = loader.load(self.entry("late"))
        mv = api.Plugin(loaded)
        host._deliver(loaded, mv, self.root, "late")  # the host starts
        self.assertEqual(loaded.module.SEEN, [4, 5])
        plugins.set_enabled(self.root, "late", False)
        status(3)  # early keeps the journal recording
        plugins.set_enabled(self.root, "late", True)  # turned on again: it starts after what happened while off
        self.assertEqual(host._read_cursor(self.root, "late"), 8)
        plugins.set_enabled(self.root, "late", True)  # on already: its cursor is left alone
        self.assertEqual(host._read_cursor(self.root, "late"), 8)
        status(1)
        host._deliver(loaded, mv, self.root, "late")
        self.assertEqual(loaded.module.SEEN, [4, 5, 9])
