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
from .full_jsonl import (
    full_jsonl_path,
    last_uuid_in,
    live_jsonl_path,
    sync_full,
    write_marker,
)
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
    EXPANDING = "expanding"
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
        total_budget: int,
        kill_session_fn: Optional[Callable[[], None]] = None,
        summarize_only: bool = False,
        expand_only: bool = False,
        scheduler_name: str = "proportional",
        scheduler_params: Optional[dict] = None,
        image_mode: str = "budget_fit",
    ) -> None:
        self.claude_uuid = claude_uuid
        self.cwd = cwd
        self.live_jsonl = live_jsonl
        self.total_budget = total_budget
        self.kill_session_fn = kill_session_fn
        # Summarize-only mode: run every step through tree-build, then
        # STOP — do not expand + rewrite live. Useful for pre-warming
        # the tree cache before an eventual compaction. The compacting
        # lock still gates the session (so no new turns land mid-build).
        self.summarize_only = summarize_only
        # Expand-only mode: skip kill / sync / tree-build entirely.
        # Assumes trees are already cached (typically because a prior
        # summarize-only run just built them) and jumps straight to
        # expansion + live rewrite. Two-phase compaction flow calls
        # this after the user has picked a scheduler + budget.
        self.expand_only = expand_only
        # Scheduler selection for the expansion phase. proportional /
        # recency_boosted / last_k_raw; params dict carries scheduler-
        # specific args (``k`` for last_k_raw; ``recency_ratio`` +
        # ``recency_half_life`` for recency_boosted). Ignored for
        # schedulers that take no params.
        self.scheduler_name = scheduler_name
        self.scheduler_params = dict(scheduler_params or {})
        # Image mode passed straight to allocate_budget. "keep_all" /
        # "drop_all" now come from the picker; "budget_fit" (default)
        # preserves the legacy newest-first behaviour for any caller
        # that hasn't opted into the explicit toggle.
        self.image_mode = image_mode
        self.paths = CompactPaths.for_session(claude_uuid)
        # Populated by _count_total_turns so _count_cached_trees can
        # filter stale tree files (from earlier chain states, reverted
        # turns, etc.) out of the "built" number. Without this the
        # count could exceed total turns.
        self._chain_turn_ids: Optional[set[str]] = None
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

        # Ensure the per-session cache + log dir exists before any worker
        # tries to open files inside it.
        try:
            self.paths.cache_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            release_lock(self.claude_uuid)
            on_state(State.FAILED)
            on_done(False, f"could not create cache dir: {e}")
            return

        try:
            # Expand-only fast path: trees already built, session already
            # dormant (a prior summarize-only run left it that way), just
            # jump to expansion. Skips kill / sync / count / build.
            if self.expand_only:
                full = full_jsonl_path(self.claude_uuid)
                if not full.is_file():
                    on_state(State.FAILED)
                    on_done(False, f"full.jsonl missing at {full}")
                    return
                on_state(State.EXPANDING)
                try:
                    rendered = self._expand_and_render(full, on_log)
                except Exception as e:  # noqa: BLE001
                    on_state(State.FAILED)
                    on_done(False, f"expansion failed: {e}")
                    return
                total_turns = self._count_total_turns(full)
                on_state(State.DONE)
                on_done(
                    True,
                    f"Compacted to budget {self.total_budget:,}t across "
                    f"{total_turns} turn(s); wrote {rendered} row(s) to live.",
                )
                return

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

            # 6. Expansion: load trees from cache, allocate budget
            # proportionally, render the new compacted live JSONL, replace
            # live atomically, update marker. Fast (no API calls).
            # Skipped entirely in summarize-only mode — the trees are the
            # deliverable there; live stays untouched.
            if self.summarize_only:
                on_state(State.DONE)
                on_log(
                    "Summarize-only: skipping expansion; live JSONL "
                    "unchanged."
                )
                on_done(
                    True,
                    f"Built trees for {total_turns} turn(s). Live JSONL "
                    f"unchanged (summarize-only mode).",
                )
                return

            if self._abort_check(on_state, on_done, "before expansion"):
                return
            on_state(State.EXPANDING)
            try:
                rendered = self._expand_and_render(full, on_log)
            except Exception as e:  # noqa: BLE001
                on_state(State.FAILED)
                on_done(False, f"expansion failed: {e}")
                return

            on_state(State.DONE)
            on_done(
                True,
                f"Compacted to budget {self.total_budget:,}t across "
                f"{total_turns} turn(s); wrote {rendered} row(s) to live.",
            )
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
        from compaction.parser import compaction_forest  # noqa: E402
        # The canonical compaction view (command turns stripped,
        # non-compactable leaves filtered, empty turns merged) so the
        # turn count / turn_ids here match exactly what the tree builder
        # and the main run() path operate on.
        forest = compaction_forest(str(full))
        # Cache turn_ids for _count_cached_trees's chain filter.
        self._chain_turn_ids = {t.turn_id for t in forest}
        return len(forest)

    def _count_cached_trees(self) -> int:
        """Count VALID cached tree files for the CURRENT chain.

        A file counts only if:
        - its filename (``<turn_id>.json``) is a turn_id in the chain
          (or the chain set hasn't been populated yet — edge case
          before ``_count_total_turns`` runs);
        - it opens cleanly as JSON;
        - EITHER the ``tree`` field is a dict that isn't an
          aborted-construction stub (constructor's synthetic wrapper for
          turns whose build failed at step 0), OR ``tree`` is ``null``
          with ``n_leaves == 0`` (a turn with no assistant leaves at
          all — construct_turn_priv_cli returns immediately without
          setting turn.root, which is the correct terminal state, not
          an unbuilt one).

        Aborted stubs are the gotcha: ``_attach_trees_from_cache``
        treats them as "no tree" and the turn falls back to
        greedy-leaves-from-start at expansion time. If those stubs
        counted as built here, the runner's ``if built >= total_turns``
        gate would skip spawning the worker — leaving those turns
        forever stuck in the greedy fallback, whose first-leaf
        verbatim behaviour balloons any turn whose first leaf is a
        big tool_result.
        """
        if not self.paths.cache_root.is_dir():
            return 0
        valid = self._chain_turn_ids
        import json  # local; we now open each candidate file
        n = 0
        for p in self.paths.cache_root.glob("*.json"):
            try:
                if p.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            if valid is not None and p.stem not in valid:
                continue
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            tree = data.get("tree")
            if tree is None:
                # A turn with zero assistant leaves legitimately has no
                # tree — construct_turn_priv_cli returns immediately
                # ("empty turn") without ever setting turn.root, so
                # tree=None here means "correctly built, nothing to
                # summarize", not "never built". A genuine build failure
                # always produces the aborted-stub Node below instead,
                # never a bare null. Confirm via n_leaves rather than
                # trusting tree=None alone, in case of a truncated write.
                if data.get("n_leaves") == 0:
                    n += 1
                continue
            if not isinstance(tree, dict):
                continue
            if _is_aborted_stub(tree):
                continue
            n += 1
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
        """Stream new log lines + emit progress updates until the worker
        is gone.

        "Gone" is detected by EITHER the PID being dead OR the worker's
        lockfile being absent. The lockfile check is the load-bearing
        one: ``os.kill(pid, 0)`` returns success forever if the OS
        recycled the worker's PID for an unrelated process — which can
        happen on long builds — and would otherwise spin this loop
        indefinitely. The worker unlinks its lockfile in a ``finally``
        block, so the file's absence is an authoritative end-of-life
        signal.

        Polls at 0.5s. Reads only the new tail of the log on each pass,
        based on a stored byte offset.
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
            worker_done = (
                not self._alive(pid)
                or not self.paths.lockfile.is_file()
            )
            if worker_done:
                # One more drain after death in case a tail line landed
                # between the last poll and the worker exiting.
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

    # ---------------------------- expansion + live-JSONL replace ----------

    def _expand_and_render(self, full: Path, on_log: LogCb) -> int:
        """Expand each turn's tree to its proportional share of the
        budget, render the compacted forest as a session JSONL, replace
        live atomically, update the mirror marker.

        Returns the number of rows written to live.
        """
        # Lazy imports — vendor is on sys.path.
        from compaction.compactor import (  # noqa: E402
            SCHEDULERS,
            allocate_budget,
            proportional,
        )
        from compaction.parser import (  # noqa: E402
            filter_compactable,
            merge_empty_turns,
            parse_session,
        )
        from compaction.priv_session import (  # noqa: E402
            compact_forest_to_session_jsonl,
        )

        # Two compaction-only filters applied BEFORE any downstream
        # step sees the forest:
        #   - strip_command_turns_ drops /context, /model, /effort
        #     turns so they don't get summarized into tree nodes and
        #     don't appear in the compacted output — those tokens are
        #     transient display cost and shouldn't be reserved against
        #     the budget's un-compactable input floor.
        #   - filter_compactable strips display-only attachment leaves
        #     (task reminders, skill listings, tool/agent/MCP deltas,
        #     file-edit snapshots, misc hook context) so the pipeline
        #     doesn't render them as user-role text blocks in the
        #     output.
        # Both categories' tokens are already booked into live_stats
        # (unfiltered parser output) for accurate dashboard display;
        # they get netted out of structural via the derivation delta.
        raw_forest = parse_session(str(full), strip_command_turns_=True)
        # Capture the source_uuids of the leaves filter_compactable will
        # drop — used below to detect stale cached trees that were built
        # BEFORE the whitelist filter existed (they still contain these
        # non-compactable leaves baked in, and would leak them into the
        # compacted output as user-role text blocks).
        noncompactable_uuids: set[str] = set()
        for turn in raw_forest:
            for leaf in turn.leaves:
                if not leaf.compactable:
                    noncompactable_uuids.update(leaf.source_uuids)
        # filter non-compactable leaves, then merge any now-empty turns
        # forward so the compacted output has no stranded empty turns.
        forest = merge_empty_turns(filter_compactable(raw_forest))
        if not forest:
            raise RuntimeError("forest is empty after parse_session")

        n_with, n_without = self._attach_trees_from_cache(
            forest, noncompactable_uuids
        )
        on_log(
            f"Loaded trees: {n_with} attached from cache, {n_without} "
            f"will fall back to greedy-from-start.",
        )

        # Resolve the scheduler by name (falls back to proportional).
        # scheduler_params carries any scheduler-specific extras (``k``
        # for last_k_raw, ``recency_ratio``/``recency_half_life`` for
        # recency_boosted); we bind them
        # by wrapping in a lambda so allocate_budget's call signature
        # stays uniform.
        base_scheduler = SCHEDULERS.get(self.scheduler_name, proportional)
        params = self.scheduler_params
        if params:
            def scheduler(forest_, budget_, _fn=base_scheduler, _p=params):
                return _fn(forest_, budget_, **_p)
        else:
            scheduler = base_scheduler

        # Whole-output allocation: reserves the verbatim user-prompt text
        # floor, keeps as many user-prompt images as fit a sub-budget
        # (newest first), and divides the rest across turns for assistant
        # content. Without this, verbatim user images ride along outside the
        # budget and a 400k compaction can still emit 800k+ tokens.
        per_turn, keep_image_turn_ids = allocate_budget(
            forest, self.total_budget, scheduler=scheduler,
            image_mode=self.image_mode,
        )
        n_img_turns = sum(
            1 for t in forest if t.user_prompt and t.user_prompt.images
        )
        if n_img_turns:
            if self.image_mode == "keep_all":
                mode_note = "keep_all mode"
            elif self.image_mode == "drop_all":
                mode_note = "drop_all mode"
            else:
                mode_note = "budget_fit mode"
            on_log(
                f"User-prompt images ({mode_note}): keeping "
                f"{len(keep_image_turn_ids)} of {n_img_turns} "
                f"image-bearing turn(s); the rest are replaced by "
                f"a placeholder."
            )
        # Log per-turn budget allocation. Cap output at first 10 turns to
        # keep the log readable on long sessions; full mapping is in the
        # rendered JSONL via the rows themselves.
        on_log(
            f"Allocated {self.total_budget:,}t across {len(forest)} "
            f"turn(s) by {self.scheduler_name} scheduler{f' {self.scheduler_params}' if self.scheduler_params else ''}:"
        )
        preview_turns = forest[:10]
        for turn in preview_turns:
            full_t = (turn.user_prompt.tokens if turn.user_prompt else 0) + sum(
                l.tokens for l in turn.leaves
            )
            allocated = per_turn.get(turn.turn_id, 0)
            ratio = (allocated / full_t * 100.0) if full_t else 0.0
            on_log(
                f"  {turn.turn_id[:8]}  full={full_t:>6,}t  "
                f"budget={allocated:>6,}t  ({ratio:5.1f}%)"
            )
        if len(forest) > 10:
            on_log(f"  … and {len(forest) - 10} more turn(s)")

        title = self._read_existing_title() or "compacted"
        jsonl_text = compact_forest_to_session_jsonl(
            forest,
            per_turn,
            session_id=self.claude_uuid,
            cwd=self.cwd,
            title=title,
            keep_image_turn_ids=keep_image_turn_ids,
        )

        # Atomic replace: temp file in the same directory, then os.replace.
        live = live_jsonl_path(self.claude_uuid, self.cwd)
        live.parent.mkdir(parents=True, exist_ok=True)
        import tempfile  # local import; only used here
        fd, tmp = tempfile.mkstemp(
            prefix=".compact_", suffix=".jsonl", dir=str(live.parent)
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(jsonl_text)
            os.replace(tmp, live)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        # Refresh the mirror marker so the next mirror call only mirrors
        # rows added AFTER the new compacted prefix. Without this, the
        # mirror would walk the new live looking for the old marker uuid
        # (gone) and silently mirror nothing forever.
        new_last = last_uuid_in(live)
        if new_last:
            write_marker(self.claude_uuid, new_last)
            on_log(f"Updated mirror marker → {new_last[:8]}.")

        n_rows = jsonl_text.count("\n")
        on_log(
            f"Wrote new compacted live JSONL: {n_rows} row(s), "
            f"{len(jsonl_text):,} bytes (was uncompacted)."
        )
        return n_rows

    def _attach_trees_from_cache(
        self, forest, noncompactable_uuids: Optional[set] = None
    ) -> tuple[int, int]:
        """Walk the forest, attach turn.root from cache when available.

        Returns (n_with_tree, n_without_tree). Turns without a cached
        tree (or with an aborted-construction stub — a synthetic root
        whose summary is just ``<construction aborted; uncollapsed
        frontier>``) fall back to greedy-leaves-from-start during
        rendering. The fallback produces real readable content; the
        aborted-stub would render as an empty bullet because its
        placeholder summary is itself an angle-bracket-wrapped string
        that gets stripped by claude code's markdown renderer.

        ``noncompactable_uuids`` — source_uuids of leaves the current
        compaction view filters out. A cached tree that references any
        of them was built BEFORE the whitelist filter existed and still
        has non-compactable content (edited_text_file, task_reminder,
        etc.) baked in; using it would leak that content into the
        compacted output as a user-role text block. Such a tree is
        rejected (deleted so it rebuilds clean, treated as no-tree for
        this run — the turn falls back to greedy-from-filtered-leaves,
        which is correct because the bad leaves are already gone from
        turn.leaves).
        """
        import json  # local import — only this method needs it
        bad_uuids = noncompactable_uuids or set()
        n_with = n_without = 0
        for turn in forest:
            path = self.paths.cache_root / f"{turn.turn_id}.json"
            tree_data = None
            if path.is_file():
                try:
                    data = json.loads(path.read_text())
                    tree_data = data.get("tree")
                except (OSError, json.JSONDecodeError):
                    tree_data = None
            if tree_data is None or _is_aborted_stub(tree_data):
                n_without += 1
                continue
            try:
                root = _reconstruct_tree(tree_data)
            except (KeyError, TypeError):
                n_without += 1
                continue
            # Reject stale trees that still carry now-filtered content.
            if bad_uuids and (_tree_source_uuids(root) & bad_uuids):
                try:
                    path.unlink()  # force a clean rebuild next time
                except OSError:
                    pass
                n_without += 1
                continue
            turn.root = root
            n_with += 1
        return n_with, n_without

    def _read_existing_title(self) -> Optional[str]:
        """Return the last ``custom-title`` value from the existing live
        JSONL, so the compacted replacement keeps the same name. Falls
        back to full.jsonl if live is missing — this is the restore
        path where we're re-materializing a live from an orphaned full.
        """
        import json  # local — narrow blast radius
        candidates = [
            live_jsonl_path(self.claude_uuid, self.cwd),
            full_jsonl_path(self.claude_uuid),
        ]
        for path in candidates:
            if not path.is_file():
                continue
            title: Optional[str] = None
            try:
                with path.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if d.get("type") == "custom-title":
                            ct = d.get("customTitle")
                            if isinstance(ct, str) and ct:
                                title = ct
            except OSError:
                continue
            if title:
                return title
        return None


_ABORTED_STUB_SUMMARY = "<construction aborted; uncollapsed frontier>"


def _is_aborted_stub(td: dict) -> bool:
    """True if the cached tree is just the constructor's
    'aborted-at-step-0' synthetic wrapper — a single Node whose summary
    is EXACTLY the placeholder ``<construction aborted; uncollapsed
    frontier>`` (see ``constructor.py``, where this string is the sole
    literal value ever assigned to a stub's ``summary``).

    Must be an exact match, not a substring check: a genuinely-built
    summary can legitimately *discuss* aborted-stub trees as content
    (e.g. a turn where the agent debugged this very compaction feature)
    and would then contain the phrase "construction aborted" without
    being one itself. A substring match misclassifies that turn as
    unbuilt forever, wasting a real rebuild attempt every retry with no
    way to ever pass.

    These trees provide no real summarization (the merge LLM never
    produced a usable response). Treat them as 'no tree' so expansion
    falls back to greedy leaves-from-start, which renders real content
    instead of an angle-bracket-only string that the markdown UI
    strips to nothing.
    """
    if not isinstance(td, dict):
        return False
    if td.get("type") != "node":
        return False
    summary = td.get("summary", "")
    return summary == _ABORTED_STUB_SUMMARY


def _tree_source_uuids(node) -> set:
    """Collect every leaf source_uuid referenced anywhere in a
    reconstructed tree (Leaf/Node). Used to detect stale trees that
    still reference now-filtered leaves."""
    out: set = set()
    stack = [node]
    while stack:
        n = stack.pop()
        children = getattr(n, "children", None)
        if children:
            stack.extend(children)
        else:  # leaf
            out.update(getattr(n, "source_uuids", []) or [])
    return out


def _reconstruct_tree(d: dict):
    """Rebuild a Leaf or Node from its serialized form (the same shape
    that ``build_tree_priv_cli.py`` writes to its tree.json files)."""
    from compaction.types import Leaf, Node  # noqa: E402
    if d.get("type") == "leaf":
        return Leaf(
            kind=d["kind"],
            content=d.get("content", {}),
            tokens=d["tokens"],
            preview=d.get("preview", ""),
            source_uuids=d.get("source_uuids", []),
            images=d.get("images", []),
        )
    if d.get("type") == "node":
        return Node(
            id=d["id"],
            children=[_reconstruct_tree(c) for c in d.get("children", [])],
            summary=d.get("summary", ""),
            tokens=d["tokens"],
            preview=d.get("preview", ""),
        )
    raise TypeError(f"unknown tree node type: {d.get('type')!r}")
