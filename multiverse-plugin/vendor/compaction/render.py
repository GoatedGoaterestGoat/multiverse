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
import re as _re
import uuid as _uuidlib

from .types import Frontier, Leaf, Node


# Anthropic / Bedrock require tool_use.name to match this pattern; an output
# containing any non-conforming name gets the WHOLE request rejected with a
# 400 ValidationException on resume. Synthetic placeholders we emit for orphan
# tool calls (and any real tool whose name somehow carries odd chars) must be
# sanitized to satisfy it.
_TOOL_NAME_RE = _re.compile(r"[^a-zA-Z0-9_-]")

# Placeholder name for a synthetic tool_use we fabricate when a tool_result's
# matching tool_use was summarized away. Regex-safe (no parens/spaces).
_SUMMARIZED_TOOL_NAME = "summarized_tool"


def _safe_tool_name(name: str | None) -> str:
    """Coerce a tool name into the API-required ``[a-zA-Z0-9_-]+`` shape.

    Replaces any disallowed char with ``_`` and falls back to a non-empty
    placeholder. Compacted output is fed back via ``claude --resume``; a
    single bad ``toolUse.name`` (e.g. ``"(summarized)"`` or ``"?"``) makes
    the provider reject the entire request, so every emitted name must pass.
    """
    cleaned = _TOOL_NAME_RE.sub("_", str(name or "")).strip("_")
    return cleaned or "tool"


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
    """Render a Node summary as a bold-labeled paragraph.

    Important: claude code's REPL discards ``'html'`` tokens (and
    their content) from the markdown lexer, so XML-style wrappers
    like ``<compacted-context>...</compacted-context>`` render as
    nothing — empty bullet points in the UI.

    We previously wrapped the summary body in a markdown blockquote
    for visual distinction, but blockquote rendering varies between
    claude code versions: some draw a dim vertical bar + italic, some
    fill with a contrasting background that inverts the theme. The
    bold ``**Compacted summary (N entries):**`` header is enough of a
    marker; keeping the body as a plain paragraph renders consistently
    across versions.
    """
    return f"**Compacted summary ({n_entries} entries):**\n\n{summary}"


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
        # an assistant message, so prefix it clearly. Keep the body as
        # a plain paragraph rather than a blockquote — blockquote
        # rendering varies between claude code versions (dim bar vs.
        # background fill).
        text = leaf.content.get("text", "") or ""
        return f"**[Embedded user message]**\n\n{text}"
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


def render_frontier_as_messages(frontier: Frontier) -> list[dict]:
    """Render a frontier as a sequence of ``{role, content}`` messages
    suitable for feeding back into a live claude session as part of its
    compacted history.

    Differences from ``render_frontier_as_message_blocks``:

      - Returns MULTIPLE messages with alternating user/assistant roles.
        ``tool_use`` blocks land in an assistant message and are
        immediately followed by a user message containing the matching
        ``tool_result`` blocks — that's the API rule for resume.

      - Emits NATIVE block types (``tool_use`` / ``tool_result`` /
        user-role ``text``) instead of markdown text wrappers. claude
        code's REPL has dedicated renderers for these block types
        (``AssistantToolUseMessage`` / ``UserToolResultMessage``) that
        bypass the markdown pipeline entirely. The flat-blocks
        renderer (used by the constructor's synthetic merge sessions)
        keeps the markdown wrappers, since that path expects content
        in one assistant message.

      - Orphan ``tool_use`` leaves (matching ``tool_result`` got merged
        into a Node summary) are followed by a synthetic
        "(result summarized)" tool_result so the structure validates.
        Orphan ``tool_result`` leaves get a synthetic placeholder
        ``tool_use`` prepended.

      - The first message is forced to be assistant-role (a leading
        user-role record gets an empty assistant placeholder before
        it). The last message is forced to be assistant-role (a
        trailing tool_result gets a "(continuing)" placeholder
        assistant after it). Both rules keep role alternation valid
        across turn boundaries — the next turn always starts with the
        user_prompt row.

    Each message dict is ``{"role": "user"|"assistant", "content": [...],
    "source_leaves": [...]}``. ``source_leaves`` is a parallel list to
    ``content``: ``source_leaves[i]`` is the ``Leaf`` that produced
    ``content[i]``, or ``None`` for synthetic blocks (Node summaries,
    orphan-pair placeholders, first/last role-forcing filler). The
    per-message usage stamper in ``turn_to_rows`` needs this to sum
    thinking-leaf costs into ``usage.output_tokens``; keeping the same
    length as ``content`` avoids drift.
    """
    messages: list[dict] = []
    current_role: str | None = None
    current_blocks: list[dict] = []
    current_sources: list[Leaf | None] = []
    pending_tool_uses: set[str] = set()

    def flush() -> None:
        nonlocal current_blocks, current_sources, current_role
        if current_blocks:
            messages.append({
                "role": current_role,
                "content": current_blocks,
                "source_leaves": current_sources,
            })
            current_blocks = []
            current_sources = []
            current_role = None

    def append(role: str, block: dict, source: Leaf | None = None) -> None:
        nonlocal current_role
        if current_role is not None and role != current_role:
            flush()
        current_role = role
        current_blocks.append(block)
        current_sources.append(source)

    def synth_tu_id() -> str:
        return f"toolu_synth_{_uuidlib.uuid4().hex[:16]}"

    for entry in frontier:
        if isinstance(entry, Leaf):
            kind = entry.kind
            if kind == "thinking":
                append("assistant", {
                    "type": "thinking",
                    "thinking": entry.content.get("text", ""),
                    "signature": entry.content.get("signature", ""),
                }, source=entry)
            elif kind == "asst_text":
                append("assistant", {
                    "type": "text",
                    "text": entry.content.get("text", ""),
                }, source=entry)
            elif kind == "tool_use":
                tu_id = entry.content.get("tool_use_id") or synth_tu_id()
                append("assistant", {
                    "type": "tool_use",
                    "id": tu_id,
                    "name": _safe_tool_name(entry.content.get("name")),
                    "input": entry.content.get("input", {}) or {},
                }, source=entry)
                pending_tool_uses.add(tu_id)
            elif kind == "tool_result":
                tu_id = entry.content.get("tool_use_id") or synth_tu_id()
                if tu_id not in pending_tool_uses:
                    # Orphan tool_result — synthesize matching tool_use
                    # in an assistant message right before it. Synthetic
                    # placeholder, no originating leaf.
                    append("assistant", {
                        "type": "tool_use",
                        "id": tu_id,
                        "name": _SUMMARIZED_TOOL_NAME,
                        "input": {},
                    })
                pending_tool_uses.discard(tu_id)
                append("user", {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": entry.content.get("text", "") or "",
                    "is_error": bool(entry.content.get("is_error", False)),
                }, source=entry)
            elif kind == "user_text":
                text = entry.content.get("text", "") or ""
                # Emit a text block unless the message was image-only (no
                # text), then append the user message's images verbatim.
                # User-message images are kept (not summarized away); a
                # surviving user_text leaf carries them into the output. Note
                # tool_result images are intentionally NOT re-emitted — those
                # live in the compactable region and their information is
                # folded into summaries instead.
                imgs = getattr(entry, "images", None) or []
                if text or not imgs:
                    append("user", {"type": "text", "text": text}, source=entry)
                for img in imgs:
                    append("user", img, source=entry)
            elif kind == "tool_exchange":
                # Combined tool_use + tool_result leaf (auto-merge
                # preprocessing). Emit as a native pair across two
                # messages.
                tu_id = entry.content.get("tool_use_id") or synth_tu_id()
                append("assistant", {
                    "type": "tool_use",
                    "id": tu_id,
                    "name": _safe_tool_name(entry.content.get("name")),
                    "input": entry.content.get("input", {}) or {},
                }, source=entry)
                append("user", {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": entry.content.get("result_text", "") or "",
                    "is_error": bool(entry.content.get("is_error", False)),
                }, source=entry)
            else:
                # Defensive — unknown leaf kind. Render as a labeled
                # paragraph so it's at least visible.
                append("assistant", {
                    "type": "text",
                    "text": f"**[{kind}]** {entry.content!r}",
                }, source=entry)
        else:  # Node summary — markdown blockquote in an assistant message
            n = _span_size(entry)
            # Node.summary is text; no thinking cost to preserve, so
            # source_leaf is None (Node isn't a Leaf and doesn't
            # contribute to the round-trip usage stamp).
            append("assistant", {
                "type": "text",
                "text": _render_summary_block(entry.summary.strip(), n),
            })

    flush()

    # Resolve any remaining unresolved tool_uses with a final user
    # message of synthetic tool_results.
    if pending_tool_uses:
        synthetic_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": "(result summarized)",
                "is_error": False,
            }
            for tu_id in pending_tool_uses
        ]
        synthetic_sources = [None] * len(synthetic_blocks)
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"].extend(synthetic_blocks)
            messages[-1]["source_leaves"].extend(synthetic_sources)
        else:
            messages.append({
                "role": "user",
                "content": synthetic_blocks,
                "source_leaves": synthetic_sources,
            })

    # Force the FIRST message to be assistant-role: the surrounding
    # turn renderer always emits a user_prompt user-role row before
    # us, so a user-first compacted body would be user → user (invalid).
    if messages and messages[0]["role"] == "user":
        messages.insert(0, {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "_(no preceding assistant content)_"}
            ],
            "source_leaves": [None],
        })

    # Force the LAST message to be assistant-role: the next turn
    # always starts with a user_prompt user-role row, so a user-last
    # body would also be user → user (invalid).
    if messages and messages[-1]["role"] == "user":
        messages.append({
            "role": "assistant",
            "content": [
                {"type": "text", "text": "_(continuing)_"}
            ],
            "source_leaves": [None],
        })

    return messages


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
