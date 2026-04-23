"""Inter-agent message inbox.

A single on-disk surface:

  ~/.multiverse/inbox/<encoded-cwd>/<recipient-name>/<message-id>.json
                                          — pending message file. Written
                                             atomically by message_agent
                                             (one tool call = one file).
                                             Consumed (deleted) by the
                                             deliver-inbox hook after a
                                             single delivery as system-
                                             reminder content.

Files are written via tmp+rename so the deliver-inbox hook never sees a
partial file mid-write.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from .paths import multiverse_dir


MULTIVERSE_DIR = multiverse_dir()
INBOX_DIR = MULTIVERSE_DIR / "inbox"


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def agent_inbox_dir(cwd: str, name: str) -> Path:
    return INBOX_DIR / _encode_cwd(cwd) / name


def new_message_id() -> str:
    return str(uuid.uuid4())


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.rename(path)


def write_message_to_inbox(
    sender_name: str,
    recipient_name: str,
    recipient_cwd: str,
    message: str,
) -> Path:
    """Drop a message into the recipient's inbox."""
    message_id = new_message_id()
    inbox_file = agent_inbox_dir(recipient_cwd, recipient_name) / f"{message_id}.json"
    _atomic_write_json(
        inbox_file,
        {
            "message_id": message_id,
            "from": sender_name,
            "body": message,
            "ts": time.time(),
        },
    )
    return inbox_file


def list_pending_messages(cwd: str, name: str) -> list[dict]:
    """List pending inbox messages for an agent, sorted by ts ascending.

    Each dict is the message JSON augmented with ``__path`` (string) so
    callers can pass it to ``consume_messages``.
    """
    d = agent_inbox_dir(cwd, name)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for path in d.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["__path"] = str(path)
        out.append(data)
    out.sort(key=lambda d: d.get("ts", 0))
    return out


def consume_messages(messages: list[dict]) -> None:
    """Delete the underlying files of the given pending messages."""
    for m in messages:
        path = m.get("__path")
        if not path:
            continue
        try:
            Path(path).unlink()
        except OSError:
            pass


def pending_inbox_count(cwd: str, name: str) -> int:
    """Cheap count for TUI display. Doesn't parse files."""
    d = agent_inbox_dir(cwd, name)
    if not d.is_dir():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except OSError:
        return 0
