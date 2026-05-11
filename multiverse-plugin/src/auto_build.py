"""Per-session auto tree-build toggle.

Background tree-build (the privileged summarization agent that fires on
every ``UserPromptSubmit``) is **opt-in per session**. The toggle is a
marker file at:

    $MULTIVERSE_ROOT/auto_build/<claude_uuid>

Presence of the file means "auto-build enabled for this session";
absence means disabled (the default). The toggle is per claude_uuid so
it works uniformly across multiverse-wrapped sessions and dormant
claude sessions, and persists across monitor / claude restarts.

Why opt-in by default: each turn build is a privileged ``claude --resume``
call against a synthetic JSONL — non-trivial latency, contributes to
subscription rate limits, and most sessions never reach compaction. The
user enables auto-build per-session when they expect to compact it later.

API:
    is_enabled(uuid)  -> bool
    enable(uuid)      -> None
    disable(uuid)     -> None
    toggle(uuid)      -> bool   (returns the new state)
    list_enabled()    -> set[str]
"""

from __future__ import annotations

from pathlib import Path

from .paths import multiverse_dir


def _dir() -> Path:
    return multiverse_dir() / "auto_build"


def marker_path(claude_uuid: str) -> Path:
    return _dir() / claude_uuid


def is_enabled(claude_uuid: str) -> bool:
    return marker_path(claude_uuid).exists()


def enable(claude_uuid: str) -> None:
    p = marker_path(claude_uuid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def disable(claude_uuid: str) -> None:
    try:
        marker_path(claude_uuid).unlink()
    except OSError:
        pass


def toggle(claude_uuid: str) -> bool:
    """Flip the state and return the NEW state (True == enabled)."""
    if is_enabled(claude_uuid):
        disable(claude_uuid)
        return False
    enable(claude_uuid)
    return True


def list_enabled() -> set[str]:
    """Set of claude UUIDs with auto-build currently enabled."""
    d = _dir()
    if not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if p.is_file()}
