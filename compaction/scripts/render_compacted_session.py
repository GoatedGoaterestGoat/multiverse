"""Render a compacted Claude Code session JSONL from a source session.

For each turn in the source JSONL, expand its tree to its allocated budget
(or use greedy-leaves-from-start when no tree is available), then emit
user_prompt + compacted assistant blocks. All turns share one synthetic
sessionId; parentUuid threads continuously across turns. The result is
loadable via `claude --resume <session-id>`.

Pre-built per-turn trees can be supplied via `--tree TURN_IDX=PATH` (repeat
for multiple turns); these were typically produced by build_tree_priv_cli.py
and saved as JSON. Turns without pre-built trees fall back to greedy
truncation at their allocated budget.

Usage:
    PYTHONPATH=src python3 scripts/render_compacted_session.py SOURCE.jsonl \\
        --budget 32000 \\
        --scheduler recency_decay \\
        --tree 0=/tmp/tree_turn0_priv.json \\
        --tree 1=/tmp/tree_turn1_priv.json \\
        --turns 0:5 \\
        --out /tmp/compacted.jsonl \\
        [--write-projects-dir]
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid as uuidlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from compaction.compactor import (
    SCHEDULERS, last_k_raw, proportional, recency_decay, uniform,
)
from compaction.parser import parse_session
from compaction.preprocess import PreprocessConfig, preprocess_forest
from compaction.priv_session import (
    compact_forest_to_session_jsonl, projects_dir,
)
from compaction.types import Leaf, Node


def reconstruct(d):
    if d["type"] == "leaf":
        return Leaf(kind=d["kind"], content=d.get("content", {}),
                    tokens=d["tokens"], preview=d.get("preview", ""),
                    source_uuids=d.get("source_uuids", []))
    return Node(id=d["id"], children=[reconstruct(c) for c in d["children"]],
                summary=d.get("summary", ""), tokens=d["tokens"],
                preview=d.get("preview", ""))


def parse_tree_arg(s: str) -> tuple[int, str]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"--tree expects TURN_IDX=PATH, got {s!r}")
    idx_s, path = s.split("=", 1)
    return int(idx_s), path


def parse_turns_arg(s: str) -> tuple[int, int]:
    """Parse "0:5" or "3" into (start, end) inclusive of start, exclusive of end."""
    if ":" in s:
        a, b = s.split(":", 1)
        return int(a), int(b)
    n = int(s)
    return n, n + 1


def resolve_scheduler(name: str, half_life: float, last_k: int):
    if name == "recency_decay":
        return lambda f, b: recency_decay(f, b, half_life=half_life)
    if name == "last_k_raw":
        return lambda f, b: last_k_raw(f, b, k=last_k)
    return SCHEDULERS[name]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source", help="path to source JSONL")
    p.add_argument("--budget", type=int, default=32_000,
                   help="total token budget across all selected turns")
    p.add_argument("--scheduler", choices=sorted(SCHEDULERS.keys()),
                   default="recency_decay")
    p.add_argument("--half-life", type=float, default=5.0)
    p.add_argument("--last-k", type=int, default=3)
    p.add_argument("--tree", action="append", type=parse_tree_arg,
                   default=[],
                   help="pre-built tree JSON for turn TURN_IDX=PATH (repeatable)")
    p.add_argument("--turns", type=parse_turns_arg, default=None,
                   help='turn range "START:END" (default: all turns)')
    p.add_argument("--out", default=None,
                   help="output path (default: /tmp/compacted_<sessionid>.jsonl)")
    p.add_argument("--write-projects-dir", action="store_true",
                   help="also place a copy in ~/.claude/projects/<slug>/<id>.jsonl "
                        "so `claude --resume <id>` finds it")
    p.add_argument("--title", default="compacted_forest")
    args = p.parse_args()

    forest = preprocess_forest(parse_session(args.source), PreprocessConfig())

    # Slice down to the selected turn range
    if args.turns is not None:
        a, b = args.turns
        if a < 0 or b > len(forest):
            print(f"--turns {a}:{b} out of range (forest has {len(forest)} turns)",
                  file=sys.stderr)
            return 1
        forest = forest[a:b]

    # Attach pre-built trees (idx is into the SLICED forest if --turns was used)
    tree_map = dict(args.tree)
    n_with_trees = 0
    for idx, path in tree_map.items():
        if args.turns is not None:
            local_idx = idx - args.turns[0]
        else:
            local_idx = idx
        if not (0 <= local_idx < len(forest)):
            print(f"--tree {idx}=... out of range in selected turns", file=sys.stderr)
            return 1
        with open(path) as f:
            tree_data = json.load(f)
        forest[local_idx].root = reconstruct(tree_data["tree"])
        n_with_trees += 1

    print(f"Selected {len(forest)} turn(s); {n_with_trees} have pre-built trees, "
          f"{len(forest) - n_with_trees} will fall back to greedy truncation",
          file=sys.stderr)

    sched = resolve_scheduler(args.scheduler, args.half_life, args.last_k)
    per_turn_budget = sched(forest, args.budget)
    print("Per-turn budget allocation:", file=sys.stderr)
    for turn in forest:
        print(f"  {turn.turn_id[:8]}  {per_turn_budget.get(turn.turn_id, 0):>6}t  "
              f"({len(turn.leaves)} leaves, full={sum(l.tokens for l in turn.leaves)}t, "
              f"tree={'yes' if turn.root else 'no'})",
              file=sys.stderr)

    session_id = str(uuidlib.uuid4())
    jsonl_text = compact_forest_to_session_jsonl(
        forest, per_turn_budget, session_id=session_id, title=args.title,
    )

    out = args.out or f"/tmp/compacted_{session_id[:8]}.jsonl"
    Path(out).write_text(jsonl_text)
    print(f"\nWrote compacted JSONL to {out}", file=sys.stderr)
    print(f"  session_id: {session_id}", file=sys.stderr)
    print(f"  size: {len(jsonl_text)} chars / "
          f"{len(jsonl_text.splitlines())} rows", file=sys.stderr)

    if args.write_projects_dir:
        d = projects_dir()
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{session_id}.jsonl"
        target.write_text(jsonl_text)
        print(f"  also wrote to {target}", file=sys.stderr)
        print(f"  resume with: claude --resume {session_id}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
