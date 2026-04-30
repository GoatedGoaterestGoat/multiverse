"""Privileged-mode tree construction: model receives the conversation
as native `messages` (with thinking blocks intact), reads server-side-decrypted
chain-of-thought when picking merges, and writes summaries that incorporate
the actual reasoning rather than just the visible outcomes.

Requirements:
  - ANTHROPIC_API_KEY set in environment.
  - That key on the same Anthropic account that produced the source session;
    otherwise thinking signatures fail validation and the API 400s.

Usage:
    PYTHONPATH=src python3 scripts/build_tree_priv.py SESSION.jsonl TURN_IDX \\
        [--out tree.json] [--model claude-sonnet-4-6]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from compaction.constructor import (
    ConstructorConfig,
    _make_client,
    _request_merge_priv,
    _summary_preview,
    _valid_span,
)
from compaction.parser import parse_session
from compaction.preprocess import PreprocessConfig, preprocess_forest
from compaction.tokenize import count_tokens
from compaction.types import Leaf, Node, Turn


def build_tree_priv(turn: Turn, model: str = "claude-sonnet-4-6"):
    frontier = list(turn.leaves)
    if not frontier:
        return None, []
    if len(frontier) == 1:
        return frontier[0], []

    cfg = ConstructorConfig(model=model, capture_trace=True)
    client = _make_client(cfg.api_key)
    history: list[dict] = []
    next_id = 0
    safety_cap = int(cfg.safety_cap_multiplier * len(frontier))

    print(
        f"Constructing (PRIVILEGED MODE): {len(frontier)} leaves -> 1 root",
        file=sys.stderr,
    )

    for step in range(safety_cap):
        if len(frontier) == 1:
            break

        t0 = time.time()
        try:
            start, end, summary = _request_merge_priv(client, turn, frontier, cfg)
        except Exception as e:
            print(f"\n  step {step}: FAIL: {e}", file=sys.stderr)
            history.append({"step": step, "error": str(e)})
            break
        elapsed = time.time() - t0

        if not _valid_span(start, end, len(frontier)):
            print(
                f"\n  step {step}: INVALID span ({start},{end}) for frontier size "
                f"{len(frontier)}",
                file=sys.stderr,
            )
            history.append({
                "step": step, "error": "invalid span",
                "start": start, "end": end, "frontier_size": len(frontier),
            })
            break

        merged_children = list(frontier[start:end])
        new_node = Node(
            id=next_id, children=merged_children, summary=summary,
            tokens=count_tokens(summary), preview=_summary_preview(summary),
        )
        next_id += 1
        frontier = list(frontier[:start]) + [new_node] + list(frontier[end:])

        history.append({
            "step": step, "node_id": new_node.id, "start": start, "end": end,
            "span": end - start, "summary": summary,
            "summary_tokens": new_node.tokens,
            "elapsed_s": round(elapsed, 2),
            "frontier_size_after": len(frontier),
        })
        print(
            f"  step {step:>3d}: merge [{start:>2d}:{end:>2d}] "
            f"({end - start} entries) -> #{step} | {new_node.tokens:>3d}tok summary "
            f"| frontier {len(frontier):>3d} | {elapsed:.1f}s",
            file=sys.stderr,
        )

    if len(frontier) == 1:
        return frontier[0], history

    root = Node(
        id=next_id, children=list(frontier),
        summary="<construction aborted; remaining frontier wrapped>",
        tokens=sum(getattr(n, "tokens", 0) for n in frontier),
        preview="<aborted>",
    )
    return root, history


def serialize(node):
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("session", help="path to JSONL session log")
    p.add_argument("turn_idx", type=int, help="turn index to construct")
    p.add_argument("--out", default=None)
    p.add_argument("--model", default="claude-sonnet-4-6")
    args = p.parse_args()

    forest = preprocess_forest(parse_session(args.session), PreprocessConfig())
    if args.turn_idx >= len(forest):
        print(
            f"only {len(forest)} turns; index {args.turn_idx} out of range",
            file=sys.stderr,
        )
        return 1

    turn = forest[args.turn_idx]
    out = args.out or f"tree_turn{args.turn_idx}_priv.json"

    total = sum(l.tokens for l in turn.leaves)
    print(
        f"=== turn {args.turn_idx} (id {turn.turn_id[:8]}) — PRIVILEGED MODE ===\n"
        f"{len(turn.leaves)} leaves | {total}t (verbatim cost)",
        file=sys.stderr,
    )

    root, history = build_tree_priv(turn, model=args.model)

    prompt_data = serialize(turn.user_prompt) if turn.user_prompt is not None else None

    data = {
        "session": args.session,
        "turn_idx": args.turn_idx,
        "turn_id": turn.turn_id,
        "n_leaves": len(turn.leaves),
        "model": args.model,
        "mode": "priv",
        "user_prompt": prompt_data,
        "leaves": [serialize(l) for l in turn.leaves],
        "tree": serialize(root) if root is not None else None,
        "history": history,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"\nSaved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
