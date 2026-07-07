#!/usr/bin/env python3
"""Hook: emit an ``agent.response`` turn-end signal for the SecondBrain connector.

Wired to the **Stop** hook — the agent finished a turn NORMALLY and went idle. That is
exactly (and only) what ``agent.response`` is: waiting-on-permission and waiting-on-a-
question (the ask tool) are their own separate event pairs, NOT agent.response.

This is the **firsthand** production point — the agent's own turn-end. It stays deliberately
CHEAP: it records only a lightweight signal, NOT the transcript. The connector daemon
expands each signal into the full ``agent.response`` event (generating the partial
transcript via ``sb_transcript``) off the agent's critical path, so no agent turn is slowed
by a parse.

``sb_notify.record_response_signal`` applies the "workspace has connected to SB before"
gate, so this is a no-op for a workspace that never opted in. All failures are swallowed —
a hook must never break the agent's turn.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    cwd = os.environ.get("MULTIVERSE_SESSION_CWD")
    agent = os.environ.get("MULTIVERSE_SESSION_NAME")
    if not cwd or not agent:
        return  # not a named multiverse-managed session — nothing to report

    # sb_notify is pure/light (no websockets/cryptography) — importable under system python.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from src import sb_notify

        sb_notify.record_response_signal(cwd, agent)
    except Exception:  # noqa: BLE001 - never break the agent's turn
        pass


if __name__ == "__main__":
    main()
