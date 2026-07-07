"""Firsthand recording of Multiverse spawn events into the SecondBrain pending queue.

This is the "connect Multiverse directly to SecondBrain" seam on the *production* side.
Instead of the connector polling ``~/.multiverse/spawn_requests/`` and racing the spawn
tool's cleanup (which deletes a rejection's resolution within ~250ms), Multiverse's own
spawn choke-points call in here **at the moment the event happens**, where the data is
authoritative and can't be missed:

  - ``spawn_requests.write_spawn_request`` → :func:`record_spawn_request`
  - ``spawn_requests.write_resolution``    → :func:`record_spawn_decision`

Each drops a **durable event record** into ``~/.multiverse/secondbrain/pending/``. The
connector daemon (:mod:`src.sb_connector`) consumes those, assigns the monotonic ``seq``,
and relays to SB with its durable-buffer/replay machinery.

**Gate (per the user):** a record is written only when this **workspace has connected to
SecondBrain before** — i.e. its ``workspace_id`` is in the connector's persisted ``seeded``
set. A never-connected workspace records nothing, so no stale queue ever accumulates.

Kept **dependency-light on purpose** (stdlib + :mod:`.paths` + :mod:`.sb_events`): it is
imported by ``spawn_requests`` — which the MCP server and monitor import — so it must not
pull in ``websockets``/``cryptography`` or create an import cycle. All failures are
swallowed: recording an SB event must never break the core spawn flow.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from . import sb_events
from .paths import multiverse_dir

#: Drop pending records older than this if never consumed (device seeded but connector
#: never ran). Spawn events are rare, so this is just a safety bound.
_MAX_PENDING_AGE_S = 7 * 24 * 3600


# ---- paths (lightweight; mirror sb_connector's layout without importing it) ----------


def _sb_dir() -> Path:
    return multiverse_dir() / "secondbrain"


def pending_dir() -> Path:
    return _sb_dir() / "pending"


def _sidecar_path() -> Path:
    return _sb_dir() / "device.key.json"


def _state_path() -> Path:
    return _sb_dir() / "state.json"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve(cwd: str) -> str:
    return str(Path(cwd).expanduser().resolve())


def _label(cwd: str) -> str:
    return Path(cwd).name or cwd


# ---- the "connected before" gate -----------------------------------------------------


def _device_id() -> str | None:
    data = _read_json(_sidecar_path())
    return data.get("device_id") if data else None


def _seeded() -> set[str]:
    data = _read_json(_state_path()) or {}
    return set(data.get("seeded", []))


def _recordable_workspace_id(cwd: str) -> str | None:
    """``workspace_id`` if this device is enrolled AND this workspace has connected to SB
    before (is seeded); otherwise ``None`` — meaning: do not record."""
    device_id = _device_id()
    if not device_id:
        return None
    workspace_id = f"{device_id}:{_resolve(cwd)}"
    return workspace_id if workspace_id in _seeded() else None


# ---- writing pending records ---------------------------------------------------------


def _safe(event_id: str) -> str:
    return event_id.replace(":", "-").replace("/", "_")


def _write_record(
    event_id: str, kind: str, workspace_id: str, cwd: str, agent: str, payload: dict
) -> None:
    pd = pending_dir()
    pd.mkdir(parents=True, exist_ok=True)
    path = pd / f"{_safe(event_id)}.json"
    if path.exists():
        return  # deterministic name per event → idempotent if produced twice
    record = {
        "event_id": event_id,
        "kind": kind,
        "workspace_id": workspace_id,
        "workspace_label": _label(cwd),
        "agent": agent,
        "payload": payload,
        "ts": _now_iso(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)
    _prune_old()


def _prune_old() -> None:
    pd = pending_dir()
    if not pd.is_dir():
        return
    cutoff = time.time() - _MAX_PENDING_AGE_S
    for p in pd.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def record_spawn_request(req: dict) -> None:
    """Record an ``agent.spawn_request`` event at creation time (``write_spawn_request``)."""
    try:
        wsid = _recordable_workspace_id(req.get("cwd", ""))
        if wsid is None:
            return
        agent, payload = sb_events.spawn_request_event_fields(req)
        _write_record(
            f"spawnreq:{req.get('id', '')}", "agent.spawn_request",
            wsid, req.get("cwd", ""), agent, payload,
        )
    except Exception:  # noqa: BLE001 - never break the spawn flow
        pass


def record_spawn_decision(req: dict, status: str, name: str | None) -> None:
    """Record a ``user.spawn_decision`` event at resolution time (``write_resolution``).

    ``req`` is the original request dict (read from its file by the caller, which still
    exists at resolution time). A ``timeout``/non-decision status records nothing.
    """
    try:
        wsid = _recordable_workspace_id(req.get("cwd", ""))
        if wsid is None:
            return
        composed = sb_events.spawn_decision_event_fields(req, status, name)
        if composed is None:
            return  # timeout / not a user decision
        agent, payload = composed
        _write_record(
            f"spawndec:{req.get('id', '')}", "user.spawn_decision",
            wsid, req.get("cwd", ""), agent, payload,
        )
    except Exception:  # noqa: BLE001
        pass


# ---- reading / clearing (consumer side: the connector) -------------------------------


def list_pending() -> list[dict]:
    """All pending event records, oldest first (by ``ts``). Each dict has an added
    ``_path`` key for :func:`delete_pending`."""
    pd = pending_dir()
    if not pd.is_dir():
        return []
    out: list[dict] = []
    for p in pd.glob("*.json"):
        if p.name.endswith(".json.tmp"):
            continue
        rec = _read_json(p)
        if rec:
            rec["_path"] = str(p)
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", ""))
    return out


def delete_pending(record: dict) -> None:
    try:
        Path(record["_path"]).unlink()
    except (OSError, KeyError):
        pass
