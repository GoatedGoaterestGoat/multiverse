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
