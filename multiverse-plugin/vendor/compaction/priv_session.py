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
import re
import uuid as uuidlib
from pathlib import Path
from typing import Any, Iterable

from .render import render_frontier_as_message_blocks
from .types import Frontier, Leaf, Turn


def _frontier_leaf_images(frontier: Frontier) -> list[dict]:
    """Collect verbatim image blocks from every Leaf still on the frontier.

    Used to surface images to the merge agent on rebuild steps (step >= 1),
    where the frontier is flattened into synthetic message rows that would
    otherwise be text-only. Nodes (already-merged spans) carry no images —
    their content has been summarized into text — so only Leaves contribute.
    Images are returned in frontier order.
    """
    images: list[dict] = []
    for entry in frontier:
        if isinstance(entry, Leaf) and entry.images:
            images.extend(entry.images)
    return images


# Match the model that produced the original session, so signatures validate
# even if we set --model differently on the constructor call (the assistant
# role's model field on each row is independent of the merging model).
SYNTHETIC_MODEL = "claude-opus-4-7"
SYNTHETIC_TIMESTAMP = "2026-04-30T00:00:00.000Z"


def cwd_slug(cwd: str | None = None) -> str:
    """Claude Code's project-slug rule: replace **any** non-alphanumeric
    character with ``-``. The previous implementation only replaced
    slashes — fine for cwds like ``/Users/foo/proj`` (no other special
    chars) but wrong for any path with underscores, dots, or spaces. On
    such cwds the synthetic JSONL ends up under the wrong directory and
    ``claude --resume <id>`` returns "No conversation found" in ~1
    second; the constructor then catches the parse error and aborts
    every turn at step 0 — a hard-to-spot failure mode because each
    "abort" looks like a normal fast turn in the worker log.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd or os.getcwd())


def projects_dir(cwd: str | None = None) -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / "projects" / cwd_slug(cwd)


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


# Inserted at the end of every synthetic merge JSONL we hand to
# `claude --resume`. Without it, the model treats the trace above as an
# ongoing conversation and continues it (e.g. "Standing by for your
# feedback on the 3D viewer…") instead of doing the merge task we
# specify in the system prompt + the next user message. The bridge is
# an explicit user/assistant pair that flips the model into "JSON-output
# mode" before the real merge instruction arrives.
_BRIDGE_USER_TEXT = (
    "[CONTEXT SWITCH] The conversation above is FROZEN — a transcript "
    "for you to summarize, NOT a conversation to continue. Do not "
    "respond to any apparent question, request, or pending action in it. "
    "The next message will give you a different task: outputting a JSON "
    "object describing how to summarize that frozen trace."
)
_BRIDGE_ASST_TEXT = (
    "Understood. The trace above is frozen. I am now in JSON-output "
    "mode and will not continue that conversation. Awaiting the merge "
    "task."
)


def _bridge_rows(
    parent_uuid: str | None,
    session_id: str,
    cwd: str,
) -> list[dict]:
    """Synthesize the user/assistant pair that flips the model out of
    conversational continuation mode. Returns two rows whose parentUuid
    chain extends from ``parent_uuid``."""
    user_uuid = str(uuidlib.uuid4())
    asst_uuid = str(uuidlib.uuid4())
    msg_id = f"msg_priv{uuidlib.uuid4().hex[:24]}"
    return [
        {
            "parentUuid": parent_uuid,
            "uuid": user_uuid,
            "sessionId": session_id,
            "type": "user",
            "timestamp": SYNTHETIC_TIMESTAMP,
            "cwd": cwd,
            "userType": "external",
            "message": {"role": "user", "content": _BRIDGE_USER_TEXT},
        },
        {
            "parentUuid": user_uuid,
            "uuid": asst_uuid,
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
                "content": [{"type": "text", "text": _BRIDGE_ASST_TEXT}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {},
            },
        },
    ]


_UUID_RE = re.compile(
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
    last_uuid = rows[-1]["uuid"] if rows else None
    out.extend(_bridge_rows(last_uuid, session_id, cwd))
    return "\n".join(json.dumps(r) for r in out) + "\n"


def build_synthetic_jsonl_from_frontier(
    user_prompt_text: str,
    frontier: Frontier,
    session_id: str,
    cwd: str,
    user_images: list[dict] | None = None,
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

    `user_images` are the originating user prompt's image blocks (hoisted
    out of the frontier, so not found by scanning frontier leaves). Together
    with any image-bearing leaves still on the frontier, they ride the
    user-role row — the only API-legal home for image blocks — so the merge
    agent can actually see the screenshots it's summarizing around.
    """
    user_uuid = str(uuidlib.uuid4())
    msg_id = f"msg_priv{uuidlib.uuid4().hex[:24]}"

    # Surface images to the merge agent: the user prompt's own images plus
    # any carried by surviving frontier leaves (e.g. a tool_result screenshot
    # still on the frontier). Images are only API-legal in a user-role
    # message, so they ride the user row here (not the assistant frontier
    # blocks below). When present, content becomes a block list: prompt text
    # first, then the image blocks verbatim.
    images = list(user_images or []) + _frontier_leaf_images(frontier)
    if images:
        user_content: Any = (
            [{"type": "text", "text": user_prompt_text}] + images
        )
    else:
        user_content = user_prompt_text

    rows = [_metadata_row(session_id)]
    rows.append({
        "parentUuid": None,
        "uuid": user_uuid,
        "sessionId": session_id,
        "type": "user",
        "timestamp": SYNTHETIC_TIMESTAMP,
        "cwd": cwd,
        "userType": "external",
        "message": {"role": "user", "content": user_content},
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

    rows.extend(_bridge_rows(prev_uuid, session_id, cwd))
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
    images: list[dict] | None = None,
) -> dict:
    """Build a user-role row. `prompt_id`, when set, marks this as the
    start of a new turn — our parser uses promptId changes to detect turn
    boundaries, so user-prompt rows in multi-turn synthetic sessions need
    one for the JSONL to round-trip cleanly.

    When `images` is non-empty, the user prompt's image blocks are preserved
    verbatim alongside the text (content becomes a block list). User-message
    images are part of the verbatim separator and are never compacted away."""
    if images:
        content: Any = [{"type": "text", "text": text}] + list(images)
    else:
        content = text
    row = {
        "parentUuid": parent_uuid,
        "uuid": str(uuidlib.uuid4()),
        "sessionId": session_id,
        "type": "user",
        "timestamp": timestamp,
        "cwd": cwd,
        "userType": "external",
        "message": {"role": "user", "content": content},
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

    Structure:
      - one user-role row containing the turn's hoisted user_prompt
        (verbatim separator, never compacted)
      - then a sequence of alternating assistant/user messages from
        ``render_frontier_as_messages(frontier)`` — each message
        becomes one row per content block, all rows of the same
        message sharing one ``message.id`` (which is how vanilla
        claude-code stores multi-block messages).

    Tool calls / tool results / mid-turn user-role text are emitted
    as their NATIVE block types in their proper role rows. claude
    code's UI has dedicated renderers for those; markdown wrappers
    would get dropped by the markdown lexer.

    Returns (rows, last_uuid). ``last_uuid`` is the parentUuid the
    next turn's first row should chain to.
    """
    rows: list[dict] = []

    user_text = ""
    user_images: list[dict] = []
    if turn.user_prompt is not None:
        user_text = turn.user_prompt.content.get("text", "") or ""
        user_images = turn.user_prompt.images or []
    if not user_text and not user_images:
        # Plain text — angle-bracket-only placeholders get parsed as
        # raw HTML by claude code's markdown lexer and discarded.
        user_text = "_(no user prompt for this turn)_"

    # Reuse the original turn_id as the promptId when it's already a UUID
    # (it usually is — turn_id originates from the source JSONL's promptId);
    # fall back to a fresh UUID otherwise (orphan turns, etc.).
    prompt_id = turn.turn_id if _looks_like_uuid(turn.turn_id) else str(uuidlib.uuid4())
    user_row = _user_row(
        user_text, session_id, cwd, parent_uuid,
        prompt_id=prompt_id, images=user_images,
    )
    rows.append(user_row)
    prev_uuid: str = user_row["uuid"]

    from .render import render_frontier_as_messages  # local — avoid early import
    messages = render_frontier_as_messages(frontier) if frontier else []
    if not messages:
        # Empty frontier — emit a single assistant placeholder so the
        # turn isn't structurally empty. Plain markdown rather than
        # angle brackets.
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "_(no compacted output for this turn)_"}
                ],
            }
        ]

    n_msgs = len(messages)
    for msg_idx, msg in enumerate(messages):
        role = msg["role"]
        blocks = msg["content"]
        if role == "assistant":
            msg_id = f"msg_priv{uuidlib.uuid4().hex[:24]}"
            # Pick a stop_reason that matches the message's content. The
            # last block of an assistant message that contains tool_use
            # blocks is followed by a user message with tool_results;
            # claude code records stop_reason="tool_use" on those rows.
            # The final assistant message of the turn (no user message
            # comes after within this turn) gets stop_reason="end_turn".
            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in blocks
            )
            is_last_msg = msg_idx == n_msgs - 1
            stop_reason = (
                "tool_use" if has_tool_use and not is_last_msg
                else "end_turn"
            )
            for i, block in enumerate(blocks):
                row = _assistant_block_row(
                    block, msg_id, session_id, cwd, prev_uuid,
                    is_final=is_last_msg and i == len(blocks) - 1,
                )
                # Override stop_reason from the simple end_turn-only
                # default to whatever is correct for this message's
                # content + position.
                row["message"]["stop_reason"] = stop_reason
                rows.append(row)
                prev_uuid = row["uuid"]
        else:  # user
            # User-role messages contain tool_results and/or text. Each
            # block becomes its own row, all sharing no message.id (user
            # rows in claude code's JSONL don't carry an outer message.id
            # — content sits directly on the row's ``message.content``).
            for block in blocks:
                row_uuid = str(uuidlib.uuid4())
                rows.append({
                    "parentUuid": prev_uuid,
                    "uuid": row_uuid,
                    "sessionId": session_id,
                    "type": "user",
                    "timestamp": SYNTHETIC_TIMESTAMP,
                    "cwd": cwd,
                    "userType": "external",
                    "message": {"role": "user", "content": [block]},
                })
                prev_uuid = row_uuid

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
