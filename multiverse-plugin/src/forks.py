"""On-disk registry of ask-fork session UUIDs.

``ask_agent`` / ``ask_all`` spawn ephemeral ``claude --fork-session``
subprocesses to answer side-questions. Each fork's JSONL inherits the
origin session's ``custom-title``, so a leftover fork would masquerade as
a dormant agent on the dashboard and in ``list_agents``.

The monitor has an in-memory ``_active_fork_uuids`` set, but it only ever
covered the TUI ask-all path — the only one that runs inside the monitor
process. Agent-initiated ``ask_agent`` runs in a separate MCP-server
process and could never reach that set.

This module makes fork tracking process-independent: each fork UUID is a
file under ``<multiverse_root>/forks/``, written before the subprocess is
spawned and removed after cleanup. ``build_dashboard_rows`` reads the
directory and skips those UUIDs — so forks are hidden no matter which
process spawned them, and the hiding survives a kill mid-ask (when the
in-process ``finally`` cleanup never runs).

``gc()`` reaps stale entries — and the orphaned JSONLs they point at — so
a fork killed mid-ask doesn't linger forever.
"""

from __future__ import annotations

import time
from pathlib import Path

from .atomic_files import atomic_write_text
from .paths import multiverse_dir


def _forks_dir() -> Path:
    return multiverse_dir() / "forks"


def mark_active(fork_uuid: str, jsonl_path: Path) -> None:
    """Register a fork UUID before spawning its subprocess.

    The registry file records the spawn time and the fork JSONL path so a
    later ``gc()`` sweep can delete an orphaned JSONL if the in-process
    cleanup never ran.
    """
    d = _forks_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(d / fork_uuid, f"{time.time()}\n{jsonl_path}\n")
    except OSError:
        pass


def mark_done(fork_uuid: str) -> None:
    """Remove a fork UUID's registry entry after its JSONL has been cleaned."""
    try:
        (_forks_dir() / fork_uuid).unlink()
    except OSError:
        pass


def active_uuids() -> set[str]:
    """UUIDs of all registered forks — in-flight or orphaned-but-not-yet-GC'd.

    Callers union this into the set of claude UUIDs to skip when scanning
    for dormant sessions, so a fork JSONL is never surfaced as an agent.
    """
    d = _forks_dir()
    if not d.is_dir():
        return set()
    try:
        return {p.name for p in d.iterdir() if p.is_file()}
    except OSError:
        return set()


def gc(max_age_seconds: float = 3600.0) -> None:
    """Reap fork registry entries older than ``max_age_seconds``.

    For each stale entry, deletes the orphaned fork JSONL it points at (if
    it still exists), then the registry file. A fork subprocess is bounded
    by the ask timeout (default 60s), so an entry older than an hour is
    unambiguously an orphan from an ask that was killed mid-flight.
    """
    d = _forks_dir()
    if not d.is_dir():
        return
    now = time.time()
    try:
        entries = list(d.iterdir())
    except OSError:
        return
    for p in entries:
        if not p.is_file():
            continue
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        try:
            ts = float(lines[0]) if lines else 0.0
        except ValueError:
            ts = 0.0
        if now - ts < max_age_seconds:
            continue
        if len(lines) >= 2 and lines[1].strip():
            try:
                Path(lines[1].strip()).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            p.unlink()
        except OSError:
            pass
