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

Stale requests (older than their declared timeout) are ignored by readers;
the tool cleans up both files on return. If the tool crashes before
cleanup, stale files persist but are skipped by ``list_pending_requests``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from .inbox import MULTIVERSE_DIR


SPAWN_REQUESTS_DIR = MULTIVERSE_DIR / "spawn_requests"

_DEFAULT_POLL_INTERVAL = 0.25


def new_request_id() -> str:
    return str(uuid.uuid4())


def request_path(req_id: str) -> Path:
    return SPAWN_REQUESTS_DIR / f"{req_id}.json"


def resolution_path(req_id: str) -> Path:
    return SPAWN_REQUESTS_DIR / f"{req_id}.resolved.json"


def write_spawn_request(
    req_id: str,
    from_name: str,
    cwd: str,
    instruction: str,
    default_name: str,
    caller_tmux_session: str,
    timeout_seconds: int,
) -> Path:
    """Atomically (tmp+rename) drop a spawn request into the watch dir."""
    SPAWN_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = request_path(req_id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = {
        "id": req_id,
        "from": from_name,
        "cwd": cwd,
        "instruction": instruction,
        "default_name": default_name,
        "caller_tmux_session": caller_tmux_session,
        "timeout_seconds": int(timeout_seconds),
        "ts": time.time(),
    }
    tmp.write_text(json.dumps(data))
    tmp.rename(path)
    return path


def list_pending_requests(cwd: str) -> list[dict]:
    """Unresolved, non-stale requests matching ``cwd``, sorted oldest first.

    The monitor calls this every tick. Skips:
      - resolution files (``*.resolved.json``) and temp files (``*.tmp``)
      - requests from other cwds
      - requests older than their declared timeout
      - requests that already have a resolution
    """
    if not SPAWN_REQUESTS_DIR.is_dir():
        return []

    now = time.time()
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
        timeout = int(data.get("timeout_seconds") or 300)
        ts = float(data.get("ts") or 0)
        if now - ts > timeout:
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
        "ts": time.time(),
    }
    content = json.dumps(data).encode()
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    return True


def wait_for_resolution(
    req_id: str,
    timeout_seconds: float,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> Optional[dict]:
    """Block until a resolution appears or ``timeout_seconds`` elapses."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        res = read_resolution(req_id)
        if res is not None:
            return res
        time.sleep(poll_interval)
    return None


def cleanup_request(req_id: str) -> None:
    """Delete both the request and resolution files. Swallows errors."""
    for p in (request_path(req_id), resolution_path(req_id)):
        try:
            p.unlink()
        except OSError:
            pass
