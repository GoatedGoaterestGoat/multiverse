#!/usr/bin/env python3
"""Hook: emit a ``user.prompt`` turn-start signal for the SecondBrain connector.

Wired to **UserPromptSubmit** — fires whenever a user prompt lands in a Claude session,
whether from a real user typing at the terminal, SB's own ``prompt_agent`` action's
send-keys delivery, or a resume's auto-submitted positional prompt. All three semantically
"the user prompted this agent," so a single hook catches all three.

Stays CHEAP: only a lightweight signal is written (the prompt text + session context) —
NO transcript parse on the agent's critical path. The connector daemon expands each
signal into the full ``user.prompt`` event (generating the partial transcript via
``sb_transcript`` + composing ``overview_since_last``) off the critical path.

``sb_notify.record_user_prompt_signal`` applies the "workspace has connected to SB before"
gate, so this is a no-op for a workspace that never opted in. All failures are swallowed —
a hook must never break the user's prompt from submitting.
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
    text = str(data.get("prompt") or "")
    if not text:
        return  # nothing to observe

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from src import sb_notify

        sb_notify.record_user_prompt_signal(cwd, agent, text)
    except Exception:  # noqa: BLE001 - never break the user's prompt
        pass


if __name__ == "__main__":
    main()
