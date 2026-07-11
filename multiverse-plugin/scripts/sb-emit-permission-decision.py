#!/usr/bin/env python3
"""Hook: emit a ``user.permission_decision`` signal for the SecondBrain connector.

Wired to **PostToolUse[.*]** — the pair-close of the permission pair. If a permission
prompt fired for this tool_use_id (as flagged by :mod:`sb-emit-permission-request`), we
emit ``user.permission_decision`` with the decision derived from the tool_response.
Otherwise we silently clean up the PreToolUse candidate (no permission was involved).

Decision heuristic: Claude Code emits a distinct denial marker in the tool_response when
the user denies a tool call (an error/refusal payload), while an allow yields a normal
response. We check the tool_response defensively for common denial signatures; unknown
shapes fall through to ``allow`` since PostToolUse fires *after* the tool executed, and a
tool that executed was necessarily permitted. The raw ``tool_response`` rides in the
signal for anyone (SB or debugging) who wants to inspect it.

Stays CHEAP; gated (at signal-write) on the workspace being seeded; all failures
swallowed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Substrings that indicate a user-denied tool call in the tool_response. Non-exhaustive on
# purpose — we prefer false-allows (which are harmless: PostToolUse only fires post-exec)
# over false-denies (which would misinform SB's model of the user's decision).
_DENIAL_HINTS = (
    "doesn't want to proceed",
    "permission denied by user",
    "user rejected",
    "user denied",
    "declined the tool call",
)


def _looks_like_denial(tool_response) -> bool:
    if tool_response is None:
        return False
    # tool_response can be a dict, list, or string depending on the tool + version.
    def _walk(v) -> bool:
        if isinstance(v, str):
            s = v.lower()
            return any(h in s for h in _DENIAL_HINTS)
        if isinstance(v, dict):
            if v.get("isError") and _walk(v.get("content")):
                return True
            return any(_walk(x) for x in v.values())
        if isinstance(v, list):
            return any(_walk(x) for x in v)
        return False
    return _walk(tool_response)


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
    if not tool_use_id:
        return

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from src import sb_notify

        # Only fire the decision event if a permission prompt actually happened for this
        # tool_use_id. Otherwise this is just a routine PostToolUse for an auto-allowed
        # tool — silently clean up the candidate.
        if sb_notify.had_permission_prompt(tool_use_id):
            tool_response = data.get("tool_response")
            decision = "deny" if _looks_like_denial(tool_response) else "allow"
            sb_notify.record_permission_decision_signal(
                cwd,
                agent,
                tool_use_id,
                decision,
                data.get("tool_name", ""),
                (data.get("tool_input") or None),
                tool_response,
            )
        sb_notify.cleanup_pending_tool(tool_use_id)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
