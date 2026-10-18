"""tests/run.py, the parallel runner, on small test directories of its own: it runs what discovery runs, once
each; a failure, an import error, a hang or a write under a guarded home fails the run; a unit it stops still
cleans up and leaves nothing running; and runs at once share the machine's slots."""
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

NEVER = "import unittest\nclass Never(unittest.TestCase):\n    def test_never(self): self.fail('never loaded')\n"


class RunnerTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        temporary = tempfile.TemporaryDirectory(prefix="mv2-runner-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.slots = self.work / "slots"  # never the machine's: these runners must not wait for the suite's
        self.fresh("main")

    def fresh(self, name):
        """A directory of its own for one case of a table: its tests, record and marker files."""
        self.row = self.work / name
        self.tests = self.row / "tests"
        self.tests.mkdir(parents=True)
        self.record = self.row / "record.txt"
        os.environ["RECORD"] = str(self.record)
        return self.row

    def module(self, name, text, where=None):
        (where or self.tests).joinpath(f"{name}.py").write_text(textwrap.dedent(text))

    def package(self, name, init="", modules=()):
        """A package directory under tests/, with its __init__.py and (name, text) modules."""
        directory = self.tests.joinpath(*name.split("."))
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text(textwrap.dedent(init))
        for module, text in modules:
            self.module(module, text, directory)
        return directory

    def command(self, *args):
        return [sys.executable, str(RUNNER), "-s", str(self.tests), "--slots-directory", str(self.slots), *args]

    def run_tests(self, *args, timeout=120):
        return subprocess.run(self.command("-j", "3", *args), text=True, capture_output=True, timeout=timeout)

    def recorded(self):
        return sorted(self.record.read_text().split()) if self.record.exists() else []

    def discovered(self):
        return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(self.tests)], text=True,
                              capture_output=True, timeout=60)

    def test_it_runs_exactly_what_unittest_discover_runs(self):
        """Runs what `unittest discover` runs, once each, through load_tests, packages and shards; a test it dropped or
        ran twice would still leave the run green, so the loss would go unnoticed."""
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
        # A fixture class imported from another module, its own tests chosen by load_tests
        # (as test_agent_identity_lifecycle does).
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
        extra = self.row / "extra"
        extra.mkdir()
        self.module("test_extra", """
            from recording import Recorded
            class Extra(Recorded):
                def test_seven(self): self.record()
            """, extra)
        self.module("test_gamma", f"""
            def load_tests(loader, tests, pattern):
                top = loader._top_level_dir
                tests.addTests(loader.discover({str(extra)!r}, top_level_dir={str(extra)!r}))
                loader._top_level_dir = top  # as test_plugin_checkouts must on Python 3.11
                return tests
            """)
        recorded = """
            from recording import Recorded
            class Case(Recorded):
                def test_{0}(self): self.record()
            """
        self.package("pkg", modules=[("test_inner", recorded.format("inner")), ("helpers", "x = 1\n")])
        self.package("pkg.deeper", modules=[("test_deep", recorded.format("deep"))])
        # Its own tests, in its __init__.py, and its modules too: it has no load_tests.
        self.package("withinit", init=recorded.format("init"), modules=[("test_mod", recorded.format("mod"))])
        # A load_tests in __init__.py loads the package's tests itself: discovery goes no deeper.
        self.package("loaded", init="""
            def load_tests(loader, tests, pattern):
                return loader.loadTestsFromName("loaded.test_one")
            """, modules=[("test_one", recorded.format("one")), ("test_two", NEVER)])
        notpkg = self.tests / "notpkg"  # no __init__.py: not a package, so not searched
        notpkg.mkdir()
        self.module("test_ignored", NEVER, notpkg)
        discovered = self.discovered()
        self.assertEqual(discovered.returncode, 0, discovered.stderr)
        expected = self.recorded()
        self.assertEqual(len(expected), 12, expected)
        self.record.unlink()
        # A long recorded time splits test_alpha into shards.
        (self.row / ".run").mkdir()
        (self.row / ".run/test-durations.json").write_text(json.dumps({"modules": {"test_alpha": 100}}))
        result = self.run_tests()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), expected)
        self.assertIn("test_alpha (1 of 5)", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "OK")
        modules = ["loaded", "pkg.deeper.test_deep", "pkg.test_inner", "test_alpha", "test_beta", "test_gamma",
                   "withinit", "withinit.test_mod"]
        # This run's times replace the record, so the next run plans with them.
        times = json.loads((self.row / ".run/test-durations.json").read_text())["modules"]
        self.assertEqual(sorted(times), modules)
        self.assertLess(times["test_alpha"], 100)
        listed = subprocess.run(self.command("--list"), text=True, capture_output=True, timeout=60)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.split(), modules)
        # A package by name runs its modules; a module by its path inside a package runs that module.
        self.record.unlink()
        result = self.run_tests("pkg")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), ["pkg.deeper.test_deep.Case.test_deep", "pkg.test_inner.Case.test_inner"])
        self.record.unlink()
        result = self.run_tests(str(self.tests / "pkg" / "deeper" / "test_deep.py"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded(), ["pkg.deeper.test_deep.Case.test_deep"])

    def test_a_module_that_fails_to_import_counts_as_discover_counts_it(self):
        """A module that raises SkipTest or another error as it is imported, found or named, by module or by a dotted
        name, in a package or not, is a skipped or failing test as discovery makes it; one that vanished or crashed
        its worker would leave no trace of the tests it held."""
        skipping = "import unittest\nraise unittest.SkipTest('not on this machine')\n"
        breaking = "raise RuntimeError('not an ImportError')\n"
        for case, names in (("found by discovery", ()),
                            ("named by a dotted name", ("test_skipped.Some", "test_broken.Missing.test_x")),
                            ("named by a dotted name in a package",
                             ("pkg.test_skipped.Some", "pkg.test_broken.Missing.test_x"))):
            with self.subTest(case):
                self.fresh(case.replace(" ", "-"))
                if names and names[0].startswith("pkg."):
                    self.package("pkg", modules=[("test_skipped", skipping), ("test_broken", breaking)])
                else:
                    self.module("test_skipped", skipping)
                    self.module("test_broken", breaking)
                result = self.run_tests(*names)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("RuntimeError: not an ImportError", result.stdout)
                self.assertNotIn("units without a result", result.stdout)  # no worker crashed without a report
                self.assertEqual(result.stdout.splitlines()[-1], "FAILED (errors=1, skipped=1)")
                if not names:  # as discovery itself counts them
                    self.assertEqual(self.discovered().stderr.splitlines()[-1], "FAILED (errors=1, skipped=1)")

    def test_a_failure_an_error_or_a_hang_fails_the_run_and_shows_the_tests_own_lines(self):
        """A failing test, an error, an import error or a hang fails the run, and the report shows each failing test's
        own file and line; a run that exits 0, or a report that loses the line (as the runner's own wrapper once made
        it, 3c77b76), hides or misplaces failures."""
        failing = """
            import unittest
            class Fails(unittest.TestCase):
                def setUp(self):
                    if self._testMethodName == "test_set_up":
                        raise ValueError("the planted setUp error")
                def test_wrong(self):
                    self.assertEqual(1, 2, "the planted failure")
                def test_erring(self):
                    {}["the planted error"]
                def test_set_up(self): pass
                def test_right(self): pass
            """
        self.module("test_fails", failing)
        self.module("test_broken", """
            raise ImportError("the planted import error")
            """)
        cleaned = self.row / "cleaned"
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
        shown, lines = str(self.tests / "test_fails.py"), textwrap.dedent(failing).splitlines()
        for statement, where in (('raise ValueError("the planted setUp error")', "setUp"),
                                 ('self.assertEqual(1, 2, "the planted failure")', "test_wrong"),
                                 ('{}["the planted error"]', "test_erring")):
            line = next(number for number, text in enumerate(lines, 1) if statement in text)
            self.assertIn(f'File "{shown}", line {line}, in {where}', result.stdout)
        self.assertNotIn("in marked", result.stdout)  # the runner's own frames are not the test's
        self.assertIn("the planted import error", result.stdout)
        self.assertIn("stopped after 10 s", result.stdout)
        self.assertIn("Stopped: stopped by tests/run.py", result.stdout)
        self.assertTrue(cleaned.exists())  # the hung test still ran its cleanups
        fine = [line for line in result.stdout.splitlines() if "] test_fine: 1 tests in" in line]
        self.assertEqual(len(fine), 1, result.stdout)
        self.assertNotIn("FAILED", fine[0])
        self.assertEqual(result.stdout.splitlines()[-1], "FAILED (failures=1, errors=4)")

    def test_a_test_that_patches_time_sleep_sees_only_its_own_calls(self):
        """The runner's threads never sleep through time.sleep, which tests patch process-wide to drive fake clocks;
        a runner thread in a test's fake made it fail far from the runner ('751.0 != 5', 7cd5810)."""
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

    def test_a_write_under_the_guarded_home_fails_its_test_even_when_the_code_swallows_the_refusal(self):
        """A write under the real ~/.multiverse fails the test whose thread made it, even in a plain TestCase whose
        code swallows the refusal, as a best-effort cache write does; otherwise tests write the user's real home and
        nothing says so."""
        guarded = self.row / "guarded"
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
        self.assertIn("ERROR: test_swallowed (test_writes.Writes.test_swallowed)", result.stdout)
        self.assertIn(f"{guarded / 'cache.json'} in test_writes.Writes.test_swallowed", result.stdout)
        self.assertFalse((guarded / "cache.json").exists())

    def test_no_credential_or_other_checkouts_mv2_reaches_a_module_test_or_child_process(self):
        """Before it imports or starts anything, the runner drops every variable that may hold a credential (tokens,
        secrets, keys, passwords, auth sockets, askpass helpers) and points MV2_CLI and MV2_PYTHON at the mv2 under
        test, not the running agent's; otherwise a failing assertion that prints an environment leaks a token, and a
        worktree's tests run the live checkout's mv2, and nothing shows either."""
        planted = {"FAKE_SERVICE_TOKEN": "planted-token-value", "GITHUB_PERSONAL_ACCESS_TOKEN": "planted-gh-value",
                   "DEPLOY_KEY": "planted-key", "NPM_AUTH": "planted-auth", "MYSQL_PWD": "planted-pwd",
                   "GIT_ASKPASS": "/planted/askpass", "VSCODE_GIT_ASKPASS_MAIN": "/planted/vscode-askpass",
                   "SSH_AUTH_SOCK": "/planted/agent.sock"}
        kept = {"GIT_AUTHOR_NAME": "not a credential"}  # git's identity, which tests need
        elsewhere = {"MV2_CLI": "/another/checkout/bin/mv2", "MV2_PYTHON": "/another/checkout/.venv/bin/python"}
        own = {"MV2_CLI": str(RUNNER.parents[1] / "bin" / "mv2"), "MV2_PYTHON": sys.executable}
        self.module("test_secrets", f"""
            import json, os, subprocess, sys, unittest
            NAMES = {sorted(planted) + sorted(kept)!r}
            SEEN_AT_IMPORT = [name for name in NAMES if name in os.environ]
            class Plain(unittest.TestCase):
                def test_nothing_sees_a_planted_credential(self):
                    child = subprocess.run([sys.executable, "-c", "import json, os, sys; "
                                            "print(json.dumps([name for name in sys.argv[1:] if name in os.environ]))",
                                            *NAMES], capture_output=True, text=True, check=True)
                    seen = {{"import": SEEN_AT_IMPORT, "test": [name for name in NAMES if name in os.environ],
                             "child": json.loads(child.stdout)}}
                    self.assertEqual(seen, {{"import": {sorted(kept)!r}, "test": {sorted(kept)!r},
                                            "child": {sorted(kept)!r}}})
                    mv2 = subprocess.run([sys.executable, "-c", "import json, os; print(json.dumps("
                                          "[os.environ.get('MV2_CLI'), os.environ.get('MV2_PYTHON')]))"],
                                         capture_output=True, text=True, check=True)
                    self.assertEqual(json.loads(mv2.stdout), {[own["MV2_CLI"], own["MV2_PYTHON"]]!r})
            """)
        result = subprocess.run(self.command("-v", "test_secrets"), env={**os.environ, **planted, **kept, **elsewhere},
                                text=True,
                                capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 tests", result.stdout)
        for value in planted.values():
            self.assertNotIn(value, result.stdout + result.stderr)

    def interrupted(self, how):
        """Start a run whose second unit waits, stop the runner with signal `how` once the first unit has failed and
        the second is waiting, and return (the runner's exit status, its output, the waiting worker's pid, and
        whether that test's cleanup ran)."""
        started, cleaned = self.row / "started", self.row / "cleaned"
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
        # Started with Ctrl-C ignored, as `tests/run.py &` in a script starts it: the runner must restore it. Through
        # runpy and from /, which puts neither tests/ nor the checkout on sys.path (fdb61f6).
        ignoring = ("import runpy, signal, sys; signal.signal(signal.SIGINT, signal.SIG_IGN); "
                    "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
        runner = subprocess.Popen([sys.executable, "-c", ignoring, *self.command()[1:]], cwd="/", text=True,
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

    def test_stopping_a_run_runs_the_cleanups_of_the_tests_it_stops(self):
        """Ctrl-C, SIGTERM or the runner's own death stops each running test so that its cleanups run (they stop the
        tmux servers and daemons it started) and still shows the failures so far; a skipped cleanup leaks processes
        nobody sees."""
        for how, code in ((signal.SIGINT, 130), (signal.SIGTERM, 130), (signal.SIGKILL, -signal.SIGKILL)):
            with self.subTest(signal.Signals(how).name):
                self.fresh(signal.Signals(how).name)
                status, output, worker, cleaned = self.interrupted(how)
                self.assertEqual(status, code, output)
                if how != signal.SIGKILL:
                    self.assertIn("the failure before the interrupt", output)  # what had failed is still shown
                self.assertFalse(self.running(worker))
                self.assertTrue(cleaned)  # the stopped test still ran its cleanups

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

    def test_a_stop_reaches_the_test_and_never_cuts_a_cleanup_short(self):
        """A stop interrupts the test even through a broad `except` and in an async test, never a cleanup already
        running, and a test that ignores it is killed after the grace; otherwise a stop either leaks what the test
        started or kills a cleanup half done."""
        with self.subTest("a broad except"):
            started, cleaned = self.fresh("swallows") / "started", self.row / "cleaned"
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
        with self.subTest("an async test"):
            started, cleaned = self.fresh("async") / "started", self.row / "cleaned"
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
        with self.subTest("during a cleanup"):
            cleaning, cleaned = self.fresh("cleanup") / "cleaning", self.row / "cleaned"
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
            code, output = self.stopped_once(cleaning)
            self.assertEqual(code, 130, output)
            self.assertTrue(cleaned.exists(), output)  # the stop waited for the cleanup instead of cutting it short
        with self.subTest("ignored past the grace"):
            self.fresh("stubborn")
            self.module("test_stubborn", """
                import signal, time, unittest
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

    def orphaned(self, body, cleanup):
        """Start a run whose test tracks a detached helper, registers `cleanup` and runs `body`, and kill the runner
        outright once the test writes MARKER; return the worker's and the helper's PIDs."""
        marker, helper_file = self.row / "marker", self.row / "helper"
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

    @staticmethod
    def alive(pid):
        """Running and not a zombie waiting to be reaped."""
        from multiverse.runtime.process import process_snapshot
        snapshot = process_snapshot(pid)
        return snapshot is not None and not snapshot[1].startswith("Z")

    @staticmethod
    def zombie(pid):
        from multiverse.runtime.process import process_snapshot
        snapshot = process_snapshot(pid)
        return snapshot is not None and snapshot[1].startswith("Z")

    def test_an_orphaned_worker_ends_within_its_grace_with_what_it_tracked(self):
        """When the runner dies, each unit's watchdog stops it within the grace and ends what it tracked, even if a
        cleanup hangs or the test ignores SIGTERM; nothing else would, and the orphans run on unseen."""
        for name, body, cleanup in (
                ("a stuck cleanup", "pass", "Path(MARKER).write_text('cleaning')\ntime.sleep(600)"),
                ("a test ignoring SIGTERM",
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nPath(MARKER).write_text('ignoring')\ntime.sleep(600)",
                 "pass")):
            with self.subTest(name):
                self.fresh(name.replace(" ", "-"))
                worker, helper = self.orphaned(body=body, cleanup=cleanup)
                self.assertTrue(self.gone_soon(worker, helper), "the worker or its tracked helper outlived the grace")

    @unittest.skipUnless(shutil.which("tmux"), "tmux required")
    def test_what_a_stopped_unit_tracked_outside_its_process_group_is_ended(self):
        """A tmux server and a daemon a test started in sessions of their own and named with track() are ended after
        its unit even when none of its cleanups ran; the runner's kill of the unit cannot reach them, and they would
        keep running unseen."""
        socket, started = self.row / "tmux.sock", self.row / "started"
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
        self.assertTrue(self.gone_soon(daemon))

    def test_a_tracked_daemon_group_loses_its_helpers_after_its_leader_exits(self):
        """Helpers a tracked daemon leaves behind are ended with its group after the daemon has exited: those running
        when it was tracked, those the worker saw later, those of a unit that ends at once (de07cdc), and late ones
        once track() is called again (the documented remedy); a daemon's git or registry helpers outlive their test
        unseen."""
        with self.subTest("seen by the worker after the leader exited"):
            row = self.fresh("seen")
            ready, helpers, leader = (row / name for name in ("ready", "helpers", "leader.py"))
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
                        deadline = time.monotonic() + 60  # until the worker has seen both, however loaded
                        while time.monotonic() < deadline:
                            seen = Path(TRACKED).read_text()
                            if all(f'"pid": {{pid}},' in seen for pid in helpers):
                                break
                            time.sleep(0.05)
                        Path({str(ready)!r} + ".done").write_text("")
                        time.sleep(600)
                """)
            code, output = self.stopped_once(self.row / "ready.done", "--grace", "1", seconds=90)
            first, second = (int(pid) for pid in helpers.read_text().split())
            for pid in (first, second):
                self.addCleanup(self.kill_if_running, pid)
            self.assertEqual(code, 130, output)
            self.assertTrue(self.gone_soon(first), "the helper started before the group was tracked")
            self.assertTrue(self.gone_soon(second), "the helper started after it")
        with self.subTest("a unit that ends before the worker's first look"):
            row = self.fresh("quick")
            helper_file, release, leader = (row / name for name in ("helper", "release", "leader.py"))
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
        with self.subTest("a late helper, with the leader tracked again"):
            row = self.fresh("late")
            go, helper_file, leader = (row / name for name in ("go", "helper", "leader.py"))
            leader.write_text(textwrap.dedent(f"""
                import subprocess, sys, time
                from pathlib import Path
                while not Path({str(go)!r}).exists():
                    time.sleep(0.005)
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", {str(self.work)!r}])
                Path({str(helper_file)!r}).write_text(str(child.pid))
                while not Path({str(go)!r} + ".2").exists():
                    time.sleep(0.005)
                """))
            self.module("test_late", f"""
                import subprocess, sys, time, unittest
                from pathlib import Path
                from isolation import track
                class Late(unittest.TestCase):
                    def test_group(self):
                        leader = subprocess.Popen([sys.executable, {str(leader)!r}], start_new_session=True)
                        track(pid=leader.pid)  # no helper yet
                        Path({str(go)!r}).touch()
                        while not Path({str(helper_file)!r}).exists():
                            time.sleep(0.005)
                        track(pid=leader.pid)  # again, now that it has started its helper and is still running
                        Path({str(go)!r} + ".2").touch()
                        leader.wait()
                """)
            result = self.run_tests("test_late")
            helper = self.pid_in(helper_file)
            self.addCleanup(self.kill_if_running, helper)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(self.gone_soon(helper), "the helper outlived the unit that tracked its group")

    def test_the_runner_never_signals_a_process_it_cannot_attest(self):
        """Ending tracked processes acts only on a process whose start time is the one recorded, and on a group only
        while a recorded process is in it; reused pids, zombies, damaged records and a group of only zombies (which
        macOS refuses to signal) are left alone without an error; a wrong kill would hit another of the user's
        processes."""
        sys.path.insert(0, str(RUNNER.parent))
        self.addCleanup(sys.path.remove, str(RUNNER.parent))
        from run import Run
        import tracking
        from multiverse.runtime.process import process_started
        with self.subTest("ending what a unit tracked"):
            tracked = self.row / "tracked.jsonl"
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
                     json.dumps({"tmux_socket": str(self.row / "no-server.sock")}),  # its server is gone already
                     json.dumps({"group": [1], "member_of": {"a": 1}, "tmux_socket": [2], "pid": [3], "started": 4}),
                     "[]",
                     '{"pid": 1, "start']  # cut short when its unit was killed
            (self.row / "no-server.sock").write_text("")
            tracked.write_text("\n".join(lines))
            self.assertEqual(Run.end_tracked(tracked), [])
            self.assertIsNone(survivor.poll())  # spared: not the process that was tracked
            self.assertEqual(Run.end_tracked(self.row / "missing.jsonl"), [])
            tracked.write_text(json.dumps({"pid": survivor.pid, "started": process_started(survivor.pid)}) + "\n")
            self.assertEqual(Run.end_tracked(tracked), [f"process {survivor.pid}"])
            self.assertEqual(survivor.wait(timeout=10), -signal.SIGKILL)
        with self.subTest("sampling a group"):
            child = "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)', sys.argv[1]]); "
            leader = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; " + child
                                       + "time.sleep(600)", str(self.work)], start_new_session=True)
            self.addCleanup(lambda: self.kill_group(leader))
            deadline = time.monotonic() + 30
            while len([pid for pid, group, _, _ in tracking.processes() if group == leader.pid]) < 2:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            tracked = self.row / "group.jsonl"
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
            self.assertEqual([member["member_of"] for member in members], [leader.pid])  # its child, not the leader
            self.assertNotEqual(members[0]["pid"], leader.pid)
        with self.subTest("killing a unit whose group holds only a zombie"):
            unit = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
            deadline = time.monotonic() + 10
            while not self.zombie(unit.pid) and time.monotonic() < deadline:  # exited, alone in its group, unreaped
                time.sleep(0.05)
            self.assertTrue(self.zombie(unit.pid))
            Run.kill(unit)  # macOS refuses killpg here (EPERM): nothing is left to kill, and it reaps the unit
            self.assertEqual(unit.returncode, 0)

    @staticmethod
    def kill_group(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()

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
        """Concurrent runs, whatever each one's -j, never run more units at once than the machine's slots; a broken
        lock shows only as load and timing flakes in other agents' runs."""
        log = self.row / "times.log"
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

    def test_a_slots_directory_others_could_change_is_refused(self):
        """The slots directory must be a real directory owned by this user with mode 700; a link or a shared one would
        let another user hold or swap the suite's slots, and nothing would say so."""
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

    def kill_if_running(self, pid):
        """Kill `pid` if it is still one this test started (never a process that took the number since)."""
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], text=True, capture_output=True).stdout
        if str(self.work) in command:
            os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
