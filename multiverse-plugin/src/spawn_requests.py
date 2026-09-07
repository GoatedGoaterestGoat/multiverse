"""Agent-initiated subagent spawn requests.

The ``spawn_agent`` MCP tool writes one of these when an agent wants to
start a new subagent; the monitor's watcher reads it, pops a naming widget,
and writes a resolution. The tool polls for the resolution and then calls
``create_session`` with the user-approved name.

On-disk layout (under ``~/.multiverse/spawn_requests/``):

  <req-id>.json           — the request (written atomically by the tool)
  <req-id>.resolved.json  — the resolution (written atomically, O_EXCL)
  <req-id>.outcome.json   — what the tool actually did with it

Both live in the same directory so the monitor and the tool only have to
watch one place. Resolutions are ``O_EXCL``-atomic so two monitors racing
on the same request can't double-resolve — first writer wins, losers peek
at the resolved file to learn the actual outcome.

Requests have no timeout. The tool cleans up the request and resolution on
return; if the caller is hard-killed before its ``finally`` runs,
``list_pending_requests`` deletes the orphan once the caller's tmux session
is gone.

The outcome record deliberately outlives that cleanup. An approved request
is not a created agent — ``create_session`` can still fail, or the caller
can die between the two — and the monitor has no other way to tell the
difference, since a request file disappears either way. So the tool says
which happened, and under what name the session was actually created (the
user can rename it in the widget, and that name is the one that matters).
Whoever consumes an outcome deletes it; ``list_pending_requests`` sweeps
any that no monitor was around to read.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from . import sb_notify
from .atomic_files import atomic_create_json, atomic_write_json
from .hosts import current_host
from .inbox import MULTIVERSE_DIR


SPAWN_REQUESTS_DIR = MULTIVERSE_DIR / "spawn_requests"


def _caller_alive(caller_tmux_session: str, caller_host: str = "") -> bool:
    """True if the request's caller is still running (its tmux session
    exists). A spawn request has no timeout — it waits until the user
    resolves it — so orphan requests (from a caller that was hard-killed
    without cleaning up) are reaped by this liveness check instead. On any
    uncertainty (no session name recorded, tmux missing/erroring) we return
    True so a legitimate pending request is never dropped by mistake.

    ``tmux has-session`` only ever sees this host's tmux server, so a caller
    on another host in a shared-``MULTIVERSE_ROOT`` deployment always looks
    dead from here — and being "reaped" means its request file is DELETED
    while the caller sits blocked on it forever, waiting for an answer no
    monitor will ever be asked for. A recorded host that isn't ours is
    therefore uncertainty, not death, and takes the same benefit of the
    doubt as an unreadable tmux.
    """
    if caller_host and caller_host != current_host():
        return True
    if not caller_tmux_session:
        return True
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", caller_tmux_session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def new_request_id() -> str:
    return str(uuid.uuid4())


def request_path(req_id: str) -> Path:
    return SPAWN_REQUESTS_DIR / f"{req_id}.json"


def resolution_path(req_id: str) -> Path:
    return SPAWN_REQUESTS_DIR / f"{req_id}.resolved.json"


def outcome_path(req_id: str) -> Path:
    return SPAWN_REQUESTS_DIR / f"{req_id}.outcome.json"


def write_spawn_request(
    req_id: str,
    from_name: str,
    cwd: str,
    instruction: str,
    default_name: str,
    caller_tmux_session: str,
    caller_runtime: str = "claude",
) -> Path:
    """Atomically (tmp+rename) drop a spawn request into the watch dir."""
    SPAWN_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = request_path(req_id)
    data = {
        "id": req_id,
        "from": from_name,
        "cwd": cwd,
        "instruction": instruction,
        "default_name": default_name,
        "caller_tmux_session": caller_tmux_session,
        "caller_runtime": caller_runtime,
        "ts": time.time(),
    }
    atomic_write_json(path, data)
    # Firsthand: record the request for the SecondBrain connector (no-op unless
    # this workspace connected before). Never break the spawn flow.
    sb_notify.record_spawn_request(data)
    return path


# How long an unread outcome record sticks around. It only exists to tell
# a monitor what happened; with no monitor running there is nobody to tell,
# and the group filing it would have driven is long past mattering.
_OUTCOME_TTL_SECONDS = 3600.0


def _sweep_stale_outcome(path: Path, cwd: str) -> None:
    """Delete an outcome nobody was around to read. Swallows errors."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if data.get("cwd") not in (None, cwd):
        return
    if time.time() - float(data.get("ts") or 0) <= _OUTCOME_TTL_SECONDS:
        return
    try:
        path.unlink()
    except OSError:
        pass


def list_pending_requests(cwd: str) -> list[dict]:
    """Unresolved requests matching ``cwd``, sorted oldest first.

    The monitor calls this every tick. Requests have no timeout — they stay
    pending until the user resolves them. Skips:
      - resolution files (``*.resolved.json``) and temp files (``*.tmp``)
      - requests from other cwds
      - requests whose caller is no longer alive (orphans are deleted because
        the waiting tool was hard-killed without cleaning up)
      - requests that already have a resolution
    """
    if not SPAWN_REQUESTS_DIR.is_dir():
        return []

    out: list[dict] = []
    for path in SPAWN_REQUESTS_DIR.iterdir():
        name = path.name
        if not name.endswith(".json"):
            continue
        if name.endswith(".resolved.json") or name.endswith(".tmp"):
            continue
        if name.endswith(".outcome.json"):
            _sweep_stale_outcome(path, cwd)
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("cwd") != cwd:
            continue
        if not _caller_alive(
            data.get("caller_tmux_session", ""), data.get("caller_host", "")
        ):
            orphan_paths = (
                path,
                path.with_name(f"{path.stem}.resolved.json"),
                path.with_name(f"{path.stem}.outcome.json"),
            )
            for orphan_path in orphan_paths:
                try:
                    orphan_path.unlink()
                except OSError:
                    pass
            continue
        if resolution_path(data.get("id", "")).is_file():
            continue
        out.append(data)
    out.sort(key=lambda d: d.get("ts", 0))
    return out


def write_outcome(
    req_id: str,
    *,
    cwd: str,
    created: bool,
    name: Optional[str] = None,
    error: Optional[str] = None,
) -> Path:
    """Record what became of an approved request.

    ``name`` is the name the session was actually created under, which is
    what the monitor files — not the default the requesting agent asked
    for, and not necessarily the one it expected, because the user can
    type a different one into the approval widget.
    """
    SPAWN_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = outcome_path(req_id)
    atomic_write_json(
        path,
        {
            "id": req_id,
            "cwd": cwd,
            "created": bool(created),
            "name": name,
            "error": error,
            "ts": time.time(),
        },
    )
    return path


def read_outcome(req_id: str) -> Optional[dict]:
    path = outcome_path(req_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def clear_outcome(req_id: str) -> None:
    """Drop an outcome once it has been acted on. Swallows errors."""
    try:
        outcome_path(req_id).unlink()
    except OSError:
        pass


def read_resolution(req_id: str) -> Optional[dict]:
    path = resolution_path(req_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_resolution(
    req_id: str,
    status: str,
    name: Optional[str] = None,
    reason: Optional[str] = None,
    runtime: Optional[str] = None,
) -> bool:
    """O_EXCL-atomic resolution write. Returns ``False`` if another writer won.

    Losers should call ``read_resolution`` to learn what the winner wrote —
    useful for telling a user "another monitor already named this agent X".
    """
    SPAWN_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = resolution_path(req_id)
    data = {
        "status": status,
        "name": name,
        "reason": reason,
        "runtime": runtime,
        "ts": time.time(),
    }
    try:
        created = atomic_create_json(path, data)
    except OSError:
        return False
    if not created:
        return False
    # Firsthand: record the user's decision for the SecondBrain connector. We won the
    # O_EXCL race (authoritative resolver), and the request file still exists (the spawn
    # tool cleans up only after reading this resolution), so read it for the agent/name
    # details. This is exactly what a poll-based observer would miss on a fast rejection.
    try:
        req = json.loads(request_path(req_id).read_text())
        sb_notify.record_spawn_decision(req, status, name)
    except Exception:  # noqa: BLE001 - never break the resolution write
        pass
    return True


def cleanup_request(req_id: str) -> None:
    """Delete both the request and resolution files. Swallows errors."""
    for p in (request_path(req_id), resolution_path(req_id)):
        try:
            p.unlink()
        except OSError:
            pass
