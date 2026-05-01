"""Cross-monitor compaction lock state.

While the user is compacting a session through one monitor, every other
monitor in the workspace must see the row as ``compacting`` and refuse to
start, resume, or kill it. The lock is a single file:

    ~/.multiverse/compacting/<claude_uuid>.json

Created with ``O_EXCL`` so two simultaneous Compact actions across two
monitors can't both win. Body carries the holding monitor's PID so a
second monitor can take over if the original was killed mid-compaction
(stale-lock recovery).

Lock file lifecycle:
  acquire_lock()  → atomic create; releases on stale-PID detection
  release_lock()  → unlink

Dashboard renders any row whose ``claude_uuid`` is in ``list_locked()``
with status overridden to ``compacting`` and disables Enter/Resume/Kill.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .paths import multiverse_dir


def _compact_dir() -> Path:
    return multiverse_dir() / "compacting"


def lock_path(claude_uuid: str) -> Path:
    return _compact_dir() / f"{claude_uuid}.json"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(claude_uuid: str, monitor_pid: int | None = None) -> bool:
    """Atomically take the compacting lock for ``claude_uuid``.

    Returns ``True`` on success, ``False`` if another live monitor holds
    it. Stale locks (whose holding PID is dead) are reaped automatically.
    """
    _compact_dir().mkdir(parents=True, exist_ok=True)
    p = lock_path(claude_uuid)
    payload = json.dumps({
        "started_at": time.time(),
        "monitor_pid": monitor_pid or os.getpid(),
    })

    for _attempt in range(3):
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # Maybe a stale lock — check the holder PID.
            try:
                existing = json.loads(p.read_text())
                holder = int(existing.get("monitor_pid", 0))
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                # Unreadable lock; bail rather than guess.
                return False
            if _alive(holder):
                return False
            # Stale — try to remove and retry.
            try:
                p.unlink()
            except OSError:
                return False
            continue

        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
        except OSError:
            try:
                p.unlink()
            except OSError:
                pass
            return False
        return True

    return False


def release_lock(claude_uuid: str) -> None:
    try:
        lock_path(claude_uuid).unlink()
    except OSError:
        pass


def is_locked(claude_uuid: str) -> bool:
    return lock_path(claude_uuid).exists()


def read_lock(claude_uuid: str) -> dict | None:
    try:
        return json.loads(lock_path(claude_uuid).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def list_locked() -> set[str]:
    """Return the set of claude UUIDs currently locked for compaction."""
    d = _compact_dir()
    if not d.is_dir():
        return set()
    out: set[str] = set()
    for p in d.glob("*.json"):
        out.add(p.stem)
    return out
