"""Parse a Claude Code JSONL session log into a Forest of Turns.

A turn is delimited by a `user`-with-string-content row (the actual user
prompt). Everything between two user prompts (assistant content blocks
and tool results) belongs to the prior prompt's turn.

Metadata rows (custom-title, file-history-snapshot, attachment, system,
queue-operation, last-prompt, agent-name, permission-mode) are dropped.

Each surviving JSONL row becomes one Leaf. Auto-merging tool_use/tool_result
pairs is a separate preprocess step (see preprocess.py); the parser leaves
them as distinct leaves with linkage info preserved on `Leaf.content`.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from .tokenize import count_tokens
from .types import Forest, Leaf, Turn

# Top-level `type` values that carry no compactable content.
SKIP_TYPES = frozenset({
    "custom-title",
    "agent-name",
    "permission-mode",
    "file-history-snapshot",
    "attachment",
    "last-prompt",
    "queue-operation",
    "system",
})


def _iter_jsonl(path: str) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _classify_and_extract(row: dict[str, Any]) -> Leaf | None:
    """Turn one JSONL row into a Leaf, or None if the row is metadata.

    Each assistant API message is split across multiple JSONL rows in
    Claude Code (one per content block: thinking / text / tool_use), so
    each meaningful row has exactly one content block. We rely on this.
    """
    t = row.get("type")
    if t in SKIP_TYPES or t is None:
        return None

    msg = row.get("message")
    if not isinstance(msg, dict):
        return None

    content = msg.get("content")
    uuid = row.get("uuid", "")

    # user/<string>: user prompt
    if t == "user" and isinstance(content, str):
        return Leaf(
            kind="user_text",
            content={"text": content},
            tokens=count_tokens(content),
            preview=_preview(content, "user"),
            source_uuids=[uuid],
        )

    # user/<list>: tool_result, or rare user text-in-list
    if t == "user" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                result_content = block.get("content")
                # `content` may be a string or a list of {type:text,text:...}
                if isinstance(result_content, list):
                    text = "\n".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = str(result_content) if result_content is not None else ""
                return Leaf(
                    kind="tool_result",
                    content={
                        "tool_use_id": block.get("tool_use_id"),
                        "text": text,
                        "is_error": block.get("is_error", False),
                    },
                    tokens=count_tokens(text),
                    preview=_preview(text, "tool_result"),
                    source_uuids=[uuid],
                )
            if btype == "text":
                text = block.get("text", "")
                return Leaf(
                    kind="user_text",
                    content={"text": text},
                    tokens=count_tokens(text),
                    preview=_preview(text, "user"),
                    source_uuids=[uuid],
                )
        return None

    # assistant/<list>: one content block per row in this format
    if t == "assistant" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                text = block.get("thinking", "")
                return Leaf(
                    kind="thinking",
                    content={"text": text, "signature": block.get("signature")},
                    tokens=count_tokens(text),
                    preview=_preview(text, "thinking") if text else "<thinking: encrypted>",
                    source_uuids=[uuid],
                )
            if btype == "text":
                text = block.get("text", "")
                return Leaf(
                    kind="asst_text",
                    content={"text": text},
                    tokens=count_tokens(text),
                    preview=_preview(text, "asst"),
                    source_uuids=[uuid],
                )
            if btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                tool_use_id = block.get("id")
                rendered = f"{name}({_short_args(inp)})"
                return Leaf(
                    kind="tool_use",
                    content={
                        "tool_use_id": tool_use_id,
                        "name": name,
                        "input": inp,
                    },
                    tokens=count_tokens(rendered) + count_tokens(json.dumps(inp)),
                    preview=rendered,
                    source_uuids=[uuid],
                )
        return None

    return None


def _short_args(inp: Any, max_len: int = 80) -> str:
    """One-line rendering of a tool's input for previews."""
    if isinstance(inp, dict):
        # Prefer common keys when available
        for key in ("command", "file_path", "path", "url", "query", "pattern"):
            if key in inp and isinstance(inp[key], str):
                v = inp[key]
                return v if len(v) <= max_len else v[: max_len - 3] + "..."
        # Fallback to JSON
        s = json.dumps(inp)
    else:
        s = str(inp)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _preview(text: str, kind: str, max_len: int = 120) -> str:
    """Compact one-line preview suitable for agent prompts."""
    if not text:
        return f"<empty {kind}>"
    flat = " ".join(text.split())
    return flat if len(flat) <= max_len else flat[: max_len - 3] + "..."


def _is_user_prompt_row(row: dict[str, Any]) -> bool:
    """True if this row starts a new turn (a user prompt, not a tool_result)."""
    if row.get("type") != "user":
        return False
    msg = row.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return True
    return False


def parse_session(path: str) -> Forest:
    """Parse a Claude Code JSONL session log into a Forest of Turns."""
    turns: Forest = []
    current: Turn | None = None
    pending_orphans: list[Leaf] = []

    for row in _iter_jsonl(path):
        leaf = _classify_and_extract(row)

        if _is_user_prompt_row(row):
            if current is not None:
                turns.append(current)
            turn_id = row.get("promptId") or row.get("uuid") or f"turn-{len(turns)}"
            current = Turn(turn_id=turn_id, leaves=[])
            # Drain any pre-first-prompt orphans into this turn
            if pending_orphans:
                current.leaves.extend(pending_orphans)
                pending_orphans = []
            if leaf is not None:
                current.leaves.append(leaf)
            continue

        # Non-prompt row
        if leaf is None:
            continue
        if current is None:
            # Content before any user prompt; stash and attach to first turn
            pending_orphans.append(leaf)
        else:
            current.leaves.append(leaf)

    if current is not None:
        turns.append(current)
    elif pending_orphans:
        # Edge case: file with content but no user prompt at all
        turns.append(Turn(turn_id="turn-0", leaves=pending_orphans))

    return turns
