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


def _fmt_lines(text: str, prefix: str = "> ") -> list[str]:
    if not text:
        return [f"{prefix}(empty)"]
    return [f"{prefix}{line}" for line in text.splitlines() or [text]]


def _format_one_message(m: dict, idx: int, total: int) -> list[str]:
    """Render a single message: separator, header lines, body. Shared by
    both delivery paths so the per-message layout stays in lockstep."""
    from_name = m.get("from", "?")
    body = m.get("body", "")
    ts = m.get("ts", 0)
    ts_str = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
    )
    return [
        "─" * 56,
        f"[{idx}/{total}] From agent: {from_name}",
        f"        received: {ts_str}",
        "",
        *_fmt_lines(body),
        "",
    ]


def render_messages_as_system_reminder(messages: list[dict]) -> str:
    """Hook delivery format: push-style, wrapped in ``<system-reminder>``.

    Used by ``scripts/deliver-inbox.py`` because hook injection drops the
    content into the conversation as a synthetic user message (via
    ``additionalContext`` or Stop's ``decision: "block"`` reason). The
    ``<system-reminder>`` wrapper tells the model "this is out-of-band, not
    a real user turn" and the "You have received N new messages" header
    frames it as a push notification.
    """
    n = len(messages)
    plural = "s" if n != 1 else ""
    parts: list[str] = ["<system-reminder>"]
    parts.append(
        f"[Multiverse Inter-Agent] You have received {n} new "
        f"message{plural} from other agents in your workspace."
    )
    parts.append("")
    for i, m in enumerate(messages, 1):
        parts.extend(_format_one_message(m, i, n))
    parts.append("─" * 56)
    parts.append(
        "You may use any tools (Read, Bash, Grep, etc.) to act on these "
        "messages. To reply to a sender, call the `message_agent` tool "
        "with their name as the target. Replies are not required — only "
        "respond if it's useful."
    )
    parts.append("</system-reminder>")
    return "\n".join(parts)


def render_messages_as_tool_result(messages: list[dict]) -> str:
    """check_inbox tool-result format: pull-style, plain text.

    The agent explicitly called ``check_inbox`` and is awaiting a tool
    result — so wrapping in ``<system-reminder>`` (which signals
    "unsolicited push") would be a mixed signal, and the
    "You have received N new messages" header would be off-tone. Same
    per-message layout as the hook path; different framing.
    """
    n = len(messages)
    plural = "s" if n != 1 else ""
    parts: list[str] = [f"{n} pending inbox message{plural}:", ""]
    for i, m in enumerate(messages, 1):
        parts.extend(_format_one_message(m, i, n))
    parts.append("─" * 56)
    parts.append(
        "To reply to any sender, call `message_agent` with their name as "
        "the target. Replies are not required — only respond if it's useful."
    )
    return "\n".join(parts)
