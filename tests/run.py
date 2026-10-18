"""Run mv2's tests in parallel: each test module, or a shard of a slow one, in a fresh interpreter.

    .venv/bin/python tests/run.py                       # everything `unittest discover -s tests` runs
    .venv/bin/python tests/run.py test_tui test_kill    # some modules (a dotted name runs one class or test)
    .venv/bin/python tests/run.py -j 4 -v

A module runs through its own load_tests, as discovery runs it; a shard runs every Nth of its tests. Modules
start longest first, by the times the last run recorded in .run/test-durations.json, and one the record does
not know counts as long. Every process guards the real ~/.multiverse (home_safety) and fails if a test wrote
there.

Runs started at once share the machine: a unit starts only when it holds one of the CPU count's slots in
/tmp/mv2-test-slots-<uid> (a directory only this user may change), so two agents' full runs take turns instead of
overloading it. A unit still running after --timeout seconds, or when the run is interrupted (Ctrl-C or SIGTERM),
is stopped: the stop interrupts setUp or the test itself, never a tearDown or cleanup already running, so the test
fails and its cleanups still stop the tmux servers and native daemons it started. A unit not done GRACE seconds
later is killed. Should the runner itself die, each unit's watchdog process stops it the same way, with the same
grace. Either way, what the unit's tests named with isolation.track and left running is ended afterwards: a tmux
server by its socket, a process only while its start time matches, a group a daemon leads while a process seen in
it is still there.
"""
import argparse
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import random
import select
import signal
import stat
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
OBSERVE_SECONDS = 0.5  # how often a worker records who is in the process groups its tests tracked
SLOTS = Path(f"/tmp/mv2-test-slots-{os.getuid()}")  # shared by every run of the suite on this machine


# --- a worker: one unit, in its own interpreter -------------------------------------------------------------

def _tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _tests(test)
        else:
            yield test


class Stopped(BaseException):
    """The runner stopped this unit (interrupted, or past its time). The test running ends with this error, so its
    tearDown and cleanups still stop the tmux servers and native daemons it started. A BaseException, so that a
    test's or mv2's `except Exception` cannot swallow it."""


_interruptible = False  # while a test's setUp or body runs: the only time a stop may raise (never in its cleanups)
__unittest = True  # unittest leaves this module's frames, such as _mark_interruptible's, out of a test's traceback


def _mark_interruptible(test):
    """Let a stop raise in `test`'s setUp and body, and nowhere else."""
    for name in ("_callSetUp", "_callTestMethod"):
        method = getattr(test, name, None)
        if method is None:
            continue

        def marked(*args, method=method):
            global _interruptible
            _interruptible = True
            try:
                return method(*args)
            finally:
                _interruptible = False
        setattr(test, name, marked)


class _Result(unittest.TextTestResult):
    running = None  # this process's result, for _stop

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.times, self.test = {}, None
        _Result.running = self

    def startTest(self, test):
        self._started, self.test = time.monotonic(), test
        import home_safety
        home_safety.running(test.id())  # a refused write is charged to this test, even in a plain TestCase
        super().startTest(test)

    def stopTest(self, test):
        import home_safety
        try:
            home_safety.check(test.id())  # what this test wrote under the real home, if its fixtures did not say
        except AssertionError:
            self.addError(test, sys.exc_info())
        home_safety.running(None)
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
    if _interruptible:
        raise Stopped("stopped by tests/run.py")


def _watchdog(runner, worker_alive, worker, grace, tracked):
    """A unit's watchdog, a process of its own beside the worker: should the runner go, however it ended, do what it
    would have. Stop the worker (_stop), give it `grace` seconds to finish, then end what its tests tracked
    (Run.end_tracked) and kill what is left of its process group. A process, not a thread in the worker, so that a
    test that ignores SIGTERM or a cleanup that never returns cannot hold it up; it ends once the worker has.

    Nothing is ever written to either pipe: one becomes readable only when the runner's end of it closes, the other
    when the worker's does."""
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, signal.SIG_IGN)  # it ends by itself, once the worker has
    if worker_alive in select.select([runner, worker_alive], [], [])[0]:
        if select.select([runner], [], [], 0)[0]:  # the worker ended, and the runner is gone too: nobody else will
            Run.end_tracked(tracked)
        return
    try:
        os.kill(worker, signal.SIGTERM)
    except ProcessLookupError:
        pass
    select.select([worker_alive], [], [], grace)
    Run.end_tracked(tracked)
    try:
        os.killpg(worker, signal.SIGKILL)  # the worker leads its own session: it and its tests' processes
    except OSError:
        pass  # none left (macOS also refuses a group of only zombies)


def _records(path):
    """What a unit's tests tracked (isolation.track) and its worker saw in their groups (_observe), one JSON object
    a line; a line cut short when the unit was killed is skipped."""
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, TypeError):
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _processes():
    """(pid, process group, state, start) of every process, the start in the form process_started gives; [] when
    ps cannot say."""
    if str(HERE.parent / "src") not in sys.path:
        sys.path.insert(0, str(HERE.parent / "src"))
    from multiverse.runtime.process import START
    try:
        output = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat=,lstart="], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
                                timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 8 and fields[0].isdigit() and fields[1].isdigit() and START.fullmatch(" ".join(fields[3:])):
            found.append((int(fields[0]), int(fields[1]), fields[2], " ".join(fields[3:])))
    return found


def _observe(tracked):
    """Record, with its start time, every process seen in a process group a test tracked, for as long as the group
    lasts: its leader can exit first and leave helpers behind, and Run.end_tracked ends the group only while a
    process recorded here is still in it. A group seen empty is not watched again, since its number may later
    belong to another group."""
    recorded, gone = set(), set()
    while True:
        time.sleep(OBSERVE_SECONDS)
        groups = {record["group"] for record in _records(tracked) if type(record.get("group")) is int} - gone
        if not groups:
            continue
        found = [(pid, group, started) for pid, group, state, started in _processes() if group in groups]
        gone |= groups - {group for _, group, _ in found}
        lines = [json.dumps({"member_of": group, "pid": pid, "started": started}) + "\n"
                 for pid, group, started in found if (pid, started) not in recorded]
        if lines:
            recorded.update((pid, started) for pid, _, started in found)
            with open(tracked, "a") as out:
                out.write("".join(lines))


def worker(start, target, shard, shards, report, verbosity, watch, grace):
    tracked, worker_pid = os.environ.get("MV2_TEST_TRACKED"), os.getpid()
    worker_alive, alive = os.pipe()  # the watchdog reads EOF from `worker_alive` once this process is gone
    if os.fork() == 0:  # before anything starts a thread
        os.close(alive)
        try:
            os.setpgid(0, 0)  # out of the unit's group, so never a zombie in it when the runner kills that group
            _watchdog(watch, worker_alive, worker_pid, grace, tracked)
        finally:
            os._exit(0)
    os.close(worker_alive)
    os.close(watch)  # the watchdog has it; the processes this unit's tests start never do
    sys.path[:0] = [str(start)] + ([str(HERE)] if Path(start) != HERE else [])
    import home_safety
    home_safety.install()  # strict: before a test module or fixture moves HOME
    loader = unittest.TestLoader()
    module = target.split(".", 1)[0]  # a module, or a class or test in one, by name
    try:  # as discovery imports a module: a SkipTest skips it, anything else is a failing test
        __import__(module)
    except unittest.SkipTest as skip:
        suite = unittest.loader._make_skipped_test(module, skip, unittest.TestSuite)
    except BaseException:
        suite, _ = unittest.loader._make_failed_import_test(module, unittest.TestSuite)
    else:
        suite = (loader.loadTestsFromName(target) if "." in target
                 else loader.loadTestsFromModule(sys.modules[module], pattern="test*.py"))
    tests = [test for index, test in enumerate(_tests(suite)) if index % shards == shard]
    for test in tests:
        _mark_interruptible(test)
    signal.signal(signal.SIGTERM, _stop)
    if tracked:
        threading.Thread(target=_observe, args=(tracked,), daemon=True).start()
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
        self.slots.mkdir(mode=0o700, parents=True, exist_ok=True)  # again, should it have gone since the check
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
        tracked = Path(scratch) / f"{target}.{shard}.tracked"  # what its tests started outside it (isolation.track)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(self.start), target, str(shard),
                   str(shards), str(report), "2" if self.verbose else "1", f"{self.grace:g}"]
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
                                                   start_new_session=True, pass_fds=(watch,),
                                                   env={**os.environ, "MV2_TEST_TRACKED": str(tracked)})
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
            ended = self.end_tracked(tracked)
            data = json.loads(report.read_text()) if report.exists() else None
            if ended:
                with output.open("a") as out:
                    out.write(f"\nThe runner ended what this unit left running: {', '.join(ended)}.\n")
                if data is not None:
                    data["ended"] = ended
        except Exception:
            data = None
            with output.open("a") as out:
                out.write("\n" + traceback.format_exc())
        return code, time.monotonic() - began, data, output.read_text(errors="replace")

    @staticmethod
    def end_tracked(path):
        """End what a unit's tests started outside its process group and named with isolation.track (a tmux server,
        a daemon) that is still running, however the unit ended; return what had to be ended.

        A process is ended only while its start time is the one recorded, never a later process that took its
        number. A tracked group leader's whole group is ended while any process recorded in it (the leader, or a
        member its worker saw, _observe) is still there, even after the leader itself has gone: a group's number
        cannot be reused while a process is in it, so a later group with the same number holds none of those."""
        records, ended = _records(path), []
        for socket in dict.fromkeys(record["tmux_socket"] for record in records
                                    if isinstance(record.get("tmux_socket"), str)):
            if not os.path.exists(socket):
                continue
            try:
                stopped = subprocess.run(["tmux", "-S", socket, "kill-server"], capture_output=True,
                                         stdin=subprocess.DEVNULL, timeout=10)
            except (OSError, subprocess.SubprocessError):
                stopped = None
            if stopped is not None and stopped.returncode == 0:
                ended.append(f"the tmux server on {socket}")
        identities = {(record["pid"], record["started"]) for record in records
                      if type(record.get("pid")) is int and isinstance(record.get("started"), str)}
        groups = {record[key] for record in records for key in ("group", "member_of") if type(record.get(key)) is int}
        if not identities:
            return ended
        live = {(pid, started): group for pid, group, state, started in _processes()
                if (pid, started) in identities and not state.startswith("Z")}
        killed = set()
        for group in sorted(group for group in groups if group in live.values()):
            try:
                os.killpg(group, signal.SIGKILL)
                killed.add(group)
                ended.append(f"process group {group}")
            except (ProcessLookupError, PermissionError):
                pass  # then its recorded members one by one, below
        for (pid, _), group in sorted(live.items()):
            if group not in killed:
                try:
                    os.kill(pid, signal.SIGKILL)
                    ended.append(f"process {pid}")
                except (ProcessLookupError, PermissionError):
                    pass
        return ended

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
        except (ProcessLookupError, PermissionError):
            pass  # none left (macOS refuses to signal a group whose remaining members are zombies)
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

    def slots_problem(self):
        """Why the slots directory cannot be trusted, or None. It must be a real directory, not a link, that only this
        user can change: otherwise another user could hold the slots or swap them."""
        try:
            self.slots.mkdir(mode=0o700, parents=True, exist_ok=True)
            status = os.lstat(self.slots)
        except OSError as error:
            return f"Cannot use {self.slots} for the machine's slots: {error}"
        if not stat.S_ISDIR(status.st_mode):
            return f"{self.slots} must be a directory, not a link or a file. Remove it and run again."
        if status.st_uid != os.getuid() or status.st_mode & 0o077:
            return (f"{self.slots} must be yours alone (owner {os.getuid()}, mode 700), but has owner {status.st_uid} "
                    f"and mode {stat.S_IMODE(status.st_mode):o}. Remove it and run again.")
        return None

    def __call__(self, names):
        problem = self.slots_problem()
        if problem:
            print(problem, file=sys.stderr)
            return 2
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
                          + (f", FAILED (exit {code})" if failed else "")
                          + (f"; ended what it left running: {', '.join(data['ended'])}" if data and data.get("ended")
                             else ""), flush=True)
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
        start, target, shard, shards, report, verbosity, grace, watch = argv[1:]
        return worker(start, target, int(shard), int(shards), report, int(verbosity), int(watch), float(grace))
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
