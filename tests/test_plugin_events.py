"""The event journal and the plugin host: recording, rolling, pruning, reading, handlers, services and cursors."""
import errno
import json
import fcntl
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

    def test_a_prompt_event_says_what_became_of_it_and_is_never_written_twice(self):
        """prompt_event tells core written, unsubscribed or retry, writes a replayed key once and never shows the key; else core drops or repeats prompt edges, or plugins see its bookkeeping."""
        agent = self.agent("worker")
        opened = {"id": "p1", "kind": "permission", "requires_waiting": True}
        self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k1"), "unsubscribed")
        self.assertEqual(events.segments(self.root), [])  # nobody subscribes: nothing written
        self.subscribe(("prompt.*",))
        for _ in range(2):  # the second is a replay after an append whose acknowledgement was lost
            self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k1"), "written")
        self.assertEqual(events.prompt_event(agent, "prompt.closed", "L1",
                                             {"id": "p1", "kind": "permission", "state": "gone", "cause": "resolved"},
                                             key="k2"), "written")
        found, _ = events.read(self.root, 0)
        self.assertEqual([(event["type"], event["data"]["id"]) for event in found],
                         [("prompt.opened", "p1"), ("prompt.closed", "p1")])
        self.assertTrue(all(events.REPLAY not in event for event in found))  # core's key, never a plugin's
        self.assertEqual(found[0]["agent"]["name"], "worker")
        for case, call in (("the journal busy", lambda: mock.patch.object(events, "LOCK_SECONDS", 0)),
                           ("no replay key", None)):
            with self.subTest(case):
                if call is None:
                    self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key=""), "retry")
                    continue
                with events._bounded_lock(events.directory(self.root) / ".lock") as held, call():
                    self.assertTrue(held)
                    self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k3"), "retry")
        self.assertEqual(len(events.read(self.root, 0)[0]), 2)
        with self.subTest("a replay after more than the journal's tail was written since"):
            self.subscribe(("prompt.*", "agent.*"), name="busy")
            for index in range(200):  # other writers push k1 far out of the tail
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"pad": "x" * 512, "n": index})
            self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k1"), "written")
            self.assertEqual(sum(event["type"] == "prompt.opened" for event in events.read(self.root, 0, 10_000)[0]),
                             1)  # its receipt says so: not written twice
            self.assertTrue(events.acknowledge(agent, ["k1", "k2"]))
            self.assertEqual(json.loads((events.directory(self.root) / events.RECEIPTS).read_text()), {})
        with self.subTest("a receipt that couldn't be saved"):
            with mock.patch.object(events, "_write_receipts", side_effect=OSError(28, "No space left on device")):
                self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k4"), "retry")
            self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k4"), "written")
        with self.subTest("the receipts caught mid-rewrite"):
            events.prompt_event(agent, "prompt.opened", "L1", opened, key="k5")
            for index in range(200):  # k5 far out of the tail: only its receipt knows it
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"pad": "x" * 512, "n": index})
            receipts, real, missed = events.directory(self.root) / events.RECEIPTS, Path.read_text, []

            def replaced_once(path, *args, **kwargs):
                if path == receipts and not missed:
                    missed.append(path)
                    raise FileNotFoundError(2, "No such file or directory", str(path))
                return real(path, *args, **kwargs)
            with mock.patch.object(Path, "read_text", replaced_once):
                self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k5"), "written")
            self.assertTrue(missed)
        with self.subTest("a torn line holding the key proves nothing"):
            _, segment = events.segments(self.root)[-1]
            torn = events._line({"schema": 1, "seq": 10 ** 6, "type": "prompt.opened", "agent": None, "launch": "L1",
                                 "data": opened, events.REPLAY: "k7"})

            def short_append(root, event):
                with segment.open("ab") as stream:
                    stream.write(torn[:-2])
                raise OSError("short write")

            with mock.patch.object(events, "_append", side_effect=short_append):
                self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k7"), "retry")
            self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k7"), "written")
            self.assertTrue(any(event["data"]["id"] == "p1" and event["seq"] < 10 ** 6
                                for event in events.read(self.root, 0, 10_000)[0][-1:]))  # written for real
        with self.subTest("receipts that aren't UTF-8"):
            (events.directory(self.root) / events.RECEIPTS).write_bytes(b"\xff\xfe{")
            self.assertEqual(events.prompt_event(agent, "prompt.opened", "L1", opened, key="k6"), "written")  # not
            # retried for ever: unreadable receipts fall back to the tail
        self.assertEqual(sum(event["type"] == "prompt.opened" and event["data"]["id"] == "p1"
                             for event in events.read(self.root, 0, 10_000)[0]), 5)  # k1, k4, k5, k6, k7: each once

    def test_replay_proof_includes_a_complete_row_at_the_tail_boundary(self):
        """A saved event can be appended twice when the receipt is lost and its row starts at the tail boundary."""
        agent = self.agent("worker")
        self.subscribe(("prompt.*",))
        opened = {"kind": "question", "requires_waiting": False}
        for identity in ("earlier", "boundary"):
            self.assertEqual(events.prompt_event(agent, "prompt.opened", "launch", {**opened, "id": identity},
                                                 key=identity), "written")
        _, segment = events.segments(self.root)[-1]
        _, tail = segment.read_bytes().split(b"\n", 1)
        with mock.patch.object(events, "TAIL_BYTES", len(tail)), mock.patch.object(events, "_receipts", return_value={}):
            self.assertEqual(events.prompt_event(agent, "prompt.opened", "launch", {**opened, "id": "boundary"},
                                                 key="boundary"), "written")
        self.assertEqual([row["data"]["id"] for row in events.read(self.root, 0)[0]], ["earlier", "boundary"])

    def test_a_complete_row_without_its_final_newline_reaches_incremental_readers_once(self):
        """A short write can leave JSON visible in history but hidden from a subscriber's current cursor."""
        agent = self.agent("worker")
        self.subscribe(("prompt.*",))
        opened = {"kind": "question", "requires_waiting": False}
        self.assertEqual(events.prompt_event(agent, "prompt.opened", "launch", {**opened, "id": "earlier"},
                                             key="earlier"), "written")
        _, segment = events.segments(self.root)[-1]

        def lose_newline(root, event):
            with segment.open("ab") as stream:
                stream.write(events._line({**event, "seq": 2})[:-1])
            raise OSError("short write")

        with mock.patch.object(events, "_append", side_effect=lose_newline):
            self.assertEqual(events.prompt_event(agent, "prompt.opened", "launch", {**opened, "id": "q"},
                                                 key="complete"), "retry")
        before, _ = events.read(self.root, 0)
        self.assertEqual([row["data"]["id"] for row in before], ["earlier", "q"])
        self.assertEqual(events.read(self.root, 1)[0], [])
        self.assertEqual(events.prompt_event(agent, "prompt.opened", "launch", {**opened, "id": "q"},
                                             key="complete"), "written")
        opened_after, _ = events.read(self.root, 1)
        self.assertEqual([(row["seq"], row["data"]["id"]) for row in opened_after], [(2, "q")])

    def test_a_busy_journal_spills_events_and_merges_them_in_order(self):
        """Events recorded while the journal is busy are spilled and merged in order, each once; a break drops or doubles them."""
        self.subscribe()
        spill = events.directory(self.root) / events.SPILL
        with self.subTest("busy, then merged"):
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": 0})
            with events._bounded_lock(events.directory(self.root) / ".lock") as held:
                self.assertTrue(held)
                for index in (1, 2, 3):
                    self.assertIsNone(events.emit(self.root, "agent.status", {"id": "a", "name": "w"},
                                                  data={"n": index}))
            self.assertEqual([event["data"]["n"] for event in events.read(self.root, 0)[0]], [0])
            events.merge(self.root)  # the plugin host's poll, with no writer since
            found = events.read(self.root, 0)[0]
            self.assertEqual([event["data"]["n"] for event in found], [0, 1, 2, 3])
            self.assertEqual([event["seq"] for event in found], [1, 2, 3, 4])
            self.assertEqual(sorted(path.name for path in spill.iterdir()), [])
        with self.subTest("a spiller that opened its file before a merger took it"):
            events._spill(self.root, {"schema": 1, "seq": 0, "type": "agent.status", "data": {"n": 4}})
            [first] = spill.iterdir()
            real, raced = fcntl.flock, []

            def merger_between(descriptor, operation):  # after the spiller's open, before its lock: a merger takes
                if not raced:                          # the file, reads it and deletes it
                    raced.append(True)
                    taken = spill / (".merging-" + first.name)
                    os.replace(first, taken)
                    events.merge(self.root)
                return real(descriptor, operation)
            with mock.patch.object(events.fcntl, "flock", side_effect=merger_between):
                events._spill(self.root, {"schema": 1, "seq": 0, "type": "agent.status", "data": {"n": 5}})
            events.merge(self.root)
            self.assertEqual([event["data"]["n"] for event in events.read(self.root, 0)[0]][-2:], [4, 5])
        with self.subTest("a merge cut short goes on where it stopped"):
            for index in range(6, 46):
                events._spill(self.root, {"schema": 1, "seq": 0, "type": "agent.status", "data": {"n": index}})
            with spill.joinpath(next(spill.iterdir()).name).open("ab") as handle:
                handle.write(b'{"schema":1,"seq":0,"type":"agent.st')  # torn by a killed process
            with events._bounded_lock(events.directory(self.root) / ".lock") as held:
                events._merge_spills(self.root, budget=600)  # a few lines only
            self.assertTrue(any(path.name.endswith(".offset") for path in spill.iterdir()))
            events.merge(self.root)
            numbers = [event["data"]["n"] for event in events.read(self.root, 0, 1000)[0]]
            self.assertEqual([n for n in numbers if n >= 6], list(range(6, 46)))  # each once, in order; torn one dropped
            self.assertEqual(list(spill.iterdir()), [])

    def test_spilled_events_stay_first_once_and_bounded(self):
        """Spilled events stay first, a cut-off merge never repeats a row, and a locked spill file never stalls a recorder."""
        self.subscribe()

        def spill(*numbers):
            with events._bounded_lock(events.directory(self.root) / ".lock") as held, \
                    mock.patch.object(events, "LOCK_SECONDS", 0):
                self.assertTrue(held)
                for n in numbers:
                    events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": n})

        def seen():
            return [event["data"]["n"] for event in events.read(self.root, 0, 1000)[0]]
        with self.subTest("a newer event behind spills one merge can't drain"):
            spill(1, 2)
            first = events._spill_path(self.root).read_bytes().splitlines(keepends=True)[0]
            merge = events._merge_spills
            with mock.patch.object(events, "_merge_spills",
                                   side_effect=lambda root, budget=None, single=False: merge(root, len(first) + 1)):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": 3})
            events.merge(self.root)
            self.assertEqual(seen(), [1, 2, 3])
        with self.subTest("a merge cut off right after an append"):
            spill(4, 5)
            append = events._append

            def cut_off(root, event):
                append(root, event)
                raise OSError("cut off after the append")
            with mock.patch.object(events, "_append", side_effect=cut_off):
                events.merge(self.root)
            events.merge(self.root)
            self.assertEqual(seen()[-2:], [4, 5])
            self.assertEqual(seen().count(4), 1)
        with self.subTest("a spill file held locked"):
            path = events._spill_path(self.root)
            path.parent.mkdir(parents=True, exist_ok=True)
            holder = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(holder, fcntl.LOCK_EX)
            started = time.monotonic()
            spill(6, 7)
            self.assertLess(time.monotonic() - started, 2)  # never waits on it
            os.close(holder)
            events.merge(self.root)
            self.assertEqual(seen()[-2:], [6, 7])  # kept in a new file of its own, merged after, in order
            before = events._spill_path(self.root).name
            with mock.patch.object(events.time, "time_ns", return_value=1):  # the clock stepped back
                self.assertGreater(events._spill_path(self.root, fresh=True).name, before)  # still sorts after

    def test_spills_survive_cut_off_merges_torn_writes_and_keep_cross_process_order(self):
        """Spills stay whole, once and in age order across processes through cut-off merges and torn writes; else events vanish."""
        self.subscribe()
        base = events.directory(self.root)

        def spill(*numbers):
            with events._bounded_lock(base / ".lock"), mock.patch.object(events, "LOCK_SECONDS", 0):
                for n in numbers:
                    events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": n})

        def seen(after=0):
            return [row["data"]["n"] for row in events.read(self.root, after, 1000)[0]]
        events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": 0})
        with self.subTest("cut off after the append, before its number was published"):
            spill(1)
            with mock.patch.object(events, "_write_counter", side_effect=OSError("cut off")):
                events.merge(self.root)
            events.merge(self.root)
            self.assertEqual(seen(1), [1])  # a reader past 0 sees it
        with self.subTest("progress a cut-off cleanup left behind"):
            folder = base / events.SPILL
            (folder / (events.CLAIM + events._spill_path(self.root).name[:-6] + ".jsonl.offset")).write_text(
                json.dumps({"offset": 10 ** 6, "pending": None}))
            spill(2, 3)
            events.merge(self.root)
            self.assertEqual(seen()[-2:], [2, 3])
        with self.subTest("a merge that fails keeps the event that tried it"):
            spill(4)
            with mock.patch.object(events, "_save_progress", side_effect=OSError("no room")):
                events.emit(self.root, "agent.status", {"id": "a", "name": "w"}, data={"n": 5})
            events.merge(self.root)
            self.assertEqual(seen()[-2:], [4, 5])
        with self.subTest("a short write"):
            real = events.os.write
            with mock.patch.object(events.os, "write", lambda fd, data: real(fd, data[:max(1, len(data) // 2)])):
                spill(6)
            spill(7)
            events.merge(self.root)
            self.assertEqual(seen()[-2:], [6, 7])
        with self.subTest("two processes' spills in the order they happened"):
            with mock.patch.object(events.os, "getpid", return_value=11):
                spill(8)
                first = events._spill_name
            with mock.patch.object(events.os, "getpid", return_value=12):
                spill(9)
            with mock.patch.object(events, "_spill_name", first), \
                    mock.patch.object(events.os, "getpid", return_value=11):
                spill(10)
            events.merge(self.root)
            self.assertEqual(seen()[-3:], [8, 9, 10])
        with self.subTest("a special file or a link where a spill would go"):
            path = events._spill_path(self.root)
            path.unlink(missing_ok=True)
            os.mkfifo(path)  # opening it to write would block without O_NONBLOCK
            started = time.monotonic()
            spill(11)
            self.assertLess(time.monotonic() - started, 2)
            events.merge(self.root)
            self.assertEqual(seen()[-1], 11)  # written to a fresh file instead, and merged
            folder = base / events.SPILL
            elsewhere = self.work / "elsewhere"
            elsewhere.mkdir()
            for child in folder.iterdir():
                child.unlink()
            folder.rmdir()
            folder.symlink_to(elsewhere)  # the spill folder a link: never written through
            spill(12)
            self.assertEqual(list(elsewhere.iterdir()), [])

    def test_merges_hold_order_and_once_through_damage_and_bounds(self):
        """Merges keep each process's files in order, damage never replayed, work bounded; else events double or reorder."""
        self.subscribe()
        base = events.directory(self.root)

        def seed(pid, numbers, padding=""):
            with events._bounded_lock(base / ".lock"), mock.patch.object(events, "LOCK_SECONDS", 0), \
                    mock.patch.object(events.os, "getpid", return_value=pid):
                for n in numbers:
                    with mock.patch.object(events, "now", return_value=f"2026-01-01T00:{n // 60:02}:{n % 60:02}+00:00"):
                        events.emit(self.root, "agent.status", data={"n": n, "pad": padding})

        def seen():
            return [row["data"]["n"] for row in events.read(self.root, 0, 1000)[0]]

        def cut_off_at(number):
            real = events._append

            def append(root, event):
                done = real(root, event)
                if event["data"]["n"] == number:
                    raise OSError("cut off after the append")
                return done
            return mock.patch.object(events, "_append", append)
        with self.subTest("a cut-off merge across files repeats nothing"):
            seed(11, [0])
            seed(12, range(1, 9), "x" * 12000)
            with cut_off_at(8):
                events.merge(self.root)
            events.merge(self.root)
            self.assertEqual(seen(), list(range(9)))
        with self.subTest("progress that can't be read waits, never starts over"):
            seed(13, [9])
            with cut_off_at(9):
                events.merge(self.root)
            real_open = os.open

            def unreadable(path, flags, *args, **keywords):
                if flags & os.O_ACCMODE == os.O_RDONLY and str(path).endswith(".offset"):
                    raise OSError(errno.EIO, "progress unreadable for now")
                return real_open(path, flags, *args, **keywords)
            with mock.patch.object(os, "open", unreadable):
                events.merge(self.root)
            events.merge(self.root)
            self.assertEqual(seen(), list(range(10)))
        with self.subTest("damaged progress: the journal says which rows are in"):
            seed(14, [10, 11])
            with cut_off_at(10):
                events.merge(self.root)
            [progress] = [path for path in (base / events.SPILL).iterdir() if path.name.endswith(".offset")]
            progress.write_text("not progress")
            events.merge(self.root)
            self.assertEqual(seen(), list(range(12)))
        with self.subTest("a spill folder that can't be opened isn't an empty backlog"):
            seed(15, [12])
            real_open = os.open

            def refused(path, flags, *args, **keywords):
                if Path(path) == base / events.SPILL:
                    raise OSError(errno.EACCES, "refused for now")
                return real_open(path, flags, *args, **keywords)
            with mock.patch.object(os, "open", refused):
                events.emit(self.root, "agent.status", data={"n": 99})
            self.assertNotIn(99, seen())  # never ahead of 12
            events.merge(self.root)
            self.assertEqual(seen()[-1], 12)
        with self.subTest("one process's files in succession, whatever the clock said"):
            seed(16, [14])
            with mock.patch.object(events.os, "getpid", return_value=16):
                first = events._spill_path(self.root)
            with first.open("rb") as holder:
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
                seed(16, [13])  # recorded later, though the clock reads earlier: a new file of this process
            events.merge(self.root)
            self.assertEqual(seen()[-2:], [14, 13])
        with self.subTest("a merge that fails mid-way leaves no file open"):
            seed(17, [15])
            seed(18, [16])
            held, real_open, real_close, made = set(), os.open, os.close, []

            def opening(*args, **kwargs):
                descriptor = real_open(*args, **kwargs)
                held.add(descriptor)
                return descriptor

            def closing(descriptor):
                held.discard(descriptor)
                real_close(descriptor)

            def reader(*args):
                if made:
                    raise OSError(errno.EMFILE, "out of descriptors")
                made.append(real(*args))
                return made[0]
            real = events._Claimed
            with mock.patch.object(events, "_Claimed", reader), mock.patch.object(os, "open", opening), \
                    mock.patch.object(os, "close", closing):
                events.merge(self.root)
            self.assertEqual(held, set())
            events.merge(self.root)
            self.assertEqual(seen()[-2:], [15, 16])
        with self.subTest("a recorder reads no more than its budget"):
            for pid in range(4):
                seed(20 + pid, range(17 + 3 * pid, 20 + 3 * pid), "x" * 12000)
            reads, real_pread = [], os.pread
            with mock.patch.object(os, "pread", lambda fd, size, at: reads.append(size) or real_pread(fd, size, at)):
                events.emit(self.root, "agent.status", data={"n": 40})
            self.assertLessEqual(sum(reads), events.EMIT_MERGE_BYTES)

    def test_damaged_progress_and_merge_limits_never_double_hide_or_reorder(self):
        """Damaged progress reconciles once, no row passes an older one, a recorder stays in budget; else events double."""
        self.subscribe()
        base = events.directory(self.root)
        def seed(pid, numbers, padding=""):  # each row recorded at second `n`, so the right order is by number
            with events._bounded_lock(base / ".lock"), mock.patch.object(events, "LOCK_SECONDS", 0), \
                    mock.patch.object(events.os, "getpid", return_value=pid):
                for n in numbers:
                    with mock.patch.object(events, "now", return_value=f"2026-01-01T00:{n // 60:02}:{n % 60:02}+00:00"):
                        events.emit(self.root, "agent.status", data={"n": n, "pad": padding})

        def seen(after=0):
            return [row["data"]["n"] for row in events.read(self.root, after, 1000)[0]]

        def interrupted(numbers, padding=""):
            seed(41, numbers, padding)
            real = events._append

            def append(root, event):
                done = real(root, event)
                if event["data"]["n"] == numbers[-1]:
                    raise OSError("cut off after the last append")
                return done
            with mock.patch.object(events, "_append", append):
                events.merge(self.root)
            [progress] = [path for path in (base / events.SPILL).iterdir() if path.name.endswith(".offset")]
            progress.write_text("torn")
        with self.subTest("damaged progress reconciled across a budget cut"):
            interrupted([0, 1, 2], "x" * 12000)
            with events._bounded_lock(base / ".lock"):
                events._merge_spills(self.root, budget=12500)
            events.merge(self.root)
            self.assertEqual(seen(), [0, 1, 2])
        with self.subTest("a journal that can't be read proves nothing"):
            interrupted([3])
            real = Path.read_bytes
            segment = events.segments(self.root)[-1][1]
            with mock.patch.object(type(segment), "read_bytes",
                                   lambda path: (_ for _ in ()).throw(OSError("unreadable")) if path == segment
                                   else real(path)):
                events.merge(self.root)
            events.merge(self.root)
            self.assertEqual(seen(), [0, 1, 2, 3])
        with self.subTest("a recovered row's number is published"):
            last = seen()
            seed(42, [4])
            with mock.patch.object(events, "_write_counter", side_effect=OSError("cut off before the counter")):
                events.merge(self.root)
            [progress] = [path for path in (base / events.SPILL).iterdir() if path.name.endswith(".offset")]
            progress.write_text("torn")
            events.merge(self.root)
            self.assertEqual(seen(len(last)), [4])
        with self.subTest("no append while another process's next row is unknown"):
            seed(43, [6])  # a small row, recorded later
            seed(44, [5], "x" * 12000)  # a large one, recorded earlier: unknown within the budget below
            first = min((base / events.SPILL).glob("*.jsonl"))
            with events._bounded_lock(base / ".lock"):
                events._merge_spills(self.root, budget=4 * first.stat().st_size)
            events.merge(self.root)
            self.assertEqual(seen()[-2:], [5, 6])
        with self.subTest("a later row never passes an older one written to a reused file"):
            seed(45, [7])
            with mock.patch.object(events.os, "getpid", return_value=45):
                mine = events._spill_name
            seed(46, [8])
            with mock.patch.object(events, "_spill_name", mine):
                seed(45, [9])
            events.merge(self.root)
            events.merge(self.root)
            self.assertEqual(seen()[-3:], [7, 8, 9])
        with self.subTest("a recorder's reads and appends stay in its budget, and it never reconciles"):
            seed(47, range(10, 18), "x" * 12000)
            spent, real_read, real_append = [], os.pread, events._append

            def counted_read(fd, size, at):
                data = real_read(fd, size, at)
                spent.append(len(data))
                return data

            def counted_append(root, event):
                spent.append(len(events._line(event)))
                return real_append(root, event)
            with mock.patch.object(os, "pread", counted_read), mock.patch.object(events, "_append", counted_append):
                events.emit(self.root, "agent.status", data={"n": 18})
            self.assertLessEqual(sum(spent), events.EMIT_MERGE_BYTES)
            events.merge(self.root)
            interrupted([19])
            reads, real = [], Path.read_bytes
            with mock.patch.object(Path, "read_bytes",
                                   lambda path: (lambda data: reads.append(len(data)) or data)(real(path))):
                events.emit(self.root, "agent.status", data={"n": 20})
            self.assertEqual(reads, [])  # the whole journal is the host's to read

    def test_a_merge_tells_order_cheaply_holds_no_file_and_never_guesses(self):
        """Merges order rows from a few bytes each, hold no file open, never guess an unreadable one; else they stall."""
        self.subscribe()
        base = events.directory(self.root)

        def seed(pid, numbers, padding=""):  # each row recorded at second `n`, so the right order is by number
            with events._bounded_lock(base / ".lock"), mock.patch.object(events, "LOCK_SECONDS", 0), \
                    mock.patch.object(events.os, "getpid", return_value=pid):
                for n in numbers:
                    with mock.patch.object(events, "now", return_value=f"2026-01-01T00:{n // 60:02}:{n % 60:02}+00:00"):
                        events.emit(self.root, "agent.status", data={"n": n, "pad": padding})

        def seen():
            return [row["data"]["n"] for row in events.read(self.root, 0, 1000)[0]]
        with self.subTest("a waiting file that can't be read holds newer rows back"):
            mark = len(seen())
            seed(51, [101])
            seed(52, [100])
            unreadable, real_open = max((base / events.SPILL).glob("*.jsonl")).name, os.open

            def opening(name, *args, **kwargs):
                if name == unreadable:
                    raise OSError(errno.EIO, "unreadable for now")
                return real_open(name, *args, **kwargs)
            with mock.patch.object(os, "open", opening):
                events.merge(self.root)
            self.assertEqual(seen()[mark:], [])
            events.merge(self.root)
            self.assertEqual(seen()[mark:], [100, 101])
        with self.subTest("large rows at a small budget still go, each merge within it"):
            mark = len(seen())
            for pid in range(61, 67):
                seed(pid, [pid + 49], "x" * 12000)
            spent, real_pread = [], os.pread

            def counting(descriptor, size, at):
                data = real_pread(descriptor, size, at)
                spent[-1] += len(data)
                return data
            with mock.patch.object(os, "pread", counting), \
                    events._bounded_lock(base / ".lock"):
                for _ in range(12):
                    spent.append(0)
                    events._merge_spills(self.root, budget=30000)
            self.assertLessEqual(max(spent), 30000)
            self.assertEqual(seen()[mark:], list(range(110, 116)))
        with self.subTest("a merge holds no spill file open between reads"):
            mark = len(seen())
            for pid in range(71, 76):
                seed(pid, [pid + 49, pid + 54])
            held, most, real_open, real_close = set(), [0], os.open, os.close

            def opening(*args, **kwargs):
                descriptor = real_open(*args, **kwargs)
                held.add(descriptor)
                most[0] = max(most[0], len(held))
                return descriptor

            def closing(descriptor):
                held.discard(descriptor)
                real_close(descriptor)
            with events._bounded_lock(base / ".lock"), mock.patch.object(os, "open", opening), \
                    mock.patch.object(os, "close", closing):
                events._merge_spills(self.root)
            self.assertLessEqual(most[0], 3)  # the spill folder, a file read or claimed, a progress write
            self.assertEqual(sorted(seen()[mark:]), list(range(120, 130)))
        with self.subTest("claims of a reused name keep their order whatever the clock does"):
            mark = len(seen())
            seed(81, [130])
            first = events._spill_name
            folder = os.open(base / events.SPILL, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with events._bounded_lock(base / ".lock"):
                    with mock.patch.object(events.time, "time_ns", return_value=200):
                        self.assertTrue(events._claim(folder, first[1]))
                    with mock.patch.object(events, "_spill_name", first):
                        seed(81, [131])
                    with mock.patch.object(events.time, "time_ns", return_value=100):
                        self.assertTrue(events._claim(folder, first[1]))
            finally:
                os.close(folder)
            events.merge(self.root)
            self.assertEqual(seen()[mark:], [130, 131])
        with self.subTest("a recorder leaves a row in doubt to the host"):
            mark = len(seen())
            seed(91, [140])
            real = events._append

            def cut_off(root, event):
                real(root, event)
                raise OSError("cut off right after the append")
            with mock.patch.object(events, "_append", cut_off):
                events.merge(self.root)
            with mock.patch.object(events, "_replayed", wraps=events._replayed) as replayed, \
                    mock.patch.object(events, "now", return_value="2026-01-01T00:02:21+00:00"):
                events.emit(self.root, "agent.status", data={"n": 141, "pad": ""})
            self.assertEqual(replayed.call_count, 0)  # proving it reads the journal: the host's work
            events.merge(self.root)
            self.assertEqual(seen()[mark:], [140, 141])
        with self.subTest("a waiting file a spiller holds mid-row holds newer rows back"):
            mark = len(seen())
            seed(101, [151])
            with mock.patch.object(events.os, "getpid", return_value=102):
                waiting = events._spill_path(self.root)
            row = events._line({"at": "2026-01-01T00:02:30+00:00", "schema": 1, "seq": 0, "type": "agent.status",
                                "agent": None, "launch": None, "data": {"n": 150, "pad": ""},
                                events.REPLAY: "spill/held"})
            with waiting.open("wb") as spilling:
                fcntl.flock(spilling.fileno(), fcntl.LOCK_EX)
                spilling.write(row[:11])
                spilling.flush()
                events.merge(self.root)
                before = seen()[mark:]
                spilling.write(row[11:])
                spilling.flush()
            events.merge(self.root)
            self.assertEqual((before, seen()[mark:]), ([], [150, 151]))
        with self.subTest("a process's later file never goes before its earlier one, whatever its rows' times"):
            mark = len(seen())
            seed(110, [162])
            seed(111, [163])
            with mock.patch.object(events.os, "getpid", return_value=111):
                earlier = events._spill_path(self.root)
            with earlier.open("rb") as holding:
                fcntl.flock(holding.fileno(), fcntl.LOCK_EX)
                seed(111, [161])  # held: a new file of process 111, its row dated earlier (the clock stepped back)
            events.merge(self.root)
            self.assertEqual(seen()[mark:], [162, 163, 161])
        with self.subTest("many files at a small budget merge across passes, each going on from the last"):
            mark = len(seen())
            for k in range(26):
                seed(120 + k, [170 + k])
            with events._bounded_lock(base / ".lock"):
                for _ in range(16):
                    events._merge_spills(self.root, budget=2900)
            self.assertEqual(seen()[mark:], list(range(170, 196)))
        spill = base / events.SPILL
        with self.subTest("a row a cut-off merge appended is settled before anything newer goes in"):
            mark = len(seen())
            seed(130, [230])
            real = events._append

            def cut_off(root, event):
                real(root, event)
                raise OSError("cut off right after the append")
            with mock.patch.object(events, "_append", cut_off):
                events.merge(self.root)
            seed(131, range(220, 229), "x" * 12000)  # dated earlier, enough to push it out of the replay tail
            events.merge(self.root)
            self.assertEqual(seen()[mark:], [230, *range(220, 229)])
        with self.subTest("lines that aren't events are passed once, not read again by every merge"):
            mark = len(seen())
            seed(132, [240])
            waiting = max(spill.glob("*.jsonl"))
            waiting.write_bytes((b"{" + b"x" * 1100 + b"\n") * 4 + waiting.read_bytes())
            with events._bounded_lock(base / ".lock"):
                for _ in range(9):
                    events._merge_spills(self.root, budget=3000)
            self.assertEqual(seen()[mark:], [240])
        with self.subTest("a file claimed before keeps its place among its process's files"):
            mark = len(seen())
            seed(133, [251])
            with (spill / events._spill_name[1]).open("rb") as holding:
                fcntl.flock(holding.fileno(), fcntl.LOCK_EX)
                seed(133, [250])  # a later file of that process, dated earlier
            folder = os.open(spill, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with events._bounded_lock(base / ".lock"):
                    self.assertTrue(events._claim(folder, events._spill_name[1]))  # the later one claimed first
            finally:
                os.close(folder)
            events.merge(self.root)
            self.assertEqual(seen()[mark:], [251, 250])
        with self.subTest("a saved time out of range is learned again"):
            mark = len(seen())
            seed(134, [260])
            folder = os.open(spill, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with events._bounded_lock(base / ".lock"):
                    claimed = events._claim(folder, events._spill_name[1])
            finally:
                os.close(folder)
            (spill / (claimed + ".offset")).write_text(json.dumps({"offset": 0, "pending": None, "when": 10 ** 400}))
            events.merge(self.root)
            self.assertEqual(seen()[mark:], [260])

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
