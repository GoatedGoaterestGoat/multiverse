#!/usr/bin/env python3
"""Long-running worker: build compaction trees for completed turns.

Source of truth is the FULL JSONL at ``~/.multiverse/full_jsonls/<uuid>.jsonl``,
not the live JSONL — the live one may have already been compacted, in which
case its summary blocks would produce nonsense trees. We bootstrap full
from live on first use (so legacy sessions Just Work).

Modes:

  --skip-last (default)
      Build trees for turns 0..N-2 only. Used by the auto-spawn hook on
      UserPromptSubmit, where the last turn is the one the user just
      submitted (assistant content not yet generated).

  --all-turns
      Build trees for every turn including the last. Used by the manual
      Compact widget, after killing the session so no more turns can be
      added mid-build.

Sets MULTIVERSE_TREE_BUILDER_ACTIVE=1 in the spawned subprocess
environment so the synthetic ``claude --resume <id>`` calls the
constructor makes don't re-enter the UserPromptSubmit hook (which would
otherwise spawn another worker and recurse).

Files:
  cache: ~/.multiverse/trees/<session_id>/<turn_id>.json
  lock:  ~/.multiverse/trees/<session_id>/.worker.lock   (PID + " all"
                                                          if --all-turns)
  log:   ~/.multiverse/trees/<session_id>/worker.log

Usage:
  tree-builder-worker.py LIVE_JSONL_PATH SESSION_ID CWD [--all-turns]

LIVE_JSONL_PATH is taken so we can bootstrap full.jsonl on the first run
when only the live one exists.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Make the vendored compaction package + the plugin's `src.full_jsonl`
# helpers importable. Both are needed: the parser to enumerate turns, and
# full_jsonl.sync_full to point the build at the always-uncompacted source.
_PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN / "vendor"))
sys.path.insert(0, str(_PLUGIN))

from compaction.parser import parse_session  # noqa: E402
from src.full_jsonl import (  # noqa: E402
    full_jsonl_path,
    sync_full,
)


def _multiverse_dir() -> Path:
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.multiverse"))


def _log(log_path: Path, msg: str) -> None:
    try:
        with log_path.open("a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _is_valid_cached_tree(path: Path) -> bool:
    """True if an existing ``<turn_id>.json`` represents a real cache hit.

    Must match ``compact_runner._count_cached_trees``'s notion of "built",
    or the runner and this worker disagree forever: a turn whose tree is a
    dict counts unless it's the constructor's aborted-stub placeholder
    (build failed partway); a turn with ``tree: null`` counts only if
    ``n_leaves == 0`` (nothing to build — the correct terminal state for
    an empty turn, not an unbuilt one). Anything else (corrupt JSON,
    missing tree with leaves present) is treated as not-yet-built so it
    gets rebuilt below instead of being skipped forever.

    The aborted-stub check is an EXACT match against the constructor's
    literal placeholder string, not a substring check — a genuinely-built
    summary can legitimately discuss aborted-stub trees as content (e.g.
    a turn where the agent debugged this very compaction feature) without
    being one itself. A substring match would misclassify that turn as
    unbuilt on every single retry, burning a real rebuild attempt each
    time with no way to ever pass.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    tree = data.get("tree")
    if tree is None:
        return data.get("n_leaves") == 0
    if not isinstance(tree, dict):
        return False
    summary = tree.get("summary", "")
    return summary != "<construction aborted; uncollapsed frontier>"


def main() -> int:
    args = sys.argv[1:]
    all_turns = False
    if "--all-turns" in args:
        all_turns = True
        args = [a for a in args if a != "--all-turns"]

    if len(args) != 3:
        print("usage: tree-builder-worker.py LIVE_JSONL SESSION_ID CWD "
              "[--all-turns]", file=sys.stderr)
        return 2

    live = Path(args[0])
    session_id = args[1]
    cwd = args[2]

    cache_root = _multiverse_dir() / "trees" / session_id
    cache_root.mkdir(parents=True, exist_ok=True)
    lockfile = cache_root / ".worker.lock"
    log = cache_root / "worker.log"

    # Take the lock (overwriting any stale value; spawn-tree-builder
    # already filtered out live workers).
    lock_payload = f"{os.getpid()}{' all' if all_turns else ''}"
    try:
        lockfile.write_text(lock_payload)
    except OSError as e:
        _log(log, f"could not write lockfile: {e}")
        return 1

    try:
        return _run(live, session_id, cwd, cache_root, log, all_turns)
    finally:
        try:
            lockfile.unlink()
        except OSError:
            pass


def _run(live: Path, session_id: str, cwd: str, cache_root: Path,
         log: Path, all_turns: bool) -> int:
    builder = _PLUGIN / "scripts" / "build_tree_priv_cli.py"
    if not builder.is_file():
        _log(log, f"build_tree_priv_cli.py not found at {builder}; abort")
        return 1

    # Sync the full JSONL up to the current live state (or bootstrap it
    # if it doesn't exist yet — legacy support). Source for the rest of
    # the build is the full JSONL.
    try:
        appended = sync_full(session_id, cwd)
    except Exception as e:  # noqa: BLE001
        _log(log, f"sync_full failed: {e}")
        return 1
    if appended:
        _log(log, f"synced {appended} new row(s) into full.jsonl before "
                  f"building")

    full = full_jsonl_path(session_id)
    if not full.is_file():
        _log(log, f"full JSONL still missing at {full} (live={live} "
                  f"exists={live.is_file()}); abort")
        return 1

    try:
        # strip command turns — /context, /model, /effort turns are
        # dropped from the compaction pipeline, so building trees for
        # them just wastes API calls and produces orphaned cache files
        # that never get used.
        forest = parse_session(str(full), strip_command_turns_=True)
    except (OSError, ValueError) as e:
        _log(log, f"parse_session failed: {e}")
        return 1

    if not forest:
        _log(log, "parsed forest is empty; nothing to build")
        return 0

    if all_turns:
        target_ids = [t.turn_id for t in forest]
        mode_label = "ALL turns"
    else:
        if len(forest) <= 1:
            _log(log, f"only {len(forest)} turn(s) parsed; nothing to build "
                      f"(skip in-progress current turn)")
            return 0
        target_ids = [t.turn_id for t in forest[:-1]]
        mode_label = f"turns 0..{len(target_ids) - 1} (skip in-progress last)"

    _log(log, f"session {session_id[:8]} | {len(forest)} turns total | "
              f"target: {mode_label}")

    # Recursion guard for the constructor's `claude --resume` subprocesses.
    env = os.environ.copy()
    env["MULTIVERSE_TREE_BUILDER_ACTIVE"] = "1"

    src_path = _PLUGIN / "vendor"
    env["PYTHONPATH"] = (
        f"{src_path}:{env['PYTHONPATH']}" if env.get("PYTHONPATH")
        else str(src_path)
    )

    built = skipped = failed = 0
    for turn_idx, pid in enumerate(target_ids):
        out_file = cache_root / f"{pid}.json"
        if out_file.exists() and out_file.stat().st_size > 0:
            if _is_valid_cached_tree(out_file):
                _log(log, f"turn {turn_idx} ({pid[:8]}) — cache hit, skip")
                skipped += 1
                continue
            _log(log, f"turn {turn_idx} ({pid[:8]}) — stale/invalid cache "
                      f"(aborted stub or corrupt), rebuilding")

        _log(log, f"turn {turn_idx} ({pid[:8]}) — building...")
        t0 = time.time()
        try:
            proc = subprocess.run(
                [
                    sys.executable, str(builder),
                    str(full), str(turn_idx),
                    "--out", str(out_file),
                    "--model", "sonnet",
                ],
                env=env,
                capture_output=True, text=True,
                timeout=3600,
            )
        except subprocess.TimeoutExpired:
            _log(log, f"  turn {turn_idx} TIMEOUT (>1h)")
            failed += 1
            continue
        elapsed = time.time() - t0

        if proc.returncode != 0:
            _log(log, f"  turn {turn_idx} FAIL (exit {proc.returncode}, "
                      f"{elapsed:.1f}s)")
            for line in proc.stderr.strip().splitlines()[-5:]:
                _log(log, f"    | {line}")
            # Don't leave a partial output on disk.
            try:
                if out_file.exists():
                    out_file.unlink()
            except OSError:
                pass
            failed += 1
            continue

        _log(log, f"  turn {turn_idx} done ({elapsed:.1f}s)")
        built += 1

    _log(log, f"worker finished | built={built} skipped={skipped} "
              f"failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
