"""Read-only Codex rollout adapter for incremental SecondBrain transcripts.

Codex rollout JSONL is an append-only event log, not Claude's ``parentUuid`` tree.
``task_started`` records open stable-ID turns and ``thread_rolled_back`` records remove
turns from the canonical history while leaving their old rows on disk.  Replaying the
log therefore gives us both the abandoned history at a delivery marker and the current
canonical history.

The public helpers in this module do not write marker state.  :mod:`sb_transcript`
owns the shared marker files and delegates Codex snapshot/peek work here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


ARG_TRUNC = 100
_WAKE_PROMPT = "Please check your inbox — a new inter-agent message just arrived."
_INBOX_RESUME_RE = re.compile(
    r"(?:\n\n)?You have \d+ pending inbox messages? from other agents in your "
    r"workspace\. Call the multiverse check_inbox tool to read and clear them, then "
    r"respond or act as appropriate\.\s*$"
)


@dataclass
class TranscriptItem:
    key: str
    kind: str
    text: str = ""
    name: str = ""
    arguments: Any = None
    error: bool = False


@dataclass
class TranscriptTurn:
    turn_id: str | None
    start_key: str
    timestamp: str | None
    items: list[TranscriptItem] = field(default_factory=list)
    status: str = "in_progress"

    @property
    def key(self) -> str:
        if self.turn_id:
            return f"turn:{self.turn_id}"
        if self.items:
            return f"pending:{self.items[0].key}"
        return f"pending:{self.start_key}"


@dataclass(frozen=True)
class TurnIdentity:
    key: str
    item_keys: tuple[str, ...]


@dataclass
class RolloutSnapshot:
    thread_id: str
    record_offset: int
    record_hash: str | None
    turns: list[TranscriptTurn]

    def identity(self) -> list[TurnIdentity]:
        return [
            TurnIdentity(turn.key, tuple(item.key for item in turn.items))
            for turn in self.turns
        ]


class _HistoryBuilder:
    def __init__(self) -> None:
        self.turns: list[TranscriptTurn] = []
        self.active: TranscriptTurn | None = None
        self.pending_users: list[TranscriptItem] = []
        self.pending_timestamp: str | None = None

    def view(self) -> list[TranscriptTurn]:
        turns = list(self.turns)
        if self.pending_users:
            turns.append(
                TranscriptTurn(
                    turn_id=None,
                    start_key=self.pending_users[0].key,
                    timestamp=self.pending_timestamp,
                    items=list(self.pending_users),
                )
            )
        return turns

    def identity(self) -> list[TurnIdentity]:
        return [
            TurnIdentity(turn.key, tuple(item.key for item in turn.items))
            for turn in self.view()
        ]

    def apply(self, record: dict, record_key: str) -> None:
        typ = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        payload_type = payload.get("type")
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, str):
            timestamp = None

        if typ == "event_msg" and payload_type == "task_started":
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                turn_id = None
            turn = TranscriptTurn(
                turn_id=turn_id,
                start_key=record_key,
                timestamp=timestamp or self.pending_timestamp,
                items=list(self.pending_users),
            )
            self.pending_users.clear()
            self.pending_timestamp = None
            self.turns.append(turn)
            self.active = turn
            return

        if typ == "event_msg" and payload_type in ("task_complete", "turn_aborted"):
            turn_id = payload.get("turn_id")
            target = self._find_turn(turn_id)
            if target is not None:
                target.status = (
                    "completed" if payload_type == "task_complete" else "aborted"
                )
            if self.active is target or not isinstance(turn_id, str):
                self.active = None
            return

        if typ == "event_msg" and payload_type == "thread_rolled_back":
            raw_count = payload.get("num_turns", 0)
            try:
                count = max(0, int(raw_count))
            except (TypeError, ValueError):
                count = 0
            if count:
                del self.turns[max(0, len(self.turns) - count) :]
            self.active = None
            self.pending_users.clear()
            self.pending_timestamp = None
            return

        if typ == "event_msg" and payload_type == "user_message":
            text = payload.get("message")
            if not isinstance(text, str):
                text = ""
            text = _clean_user_text(text)
            images = payload.get("images") or payload.get("local_images") or []
            if not text.strip() and isinstance(images, list) and images:
                text = f"<{len(images)} image(s)>"
            if not text.strip():
                return
            item = TranscriptItem(key=f"user:{record_key}", kind="user", text=text)
            if self.active is not None:
                self.active.items.append(item)
            else:
                if not self.pending_users:
                    self.pending_timestamp = timestamp
                self.pending_users.append(item)
            return

        if typ != "response_item" or self.active is None:
            return

        if payload_type == "message" and payload.get("role") == "assistant":
            text = _message_text(payload.get("content"))
            if text:
                self.active.items.append(
                    TranscriptItem(
                        key=_item_key("assistant", payload, record_key),
                        kind="assistant",
                        text=text,
                    )
                )
            return

        if payload_type in ("custom_tool_call", "function_call"):
            name = payload.get("name")
            if not isinstance(name, str) or not name:
                name = "?"
            arguments = payload.get("input")
            if arguments is None:
                arguments = payload.get("arguments")
            self.active.items.append(
                TranscriptItem(
                    key=_item_key("tool", payload, record_key),
                    kind="tool",
                    name=name,
                    arguments=arguments,
                    error=str(payload.get("status") or "").lower()
                    in ("failed", "error"),
                )
            )

    def _find_turn(self, turn_id: Any) -> TranscriptTurn | None:
        if isinstance(turn_id, str) and turn_id:
            for turn in reversed(self.turns):
                if turn.turn_id == turn_id:
                    return turn
        return self.active


def _message_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("output_text", "text"):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    return "\n".join(chunks)


def _clean_user_text(text: str) -> str:
    """Remove Multiverse-only wake context without hiding real user prompts."""
    if text.strip() == _WAKE_PROMPT:
        return ""
    return _INBOX_RESUME_RE.sub("", text).rstrip()


def _item_key(kind: str, payload: dict, record_key: str) -> str:
    value = payload.get("id")
    if isinstance(value, str) and value:
        return f"{kind}:id:{value}"
    value = payload.get("call_id")
    if isinstance(value, str) and value:
        return f"{kind}:call:{value}"
    return f"{kind}:{record_key}"


def _record_hash(raw: bytes) -> str:
    return hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()


def find_rollout_path(codex_home: Path, thread_id: str) -> Path | None:
    """Newest materialized rollout whose filename belongs to ``thread_id``."""
    if not thread_id:
        return None
    sessions_dir = Path(codex_home) / "sessions"
    if not sessions_dir.is_dir():
        return None
    suffix = f"-{thread_id}.jsonl"
    candidates: list[Path] = []
    try:
        for path in sessions_dir.rglob("rollout-*.jsonl"):
            if path.name.endswith(suffix):
                candidates.append(path)
    except OSError:
        return None
    if not candidates:
        return None
    dated: list[tuple[float, Path]] = []
    for path in candidates:
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(dated, default=(0.0, candidates[-1]), key=lambda item: item[0])[1]


def _parse_path(
    path: Path,
    thread_id: str,
    *,
    stop_offset: int | None = None,
    prefix_offset: int | None = None,
    expected_prefix_hash: str | None = None,
) -> tuple[RolloutSnapshot, list[TurnIdentity] | None]:
    """Replay complete lines from one stable file-size snapshot.

    ``stop_offset`` reads only a historical prefix. ``prefix_offset`` captures the
    canonical identity at that boundary while continuing through the current file,
    avoiding a second full-file pass for the common same-thread case.
    """
    builder = _HistoryBuilder()
    marker_identity: list[TurnIdentity] | None = [] if prefix_offset == 0 else None
    last_offset = 0
    last_hash: str | None = None
    try:
        with path.open("rb") as fh:
            snapshot_size = os.fstat(fh.fileno()).st_size
            limit = snapshot_size
            if stop_offset is not None:
                limit = min(limit, max(0, stop_offset))
            while fh.tell() < limit:
                remaining = limit - fh.tell()
                raw = fh.readline(remaining)
                if not raw:
                    break
                # A writer may be in the middle of its final JSONL record. It is not
                # part of this snapshot until the terminating newline is present.
                if not raw.endswith(b"\n"):
                    break
                end_offset = fh.tell()
                digest = _record_hash(raw)
                last_offset = end_offset
                last_hash = digest
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    record = None
                if isinstance(record, dict):
                    builder.apply(record, digest)
                if prefix_offset is not None and end_offset == prefix_offset:
                    if expected_prefix_hash is None or expected_prefix_hash == digest:
                        marker_identity = builder.identity()
            # A marker beyond the captured file or between record boundaries is invalid.
            if prefix_offset is not None and prefix_offset > last_offset:
                marker_identity = None
    except OSError:
        pass
    return (
        RolloutSnapshot(
            thread_id=thread_id,
            record_offset=last_offset,
            record_hash=last_hash,
            turns=builder.view(),
        ),
        marker_identity,
    )


def load_snapshot(codex_home: Path, thread_id: str) -> RolloutSnapshot | None:
    path = find_rollout_path(codex_home, thread_id)
    if path is None:
        return None
    snapshot, _ = _parse_path(path, thread_id)
    return snapshot


def initialize_cursor(codex_home: Path, thread_id: str) -> dict | None:
    snapshot = load_snapshot(codex_home, thread_id)
    if snapshot is None:
        return None
    return _cursor(snapshot)


def peek_partial_transcript(
    codex_home: Path,
    thread_id: str,
    marker: dict | None,
) -> tuple[str, dict] | None:
    """Return the canonical transcript suffix and an exact snapshot cursor."""
    current_path = find_rollout_path(codex_home, thread_id)
    if current_path is None:
        return None

    marker_identity: list[TurnIdentity] | None = None
    marker_runtime = marker.get("runtime") if isinstance(marker, dict) else None
    marker_thread = marker.get("thread_id") if isinstance(marker, dict) else None
    marker_offset = marker.get("record_offset") if isinstance(marker, dict) else None
    marker_hash = marker.get("record_hash") if isinstance(marker, dict) else None
    if not isinstance(marker_offset, int) or marker_offset < 0:
        marker_offset = None
    if not isinstance(marker_hash, str):
        marker_hash = None

    if (
        marker_runtime == "codex"
        and marker_thread == thread_id
        and marker_offset is not None
    ):
        current, marker_identity = _parse_path(
            current_path,
            thread_id,
            prefix_offset=marker_offset,
            expected_prefix_hash=marker_hash,
        )
    else:
        current, _ = _parse_path(current_path, thread_id)
        if (
            marker_runtime == "codex"
            and isinstance(marker_thread, str)
            and marker_thread
            and marker_offset is not None
        ):
            old_path = find_rollout_path(codex_home, marker_thread)
            if old_path is not None:
                old, _ = _parse_path(
                    old_path,
                    marker_thread,
                    stop_offset=marker_offset,
                )
                if old.record_offset == marker_offset and (
                    marker_hash is None or old.record_hash == marker_hash
                ):
                    marker_identity = old.identity()

    if marker_identity is not None:
        start_turn, start_item = _suffix_position(marker_identity, current.turns)
    else:
        start_turn, start_item = _fallback_marker_position(marker, current.turns)
    text = _render_suffix(current.turns, start_turn, start_item)
    return text, _cursor(current)


def _suffix_position(
    old: list[TurnIdentity], current: list[TranscriptTurn]
) -> tuple[int, int]:
    shared = min(len(old), len(current))
    for turn_index in range(shared):
        old_turn = old[turn_index]
        current_turn = current[turn_index]
        current_items = tuple(item.key for item in current_turn.items)
        same_pending_turn = (
            old_turn.key.startswith("pending:")
            and bool(old_turn.item_keys)
            and bool(current_items)
            and old_turn.item_keys[0] == current_items[0]
        )
        if old_turn.key != current_turn.key and not same_pending_turn:
            return turn_index, 0
        item_shared = min(len(old_turn.item_keys), len(current_items))
        item_index = 0
        while (
            item_index < item_shared
            and old_turn.item_keys[item_index] == current_items[item_index]
        ):
            item_index += 1
        if item_index < len(old_turn.item_keys) or item_index < len(current_items):
            return turn_index, item_index
    return shared, 0


def _fallback_marker_position(
    marker: dict | None, current: list[TranscriptTurn]
) -> tuple[int, int]:
    if not isinstance(marker, dict) or marker.get("runtime") != "codex":
        return 0, 0
    turn_id = marker.get("turn_id")
    item_id = marker.get("item_id")
    if not isinstance(turn_id, str) or not turn_id:
        return 0, 0
    for turn_index, turn in enumerate(current):
        if turn.turn_id != turn_id:
            continue
        if isinstance(item_id, str) and item_id:
            for item_index, item in enumerate(turn.items):
                if item.key == item_id:
                    return turn_index, item_index + 1
        # A cursor can be captured after task_started but before the first
        # renderable item. Re-deliver from the turn start instead of assuming an
        # item-less turn was fully delivered; overlap is preferable to a gap.
        return turn_index, 0
    return 0, 0


def _cursor(snapshot: RolloutSnapshot) -> dict:
    turn = snapshot.turns[-1] if snapshot.turns else None
    item = turn.items[-1] if turn is not None and turn.items else None
    return {
        "runtime": "codex",
        "thread_id": snapshot.thread_id,
        "record_offset": snapshot.record_offset,
        "record_hash": snapshot.record_hash,
        "turn_id": turn.turn_id if turn is not None else None,
        "item_id": item.key if item is not None else None,
    }


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        iso = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ts


def _short_args(value: Any) -> str:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = value
        value = decoded
    if isinstance(value, dict):
        for key in (
            "command",
            "file_path",
            "path",
            "url",
            "query",
            "pattern",
            "prompt",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                text = candidate
                break
        else:
            text = json.dumps(value, ensure_ascii=False)
    elif value is None:
        text = ""
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= ARG_TRUNC else text[: ARG_TRUNC - 1] + "…"


def _render_suffix(
    turns: list[TranscriptTurn], start_turn: int, start_item: int
) -> str:
    blocks: list[str] = []
    for turn_index in range(start_turn, len(turns)):
        turn = turns[turn_index]
        first_item = start_item if turn_index == start_turn else 0
        items = turn.items[first_item:]
        if not items:
            continue
        timestamp = _fmt_ts(turn.timestamp)
        lines = [f"[turn {timestamp}]" if timestamp else "[turn]"]
        for item in items:
            if item.kind == "user":
                lines.append(f"[user] {item.text.strip()}")
            elif item.kind == "assistant":
                lines.append(f"[assistant] {item.text.strip()}")
            elif item.kind == "tool":
                args = _short_args(item.arguments)
                error = " (error)" if item.error else ""
                lines.append(f"[tool:{item.name}] {args}{error}".rstrip())
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
