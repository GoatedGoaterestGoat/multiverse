"""A handler that runs out of time never stalls its siblings or its plugin for good (PR #20 re-check, item 1)."""
from pathlib import Path
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, events, host, loader
from multiverse.plugins.testing import PluginTestCase

SLOW_AND_FAST = """
import threading
import time

HOLD = {}  # universe path -> threading.Event: slow waits on it there; elsewhere it sleeps SLEEP
SLEEP = {"seconds": 0.0}
SEEN = {"slow": [], "fast": []}

def register(mv):
    @mv.events.on("agent.status")
    def slow(event):
        SEEN["slow"].append((event.universe.name, event.seq))
        hold = HOLD.get(event.universe.name)
        if hold is not None:
            hold.wait(10)
        time.sleep(SLEEP["seconds"])

    @mv.events.on("agent.status")
    def fast(event):
        SEEN["fast"].append((event.universe.name, event.seq))
"""


class StuckHandlerTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("pair", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                          {"plugin.py": SLOW_AND_FAST})
        self.enable("pair")
        self.loaded = loader.load(self.entry("pair"))
        self.module = self.loaded.module
        self.mv = api.Plugin(self.loaded)
        self.addCleanup(lambda: [hold.set() for hold in self.module.HOLD.values()])

    def emit(self, scope, count):
        host._write_cursor(scope, "pair", 0)
        for index in range(count):
            events.emit(scope, "agent.status", {"id": "a", "name": "w"}, data={"to": str(index)})

    def deliver_until(self, done, seconds=20, scopes=None):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for scope in scopes or [self.root]:
                host._deliver(self.loaded, self.mv, scope, "pair")
            if done():
                return True
            time.sleep(0.05)
        return False

    def seen(self, handler, name="test"):
        return [seq for universe, seq in self.module.SEEN[handler] if universe == name]

    def test_a_handler_always_out_of_time_is_given_up_on_and_its_sibling_gets_each_event_once(self):
        self.module.SLEEP["seconds"] = 0.4
        self.emit(self.root, 4)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1):
            # The sibling has every event while the slow handler is still on the first.
            self.assertTrue(self.deliver_until(lambda: self.seen("fast") == [1, 2, 3, 4], seconds=3),
                            self.module.SEEN)
            self.assertEqual(host._read_cursor(self.root, "pair"), 0)  # never past an unfinished event
            self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 4), self.module.SEEN)
        self.assertEqual(self.seen("fast"), [1, 2, 3, 4])  # never replayed to the handler that finished
        self.assertEqual(self.seen("slow"), [seq for seq in (1, 2, 3, 4) for _ in range(host.TIMEOUTS_PER_EVENT)])
        given_up = [error for error in self.loaded.errors if "gave up" in error]
        self.assertEqual(len(given_up), 4, self.loaded.errors)
        self.assertIn(f"gave up on seq 1 after {host.TIMEOUTS_PER_EVENT} timeouts", given_up[0])

    def test_a_handler_stuck_in_one_universe_still_gets_another_universes_events(self):
        child = self.subverse("child")
        self.enable("pair", root=child)
        self.module.HOLD["test"] = __import__("threading").Event()
        self.emit(self.root, 1)
        self.emit(child, 2)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1):
            self.assertTrue(self.deliver_until(lambda: self.seen("slow", "child") == [1, 2], seconds=3,
                                               scopes=[self.root, child]), self.module.SEEN)
            self.assertEqual(host._read_cursor(child, "pair"), 2)
            self.assertEqual(host._read_cursor(self.root, "pair"), 0)
            self.module.HOLD["test"].set()
            self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 1,
                                               scopes=[self.root, child]))
        self.assertEqual(self.seen("fast"), [1])

    def test_a_journal_that_started_again_is_delivered_again(self):
        import shutil
        self.emit(self.root, 2)
        self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 2))
        shutil.rmtree(events.directory(self.root))  # someone removed the universe's journal
        self.emit(self.root, 1)
        self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 1))
        self.assertEqual(self.seen("fast"), [1, 2, 1])
