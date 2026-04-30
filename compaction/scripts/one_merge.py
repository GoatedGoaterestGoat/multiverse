"""One-merge smoke test: parse + preprocess one turn, run a SINGLE merge
through the Claude CLI, print the before/after frontier and the merge
decision.

Usage:
    PYTHONPATH=src python3 scripts/one_merge.py [SESSION.jsonl] [TURN_INDEX]

Defaults to assets/sam3d_singleframe.jsonl turn 2.

Requires the `claude` CLI to be on PATH and signed in. No API key needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from compaction.cli_backend import CLIBackendConfig, request_merge_via_cli
from compaction.parser import parse_session
from compaction.preprocess import PreprocessConfig, preprocess_forest
from compaction.render import render_frontier_for_constructor
from compaction.tokenize import count_tokens
from compaction.types import Leaf, Node


DEFAULT_SESSION = (
    "/Users/admin/Documents/Github/Multiverse/assets/sam3d_singleframe.jsonl"
)


def main() -> int:
    session = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SESSION
    turn_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    forest = preprocess_forest(parse_session(session), PreprocessConfig())
    if turn_idx >= len(forest):
        print(f"only {len(forest)} turns; index {turn_idx} out of range", file=sys.stderr)
        return 1

    turn = forest[turn_idx]
    frontier = list(turn.leaves)
    print(f"=== turn {turn_idx} (id {turn.turn_id[:8]}) ===")
    print(f"{len(frontier)} leaves, {sum(l.tokens for l in frontier)} tokens")
    print()

    print("--- INITIAL FRONTIER ---")
    print(render_frontier_for_constructor(frontier))
    print()

    print("--- calling claude CLI (sonnet) ---", flush=True)
    cfg = CLIBackendConfig(model="sonnet")
    try:
        start, end, summary = request_merge_via_cli(frontier, cfg)
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    print(f"start = {start}")
    print(f"end   = {end}")
    print(f"span size = {end - start}")
    print(f"summary ({count_tokens(summary)} tokens):")
    print(f"  {summary}")
    print()

    if not (0 <= start < end <= len(frontier) and (end - start) >= 2):
        print(f"INVALID span for frontier of size {len(frontier)}", file=sys.stderr)
        return 3

    merged_children = list(frontier[start:end])
    new_node = Node(
        id=0,
        children=merged_children,
        summary=summary,
        tokens=count_tokens(summary),
        preview=" ".join(summary.split())[:120],
    )
    new_frontier = list(frontier[:start]) + [new_node] + list(frontier[end:])

    print("--- MERGED CHILDREN ---")
    for i, c in enumerate(merged_children):
        kind = c.kind if isinstance(c, Leaf) else f"node#{c.id}"
        print(f"  [{start + i}] {kind} | {c.tokens} tok | {c.preview!r}")
    print()

    print("--- NEW FRONTIER ---")
    print(render_frontier_for_constructor(new_frontier))
    print()

    old_tot = sum(n.tokens for n in frontier)
    new_tot = sum(n.tokens for n in new_frontier)
    print(f"frontier tokens: {old_tot} -> {new_tot} (delta {new_tot - old_tot:+d})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
