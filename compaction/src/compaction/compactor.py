"""Read path: expand a forest into a token-budget-fitting context.

Two cleanly-separated layers:

1. `expand_to_budget` — fixed per-tree primitive. Always priority-by-id.
   Starts at the root frontier, expands the highest-id node each step
   until the next expansion would exceed the budget.

2. `compact` — across-tree budget allocator (the *scheduler*). Pluggable.
   Takes a forest and a total budget, returns a per-turn allocation,
   then calls `expand_to_budget` once per turn.

Schedulers are pure functions of (forest, total_budget) -> dict[turn_id, int].
The expansion primitive is unchangeable; schedulers are where the knobs
live.
"""

from __future__ import annotations

from typing import Callable, Iterable

from .types import Forest, Frontier, Leaf, Node, Turn

Scheduler = Callable[[Forest, int], dict[str, int]]


# ---------------------------------------------------------------------------
# Expansion: per-tree primitive (fixed, no knobs)
# ---------------------------------------------------------------------------

def expand_to_budget(root: Node | Leaf, budget: int) -> Frontier:
    """Expand a single tree's frontier from [root] until the next
    expansion would exceed the budget. Priority is always node.id
    (highest first = undo most recent merge first).

    If the root is already a Leaf (single-leaf turn), returns [root].
    If `budget` is non-positive, returns [root] unchanged.
    """
    frontier: Frontier = [root]
    if budget <= 0:
        return frontier

    while True:
        # Candidates: Nodes in the current frontier that have children.
        candidates = [n for n in frontier if isinstance(n, Node) and n.children]
        if not candidates:
            return frontier
        target = max(candidates, key=lambda n: n.id)

        expanded: Frontier = []
        for n in frontier:
            if n is target:
                expanded.extend(target.children)
            else:
                expanded.append(n)

        if _frontier_tokens(expanded) > budget:
            return frontier
        frontier = expanded


def _frontier_tokens(frontier: Iterable[Node | Leaf]) -> int:
    return sum(n.tokens for n in frontier)


# ---------------------------------------------------------------------------
# Compaction: across-tree budget allocator
# ---------------------------------------------------------------------------

def compact(forest: Forest, total_budget: int, scheduler: Scheduler) -> dict:
    """Allocate `total_budget` across turns via `scheduler`, then expand
    each turn to its allocation. Returns a structured result with the
    chosen frontiers and bookkeeping; callers render with `render.py`.
    """
    per_turn = scheduler(forest, total_budget)
    chosen: list[tuple[Turn, Frontier]] = []
    used_total = 0
    for t in forest:
        budget = per_turn.get(t.turn_id, 0)
        if t.root is None:
            # Pre-construction fallback: take leaves greedily from the
            # start of the turn until the budget runs out.
            frontier: Frontier = []
            tokens_used = 0
            for leaf in t.leaves:
                if tokens_used + leaf.tokens > budget and frontier:
                    break
                frontier.append(leaf)
                tokens_used += leaf.tokens
        else:
            frontier = expand_to_budget(t.root, budget)
        used_total += _frontier_tokens(frontier)
        chosen.append((t, frontier))
    return {
        "frontiers": chosen,
        "per_turn_budget": per_turn,
        "used_tokens": used_total,
        "total_budget": total_budget,
    }


# ---------------------------------------------------------------------------
# Shipped schedulers
# ---------------------------------------------------------------------------

def uniform(forest: Forest, budget: int) -> dict[str, int]:
    """Equal split across all turns."""
    if not forest:
        return {}
    per = budget // len(forest)
    return {t.turn_id: per for t in forest}


def recency_decay(forest: Forest, budget: int, half_life: float = 5.0) -> dict[str, int]:
    """Exponential decay: most-recent turn gets the most budget.

    Weight for the kth-most-recent turn is 0.5 ** (k / half_life).
    Total budget is split proportionally.
    """
    if not forest:
        return {}
    weights: dict[str, float] = {}
    for k, t in enumerate(reversed(forest)):
        weights[t.turn_id] = 0.5 ** (k / half_life)
    total_w = sum(weights.values())
    return {tid: int(budget * w / total_w) for tid, w in weights.items()}


def proportional(forest: Forest, budget: int) -> dict[str, int]:
    """Budget proportional to the turn's full token count."""
    if not forest:
        return {}
    sizes = {t.turn_id: max(1, _full_tokens(t)) for t in forest}
    total = sum(sizes.values())
    return {tid: int(budget * s / total) for tid, s in sizes.items()}


def last_k_raw(forest: Forest, budget: int, k: int = 3) -> dict[str, int]:
    """Last k turns get unlimited budget (the renderer will use root or
    full leaves as available); the rest share the remainder uniformly.

    If the last k turns alone exceed the budget, falls back to uniform
    allocation across the whole forest.
    """
    if not forest:
        return {}
    if k >= len(forest):
        return uniform(forest, budget)
    last = forest[-k:]
    rest = forest[:-k]
    raw_cost = sum(_full_tokens(t) for t in last)
    if raw_cost >= budget:
        return uniform(forest, budget)
    per_rest = (budget - raw_cost) // max(1, len(rest))
    out = {t.turn_id: _full_tokens(t) for t in last}
    out.update({t.turn_id: per_rest for t in rest})
    return out


def cap(scheduler: Scheduler, max_per_turn: int) -> Scheduler:
    """Wrap any scheduler to cap per-turn allocations and redistribute
    overflow uniformly to the un-capped turns.
    """
    def wrapped(forest: Forest, budget: int) -> dict[str, int]:
        alloc = scheduler(forest, budget)
        overflow = sum(max(0, v - max_per_turn) for v in alloc.values())
        capped = {tid: min(v, max_per_turn) for tid, v in alloc.items()}
        targets = [tid for tid, v in alloc.items() if v <= max_per_turn]
        if targets and overflow:
            bonus = overflow // len(targets)
            for tid in targets:
                capped[tid] += bonus
        return capped
    return wrapped


# Registry for config-driven instantiation.
SCHEDULERS: dict[str, Callable] = {
    "uniform": uniform,
    "recency_decay": recency_decay,
    "proportional": proportional,
    "last_k_raw": last_k_raw,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_tokens(turn: Turn) -> int:
    """Token count if the entire turn were rendered raw (all leaves)."""
    return sum(l.tokens for l in turn.leaves)
