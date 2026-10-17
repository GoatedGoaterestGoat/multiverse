"""tests/run.py, the parallel runner, on small test directories of its own: it runs what discovery runs, once
each; a failure, an import error, a hang or a write under a guarded home fails the run; a unit it stops still
cleans up; and runs at once share the machine's slots."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from isolation import isolate

RUNNER = Path(__file__).resolve().parent / "run.py"

RECORDING = """
import os, unittest

class Recorded(unittest.TestCase):
    def record(self):
        with open(os.environ["RECORD"], "a") as out:
            out.write(self.id() + "\\n")
"""


class RunnerTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        temporary = tempfile.TemporaryDirectory(prefix="mv2-runner-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.tests = self.work / "tests"
        self.tests.mkdir()
        self.record = self.work / "record.txt"
        os.environ["RECORD"] = str(self.record)
        self.slots = self.work / "slots"  # never the machine's: these runners must not wait for the suite's

    def module(self, name, text, where=None):
        (where or self.tests).joinpath(f"{name}.py").write_text(textwrap.dedent(text))

    def command(self, *args):
        return [sys.executable, str(RUNNER), "-s", str(self.tests), "--slots-directory", str(self.slots), *args]

    def run_tests(self, *args, timeout=120):
        return subprocess.run(self.command("-j", "3", *args), text=True, capture_output=True, timeout=timeout)

    def recorded(self):
        return sorted(self.record.read_text().split()) if self.record.exists() else []

    def test_it_runs_what_discovery_runs_once_each_even_split_into_shards(self):
        self.module("recording", RECORDING)
        self.module("test_alpha", """
            from recording import Recorded
            class First(Recorded):
                def test_one(self): self.record()
                def test_two(self): self.record()
                def test_three(self): self.record()
            class Second(Recorded):
                def test_four(self): self.record()
                def test_five(self): self.record()
            """)
        # A fixture class imported from another module, its own tests chosen by load_tests (as test_update_* do).
        self.module("test_beta", """
            import unittest
            from test_alpha import First
            class Derived(First):
                def test_six(self): self.record()
            def load_tests(loader, tests, pattern):
                return unittest.TestSuite(Derived(name) for name in loader.getTestCaseNames(Derived)
                                          if name in Derived.__dict__)
            """)
        # Tests from another directory added by load_tests (as test_plugin_checkouts adds the plugins' suites).
        extra = self.work / "extra"
        extra.mkdir()
        self.module("test_extra", """
            from recording import Recorded
            class Extra(Recorded):
                def test_seven(self): self.record()
            """, extra)
        self.module("test_gamma", f"""
            def load_tests(loader, tests, pattern):
                tests.addTests(loader.discover({str(extra)!r}, top_level_dir={str(extra)!r}))
                return tests
            """)
        discovered = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(self.tests)],
                                    text=True, capture_output=True, timeout=60)
        self.assertEqual(discovered.returncode, 0, discovered.stderr)
        expected = self.recorded()
        self.assertEqual(len(expected), 7)
        self.record.unlink()
        # A long recorded time splits test_alpha into shards.
        (self.work / ".run").mkdir()
        (self.work / ".run/test-durations.json").write_text(json.dumps({"modules": {"test_alpha": 100}}))
        result = self.run_tests()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), expected)
        self.assertIn("test_alpha (1 of 5)", result.stdout)
        self.assertIn("Ran 7 tests", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "OK")
        # This run's times replace the record, so the next run plans with them.
        times = json.loads((self.work / ".run/test-durations.json").read_text())["modules"]
        self.assertEqual(sorted(times), ["test_alpha", "test_beta", "test_gamma"])
        self.assertLess(times["test_alpha"], 100)

    def test_one_module_or_one_test_by_name(self):
        self.module("recording", RECORDING)
        self.module("test_alpha", """
            from recording import Recorded
            class First(Recorded):
                def test_one(self): self.record()
                def test_two(self): self.record()
            """)
        self.module("test_other", """
            import unittest
            class Other(unittest.TestCase):
                def test_never(self): self.fail("not asked for")
            """)
        result = self.run_tests("test_alpha.First.test_two", "test_alpha.First.test_two")  # asked twice, run twice
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), ["test_alpha.First.test_two"] * 2)
        self.record.unlink()
        result = self.run_tests(str(self.tests / "test_alpha.py"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), ["test_alpha.First.test_one", "test_alpha.First.test_two"])

    def test_a_failure_an_import_error_and_a_hang_each_fail_the_run(self):
        self.module("test_fails", """
            import unittest
            class Fails(unittest.TestCase):
                def test_wrong(self): self.assertEqual(1, 2, "the planted failure")
                def test_right(self): pass
            """)
        self.module("test_broken", """
            raise ImportError("the planted import error")
            """)
        cleaned = self.work / "cleaned"
        self.module("test_hangs", f"""
            import time, unittest
            from pathlib import Path
            class Hangs(unittest.TestCase):
                def test_forever(self):
                    self.addCleanup(Path({str(cleaned)!r}).write_text, "cleaned")
                    time.sleep(600)
            """)
        self.module("test_fine", """
            import unittest
            class Fine(unittest.TestCase):
                def test_fine(self): pass
            """)
        start = time.monotonic()
        result = self.run_tests("--timeout", "10")  # far past any start-up, however loaded the machine
        self.assertLess(time.monotonic() - start, 60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL: test_wrong", result.stdout)
        self.assertIn("the planted failure", result.stdout)
        self.assertIn("the planted import error", result.stdout)
        self.assertIn("test_hangs: 1 tests", result.stdout)
        self.assertIn("stopped after 10 s", result.stdout)
        self.assertIn("Stopped: stopped by tests/run.py", result.stdout)
        self.assertTrue(cleaned.exists())  # the hung test still ran its cleanups
        fine = [line for line in result.stdout.splitlines() if "] test_fine: 1 tests in" in line]
        self.assertEqual(len(fine), 1, result.stdout)
        self.assertNotIn("FAILED", fine[0])
        self.assertEqual(result.stdout.splitlines()[-1], "FAILED (failures=1, errors=2)")

    def test_a_unit_that_does_not_end_within_its_grace_is_killed(self):
        self.module("test_stubborn", """
            import os, signal, time, unittest
            class Stubborn(unittest.TestCase):
                def test_ignores_the_runner(self):
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    time.sleep(600)
            """)
        start = time.monotonic()
        result = self.run_tests("--timeout", "10", "--grace", "1")
        self.assertLess(time.monotonic() - start, 40)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("killed after 10 s, its cleanups not done in 1 s", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "FAILED (units without a result=1)")

    def test_a_write_under_a_guarded_home_fails_its_unit_even_when_the_code_swallows_the_refusal(self):
        guarded = self.work / "guarded"
        guarded.mkdir()
        self.module("test_writes", f"""
            import threading, unittest
            import home_safety

            class Writes(unittest.TestCase):
                def test_swallowed(self):
                    home_safety.install(home={str(guarded)!r})  # a stand-in for the real ~/.multiverse
                    def write():
                        try:
                            open({str(guarded / "cache.json")!r}, "w").close()
                        except PermissionError:
                            pass  # as a best-effort cache write would
                    thread = threading.Thread(target=write)
                    thread.start()
                    thread.join()
            """)
        result = self.run_tests()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # Charged to the test whose thread wrote, though the test is a plain TestCase and never named itself.
        self.assertIn("ERROR: test_swallowed (test_writes.Writes.test_swallowed)", result.stdout)
        self.assertIn(f"{guarded / 'cache.json'} in test_writes.Writes.test_swallowed", result.stdout)
        self.assertFalse((guarded / "cache.json").exists())

    def interrupted(self, how):
        """Start a run whose second unit waits, stop the runner with signal `how` once the first unit has failed and
        the second is waiting, and return (the runner's exit status, its output, the waiting worker's pid, and
        whether that test's cleanup ran)."""
        started, cleaned = self.work / "started", self.work / "cleaned"
        self.module("test_fails_first", """
            import unittest
            class FailsFirst(unittest.TestCase):
                def test_wrong(self): self.fail("the failure before the interrupt")
            """)
        self.module("test_waits", f"""
            import os, time, unittest
            from pathlib import Path
            class Waits(unittest.TestCase):
                def test_waits(self):
                    self.addCleanup(Path({str(cleaned)!r}).write_text, "cleaned")
                    Path({str(started)!r}).write_text(str(os.getpid()))
                    time.sleep(600)
            """)
        # Started with Ctrl-C ignored, as `tests/run.py &` in a script starts it: the runner must restore it.
        ignoring = ("import runpy, signal, sys; signal.signal(signal.SIGINT, signal.SIG_IGN); "
                    "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
        runner = subprocess.Popen([sys.executable, "-c", ignoring, *self.command()[1:]], text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(runner.stdout), daemon=True)
        reader.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (started.exists()
                                                   and any("] test_fails_first:" in line for line in lines)):
            time.sleep(0.05)
        worker = int(started.read_text())
        self.addCleanup(self.kill_if_running, worker)  # should the runner leave it behind
        runner.send_signal(how)
        runner.wait(timeout=30)
        deadline = time.monotonic() + 30  # once its runner is gone, a worker stops by itself
        while self.running(worker) and time.monotonic() < deadline:
            time.sleep(0.05)
        reader.join(timeout=30)
        runner.stdout.close()
        return runner.returncode, "".join(lines), worker, cleaned.exists()

    def running(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_ctrl_c_stops_the_units_still_running_after_their_cleanups(self):
        code, output, worker, cleaned = self.interrupted(signal.SIGINT)
        self.assertEqual(code, 130, output)
        self.assertIn("Interrupted", output)
        self.assertIn("the failure before the interrupt", output)  # what had failed is still shown
        self.assertFalse(self.running(worker))
        self.assertTrue(cleaned)  # the interrupted test still ran its cleanups

    def test_a_terminated_run_stops_its_units_as_ctrl_c_does(self):
        code, output, worker, cleaned = self.interrupted(signal.SIGTERM)
        self.assertEqual(code, 130, output)
        self.assertIn("the failure before the interrupt", output)
        self.assertFalse(self.running(worker))
        self.assertTrue(cleaned)

    def test_a_unit_whose_runner_is_killed_stops_by_itself_after_its_cleanups(self):
        code, output, worker, cleaned = self.interrupted(signal.SIGKILL)
        self.assertEqual(code, -signal.SIGKILL)
        self.assertFalse(self.running(worker))
        self.assertTrue(cleaned)

    def wait_for(self, path, seconds=30):
        deadline = time.monotonic() + seconds
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return path.exists()

    def stopped_once(self, started, *args):
        """Run the runner until a test writes `started`, then press Ctrl-C; return its exit status and output."""
        runner = subprocess.Popen(self.command(*args), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        self.assertTrue(self.wait_for(started))
        runner.send_signal(signal.SIGINT)
        output = runner.communicate(timeout=60)[0]
        return runner.returncode, output

    def test_a_stop_is_not_swallowed_by_a_broad_except_and_cleanups_still_run(self):
        started, cleaned = self.work / "started", self.work / "cleaned"
        self.module("test_swallows", f"""
            import time, unittest
            from pathlib import Path
            class Swallows(unittest.TestCase):
                def test_retries_whatever_goes_wrong(self):
                    self.addCleanup(Path({str(cleaned)!r}).write_text, "cleaned")
                    Path({str(started)!r}).write_text("started")
                    while True:
                        try:
                            time.sleep(600)
                        except Exception:
                            pass
            """)
        code, output = self.stopped_once(started, "--grace", "20")
        self.assertEqual(code, 130, output)
        self.assertTrue(cleaned.exists(), output)  # swallowed, it would have been killed after 20 s, uncleaned

    def test_a_stop_during_a_cleanup_lets_that_cleanup_finish(self):
        cleaning, cleaned = self.work / "cleaning", self.work / "cleaned"
        self.module("test_slow_cleanup", f"""
            import time, unittest
            from pathlib import Path
            def slow_cleanup():
                Path({str(cleaning)!r}).write_text("cleaning")
                time.sleep(2)
                Path({str(cleaned)!r}).write_text("cleaned")
            class SlowCleanup(unittest.TestCase):
                def test_quick(self):
                    self.addCleanup(slow_cleanup)
            """)
        runner = subprocess.Popen(self.command(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        self.assertTrue(self.wait_for(cleaning))
        runner.send_signal(signal.SIGINT)
        output = runner.communicate(timeout=30)[0]
        self.assertEqual(runner.returncode, 130, output)
        self.assertTrue(cleaned.exists(), output)  # the stop waited for the cleanup instead of cutting it short

    @unittest.skipUnless(shutil.which("tmux"), "tmux required")
    def test_what_a_killed_unit_tracked_outside_its_process_group_is_ended(self):
        socket, started = self.work / "tmux.sock", self.work / "started"
        self.module("test_leaves", f"""
            import signal, subprocess, sys, time, unittest
            from pathlib import Path
            from isolation import track
            class Leaves(unittest.TestCase):
                def test_starts_a_server_and_a_daemon(self):
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # so no cleanup of its own ever runs
                    subprocess.run(["tmux", "-S", {str(socket)!r}, "new-session", "-d", "sleep 600"], check=True)
                    track(tmux_socket={str(socket)!r})
                    daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", {str(started)!r}],
                                              start_new_session=True)
                    track(pid=daemon.pid)
                    Path({str(started)!r}).write_text(str(daemon.pid))
                    time.sleep(600)
            """)
        self.addCleanup(subprocess.run, ["tmux", "-S", str(socket), "kill-server"], capture_output=True)
        code, output = self.stopped_once(started, "--grace", "1")  # its cleanups never run: it ignores the stop
        daemon = int(started.read_text())
        self.addCleanup(self.kill_if_running, daemon)
        self.assertEqual(code, 130, output)
        tmux = subprocess.run(["tmux", "-S", str(socket), "list-sessions"], capture_output=True)
        self.assertNotEqual(tmux.returncode, 0)  # the server is gone
        deadline = time.monotonic() + 10
        while self.alive(daemon) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.alive(daemon))

    @staticmethod
    def alive(pid):
        """Running and not a zombie waiting to be reaped."""
        from multiverse.runtime.process import process_snapshot
        snapshot = process_snapshot(pid)
        return snapshot is not None and not snapshot[1].startswith("Z")

    def timed_module(self, name, log):
        """A module of two tests that each log when they start and end, 0.3 s apart."""
        self.module(name, f"""
            import time, unittest
            class Timed(unittest.TestCase):
                def log(self, event):
                    with open({str(log)!r}, "a") as out:
                        out.write(f"{{time.monotonic()}} {{event}} {{self.id()}}\\n")
                def test_one(self): self.log("start"); time.sleep(0.3); self.log("end")
                def test_two(self): self.log("start"); time.sleep(0.3); self.log("end")
            """)

    def intervals(self, log):
        events = [line.split() for line in log.read_text().splitlines()]
        starts = {test: float(at) for at, event, test in events if event == "start"}
        return sorted((starts[test], float(at)) for at, event, test in events if event == "end")

    def test_runs_at_once_share_the_machines_slots(self):
        log = self.work / "times.log"
        self.timed_module("test_first", log)
        self.timed_module("test_second", log)
        # Two runs at once, each asking for two units at a time, on a machine with one slot: one unit at a time.
        runs = [subprocess.Popen(self.command("-j", "2", "--budget", "1", name), text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT) for name in ("test_first", "test_second")]
        outputs = [run.communicate(timeout=60)[0] for run in runs]
        self.assertEqual([run.returncode for run in runs], [0, 0], outputs)
        intervals = self.intervals(log)
        self.assertEqual(len(intervals), 4)
        for (_, end), (start, _) in zip(intervals, intervals[1:]):
            self.assertLessEqual(end, start, intervals)

    def test_a_run_waits_while_the_slots_are_taken_and_says_so(self):
        log = self.work / "times.log"
        self.timed_module("test_first", log)
        self.slots.mkdir(mode=0o700)
        held = (self.slots / "slot-0").open("a")  # another run holding the machine's only slot
        fcntl.flock(held, fcntl.LOCK_EX)
        run = subprocess.Popen(self.command("--budget", "1"), text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
        self.addCleanup(lambda: run.poll() is None and run.kill())
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(run.stdout), daemon=True)
        reader.start()
        waiting = "Waiting for a slot: other runs of the suite are using some of this machine's 1.\n"
        deadline = time.monotonic() + 30
        while waiting not in lines and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(waiting, lines)
        self.assertFalse(log.exists())  # nothing has started while the slot is held
        held.close()
        run.wait(timeout=60)
        reader.join(timeout=30)
        run.stdout.close()
        self.assertEqual(run.returncode, 0, "".join(lines))
        self.assertEqual(len(self.intervals(log)), 2)

    def test_a_slots_directory_others_could_change_is_refused(self):
        self.module("test_fine", """
            import unittest
            class Fine(unittest.TestCase):
                def test_fine(self): pass
            """)
        self.slots.mkdir(mode=0o700)
        os.chmod(self.slots, 0o777)  # anyone could take or swap the slots
        result = self.run_tests()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"{self.slots} must be yours alone (owner {os.getuid()}, mode 700), but has owner "
                      f"{os.getuid()} and mode 777", result.stderr)
        self.assertNotIn("units on", result.stdout)  # nothing ran
        self.slots.rmdir()
        elsewhere = self.work / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.slots.symlink_to(elsewhere)  # a link could point anywhere
        result = self.run_tests()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"{self.slots} must be a directory, not a link or a file", result.stderr)
        self.slots.unlink()
        result = self.run_tests()  # made fresh, as only this user can use it
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE(os.lstat(self.slots).st_mode), 0o700)

    def test_an_import_failure_counts_as_discovery_counts_it(self):
        self.module("test_skipped_module", """
            import unittest
            raise unittest.SkipTest("not on this machine")
            """)
        self.module("test_broken_module", """
            raise RuntimeError("not an ImportError")
            """)
        result = self.run_tests()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        skipped = [line for line in result.stdout.splitlines() if "] test_skipped_module: 1 tests in" in line]
        self.assertEqual(len(skipped), 1, result.stdout)
        self.assertNotIn("FAILED", skipped[0])  # a skipped module, not a failed one
        self.assertIn("RuntimeError: not an ImportError", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "FAILED (errors=1, skipped=1)")
        discovered = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(self.tests)],
                                    text=True, capture_output=True, timeout=60)
        self.assertEqual(discovered.stderr.splitlines()[-1], "FAILED (errors=1, skipped=1)")

    def test_a_stopped_async_test_still_runs_its_cleanups(self):
        started, cleaned = self.work / "started", self.work / "cleaned"
        self.module("test_async_waits", f"""
            import asyncio, unittest
            from pathlib import Path
            class AsyncWaits(unittest.IsolatedAsyncioTestCase):
                async def test_awaits(self):
                    self.addCleanup(Path({str(cleaned)!r}).write_text, "cleaned")
                    Path({str(started)!r}).write_text("started")
                    await asyncio.sleep(600)
            """)
        code, output = self.stopped_once(started, "--grace", "20")
        self.assertEqual(code, 130, output)
        self.assertTrue(cleaned.exists(), output)

    def test_ending_what_a_unit_tracked_spares_reused_pids_zombies_and_damaged_lines(self):
        sys.path.insert(0, str(RUNNER.parent))
        self.addCleanup(sys.path.remove, str(RUNNER.parent))
        from run import Run
        from multiverse.runtime.process import process_started
        tracked = self.work / "tracked.jsonl"
        survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", str(self.work)],
                                    start_new_session=True)
        self.addCleanup(survivor.wait)
        self.addCleanup(survivor.kill)
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(zombie.wait)
        deadline = time.monotonic() + 10
        while not self.zombie(zombie.pid) and time.monotonic() < deadline:  # exited, not reaped yet
            time.sleep(0.05)
        self.assertTrue(self.zombie(zombie.pid))
        lines = [json.dumps({"pid": survivor.pid, "started": "Thu Jan  1 00:00:00 1970"}),  # the PID taken since
                 json.dumps({"pid": zombie.pid, "started": process_started(zombie.pid)}),
                 json.dumps({"tmux_socket": str(self.work / "no-server.sock")}),  # its server is gone already
                 '{"pid": 1, "start']  # cut short when its unit was killed
        (self.work / "no-server.sock").write_text("")
        tracked.write_text("\n".join(lines))
        self.assertEqual(Run.end_tracked(tracked), [])
        self.assertIsNone(survivor.poll())  # spared: not the process that was tracked
        self.assertEqual(Run.end_tracked(self.work / "missing.jsonl"), [])
        tracked.write_text(json.dumps({"pid": survivor.pid, "started": process_started(survivor.pid)}) + "\n")
        self.assertEqual(Run.end_tracked(tracked), [f"process {survivor.pid}"])
        self.assertEqual(survivor.wait(timeout=10), -signal.SIGKILL)

    @staticmethod
    def zombie(pid):
        from multiverse.runtime.process import process_snapshot
        snapshot = process_snapshot(pid)
        return snapshot is not None and snapshot[1].startswith("Z")

    def kill_if_running(self, pid):
        """Kill `pid` if it is still one this test started (never a process that took the number since)."""
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], text=True, capture_output=True).stdout
        if str(self.work) in command:
            os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
