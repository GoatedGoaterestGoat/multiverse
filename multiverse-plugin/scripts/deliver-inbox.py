#!/usr/bin/env python3
"""Multiverse inter-agent inbox delivery hook.

Wired to SessionStart, UserPromptSubmit, and Stop. On each fire:
  1. Read pending messages from this agent's inbox
     (~/.multiverse/inbox/<encoded-cwd>/<name>/*.json)
  2. If empty → exit silently (the common case, ~30ms tax per hook fire).
  3. Otherwise → return injection JSON sized to the firing hook event,
     then delete the consumed files so the next hook fire sees an empty
     inbox.

Output format depends on the hook event:
  - SessionStart, UserPromptSubmit → hookSpecificOutput.additionalContext
  - Stop                            → decision: "block", reason: <content>
    (claude-code prepends "Stop hook feedback:" to the reason and emits it
    as a synthetic user message, triggering another model turn — exactly
    the wakeup we want when an inbox arrives mid-task.)

The agent-callable ``check_inbox`` MCP tool reuses the same renderer +
consume path (``src.inbox.render_messages_as_system_reminder`` +
``consume_messages``) so a hook-delivered batch and a tool-delivered
batch are byte-identical.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make `src` package importable so we can share the formatter + consume
# logic with the MCP-tool path. src.inbox + src.paths are stdlib-only;
# src/__init__.py is empty — total import overhead is single-digit ms.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.inbox import (  # noqa: E402
    consume_messages,
    list_pending_messages,
    render_messages_as_system_reminder,
)


def _read_input() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main() -> None:
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    name = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not name:
        return  # not running under multiverse

    messages = list_pending_messages(cwd, name)
    if not messages:
        return  # nothing to deliver

    hook_input = _read_input()
    event = hook_input.get("hook_event_name") or "UserPromptSubmit"

    body = render_messages_as_system_reminder(messages)

    if event == "Stop":
        out = {"decision": "block", "reason": body}
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": body,
            }
        }

    print(json.dumps(out))
    consume_messages(messages)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — hooks must never crash claude
        sys.stderr.write(f"deliver-inbox: {e}\n")
        sys.exit(0)
