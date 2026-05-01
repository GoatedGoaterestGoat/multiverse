"""Compaction orchestration driven by the monitor's Compact widget.

This module owns the state machine for a single user-triggered compaction:

  1. Acquire the cross-monitor compacting lock (so other monitors render
     the row as ``compacting`` and refuse actions on it).
  2. Kill the session if alive — moves it to dormant so no new turns can
     land mid-build.
  3. If a tree-build worker is already running for this session (likely
     the auto-worker fired by the last UserPromptSubmit, which skipped
     the in-progress last turn), wait for it to finish — tailing its log
     to the UI.
  4. Once that's done (or if no auto-worker was running), check whether
     the last turn's tree exists. If not (the common case after step 3),
     spawn a new worker with ``--all-turns`` to fill the gap. The worker
     will skip turns that already have cached trees, so this is cheap
     except for the missing one.
  5. Wait for the new worker to finish (also tailing log).
  6. Release the lock.

The actual rendering of the compacted live JSONL — picking a budget,
running the scheduler, writing the new live.jsonl — is a separate step
that will live in a dedicated module once the expansion API lands.

Callbacks let the UI stay decoupled:
  ``on_log(line)``       — one line of text from the worker log.
  ``on_progress(b, t)``  — trees built / total turns.
  ``on_state(state)``    — short state token, see ``State`` enum below.
  ``on_done(ok, msg)``   — final result.

All callbacks are invoked from the runner's thread; the caller is
responsible for marshalling them to the UI thread (e.g. via Textual's
``call_from_thread``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .compact_state import acquire_lock, release_lock
from .full_jsonl import full_jsonl_path, sync_full
from .paths import multiverse_dir

# Vendored compaction package — needed for `parse_session` to count turns
# of the full JSONL. Adding to sys.path here so callers don't have to.
_PLUGIN = Path(__file__).resolve().parent.parent
_VENDOR = str(_PLUGIN / "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


# ---------------------------------------------------------------------------
# State labels (purely informational — the UI uses these for status text)
# ---------------------------------------------------------------------------

class State:
    LOCKING = "locking"
    KILLING = "killing"
    SYNCING = "syncing"
    WAITING_AUTO = "waiting_for_auto_worker"
    BUILDING = "building_remaining"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class CompactPaths:
    """Resolved per-session paths used during compaction."""
    cache_root: Path
    lockfile: Path
    log: Path

    @classmethod
    def for_session(cls, claude_uuid: str) -> "CompactPaths":
        cache_root = multiverse_dir() / "trees" / claude_uuid
        return cls(
            cache_root=cache_root,
            lockfile=cache_root / ".worker.lock",
            log=cache_root / "worker.log",
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

LogCb = Callable[[str], None]
ProgCb = Callable[[int, int], None]
StateCb = Callable[[str], None]
DoneCb = Callable[[bool, str], None]


class CompactRunner:
    """Orchestrates one compaction. ``run()`` blocks; call from a
    worker thread."""

    def __init__(
        self,
        claude_uuid: str,
        cwd: str,
        live_jsonl: Path,
        kill_session_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self.claude_uuid = claude_uuid
        self.cwd = cwd
        self.live_jsonl = live_jsonl
        self.kill_session_fn = kill_session_fn
        self.paths = CompactPaths.for_session(claude_uuid)
        self._cancelled = threading.Event()
        self._log_offset = 0  # bytes already streamed to UI

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    # ---------------------------------------------------------------- public

    def run(
        self,
        on_log: LogCb,
        on_progress: ProgCb,
        on_state: StateCb,
        on_done: DoneCb,
    ) -> None:
        try:
            self._run(on_log, on_progress, on_state, on_done)
        except Exception as e:  # noqa: BLE001 — UI must always get a closure
            on_state(State.FAILED)
            on_done(False, f"unexpected error: {e}")

    def _run(self, on_log, on_progress, on_state, on_done) -> None:
        on_state(State.LOCKING)
        if not acquire_lock(self.claude_uuid):
            on_state(State.FAILED)
            on_done(False, "Another monitor is already compacting this session.")
            return

        try:
            # 1. Kill the live session if any. Any tmux session backing it
            # disappears; the multiverse state dir is reaped. The compacting
            # lock lives outside that dir, so it survives.
            on_state(State.KILLING)
            if self.kill_session_fn is not None:
                try:
                    self.kill_session_fn()
                    on_log("Killed live session → dormant.")
                except Exception as e:  # noqa: BLE001
                    on_log(f"WARN: kill_session raised: {e!r}")
            else:
                on_log("Session was already dormant; no kill needed.")

            if self._abort_check(on_state, on_done, "after kill"):
                return

            # 2. Sync full.jsonl from live (idempotent, bootstraps if needed).
            on_state(State.SYNCING)
            try:
                appended = sync_full(self.claude_uuid, self.cwd)
                if appended:
                    on_log(f"Synced {appended} new row(s) into full.jsonl.")
                else:
                    on_log("full.jsonl already up-to-date.")
            except Exception as e:  # noqa: BLE001
                on_state(State.FAILED)
                on_done(False, f"sync_full failed: {e}")
                return

            full = full_jsonl_path(self.claude_uuid)
            if not full.is_file():
                on_state(State.FAILED)
                on_done(False, f"full.jsonl missing at {full}")
                return

            # 3. Total turn count from full JSONL — the denominator for
            # the progress bar.
            try:
                total_turns = self._count_total_turns(full)
            except Exception as e:  # noqa: BLE001
                on_state(State.FAILED)
                on_done(False, f"parse_session failed: {e}")
                return
            on_log(f"Full session has {total_turns} turn(s).")
            on_progress(self._count_cached_trees(), total_turns)

            if self._abort_check(on_state, on_done, "before build"):
                return

            # 4. If an existing worker is already running (almost certainly
            # the auto-worker triggered by the last UserPromptSubmit, which
            # skips the in-progress last turn), wait for it to finish
            # before we spawn our own.
            existing_pid = self._read_lock_pid()
            if existing_pid and self._alive(existing_pid):
                on_state(State.WAITING_AUTO)
                on_log(f"Existing worker (PID {existing_pid}) is running; "
                       f"tailing its log until it finishes...")
                self._tail_log_until_pid_dies(
                    existing_pid, on_log, on_progress, total_turns)
                if self.cancelled:
                    self._abort_now(on_state, on_done, "while waiting on auto-worker")
                    return

            # 5. Now check completion. If still missing turns, spawn an
            # all-turns worker (which is cache-aware and only builds what's
            # missing — typically just the last turn after the auto-worker).
            built = self._count_cached_trees()
            on_progress(built, total_turns)
            if built >= total_turns:
                on_log("All trees already built; skipping further worker.")
            else:
                on_state(State.BUILDING)
                missing = total_turns - built
                on_log(f"{missing} turn(s) still missing trees; "
                       f"spawning --all-turns worker.")
                proc = self._spawn_all_turns_worker()
                if proc is None:
                    on_state(State.FAILED)
                    on_done(False, "failed to spawn worker")
                    return
                self._tail_log_until_pid_dies(
                    proc.pid, on_log, on_progress, total_turns)
                if self.cancelled:
                    self._abort_now(on_state, on_done, "while building remaining")
                    return

            built = self._count_cached_trees()
            on_progress(built, total_turns)
            if built < total_turns:
                on_state(State.FAILED)
                on_done(
                    False,
                    f"finished but only {built}/{total_turns} trees built — "
                    f"see worker.log for details.",
                )
                return

            on_state(State.DONE)
            on_done(True, f"All {total_turns} tree(s) built.")
        finally:
            release_lock(self.claude_uuid)

    # ---------------------------------------------------------------- private

    def _abort_check(self, on_state, on_done, context: str) -> bool:
        if self.cancelled:
            self._abort_now(on_state, on_done, context)
            return True
        return False

    def _abort_now(self, on_state, on_done, context: str) -> None:
        on_state(State.ABORTED)
        on_done(False, f"Aborted by user ({context}).")

    def _count_total_turns(self, full: Path) -> int:
        # Lazy import; vendor is on sys.path.
        from compaction.parser import parse_session  # noqa: E402
        return len(parse_session(str(full)))

    def _count_cached_trees(self) -> int:
        if not self.paths.cache_root.is_dir():
            return 0
        n = 0
        for p in self.paths.cache_root.glob("*.json"):
            try:
                if p.stat().st_size > 0:
                    n += 1
            except OSError:
                continue
        return n

    def _read_lock_pid(self) -> Optional[int]:
        try:
            raw = self.paths.lockfile.read_text().strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            return int(raw.split()[0])
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _tail_log_until_pid_dies(
        self,
        pid: int,
        on_log: LogCb,
        on_progress: ProgCb,
        total_turns: int,
    ) -> None:
        """Stream new log lines + emit progress updates until PID is dead.

        Polls at 0.5s. Reads only the new tail of the log on each pass,
        based on a stored byte offset. Exits when the PID is no longer
        alive — caller is responsible for the success/failure verdict
        based on cache contents.
        """
        # If logs already exist from earlier runs, fast-forward our
        # offset to the end so we only stream genuinely new lines.
        if self._log_offset == 0:
            try:
                self._log_offset = self.paths.log.stat().st_size
            except OSError:
                self._log_offset = 0

        while True:
            self._stream_new_log_lines(on_log)
            on_progress(self._count_cached_trees(), total_turns)
            if not self._alive(pid):
                # One more drain after death in case a tail line landed
                # between the last poll and the PID exiting.
                self._stream_new_log_lines(on_log)
                on_progress(self._count_cached_trees(), total_turns)
                return
            if self.cancelled:
                return
            time.sleep(0.5)

    def _stream_new_log_lines(self, on_log: LogCb) -> None:
        log = self.paths.log
        if not log.is_file():
            return
        try:
            size = log.stat().st_size
        except OSError:
            return
        if size <= self._log_offset:
            if size < self._log_offset:
                # Log was rotated/truncated — start from the top.
                self._log_offset = 0
            else:
                return
        try:
            with log.open() as f:
                f.seek(self._log_offset)
                chunk = f.read()
                self._log_offset = f.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            if line.strip():
                on_log(line)

    def _spawn_all_turns_worker(self) -> Optional[subprocess.Popen]:
        worker = _PLUGIN / "scripts" / "tree-builder-worker.py"
        if not worker.is_file():
            return None
        try:
            log_f = self.paths.log.open("ab")
        except OSError:
            return None
        try:
            proc = subprocess.Popen(
                [
                    sys.executable, str(worker),
                    str(self.live_jsonl),
                    self.claude_uuid,
                    self.cwd,
                    "--all-turns",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
            )
        finally:
            log_f.close()
        try:
            self.paths.lockfile.write_text(f"{proc.pid} all")
        except OSError:
            pass
        return proc
