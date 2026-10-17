"""Run mv2's tests in parallel: each test module, or a shard of a slow one, in a fresh interpreter.

    .venv/bin/python tests/run.py                       # everything `unittest discover -s tests` runs
    .venv/bin/python tests/run.py test_tui test_kill    # some modules (a dotted name runs one class or test)
    .venv/bin/python tests/run.py -j 4 -v

A module runs through its own load_tests, as discovery runs it; a shard runs every Nth of its tests. Modules
start longest first, by the times the last run recorded in .run/test-durations.json, and one the record does
not know counts as long. Every process guards the real ~/.multiverse (home_safety) and fails if a test wrote
there.

Runs started at once share the machine: a unit starts only when it holds one of the CPU count's slots in
/tmp/mv2-test-slots-<uid>, so two agents' full runs take turns instead of overloading it. A unit still running
after --timeout seconds, or when the run is interrupted (Ctrl-C or SIGTERM), is stopped: its test fails and still
runs its cleanups (stopping the tmux servers and native daemons it started); one not done GRACE seconds later is
killed. A unit whose runner is killed outright stops the same way.
"""
import argparse
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest

HERE = Path(__file__).resolve().parent
SHARD_SECONDS = 20  # a module the record says takes longer is split into shards of about this
UNKNOWN_SECONDS = 30  # a module the record does not know starts early, unsplit
TIMEOUT = 1200
GRACE = 30  # seconds a unit the runner stops gets to finish its test's cleanups before it is killed
SLOTS = Path(f"/tmp/mv2-test-slots-{os.getuid()}")  # shared by every run of the suite on this machine


# --- a worker: one unit, in its own interpreter -------------------------------------------------------------

def _tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _tests(test)
        else:
            yield test


class Stopped(Exception):
    """The runner stopped this unit (interrupted, or past its time). The test running ends with this error, so its
    tearDown and cleanups still stop the tmux servers and native daemons it started."""


class _Result(unittest.TextTestResult):
    running = None  # this process's result, for _stop

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.times, self.test = {}, None
        _Result.running = self

    def startTest(self, test):
        self._started, self.test = time.monotonic(), test
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        self.times[test.id()], self.test = round(time.monotonic() - self._started, 3), None


def _stop(signum, frame):
    """SIGTERM from the runner: start no other test and end the one running. Once only, so that a second signal
    cannot cut its cleanups short."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    result = _Result.running
    if result is None:
        raise SystemExit(128 + signum)  # no test has started: nothing to clean up
    result.stop()
    if result.test is not None:
        raise Stopped("stopped by tests/run.py")


def _watch(fd):
    """Stop this unit once the runner that started it is gone, however it ended: nothing is ever written to the pipe,
    so a read returns only when the runner's end of it closes."""
    while os.read(fd, 1):
        pass
    os.kill(os.getpid(), signal.SIGTERM)


def worker(start, target, shard, shards, report, verbosity, watch):
    sys.path[:0] = [str(start)] + ([str(HERE)] if Path(start) != HERE else [])
    import home_safety
    home_safety.install()  # strict: before a test module or fixture moves HOME
    loader = unittest.TestLoader()
    if "." in target:  # one class or test, by name
        suite = loader.loadTestsFromName(target)
    else:
        try:
            __import__(target)
        except Exception:
            suite = loader.loadTestsFromName(target)  # a test that fails with the import error
        else:
            suite = loader.loadTestsFromModule(sys.modules[target], pattern="test*.py")
    tests = [test for index, test in enumerate(_tests(suite)) if index % shards == shard]
    signal.signal(signal.SIGTERM, _stop)
    os.set_inheritable(watch, False)  # the processes its tests start do not keep the runner's pipe
    threading.Thread(target=_watch, args=(watch,), daemon=True).start()
    result = unittest.TextTestRunner(resultclass=_Result, verbosity=verbosity, stream=sys.stderr).run(
        unittest.TestSuite(tests))
    errors = [[test.id(), text] for test, text in result.errors]
    try:
        home_safety.check()  # what no test's own check reported, such as a write by a thread that outlived it
    except AssertionError as error:
        errors.append([f"{target} (after its tests)", str(error)])
    Path(report).write_text(json.dumps({
        "run": result.testsRun, "skipped": [[test.id(), reason] for test, reason in result.skipped], "errors": errors,
        "failures": ([[test.id(), text] for test, text in result.failures]
                     + [[test.id(), "unexpected success"] for test in result.unexpectedSuccesses]),
        "times": result.times}))
    sys.stderr.flush()
    return 0 if result.wasSuccessful() and not errors else 1


# --- the parent: plan the units, run them, report -----------------------------------------------------------

class Run:
    def __init__(self, start, jobs, timeout, verbose, slots=SLOTS, budget=None, grace=GRACE):
        self.start, self.jobs, self.timeout, self.verbose = Path(start).resolve(), jobs, timeout, verbose
        self.grace = grace
        self.durations = self.start.parent / ".run" / "test-durations.json"
        self.processes, self.lock, self.stopping = set(), threading.Lock(), threading.Event()
        self.slots, self.budget, self.held, self.told = Path(slots), budget or os.cpu_count() or 4, 0, False

    def targets(self, names):
        if not names:
            return sorted(path.stem for path in self.start.glob("test*.py") if path.stem.isidentifier())
        return [name[:-3].replace(os.sep, ".").rpartition(".")[2] if name.endswith(".py") else name
                for name in names]

    def recorded(self):
        try:
            return dict(json.loads(self.durations.read_text())["modules"])
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def plan(self, targets):
        """(target, shard, shards) for each unit, the longest expected first."""
        known, units = self.recorded(), []
        for target in targets:
            expected = known.get(target, UNKNOWN_SECONDS) if "." not in target else 0
            shards = max(1, round(expected / SHARD_SECONDS)) if target in known else 1
            units += [(expected / shards, target, shard, shards) for shard in range(shards)]
        return [unit[1:] for unit in sorted(units, key=lambda unit: -unit[0])]

    def slot(self):
        """Wait for one of the machine's slots and return it held, or None once the run is stopping.

        Every run of the suite on this machine shares `budget` slots, so runs started at once take turns instead
        of overloading it. Closing the file frees its slot, as does the end of this process however it ends."""
        self.slots.mkdir(mode=0o700, parents=True, exist_ok=True)
        while not self.stopping.is_set():
            for index in random.sample(range(self.budget), self.budget):
                handle = (self.slots / f"slot-{index}").open("a")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                with self.lock:
                    self.held += 1
                return handle
            with self.lock:  # all taken, some by other runs: say so once
                tell, self.told = not self.told and self.held < self.budget, self.told or self.held < self.budget
            if tell:
                print(f"Waiting for a slot: other runs of the suite are using some of this machine's {self.budget}.",
                      flush=True)
            self.stopping.wait(0.2)
        return None

    def unit(self, target, shard, shards, scratch):
        """Run one unit in a new process once it has a slot: (exit code or why it has none, seconds, report or
        None, output)."""
        slot = self.slot()
        if slot is None:
            return "not started", 0.0, None, ""
        try:
            return self._unit(target, shard, shards, scratch)
        finally:
            slot.close()
            with self.lock:
                self.held -= 1

    def _unit(self, target, shard, shards, scratch):
        report, output = Path(scratch) / f"{target}.{shard}.json", Path(scratch) / f"{target}.{shard}.log"
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(self.start), target, str(shard),
                   str(shards), str(report), "2" if self.verbose else "1"]
        began, code = time.monotonic(), "not started"
        try:
            with output.open("wb") as out:
                with self.lock:
                    if self.stopping.is_set():
                        return code, 0.0, None, ""
                    watch, alive = os.pipe()  # the worker reads EOF from `watch` once this process is gone (_watch)
                    try:
                        process = subprocess.Popen(command + [str(watch)], cwd=self.start.parent, stdout=out,
                                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                   start_new_session=True, pass_fds=(watch,))
                    except BaseException:
                        os.close(alive)
                        raise
                    finally:
                        os.close(watch)
                    self.processes.add(process)
                try:
                    code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    code = (f"stopped after {self.timeout:g} s" if self.end(process)
                            else f"killed after {self.timeout:g} s, its cleanups not done in {self.grace:g} s")
                finally:
                    os.close(alive)
                    with self.lock:
                        self.processes.discard(process)
            data = json.loads(report.read_text()) if report.exists() else None
        except Exception:
            data = None
            with output.open("a") as out:
                out.write("\n" + traceback.format_exc())
        return code, time.monotonic() - began, data, output.read_text(errors="replace")

    def end(self, process):
        """Stop a unit: SIGTERM lets the test running fail and clean up (_stop). Then, or once it has had `grace`
        seconds, kill what is left of its process group. Whether it ended by itself."""
        try:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=self.grace)
            ended = True
        except subprocess.TimeoutExpired:
            ended = False
        self.kill(process)
        return ended

    @staticmethod
    def kill(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)  # the unit's own session: its tests' children too
        except ProcessLookupError:
            pass
        process.wait()

    def stop(self):
        """End every unit still running, each given its grace to clean up (end); none starts after this."""
        self.stopping.set()
        with self.lock:
            running = list(self.processes)
        enders = [threading.Thread(target=self.end, args=(process,), daemon=True) for process in running]
        for ender in enders:
            ender.start()
        for ender in enders:
            ender.join()

    def kill_all(self):
        with self.lock:
            for process in list(self.processes):
                self.kill(process)

    def __call__(self, names):
        units = self.plan(self.targets(names))
        print(f"{len(units)} units on {self.jobs} processes", flush=True)
        began, totals, problems, measured, done = time.monotonic(), {"run": 0, "skipped": 0}, [], {}, 0
        counts = {"failures": 0, "errors": 0}
        with tempfile.TemporaryDirectory(prefix="mv2-tests-") as scratch:
            pool = concurrent.futures.ThreadPoolExecutor(self.jobs)
            try:
                futures = {}
                for index, unit in enumerate(units):  # a directory each: a name given twice runs twice
                    (Path(scratch) / str(index)).mkdir()
                    futures[pool.submit(self.unit, *unit, Path(scratch) / str(index))] = unit
                for future in concurrent.futures.as_completed(futures):
                    target, shard, shards = futures[future]
                    code, seconds, data, output = future.result()
                    label = target if shards == 1 else f"{target} ({shard + 1} of {shards})"
                    done += 1
                    if data:
                        totals["run"] += data["run"]
                        totals["skipped"] += len(data["skipped"])
                        counts["failures"] += len(data["failures"])
                        counts["errors"] += len(data["errors"])
                        if "." not in target:
                            measured[target] = measured.get(target, 0) + sum(data["times"].values())
                    failed = code != 0 or not data or bool(data["failures"] or data["errors"])
                    print(f"[{done}/{len(units)}] {label}: {data['run'] if data else '?'} tests in {seconds:.1f} s"
                          + (f", FAILED (exit {code})" if failed else ""), flush=True)
                    if self.verbose:
                        print(output, flush=True)
                    if failed:
                        problems.append((label, code, self.details(output, data), data is not None))
            except KeyboardInterrupt:
                print(f"\nInterrupted: stopping the units still running, each with up to {self.grace:g} s to clean up "
                      "(Ctrl-C again to kill them now).", flush=True)
                try:
                    self.stop()
                except KeyboardInterrupt:
                    self.kill_all()
                pool.shutdown(cancel_futures=True)
                self.show(problems)
                print("Interrupted: the units still running were stopped.", flush=True)
                return 130
            pool.shutdown()
        self.show(problems)
        self.record(measured)
        print("-" * 70 + f"\nRan {totals['run']} tests in {time.monotonic() - began:.1f}s on {self.jobs} processes\n")
        skipped = f"skipped={totals['skipped']}" if totals["skipped"] else ""
        if not problems:
            print(f"OK ({skipped})" if skipped else "OK")
            return 0
        found = [f"{kind}={count}" for kind, count in counts.items() if count]
        broken = sum(1 for _, _, _, reported in problems if not reported)
        found += [f"units without a result={broken}"] if broken else []
        print(f"FAILED ({', '.join(found + ([skipped] if skipped else []))})")
        return 1

    @staticmethod
    def show(problems):
        for label, code, text, _ in problems:
            print("=" * 70 + f"\n{label} (exit {code})\n" + "-" * 70 + f"\n{text}", flush=True)

    @staticmethod
    def details(output, data):
        """The unit's output, and any failure in its report that the output does not already show."""
        for kind in ("failures", "errors"):
            for test, detail in (data or {}).get(kind, []):
                if detail not in output:
                    output += f"\n{'FAIL' if kind == 'failures' else 'ERROR'}: {test}\n{detail}\n"
        return output

    def record(self, measured):
        """Keep each module's latest time for the next run's plan (other modules keep theirs)."""
        if not measured:
            return
        self.durations.parent.mkdir(exist_ok=True)
        temporary = self.durations.with_name(f".{self.durations.name}.{os.getpid()}")
        temporary.write_text(json.dumps({"modules": {**self.recorded(),
                                                     **{k: round(v, 2) for k, v in measured.items()}}},
                                        indent=1, sort_keys=True) + "\n")
        temporary.replace(self.durations)


def _terminated(signum, frame):
    raise KeyboardInterrupt  # a kill stops the run as Ctrl-C does: each unit with its cleanups


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--worker"]:
        start, target, shard, shards, report, verbosity, watch = argv[1:]
        return worker(start, target, int(shard), int(shards), report, int(verbosity), int(watch))
    signal.signal(signal.SIGINT, signal.default_int_handler)  # even when started with it ignored, as `cmd &` is
    signal.signal(signal.SIGTERM, _terminated)
    parser = argparse.ArgumentParser(prog="tests/run.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("names", nargs="*", metavar="NAME",
                        help="test modules (test_tui, tests/test_tui.py) or dotted classes and tests; default: all")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                        help="units at once (default: the CPU count)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="show each unit's tests as it ends")
    parser.add_argument("--timeout", type=float, default=TIMEOUT,
                        help=f"seconds before a unit is killed (default: {TIMEOUT})")
    parser.add_argument("-s", "--start-directory", default=str(HERE), help=argparse.SUPPRESS)
    parser.add_argument("--slots-directory", default=str(SLOTS), help=argparse.SUPPRESS)  # for the runner's tests
    parser.add_argument("--budget", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--grace", type=float, default=GRACE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return Run(args.start_directory, max(1, args.jobs), args.timeout, bool(args.verbose), args.slots_directory,
               args.budget, args.grace)(args.names)


if __name__ == "__main__":
    sys.exit(main())
