"""End-to-end CLI: JSONL session log -> compacted context.

Usage:

    python -m compaction.cli SESSION.jsonl --budget 32000 [--scheduler NAME]
                              [--dry-run]
                              [--out compacted.txt]

Pipeline:

    parse_session -> preprocess_forest -> construct_forest -> compact

`--dry-run` swaps the real Anthropic-based constructor for a deterministic
local stub that merges the leftmost adjacent pair on each step and writes
a placeholder summary. Useful for verifying the end-to-end wiring without
burning tokens.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from .compactor import (
    SCHEDULERS,
    compact,
    last_k_raw,
    proportional,
    recency_decay,
    uniform,
)
from .parser import parse_session
from .preprocess import PreprocessConfig, preprocess_forest
from .render import render_frontier_as_messages
from .tokenize import count_tokens
from .types import Forest, Leaf, Node, Turn


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="compaction", description=__doc__)
    p.add_argument("session", help="path to a Claude Code JSONL session log")
    p.add_argument("--budget", type=int, default=32_000, help="total token budget")
    p.add_argument(
        "--scheduler",
        choices=sorted(SCHEDULERS.keys()),
        default="recency_decay",
    )
    p.add_argument(
        "--half-life", type=float, default=5.0,
        help="half-life for recency_decay (turns)",
    )
    p.add_argument(
        "--last-k", type=int, default=3,
        help="k for last_k_raw scheduler",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="use deterministic local stub instead of Anthropic API",
    )
    p.add_argument(
        "--no-construct", action="store_true",
        help="skip construction; use the leaves-passthrough fallback",
    )
    p.add_argument(
        "--out", default=None,
        help="write compacted output to this file (default: stdout)",
    )
    p.add_argument(
        "--stats", action="store_true",
        help="print per-stage stats and exit (no compaction output)",
    )
    args = p.parse_args(argv)

    forest = parse_session(args.session)
    print_stage("parse", forest)

    forest = preprocess_forest(forest, PreprocessConfig())
    print_stage("preprocess", forest)

    if not args.no_construct:
        if args.dry_run:
            _dry_run_construct(forest)
            print_stage("construct (dry-run)", forest, with_roots=True)
        else:
            from .constructor import ConstructorConfig, construct_forest
            cfg = ConstructorConfig()
            try:
                construct_forest(forest, cfg, progress=_progress)
            except ImportError as e:
                print(f"error: {e}\nHint: pip install anthropic", file=sys.stderr)
                return 1
            print_stage("construct", forest, with_roots=True)

    if args.stats:
        return 0

    sched = _resolve_scheduler(args)
    result = compact(forest, total_budget=args.budget, scheduler=sched)
    text = render_frontier_as_messages(_flatten_result(result))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        sys.stderr.write(
            f"wrote {len(text)} chars / {count_tokens(text)} tokens to {args.out}\n"
        )
    else:
        print(text)

    sys.stderr.write(
        f"compaction: scheduler={args.scheduler} budget={args.budget} "
        f"used={result['used_tokens']} turns={len(result['frontiers'])}\n"
    )
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_scheduler(args):
    name = args.scheduler
    if name == "recency_decay":
        return lambda f, b: recency_decay(f, b, half_life=args.half_life)
    if name == "last_k_raw":
        return lambda f, b: last_k_raw(f, b, k=args.last_k)
    return SCHEDULERS[name]


def _flatten_result(result):
    """Interleave each turn's verbatim user_prompt before its (compacted)
    frontier. The prompt is rendered raw and is never part of the tree."""
    out = []
    for turn, frontier in result["frontiers"]:
        if turn.user_prompt is not None:
            out.append(turn.user_prompt)
        out.extend(frontier)
    return out


def _progress(i, n, turn):
    sys.stderr.write(
        f"\rconstructing turn {i + 1}/{n} ({len(turn.leaves)} leaves)..."
    )
    sys.stderr.flush()


def print_stage(name: str, forest: Forest, with_roots: bool = False) -> None:
    n_leaves = sum(len(t.leaves) for t in forest)
    n_tokens = sum(l.tokens for t in forest for l in t.leaves)
    kinds = Counter(l.kind for t in forest for l in t.leaves)
    n_prompts = sum(1 for t in forest if t.user_prompt is not None)
    prompt_tokens = sum(t.user_prompt.tokens for t in forest if t.user_prompt is not None)
    msg = (
        f"[{name}] turns={len(forest)} prompts={n_prompts} ({prompt_tokens:,} tok, verbatim) "
        f"leaves={n_leaves} tokens={n_tokens:,} kinds={dict(kinds)}"
    )
    if with_roots:
        n_roots = sum(1 for t in forest if t.root is not None)
        msg += f" roots_built={n_roots}"
    sys.stderr.write(msg + "\n")


# ---------------------------------------------------------------------------
# Dry-run constructor: no API calls
# ---------------------------------------------------------------------------

def _dry_run_construct(forest: Forest) -> None:
    """Build a tree on each turn by mechanically merging the leftmost pair.

    Produces a left-leaning binary tree with deterministic placeholder
    summaries. Useful for verifying the rest of the pipeline.
    """
    for turn in forest:
        turn.root = _dry_build(turn.leaves)


def _dry_build(leaves: list[Leaf]):
    if not leaves:
        return None
    if len(leaves) == 1:
        return leaves[0]
    next_id = 0
    frontier: list = list(leaves)
    while len(frontier) > 1:
        a, b = frontier[0], frontier[1]
        merged = Node(
            id=next_id,
            children=[a, b],
            summary=f"[stub merge of {_describe(a)} + {_describe(b)}]",
            tokens=max(1, (a.tokens + b.tokens) // 4),
            preview=f"merged({_short(a.preview)}|{_short(b.preview)})",
        )
        next_id += 1
        frontier = [merged] + frontier[2:]
    return frontier[0]


def _describe(entry):
    if isinstance(entry, Leaf):
        return entry.kind
    return f"node#{entry.id}"


def _short(s: str, n: int = 30) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


if __name__ == "__main__":
    sys.exit(main())
