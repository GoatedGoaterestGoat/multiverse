"""Build one turn's compaction tree via the privileged CLI constructor.

The constructor reads each turn through `claude --resume` against a synthetic
session containing only that turn's content (with thinking blocks intact, so
signatures validate server-side and the model has full access to original
chain-of-thought when picking merges). Multi-step: rewrites the synthetic
session each merge to reflect the current frontier.

Usage:
    PYTHONPATH=src python3 scripts/build_tree_priv_cli.py SESSION.jsonl TURN_IDX \\
        [--out tree.json] [--model sonnet]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from compaction.constructor import ConstructorConfig, construct_turn_priv_cli
from compaction.parser import compaction_forest
from compaction.preprocess import PreprocessConfig, preprocess_forest
from compaction.types import Leaf, Node


def serialize(node):
    if isinstance(node, Leaf):
        return {
            "type": "leaf",
            "kind": node.kind,
            "tokens": node.tokens,
            "preview": node.preview,
            "content": node.content,
            "source_uuids": node.source_uuids,
            "images": node.images,
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
    p.add_argument("session", help="path to source JSONL")
    p.add_argument("turn_idx", type=int)
    p.add_argument("--out", default=None)
    p.add_argument("--model", default="sonnet")
    args = p.parse_args()

    # Canonical compaction view (command turns stripped, non-compactable
    # leaves filtered, empty turns merged) — the tree must be built over
    # exactly the turns/leaves that end up in the compacted output.
    # turn_idx is relative to this same view the worker enumerates.
    forest = preprocess_forest(
        compaction_forest(args.session),
        PreprocessConfig(),
    )
    if args.turn_idx >= len(forest):
        print(f"only {len(forest)} turns; index {args.turn_idx} out of range",
              file=sys.stderr)
        return 1

    turn = forest[args.turn_idx]
    out = args.out or f"tree_turn{args.turn_idx}_priv.json"

    total = sum(l.tokens for l in turn.leaves)
    print(
        f"=== turn {args.turn_idx} (id {turn.turn_id[:8]}) — PRIV-CLI ===\n"
        f"{len(turn.leaves)} leaves | {total}t (verbatim — what the live "
        f"agent will pay if the full frontier is preserved)",
        file=sys.stderr,
    )

    cfg = ConstructorConfig(model=args.model, capture_trace=True)
    t0 = time.time()
    last_ts = [t0]

    def progress(step, summary, new_frontier_size):
        now = time.time()
        elapsed = now - last_ts[0]
        last_ts[0] = now
        s = " ".join(summary.split())
        if len(s) > 90:
            s = s[:87] + "..."
        print(
            f"  step {step:>3d} | frontier {new_frontier_size:>3d} | "
            f"{elapsed:5.1f}s | {s}",
            file=sys.stderr,
        )

    trace = construct_turn_priv_cli(
        turn, args.session, cfg, progress=progress
    )
    total_elapsed = time.time() - t0

    print(
        f"\nDone in {total_elapsed:.1f}s. "
        f"Status: {'completed' if trace.finished else f'aborted: {trace.aborted_reason}'}\n"
        f"Merges: {len(trace.merges)}",
        file=sys.stderr,
    )

    prompt_data = serialize(turn.user_prompt) if turn.user_prompt is not None else None
    data = {
        "session": args.session,
        "turn_idx": args.turn_idx,
        "turn_id": turn.turn_id,
        "n_leaves": len(turn.leaves),
        "model": args.model,
        "mode": "priv-cli",
        "user_prompt": prompt_data,
        "leaves": [serialize(l) for l in turn.leaves],
        "tree": serialize(turn.root) if turn.root is not None else None,
        "history": trace.merges,
    }
    # tmp + atomic replace, not open(out, "w"). Truncating opens two
    # windows, and the second is the expensive one:
    #
    #  1. A torn file if we die mid-dump. Every reader already tolerates
    #     that (_is_valid_cached_tree and _attach_trees_from_cache both
    #     catch JSONDecodeError and treat it as no-tree), so the cost is
    #     a rebuild, not corruption.
    #  2. The one that actually bites: `open(out, "w")` destroys an
    #     EXISTING good tree the instant it opens, before the
    #     replacement is written. Die in between and we've thrown away
    #     a tree we already had — the same destroy-before-commit shape
    #     as the inbox drain, and here each rebuild is tens of seconds
    #     of model calls (hours across a whole session).
    #
    # os.replace leaves the old file untouched until the new one is
    # complete on disk.
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".tree_", suffix=".json", dir=str(out_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    print(f"Saved tree to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
