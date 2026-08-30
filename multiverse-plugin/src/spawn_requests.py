"""Agent-initiated subagent spawn requests.

The ``spawn_agent`` MCP tool writes one of these when an agent wants to
start a new subagent; the monitor's watcher reads it, pops a naming widget,
and writes a resolution. The tool polls for the resolution and then calls
``create_session`` with the user-approved name.

On-disk layout (under ``~/.multiverse/spawn_requests/``):

  <req-id>.json           — the request (written atomically by the tool)
  <req-id>.resolved.json  — the resolution (written atomically, O_EXCL)

Both live in the same directory so the monitor and the tool only have to
watch one place. Resolutions are ``O_EXCL``-atomic so two monitors racing
on the same request can't double-resolve — first writer wins, losers peek
at the resolved file to learn the actual outcome.

Requests have no timeout. The tool cleans up both files on return; if the
caller is hard-killed before its ``finally`` runs, ``list_pending_requests``
deletes the orphan once the caller's tmux session is gone.
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
from .inbox import MULTIVERSE_DIR


SPAWN_REQUESTS_DIR = MULTIVERSE_DIR / "spawn_requests"


def _caller_alive(caller_tmux_session: str) -> bool:
    """True if the request's caller is still running (its tmux session
    exists). A spawn request has no timeout — it waits until the user
    resolves it — so orphan requests (from a caller that was hard-killed
    without cleaning up) are reaped by this liveness check instead. On any
    uncertainty (no session name recorded, tmux missing/erroring) we return
    True so a legitimate pending request is never dropped by mistake.
    """
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


def _notifies_secondbrain(request: dict) -> bool:
    """Pi spawn activity stays outside the unsupported SecondBrain seam."""
    return request.get("caller_runtime", "claude") in ("claude", "codex")


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
    # Firsthand: record supported-runtime requests for the SecondBrain
    # connector (no-op unless this workspace connected before). Pi explicitly
    # has no SecondBrain integration. Never break the spawn flow.
    if _notifies_secondbrain(data):
        sb_notify.record_spawn_request(data)
    return path


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
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("cwd") != cwd:
            continue
        if not _caller_alive(data.get("caller_tmux_session", "")):
            orphan_paths = (
                path,
                path.with_name(f"{path.stem}.resolved.json"),
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
        if _notifies_secondbrain(req):
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
