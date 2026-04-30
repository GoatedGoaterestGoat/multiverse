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


def _build_usage_index(path: str) -> tuple[dict[str, int], dict[str, int]]:
    """Pre-pass over the JSONL: build per-assistant-message totals for
    `output_tokens` and visible-content tokens. Used to estimate the cost
    of redacted thinking blocks via `output_tokens - visible`.

    Visible content = sum of text-block tokens + tool_use (name + input)
    tokens. Thinking blocks have empty plaintext, so they contribute zero
    here; the residual after subtraction is the thinking estimate.
    """
    out_tok: dict[str, int] = {}
    visible_tok: dict[str, int] = {}
    for row in _iter_jsonl(path):
        if row.get("type") != "assistant":
            continue
        msg = row.get("message", {})
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        if not mid:
            continue
        u = msg.get("usage")
        if isinstance(u, dict) and "output_tokens" in u:
            out_tok[mid] = u["output_tokens"]
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                visible_tok[mid] = visible_tok.get(mid, 0) + count_tokens(block.get("text", ""))
            elif bt == "tool_use":
                nm = block.get("name", "")
                inp = json.dumps(block.get("input", {}))
                visible_tok[mid] = visible_tok.get(mid, 0) + count_tokens(nm + inp)
    return out_tok, visible_tok


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
                # tokens here is a baseline (count_tokens of any visible
                # plaintext). The post-pass in parse_session() overrides
                # this with the verbatim estimate from message-level usage
                # for redacted thinking — that's the cost the live agent
                # actually pays when this leaf is preserved as a native
                # thinking block in the compacted output.
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


def _is_user_prompt_row(row: dict[str, Any], last_pid: str | None) -> bool:
    """True if this row starts a new turn.

    The harness assigns a fresh `promptId` to every real user prompt and
    inherits it for everything (tool_results, hook-injected user records,
    assistant blocks) produced in response. So a `promptId` change is the
    canonical turn boundary; content-shape heuristics misfire on synthetic
    user records like "Stop hook feedback:" that share a promptId with the
    prompt they're paired to.
    """
    if row.get("type") != "user":
        return False
    pid = row.get("promptId")
    if pid is None or pid == last_pid:
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
    out_tok, visible_tok = _build_usage_index(path)

    turns: Forest = []
    current: Turn | None = None
    pending_orphans: list[Leaf] = []
    last_pid: str | None = None

    for row in _iter_jsonl(path):
        leaf = _classify_and_extract(row)

        # For thinking leaves, override `tokens` with an estimate of the
        # original API-billed thinking length: output_tokens of the source
        # message minus the visible content. This is the cost the live
        # agent will pay if this leaf is preserved as a native thinking
        # block in the compacted output (signature decrypts server-side).
        if leaf is not None and leaf.kind == "thinking":
            mid = (row.get("message") or {}).get("id")
            if mid and mid in out_tok:
                est = max(0, out_tok[mid] - visible_tok.get(mid, 0))
                if est > leaf.tokens:
                    leaf.tokens = est

        if _is_user_prompt_row(row, last_pid):
            if current is not None:
                turns.append(current)
            turn_id = row.get("promptId") or row.get("uuid") or f"turn-{len(turns)}"
            last_pid = row.get("promptId")
            # The user prompt is hoisted out of `leaves` — it acts as a
            # verbatim separator and never enters the compactable region.
            current = Turn(turn_id=turn_id, user_prompt=leaf, leaves=[])
            # Drain any pre-first-prompt orphans into this turn's leaves
            if pending_orphans:
                current.leaves.extend(pending_orphans)
                pending_orphans = []
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
        turns.append(Turn(turn_id="turn-0", user_prompt=None, leaves=pending_orphans))

    return turns
