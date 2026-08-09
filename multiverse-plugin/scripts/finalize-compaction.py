#!/usr/bin/env python3
"""One-shot finalize: run the expansion + live-replace step for a session
whose tree-build is already complete.

Use this when the Compact widget got stuck after tree-build (UI still
reads 'Esc to abort' but every turn has a cached tree). The script does
the same thing the runner's ``_expand_and_render`` would have done:
parse the full JSONL, attach trees from cache, allocate budget
proportionally, render the compacted JSONL via
``compact_forest_to_session_jsonl``, atomically replace live, and
update the mirror marker.

Quit the monitor first (so the orphaned runner thread can't race with
this script). Force-releases the compacting lock at the end.

Usage:
    PYTHONPATH=multiverse-plugin uv run python \
        multiverse-plugin/scripts/finalize-compaction.py \
        <claude_uuid> <cwd> <total_budget_tokens>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN))
sys.path.insert(0, str(_PLUGIN / "vendor"))

from src.compact_runner import (  # noqa: E402
    _collect_state_rows,
    _is_aborted_stub,
    _reconstruct_tree,
    splice_state_rows,
)
from src.compact_state import lock_path, release_lock  # noqa: E402
from src.full_jsonl import (  # noqa: E402
    full_jsonl_path,
    last_uuid_in,
    live_jsonl_path,
    write_marker,
)
from src.paths import multiverse_dir  # noqa: E402
from compaction.compactor import allocate_budget, proportional  # noqa: E402
from compaction.parser import parse_session  # noqa: E402
from compaction.priv_session import compact_forest_to_session_jsonl  # noqa: E402


def _read_existing_title(live: Path) -> str | None:
    if not live.is_file():
        return None
    title: str | None = None
    with live.open() as f:
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
    return title


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("claude_uuid")
    p.add_argument("cwd")
    p.add_argument("budget", type=int, help="total token budget")
    p.add_argument("--force", action="store_true",
                   help="release the compacting lock even if its monitor PID looks alive")
    args = p.parse_args()

    uuid = args.claude_uuid
    cwd = args.cwd
    budget = args.budget

    lp = lock_path(uuid)
    if lp.is_file():
        try:
            holder = json.loads(lp.read_text()).get("monitor_pid")
        except (OSError, json.JSONDecodeError):
            holder = None
        if holder:
            try:
                os.kill(int(holder), 0)
                alive = True
            except OSError:
                alive = False
        else:
            alive = False
        if alive and not args.force:
            print(
                f"compacting lock at {lp} is held by alive PID {holder}. "
                f"Quit the monitor first, or pass --force.",
                file=sys.stderr,
            )
            return 2

    full = full_jsonl_path(uuid)
    if not full.is_file():
        print(f"full.jsonl missing at {full}", file=sys.stderr)
        return 1

    cache_root = multiverse_dir() / "trees" / uuid
    if not cache_root.is_dir():
        print(f"trees cache missing at {cache_root}", file=sys.stderr)
        return 1

    t0 = time.time()
    forest = parse_session(str(full))
    print(f"[parse] {len(forest)} turns in {time.time()-t0:.1f}s", file=sys.stderr)

    t0 = time.time()
    n_with = n_without = 0
    for turn in forest:
        path = cache_root / f"{turn.turn_id}.json"
        if not path.is_file():
            n_without += 1
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            n_without += 1
            continue
        td = data.get("tree")
        if td is None or _is_aborted_stub(td):
            n_without += 1
            continue
        try:
            turn.root = _reconstruct_tree(td)
            n_with += 1
        except (KeyError, TypeError):
            n_without += 1
    print(f"[attach] {n_with} attached, {n_without} fallback in "
          f"{time.time()-t0:.1f}s", file=sys.stderr)

    t0 = time.time()
    per_turn, keep_image_turn_ids = allocate_budget(
        forest, budget, scheduler=proportional
    )
    print(f"[scheduler] allocate_budget in {time.time()-t0:.2f}s", file=sys.stderr)

    title = _read_existing_title(live_jsonl_path(uuid, cwd)) or "compacted"

    t0 = time.time()
    jsonl_text = compact_forest_to_session_jsonl(
        forest, per_turn,
        session_id=uuid, cwd=cwd, title=title,
        keep_image_turn_ids=keep_image_turn_ids,
    )
    print(f"[render] {jsonl_text.count(chr(10))} rows / "
          f"{len(jsonl_text):,}B in {time.time()-t0:.1f}s", file=sys.stderr)

    live = live_jsonl_path(uuid, cwd)

    # Carry session-state rows (Remote Control's bridge-session pointer,
    # permission-mode, agent-name, mode) across the rewrite — the
    # renderer only emits transcript rows. Read from the OUTGOING live
    # before it's replaced. Same logic the monitor-driven path uses.
    state_rows = _collect_state_rows(live)
    if state_rows:
        jsonl_text = splice_state_rows(jsonl_text, state_rows)
        print(f"[state] preserved: "
              f"{', '.join(r.get('type','?') for r in state_rows)}",
              file=sys.stderr)

    live.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(prefix=".compact_", suffix=".jsonl",
                               dir=str(live.parent))
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

    new_last = last_uuid_in(live)
    if new_last:
        write_marker(uuid, new_last)
        print(f"[marker] updated → {new_last[:8]}", file=sys.stderr)

    release_lock(uuid)
    print(f"[done] live → {live} ({live.stat().st_size:,}B); lock released",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
