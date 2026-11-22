"""A handler that runs out of time never stalls its siblings or its plugin for good (PR #20 re-check, item 1), and
the cursor never skips a journal's events."""
import json
import re
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
LATE = {"seq": None, "seconds": 0.0}  # fast finishes this event, but only after this long
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
        if event.seq == LATE["seq"]:
            time.sleep(LATE["seconds"])
"""


KEEPER = """
import builtins
import json
from pathlib import Path

def register(mv):
    for kind in json.loads((Path(__file__).parent / "plugin.json").read_text())["events"]:
        mv.events.on(kind)(lambda event, kind=kind: builtins.KEPT.append((kind, event.seq)))
"""


class ReloadTest(PluginTestCase):
    def test_a_running_worker_leaves_a_changed_plugins_events_to_its_new_version(self):
        """A worker whose plugin changed stops before skipping what the new version follows; else those are lost."""
        import builtins
        from multiverse.plugins import trust
        from multiverse.tools import plugins as tool
        builtins.KEPT = []
        self.addCleanup(lambda: delattr(builtins, "KEPT"))

        def worker(name):  # what a worker does as it starts: observe, let the loader choose, load
            observed = host._observe(self.root, name)
            entry = self.entry(name)
            loaded = loader.load(entry)
            return loaded, host._still_running(entry, loaded, self.root, observed)
        # (source, where, whether it runs at approved content)
        for number, (source, where, approved) in enumerate((("universe", self.root / "plugins", True),
                                                             ("personal", trust.user_directory() / "plugins", False))):
            name = f"keeper{number}"
            with self.subTest(source):
                def release(kinds):
                    self.write_plugin(name, {"module": "plugin", "extends": ["events"], "events": kinds},
                                      {"plugin.py": KEEPER}, where=where)
                release(["agent.status"])
                if approved:
                    tool.run(self.root, plugin_action="trust", name=name)
                self.enable(name)
                loaded, check = worker(name)
                host._write_cursor(self.root, name, 0)
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={})
                self.assertIsNone(host._deliver(loaded, api.Plugin(loaded), self.root, name, current=check))
                release(["agent.status", "message.saved"])  # plugin.json alone changes
                if approved:
                    self.assertEqual(check(), "unchanged")  # an edit nobody approved changes nothing that runs
                    tool.run(self.root, plugin_action="trust", name=name)
                saved = events.emit(self.root, "message.saved", data={})
                self.assertIsNotNone(saved)
                if approved:  # while the approval can't be read: nothing consumed, nothing restarted
                    with mock.patch.object(trust, "pins_now", return_value=None):
                        self.assertEqual(host._deliver(loaded, api.Plugin(loaded), self.root, name, current=check),
                                         "unknown")
                self.assertEqual(host._deliver(loaded, api.Plugin(loaded), self.root, name, current=check),
                                 "changed")
                self.assertLess(host._read_cursor(self.root, name, events.journal_id(self.root)), saved["seq"])
                loader.reset()  # what a fresh worker process does: its new version, loaded anew
                fresh, fresh_check = worker(name)
                host._deliver(fresh, api.Plugin(fresh), self.root, name, current=fresh_check)
                self.assertIn(("message.saved", saved["seq"]), builtins.KEPT)
                self.enable(name, False)
        with self.subTest("approved while it loaded: bound to what loaded"):
            self.write_plugin("loading", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                              {"plugin.py": KEEPER}, where=self.root / "plugins")
            tool.run(self.root, plugin_action="trust", name="loading")
            self.enable("loading")
            observed = host._observe(self.root, "loading")
            entry = self.entry("loading")
            loaded = loader.load(entry)
            self.write_plugin("loading", {"module": "plugin", "extends": ["events"], "events": ["message.saved"]},
                              {"plugin.py": KEEPER}, where=self.root / "plugins")
            tool.run(self.root, plugin_action="trust", name="loading")  # before its worker took its baseline
            self.assertEqual(host._still_running(entry, loaded, self.root, observed)(), "changed")
            self.enable("loading", False)
        with self.subTest("a live plugin edited between its selection and its load"):
            fixed = "def register(mv):\n    mv.events.on('agent.status')(lambda event: None)\n"
            self.write_plugin("between", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                              {"plugin.py": fixed}, where=trust.user_directory() / "plugins")
            self.enable("between")
            observed = host._observe(self.root, "between")
            entry = self.entry("between")  # selected with its manifest as it was
            self.write_plugin("between", {"module": "plugin", "extends": ["events"],
                                          "events": ["agent.status", "message.saved"]},
                              {"plugin.py": fixed}, where=trust.user_directory() / "plugins")
            loaded = loader.load(entry)  # what loaded follows agent.status alone; its plugin.json says more now
            self.assertEqual(host._still_running(entry, loaded, self.root, observed)(), "changed")
            self.enable("between", False)
        with self.subTest("a copy the loader passes over, unchanged, restarts nothing"):
            for where, kinds in ((self.root / "plugins", ["agent.status"]),
                                 (trust.user_directory() / "plugins", ["agent.status"])):
                self.write_plugin("fallback", {"module": "plugin", "extends": ["events"], "events": kinds},
                                  {"plugin.py": KEEPER}, where=where)
            tool.run(self.root, plugin_action="trust", name="fallback")
            self.write_plugin("fallback", {"module": "plugin", "extends": ["events"], "events": ["message.saved"]},
                              {"plugin.py": KEEPER}, where=self.root / "plugins")  # an edit nobody approved
            self.enable("fallback")
            loaded, check = worker("fallback")
            self.assertEqual(check(), "unchanged")  # chosen as it was, and would be again: no restart loop
            tool.run(self.root, plugin_action="trust", name="fallback")  # approved now: chosen in its place
            self.assertEqual(check(), "changed")
            self.enable("fallback", False)
        with self.subTest("a passed-over copy restored to what was approved, and one turned unreadable"):
            for where in (self.root / "plugins", trust.user_directory() / "plugins"):
                self.write_plugin("restored", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                                  {"plugin.py": KEEPER}, where=where)
            tool.run(self.root, plugin_action="trust", name="restored")
            universe = self.root / "plugins" / "restored"
            approved_bytes = (universe / "plugin.py").read_bytes()
            (universe / "plugin.py").write_text(KEEPER + "# edited\n")  # nobody approved this
            self.enable("restored")
            loaded, check = worker("restored")
            self.assertEqual(check(), "unchanged")
            (universe / "plugin.py").write_bytes(approved_bytes)  # undone: eligible again, its pin as it was
            with mock.patch.object(trust, "signature", side_effect=AssertionError("walked before a page")):
                self.assertEqual(check(), "unchanged")  # before a page: no tree walked (the user's choice)
            self.assertEqual(check(full=True), "changed")  # the 5-second refresh notices it
            loaded, check = worker("restored")
            (universe / "plugin.json").chmod(0)  # ahead of it, and now unreadable: it could be chosen
            try:
                self.assertIn(check(), ("changed", "unknown"))
                self.assertNotEqual(check(), "unchanged")
            finally:
                (universe / "plugin.json").chmod(0o644)
            self.enable("restored", False)
        with self.subTest("its plugin.json unreadable for a moment is unknown, never gone"):
            self.write_plugin("blink", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                              {"plugin.py": KEEPER}, where=trust.user_directory() / "plugins")
            self.enable("blink")
            loaded, check = worker("blink")
            manifest_file = trust.user_directory() / "plugins" / "blink" / "plugin.json"
            manifest_file.chmod(0)
            try:
                self.assertEqual(check(), "unknown")
            finally:
                manifest_file.chmod(0o644)
            self.assertEqual(check(), "unchanged")
            self.enable("blink", False)
        with self.subTest("a damaged pin is unknown, a removed one a change, and nothing is discovered in full"):
            self.write_plugin("pinned", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                              {"plugin.py": KEEPER}, where=self.root / "plugins")
            tool.run(self.root, plugin_action="trust", name="pinned")
            self.enable("pinned")
            loaded, check = worker("pinned")
            pins = trust.user_directory() / trust.FILE
            saved = pins.read_bytes()
            data = json.loads(saved)
            folder = str((self.root / "plugins" / "pinned").resolve())
            with mock.patch.object(loader, "entry", side_effect=AssertionError("full discovery")), \
                    mock.patch.object(loader, "entries", side_effect=AssertionError("full discovery")):
                self.assertEqual(check(), "unchanged")
                pins.write_text(json.dumps({**data, "plugins": {**data["plugins"], folder: None}}))
                self.assertEqual(check(), "unknown")
                pins.write_text(json.dumps({**data, "plugins": {k: v for k, v in data["plugins"].items()
                                                                if k != folder}}))
                self.assertEqual(check(), "changed")
            pins.write_bytes(saved)
            self.enable("pinned", False)
        with self.subTest("another copy selected in its place"):
            self.write_plugin("forkme", {"module": "plugin", "extends": ["events"], "events": ["agent.status"]},
                              {"plugin.py": KEEPER}, where=trust.user_directory() / "plugins")
            self.enable("forkme")
            loaded, check = worker("forkme")
            self.write_plugin("forkme", {"module": "plugin", "extends": ["events"], "events": ["message.saved"]},
                              {"plugin.py": KEEPER}, where=self.root / "plugins")
            self.assertEqual(check(), "changed")  # a copy of it appeared: chosen again, once
            loaded, check = worker("forkme")  # the loader passes over an untrusted one: this one again
            self.assertEqual((loaded.source, check()), ("personal", "unchanged"))
            tool.run(self.root, plugin_action="trust", name="forkme")  # a trusted one is selected instead
            self.assertEqual(check(), "changed")
            self.enable("forkme", False)
        restarts, hung, stopped = {}, {}, {}
        self.assertTrue(host._may_restart(self.root, "keeper0", host.RELOAD_EXIT, restarts, hung, stopped))
        self.assertEqual((restarts, stopped), ({}, {}))  # started again at once, never counted against it


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
        and a worker restart, even an event it finished only after its limit, and the shared cursor waits on the
        stuck event until it ends; fragile because of per-handler bookkeeping and read windows: a sibling 1,200
        events ahead once got them all again (PR #20 review 4), and one that finished an event late was called
        with it again (a load-dependent full-suite failure at 6df7376)."""
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
        # And fast runs past its limit once, on event 1243, but finishes it: done, never called with it again.
        self.module.LATE.update(seq=1243, seconds=0.15)
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
                self.module.HOLD["slow"] = threading.Event()  # slow stays on event 1 until the cursor is checked
                self.emit(slow, 4)
                # The sibling has every event while the slow handler is still on the first, however many polls a
                # loaded machine takes (a call of fast's can end past its limit too), and the cursor waits on it.
                with mock.patch.object(host, "TIMEOUTS_PER_EVENT", 1000):  # not given up on yet, however long
                    self.assertTrue(self.deliver_until(lambda: self.seen("fast", "slow") == [1, 2, 3, 4], seconds=10,
                                                       scopes=[slow]), self.module.SEEN)
                    self.assertEqual(host._read_cursor(slow, "pair"), 0)  # never past an unfinished event
                self.module.HOLD["slow"].set()  # from here each of its calls returns 0.4 s (4 limits) after it starts
                self.assertTrue(self.deliver_until(lambda: host._read_cursor(slow, "pair") == 4, scopes=[slow]),
                                self.module.SEEN)
                self.assertEqual(self.seen("fast", "slow"), [1, 2, 3, 4])  # never replayed to the handler that finished
                # Each call ran past its limit for 4 limits' time, then returned: one call each, never repeated.
                self.assertEqual(self.seen("slow", "slow"), [1, 2, 3, 4])
                given_up = [error for error in self.loaded.errors if "gave up" in error]
                # Given up when the host looked while it still ran past 3 limits; finished if it looked only after it
                # had returned (a loaded machine): either way never before TIMEOUTS_PER_EVENT limits.
                counts = [re.search(r"gave up on seq (\d+) after (\d+) timeouts", error) for error in given_up]
                self.assertTrue(all(counts), given_up)
                self.assertTrue(set(int(found[1]) for found in counts) <= {1, 2, 3, 4}, given_up)
                self.assertTrue(all(int(found[2]) >= host.TIMEOUTS_PER_EVENT for found in counts), given_up)
            with self.subTest("a call that never returns, beside another universe"):
                self.module.SLEEP["seconds"] = 0.0  # set here too, so a failure above leaves this one alone
                self.module.HOLD["stuck"] = threading.Event()  # slow never returns there (until the test ends)
                self.emit(stuck, 3)
                self.emit(other, 2)
                host._deliver(self.loaded, self.mv, stuck, "pair")  # slow is on event 1 there for good
                # The other universe moves on meanwhile, in as many polls as a loaded machine takes.
                self.assertTrue(self.deliver_until(lambda: host._read_cursor(other, "pair") == 2, scopes=[other]),
                                self.module.SEEN)
                self.assertEqual(self.seen("slow", "other"), [1, 2])  # the stuck universe holds up only itself
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


class LateEndTest(Fixture):
    def test_a_late_end_answers_only_its_own_journal_and_event(self):
        """A handler call that finished after its limit answers only the event it was for, in the journal it was
        read from; fragile because the kept end was keyed by the handler alone, so once the journal was replaced
        an old journal's late end marked the new journal's event 1 finished without running it (reviewer's
        recheck at 5d76fe9), and that event is silently skipped."""
        held = threading.Event()
        self.module.HOLD["test"] = held  # slow is stuck on event 1 until set
        self.emit(self.root, 1)
        key = f"handler 0 in {self.root} #0"
        with mock.patch.object(host, "HANDLER_SECONDS", 0.02), mock.patch.object(host, "TIMEOUTS_PER_EVENT", 1000):
            host._deliver(self.loaded, self.mv, self.root, "pair")  # runs past its limit: kept as a late end
            self.assertEqual(self.seen("slow"), [1])
            held.set()
            deadline = time.monotonic() + 10
            while loader.still_running(self.loaded, key) and time.monotonic() < deadline:
                time.sleep(0.01)
            previous = events.journal_id(self.root)
            shutil.rmtree(events.directory(self.root))  # the journal replaced: its event 1 is another event
            self.emit(self.root, 1)
            self.assertNotEqual(events.journal_id(self.root), previous)
            host._deliver(self.loaded, self.mv, self.root, "pair")
        self.assertEqual(self.seen("slow"), [1, 1])  # the new event 1 ran; the old end didn't stand in for it
        self.assertEqual(host._read_cursor(self.root, "pair"), 1)


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
