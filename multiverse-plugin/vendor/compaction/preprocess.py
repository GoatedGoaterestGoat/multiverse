"""Preprocessing transforms applied to a turn's leaves before construction.

Currently shipped:

* `auto_merge_tool_pairs` — collapse each `tool_use` leaf with its matching
  `tool_result` leaf (by `tool_use_id`) into a single `tool_exchange` leaf.
  This roughly halves the leaf count on tool-heavy turns and gives the
  constructor agent a more useful unit to reason about.

* `collapse_consecutive` — collapse runs of consecutive same-kind leaves
  (e.g. several thinking blocks in a row) into one leaf. Off by default;
  enable per-kind via config.

Pre-summarization of oversized leaves (running Haiku on huge tool outputs
to generate a tighter preview while keeping full content for lossless
fallback) is a separate step in the pipeline and lives in `summarize.py`
(not yet implemented).
"""

from __future__ import annotations

from dataclasses import dataclass

from .tokenize import count_tokens
from .types import Leaf, Turn


@dataclass
class PreprocessConfig:
    # Disabled by default: tool_use and tool_result are kept as separate
    # leaves so the constructor agent sees the true call/result structure
    # and can decide on its own when (and whether) to merge them.
    auto_merge_tool_pairs: bool = False
    collapse_consecutive_thinking: bool = False
    collapse_consecutive_asst_text: bool = False
    preview_max_len: int = 120


def preprocess_turn(turn: Turn, config: PreprocessConfig | None = None) -> Turn:
    """Apply all enabled preprocessing transforms to a turn's leaves."""
    cfg = config or PreprocessConfig()
    leaves = list(turn.leaves)

    if cfg.auto_merge_tool_pairs:
        leaves = auto_merge_tool_pairs(leaves, max_preview=cfg.preview_max_len)

    if cfg.collapse_consecutive_thinking:
        leaves = collapse_consecutive(leaves, "thinking", max_preview=cfg.preview_max_len)

    if cfg.collapse_consecutive_asst_text:
        leaves = collapse_consecutive(leaves, "asst_text", max_preview=cfg.preview_max_len)

    return Turn(turn_id=turn.turn_id, user_prompt=turn.user_prompt, leaves=leaves)


def preprocess_forest(forest, config: PreprocessConfig | None = None):
    """Apply preprocessing to every turn in a forest."""
    return [preprocess_turn(t, config) for t in forest]


# ---------------------------------------------------------------------------
# auto-merge tool_use + tool_result -> tool_exchange
# ---------------------------------------------------------------------------

def auto_merge_tool_pairs(leaves: list[Leaf], max_preview: int = 120) -> list[Leaf]:
    """Pair each tool_use leaf with its matching tool_result leaf.

    Matching is by `tool_use_id`. The merged `tool_exchange` leaf is placed
    at the position of the original tool_use; the original tool_result is
    dropped. Unpaired tool_use or tool_result leaves are passed through
    unchanged (rare but possible if the session was truncated mid-call).
    """
    # Index tool_results by tool_use_id
    result_by_id: dict[str, Leaf] = {}
    for leaf in leaves:
        if leaf.kind == "tool_result":
            tid = leaf.content.get("tool_use_id")
            if tid:
                result_by_id[tid] = leaf

    consumed_result_uuids: set[str] = set()
    out: list[Leaf] = []
    for leaf in leaves:
        if leaf.kind == "tool_use":
            tid = leaf.content.get("tool_use_id")
            result = result_by_id.get(tid) if tid else None
            if result is not None:
                # mark by source uuid (or any stable handle)
                for u in result.source_uuids:
                    consumed_result_uuids.add(u)
                out.append(_make_tool_exchange(leaf, result, max_preview))
            else:
                out.append(leaf)  # unpaired tool_use
        elif leaf.kind == "tool_result":
            if any(u in consumed_result_uuids for u in leaf.source_uuids):
                continue  # already merged into a tool_exchange
            out.append(leaf)  # orphan tool_result; keep as-is
        else:
            out.append(leaf)
    return out


def _make_tool_exchange(call: Leaf, result: Leaf, max_preview: int) -> Leaf:
    name = call.content.get("name", "?")
    inp = call.content.get("input", {})
    args_preview = _short_args(inp, max_len=60)

    is_error = bool(result.content.get("is_error"))
    result_text = result.content.get("text", "") or ""
    result_summary = _summarize_result(result_text, is_error)

    full_preview = f"{name}({args_preview}) -> {result_summary}"
    if len(full_preview) > max_preview:
        full_preview = full_preview[: max_preview - 3] + "..."

    # The tokens we account for are call_args + result_content; this is
    # what would have been billed in the live conversation.
    tokens = call.tokens + result.tokens

    return Leaf(
        kind="tool_exchange",
        content={
            "tool_use_id": call.content.get("tool_use_id"),
            "name": name,
            "input": inp,
            "result_text": result_text,
            "is_error": is_error,
        },
        tokens=tokens,
        preview=full_preview,
        source_uuids=list(call.source_uuids) + list(result.source_uuids),
    )


def _short_args(inp, max_len: int = 60) -> str:
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "url", "query", "pattern"):
            if key in inp and isinstance(inp[key], str):
                v = inp[key]
                return v if len(v) <= max_len else v[: max_len - 3] + "..."
        import json
        s = json.dumps(inp)
    else:
        s = str(inp)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _summarize_result(text: str, is_error: bool, max_len: int = 50) -> str:
    """One-line outcome summary of a tool result for preview rendering.

    For v1 this is a deterministic heuristic: error vs ok, line count if
    multi-line, token count for big payloads. A future pre-summarize step
    can replace this with a Haiku-generated outcome line.
    """
    if not text:
        return "<empty result>"
    flat = text.strip()
    if is_error:
        first = flat.splitlines()[0] if flat else ""
        head = first if len(first) <= max_len else first[: max_len - 3] + "..."
        return f"error: {head}"
    nl = flat.count("\n") + 1
    toks = count_tokens(flat)
    if nl == 1:
        head = flat if len(flat) <= max_len else flat[: max_len - 3] + "..."
        return head
    return f"ok ({nl} lines, {toks} tok)"


# ---------------------------------------------------------------------------
# collapse consecutive same-kind leaves
# ---------------------------------------------------------------------------

def collapse_consecutive(leaves: list[Leaf], kind: str, max_preview: int = 120) -> list[Leaf]:
    """Merge runs of consecutive `kind` leaves into a single leaf.

    Concatenates content (with separators) and sums tokens. Used for
    thinking-block runs, repeated assistant text, etc.
    """
    out: list[Leaf] = []
    i = 0
    while i < len(leaves):
        leaf = leaves[i]
        if leaf.kind != kind:
            out.append(leaf)
            i += 1
            continue
        j = i + 1
        while j < len(leaves) and leaves[j].kind == kind:
            j += 1
        run = leaves[i:j]
        if len(run) == 1:
            out.append(run[0])
        else:
            out.append(_concat_run(run, max_preview))
        i = j
    return out


def _concat_run(run: list[Leaf], max_preview: int) -> Leaf:
    texts = []
    sources = []
    tokens = 0
    for leaf in run:
        t = leaf.content.get("text", "")
        if t:
            texts.append(t)
        tokens += leaf.tokens
        sources.extend(leaf.source_uuids)
    joined = "\n\n".join(texts)
    preview = " ".join(joined.split())
    if len(preview) > max_preview:
        preview = preview[: max_preview - 3] + "..."
    return Leaf(
        kind=run[0].kind,
        content={"text": joined, "n_collapsed": len(run)},
        tokens=tokens,
        preview=preview,
        source_uuids=sources,
    )
