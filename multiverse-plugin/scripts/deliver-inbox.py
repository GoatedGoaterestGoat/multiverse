#!/usr/bin/env python3
"""Multiverse inter-agent inbox delivery hook.

Wired to SessionStart, UserPromptSubmit, and Stop. On each fire:
  1. Read pending messages from this agent's inbox
     (~/.multiverse/inbox/<encoded-cwd>/<name>/*.json)
  2. If empty → exit silently (the common case, ~50ms tax per hook fire).
  3. Otherwise → return injection JSON sized to the firing hook event,
     then delete the consumed files so the next hook fire sees an empty
     inbox.

Output format depends on the hook event:
  - SessionStart, UserPromptSubmit → hookSpecificOutput.additionalContext
  - Stop                            → decision: "block", reason: <content>
    (claude-code prepends "Stop hook feedback:" to the reason and emits it
    as a synthetic user message, triggering another model turn — exactly
    the wakeup we want when an inbox arrives mid-task.)

Stdlib-only on purpose: invoked per hook fire, so we want a fast Python
startup with no uv overhead.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


INBOX_DIR = Path(os.path.expanduser("~/.multiverse")) / "inbox"


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _read_input() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _gather_messages(inbox: Path) -> list[dict]:
    if not inbox.is_dir():
        return []
    out: list[dict] = []
    for path in inbox.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["__path"] = str(path)
        out.append(data)
    out.sort(key=lambda d: d.get("ts", 0))
    return out


def _consume(messages: list[dict]) -> None:
    for m in messages:
        path = m.get("__path")
        if not path:
            continue
        try:
            Path(path).unlink()
        except OSError:
            pass


def _fmt_lines(text: str, prefix: str = "> ") -> list[str]:
    if not text:
        return [f"{prefix}(empty)"]
    return [f"{prefix}{line}" for line in text.splitlines() or [text]]


def _render(messages: list[dict]) -> str:
    n = len(messages)
    plural = "s" if n != 1 else ""
    parts: list[str] = []
    parts.append("<system-reminder>")
    parts.append(
        f"[Multiverse Inter-Agent] You have received {n} new "
        f"message{plural} from other agents in your workspace."
    )
    parts.append("")
    for i, m in enumerate(messages, 1):
        from_name = m.get("from", "?")
        body = m.get("body", "")
        ts = m.get("ts", 0)
        ts_str = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
        )

        parts.append("─" * 56)
        parts.append(f"[{i}/{n}] Message from agent: {from_name}")
        parts.append(f"        received: {ts_str}")
        parts.append("")
        parts.extend(_fmt_lines(body))
        parts.append("")

    parts.append("─" * 56)
    parts.append(
        "You may use any tools (Read, Bash, Grep, etc.) to act on these "
        "messages. To reply to a sender, call the `message_agent` tool "
        "with their name as the target. Replies are not required — only "
        "respond if it's useful."
    )
    parts.append("</system-reminder>")
    return "\n".join(parts)


def main() -> None:
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not name:
        return  # not running under multiverse

    inbox = INBOX_DIR / _encode_cwd(cwd) / name
    messages = _gather_messages(inbox)
    if not messages:
        return  # nothing to deliver

    hook_input = _read_input()
    event = hook_input.get("hook_event_name") or "UserPromptSubmit"

    body = _render(messages)

    if event == "Stop":
        # Stop hook can't return additionalContext — but decision:block with
        # `reason` becomes a synthetic user message ("Stop hook feedback:\n…")
        # and triggers another model turn, which is exactly the wakeup we
        # want when an inbox arrives mid-task.
        out = {"decision": "block", "reason": body}
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": body,
            }
        }

    print(json.dumps(out))
    _consume(messages)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — hooks must never crash claude
        sys.stderr.write(f"deliver-inbox: {e}\n")
        sys.exit(0)
