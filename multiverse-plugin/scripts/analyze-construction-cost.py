#!/usr/bin/env python3
"""Price tree construction over the local summary-tree cache.

Answers "what does goat compaction actually cost to build, and what would the
cheaper prompt layouts save" from real data rather than a model of it: every
cached tree records the merge sequence that produced it, so the frontier at
every step can be replayed exactly.

Three layouts are priced, in Anthropic billed input units (uncached 1.0,
cache write 1.25, cache read 0.1):

  current  — one stateless request per merge, with the assistant block
             re-rendered from the CURRENT frontier. The block differs from
             the previous step's at the merge site, so the cache only carries
             the bytes before it.
  append   — one session per turn, appended to. The transcript prefix is
             byte-identical from step to step; only a short delta is new.
  level    — append, but one call per LEVEL instead of one per merge, so the
             number of round trips drops from O(merges) to O(log N).

    scripts/analyze-construction-cost.py [--trees ~/.multiverse/trees]

Trees without a recorded merge history (built before history capture, or
single-leaf turns) are skipped.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from pathlib import Path

# Prompt-layout constants. The index is one preview line per frontier entry;
# the boilerplate is the system prompt plus merge instruction.
IDX_PER_ENTRY = 35
BOILER = 400
# Appended tail per step: "merged [a,b) -> <summary>" plus a short re-prompt.
DELTA = 250
# Anthropic cache economics.
W, R = 1.25, 0.10


def replay(tree: dict):
    """Reconstruct (leaf tokens, per-step frontier facts) from a cached tree."""
    leaves = [int(l.get("tokens", 0)) for l in tree["leaves"]]
    hist = tree.get("history") or []
    if not leaves or not hist:
        return None
    frontier = list(leaves)
    steps = []
    for h in hist:
        s, e = h["start"], h["end"]
        if not (0 <= s < e <= len(frontier)):
            return None            # history inconsistent with leaves; skip
        steps.append((sum(frontier), sum(frontier[:s]), len(frontier)))
        frontier = frontier[:s] + [max(1, len(h.get("summary", "")) // 4)] + frontier[e:]
    return leaves, steps, hist


def price(tree: dict):
    r = replay(tree)
    if r is None:
        return None
    leaves, steps, hist = r
    T, N, K = sum(leaves), len(leaves), len(steps)

    current = 0.0
    for i, (ftok, prefix, fsize) in enumerate(steps):
        idx = fsize * IDX_PER_ENTRY + BOILER
        if i == 0:
            current += (ftok + idx) * W          # cold: all of it is a write
        else:
            current += prefix * R + (ftok - prefix + idx) * W

    grown = T + N * IDX_PER_ENTRY + BOILER
    append = grown * W
    for _ in range(K):
        append += grown * R + DELTA * W
        grown += DELTA

    # Levels follow the observed arity: variable-arity merges collapse more
    # than two entries at a time, so log2 would overstate the level count.
    arity = statistics.mean(h["end"] - h["start"] for h in hist)
    levels = max(1, math.ceil(math.log(max(2, N)) / math.log(max(1.5, arity))))
    grown = T + N * IDX_PER_ENTRY + BOILER
    level = grown * W
    for _ in range(levels):
        level += grown * R + (N * IDX_PER_ENTRY // 2) * W
        grown += N * IDX_PER_ENTRY // 2

    return dict(N=N, T=T, K=K, current=current, append=append,
                level=level, levels=levels)


def show(label: str, rows: list[dict]) -> None:
    if not rows:
        return
    n = len(rows)
    T = sum(r["T"] for r in rows)
    cur = sum(r["current"] for r in rows)
    app = sum(r["append"] for r in rows)
    lvl = sum(r["level"] for r in rows)
    K = sum(r["K"] for r in rows)
    L = sum(r["levels"] for r in rows)
    print(
        f"{label:<20} n={n:<5} tok/turn={T / n:>7.0f} calls={K / n:>5.1f} | "
        f"billed-in/turn: current={cur / n:>8.0f} append={app / n:>8.0f} "
        f"level={lvl / n:>8.0f} | saving: append {cur / app:>4.1f}x  "
        f"level {cur / lvl:>4.1f}x  calls {K / max(1, L):>4.1f}x"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trees", default=os.path.expanduser("~/.multiverse/trees"))
    args = ap.parse_args()

    root = Path(args.trees)
    if not root.is_dir():
        print(f"no tree cache at {root}")
        return 1

    rows, skipped = [], 0
    for path in root.rglob("*.json"):
        try:
            tree = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "leaves" not in tree:
            continue
        p = price(tree)
        if p is None:
            skipped += 1
        else:
            rows.append(p)

    if not rows:
        print("no trees with a recorded merge history")
        return 1

    print(f"{len(rows)} trees priced ({skipped} skipped: no merge history)\n")
    show("ALL", rows)
    for lo, hi in [(0, 10), (10, 25), (25, 60), (60, 10 ** 9)]:
        label = f"N in [{lo},{hi})" if hi < 10 ** 9 else f"N >= {lo}"
        show(label, [r for r in rows if lo <= r["N"] < hi])

    T = sum(r["T"] for r in rows)
    print(f"\naggregate over {T / 1e6:.2f}M transcript tokens:")
    for key, label in (("current", "current"), ("append", "append"), ("level", "level")):
        v = sum(r[key] for r in rows)
        print(f"  {label:<8} {v / 1e6:5.2f}M billed input units  ({v / T:.2f}x transcript)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
