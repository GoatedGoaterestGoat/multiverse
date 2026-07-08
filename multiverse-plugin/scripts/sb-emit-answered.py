#!/usr/bin/env python3
"""Hook: emit a ``user.answered`` signal for the SecondBrain connector.

Wired to **PostToolUse[AskUserQuestion]** — the pair-close of the ask-tool question. At
this point we have the tool response (the user's selection(s)) and the same
``tool_use_id`` as the earlier PreToolUse fire, which is the shared ``question_id`` SB
uses to pair this with the ``agent.question`` it already ingested.

The signal is intentionally minimal (structured answers + question_id + the original
questions for context). No transcript is attached — the agent was blocked in the ask tool
between pre and post, so nothing new happened on its side.

Gated on the workspace being seeded; all failures swallowed.
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

    if data.get("tool_name") != "AskUserQuestion":
        return
    tool_use_id = data.get("tool_use_id") or data.get("toolUseId") or ""
    if not tool_use_id:
        return

    # The tool response format Claude Code exposes may vary — accept both a raw list
    # (the AskUserQuestion answers, one per input question) and a wrapping dict.
    resp = data.get("tool_response")
    answers = None
    if isinstance(resp, dict):
        answers = resp.get("answers") if "answers" in resp else resp
    else:
        answers = resp
    # Keep the original questions for context so SB can re-render the pair on its side.
    questions = (data.get("tool_input") or {}).get("questions")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from src import sb_notify

        sb_notify.record_answered_signal(cwd, agent, tool_use_id, answers, questions)
    except Exception:  # noqa: BLE001 - never break the agent's turn
        pass


if __name__ == "__main__":
    main()
