"""Synthetic Claude Code session JSONL construction for privileged-mode
constructor calls.

The privileged constructor needs the model to receive a turn's content
(thinking blocks intact, signatures preserved) as native API messages, via
subscription auth. The cleanest path is `claude --resume <id>` against a
synthetic session JSONL we wrote into `~/.claude/projects/<slug>/`.

Two builders here:

* `build_synthetic_jsonl_from_source` — for the FIRST merge step, when the
  frontier equals the original leaves. Extracts only this turn's rows from
  the source JSONL, rewires `parentUuid` to start fresh, and updates
  `sessionId` / `cwd`. Preserves the original assistant/user role
  alternation (so any tool_use blocks have their matching tool_results).

* `build_synthetic_jsonl_from_frontier` — for subsequent merge steps, when
  the frontier contains a mix of original leaves and summary Nodes. Flattens
  everything into ONE assistant message whose content blocks come from
  `render_frontier_as_message_blocks`. Surviving thinking leaves emit
  native thinking blocks (verbatim). Other leaves and Node summaries emit
  text blocks, with `<compacted-context entries="N">...</compacted-context>`
  markers around summary text so the consumer can distinguish summarized
  regions from normal assistant output.

Both write to a path under `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`
that `claude --resume` will load.
"""

from __future__ import annotations

import json
import os
import uuid as uuidlib
from pathlib import Path
from typing import Iterable

from .render import render_frontier_as_message_blocks
from .types import Frontier, Leaf, Turn


# Match the model that produced the original session, so signatures validate
# even if we set --model differently on the constructor call (the assistant
# role's model field on each row is independent of the merging model).
SYNTHETIC_MODEL = "claude-opus-4-7"
SYNTHETIC_TIMESTAMP = "2026-04-30T00:00:00.000Z"


def cwd_slug(cwd: str | None = None) -> str:
    """Claude Code's project-slug rule: replace `/` with `-`."""
    return (cwd or os.getcwd()).replace("/", "-")


def projects_dir(cwd: str | None = None) -> Path:
    return Path.home() / ".claude" / "projects" / cwd_slug(cwd)


def session_path(session_id: str, cwd: str | None = None) -> Path:
    d = projects_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id}.jsonl"


def _metadata_row(session_id: str, title: str = "priv_compaction") -> dict:
    return {
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }


_UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _looks_like_uuid(s: str) -> bool:
    return bool(s) and bool(_UUID_RE.match(s))


def build_synthetic_jsonl_from_source(
    source_path: str,
    turn: Turn,
    session_id: str,
    cwd: str,
) -> str:
    """Extract this turn's rows verbatim from the source JSONL, rewiring
    sessionId/cwd/parentUuid for use as a fresh resumable session.

    Use this for the FIRST merge step (frontier = original leaves), since
    the original session structure (alternating user/assistant roles with
    matching tool_use/tool_result pairs) is preserved exactly.
    """
    target_uuids: set[str] = set()
    if turn.user_prompt:
        target_uuids.update(turn.user_prompt.source_uuids)
    for leaf in turn.leaves:
        target_uuids.update(leaf.source_uuids)

    rows: list[dict] = []
    with open(source_path) as f:
        for line in f:
            d = json.loads(line)
            if d.get("uuid") in target_uuids:
                rows.append(d)

    prev_uuid: str | None = None
    for d in rows:
        d["sessionId"] = session_id
        d["cwd"] = cwd
        d["parentUuid"] = prev_uuid
        prev_uuid = d["uuid"]

    out = [_metadata_row(session_id)]
    out.extend(rows)
    return "\n".join(json.dumps(r) for r in out) + "\n"


def build_synthetic_jsonl_from_frontier(
    user_prompt_text: str,
    frontier: Frontier,
    session_id: str,
    cwd: str,
) -> str:
    """Build a session JSONL where the assistant message contains one
    content block per frontier entry. Use this for merge steps AFTER the
    first, when the frontier mixes original leaves with summary Nodes.

    Structure:
      - metadata row (custom-title)
      - one user-role row containing `user_prompt_text`
      - one assistant-role row PER content block, all sharing the same
        synthetic message.id, parentUuid chained from the previous row

    `render_frontier_as_message_blocks(frontier)` provides the content
    blocks: thinking leaves verbatim with signatures; other leaves as
    descriptive text; summary nodes as text wrapped in <compacted-context>.
    """
    user_uuid = str(uuidlib.uuid4())
    msg_id = f"msg_priv{uuidlib.uuid4().hex[:24]}"

    rows = [_metadata_row(session_id)]
    rows.append({
        "parentUuid": None,
        "uuid": user_uuid,
        "sessionId": session_id,
        "type": "user",
        "timestamp": SYNTHETIC_TIMESTAMP,
        "cwd": cwd,
        "userType": "external",
        "message": {"role": "user", "content": user_prompt_text},
    })

    blocks = render_frontier_as_message_blocks(frontier)
    prev_uuid = user_uuid
    for i, block in enumerate(blocks):
        row_uuid = str(uuidlib.uuid4())
        rows.append({
            "parentUuid": prev_uuid,
            "uuid": row_uuid,
            "sessionId": session_id,
            "type": "assistant",
            "timestamp": SYNTHETIC_TIMESTAMP,
            "cwd": cwd,
            "userType": "external",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": SYNTHETIC_MODEL,
                "content": [block],
                "stop_reason": "end_turn" if i == len(blocks) - 1 else None,
                "stop_sequence": None,
                "usage": {},
            },
        })
        prev_uuid = row_uuid

    return "\n".join(json.dumps(r) for r in rows) + "\n"


def write_session(jsonl_text: str, session_id: str, cwd: str | None = None) -> Path:
    """Persist a synthetic JSONL to ~/.claude/projects/<slug>/<id>.jsonl
    and return the path."""
    p = session_path(session_id, cwd)
    p.write_text(jsonl_text)
    return p


# ---------------------------------------------------------------------------
# Multi-turn compacted session rendering
# ---------------------------------------------------------------------------
#
# Once every turn has its tree built, the entire forest can be rendered as
# one synthetic Claude Code session JSONL — the live agent can `--resume`
# it and continue from a fully-compacted state. Per-turn output is produced
# independently (so it can be cached/inspected per turn at a chosen
# compaction level); the multi-turn assembler stitches them together by
# chaining parentUuid and sharing one sessionId.


def _user_row(
    text: str,
    session_id: str,
    cwd: str,
    parent_uuid: str | None,
    prompt_id: str | None = None,
    timestamp: str = SYNTHETIC_TIMESTAMP,
) -> dict:
    """Build a user-role row. `prompt_id`, when set, marks this as the
    start of a new turn — our parser uses promptId changes to detect turn
    boundaries, so user-prompt rows in multi-turn synthetic sessions need
    one for the JSONL to round-trip cleanly."""
    row = {
        "parentUuid": parent_uuid,
        "uuid": str(uuidlib.uuid4()),
        "sessionId": session_id,
        "type": "user",
        "timestamp": timestamp,
        "cwd": cwd,
        "userType": "external",
        "message": {"role": "user", "content": text},
    }
    if prompt_id is not None:
        row["promptId"] = prompt_id
    return row


def _assistant_block_row(
    block: dict,
    msg_id: str,
    session_id: str,
    cwd: str,
    parent_uuid: str,
    is_final: bool,
    timestamp: str = SYNTHETIC_TIMESTAMP,
) -> dict:
    return {
        "parentUuid": parent_uuid,
        "uuid": str(uuidlib.uuid4()),
        "sessionId": session_id,
        "type": "assistant",
        "timestamp": timestamp,
        "cwd": cwd,
        "userType": "external",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": SYNTHETIC_MODEL,
            "content": [block],
            "stop_reason": "end_turn" if is_final else None,
            "stop_sequence": None,
            "usage": {},
        },
    }


def turn_to_rows(
    turn: Turn,
    frontier: Frontier,
    session_id: str,
    cwd: str,
    parent_uuid: str | None,
) -> tuple[list[dict], str | None]:
    """Render one turn's compacted output as session JSONL rows.

    Emits one user-role row containing the turn's user_prompt followed by
    one assistant-role row per content block from
    `render_frontier_as_message_blocks(frontier)` — thinking leaves stay as
    native blocks (with signatures), other leaves and Node summaries become
    text blocks (Node summaries wrapped in `<compacted-context>` markers).

    Returns (rows, last_uuid). `last_uuid` is the parentUuid the next
    turn's first row should chain to.
    """
    rows: list[dict] = []

    user_text = ""
    if turn.user_prompt is not None:
        user_text = turn.user_prompt.content.get("text", "") or ""
    if not user_text:
        user_text = "<no user prompt for this turn>"

    # Reuse the original turn_id as the promptId when it's already a UUID
    # (it usually is — turn_id originates from the source JSONL's promptId);
    # fall back to a fresh UUID otherwise (orphan turns, etc.).
    prompt_id = turn.turn_id if _looks_like_uuid(turn.turn_id) else str(uuidlib.uuid4())
    user_row = _user_row(user_text, session_id, cwd, parent_uuid, prompt_id=prompt_id)
    rows.append(user_row)
    prev_uuid: str = user_row["uuid"]

    blocks = render_frontier_as_message_blocks(frontier) if frontier else []
    if not blocks:
        # Empty frontier — emit a single placeholder text block so the
        # alternation holds and the message isn't empty.
        blocks = [{"type": "text", "text": "<no compacted output for this turn>"}]

    msg_id = f"msg_priv{uuidlib.uuid4().hex[:24]}"
    for i, block in enumerate(blocks):
        row = _assistant_block_row(
            block, msg_id, session_id, cwd, prev_uuid,
            is_final=(i == len(blocks) - 1),
        )
        rows.append(row)
        prev_uuid = row["uuid"]

    return rows, prev_uuid


def _expand_turn_frontier(turn: Turn, budget: int) -> Frontier:
    """Expand a turn to its allocated budget. Uses the constructed tree if
    present; falls back to greedy-leaves-from-start if not."""
    # Lazy import to avoid the circular dependency (compactor depends on
    # types only; priv_session is a higher-level layer).
    from .compactor import expand_to_budget

    if turn.root is not None:
        return expand_to_budget(turn.root, budget)
    # No tree built — take leaves greedily from the start until budget
    # would be exceeded.
    frontier: Frontier = []
    tokens_used = 0
    for leaf in turn.leaves:
        if tokens_used + leaf.tokens > budget and frontier:
            break
        frontier.append(leaf)
        tokens_used += leaf.tokens
    return frontier


def compact_forest_to_session_jsonl(
    forest,
    per_turn_budget: dict[str, int],
    session_id: str | None = None,
    cwd: str | None = None,
    title: str = "compacted_forest",
) -> str:
    """Render a compacted forest as one synthetic Claude Code session JSONL.

    For each turn, expand its tree to the allocated per-turn budget (or
    greedy leaves if the tree wasn't built), emit user_prompt + compacted
    assistant rows, chain parentUuid continuously across turns. All rows
    share one sessionId. Result is loadable via `claude --resume`.

    `per_turn_budget` is the output of one of the schedulers in
    `compactor.py` (e.g. `recency_decay`, `last_k_raw`, `proportional`).
    """
    session_id = session_id or str(uuidlib.uuid4())
    cwd = cwd or os.getcwd()

    out_rows: list[dict] = [_metadata_row(session_id, title)]
    parent_uuid: str | None = None

    for turn in forest:
        budget = per_turn_budget.get(turn.turn_id, 0)
        frontier = _expand_turn_frontier(turn, budget)
        rows, parent_uuid = turn_to_rows(
            turn, frontier, session_id, cwd, parent_uuid
        )
        out_rows.extend(rows)

    return "\n".join(json.dumps(r) for r in out_rows) + "\n"
