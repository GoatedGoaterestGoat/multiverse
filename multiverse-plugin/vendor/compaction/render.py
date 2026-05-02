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

import json as _json

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
# Compaction output — text mode (default)
# ---------------------------------------------------------------------------

def render_frontier_as_messages(frontier: Frontier) -> str:
    """Flatten a chosen frontier into a readable transcript.

    For a Leaf, emits its content (kind-aware).
    For a Node, emits its summary marked as a compressed-region.

    Lossy on thinking blocks (rendered as `[thinking: encrypted]` placeholder).
    Use `render_frontier_as_message_blocks` for byte-equivalent thinking-block
    preservation.
    """
    lines: list[str] = []
    for entry in frontier:
        lines.append(_render_entry(entry))
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Compaction output — structured mode (Anthropic content blocks)
# ---------------------------------------------------------------------------

def render_frontier_as_message_blocks(frontier: Frontier) -> list[dict]:
    """Render a frontier as a list of Anthropic content blocks suitable for
    use as a single assistant-role message's content array.

    Thinking leaves emit `{"type":"thinking", "thinking":..., "signature":...}`
    blocks **verbatim**. The signature carries the encrypted thinking content;
    when fed back to the API under the same credential, the server validates
    the signature and decrypts, giving the next-turn model server-side access
    to the original chain-of-thought.

    All other leaves and Node summaries emit `{"type":"text", "text":...}`.
    Tool calls become text descriptions rather than `{"type":"tool_use"}`
    blocks because tool_use requires a matching tool_result in a following
    user message, which a compacted assistant-only output can't supply.

    Returns a list of dicts shaped like Anthropic's content-block schema. The
    caller wraps them in a `{"role":"assistant", "content":[...]}` message
    when feeding back to the live agent.
    """
    blocks: list[dict] = []
    for entry in frontier:
        if isinstance(entry, Leaf):
            if entry.kind == "thinking":
                blocks.append({
                    "type": "thinking",
                    "thinking": entry.content.get("text", ""),
                    "signature": entry.content.get("signature", ""),
                })
            else:
                blocks.append({"type": "text", "text": _render_leaf_for_block(entry)})
        else:  # Node summary
            n = _span_size(entry)
            blocks.append({
                "type": "text",
                "text": _render_summary_block(entry.summary.strip(), n),
            })
    return blocks


def _render_summary_block(summary: str, n_entries: int) -> str:
    """Render a Node summary as a markdown blockquote prefixed by a bold
    label. Important: claude code's REPL discards ``'html'`` tokens
    (and their content) from the markdown lexer, so XML-style wrappers
    like ``<compacted-context>...</compacted-context>`` render as
    nothing — empty bullet points in the UI. Markdown blockquotes do
    render."""
    quoted = "\n".join(
        f"> {line}" if line.strip() else ">"
        for line in summary.split("\n")
    )
    return f"**Compacted summary ({n_entries} entries):**\n\n{quoted}"


def _render_leaf_for_block(leaf: Leaf) -> str:
    """Render a non-thinking leaf as the body of a ``text`` block inside
    an assistant message in the compacted live JSONL.

    Format choice: markdown delimiters (bold labels, fenced code
    blocks, blockquotes) rather than XML-like tags. claude code's
    markdown lexer produces ``'html'`` tokens for raw HTML/XML, and
    its renderer discards those tokens (and their inner content)
    entirely — so XML wrappers render as empty bullets. Markdown
    delimiters render normally.
    """
    kind = leaf.kind
    if kind == "asst_text":
        return leaf.content.get("text", "")
    if kind == "user_text":
        # Mid-turn synthetic user record (e.g. Stop hook feedback) that
        # the parser captured under this turn's promptId. We're inside
        # an assistant message, so prefix it clearly + blockquote.
        text = leaf.content.get("text", "")
        quoted = "\n".join(
            f"> {line}" if line.strip() else ">"
            for line in text.split("\n")
        )
        return f"**[Embedded user message]**\n\n{quoted}"
    if kind == "tool_use":
        name = leaf.content.get("name", "?")
        inp = leaf.content.get("input", {})
        inp_s = (
            _json.dumps(inp, indent=2)
            if isinstance(inp, (dict, list)) else str(inp)
        )
        fence = _safe_fence(inp_s)
        return f"**Tool call: `{name}`**\n\n{fence}json\n{inp_s}\n{fence}"
    if kind == "tool_result":
        text = leaf.content.get("text", "")
        fence = _safe_fence(text)
        return f"**Tool result:**\n\n{fence}\n{text}\n{fence}"
    if kind == "tool_exchange":
        name = leaf.content.get("name", "?")
        inp = leaf.content.get("input", {})
        result = leaf.content.get("result_text", "")
        is_error = bool(leaf.content.get("is_error"))
        inp_s = (
            _json.dumps(inp, indent=2)
            if isinstance(inp, (dict, list)) else str(inp)
        )
        marker = " *(error)*" if is_error else ""
        in_fence = _safe_fence(inp_s)
        out_fence = _safe_fence(result)
        return (
            f"**Tool call: `{name}`**{marker}\n\n"
            f"{in_fence}json\n{inp_s}\n{in_fence}\n\n"
            f"**Result:**\n\n"
            f"{out_fence}\n{result}\n{out_fence}"
        )
    # Defensive — unknown kind. Use a labeled paragraph rather than an
    # XML wrapper so it still renders.
    return f"**[{kind}]** {leaf.content!r}"


def _safe_fence(content: str) -> str:
    """Pick a code-fence length longer than the longest run of backticks
    inside ``content`` so the fence doesn't get accidentally closed by
    embedded triple-backticks (common in shell output that quotes
    markdown)."""
    max_run = 0
    run = 0
    for ch in content:
        if ch == "`":
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    return "`" * max(3, max_run + 1)


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
