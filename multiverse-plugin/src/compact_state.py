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

from .atomic_files import atomic_create_json, exclusive_file_lock
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
    payload = {
        "started_at": time.time(),
        "monitor_pid": monitor_pid or os.getpid(),
    }

    guard = _compact_dir() / ".claims.guard"
    with exclusive_file_lock(guard):
        for _attempt in range(3):
            try:
                created = atomic_create_json(p, payload)
            except OSError:
                return False
            if not created:
                # Maybe a stale lock — check the holder PID.
                try:
                    existing = json.loads(p.read_text())
                    holder = int(existing.get("monitor_pid", 0))
                except (OSError, json.JSONDecodeError, ValueError, TypeError):
                    # Unreadable lock; bail rather than guess.
                    return False
                if _alive(holder):
                    return False
                # Stale — remove it while still holding the acquisition guard.
                try:
                    p.unlink()
                except OSError:
                    return False
                continue
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
    """Return the set of claude UUIDs currently locked for compaction by a
    LIVE monitor.

    Staleness matters here, not just in ``acquire_lock``. A lock whose
    holder is dead — the monitor was quit or crashed mid-compaction —
    used to keep its row rendering as ``compacting`` forever, and since
    the Compact action refuses any row not in ``_COMPACTABLE_STATUSES``,
    ``acquire_lock``'s stale-lock recovery could never be reached: the
    row was permanently unopenable and the compaction unfinishable. The
    two functions have to agree on what "locked" means.

    Deliberately read-only — a dead holder's lock is skipped but NOT
    unlinked. Reaping belongs in ``acquire_lock``, which does it under
    the acquisition guard; deleting from here (called every refresh, by
    every monitor) would race that.
    """
    d = _compact_dir()
    if not d.is_dir():
        return set()
    out: set[str] = set()
    for p in d.glob("*.json"):
        try:
            holder = int(json.loads(p.read_text()).get("monitor_pid", 0))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            # Unreadable/!malformed lock: treat as held, matching
            # acquire_lock's "bail rather than guess" stance.
            out.add(p.stem)
            continue
        if _alive(holder):
            out.add(p.stem)
    return out
