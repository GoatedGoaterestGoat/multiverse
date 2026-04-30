"""Run full bottom-up construction on one turn via the Claude CLI backend
and save the resulting tree (plus the merge history) to a JSON file.

Each merge is one CLI call (~5-15s), so a turn with N leaves takes roughly
(N-1) * 10s. Defaults to turn 2 (8 leaves, ~1 minute) to keep the demo
cheap. Pass a different index for bigger turns.

Usage:
    PYTHONPATH=src python3 scripts/build_tree.py \\
        [SESSION.jsonl] [TURN_INDEX] [--out tree.json] [--model sonnet]

Saves to `tree_turn{IDX}.json` by default.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from compaction.cli_backend import CLIBackendConfig, request_merge_via_cli
from compaction.parser import parse_session
from compaction.preprocess import PreprocessConfig, preprocess_forest
from compaction.tokenize import count_tokens
from compaction.types import Leaf, Node, Turn

DEFAULT_SESSION = (
    "/Users/admin/Documents/Github/Multiverse/assets/sam3d_singleframe.jsonl"
)


def build_tree_for_turn(turn: Turn, model: str = "sonnet") -> tuple[object, list[dict]]:
    """Run the full merge loop on `turn`. Returns (root, history)."""
    frontier = list(turn.leaves)
    if not frontier:
        return None, []
    if len(frontier) == 1:
        return frontier[0], []

    cfg = CLIBackendConfig(model=model)
    history: list[dict] = []
    next_id = 0

    print(
        f"Constructing: {len(frontier)} leaves -> 1 root "
        f"({len(frontier) - 1} merges expected)",
        file=sys.stderr,
    )

    while len(frontier) > 1:
        step = next_id
        t0 = time.time()
        try:
            start, end, summary = request_merge_via_cli(frontier, cfg)
        except Exception as e:
            print(f"\n  step {step}: FAIL: {e}", file=sys.stderr)
            history.append({"step": step, "error": str(e)})
            break
        elapsed = time.time() - t0

        if not (0 <= start < end <= len(frontier) and (end - start) >= 2):
            print(
                f"\n  step {step}: INVALID span ({start},{end}) "
                f"for frontier size {len(frontier)}",
                file=sys.stderr,
            )
            history.append({"step": step, "error": "invalid span",
                             "start": start, "end": end,
                             "frontier_size": len(frontier)})
            break

        merged_children = list(frontier[start:end])
        new_node = Node(
            id=next_id,
            children=merged_children,
            summary=summary,
            tokens=count_tokens(summary),
            preview=" ".join(summary.split())[:120],
        )
        next_id += 1
        frontier = list(frontier[:start]) + [new_node] + list(frontier[end:])

        history.append({
            "step": step,
            "node_id": new_node.id,
            "start": start,
            "end": end,
            "span": end - start,
            "summary": summary,
            "summary_tokens": new_node.tokens,
            "elapsed_s": round(elapsed, 2),
            "frontier_size_after": len(frontier),
        })

        print(
            f"  step {step:>3d}: merge [{start:>2d}:{end:>2d}] "
            f"({end - start} entries) -> #{step} "
            f"| {new_node.tokens:>3d}tok summary "
            f"| frontier {len(frontier):>3d} "
            f"| {elapsed:.1f}s",
            file=sys.stderr,
        )

    if len(frontier) == 1:
        return frontier[0], history

    # Aborted before reaching root: wrap remaining frontier in a synthetic
    # node so visualization still has a single root.
    root = Node(
        id=next_id,
        children=list(frontier),
        summary="<construction aborted; remaining frontier wrapped>",
        tokens=sum(getattr(n, "tokens", 0) for n in frontier),
        preview="<aborted>",
    )
    return root, history


def serialize(node) -> dict:
    """Recursively serialize a Leaf or Node tree to plain dicts."""
    if isinstance(node, Leaf):
        return {
            "type": "leaf",
            "kind": node.kind,
            "tokens": node.tokens,
            "preview": node.preview,
            "content": node.content,
            "source_uuids": node.source_uuids,
        }
    if isinstance(node, Node):
        return {
            "type": "node",
            "id": node.id,
            "tokens": node.tokens,
            "preview": node.preview,
            "summary": node.summary,
            "children": [serialize(c) for c in node.children],
        }
    return {"type": "unknown", "repr": repr(node)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("session", nargs="?", default=DEFAULT_SESSION)
    p.add_argument("turn_idx", nargs="?", type=int, default=2)
    p.add_argument("--out", default=None,
                   help="output JSON path (default: tree_turn{IDX}.json)")
    p.add_argument("--model", default="sonnet")
    args = p.parse_args()

    forest = preprocess_forest(parse_session(args.session), PreprocessConfig())
    if args.turn_idx >= len(forest):
        print(
            f"only {len(forest)} turns; index {args.turn_idx} out of range",
            file=sys.stderr,
        )
        return 1

    turn = forest[args.turn_idx]
    out = args.out or f"tree_turn{args.turn_idx}.json"

    print(
        f"=== turn {args.turn_idx} (id {turn.turn_id[:8]}) ===\n"
        f"{len(turn.leaves)} leaves, "
        f"{sum(l.tokens for l in turn.leaves)} tokens\n"
        f"first leaf: {turn.leaves[0].preview[:100]!r}",
        file=sys.stderr,
    )

    root, history = build_tree_for_turn(turn, model=args.model)

    data = {
        "session": args.session,
        "turn_idx": args.turn_idx,
        "turn_id": turn.turn_id,
        "n_leaves": len(turn.leaves),
        "model": args.model,
        "user_prompt": serialize(turn.user_prompt) if turn.user_prompt is not None else None,
        "leaves": [serialize(l) for l in turn.leaves],
        "tree": serialize(root) if root is not None else None,
        "history": history,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"\nSaved tree to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
