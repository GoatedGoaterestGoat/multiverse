"""The plugin host: where plugins' event handlers and services run, outside every other mv2 process.

    python -m multiverse.plugins.host ROOT                the supervisor (one per top-level universe tree)
    python -m multiverse.plugins.host --worker ROOT NAME  one plugin's worker

The supervisor loads no plugin code. It runs while some plugin that is on
somewhere in the tree extends `events` or `service`, keeps one worker process
per such plugin, and restarts a worker that exits at most 3 times in 5 minutes,
then leaves it stopped with the reason. It prunes the tree's journals hourly
and exits once no plugin needs it.

A worker loads its plugin once, starts its services in threads, and tails the
journal of every universe in the tree where the plugin is on, calling the
handlers that match each event. When the plugin is turned on or off in a
universe, its services stop and start again with the new set of universes. Its cursor per universe is
state/plugins/NAME/events.cursor, so after a restart a handler sees events at
least once; a worker that starts with no cursor begins at the journal's end.

`mv2 plugins host start|stop|status` runs the supervisor in a tmux session
mv2 owns. Its state is state/plugins/host/ of the top-level universe:
host.lock (held while it runs), status.json and host.log.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import fnmatch
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .. import universe as universes
from ..store import now, write
from . import api, events, loader

HEARTBEAT_SECONDS = 15.0  # a supervisor whose status is older than this is not running
RESTARTS = 3
RESTART_WINDOW = 300
SUPERVISE_SECONDS = 2.0
WORKER_POLL_SECONDS = 0.5
PRUNE_SECONDS = 3600
HANDLER_SECONDS = 60.0
TIMEOUTS_PER_EVENT = 3  # a handler out of time on one event this many times is given up on for that event
NEEDS = frozenset({"events", "service"})


def host_directory(root):
    return Path(root) / "state" / "plugins" / "host"


def needed(root):
    """{plugin name: [universes where it is on]} of plugins in the tree that need the host. Reads manifests only."""
    result = {}
    for scope in universes.tree(Path(root)):
        for found in loader.entries(scope):
            if found.usable and NEEDS & found.manifest.extends:
                result.setdefault(found.name, []).append(scope)
    return result


def _log(root, text):
    try:
        loader.append_log(host_directory(root) / "host.log", f"{now()} {text}")
    except OSError:
        pass


def _held(root):
    """An open, locked host.lock, or None when another host holds it."""
    path = host_directory(root) / "host.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def running(root):
    """Whether a supervisor runs for this tree: its process is alive and its heartbeat (status.json, written every
    SUPERVISE_SECONDS) is recent. It never takes the host lock, so a status check can never make a supervisor
    that is just starting find the lock taken and give up."""
    try:
        recorded = json.loads((host_directory(root) / "status.json").read_text())
    except (OSError, ValueError):
        return False
    pid = recorded.get("pid") if isinstance(recorded, dict) else None
    if recorded.get("state") != "running" or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive, someone else's: the beat below decides
    except OSError:
        return False
    return _age(recorded.get("updated_at", "")) <= HEARTBEAT_SECONDS


def supervise(root, *, exit_when_idle=True, stopping=None):
    """Run the supervisor until stopped (SIGTERM, or `stopping` when given) or no plugin needs it."""
    root = Path(root).resolve()
    held = _held(root)
    if held is None:
        print("The plugin host is already running for this universe.", file=sys.stderr)
        return 0
    if stopping is None:
        stopping = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, lambda *_: stopping.set())
    workers, restarts, stopped = {}, {}, {}
    started, pruned = now(), 0.0
    _log(root, f"host started (pid {os.getpid()})")
    try:
        while not stopping.is_set():
            try:
                wanted = needed(root)
            except Exception as error:  # a broken tree must not end the host; try again
                _log(root, f"could not read the plugins here: {type(error).__name__}: {error}")
                wanted = {name: [] for name in workers}
            for name in list(workers):
                if name not in wanted:
                    _stop_worker(workers.pop(name))
                    _log(root, f"{name}: stopped (no longer on)")
            for name in list(stopped):
                if name not in wanted:
                    del stopped[name]  # turning it off and on again gives it a fresh start
            for name in wanted:
                process = workers.get(name)
                if process is not None and process.poll() is None:
                    continue
                if process is not None:
                    _log(root, f"{name}: worker exited with {process.returncode}")
                    recent = [moment for moment in restarts.get(name, []) if time.monotonic() - moment < RESTART_WINDOW]
                    if len(recent) >= RESTARTS:
                        stopped[name] = f"its worker exited {len(recent) + 1} times in 5 minutes; left stopped"
                        del workers[name]
                        _log(root, f"{name}: {stopped[name]}")
                        continue
                    restarts[name] = [*recent, time.monotonic()]
                if name in stopped:
                    continue
                workers[name] = subprocess.Popen([sys.executable, "-m", "multiverse.plugins.host", "--worker",
                                                  str(root), name], stdin=subprocess.DEVNULL)
            _write_status(root, started, workers, restarts, stopped)
            if time.monotonic() - pruned >= PRUNE_SECONDS:
                pruned = time.monotonic()
                for scope in universes.tree(root):
                    if events.segments(scope):
                        events.prune(scope)
            if exit_when_idle and not wanted:
                _log(root, "host stopped: no plugin here needs it")
                break
            stopping.wait(SUPERVISE_SECONDS)
    finally:
        for process in workers.values():
            _stop_worker(process)
        _write_status(root, started, {}, restarts, stopped, state="stopped")
        held.close()
    return 0


def _stop_worker(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _write_status(root, started, workers, restarts, stopped, state="running"):
    try:
        path = host_directory(root) / "status.json"
        previous = json.loads(path.read_text()) if path.is_file() else {}
        write(path, {**{key: previous[key] for key in ("socket", "session", "pane") if key in previous},
                     "state": state, "pid": os.getpid(), "started_at": started, "updated_at": now(),
                     "workers": {name: {"pid": process.pid, "state": "running" if process.poll() is None else "exited",
                                        "restarts": len(restarts.get(name, []))} for name, process in workers.items()},
                     "stopped": stopped})
    except (OSError, ValueError):
        pass


def status(root):
    """{running, state, pid, workers, stopped, ...} of this tree's host."""
    root = Path(root)
    path = host_directory(root) / "status.json"
    try:
        recorded = json.loads(path.read_text())
    except (OSError, ValueError):
        recorded = {}
    alive = running(root)
    return {**recorded, "running": alive, "state": "running" if alive else "stopped",
            "needed": sorted(needed(root))}


def start(root):
    """Start the supervisor in a tmux session mv2 owns, unless one runs already."""
    from ..terminal.tmux import Tmux
    root = Path(root).resolve()
    if running(root):
        return {**status(root), "started": False}
    meta = json.loads((root / "universe.json").read_text())
    backend = Tmux(os.environ.get("MV2_TMUX_SOCKET"))
    try:
        terminal = backend.create(f"mv2-plugins-{meta['id'][:8]}", root,
                                  [sys.executable, "-m", "multiverse.plugins.host", str(root)])
    except Exception as error:
        if "duplicate session" not in str(error):
            raise
        # Another start (a second dashboard) made the session first: wait for its supervisor instead.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not running(root):
            time.sleep(0.2)
        return {**status(root), "started": False}
    path = host_directory(root) / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write(path, {"state": "starting", "socket": terminal["socket"], "session": terminal["session"],
                 "pane": terminal["pane"], "updated_at": now()})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not running(root):
        time.sleep(0.2)
    return {**status(root), "started": True}


def stop(root):
    """Stop the supervisor (it stops its workers) and remove its tmux session."""
    from ..terminal.tmux import Tmux
    root = Path(root).resolve()
    recorded = status(root)
    if recorded["running"] and recorded.get("pid"):
        try:
            os.kill(recorded["pid"], signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and running(root):
            time.sleep(0.1)
    if recorded.get("session") and recorded.get("socket"):
        try:
            Tmux(recorded["socket"]).run("kill-session", "-t", recorded["session"])
        except Exception:
            pass  # already gone
    return {**status(root), "stopped": not running(root)}


def _cursor_path(scope, name):
    return Path(scope) / "state" / "plugins" / name / "events.cursor"


def _read_cursor(scope, name):
    """The last sequence handled here, or, the first time, the journal's end: a new worker does not replay history."""
    path = _cursor_path(scope, name)
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        found = events.segments(scope)
        last = events._last_sequence(found[-1][1]) if found else 0
        _write_cursor(scope, name, last)
        return last


def _write_cursor(scope, name, sequence):
    if not (Path(scope) / "universe.json").is_file():
        return  # removed or moved meanwhile: a cursor must never bring its directories back
    path = _cursor_path(scope, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".events.cursor.tmp")
    temporary.write_text(f"{sequence}\n")
    temporary.replace(path)


def _age(text):
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())
    except (AttributeError, ValueError):
        return 0.0


def work(root, name):
    """One plugin's worker: its services, and its handlers over the journals where it is on."""
    root = Path(root).resolve()
    loader.process = "host"
    scopes = needed(root).get(name, [])
    if not scopes:
        return 0
    try:
        loaded = loader.load(loader.entry(scopes[0], name))
    except (OSError, ValueError) as error:
        _log(root, f"{name}: {error}")
        return 1
    mv = api.Plugin(loaded)
    # The supervisor that started this worker. If it dies without stopping its workers (kill -9, the OOM killer,
    # a crash), this process is reparented and its parent changes: then it stops too. A changed parent cannot be
    # fooled the way a recorded pid could be, by another process reusing that pid.
    supervisor = os.getppid()
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda *_: stopping.set())
    failed = threading.Event()
    services = _Services(loaded, mv, root, name, failed)
    services.start(scopes)
    refreshed = 0.0
    try:
        while not stopping.is_set() and not failed.is_set():
            if os.getppid() != supervisor:
                _log(root, f"{name}: supervisor gone; stopping")
                break
            if time.monotonic() - refreshed > 5:
                refreshed = time.monotonic()
                current = needed(root).get(name, [])
                if not current:
                    break
                if current != scopes:
                    # Turned on or off in a universe: its services start again with the new set.
                    scopes = current
                    services.stop()
                    services.start(scopes)
            if loaded.registrations.handlers:
                for scope in scopes:
                    _deliver(loaded, mv, scope, name)
            stopping.wait(WORKER_POLL_SECONDS)
    finally:
        stopping.set()
        services.stop()
    return 1 if failed.is_set() else 0


class _Services:
    """One plugin's services in its worker: started for a set of universes, stopped, started again."""

    def __init__(self, loaded, mv, root, name, failed):
        self.loaded, self.mv, self.root, self.name, self.failed = loaded, mv, root, name, failed
        self.stopping, self.threads = None, []

    def start(self, scopes):
        if not self.loaded.registrations.services:
            return
        self.stopping, self.threads = threading.Event(), []
        _log(self.root, f"{self.name}: services start for {', '.join(str(scope) for scope in scopes)}")
        for start_service, _ in self.loaded.registrations.services:
            context = api.ServiceContext([self.mv.universe(scope) for scope in scopes], self.stopping,
                                         lambda text, scope=scopes[0]: self.mv.log(text, scope))

            def run(start_service=start_service, context=context):
                try:
                    start_service(context)
                except Exception as error:
                    loader.record(self.loaded, "service", error, self.root)
                    self.failed.set()
            thread = threading.Thread(target=run, name=f"{self.name} service", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        if self.stopping is None:
            return
        self.stopping.set()
        for _, stop_service in self.loaded.registrations.services:
            if stop_service is not None:
                loader.guard(self.loaded, "service stop", stop_service, root=self.root)
        for thread in self.threads:
            thread.join(5)
        self.stopping, self.threads = None, []


def _deliver(loaded, mv, scope, name):
    """Deliver what is new in `scope`'s journal, at least once to each handler.

    Each handler goes through the events in order on its own: one still
    running, or out of time, holds only itself, and its siblings go on. A
    handler that finished an event never gets it again while a sibling catches
    up. One out of time on the same event TIMEOUTS_PER_EVENT times is given up
    on for that event, with "gave up on seq N" recorded. The cursor on disk
    moves past an event once every matching handler finished with it, failed
    (recorded) or was given up on. A universe that was removed or moved is left
    alone."""
    scope = Path(scope)
    if not (scope / "universe.json").is_file():
        return
    after = _read_cursor(scope, name)
    found, gap = events.read(scope, after)
    if gap is not None:
        found = [{"type": "events.gap", "seq": gap["to"], "at": now(), "data": gap}, *found]
    if found:
        newest = found[-1]["seq"]
        for key in [key for key, seq in _finished.items() if key[:2] == (loaded.name, str(scope)) and seq > newest]:
            del _finished[key]  # the journal started again (its state was removed): nothing here was seen yet
    universe = mv.universe(scope)
    held, complete = set(), True  # handlers that have not finished an earlier event in this pass
    for event in found:
        if not _dispatch(loaded, universe, event, scope, held):
            complete = False
        elif complete:
            _write_cursor(scope, name, event["seq"])


# Per (plugin, universe, handler): the last seq it finished, and (seq, timeouts) for the event it is on.
_finished, _timeouts = {}, {}


def _dispatch(loaded, universe, event, scope, held=None):
    """Run each matching handler that has not finished `event`; False while one of them has not finished it.

    A handler in `held` (still on an earlier event) is not called: each handler sees the events in order."""
    held = set() if held is None else held
    kind, seq, finished = event.get("type", ""), event.get("seq", 0), True
    for index, (pattern, handler) in enumerate(loaded.registrations.handlers):
        if not fnmatch.fnmatchcase(kind, pattern):
            continue
        key = (loaded.name, str(scope), index)
        if _finished.get(key, -1) >= seq:
            continue  # it finished this one while a sibling held the cursor
        if index in held:
            finished = False
            continue
        value = api.Event(kind, seq, event.get("at", ""), universe, event.get("agent"), event.get("launch"),
                          event.get("data") or {}, _age(event.get("at", "")))
        label = getattr(handler, "__name__", f"handler {index + 1}")
        what = f"event {kind} ({label})"
        outcome = loader.guarded(loaded, what, handler, value, root=scope, timeout=HANDLER_SECONDS,
                                 key=f"handler {index} in {scope}")
        if outcome.state == "timeout":
            on, count = _timeouts.get(key, (seq, 0))
            count = count + 1 if on == seq else 1
            _timeouts[key] = (seq, count)
            if count < TIMEOUTS_PER_EVENT:
                held.add(index)
                finished = False
                continue
            loader.record(loaded, what, TimeoutError(f"gave up on seq {seq} after {count} timeouts"), scope)
        elif outcome.state == "busy":  # its call on this or an earlier event still runs
            held.add(index)
            finished = False
            continue
        _finished[key] = seq
        _timeouts.pop(key, None)
    return finished


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m multiverse.plugins.host", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", action="store_true", help="run one plugin's worker")
    parser.add_argument("root")
    parser.add_argument("name", nargs="?")
    args = parser.parse_args(argv)
    if args.worker:
        if not args.name:
            parser.error("--worker needs the plugin's name")
        return work(args.root, args.name)
    return supervise(args.root)


if __name__ == "__main__":
    sys.exit(main())
