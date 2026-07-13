#!/usr/bin/env python3
"""Hook: emit an ``agent.question`` signal for the SecondBrain connector.

Wired to **PreToolUse[AskUserQuestion]** — the firsthand production point for an ask-tool
multi-choice question. At this exact moment we have the tool input (the ``questions``
array with per-question ``question`` / ``header`` / ``options`` / ``multiSelect``) and the
``tool_use_id`` — which becomes the stable ``question_id`` the paired ``user.answered``
event will echo.

Stays CHEAP: only a lightweight signal is written (not the transcript). The connector
generates the partial transcript OFF the agent's critical path — no ask is slowed by a
parse.

``sb_notify.record_question_signal`` applies the "workspace has connected to SB before"
gate, so this is a no-op for a workspace that never opted in. All failures are swallowed —
a hook must never break the agent's turn.
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
        return  # not a named multiverse-managed session — nothing to report

    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return

    # Defensive: only fire if it's actually the ask tool with a questions payload.
    if data.get("tool_name") != "AskUserQuestion":
        return
    tool_input = data.get("tool_input") or {}
    questions = tool_input.get("questions") or []
    tool_use_id = data.get("tool_use_id") or data.get("toolUseId") or ""
    if not questions or not tool_use_id:
        return

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from src import sb_notify

        sb_notify.record_question_signal(cwd, agent, tool_use_id, questions)
        # ALSO populate the pending_tools/ cache — sb-track-tool.py at PreToolUse[.*]
        # doesn't reliably fire for AskUserQuestion (interactive tool; Claude Code seems
        # to skip the general matcher when a specific one exists). Duplicating the
        # record here at the specific matcher guarantees find_pending_askuserquestion
        # (the resolve-from-live-state path) has a source of truth.
        sb_notify.record_pending_tool(cwd, agent, tool_use_id, "AskUserQuestion", tool_input)
    except Exception:  # noqa: BLE001 - never break the agent's turn
        pass


if __name__ == "__main__":
    main()
