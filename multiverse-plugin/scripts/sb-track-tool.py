#!/usr/bin/env python3
"""Hook: stash a tool-use candidate for the SecondBrain permission-pair correlation.

Wired to **PreToolUse[.*]** — fires before every tool call. We don't yet know whether
this specific call will require permission (that's decided by the user's allowlist at
Claude Code time), so we stash the tool_use_id + tool_name + tool_input unconditionally.
The Notification hook (:mod:`sb-emit-permission-request`) may later flag this candidate
with a ``.perm`` marker; PostToolUse (:mod:`sb-emit-permission-decision`) always cleans
it up regardless.

Stays deliberately CHEAP (only sb_notify — no websockets/cryptography). Bounded working
set — candidates cleared on PostToolUse. All failures swallowed so a hook never breaks a
tool call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    agent = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not agent:
        return

    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return

    tool_use_id = data.get("tool_use_id") or data.get("toolUseId") or ""
    tool_name = data.get("tool_name") or ""
    tool_input = data.get("tool_input")
    if not tool_use_id or not tool_name:
        return

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from src import sb_notify

        sb_notify.record_pending_tool(cwd, agent, tool_use_id, tool_name, tool_input)
    except Exception:  # noqa: BLE001 - never break the tool call
        pass


if __name__ == "__main__":
    main()
