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

    def test_a_failure_or_error_shows_the_tests_own_lines(self):
        self.module("test_shown", """
            import unittest
            class Shown(unittest.TestCase):
                def test_failing(self):
                    self.assertEqual(1, 2, "the planted failure")
                def test_erring(self):
                    {}["the planted error"]
                def setUp(self):
                    if self._testMethodName == "test_never":
                        raise ValueError("the planted setUp error")
                def test_never(self): pass
            """)
        result = self.run_tests("test_shown")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        shown = str(self.tests / "test_shown.py")
        self.assertIn(f'File "{shown}", line 5, in test_failing', result.stdout)
        self.assertIn(f'File "{shown}", line 7, in test_erring', result.stdout)
        self.assertIn(f'File "{shown}", line 10, in setUp', result.stdout)
        self.assertNotIn("in marked", result.stdout)  # the runner's own frames are not the test's

    def test_a_test_that_patches_time_sleep_sees_only_its_own_calls(self):
        self.module("test_patched_sleep", """
            import threading, time, unittest
            from unittest import mock
            class PatchedSleep(unittest.TestCase):
                def test_fake_clock(self):
                    calls = []
                    with mock.patch.object(time, "sleep", side_effect=lambda seconds: calls.append(seconds)):
                        threading.Event().wait(1.5)  # long enough for any thread of the runner's to sleep
                    self.assertEqual(calls, [], "a thread of the runner's slept in this test's fake time.sleep")
            """)
        result = self.run_tests("test_patched_sleep")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (self.pid_in(started)
                                                   and any("] test_fails_first:" in line for line in lines)):
            time.sleep(0.05)
        worker = self.pid_in(started)
        self.assertTrue(worker, "".join(lines))  # the waiting test wrote its pid
        self.addCleanup(self.kill_if_running, worker)  # should the runner leave it behind
        runner.send_signal(how)
        runner.wait(timeout=30)
        deadline = time.monotonic() + 30  # once its runner is gone, a worker stops by itself
        while self.running(worker) and time.monotonic() < deadline:
            time.sleep(0.05)
        reader.join(timeout=30)
        runner.stdout.close()
        return runner.returncode, "".join(lines), worker, cleaned.exists()

    @staticmethod
    def pid_in(path):
        """The pid a test wrote to `path`, or None while it has not (yet): the file exists, empty, before it is."""
        try:
            text = path.read_text().strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

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

    def stopped_once(self, started, *args, seconds=30):
        """Run the runner until a test writes `started` (within `seconds`), then press Ctrl-C; return its exit status
        and output."""
        runner = subprocess.Popen(self.command(*args), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        self.assertTrue(self.wait_for(started, seconds))
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
        daemon = self.pid_in(started)
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

    def waiting_line(self, holder_environment):
        """Hold the machine's only slot with a run started with `holder_environment`, start a second run, and return
        the second run's first line about waiting, the holder's pid, and whether every slot file was left empty."""
        started = self.work / "started"
        self.module("test_holds", f"""
            import os, time, unittest
            from pathlib import Path
            class Holds(unittest.TestCase):
                def test_holds(self):
                    Path({str(started)!r}).write_text(str(os.getpid()))
                    while not Path({str(started)!r} + ".release").exists():
                        time.sleep(0.05)
            """)
        log = self.work / "times.log"
        self.timed_module("test_first", log)
        holder = subprocess.Popen(self.command("--budget", "1", "test_holds"), cwd=self.work, env=holder_environment,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.assertTrue(self.wait_for_pid(started))
        waiter = subprocess.Popen(self.command("--budget", "1", "test_first"), text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
        self.addCleanup(lambda: waiter.poll() is None and waiter.kill())
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(waiter.stdout), daemon=True)
        reader.start()
        deadline = time.monotonic() + 30
        while not any(line.startswith("Waiting for a slot") for line in lines) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(log.exists())  # nothing has started while the slot is held
        Path(str(started) + ".release").write_text("")
        self.assertEqual(holder.wait(timeout=60), 0)
        self.assertEqual(waiter.wait(timeout=60), 0, "".join(lines))
        reader.join(timeout=30)
        waiter.stdout.close()
        waiting = [line for line in lines if line.startswith("Waiting for a slot")]
        self.assertEqual(len(waiting), 1, lines)  # said once, not again within half a minute
        empty = all(path.read_text() == "" for path in self.slots.glob("slot-*"))
        return waiting[0], holder.pid, empty

    def wait_for_pid(self, path, seconds=60):
        deadline = time.monotonic() + seconds
        while not self.pid_in(path) and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.pid_in(path)

    def test_a_waiting_run_names_the_agent_whose_run_holds_the_slots(self):
        agent = self.work / "agent"
        agent.mkdir()
        (agent / "properties.json").write_text(json.dumps({"id": "a1b2c3", "name": "holder-agent", "task": "x"}))
        # As AGENTS.md runs the tests: MV2_AGENT_ID unset, the agent's directory still known.
        line, pid, empty = self.waiting_line({**os.environ, "MV2_AGENT_DIR": str(agent)})
        self.assertEqual(line, f"Waiting for a slot: held by holder-agent (1 slot, pid {pid}, {self.work}).\n")
        self.assertTrue(empty)  # each run cleared what it wrote when it gave its slot back

    def test_a_waiting_run_says_when_the_holder_is_not_an_agent(self):
        line, pid, empty = self.waiting_line(dict(os.environ))
        self.assertEqual(line, f"Waiting for a slot: held by not an agent (1 slot, pid {pid}, {self.work}).\n")
        self.assertTrue(empty)

    def test_a_record_of_a_run_that_is_gone_is_not_named(self):
        from multiverse.runtime.process import process_started
        log = self.work / "times.log"
        self.timed_module("test_first", log)
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        record = {"pid": gone.pid, "started": process_started(gone.pid), "cwd": "/gone", "agent": "gone-agent"}
        gone.wait()
        self.slots.mkdir(mode=0o700)
        held = (self.slots / "slot-0").open("a")  # a run that does not say who it is (an older tests/run.py)
        fcntl.flock(held, fcntl.LOCK_EX)
        held.write(json.dumps(record) + "\n")  # left by a run that was killed
        held.flush()
        waiter = subprocess.Popen(self.command("--budget", "1", "test_first"), text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
        self.addCleanup(lambda: waiter.poll() is None and waiter.kill())
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(waiter.stdout), daemon=True)
        reader.start()
        deadline = time.monotonic() + 30
        while not any(line.startswith("Waiting for a slot") for line in lines) and time.monotonic() < deadline:
            time.sleep(0.05)
        held.close()
        self.assertEqual(waiter.wait(timeout=60), 0, "".join(lines))
        reader.join(timeout=30)
        waiter.stdout.close()
        self.assertIn("Waiting for a slot: held by 1 slot of a run that doesn't say who it is (an older tests/run.py)."
                      "\n", lines)
        self.assertNotIn("gone-agent", "".join(lines))

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
                 json.dumps({"group": [1], "member_of": {"a": 1}, "tmux_socket": [2], "pid": [3], "started": 4}),
                 "[]",
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

    @unittest.skipUnless(shutil.which("tmux"), "tmux required")
    def test_a_tracked_group_whose_leader_exited_first_still_loses_its_helpers(self):
        ready, helpers, leader = self.work / "ready", self.work / "helpers", self.work / "leader.py"
        leader.write_text(textwrap.dedent(f"""
            import subprocess, sys, time
            from pathlib import Path
            spawn = lambda: subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)",
                                              {str(helpers)!r}]).pid
            first = spawn()  # a helper before the group is tracked
            Path({str(ready)!r}).write_text(str(first))
            while not Path({str(ready)!r} + ".go").exists():
                time.sleep(0.02)
            second = spawn()  # and one after, as a daemon's late git or registry helper
            Path({str(helpers)!r}).write_text(f"{{first}} {{second}}")
            """))  # then the leader exits first, leaving both helpers in its group
        self.module("test_group", f"""
            import signal, subprocess, sys, time, unittest
            from pathlib import Path
            from isolation import TRACKED, track
            class Group(unittest.TestCase):
                def test_daemon_group(self):
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # so no cleanup of its own runs
                    leader = subprocess.Popen([sys.executable, {str(leader)!r}], start_new_session=True)
                    while not Path({str(ready)!r}).exists():
                        time.sleep(0.02)
                    track(pid=leader.pid)
                    Path({str(ready)!r} + ".go").write_text("")
                    leader.wait()  # exited and reaped
                    helpers = Path({str(helpers)!r}).read_text().split()
                    deadline = time.monotonic() + 60  # until the worker has seen both in the group, however loaded
                    while time.monotonic() < deadline:
                        seen = Path(TRACKED).read_text()
                        if all(f'"pid": {{pid}},' in seen for pid in helpers):
                            break
                        time.sleep(0.05)
                    Path({str(ready)!r} + ".done").write_text("")
                    time.sleep(600)
            """)
        code, output = self.stopped_once(self.work / "ready.done", "--grace", "1", seconds=90)
        first, second = (int(pid) for pid in helpers.read_text().split())
        for pid in (first, second):
            self.addCleanup(self.kill_if_running, pid)
        self.assertEqual(code, 130, output)
        self.assertTrue(self.gone_soon(first), "the helper started before the group was tracked")
        self.assertTrue(self.gone_soon(second), "the helper started after it")

    def orphaned(self, body, cleanup):
        """Start a run whose test tracks a detached helper, registers `cleanup` and runs `body`, and kill the runner
        outright once the test writes MARKER; return the worker's and the helper's PIDs."""
        marker, helper_file = self.work / "marker", self.work / "helper"
        cleanup = textwrap.indent(textwrap.dedent(cleanup).strip(), " " * 24)
        body = textwrap.indent(textwrap.dedent(body).strip(), " " * 20)
        self.module("test_orphan", f"""
            import os, signal, subprocess, sys, time, unittest
            from pathlib import Path
            from isolation import track
            MARKER = {str(marker)!r}
            class Orphan(unittest.TestCase):
                def test_orphaned(self):
                    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", MARKER],
                                              start_new_session=True)
                    track(pid=helper.pid)
                    Path({str(helper_file)!r}).write_text(f"{{os.getpid()}} {{helper.pid}}")
                    def cleanup():
{cleanup}
                    self.addCleanup(cleanup)
{body}
            """)
        runner = subprocess.Popen(self.command("--grace", "1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        self.assertTrue(self.wait_for(marker))
        worker, helper = (int(pid) for pid in helper_file.read_text().split())
        for pid in (worker, helper):
            self.addCleanup(self.kill_if_running, pid)
        runner.kill()
        runner.wait()
        return worker, helper

    def gone_soon(self, *pids, seconds=30):
        deadline = time.monotonic() + seconds
        while any(self.alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not any(self.alive(pid) for pid in pids)

    def test_a_worker_whose_runner_died_during_a_stuck_cleanup_ends_within_its_grace(self):
        worker, helper = self.orphaned(body="pass", cleanup="""
            Path(MARKER).write_text("cleaning")
            time.sleep(600)
            """)
        self.assertTrue(self.gone_soon(worker, helper), "the worker or its tracked helper outlived the grace")

    def test_a_worker_whose_runner_died_while_its_test_ignores_sigterm_ends_within_its_grace(self):
        worker, helper = self.orphaned(body="""
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(MARKER).write_text("ignoring")
            time.sleep(600)
            """, cleanup="pass")
        self.assertTrue(self.gone_soon(worker, helper), "the worker or its tracked helper outlived the grace")

    def test_a_dotted_selection_counts_import_failures_as_discovery_does(self):
        self.module("test_skipped_module", """
            import unittest
            raise unittest.SkipTest("not on this machine")
            """)
        self.module("test_broken_module", """
            raise RuntimeError("not an ImportError")
            """)
        result = self.run_tests("test_skipped_module.Some", "test_broken_module.Missing.test_x")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("RuntimeError: not an ImportError", result.stdout)
        self.assertNotIn("units without a result", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "FAILED (errors=1, skipped=1)")

    def test_killing_a_unit_whose_group_holds_only_a_zombie_is_not_an_error(self):
        sys.path.insert(0, str(RUNNER.parent))
        self.addCleanup(sys.path.remove, str(RUNNER.parent))
        from run import Run
        unit = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        deadline = time.monotonic() + 10
        while not self.zombie(unit.pid) and time.monotonic() < deadline:  # exited, alone in its group, unreaped
            time.sleep(0.05)
        self.assertTrue(self.zombie(unit.pid))
        Run.kill(unit)  # macOS refuses killpg here (EPERM): nothing is left to kill, and it reaps the unit
        self.assertEqual(unit.returncode, 0)

    def package(self, name, init="", modules=()):
        """A package directory under tests/, with its __init__.py and (name, text) modules."""
        directory = self.tests.joinpath(*name.split("."))
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text(textwrap.dedent(init))
        for module, text in modules:
            self.module(module, text, directory)
        return directory

    def test_packages_are_found_as_unittest_discover_finds_them(self):
        self.module("recording", RECORDING)
        recorded = """
            from recording import Recorded
            class Case(Recorded):
                def test_{0}(self): self.record()
            """
        self.module("test_top", recorded.format("top"))
        self.package("pkg", modules=[("test_inner", recorded.format("inner")), ("helpers", "x = 1\n")])
        self.package("pkg.deeper", modules=[("test_deep", recorded.format("deep"))])
        # Its own tests, in its __init__.py, and its modules too: it has no load_tests.
        self.package("withinit", init=recorded.format("init"), modules=[("test_mod", recorded.format("mod"))])
        # A load_tests in __init__.py loads the package's tests itself: discovery goes no deeper.
        self.package("loaded", init="""
            def load_tests(loader, tests, pattern):
                return loader.loadTestsFromName("loaded.test_one")
            """, modules=[("test_one", recorded.format("one")),
                          ("test_two", "import unittest\nclass Never(unittest.TestCase):\n"
                                       "    def test_never(self): self.fail('discovery never loads this')\n")])
        notpkg = self.tests / "notpkg"  # no __init__.py: not a package, so not searched
        notpkg.mkdir()
        self.module("test_ignored", "import unittest\nclass Never(unittest.TestCase):\n"
                                    "    def test_never(self): self.fail('discovery never loads this')\n", notpkg)
        discovered = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(self.tests)],
                                    text=True, capture_output=True, timeout=60)
        self.assertEqual(discovered.returncode, 0, discovered.stderr)
        expected = self.recorded()
        self.assertEqual(len(expected), 6, expected)
        self.record.unlink()
        result = self.run_tests()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), expected)
        listed = subprocess.run(self.command("--list"), text=True, capture_output=True, timeout=60)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.split(), ["loaded", "pkg.deeper.test_deep", "pkg.test_inner", "test_top",
                                                 "withinit", "withinit.test_mod"])
        # A package by name runs its modules; a module by its path inside a package runs that module.
        self.record.unlink()
        result = self.run_tests("pkg")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), ["pkg.deeper.test_deep.Case.test_deep", "pkg.test_inner.Case.test_inner"])
        self.record.unlink()
        result = self.run_tests(str(self.tests / "pkg" / "deeper" / "test_deep.py"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), ["pkg.deeper.test_deep.Case.test_deep"])

    def test_a_dotted_selection_in_a_package_counts_import_failures_as_discovery_does(self):
        self.package("pkg_skip", modules=[("test_skip", """
            import unittest
            raise unittest.SkipTest("not on this machine")
            """)])
        self.package("pkg_broken", modules=[("test_broken", """
            raise RuntimeError("not an ImportError")
            """)])
        result = self.run_tests("pkg_skip.test_skip.Some", "pkg_broken.test_broken.Missing.test_x")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("RuntimeError: not an ImportError", result.stdout)
        self.assertNotIn("units without a result", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "FAILED (errors=1, skipped=1)")

    def test_a_group_tracked_and_left_before_the_runners_first_look_still_loses_its_helper(self):
        # The leader exits and the unit ends well within the worker's first periodic look (reviewer's probe).
        helper_file, release, leader = self.work / "helper", self.work / "release", self.work / "leader.py"
        leader.write_text(textwrap.dedent(f"""
            import subprocess, sys, time
            from pathlib import Path
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", {str(self.work)!r}])
            Path({str(helper_file)!r}).write_text(str(child.pid))
            while not Path({str(release)!r}).exists():
                time.sleep(0.005)
            """))
        self.module("test_fast", f"""
            import subprocess, sys, time, unittest
            from pathlib import Path
            from isolation import track
            class Fast(unittest.TestCase):
                def test_group(self):
                    leader = subprocess.Popen([sys.executable, {str(leader)!r}], start_new_session=True)
                    while not Path({str(helper_file)!r}).exists():
                        time.sleep(0.005)
                    track(pid=leader.pid)
                    Path({str(release)!r}).touch()
                    leader.wait()
            """)
        result = self.run_tests("test_fast")
        helper = self.pid_in(helper_file)
        self.addCleanup(self.kill_if_running, helper)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.gone_soon(helper), "the helper outlived the unit that tracked its group")

    def test_a_group_is_sampled_only_while_a_process_already_recorded_in_it_is_there(self):
        sys.path.insert(0, str(RUNNER.parent))
        self.addCleanup(sys.path.remove, str(RUNNER.parent))
        import tracking
        from multiverse.runtime.process import process_started
        child = "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)', sys.argv[1]]); "
        leader = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; " + child + "time.sleep(600)",
                                   str(self.work)], start_new_session=True)
        self.addCleanup(lambda: self.kill_group(leader))
        deadline = time.monotonic() + 30
        while len([pid for pid, group, _, _ in tracking.processes() if group == leader.pid]) < 2:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        tracked = self.work / "tracked.jsonl"
        # A record of a leader with another start time: the group with its number is someone else's.
        tracked.write_text(json.dumps({"pid": leader.pid, "started": "Thu Jan  1 00:00:00 1970",
                                       "group": leader.pid}) + "\n")
        tracking.sample(tracked)
        self.assertEqual(len(tracked.read_text().splitlines()), 1)  # nothing recorded
        tracked.write_text(json.dumps({"pid": leader.pid, "started": process_started(leader.pid),
                                       "group": leader.pid}) + "\n")
        tracking.sample(tracked)
        tracking.sample(tracked)  # once each
        members = [json.loads(line) for line in tracked.read_text().splitlines()[1:]]
        self.assertEqual([member["member_of"] for member in members], [leader.pid])  # its child; the leader is known
        self.assertNotEqual(members[0]["pid"], leader.pid)

    @staticmethod
    def kill_group(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()

    def kill_if_running(self, pid):
        """Kill `pid` if it is still one this test started (never a process that took the number since)."""
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], text=True, capture_output=True).stdout
        if str(self.work) in command:
            os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
