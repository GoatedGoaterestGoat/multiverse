"""Session JSONL reading, writing, and discovery."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .paths import claude_projects_dir
from .types import ConversationIndex, JsonlEntry, estimate_tokens


def encode_cwd(cwd: str) -> str:
    """Encode a working directory path to Claude Code's project directory name.

    Replaces non-alphanumeric chars with '-'.
    e.g. /Users/admin/Documents -> -Users-admin-Documents
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def get_projects_dir() -> Path:
    """Return the Claude Code projects directory."""
    return claude_projects_dir()


def find_session_path(session_id: str, cwd: str) -> Path:
    """Resolve a session ID + cwd to the JSONL file path."""
    encoded = encode_cwd(cwd)
    path = get_projects_dir() / encoded / f"{session_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {path}")
    return path


def parse_jsonl(path: Path) -> list[JsonlEntry]:
    """Parse a JSONL file into a list of JsonlEntry objects."""
    entries = []
    with open(path, "r") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                entries.append(JsonlEntry(line_index=i, raw=raw))
            except json.JSONDecodeError:
                continue
    return entries


def write_jsonl(path: Path, entries: list[JsonlEntry]) -> None:
    """Write entries to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry.raw, ensure_ascii=False) + "\n")


def get_conversation_messages(entries: list[JsonlEntry]) -> list[ConversationIndex]:
    """Filter entries to conversation messages only and assign indices.

    Skips: file-history-snapshot, system, progress, queue-operation, sidechains.
    """
    indexed = []
    idx = 0
    for entry in entries:
        if entry.is_sidechain:
            continue
        if entry.is_conversation_message:
            indexed.append(ConversationIndex(index=idx, entry=entry))
            idx += 1
    return indexed


def format_context_info(session_id: str, entries: list[JsonlEntry]) -> str:
    """Build the table of contents string for context_info."""
    conv = get_conversation_messages(entries)

    total_chars = sum(ci.entry.char_count() for ci in conv)
    total_tokens = estimate_tokens(total_chars)

    lines = [f"Session: {session_id[:8]} | {len(conv)} messages | ~{total_tokens} tokens", ""]

    # Find turn boundaries (system entries with subtype turn_duration)
    turn_boundary_uuids = set()
    for entry in entries:
        if entry.type == "system" and entry.raw.get("subtype") == "turn_duration":
            turn_boundary_uuids.add(entry.uuid)

    # Track which parent_uuids correspond to turn boundaries
    turn_boundary_after: set[str] = set()
    for entry in entries:
        if entry.type == "system" and entry.raw.get("subtype") == "turn_duration":
            turn_boundary_after.add(entry.uuid)

    prev_parent = None
    for ci in conv:
        # Insert turn boundary marker if this message's parent is a turn_duration system entry
        if ci.entry.parent_uuid in turn_boundary_after:
            lines.append(f"[--] --- turn boundary ---")
        lines.append(ci.format_line())

    return "\n".join(lines)
