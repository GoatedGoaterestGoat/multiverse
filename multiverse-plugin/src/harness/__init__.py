"""Harness registry.

Resolution order for "which harness is this process talking to":

  1. explicit argument (a session's ``meta.json`` records the harness it was
     created with, so a mixed dashboard routes each row correctly)
  2. ``$MULTIVERSE_HARNESS``
  3. ``claude`` — the historical default, so existing sessions and existing
     state directories keep working with no migration.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from .base import ForkPlan, Harness, TranscriptInfo
from .claude import ClaudeHarness
from .pi import PiHarness

DEFAULT_HARNESS = "claude"

_REGISTRY: dict[str, Harness] = {
    ClaudeHarness.name: ClaudeHarness(),
    PiHarness.name: PiHarness(),
}


def available() -> list[str]:
    return sorted(_REGISTRY)


def installed() -> list[str]:
    """Harnesses whose CLI is actually resolvable on this machine.

    Used to decide what the new-session dialog offers: showing pi to someone
    who has not installed it produces a session that dies on spawn. The
    default harness is always included so the dialog is never empty.
    """
    out = [
        name for name, h in sorted(_REGISTRY.items())
        if shutil.which(h.binary())
    ]
    if DEFAULT_HARNESS not in out:
        out.append(DEFAULT_HARNESS)
    return out


def live_tokens(name: str, session_id: str, cwd: str) -> int:
    """Dashboard token estimate for one session, via its harness."""
    try:
        h = get_harness(name)
        path = h.transcript_path(session_id, cwd)
    except (ValueError, OSError):
        return 0
    if not path.exists():
        return 0
    return h.live_tokens(path)


def get_harness(name: Optional[str] = None) -> Harness:
    key = (name or os.environ.get("MULTIVERSE_HARNESS") or DEFAULT_HARNESS).strip()
    try:
        return _REGISTRY[key]
    except KeyError:
        raise ValueError(
            f"unknown harness {key!r}; available: {', '.join(available())}"
        ) from None


__all__ = [
    "DEFAULT_HARNESS",
    "ForkPlan",
    "Harness",
    "TranscriptInfo",
    "available",
    "get_harness",
    "installed",
    "live_tokens",
]
