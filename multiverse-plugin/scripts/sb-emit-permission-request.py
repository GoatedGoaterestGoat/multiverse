#!/usr/bin/env python3
"""Hook: emit an ``agent.permission_request`` signal for the SecondBrain connector.

Wired to **Notification** — fires when Claude Code shows the user a permission prompt
(among other blocking notifications). Since the Notification payload does NOT carry the
tool_use_id it's about, we correlate to the session's most-recent-pending PreToolUse
candidate (stashed by :mod:`sb-track-tool`), which is the tool the user is being asked
to permit. Parallel tool calls inside one turn are the imperfect edge case per the doc.

Filters:
- Skips informational notifications (idle_prompt/elicitation_*/auth_success/computer_use_*)
  — same set as :mod:`on-notification`.
- Additionally requires the message to look like a permission prompt (contains
  ``"permission"``, case-insensitive) so other blocking notifications don't fire this event.

Stays CHEAP — records only a lightweight signal (tool candidate data); the connector
expands to the full ``agent.permission_request`` (attaching the partial transcript = "why"
context) OFF the agent's critical path. Gated on the workspace being seeded. All failures
swallowed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Same informational set as on-notification.sh + sb-emit-response.py.
_INFORMATIONAL = {
    "idle_prompt",
    "elicitation_complete",
    "elicitation_response",
    "auth_success",
    "computer_use_enter",
    "computer_use_exit",
}


def main() -> None:
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    agent = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not agent:
        return

    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return

    ntype = data.get("notification_type") or ""
    if ntype in _INFORMATIONAL:
        return  # not blocking

    message = str(data.get("message") or "")
    if "permission" not in message.lower():
        return  # blocking but not a permission prompt (e.g. an elicitation)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from src import sb_notify

        candidate = sb_notify.latest_pending_tool_for_session(cwd, agent)
        if not candidate:
            return  # no correlating PreToolUse — nothing to link this Notification to
        # AskUserQuestion is not a permission-gated tool — its Notification is the modal
        # itself, not a permission prompt. Otherwise the three-hook correlation would
        # false-positive an agent.permission_request with the same tool_use_id as the
        # agent.question. Silently skip.
        if candidate.get("tool_name") == "AskUserQuestion":
            return
        tool_use_id = candidate.get("tool_use_id", "")
        # Mark it (durable) so PostToolUse knows a permission prompt happened here,
        # even if the connector consumes the request signal before PostToolUse fires.
        sb_notify.mark_permission_requested(tool_use_id)
        sb_notify.record_permission_request_signal(
            cwd,
            agent,
            tool_use_id,
            candidate.get("tool_name", ""),
            candidate.get("tool_input"),
        )
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
