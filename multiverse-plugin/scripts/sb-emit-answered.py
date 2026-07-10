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

    # The AskUserQuestion tool_response shape (per the tool schema):
    #   {"answers": {<question_text>: <picked label OR "Other"-typed text>},
    #    "annotations"?: {<question_text>: {"notes"?: <free-text/Other-typed>, "preview"?: ...}}}
    # Be defensive about shape drift — accept a bare dict of answers too, and pass along
    # annotations if present so an "Other" typed text isn't silently dropped when the
    # tool routes it via annotations.notes rather than inline in the answer string.
    resp = data.get("tool_response")
    answers = None
    annotations = None
    if isinstance(resp, dict):
        if "answers" in resp:
            answers = resp.get("answers")
            annotations = resp.get("annotations")
        else:
            answers = resp
    else:
        answers = resp
    # Keep the original questions for context so SB can re-render the pair on its side.
    questions = (data.get("tool_input") or {}).get("questions")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from src import sb_notify

        sb_notify.record_answered_signal(
            cwd, agent, tool_use_id, answers, questions, annotations
        )
    except Exception:  # noqa: BLE001 - never break the agent's turn
        pass


if __name__ == "__main__":
    main()
