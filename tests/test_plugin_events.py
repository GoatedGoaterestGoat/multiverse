"""The event journal and the plugin host: recording, rolling, pruning, reading, handlers, services and restarts."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, events, host, loader, manifest
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

CRASHER = """
def register(mv):
    def start(context):
        raise RuntimeError("service broke")
    mv.service(start)
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


class JournalTest(PluginTestCase):
    def subscribe(self, patterns=("agent.*",), name="listener"):
        self.write_plugin(name, {"extends": ["events"], "events": list(patterns)})
        self.enable(name)

    def test_nothing_is_written_without_a_subscriber(self):
        self.assertIsNone(events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": "idle"}))
        self.assertFalse(events.directory(self.root).exists())
        self.subscribe(["message.*"])
        self.assertIsNone(events.emit(self.root, "agent.status", {"id": "a", "name": "w"}))
        self.assertIsNotNone(events.emit(self.root, "message.saved", {"id": "a", "name": "w"}, data={"length": 3}))

    def test_events_get_sequence_numbers_and_read_in_order(self):
        self.subscribe()
        for status in ("working", "waiting", "idle"):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w", "extra": "dropped"}, "L1", {"to": status})
        found, gap = events.read(self.root, 0)
        self.assertIsNone(gap)
        self.assertEqual([(event["seq"], event["data"]["to"]) for event in found], [(1, "working"), (2, "waiting"), (3, "idle")])
        self.assertEqual(found[0]["agent"], {"id": "a", "name": "w"})
        self.assertEqual(found[0]["launch"], "L1")
        self.assertEqual([event["seq"] for event in events.read(self.root, 2)[0]], [3])

    def test_segments_roll_by_size_and_day_and_prune_by_age_and_size(self):
        self.subscribe()
        with mock.patch.object(events, "SEGMENT_BYTES", 300):
            for index in range(12):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": index})
        found = events.segments(self.root)
        self.assertGreater(len(found), 3)
        self.assertEqual([event["seq"] for event in events.read(self.root, 0)[0]], list(range(1, 13)))
        current = found[-1][1]
        old = time.time() - 9 * 86400
        for _, path in found:
            os.utime(path, (old, old))
        removed = events.prune(self.root)
        self.assertEqual(set(removed), {path for _, path in found[:-1]})
        self.assertTrue(current.exists())  # the segment being written is never deleted
        events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": 12})
        self.assertNotEqual(events.segments(self.root)[-1][1], current)  # it was written yesterday: a new one began
        found, gap = events.read(self.root, 0)
        self.assertEqual(gap["from"], 1)
        self.assertEqual(found[0]["seq"], gap["to"] + 1)

    def test_size_limit_from_plugins_json(self):
        self.subscribe()
        data = json.loads((self.root / "plugins.json").read_text())
        write(self.root / "plugins.json", {**data, "events": {"retain_days": 30, "max_mb": 0.0005}})
        with mock.patch.object(events, "SEGMENT_BYTES", 200):
            for index in range(20):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": index})
        closed = events.segments(self.root)[:-1]
        self.assertLessEqual(sum(path.stat().st_size for _, path in closed), 0.0005 * 1024 * 1024)
        self.assertEqual(events.retention(self.root), (30, 0.0005))

    def test_emit_never_raises(self):
        self.subscribe()
        events.directory(self.root).parent.mkdir(parents=True, exist_ok=True)
        events.directory(self.root).write_text("not a directory")
        self.assertIsNone(events.emit(self.root, "agent.status", {"id": "a"}))
        self.assertIn("agent.status", (self.root / "state" / "plugins" / "events.log").read_text())

    def test_a_busy_journal_drops_the_event_quickly_and_never_blocks(self):
        import subprocess
        self.subscribe()
        events.directory(self.root).mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c",
            "import fcntl, sys, time; h = open(sys.argv[1], 'a'); fcntl.flock(h, fcntl.LOCK_EX); "
            "print('held', flush=True); time.sleep(30)", str(events.directory(self.root) / ".lock")],
            stdout=subprocess.PIPE, text=True)

        def stop():
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.addCleanup(stop)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()
        self.assertIsNone(events.emit(self.root, "agent.status", {"id": "a", "name": "w"}))
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("dropped: the journal was busy", (self.root / "state/plugins/events.log").read_text())
        stop()
        self.assertIsNotNone(events.emit(self.root, "agent.status", {"id": "a", "name": "w"}))

    def test_sequence_numbers_survive_a_crash_and_a_torn_line(self):
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

    def test_one_event_is_bounded(self):
        self.subscribe(["*"])
        events.emit(self.root, "listener.big", data={"text": "x" * 100000})
        [event] = events.read(self.root, 0)[0]
        self.assertEqual(event["data"]["truncated"], True)
        self.assertGreater(event["data"]["bytes"], events.EVENT_BYTES)

    def test_a_segment_pruned_while_it_is_read_becomes_a_gap(self):
        self.subscribe()
        with mock.patch.object(events, "SEGMENT_BYTES", 250):
            for index in range(8):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": index})
        first = events.segments(self.root)[0][1]
        real = events.segments
        calls = []

        def racing(root):
            found = real(root)
            if not calls:
                calls.append(1)
                first.unlink()  # pruned after listing, before reading
            return found
        with mock.patch.object(events, "segments", racing):
            found, gap = events.read(self.root, 0)
        self.assertIsNotNone(gap)
        self.assertEqual(found[0]["seq"], gap["to"] + 1)
        self.assertEqual(found[-1]["seq"], 8)

    def test_manifest_and_api_subscriptions(self):
        with self.assertRaisesRegex(manifest.ManifestError, 'needs "events" in extends'):
            manifest.load(self.write_plugin("bad", {"events": ["agent.status"]}))
        with self.assertRaisesRegex(manifest.ManifestError, "events must list"):
            manifest.load(self.write_plugin("bad", {"extends": ["events"], "events": ["Agent Status"]}))
        self.write_plugin("probe", {"module": "plugin", "extends": ["events"], "events": ["agent.*", "probe.*"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.enable("probe")
        mv = api.Plugin(loader.load(self.entry("probe")))
        mv.events.on("agent.status", lambda event: None)
        with self.assertRaisesRegex(api.Error, "subscribe to them"):
            mv.events.on("message.saved", lambda event: None)
        mv.events.emit("thing", {"x": 1}, universe=self.root)
        [event] = events.read(self.root, 0)[0]
        self.assertEqual((event["type"], event["data"]), ("probe.thing", {"x": 1}))


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
        self.write_plugin("listener", {"module": "plugin", "extends": ["events"], "events": ["agent.*", "events.gap"]},
                          {"plugin.py": LISTENER})
        self.enable("listener")
        child = self.subverse("child")
        self.enable("listener", root=child)
        events.emit(self.root, "agent.status", {"id": "old", "name": "old"})  # before the worker: not replayed
        stopping, thread = self.run_host()
        self.until(lambda: (self.root / "state/plugins/listener/events.cursor").exists()
                   and (child / "state/plugins/listener/events.cursor").exists())
        events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": "waiting"})
        events.emit(child, "agent.exited", {"id": "b", "name": "c"}, data={"state": "failed"})
        self.until(lambda: len(self.lines()) == 2)
        seen = {(item["type"], item["universe"]) for item in self.lines()}
        self.assertEqual(seen, {("agent.status", str(self.root)), ("agent.exited", str(child))})
        self.assertTrue(host.running(self.root))
        status = json.loads((host.host_directory(self.root) / "status.json").read_text())
        self.assertEqual(list(status["workers"]), ["listener"])
        self.enable("listener", on=False)
        self.enable("listener", on=False, root=child)
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertFalse(host.running(self.root))

    def test_a_crashing_worker_is_restarted_three_times_then_left_stopped(self):
        self.write_plugin("crasher", {"module": "plugin", "extends": ["service"]}, {"plugin.py": CRASHER})
        self.enable("crasher")
        self.run_host()
        status = host.host_directory(self.root) / "status.json"
        self.until(lambda: "crasher" in json.loads(status.read_text()).get("stopped", {}) if status.exists() else False,
                   timeout=60)
        self.assertIn("left stopped", json.loads(status.read_text())["stopped"]["crasher"])
        self.assertIn("service: RuntimeError: service broke",
                      (self.root / "state/plugins/crasher/errors.log").read_text())

    def test_services_start_with_their_universes_and_stop_cleanly(self):
        self.write_plugin("server", {"module": "plugin", "extends": ["service"]}, {"plugin.py": SERVICE})
        self.enable("server")
        stopping, thread = self.run_host()
        out = self.work / "service.out"
        self.until(lambda: out.exists() and out.read_text().startswith("started test"))
        stopping.set()
        thread.join(30)
        self.until(lambda: out.read_text().endswith("stopped\n"))
        self.assertFalse(host.running(self.root))


    def test_services_start_again_when_the_plugin_is_turned_on_in_another_universe(self):
        self.write_plugin("server", {"module": "plugin", "extends": ["service"]}, {"plugin.py": SERVICE})
        self.enable("server")
        child = self.subverse("child")
        stopping, thread = self.run_host()
        out = self.work / "service.out"
        self.until(lambda: out.exists() and out.read_text().startswith("started test"))
        self.enable("server", root=child)
        self.until(lambda: "started child,test" in out.read_text(), timeout=30)
        self.assertEqual(out.read_text().splitlines()[:3], ["started test", "stopped", "started child,test"])
        stopping.set()
        thread.join(30)


class HostCommandTest(PluginTestCase):
    def test_status_and_events_from_the_command(self):
        from multiverse.tools import plugins as tool
        self.write_plugin("listener", {"extends": ["events"], "events": ["agent.*"]})
        child = self.subverse("child")
        self.enable("listener", root=child)
        status = tool.run(child, plugin_action="host")
        self.assertEqual((status["universe"], status["running"], status["needed"]), (str(self.root), False, ["listener"]))
        self.assertIn("is not running", tool.text("host", status))
        self.assertIn("No event journal", tool.text("events", tool.run(child, plugin_action="events")))
        events.emit(child, "agent.status", {"id": "a", "name": "w"})
        summary = tool.run(child, plugin_action="events", prune=True)
        self.assertEqual((summary["segments"], summary["newest_seq"], summary["pruned"]), (1, 1, []))
        enabled = tool.run(child, plugin_action="enable", name="listener")
        self.assertEqual((enabled["needs_host"], enabled["host_running"]), (True, False))
        self.assertIn("mv2 plugins host start", tool.text("enable", enabled))
        with self.assertRaisesRegex(ValueError, "start, stop, status|status, start, stop"):
            tool.run(child, plugin_action="host", name="restart")


TWO_HANDLERS = """
import threading
RELEASE = threading.Event()
SEEN = {"slow": [], "fast": []}

def register(mv):
    @mv.events.on("agent.status")
    def slow(event):
        if event.seq == 1:
            RELEASE.wait(10)  # stuck on the first event until released
        SEEN["slow"].append(event.seq)

    @mv.events.on("agent.status")
    def fast(event):
        SEEN["fast"].append(event.seq)
"""


class DeliveryTest(PluginTestCase):
    """At-least-once delivery per handler (PR #20 review, item 7)."""

    def setUp(self):
        super().setUp()
        self.write_plugin("pair", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                          {"plugin.py": TWO_HANDLERS})
        self.enable("pair")
        self.loaded = loader.load(self.entry("pair"))
        self.module = self.loaded.module
        self.addCleanup(self.module.RELEASE.set)
        self.mv = api.Plugin(self.loaded)
        host._write_cursor(self.root, "pair", 0)
        for index in range(3):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": str(index)})

    def cursor(self):
        return host._read_cursor(self.root, "pair")

    def test_a_stuck_handler_holds_the_cursor_and_drops_no_other_handlers_events(self):
        with mock.patch.object(host, "HANDLER_SECONDS", 0.2):
            host._deliver(self.loaded, self.mv, self.root, "pair")  # slow is stuck on event 1
            self.assertEqual(self.cursor(), 0)  # never past an event a handler did not finish
            self.assertIn(1, self.module.SEEN["fast"])  # the other handler still had it
            host._deliver(self.loaded, self.mv, self.root, "pair")  # slow still running: busy, nothing lost
            self.assertEqual(self.cursor(), 0)
            self.module.RELEASE.set()
            for _ in range(50):
                host._deliver(self.loaded, self.mv, self.root, "pair")
                if self.cursor() == 3:
                    break
                time.sleep(0.05)
        self.assertEqual(self.cursor(), 3)
        self.assertEqual(sorted(set(self.module.SEEN["slow"])), [1, 2, 3])
        self.assertEqual(sorted(set(self.module.SEEN["fast"])), [1, 2, 3])
        self.assertEqual([error for error in self.loaded.errors if "took longer" in error],
                         ["event agent.status (slow): TimeoutError: took longer than 0.2 s"])

    def test_a_failing_handler_is_recorded_and_passed(self):
        def broken(event):
            raise ValueError("bad event")
        self.loaded.registrations.handlers[0] = ("agent.status", broken)
        host._deliver(self.loaded, self.mv, self.root, "pair")
        self.assertEqual(self.cursor(), 3)  # an error is recorded, not retried forever
        self.assertEqual(self.module.SEEN["fast"], [1, 2, 3])
        self.assertIn("event agent.status (broken): ValueError: bad event", self.loaded.errors)

    def test_a_removed_subverse_is_never_recreated(self):
        import shutil
        child = self.subverse("child")
        shutil.rmtree(child)
        host._write_cursor(child, "pair", 5)
        host._deliver(self.loaded, self.mv, child, "pair")
        self.assertFalse(child.exists())


class ShortReasonTest(PluginTestCase):
    """A core event's reason is one short line, never a terminal tail with a prompt in it (PR #20 review, 7)."""

    def test_reasons_are_cut_to_their_first_line(self):
        self.write_plugin("listener", {"extends": ["events"], "events": ["*"]})
        self.enable("listener")
        tail = "Launch failed: exit 1\nLast terminal output:\n> SECRET PROMPT TEXT\n" + "x" * 8000
        events.emit(self.root, "agent.exited", {"id": "a", "name": "w"}, data={"state": "failed", "reason": tail})
        events.emit(self.root, "message.failed", {"id": "a", "name": "w"}, data={"id": "m", "reason": "y" * 500})
        events.emit(self.root, "listener.note", None, data={"reason": "keep\nall of it"})  # a plugin's own event
        exited, failed, own = events.read(self.root, 0)[0]
        self.assertEqual(exited["data"]["reason"], "Launch failed: exit 1")
        self.assertEqual(len(failed["data"]["reason"]), 200)
        self.assertTrue(failed["data"]["reason"].endswith("…"))
        self.assertEqual(own["data"]["reason"], "keep\nall of it")
        self.assertNotIn("SECRET", json.dumps([exited, failed]))
