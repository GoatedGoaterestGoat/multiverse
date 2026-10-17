"""A handler that runs out of time never stalls its siblings or its plugin for good (PR #20 re-check, item 1)."""
from pathlib import Path
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
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
            hold.wait(60)  # set when the test ends
        time.sleep(SLEEP["seconds"])

    @mv.events.on("agent.status")
    def fast(event):
        SEEN["fast"].append((event.universe.name, event.seq))
"""


class Fixture(PluginTestCase):
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


class StuckHandlerTest(Fixture):
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
        # Each call ran past its limit and kept running for 3 limits' time: given up after that one call each.
        self.assertEqual(self.seen("slow"), [1, 2, 3, 4])
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


class NeverReturnsTest(Fixture):
    """A handler whose call never returns (PR #20 review 3, item 3)."""

    def test_a_call_that_never_returns_counts_as_tries_then_its_events_move_on(self):
        import threading
        self.module.HOLD["test"] = threading.Event()  # slow never returns here (until the test ends)
        self.emit(self.root, 3)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1):
            self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 3, seconds=10),
                            (host._read_cursor(self.root, "pair"), self.module.SEEN))
        self.assertEqual(self.seen("fast"), [1, 2, 3])
        problems = plugins.problems(self.root)
        self.assertTrue(any("pair: gave up on event 1 (agent.status) in its handler slow" in problem
                            for problem in problems), problems)
        self.assertTrue(any("pair: gave up on event 1" in error
                            for item in plugins.listing(self.root) if item["name"] == "pair"
                            for error in item["errors"]))

    def test_no_handler_gets_an_event_twice_however_many_polls_while_a_sibling_is_stuck(self):
        # PR #20 review 4: once the sibling was more than one read ahead, every poll took that as a journal that
        # started again, wiped its progress and gave it everything from the stuck event on once more.
        import threading
        self.module.HOLD["test"] = threading.Event()
        self.emit(self.root, 1200)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1), mock.patch.object(host, "TIMEOUTS_PER_EVENT", 1000):
            self.assertTrue(self.deliver_until(lambda: len(self.seen("fast")) >= 1200, seconds=20),
                            len(self.seen("fast")))
            for _ in range(4):  # more polls while slow is still on event 1
                host._deliver(self.loaded, self.mv, self.root, "pair")
        fast = self.seen("fast")
        self.assertEqual(len(fast), len(set(fast)), f"{len(fast) - len(set(fast))} events delivered again")
        self.assertEqual(fast, list(range(1, 1201)))
        self.assertEqual(host._read_cursor(self.root, "pair"), 0)

    def test_siblings_keep_going_past_one_read_while_a_handler_is_stuck(self):
        import threading
        self.module.HOLD["test"] = threading.Event()
        self.emit(self.root, 505)  # one read takes at most 500 events after the cursor
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1), mock.patch.object(host, "TIMEOUTS_PER_EVENT", 1000):
            self.assertTrue(self.deliver_until(lambda: self.seen("fast") == list(range(1, 506)), seconds=10),
                            len(self.seen("fast")))
            self.assertEqual(host._read_cursor(self.root, "pair"), 0)  # slow has not finished event 1


class AbandonedCallsTest(Fixture):
    def test_only_calls_still_running_count_toward_a_worker_restart(self):
        import threading
        host._abandoned.clear()
        self.addCleanup(host._abandoned.clear)
        self.module.SLEEP["seconds"] = 0.4  # slow, but it returns
        self.emit(self.root, 2)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1):
            self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 2))
        time.sleep(0.6)  # both calls have returned by now
        host._abandoned[:] = [item for item in host._abandoned if loader.still_running(*item)]
        self.assertEqual(host._abandoned, [])
        self.module.SLEEP["seconds"] = 0.0
        self.module.HOLD["test"] = threading.Event()  # now one that never returns
        events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": "x"})
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1):
            self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 3))
        self.assertEqual(len([item for item in host._abandoned if loader.still_running(*item)]), 1)

    def test_a_worker_with_two_calls_that_never_return_restarts_itself(self):
        import threading
        host._abandoned.clear()
        self.addCleanup(host._abandoned.clear)
        self.module.HOLD["test"] = threading.Event()
        self.emit(self.root, 2)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1), \
                mock.patch.object(host.signal, "signal"):  # the worker's own signal handlers: not in a test
            start = time.monotonic()
            self.assertEqual(host.work(self.root, "pair"), 1)  # failed: the supervisor starts a fresh one
            self.assertLess(time.monotonic() - start, 10)
        log = (host.host_directory(self.root) / "host.log").read_text()
        self.assertIn("pair: 2 handler calls never returned; restarting its worker", log)
        self.assertEqual(host._read_cursor(self.root, "pair"), 2)  # both events were given up on first

    def test_mv2_plugins_says_a_give_up_once(self):
        from multiverse.tools import plugins as tool
        host._gave_up(self.root, "pair", 7, "agent.status", "slow", "it ran past its 60 s limit 3 times")
        text = tool.text("list", tool.run(self.root))
        self.assertEqual(text.count("pair: gave up on event 7 (agent.status) in its handler slow"), 1, text)
