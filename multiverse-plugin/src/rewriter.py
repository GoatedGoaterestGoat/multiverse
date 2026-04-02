"""Core JSONL rewriting logic for context folding."""

from __future__ import annotations

import copy
import uuid as uuid_mod
from datetime import datetime, timezone

from .session import get_conversation_messages, write_jsonl, find_session_path, parse_jsonl, get_projects_dir, encode_cwd
from .types import JsonlEntry, estimate_tokens


def new_uuid() -> str:
    return str(uuid_mod.uuid4())


def new_session_id() -> str:
    return str(uuid_mod.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def fold_context(
    session_id: str,
    cwd: str,
    start: int,
    end: int,
    summary: str,
) -> dict:
    """Perform context folding on a session JSONL.

    Returns dict with: new_session_id, removed_count, tokens_before, tokens_after, new_session_path
    """
    session_path = find_session_path(session_id, cwd)
    entries = parse_jsonl(session_path)
    conv = get_conversation_messages(entries)

    # Validate range
    if start < 1:
        raise ValueError("Cannot fold index 0 (first user message). Use start >= 1.")
    if start > end:
        raise ValueError(f"Invalid range: start ({start}) > end ({end})")
    if end >= len(conv):
        raise ValueError(f"End index ({end}) out of range (max {len(conv) - 1})")

    # Step 1: Map conversation indices to JSONL line indices
    target_line_indices = set()
    for ci in conv:
        if start <= ci.index <= end:
            target_line_indices.add(ci.entry.line_index)

    # Step 2: Expand targets to valid boundaries
    target_line_indices = _expand_targets(entries, target_line_indices)

    # Step 3: Identify fold operation's own traces to remove
    fold_trace_indices = _find_fold_traces(entries, conv)

    # Combine all indices to remove
    all_remove = target_line_indices | fold_trace_indices

    # Calculate stats before removal
    chars_before = sum(
        entries[i].char_count() for i in all_remove if entries[i].is_conversation_message
    )
    tokens_before = estimate_tokens(chars_before)

    # Step 4: Build the replacement summary entry
    # Find the entry just before the fold region
    sorted_targets = sorted(target_line_indices)
    first_target = sorted_targets[0]

    # Find predecessor: walk backward from first_target to find last non-removed entry
    predecessor_uuid = None
    for i in range(first_target - 1, -1, -1):
        if i not in all_remove and entries[i].uuid:
            predecessor_uuid = entries[i].uuid
            break

    # Determine if we need an [Acknowledged] assistant message for role alternation
    predecessor_role = None
    for i in range(first_target - 1, -1, -1):
        if i not in all_remove and entries[i].is_conversation_message:
            predecessor_role = entries[i].role
            break

    # Build new entries to insert at the fold point
    insert_entries = []
    ns_id = new_session_id()

    # If predecessor is user role, we need a synthetic assistant message first
    if predecessor_role == "user":
        ack_uuid = new_uuid()
        ack_entry = JsonlEntry(
            line_index=-1,
            raw={
                "parentUuid": predecessor_uuid,
                "isSidechain": False,
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "[Acknowledged]"}],
                    "model": "context-fold",
                    "id": f"synthetic-{ack_uuid[:8]}",
                    "type": "message",
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
                "uuid": ack_uuid,
                "timestamp": now_iso(),
                "sessionId": ns_id,
            },
        )
        insert_entries.append(ack_entry)
        predecessor_uuid = ack_uuid

    # The summary message (always user role)
    summary_uuid = new_uuid()
    summary_entry = JsonlEntry(
        line_index=-1,
        raw={
            "parentUuid": predecessor_uuid,
            "isSidechain": False,
            "type": "user",
            "message": {
                "role": "user",
                "content": f"[Context Fold Summary]\n\n{summary}",
            },
            "uuid": summary_uuid,
            "timestamp": now_iso(),
            "sessionId": ns_id,
        },
    )
    insert_entries.append(summary_entry)

    # Step 5: Build new entry list
    new_entries = []
    inserted = False

    for i, entry in enumerate(entries):
        if i in all_remove:
            # Insert replacement at the position of the first removed entry
            if not inserted and i in target_line_indices:
                new_entries.extend(insert_entries)
                inserted = True
            continue

        # Skip metadata entries (file-history-snapshot, progress, etc.) that fall in fold region
        if not entry.is_conversation_message and i > 0:
            # Check if this metadata entry is between removed entries
            if _is_between_removed(i, all_remove, len(entries)):
                continue

        new_entries.append(entry)

    # If we haven't inserted yet (all targets were at the end), insert now
    if not inserted:
        new_entries.extend(insert_entries)

    # Step 6: Re-link parentUuid chain
    _relink_parent_uuids(new_entries, all_remove, entries, summary_uuid)

    # Step 7: Append synthetic context_fold tool call + result
    last_uuid = new_entries[-1].uuid if new_entries else summary_uuid

    fold_tool_use_id = f"toolu_fold_{new_uuid()[:12]}"
    fold_tool_uuid = new_uuid()
    fold_assistant = JsonlEntry(
        line_index=-1,
        raw={
            "parentUuid": last_uuid,
            "isSidechain": False,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": fold_tool_use_id,
                        "name": "context_fold",
                        "input": {
                            "summary": summary,
                            "start": start,
                            "end": end,
                            "session_id": session_id,
                            "cwd": cwd,
                        },
                    }
                ],
                "model": "context-fold",
                "id": f"synthetic-{fold_tool_uuid[:8]}",
                "type": "message",
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
            "uuid": fold_tool_uuid,
            "timestamp": now_iso(),
            "sessionId": ns_id,
        },
    )

    removed_count = sum(1 for i in all_remove if entries[i].is_conversation_message)
    tokens_after = estimate_tokens(len(summary))
    result_text = (
        f"Context folded. Removed {removed_count} messages "
        f"(~{tokens_before} tokens -> ~{tokens_after} tokens). "
        f"New session: {ns_id}."
    )

    fold_result_uuid = new_uuid()
    fold_result = JsonlEntry(
        line_index=-1,
        raw={
            "parentUuid": fold_tool_uuid,
            "isSidechain": False,
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": fold_tool_use_id,
                        "content": result_text,
                        "is_error": False,
                    }
                ],
            },
            "uuid": fold_result_uuid,
            "timestamp": now_iso(),
            "sessionId": ns_id,
        },
    )

    new_entries.append(fold_assistant)
    new_entries.append(fold_result)

    # Step 8: Update all sessionIds
    for entry in new_entries:
        entry.raw["sessionId"] = ns_id

    # Write new session
    encoded = encode_cwd(cwd)
    new_path = get_projects_dir() / encoded / f"{ns_id}.jsonl"
    write_jsonl(new_path, new_entries)

    return {
        "new_session_id": ns_id,
        "removed_count": removed_count,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "new_session_path": str(new_path),
    }


def _expand_targets(
    entries: list[JsonlEntry], target_indices: set[int]
) -> set[int]:
    """Expand fold targets to include paired tool calls/results and request groups."""
    expanded = set(target_indices)

    # Build tool_use_id -> line_index maps
    tool_use_to_line: dict[str, int] = {}
    tool_result_to_line: dict[str, int] = {}

    for entry in entries:
        for tid in entry.tool_use_ids:
            if entry.role == "assistant":
                tool_use_to_line[tid] = entry.line_index
            elif entry.role == "user":
                tool_result_to_line[tid] = entry.line_index

    # Expand: if tool_use is targeted, include its tool_result, and vice versa
    changed = True
    while changed:
        changed = False
        new_adds = set()
        for idx in list(expanded):
            entry = entries[idx]
            for tid in entry.tool_use_ids:
                # If this is a tool_use, pull in the result
                if tid in tool_use_to_line and tool_use_to_line[tid] == idx:
                    result_idx = tool_result_to_line.get(tid)
                    if result_idx is not None and result_idx not in expanded:
                        new_adds.add(result_idx)
                # If this is a tool_result, pull in the use
                if tid in tool_result_to_line and tool_result_to_line[tid] == idx:
                    use_idx = tool_use_to_line.get(tid)
                    if use_idx is not None and use_idx not in expanded:
                        new_adds.add(use_idx)
        if new_adds:
            expanded |= new_adds
            changed = True

    # Expand by requestId: if any entry in a requestId group is targeted, include all
    request_groups: dict[str, list[int]] = {}
    for entry in entries:
        rid = entry.request_id
        if rid:
            request_groups.setdefault(rid, []).append(entry.line_index)

    changed = True
    while changed:
        changed = False
        for rid, group_indices in request_groups.items():
            if any(i in expanded for i in group_indices):
                for i in group_indices:
                    if i not in expanded:
                        expanded.add(i)
                        changed = True

    return expanded


def _find_fold_traces(
    entries: list[JsonlEntry], conv: list
) -> set[int]:
    """Find the context_info call and intermediate reasoning to remove.

    These are in the current turn (after last turn_duration) and before the end.
    We look for:
    - assistant tool_use entries calling "context_info"
    - their matching tool_result entries
    - thinking blocks in the same turn
    - text blocks that are part of fold reasoning
    """
    # Find the last turn boundary
    last_turn_idx = -1
    for i, entry in enumerate(entries):
        if entry.type == "system" and entry.raw.get("subtype") == "turn_duration":
            last_turn_idx = i

    if last_turn_idx == -1:
        # No turn boundaries yet — current turn is the whole session
        # Only remove context_info traces, not the user's messages
        last_turn_idx = 0

    traces = set()

    # Find context_info tool calls in the current turn
    context_info_tool_ids: set[str] = set()
    for i in range(last_turn_idx, len(entries)):
        entry = entries[i]
        if not entry.is_conversation_message:
            continue
        if entry.role == "assistant" and entry.is_tool_use:
            if "context_info" in entry.tool_names:
                traces.add(i)
                context_info_tool_ids.update(entry.tool_use_ids)
                # Also remove thinking blocks with same requestId
                if entry.request_id:
                    for j in range(last_turn_idx, len(entries)):
                        if (
                            entries[j].request_id == entry.request_id
                            and entries[j].line_index != entry.line_index
                            and "thinking" in entries[j].content_types
                        ):
                            traces.add(j)

    # Find matching tool_results for context_info
    for i in range(last_turn_idx, len(entries)):
        entry = entries[i]
        if entry.role == "user" and entry.is_tool_result:
            for tid in entry.tool_use_ids:
                if tid in context_info_tool_ids:
                    traces.add(i)

    return traces


def _is_between_removed(idx: int, removed: set[int], total: int) -> bool:
    """Check if a metadata entry at idx falls between removed conversation entries."""
    has_before = any(r < idx for r in removed)
    has_after = any(r > idx for r in removed)
    return has_before and has_after


def _relink_parent_uuids(
    new_entries: list[JsonlEntry],
    removed_indices: set[int],
    original_entries: list[JsonlEntry],
    summary_uuid: str,
) -> None:
    """Fix parentUuid pointers that reference removed entries."""
    # Build set of removed UUIDs
    removed_uuids = set()
    for i in removed_indices:
        uid = original_entries[i].uuid
        if uid:
            removed_uuids.add(uid)

    # Fix entries that point to removed entries
    for entry in new_entries:
        if entry.parent_uuid and entry.parent_uuid in removed_uuids:
            entry.raw["parentUuid"] = summary_uuid
