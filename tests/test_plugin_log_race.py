"""Plugin logs rotate at their limit, and two processes writing one log at once rotate it once and lose no line."""
from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import agent_files, api, events, host, loader
from multiverse.plugins.testing import PluginTestCase

SOURCE = Path(__file__).resolve().parents[1] / "src"

# A writer that says it is ready, waits for `go`, then logs one line. Path.replace is slowed, so two writers that
# both decide to rotate overlap: without a lock between processes the second rotation replaces the first's NAME.1.
WRITER = textwrap.dedent("""
    import pathlib, sys, time, types
    from multiverse.plugins import loader
    kind, log, go, marker = sys.argv[1:5]
    real = pathlib.Path.replace
    def slow(self, target):
        time.sleep(0.4)
        return real(self, target)
    pathlib.Path.replace = slow
    loader.ERRORS_LOG_BYTES = loader.LOG_BYTES = 100
    pathlib.Path(go).with_name(f"{kind}-{marker}.ready").touch()
    while not pathlib.Path(go).exists():
        time.sleep(0.005)
    if kind == "errors":
        root = pathlib.Path(log).parents[3]
        loader.record(types.SimpleNamespace(errors=[], name="p"), marker, ValueError(marker), root)
    else:
        loader.append_log(log, marker)
""")


def hold_lock(path, seconds):
    """A separate process holding mv2's file lock at `path` for `seconds`; returns it once the lock is held."""
    process = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, sys.argv[3]); from multiverse.store import lock\n"
        "with lock(sys.argv[1]):\n    print('held', flush=True); time.sleep(float(sys.argv[2]))"),
        str(path), str(seconds), str(SOURCE)], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "held"
    return process


class LogRaceTest(PluginTestCase):
    def test_a_workers_last_line_waits_out_its_siblings_and_no_other_line_waits(self):
        """A worker's 'supervisor gone' line waits briefly for a sibling's write while every other host or event
        line skips at once; else workers stopping together lose a line, or recovery waits on a diagnostic lock."""
        log = host.host_directory(self.root) / "host.log"
        host._log(self.root, "first")
        lock = loader.log_lock_path(log)
        with self.subTest("a worker's last line waits out a sibling's write"):
            holder = hold_lock(lock, 0.05)
            host._stopping(self.root, "holder")
            holder.wait(10)
            self.assertIn("holder: supervisor gone; stopping", log.read_text())
        with self.subTest("with a lock held on, other lines skip at once and the last line waits only its bound"):
            events_log = self.root / "state" / "plugins" / "events.log"
            events._log(self.root, "events first")
            holders = [hold_lock(lock, 3.0), hold_lock(loader.log_lock_path(events_log), 3.0)]
            for holder in holders:
                self.addCleanup(holder.wait, 10)
            start = time.monotonic()
            host._log(self.root, "skipped host line")
            events._log(self.root, "skipped events line")
            self.assertLess(time.monotonic() - start, 0.1)  # recovery logs this way: it never waits
            start = time.monotonic()
            host._stopping(self.root, "late")
            self.assertGreaterEqual(time.monotonic() - start, host.STOPPING_LOG_WAIT * 0.9)
            self.assertLess(time.monotonic() - start, 2.0)
            self.assertNotIn("skipped", log.read_text() + events_log.read_text())
            self.assertNotIn("late:", log.read_text())
        with self.subTest("no try for the lock starts past the wait, and a write is never tried twice"):
            clock, attempts, writes = [0.0], [], []

            @contextmanager
            def busy_once(_path, _root, *, blocking):
                attempts.append(clock[0])
                if len(attempts) == 1:
                    clock[0] = host.STOPPING_LOG_WAIT - 0.001  # 1 ms left: a 5 ms nap would end past it
                    raise BlockingIOError
                yield

            def failing_write(text):
                writes.append(text)
                raise BlockingIOError  # after the append began

            fake = SimpleNamespace(monotonic=lambda: clock[0],
                                   sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
            path = self.work / "late.log"
            with mock.patch.object(loader, "time", fake), mock.patch.object(agent_files, "lock", busy_once):
                loader.append_log(path, "late", blocking=False, wait=host.STOPPING_LOG_WAIT)
            self.assertEqual((attempts, path.exists()), ([0.0], False))
            opened = nullcontext(SimpleNamespace(write=failing_write))
            with mock.patch.object(agent_files, "lock", return_value=nullcontext()), \
                    mock.patch.object(loader, "open_regular", return_value=opened):
                loader.append_log(path, "one line", blocking=False, wait=host.STOPPING_LOG_WAIT)
            self.assertEqual(writes, ["one line\n"])

    def test_two_processes_rotating_one_log_lose_no_line(self):
        """Plugin, errors, events and host logs rotate at their limit, errors.log keeping one traceback per call site
        a minute, a write waits for another process's rotation, and two processes rotating one log at once rotate it
        once and lose no line; a break goes unnoticed because the only symptom is diagnostic lines lost (or a full
        disk) while the dashboard, host and CLI write at once."""
        self.write_plugin("chatty", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
        self.enable("chatty")
        loaded = loader.load(self.entry("chatty"))
        with self.subTest("every log rotates"):
            mv = api.Plugin(loaded)
            with mock.patch.object(loader, "LOG_BYTES", 500):
                for index in range(40):
                    mv.log("x" * 40, self.root)
                    events._log(self.root, "y" * 40)
                    host._log(self.root, "z" * 40)
            for log in (self.root / "state" / "plugins" / "chatty" / "plugin.log",
                        self.root / "state" / "plugins" / "events.log", host.host_directory(self.root) / "host.log"):
                self.assertTrue(log.with_name(log.name + ".1").exists(), log)
                self.assertLess(log.stat().st_size, 1000)
        with self.subTest("errors.log is throttled and rotates"):
            log = self.root / "state" / "plugins" / "chatty" / "errors.log"
            for index in range(50):  # a column failing differently on every row, every refresh
                loader.guard(loaded, "column x", lambda index=index: 1 / 0 if index % 2 else int(f"x{index}"),
                             root=self.root)
            self.assertEqual(log.read_text().count("Traceback"), 1)  # one traceback per call site and minute
            with mock.patch.object(loader, "ERRORS_LOG_BYTES", 2000):
                for index in range(200):
                    loader.record(loaded, f"call {index}", ValueError("x" * 50), self.root)
            self.assertTrue(log.with_name("errors.log.1").exists())
            self.assertLess(log.stat().st_size, 2000 + 4000)
        with self.subTest("a write waits for another process rotating the log"):
            path = self.work / "state" / "plugin.log"
            path.parent.mkdir(parents=True)
            holder = hold_lock(loader.log_lock_path(path), 1.0)
            self.addCleanup(holder.wait, 10)
            start = time.monotonic()
            loader.append_log(path, "line")
            self.assertGreater(time.monotonic() - start, 0.6)
            self.assertEqual(path.read_text(), "line\n")
        with self.subTest("two processes rotating one log at once"):
            logs = {"errors": self.work / "state" / "plugins" / "p" / "errors.log",
                    "plugin": self.work / "state" / "plugins" / "p" / "plugin.log"}
            go = self.work / "go"
            writers = []
            for kind, log in logs.items():
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("OLD " * 100 + "\n")  # past the limit: the first writer rotates it
                writers += [subprocess.Popen([sys.executable, "-c", WRITER, kind, str(log), str(go),
                                              f"writer-{index}"], env={**os.environ, "PYTHONPATH": str(SOURCE)})
                            for index in range(2)]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and len(list(self.work.glob("*.ready"))) < len(writers):
                time.sleep(0.01)  # every writer imported and waits at the gate
            go.touch()
            for writer in writers:
                self.assertEqual(writer.wait(30), 0)
            for kind, log in logs.items():
                rotated = log.with_name(log.name + ".1")
                self.assertIn("OLD", rotated.read_text(), f"{kind}: the rotated file was replaced by a second rotation")
                text = log.read_text()
                for index in range(2):
                    self.assertIn(f"writer-{index}", text, f"{kind}: writer-{index}'s line was lost")
