"""Append-only, per-day log of inter-agent communication events.

Exists to make delivery bugs diagnosable after the fact without having to
reconstruct the sequence of events from memory or SSH into a remote to
read raw JSONLs. Three event kinds, matching the three places delivery
can silently go wrong:

  message_sent       — message_agent was called (with or without
                       push_notification). Logged once per call,
                       regardless of what happens next.
  notification_routed — the push_notification routing OUTCOME: which
                       path was taken (auto-resume, tmux wake, fallback
                       to passive, already-listening) and whether it
                       succeeded. Logged only when push_notification=True.
  inbox_delivered    — messages were actually claimed and handed to an
                       agent's context — via a hook (SessionStart /
                       UserPromptSubmit / Stop) or the check_inbox tool.

Location: ``~/.multiverse/logs/inter-agent/<YYYY-MM-DD>.jsonl`` (one file
per calendar day, local time). Retention: files whose entire day is more
than RETENTION_DAYS in the past are deleted the next time anything is
logged — cheap, since the directory only ever holds a couple weeks' worth
of files, and avoids needing a separate cron/scheduled cleanup.

Logging is always best-effort: any failure here must never break the
caller (a hook, an MCP tool, or the monitor).
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any, Optional

from .paths import multiverse_dir

RETENTION_DAYS = 7
_MAX_PREVIEW_CHARS = 300

_DAY_FMT = "%Y-%m-%d"


def _log_dir() -> Path:
    return multiverse_dir() / "logs" / "inter-agent"


def _day_str(ts: float) -> str:
    return time.strftime(_DAY_FMT, time.localtime(ts))


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "?"


def _prune_old(now: float) -> None:
    d = _log_dir()
    try:
        entries = list(d.glob("*.jsonl"))
    except OSError:
        return
    cutoff = now - RETENTION_DAYS * 86400
    for p in entries:
        try:
            day_start = time.mktime(time.strptime(p.stem, _DAY_FMT))
        except ValueError:
            continue  # not one of ours (unexpected filename) — leave it alone
        if day_start + 86400 <= cutoff:
            try:
                p.unlink()
            except OSError:
                pass


def _preview(text: Optional[str], n: int = _MAX_PREVIEW_CHARS) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _append(event: dict[str, Any]) -> None:
    try:
        now = time.time()
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        _prune_old(now)
        record = {"ts": now, "host": _host(), **event}
        path = d / f"{_day_str(now)}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def log_message_sent(
    *,
    sender: str,
    recipient: str,
    cwd: str,
    push_notification: bool,
    message_id: str,
    message: str,
    sender_runtime: Optional[str] = None,
    recipient_runtime: Optional[str] = None,
) -> None:
    """A message_agent call happened — logged unconditionally, before
    any routing/delivery is attempted."""
    event: dict[str, Any] = {
        "event": "message_sent",
        "from": sender,
        "to": recipient,
        "cwd": cwd,
        "push_notification": push_notification,
        "message_id": message_id,
        "message_preview": _preview(message),
    }
    if sender_runtime:
        event["from_runtime"] = sender_runtime
    if recipient_runtime:
        event["to_runtime"] = recipient_runtime
    _append(event)


def log_notification_routed(
    *,
    sender: str,
    recipient: str,
    cwd: str,
    route: str,
    success: Optional[bool] = None,
    sender_runtime: Optional[str] = None,
    recipient_runtime: Optional[str] = None,
) -> None:
    """The push_notification routing outcome for one message_agent call.

    ``route`` records the selected auto-resume, tmux wake, already-listening,
    or passive-fallback path. ``success`` is only meaningful for routes that
    can fail (tmux wake and auto-resume).
    """
    event: dict[str, Any] = {
        "event": "notification_routed",
        "from": sender,
        "to": recipient,
        "cwd": cwd,
        "route": route,
    }
    if success is not None:
        event["success"] = success
    if sender_runtime:
        event["from_runtime"] = sender_runtime
    if recipient_runtime:
        event["to_runtime"] = recipient_runtime
    _append(event)


def log_inbox_delivered(
    *,
    recipient: str,
    cwd: str,
    hook_event: str,
    messages: list[dict],
    recipient_runtime: Optional[str] = None,
) -> None:
    """Messages were actually claimed and handed to an agent — via a
    delivery hook (SessionStart/UserPromptSubmit/Stop) or the
    check_inbox tool. ``messages`` is whatever claim_pending_messages
    returned (plain message dicts: from, message_id, body, ts)."""
    event: dict[str, Any] = {
        "event": "inbox_delivered",
        "to": recipient,
        "cwd": cwd,
        "hook_event": hook_event,
        "count": len(messages),
        "messages": [
            {
                "from": m.get("from"),
                "message_id": m.get("message_id"),
                "preview": _preview(m.get("body", "")),
            }
            for m in messages
        ],
    }
    if recipient_runtime:
        event["to_runtime"] = recipient_runtime
    _append(event)
