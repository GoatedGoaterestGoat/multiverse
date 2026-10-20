"""A handler that runs out of time never stalls its siblings or its plugin for good (PR #20 re-check, item 1), and
the cursor never skips a journal's events."""
import json
from pathlib import Path
import shutil
import sys
import threading
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
        """`count` agent.status events in `scope`'s journal, pair's cursor before them: the first one emitted, which
        makes the journal, the rest written after it at once, as emitting each would write them."""
        host._write_cursor(scope, "pair", 0)
        first = events.emit(scope, "agent.status", {"id": "a", "name": "w"}, data={"to": "0"})
        with events.segments(scope)[-1][1].open("a") as segment:
            segment.writelines(json.dumps({**first, "seq": first["seq"] + index, "data": {"to": str(index)}},
                                          separators=(",", ":")) + "\n" for index in range(1, count))
        (events.directory(scope) / "seq").write_text(f"{first['seq'] + count}\n")

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
    def test_no_handler_gets_an_event_twice_however_many_polls_while_a_sibling_is_stuck(self):
        """While one handler is stuck its sibling gets every event exactly once past one read, one poll, more polls
        and a worker restart, and the shared cursor waits on the stuck event until it ends; fragile because of
        per-handler bookkeeping and read windows, and a sibling 1,200 events ahead once got them all again (PR #20
        review 4)."""
        with self.subTest("stuck, then released"):
            small = self.subverse("small")
            self.enable("pair", root=small)
            self.module.HOLD["small"] = threading.Event()  # slow is stuck on event 1 there until it is set
            self.emit(small, 3)
            with mock.patch.object(host, "HANDLER_SECONDS", 0.2):
                host._deliver(self.loaded, self.mv, small, "pair")
                self.assertEqual(host._read_cursor(small, "pair"), 0)  # never past an event a handler did not finish
                self.assertIn(1, self.seen("fast", "small"))  # the other handler still had it
                host._deliver(self.loaded, self.mv, small, "pair")  # slow still running: busy, nothing lost
                self.assertEqual(host._read_cursor(small, "pair"), 0)
                self.module.HOLD["small"].set()
                self.assertTrue(self.deliver_until(lambda: host._read_cursor(small, "pair") == 3, scopes=[small]))
            self.assertEqual(sorted(set(self.seen("slow", "small"))), [1, 2, 3])  # at least once
            self.assertEqual(self.seen("fast", "small"), [1, 2, 3])
            self.assertEqual([error for error in self.loaded.errors if "took longer" in error],
                             ["event agent.status (slow): TimeoutError: took longer than 0.2 s"])
        self.module.HOLD["test"] = threading.Event()  # slow never returns on event 1 here
        self.emit(self.root, 2600)  # more than the 4 reads of 500 one poll makes from the cursor
        everything = list(range(1, 2601))
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1), mock.patch.object(host, "TIMEOUTS_PER_EVENT", 1000):
            with self.subTest("past one read and one poll"):
                self.assertTrue(self.deliver_until(lambda: len(self.seen("fast")) >= 2600, seconds=30),
                                len(self.seen("fast")))
                self.assertEqual(self.seen("fast"), everything)
            with self.subTest("more polls while it is still stuck"):
                for _ in range(4):
                    host._deliver(self.loaded, self.mv, self.root, "pair")
                fast = self.seen("fast")
                self.assertEqual(len(fast), len(set(fast)), f"{len(fast) - len(set(fast))} events delivered again")
            with self.subTest("a worker that starts again"):
                for table in (host._finished, host._timeouts, host._journals):
                    table.clear()  # what a fresh worker process starts with
                for _ in range(3):
                    host._deliver(self.loaded, self.mv, self.root, "pair")
                self.assertEqual(self.seen("fast"), everything)  # nothing again after the restart
        self.assertEqual(host._read_cursor(self.root, "pair"), 0)  # slow is still on event 1

    def test_a_call_that_never_returns_counts_as_tries_then_its_events_move_on(self):
        """A handler that times out, never returns or raises is given up on after TIMEOUTS_PER_EVENT tries (or
        recorded and passed), the give-up is recorded, and its events and other universes move on; a break goes
        unnoticed because a stalled plugin raises nothing, its events just stop (PR #20 re-check item 1, review 3
        item 3)."""
        slow, stuck, other, raising = (self.subverse(name) for name in ("slow", "stuck", "other", "raising"))
        for scope in (slow, stuck, other, raising):
            self.enable("pair", root=scope)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1):
            with self.subTest("every call out of time, though each returns"):
                self.module.SLEEP["seconds"] = 0.4
                self.emit(slow, 4)
                # The sibling has every event while the slow handler is still on the first.
                self.assertTrue(self.deliver_until(lambda: self.seen("fast", "slow") == [1, 2, 3, 4], seconds=3,
                                                   scopes=[slow]), self.module.SEEN)
                self.assertEqual(host._read_cursor(slow, "pair"), 0)  # never past an unfinished event
                self.assertTrue(self.deliver_until(lambda: host._read_cursor(slow, "pair") == 4, scopes=[slow]),
                                self.module.SEEN)
                self.module.SLEEP["seconds"] = 0.0
                self.assertEqual(self.seen("fast", "slow"), [1, 2, 3, 4])  # never replayed to the handler that finished
                # Each call ran past its limit and kept running for 3 limits' time: given up after that one call each.
                self.assertEqual(self.seen("slow", "slow"), [1, 2, 3, 4])
                given_up = [error for error in self.loaded.errors if "gave up" in error]
                self.assertEqual(len(given_up), 4, self.loaded.errors)
                self.assertIn(f"gave up on seq 1 after {host.TIMEOUTS_PER_EVENT} timeouts", given_up[0])
            with self.subTest("a call that never returns, beside another universe"):
                self.module.HOLD["stuck"] = threading.Event()  # slow never returns there (until the test ends)
                self.emit(stuck, 3)
                self.emit(other, 2)
                for scope in (stuck, other):
                    host._deliver(self.loaded, self.mv, scope, "pair")
                self.assertEqual(self.seen("slow", "other"), [1, 2])  # the stuck universe holds up only itself
                self.assertEqual(host._read_cursor(other, "pair"), 2)
                self.assertEqual(host._read_cursor(stuck, "pair"), 0)
                self.assertTrue(self.deliver_until(lambda: host._read_cursor(stuck, "pair") == 3, seconds=10,
                                                   scopes=[stuck]), self.module.SEEN)
                self.assertEqual(self.seen("fast", "stuck"), [1, 2, 3])
                problems = plugins.problems(stuck)
                self.assertTrue(any("pair: gave up on event 1 (agent.status) in its handler slow" in problem
                                    for problem in problems), problems)
                self.assertTrue(any("pair: gave up on event 1" in error
                                    for item in plugins.listing(stuck) if item["name"] == "pair"
                                    for error in item["errors"]))
        with self.subTest("a handler that raises"):
            def broken(event):
                raise ValueError("bad event")
            self.loaded.registrations.handlers[0] = ("agent.status", broken)
            self.emit(raising, 3)
            host._deliver(self.loaded, self.mv, raising, "pair")
            self.assertEqual(host._read_cursor(raising, "pair"), 3)  # an error is recorded, not retried forever
            self.assertEqual(self.seen("fast", "raising"), [1, 2, 3])
            self.assertIn("event agent.status (broken): ValueError: bad event", self.loaded.errors)


class AbandonedCallsTest(Fixture):
    def test_a_worker_with_two_calls_that_never_return_restarts_itself(self):
        """Abandoned handler calls that are still running, and only those, make the worker exit with HUNG_EXIT; a
        break leaks threads forever or restarts for calls that already returned, unseen."""
        host._abandoned.clear()
        self.addCleanup(host._abandoned.clear)
        with mock.patch.object(host, "HANDLER_SECONDS", 0.1):
            self.module.SLEEP["seconds"] = 0.4  # slow, but it returns
            self.emit(self.root, 2)
            self.assertTrue(self.deliver_until(lambda: host._read_cursor(self.root, "pair") == 2))
            self.assertEqual(len(host._abandoned), 2)  # both given up on while still running
            time.sleep(0.6)  # both calls have returned by now
            self.assertEqual([item for item in host._abandoned if loader.still_running(*item)], [])
            self.module.SLEEP["seconds"] = 0.0
            self.module.HOLD["test"] = threading.Event()  # now calls that never return
            for index in range(2):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"to": str(index)})
            with mock.patch.object(host, "WORKER_POLL_SECONDS", 0.05), \
                    mock.patch.object(host.signal, "signal"):  # the worker's own signal handlers: not in a test
                start = time.monotonic()
                self.assertEqual(host.work(self.root, "pair"), host.HUNG_EXIT)  # the supervisor starts a fresh one
                self.assertLess(time.monotonic() - start, 10)
        log = (host.host_directory(self.root) / "host.log").read_text()
        self.assertIn("pair: 2 handler calls never returned; restarting its worker", log)
        # Both new events were given up on first: the two calls that had returned never counted.
        self.assertEqual(host._read_cursor(self.root, "pair"), 4)


class CursorWriteOrderTest(Fixture):
    """PR #23 independent review (a): the cursor number is written before the journal it belongs to, so an
    interrupted write can only mean delivering again (at least once), never skipping."""

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
        """The cursor number is written before its journal id, so a crash between the writes means at-least-once
        delivery, never a skip; fragile because swapping two atomic writes looks harmless in a refactor, and it
        shows only after a crash (PR #23 independent review (a))."""
        for target in ("events.cursor", "events.cursor.journal"):
            with self.subTest(crash_at=target):
                journal = self.restarted_journal()
                self.interrupted(target, journal)
                after = host._read_cursor(self.root, "pair", journal)
                self.assertLessEqual(after, 3, f"a host would skip the new journal's events up to {after}")
