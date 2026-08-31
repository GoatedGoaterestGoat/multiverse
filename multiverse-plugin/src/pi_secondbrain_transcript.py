"""Incremental, tree-aware pi transcript adapter for SecondBrain.

Pi keeps its native append-only JSONL, including tree parent links and native
compaction entries.  SecondBrain markers follow the current branch by entry id;
if the user branches away from a marked entry, the adapter walks its parents to
the divergence point and emits the new suffix without rewriting pi's history.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

_ARG_TRUNC = 240


def _load_active_branch(path: Path) -> tuple[str | None, list[dict], dict[str, str | None]]:
    session_id: str | None = None
    entries: dict[str, dict] = {}
    order: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if row.get("type") == "session":
                    session_id = str(row.get("id") or "") or session_id
                    continue
                entry_id = row.get("id")
                if not isinstance(entry_id, str) or not entry_id:
                    continue
                entries[entry_id] = row
                order.append(entry_id)
    except OSError:
        return session_id, [], {}

    parent_of = {
        entry_id: (
            row.get("parentId") if isinstance(row.get("parentId"), str) else None
        )
        for entry_id, row in entries.items()
    }
    if not order:
        return session_id, [], parent_of

    active_ids: list[str] = []
    current: str | None = order[-1]
    seen: set[str] = set()
    while current and current not in seen:
        row = entries.get(current)
        if row is None:
            break
        seen.add(current)
        active_ids.append(current)
        current = parent_of.get(current)
    active_ids.reverse()
    return session_id, [entries[entry_id] for entry_id in active_ids], parent_of


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _short_args(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("command", "path", "query", "pattern", "prompt", "target_name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                text = candidate
                break
        else:
            text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= _ARG_TRUNC else text[: _ARG_TRUNC - 1] + "…"


def _fmt_ts(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        value = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


def _render_entry(row: dict) -> list[str]:
    typ = row.get("type")
    if typ == "custom_message":
        text = _content_text(row.get("content"))
        return [f"[context] {text}"] if text else []
    if typ != "message":
        return []

    message = row.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role")
    content = message.get("content")
    if role == "user":
        text = _content_text(content)
        return [f"[user] {text}"] if text else []
    if role == "assistant":
        lines: list[str] = []
        text = _content_text(content)
        if text:
            lines.append(f"[assistant] {text}")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                name = str(block.get("name") or "?")
                lines.append(f"[tool:{name}] {_short_args(block.get('arguments') or {})}".rstrip())
        return lines
    if role == "toolResult":
        if message.get("isError"):
            name = str(message.get("toolName") or "?")
            return [f"[tool-result] {name} (error)"]
        return []
    if role == "bashExecution":
        return [f"[tool:bash] {_short_args(message.get('command') or '')}".rstrip()]
    if role in ("custom", "branchSummary", "compactionSummary"):
        text = _content_text(content) or str(message.get("summary") or "").strip()
        return [f"[context] {text}"] if text else []
    return []


def _render(entries: list[dict]) -> str:
    blocks: list[str] = []
    current: list[str] = []
    current_ts = ""
    for row in entries:
        lines = _render_entry(row)
        if not lines:
            continue
        starts_turn = any(line.startswith("[user] ") for line in lines)
        if starts_turn and current:
            blocks.append(
                "\n".join([f"[turn {current_ts}]" if current_ts else "[turn]", *current])
            )
            current = []
        if not current:
            current_ts = _fmt_ts(row.get("timestamp"))
        current.extend(lines)
    if current:
        blocks.append(
            "\n".join([f"[turn {current_ts}]" if current_ts else "[turn]", *current])
        )
    return "\n\n".join(blocks)


def initialize_cursor(path: Path, session_id: str) -> dict | None:
    recorded_id, branch, _ = _load_active_branch(path)
    if recorded_id and recorded_id != session_id:
        return None
    return {
        "runtime": "pi",
        "session_id": session_id,
        "entry_id": branch[-1].get("id") if branch else None,
    }


def peek_partial_transcript(
    path: Path, session_id: str, marker: dict | None
) -> tuple[str, dict] | None:
    recorded_id, branch, parent_of = _load_active_branch(path)
    if recorded_id and recorded_id != session_id:
        return None

    index_by_id = {
        row["id"]: index
        for index, row in enumerate(branch)
        if isinstance(row.get("id"), str)
    }
    after = -1
    marker_id = marker.get("entry_id") if marker else None
    if marker and marker.get("runtime") == "pi" and marker.get("session_id") == session_id:
        current = marker_id if isinstance(marker_id, str) else None
        seen: set[str] = set()
        while current and current not in seen:
            if current in index_by_id:
                after = index_by_id[current]
                break
            seen.add(current)
            current = parent_of.get(current)

    cursor = {
        "runtime": "pi",
        "session_id": session_id,
        "entry_id": branch[-1].get("id") if branch else marker_id,
    }
    return _render(branch[after + 1 :]), cursor
