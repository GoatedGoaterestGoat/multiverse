"""Sharing a budget across turns, and expanding each turn's tree to its share.

A scheduler maps turns to the tokens of assistant content each may keep. Prompts
are always kept verbatim and are not part of the budget. A turn whose share
covers all of its leaves is kept raw and needs no tree; any other turn keeps
the frontier its tree expands to within its share.

Expansion is fixed: start from the root and undo the most recent merge (the
highest node ID) while the next undo still fits.
"""
from .config import Schedule
from .model import Entry, Leaf, Node, Turn


def _shares(turns: list[Turn], budget: int, weights: list[float]) -> dict[str, int]:
    total = sum(weights)
    if not turns:
        return {}
    if total <= 0:
        return {turn.id: budget // len(turns) for turn in turns}
    return {turn.id: int(budget * weight / total) for turn, weight in zip(turns, weights)}


def uniform(turns, budget):
    return {turn.id: budget // len(turns) for turn in turns} if turns else {}


def proportional(turns, budget):
    """Each turn's share follows its size (bulky tool output discounted by the harness)."""
    return _shares(turns, budget, [max(1, turn.weight) for turn in turns])


def recency_boosted(turns, budget, ratio=3.0, half_life=10.0):
    """Proportional, with the newest turn's tokens counting `ratio` times as much.

    The multiplier falls from `ratio` toward 1 with distance from the newest
    turn; at `half_life` turns back it is halfway.
    """
    ratio, half_life = max(1.0, ratio), max(1e-6, half_life)
    count = len(turns)
    weights = [max(1, turn.weight) * (1.0 + (ratio - 1.0) / (1.0 + (count - 1 - index) / half_life))
               for index, turn in enumerate(turns)]
    return _shares(turns, budget, weights)


def last_k_raw(turns, budget, k=2):
    """The last `k` turns whole; `budget` shared among the earlier ones by size.

    Unlike Multiverse v1, the last k stay raw even when they alone exceed the
    budget, so a budget of 0 summarizes every earlier turn to its root.
    """
    tail = turns[len(turns) - k:] if k else []
    earlier = turns[:len(turns) - len(tail)]
    return {**proportional(earlier, budget), **{turn.id: turn.tokens for turn in tail}}


def allocate(turns: list[Turn], schedule: Schedule) -> dict[str, int]:
    if schedule.scheduler == 'last_k_raw':
        return last_k_raw(turns, schedule.budget, schedule.k)
    if schedule.scheduler == 'recency_boosted':
        return recency_boosted(turns, schedule.budget, schedule.recency_ratio, schedule.recency_half_life)
    if schedule.scheduler == 'uniform':
        return uniform(turns, schedule.budget)
    return proportional(turns, schedule.budget)


def kept_raw(turn: Turn, share: int) -> bool:
    return len(turn.leaves) <= 1 or share >= turn.tokens


def needs_tree(turn: Turn, share: int) -> bool:
    return not kept_raw(turn, share)


def expand(root: Entry, budget: int) -> list[Entry]:
    """The frontier of `root` after undoing merges, newest first, while they fit `budget`."""
    if not isinstance(root, Node) or budget <= 0:
        return [root]
    internals, stack = [], [root]
    while stack:
        entry = stack.pop()
        if isinstance(entry, Node):
            internals.append(entry)
            stack.extend(entry.children)
    internals.sort(key=lambda node: -node.id)
    expanded, total = set(), root.tokens
    for node in internals:
        if total + node.delta > budget:
            break
        expanded.add(id(node))
        total += node.delta
    frontier = []

    def walk(entry):
        if isinstance(entry, Node) and id(entry) in expanded:
            for child in entry.children:
                walk(child)
        else:
            frontier.append(entry)

    walk(root)
    return frontier


def frontier_for(turn: Turn, share: int) -> list[Entry]:
    """What the turn keeps for `share`: all its leaves when raw, else its expanded tree."""
    if kept_raw(turn, share):
        return list(turn.leaves)
    if turn.root is None:
        raise ValueError(f'Turn {turn.id} needs a summary tree and has none')
    return expand(turn.root, share)


def frontier_tokens(frontier: list[Entry]) -> int:
    return sum(entry.tokens for entry in frontier)


def kind_of(entry: Entry) -> str:
    return 'summary' if isinstance(entry, Node) else entry.kind if isinstance(entry, Leaf) else 'unknown'
