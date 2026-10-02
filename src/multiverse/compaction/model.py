"""Turns, leaves and summary trees, and how a tree is stored by reference."""
from dataclasses import dataclass, field
from typing import Any, Union


def estimate_tokens(text: str) -> int:
    """About four characters a token, as Multiverse v1 counts without tiktoken."""
    return max(1, len(text) // 4) if text else 0


@dataclass
class Leaf:
    """One unit of a turn, as its harness read it from the transcript.

    `refs` are the harness's ids for the transcript rows the leaf stands for
    (Claude: row UUIDs). `tokens` estimates what the leaf costs kept verbatim,
    `images` the part of it that is images; `weight`, when set, is what it
    counts for when a scheduler shares a budget (a harness may discount bulky
    tool output). `content` is the harness's own payload and is never stored in
    a tree.
    """
    kind: str
    refs: tuple[str, ...]
    tokens: int
    preview: str = ''
    content: dict = field(default_factory=dict)
    weight: int | None = None
    images: int = 0

    @property
    def share(self) -> int:
        return self.tokens if self.weight is None else self.weight


@dataclass
class Node:
    """A summary of a contiguous span of a frontier, made by one merge.

    IDs follow merge order, so a parent's ID is greater than its children's
    and the root's is the greatest.
    """
    id: int
    children: list
    summary: str
    tokens: int

    @property
    def delta(self) -> int:
        """What expanding this node into its children adds to a frontier."""
        return sum(child.tokens for child in self.children) - self.tokens


Entry = Union[Leaf, Node]


@dataclass
class Turn:
    """One prompt and everything the agent did until the next prompt.

    The prompt is kept verbatim and is never summarized; `leaves` are the
    compactable rest, and `root` their tree once built (a single leaf is its
    own root).

    `dropped`, when set, is why the harness does not count the turn as
    conversation (Claude: a display-only command such as `/compact` or
    `/context`). Such a turn is never summarized or kept: compaction leaves it
    out entirely.
    """
    id: str
    prompt: Leaf | None
    leaves: list[Leaf]
    root: Entry | None = None
    dropped: str | None = None

    @property
    def tokens(self) -> int:
        return sum(leaf.tokens for leaf in self.leaves)

    @property
    def weight(self) -> int:
        return sum(leaf.share for leaf in self.leaves)


def leaves_under(entry: Entry):
    if isinstance(entry, Leaf):
        yield entry
    else:
        for child in entry.children:
            yield from leaves_under(child)


def tree_record(turn: Turn, root: Entry, **meta) -> dict[str, Any]:
    """The stored form of a turn's tree: summaries and structure, leaves by reference."""
    index = {id(leaf): position for position, leaf in enumerate(turn.leaves)}

    def encode(entry):
        if isinstance(entry, Leaf):
            return {'leaf': index[id(entry)]}
        return {'id': entry.id, 'summary': entry.summary, 'tokens': entry.tokens,
                'children': [encode(child) for child in entry.children]}

    leaves = [{'kind': leaf.kind, 'refs': list(leaf.refs), 'tokens': leaf.tokens} for leaf in turn.leaves]
    return {'schema': 1, 'turn': turn.id, 'leaves': leaves, 'root': encode(root), **meta}


def tree_from_record(turn: Turn, record: dict[str, Any]) -> Entry | None:
    """The turn's tree from its stored form, or None when it no longer fits the turn.

    A tree fits only the exact leaves it was built over: a turn the harness now
    reads differently (a parser change, a rewound conversation) needs a new one.
    """
    try:
        stored = [(leaf['kind'], tuple(leaf['refs'])) for leaf in record['leaves']]
        if record.get('schema') != 1 or stored != [(leaf.kind, leaf.refs) for leaf in turn.leaves]:
            return None

        def decode(value):
            if 'leaf' in value:
                return turn.leaves[value['leaf']]
            children = [decode(child) for child in value['children']]
            if len(children) < 2 or not isinstance(value['summary'], str):
                raise ValueError('malformed node')
            return Node(int(value['id']), children, value['summary'], int(value['tokens']))

        root = decode(record['root'])
        covered = [id(leaf) for leaf in leaves_under(root)]
        if covered != [id(leaf) for leaf in turn.leaves]:
            return None
        return root
    except (KeyError, TypeError, ValueError, IndexError):
        return None
