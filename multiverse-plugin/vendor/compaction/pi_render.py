"""Write a compacted forest back into a pi session.

pi's compaction model is a better fit for frontier-coherent compaction than
Claude Code's, and this module leans on it rather than fighting it.

Claude Code has no compaction primitive we can address, so multiverse rewrites
the whole live JSONL and keeps an append-only "full" mirror alongside it just
to remember what the original was. pi instead stores a **``compaction`` entry**
in the same append-only tree: ``summary`` replaces everything up to
``firstKeptEntryId``, and the replaced entries stay in the file untouched.
That gives us for free the two properties the Claude path needs machinery for:

* **Non-destructive.** The original entries are never rewritten, so
  re-compacting always reaches back to the true history. No mirror JSONL, no
  uuid marker, no bootstrap-from-legacy path.
* **Undoable.** The compaction entry is one node in the tree; navigating off
  it restores the raw history.

So the split we emit is:

    [ oldest turns ] ──> rendered into `summary` (markdown)
    [ newest turns ] ──> left as real entries, addressed by `firstKeptEntryId`

The boundary is chosen from the expansion result: turns the budget let us keep
fully raw, at the tail, stay raw; everything else is summarized. Frontier
entries inside the summarized region render as markdown — a mix of verbatim
leaves and ``**Compacted summary (N entries):**`` blocks — so the live agent
can still tell which regions were compressed and how hard.
"""

from __future__ import annotations

import json
import time
import uuid as uuidlib
from typing import Any, Optional

from .compactor import allocate_budget, expand_to_budget, proportional
from .tokenize import count_tokens
from .types import Forest, Frontier, Leaf, Node, Turn

_SUMMARY_HEADER = (
    "The conversation below this point was compacted by multiverse "
    "(frontier-coherent tree compaction). Regions marked "
    "**Compacted summary (N entries)** replace N original entries; everything "
    "else is verbatim. Treat all of it as your own prior context."
)


# ---------------------------------------------------------------------------
# Markdown rendering of a chosen frontier
# ---------------------------------------------------------------------------

def _span_size(node: Node | Leaf) -> int:
    if isinstance(node, Leaf):
        return 1
    return sum(_span_size(c) for c in node.children)


def _fence(content: str) -> str:
    """Pick a fence long enough to survive backticks inside ``content``."""
    longest = 0
    run = 0
    for ch in content or "":
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def render_leaf(leaf: Leaf) -> str:
    kind = leaf.kind
    if kind == "asst_text":
        return leaf.content.get("text", "")
    if kind == "thinking":
        text = leaf.content.get("text", "") or ""
        # pi keeps reasoning in plaintext, so nothing is lost by flattening a
        # thinking leaf to a labeled block — unlike Claude Code, where the
        # content only exists inside an encrypted signature.
        return f"**[Reasoning]**\n\n{text}" if text else "**[Reasoning omitted]**"
    if kind == "user_text":
        text = leaf.content.get("text", "") or ""
        return f"**[User]**\n\n{text}"
    if kind == "tool_use":
        name = leaf.content.get("name", "?")
        inp = json.dumps(leaf.content.get("input", {}), indent=2, default=str)
        f = _fence(inp)
        return f"**Tool call: `{name}`**\n\n{f}json\n{inp}\n{f}"
    if kind == "tool_result":
        text = leaf.content.get("text", "") or ""
        marker = " *(error)*" if leaf.content.get("is_error") else ""
        f = _fence(text)
        return f"**Tool result:**{marker}\n\n{f}\n{text}\n{f}"
    if kind == "tool_exchange":
        name = leaf.content.get("name", "?")
        inp = json.dumps(leaf.content.get("input", {}), indent=2, default=str)
        result = leaf.content.get("result_text", "") or ""
        marker = " *(error)*" if leaf.content.get("is_error") else ""
        fi, fo = _fence(inp), _fence(result)
        return (
            f"**Tool call: `{name}`**{marker}\n\n{fi}json\n{inp}\n{fi}\n\n"
            f"**Result:**\n\n{fo}\n{result}\n{fo}"
        )
    return f"**[{kind}]** {leaf.content!r}"


def render_entry(entry: Node | Leaf) -> str:
    if isinstance(entry, Leaf):
        return render_leaf(entry)
    n = _span_size(entry)
    return f"**Compacted summary ({n} entries):**\n\n{entry.summary.strip()}"


def render_turn(turn: Turn, frontier: Frontier, keep_images: bool = True) -> str:
    """One turn as markdown: verbatim user prompt, then the chosen frontier."""
    parts: list[str] = []
    prompt = ""
    if turn.user_prompt is not None:
        prompt = turn.user_prompt.content.get("text", "") or ""
    n_images = len(turn.user_prompt.images) if turn.user_prompt else 0
    if n_images and not keep_images:
        prompt = f"{prompt}\n\n_[{n_images} image(s) omitted by compaction]_"
    parts.append(f"### User\n\n{prompt}" if prompt else "### User\n\n_(no text)_")
    body = "\n\n".join(render_entry(e) for e in frontier if _renderable(e))
    parts.append(f"### Assistant\n\n{body}" if body else "### Assistant\n\n_(no content)_")
    return "\n\n".join(parts)


def _renderable(entry: Node | Leaf) -> bool:
    if isinstance(entry, Leaf):
        return bool(entry.content.get("text") or entry.content.get("input")
                    or entry.content.get("result_text") or entry.images)
    return True


def render_summary(
    chosen: list[tuple[Turn, Frontier]],
    keep_image_turn_ids: Optional[set[str]] = None,
) -> str:
    """The full ``summary`` string of a pi compaction entry."""
    keep = keep_image_turn_ids or set()
    blocks = [_SUMMARY_HEADER]
    for i, (turn, frontier) in enumerate(chosen, 1):
        blocks.append(
            f"## Turn {i}\n\n"
            + render_turn(turn, frontier, keep_images=turn.turn_id in keep)
        )
    return "\n\n---\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Planning: which turns get summarized, which stay raw
# ---------------------------------------------------------------------------

def plan_compaction(
    forest: Forest,
    total_budget: int,
    *,
    scheduler=proportional,
    image_mode: str = "budget_fit",
) -> dict[str, Any]:
    """Expand every turn to its share of ``total_budget`` and split the forest.

    Returns ``{summary, first_kept_entry_id, tokens_before, tokens_after,
    n_summarized_turns, n_kept_turns, read_files, modified_files}``.

    A tail turn is "kept raw" only when its expansion recovered *every* leaf —
    i.e. the budget was generous enough that summarizing it would lose
    information for no gain. Handing those to pi as real entries rather than
    as summary text is strictly better: they stay addressable in the tree, and
    pi will re-summarize them itself on a later compaction instead of
    compounding our markdown.
    """
    per_turn, keep_images = allocate_budget(
        forest, total_budget, scheduler=scheduler, image_mode=image_mode
    )
    chosen: list[tuple[Turn, Frontier]] = []
    for turn in forest:
        budget = per_turn.get(turn.turn_id, 0)
        if turn.root is None:
            frontier: Frontier = []
            used = 0
            for leaf in turn.leaves:
                if used + leaf.tokens > budget and frontier:
                    break
                frontier.append(leaf)
                used += leaf.tokens
        else:
            frontier = expand_to_budget(turn.root, budget)
        chosen.append((turn, frontier))

    # Longest fully-raw suffix.
    split = len(chosen)
    while split > 0:
        turn, frontier = chosen[split - 1]
        if not _is_fully_raw(turn, frontier):
            break
        split -= 1
    # Never hand back an all-raw plan: if nothing needs summarizing the caller
    # should not have asked for a compaction, and pi rejects an empty summary.
    if split == 0:
        split = max(1, len(chosen) - 1)

    summarized = chosen[:split]
    kept = chosen[split:]
    first_kept = kept[0][0].turn_id if kept else None

    tokens_before = sum(_turn_full_tokens(t) for t, _ in chosen)
    tokens_after = (
        count_tokens(render_summary(summarized, keep_images))
        + sum(_turn_full_tokens(t) for t, _ in kept)
    )
    reads, mods = _file_ops(forest)
    return {
        "summary": render_summary(summarized, keep_images),
        "first_kept_entry_id": first_kept,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "n_summarized_turns": len(summarized),
        "n_kept_turns": len(kept),
        "read_files": reads,
        "modified_files": mods,
    }


def _is_fully_raw(turn: Turn, frontier: Frontier) -> bool:
    return all(isinstance(e, Leaf) for e in frontier) and len(frontier) == len(turn.leaves)


def _turn_full_tokens(turn: Turn) -> int:
    base = sum(l.tokens for l in turn.leaves)
    if turn.user_prompt is not None:
        base += turn.user_prompt.tokens
    return base


_READ_TOOLS = {"read", "glob", "grep", "list"}
_WRITE_TOOLS = {"edit", "write", "multiedit", "apply_patch"}


def _file_ops(forest: Forest) -> tuple[list[str], list[str]]:
    """Cumulative file tracking, matching pi's own ``details`` convention so
    ``<read-files>`` / ``<modified-files>`` stay populated across compactions."""
    reads: list[str] = []
    mods: list[str] = []
    for turn in forest:
        for leaf in turn.leaves:
            if leaf.kind not in ("tool_use", "tool_exchange"):
                continue
            name = str(leaf.content.get("name", "")).lower()
            inp = leaf.content.get("input") or {}
            if not isinstance(inp, dict):
                continue
            path = inp.get("path") or inp.get("file_path")
            if not isinstance(path, str):
                continue
            if name in _WRITE_TOOLS and path not in mods:
                mods.append(path)
            elif name in _READ_TOOLS and path not in reads:
                reads.append(path)
    return reads, mods


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def compact_forest_to_pi_entry(
    forest: Forest,
    total_budget: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan shaped for an extension's ``session_before_compact`` return value."""
    plan = plan_compaction(forest, total_budget, **kwargs)
    return {
        "summary": plan["summary"],
        "firstKeptEntryId": plan["first_kept_entry_id"],
        "tokensBefore": plan["tokens_before"],
        "details": {
            "readFiles": plan["read_files"],
            "modifiedFiles": plan["modified_files"],
            "multiverse": {
                "algorithm": "frontier-coherent-tree",
                "budget": total_budget,
                "tokensAfter": plan["tokens_after"],
                "summarizedTurns": plan["n_summarized_turns"],
                "keptTurns": plan["n_kept_turns"],
            },
        },
    }


def _short_id() -> str:
    return uuidlib.uuid4().hex[:8]


def compact_forest_to_pi_jsonl(
    forest: Forest,
    total_budget: int,
    *,
    session_id: str,
    cwd: str,
    source_path: str,
    **kwargs: Any,
) -> str:
    """Full transcript text for the whole-session compaction path.

    The original entries are reproduced byte-for-byte and a ``compaction``
    entry carries the summary, so the pre-compaction history stays addressable
    in the tree and a bad compaction costs one line.

    Re-compaction **replaces** our previous compaction entry in place rather
    than appending a second one, keeping its id and parentId so entries added
    after it stay linked. Appending would leave the superseded summary inside
    the new one's kept range, where pi would render it as a second
    ``compactionSummary`` describing the same content we just re-summarized
    from source. One entry per session, always regenerated from the original
    turns, is the invariant.
    """
    plan = plan_compaction(forest, total_budget, **kwargs)
    lines = _read_lines(source_path)
    prior_idx, prior = _find_prior_compaction(lines)

    entry = {
        "type": "compaction",
        "id": (prior or {}).get("id") or _short_id(),
        "parentId": (prior or {}).get("parentId") if prior else _last_entry_id(source_path),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "summary": plan["summary"],
        "tokensBefore": plan["tokens_before"],
        "fromHook": True,
        "details": {
            "readFiles": plan["read_files"],
            "modifiedFiles": plan["modified_files"],
            "multiverse": {
                "algorithm": "frontier-coherent-tree",
                "budget": total_budget,
                "tokensAfter": plan["tokens_after"],
                "summarizedTurns": plan["n_summarized_turns"],
                "keptTurns": plan["n_kept_turns"],
            },
        },
    }
    if plan["first_kept_entry_id"]:
        entry["firstKeptEntryId"] = plan["first_kept_entry_id"]

    rendered = json.dumps(entry, ensure_ascii=False)
    if prior_idx is None:
        lines.append(rendered)
    else:
        lines[prior_idx] = rendered
    return "".join(line + "\n" for line in lines)


def _read_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return [line for line in (raw.rstrip("\n") for raw in fh) if line.strip()]


def _find_prior_compaction(lines: list[str]) -> tuple[Optional[int], Optional[dict]]:
    """Locate a compaction entry this module wrote, if any.

    Matched on our marker in ``details`` rather than on ``type`` alone: pi's
    own compaction entries, and any written by another extension, are not ours
    to overwrite.
    """
    for i in range(len(lines) - 1, -1, -1):
        try:
            row = json.loads(lines[i])
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("type") != "compaction":
            continue
        details = row.get("details")
        if isinstance(details, dict) and isinstance(details.get("multiverse"), dict):
            return i, row
    return None, None


def _last_entry_id(path: str) -> Optional[str]:
    last: Optional[str] = None
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("type") != "session":
                eid = row.get("id")
                if isinstance(eid, str):
                    last = eid
    return last
