"""Reading the on-disk summary-tree cache.

Trees live at ``<multiverse_root>/trees/<session-id>/<turn-id>.json``, written
by the tree-build worker (Claude Code) or ``compaction.pi_constructor`` (pi).
The cache is harness-neutral — the serialized shape is just
``compaction.types`` — so both compaction paths read it through here.

``compact_runner`` keeps its own copy of this walk for the moment; it carries
extra bookkeeping (aborted-stub accounting feeding the "should I spawn a
worker" gate) that only the monitor-driven path needs. This module is the
minimal read side, used by ``mvctl compact-plan``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .paths import multiverse_dir

#: The constructor's placeholder root when a build never got off the ground.
#: Treated as "no tree" — the greedy leaf-prefix fallback renders real content,
#: while this stub renders as an empty block.
ABORTED_STUB_SUMMARY = "<construction aborted; uncollapsed frontier>"


def cache_dir(session_id: str) -> Path:
    return multiverse_dir() / "trees" / session_id


def reconstruct(d: dict) -> Any:
    """Rebuild a ``Leaf``/``Node`` from its serialized form."""
    from compaction.types import Leaf, Node

    if d.get("type") == "leaf":
        return Leaf(
            kind=d["kind"],
            content=d.get("content", {}),
            tokens=d["tokens"],
            preview=d.get("preview", ""),
            source_uuids=d.get("source_uuids", []),
            images=d.get("images", []),
        )
    if d.get("type") == "node":
        return Node(
            id=d["id"],
            children=[reconstruct(c) for c in d.get("children", [])],
            summary=d.get("summary", ""),
            tokens=d["tokens"],
            preview=d.get("preview", ""),
        )
    raise TypeError(f"unknown tree node type: {d.get('type')!r}")


def is_aborted_stub(tree: Optional[dict]) -> bool:
    return (
        isinstance(tree, dict)
        and tree.get("type") == "node"
        and tree.get("summary") == ABORTED_STUB_SUMMARY
    )


def load_tree(session_id: str, turn_id: str) -> Optional[Any]:
    path = cache_dir(session_id) / f"{turn_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tree = data.get("tree")
    if tree is None or is_aborted_stub(tree):
        return None
    try:
        return reconstruct(tree)
    except (KeyError, TypeError):
        return None


def attach_cached_trees(forest, session_id: str) -> int:
    """Set ``turn.root`` from cache where a usable tree exists.

    Returns how many turns got one. The rest keep ``root is None``, which the
    expansion path already handles by taking leaves greedily from the start of
    the turn — so a compaction never has to wait for construction.
    """
    n = 0
    for turn in forest:
        tree = load_tree(session_id, turn.turn_id)
        if tree is not None:
            turn.root = tree
            n += 1
    return n
