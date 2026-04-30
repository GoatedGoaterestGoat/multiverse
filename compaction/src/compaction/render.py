"""Rendering: frontier <-> text formats.

Two distinct rendering jobs:

1. Constructor input — given the current frontier during construction, emit
   the indexed listing the constructor agent reasons over to pick the next
   merge. Compact, preview-only; the agent does NOT see full content.

2. Compaction output — given a final chosen frontier, emit the message
   stream that goes back into the live conversation (or a transcript file
   for evaluation).
"""

from __future__ import annotations

from .types import Frontier, Leaf, Node


# ---------------------------------------------------------------------------
# Constructor input
# ---------------------------------------------------------------------------

def render_frontier_for_constructor(frontier: Frontier) -> str:
    """Format the current frontier as an indexed list for the merge agent."""
    lines = [f"Current frontier ({len(frontier)} entries):"]
    for i, entry in enumerate(frontier):
        if isinstance(entry, Leaf):
            kind = entry.kind
        else:
            kind = f"node(span={_span_size(entry)})"
        preview = entry.preview.replace("\n", " ").replace('"', "'")
        lines.append(f"[{i}] {kind} | tokens={entry.tokens} | preview=\"{preview}\"")
    return "\n".join(lines)


def _span_size(node: Node) -> int:
    """Total leaves under a node (recursive)."""
    if isinstance(node, Leaf):
        return 1
    return sum(_span_size(c) for c in node.children)


# ---------------------------------------------------------------------------
# Compaction output
# ---------------------------------------------------------------------------

def render_frontier_as_messages(frontier: Frontier) -> str:
    """Flatten a chosen frontier into a readable transcript.

    For a Leaf, emits its content (kind-aware).
    For a Node, emits its summary marked as a compressed-region.
    """
    lines: list[str] = []
    for entry in frontier:
        lines.append(_render_entry(entry))
    return "\n\n".join(lines)


def _render_entry(entry: Leaf | Node) -> str:
    if isinstance(entry, Node):
        n = _span_size(entry)
        return f"[summary of {n} entries: {entry.summary.strip()}]"
    return _render_leaf(entry)


def _render_leaf(leaf: Leaf) -> str:
    if leaf.kind == "user_text":
        return f"USER: {leaf.content.get('text', '')}"
    if leaf.kind == "thinking":
        text = leaf.content.get("text", "")
        return f"[thinking] {text}" if text else "[thinking: encrypted]"
    if leaf.kind == "asst_text":
        return f"ASSISTANT: {leaf.content.get('text', '')}"
    if leaf.kind == "tool_use":
        name = leaf.content.get("name", "?")
        inp = leaf.content.get("input", {})
        return f"[tool_use {name}] input={inp}"
    if leaf.kind == "tool_result":
        return f"[tool_result] {leaf.content.get('text', '')}"
    if leaf.kind == "tool_exchange":
        name = leaf.content.get("name", "?")
        inp = leaf.content.get("input", {})
        result = leaf.content.get("result_text", "")
        is_error = leaf.content.get("is_error")
        marker = "ERROR" if is_error else "OK"
        return f"[tool_exchange {name}] input={inp}\n  -> {marker}: {result}"
    return f"[{leaf.kind}] {leaf.content}"
