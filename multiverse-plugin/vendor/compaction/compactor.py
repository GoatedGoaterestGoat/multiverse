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

from .tokenize import count_image_tokens, count_tokens
from .types import Forest, Frontier, Leaf, Node, Turn

Scheduler = Callable[[Forest, int], dict[str, int]]


# ---------------------------------------------------------------------------
# Verbatim user-prompt accounting
# ---------------------------------------------------------------------------
#
# A turn's user prompt is a verbatim separator: its TEXT is never compacted
# (and is tiny). Its IMAGES, however, can be large — and historically rode
# along for free, outside the budget entirely, so a 400k-budget compaction of
# an image-heavy session could still emit 800k+ tokens. The budget must bound
# the whole output, so user-prompt images are now droppable: we keep as many
# as fit a reserved image sub-budget (newest turns first) and replace the rest
# with a short text placeholder. User-prompt text is always kept (mandatory
# floor); assistant content gets whatever remains.

def _user_prompt_text_tokens(turn: Turn) -> int:
    """Token cost of a turn's verbatim user-prompt TEXT (images excluded).
    This is the un-droppable floor — the turn separator always keeps it."""
    if turn.user_prompt is None:
        return 0
    return count_tokens(turn.user_prompt.content.get("text", "") or "")


def _user_prompt_image_tokens(turn: Turn) -> int:
    """Token cost of a turn's verbatim user-prompt IMAGES."""
    if turn.user_prompt is None:
        return 0
    return sum(count_image_tokens(im) for im in (turn.user_prompt.images or []))


# ---------------------------------------------------------------------------
# Expansion: per-tree primitive (fixed, no knobs)
# ---------------------------------------------------------------------------

def expand_to_budget(root: Node | Leaf, budget: int) -> Frontier:
    """Expand a single tree's frontier from [root] until the next
    expansion would exceed the budget. Priority is always node.id
    (highest first = undo most recent merge first).

    Budget math uses `Leaf.tokens` directly — that field is the cost the
    live agent will pay if a leaf is preserved raw in the compacted output
    (structured rendering: thinking leaves stay as native blocks with
    signatures, billed at the original thinking length; everything else
    bills at its text-rendering cost). Node.delta is cached at construction
    time over the same `tokens` values, so the loop is O(N log N).

    If the root is a Leaf (single-leaf turn), returns [root].
    If `budget` is non-positive, returns [root] unchanged.
    """
    if budget <= 0 or not isinstance(root, Node) or not root.children:
        return [root]

    internals = _collect_internals(root)
    internals.sort(key=lambda n: -n.id)

    expanded: set[int] = set()
    total = root.tokens
    for n in internals:
        if total + n.delta > budget:
            break
        expanded.add(id(n))
        total += n.delta

    return _materialize_frontier(root, expanded)


def _collect_internals(root: Node | Leaf) -> list[Node]:
    """All internal Nodes in the subtree rooted at `root`, in arbitrary order."""
    out: list[Node] = []
    stack: list[Node | Leaf] = [root]
    while stack:
        n = stack.pop()
        if isinstance(n, Node):
            out.append(n)
            stack.extend(n.children)
    return out


def _materialize_frontier(root: Node | Leaf, expanded: set[int]) -> Frontier:
    """Walk `root` in tree order, expanding any Node whose `id()` is in
    `expanded` (recursively). Result tiles the original leaves in left-to-right
    order — the same shape the iterative algorithm would have produced.
    """
    out: Frontier = []
    def walk(n: Node | Leaf) -> None:
        if isinstance(n, Node) and id(n) in expanded:
            for c in n.children:
                walk(c)
        else:
            out.append(n)
    walk(root)
    return out


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


def recency_boosted(
    forest: Forest,
    budget: int,
    recency_ratio: float = 3.0,
    recency_half_life: float = 10.0,
) -> dict[str, int]:
    """Recency-boosted proportional.

    Each turn's weight is its leaves-token count multiplied by a
    recency multiplier that ranges from ``recency_ratio`` (newest
    turn) down toward ``1.0`` (oldest). After normalization, the
    newest turn's tokens count ``recency_ratio`` times as heavily
    per-token as the oldest — a large old turn still outweighs a
    tiny recent one because proportional stays as the floor.

    Formula:
      multiplier[k] = 1 + (recency_ratio - 1) / (1 + k / recency_half_life)
      weight[t]     = leaves_tokens[t] * multiplier[k(t)]

    where ``k`` is position from newest (0-indexed). At
    ``k == recency_half_life`` the multiplier equals
    ``(recency_ratio + 1) / 2`` — exactly halfway between max and
    baseline. Never drops below 1.0, so ancient content keeps at
    least its proportional share.

    Setting ``recency_ratio=1.0`` reduces to pure proportional.
    """
    if not forest:
        return {}
    ratio = max(1.0, recency_ratio)
    hl = max(1e-6, recency_half_life)
    weights: dict[str, float] = {}
    for k, t in enumerate(reversed(forest)):
        leaves = max(1, _full_tokens(t))
        multiplier = 1.0 + (ratio - 1.0) / (1.0 + k / hl)
        weights[t.turn_id] = leaves * multiplier
    total_w = sum(weights.values()) or 1.0
    return {tid: int(budget * w / total_w) for tid, w in weights.items()}


def proportional(forest: Forest, budget: int) -> dict[str, int]:
    """Budget proportional to the turn's full token count."""
    if not forest:
        return {}
    sizes = {t.turn_id: max(1, _full_tokens(t)) for t in forest}
    total = sum(sizes.values())
    return {tid: int(budget * s / total) for tid, s in sizes.items()}


def last_k_raw(forest: Forest, budget: int, k: int = 3) -> dict[str, int]:
    """Last ``k`` turns kept verbatim (allocated their full raw token
    cost); the remaining budget is divided across the earlier turns
    proportionally to each turn's own full token count.

    Fallback: if the last-k raw cost alone exceeds the budget, defer to
    plain ``proportional`` across the whole forest — trying to keep the
    tail verbatim under an already-blown budget would starve the rest
    entirely.
    """
    if not forest:
        return {}
    if k >= len(forest):
        return proportional(forest, budget)
    last = forest[-k:]
    rest = forest[:-k]
    raw_cost = sum(_full_tokens(t) for t in last)
    if raw_cost >= budget:
        return proportional(forest, budget)
    remaining = budget - raw_cost
    out: dict[str, int] = {t.turn_id: _full_tokens(t) for t in last}
    if rest:
        rest_sizes = {t.turn_id: max(1, _full_tokens(t)) for t in rest}
        rest_total = sum(rest_sizes.values())
        for tid, size in rest_sizes.items():
            out[tid] = int(remaining * size / rest_total)
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
    "recency_boosted": recency_boosted,
    "proportional": proportional,
    "last_k_raw": last_k_raw,
}


# ---------------------------------------------------------------------------
# Whole-output budget allocation (text floor + droppable images + assistant)
# ---------------------------------------------------------------------------

# Fraction of the post-text budget the kept user-prompt images may consume.
# The rest is reserved for assistant content so a single huge screenshot
# can't starve the actual conversation summary. Kept images are chosen
# newest-turn-first (recency); within the budget all-or-nothing per turn.
DEFAULT_IMAGE_BUDGET_FRACTION = 0.5


def allocate_budget(
    forest: Forest,
    total_budget: int,
    scheduler: Scheduler = proportional,
    image_budget_fraction: float = DEFAULT_IMAGE_BUDGET_FRACTION,
) -> tuple[dict[str, int], set[str]]:
    """Split ``total_budget`` across the WHOLE compacted output.

    Unlike a bare scheduler (which only sizes the compactable assistant
    leaves), this accounts for the verbatim user-prompt cost that the
    renderer always emits on top:

      1. User-prompt TEXT is a mandatory floor — summed and subtracted off
         the top. (Tiny; it's the turn separator.)
      2. User-prompt IMAGES are droppable. Up to
         ``image_budget_fraction`` of the remaining budget is spent keeping
         them, newest turn first, all-or-nothing per turn. Turns not in the
         returned keep-set have their images replaced by a placeholder.
      3. Whatever remains funds the assistant content, divided by
         ``scheduler``.

    Returns ``(per_turn_assistant_budget, keep_image_turn_ids)``. Both are
    consumed by the renderer (``compact_forest_to_session_jsonl``).

    The result guarantees the rendered output is approximately bounded by
    ``total_budget`` (within summary/rendering slack), instead of letting
    un-budgeted user images blow past it.
    """
    if not forest:
        return {}, set()

    # 1. Mandatory user-prompt text floor.
    text_floor = sum(_user_prompt_text_tokens(t) for t in forest)
    remaining = max(0, total_budget - text_floor)

    # 2. Keep user-prompt images newest-first within the image sub-budget.
    image_budget = int(remaining * image_budget_fraction)
    keep_images: set[str] = set()
    spent_on_images = 0
    for turn in reversed(forest):  # newest first
        cost = _user_prompt_image_tokens(turn)
        if cost <= 0:
            continue
        if spent_on_images + cost <= image_budget:
            keep_images.add(turn.turn_id)
            spent_on_images += cost

    # 3. Assistant content gets the rest.
    assistant_budget = max(0, remaining - spent_on_images)
    per_turn = scheduler(forest, assistant_budget)
    return per_turn, keep_images


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_tokens(turn: Turn) -> int:
    """Token count if the entire turn were rendered raw (all leaves)."""
    return sum(l.tokens for l in turn.leaves)
