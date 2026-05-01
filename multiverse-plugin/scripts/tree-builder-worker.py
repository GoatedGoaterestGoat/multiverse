#!/usr/bin/env python3
"""Long-running worker: build compaction trees for completed turns.

Spawned (detached) by spawn-tree-builder.py on UserPromptSubmit. Iterates
the source JSONL's turns in order, skips the LAST turn (which is the
in-progress one the user just submitted), and for each remaining turn
that doesn't already have a cached tree, runs:

    python3 compaction/scripts/build_tree_priv_cli.py SOURCE TURN_IDX --out CACHE.json

Sets MULTIVERSE_TREE_BUILDER_ACTIVE=1 in the spawned subprocess
environment so the synthetic `claude --resume <id>` calls the constructor
makes don't re-enter the UserPromptSubmit hook (which would otherwise
spawn another worker and recurse).

Files:
  cache: ~/.multiverse/trees/<session_id>/<turn_id>.json
  lock:  ~/.multiverse/trees/<session_id>/.worker.lock   (PID)
  log:   ~/.multiverse/trees/<session_id>/worker.log
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Make the vendored compaction package importable so we can use the same
# turn-boundary logic as the builder, ensuring our turn_idx values match.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from compaction.parser import parse_session  # noqa: E402


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


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: tree-builder-worker.py SOURCE.jsonl SESSION_ID CWD",
              file=sys.stderr)
        return 2

    source = Path(sys.argv[1])
    session_id = sys.argv[2]
    cwd = sys.argv[3]

    cache_root = _multiverse_dir() / "trees" / session_id
    cache_root.mkdir(parents=True, exist_ok=True)
    lockfile = cache_root / ".worker.lock"
    log = cache_root / "worker.log"

    # Take the lock (overwriting any stale value; spawn-tree-builder
    # already filtered out live workers).
    try:
        lockfile.write_text(str(os.getpid()))
    except OSError as e:
        _log(log, f"could not write lockfile: {e}")
        return 1

    try:
        return _run(source, session_id, cwd, cache_root, log)
    finally:
        try:
            lockfile.unlink()
        except OSError:
            pass


def _run(source: Path, session_id: str, cwd: str,
         cache_root: Path, log: Path) -> int:
    plugin_root = Path(__file__).resolve().parent.parent
    builder = plugin_root / "scripts" / "build_tree_priv_cli.py"
    if not builder.is_file():
        _log(log, f"build_tree_priv_cli.py not found at {builder}; abort")
        return 1

    try:
        forest = parse_session(str(source))
    except (OSError, ValueError) as e:
        _log(log, f"parse_session failed: {e}")
        return 1

    if len(forest) <= 1:
        _log(log, f"only {len(forest)} turn(s) parsed; nothing to build "
                  f"(skip in-progress current turn)")
        return 0

    completed_ids = [t.turn_id for t in forest[:-1]]  # drop in-progress
    n_total = len(completed_ids)
    _log(log, f"session {session_id[:8]} | {len(forest)} turns total | "
              f"will build trees for completed turns 0..{n_total - 1}")

    # Recursion guard for the constructor's `claude --resume` subprocesses.
    env = os.environ.copy()
    env["MULTIVERSE_TREE_BUILDER_ACTIVE"] = "1"

    src_path = plugin_root / "vendor"
    env["PYTHONPATH"] = (
        f"{src_path}:{env['PYTHONPATH']}" if env.get("PYTHONPATH")
        else str(src_path)
    )

    built = skipped = failed = 0
    for turn_idx, pid in enumerate(completed_ids):
        out_file = cache_root / f"{pid}.json"
        if out_file.exists() and out_file.stat().st_size > 0:
            _log(log, f"turn {turn_idx} ({pid[:8]}) — cache hit, skip")
            skipped += 1
            continue

        _log(log, f"turn {turn_idx} ({pid[:8]}) — building...")
        t0 = time.time()
        try:
            proc = subprocess.run(
                [
                    sys.executable, str(builder),
                    str(source), str(turn_idx),
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
