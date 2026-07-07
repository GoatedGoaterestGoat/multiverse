"""Read-only backfill queries served to SecondBrain (INFRA §10.4).

The connector answers SB's pull queries from Multiverse's file substrate, reusing the
**same session/inbox data layer** the monitor and the inter-agent tools use — so SB sees
the same roster (and the same statuses) that agents see via ``list_agents``.

Phase-2 step 1 implements the workspace-status queries:

* ``list_workspaces`` — the workspaces this device exposes (device-scoped).
* ``list_agents``     — the workspace's dashboard, per agent: ``name``, ``status``,
                        ``inbox`` (pending message count, not content), ``last_updated``.
* ``get_status``      — one agent's row from the same source.

``get_transcript`` (the recall path) is a later step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .auto_build import is_enabled as build_enabled
from .full_jsonl import live_stats_cached, structural_tokens_cached
from .inbox import pending_inbox_count
from .sessions import (
    current_host,
    list_named_agents_in_cwd,
    tmux_session_exists,
)


def _iso(ts: float) -> str:
    """A float epoch (``status_ts``) as an RFC3339 UTC string, or "" if unset."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _corrected_status(row) -> str:  # noqa: ANN001 - DashboardRow
    """The dashboard's displayed status.

    A live-looking row whose tmux session is gone **on this host** is reported ``ended`` —
    the monitor's own liveness correction. It stays host-aware (``s.host``): a session
    alive on another host in a shared ``MULTIVERSE_ROOT`` deployment is invisible to local
    tmux by construction and must NOT be falsely reported dead.
    """
    s = row.session
    if (
        s is not None
        and row.status not in ("ended",)
        and (not s.host or s.host == current_host())
        and not tmux_session_exists(s.tmux_name)
    ):
        return "ended"
    return row.status


def _tokens(row) -> int | None:  # noqa: ANN001 - DashboardRow
    """The dashboard's Tokens cell: LIVE jsonl tokens + structural overhead — what the
    session would actually cost to resume right now (reflects any past compaction).
    Both halves are mtime-cached, so repeated queries are cheap. None if no transcript yet."""
    uuid = row.claude_uuid
    if not uuid:
        return None
    live_toks, _ = live_stats_cached(uuid, row.cwd)
    return live_toks + structural_tokens_cached(uuid, row.cwd)


def _agent_row(row) -> dict:  # noqa: ANN001 - DashboardRow
    return {
        "name": row.name,
        "status": _corrected_status(row),
        "inbox": pending_inbox_count(row.cwd, row.name),  # count only, not content
        "tokens": _tokens(row),
        "last_updated": _iso(row.status_ts),
    }


def _dashboard_row(row) -> dict:  # noqa: ANN001 - DashboardRow
    """One session row in the full dashboard shape SB's ``get_dashboard`` expects
    (DESIGN §4d): a 1:1 lift of the monitor's columns."""
    uuid = row.claude_uuid
    return {
        "id": row.session.id if row.session is not None else (uuid[:8] if uuid else ""),
        "name": row.name,
        "status": _corrected_status(row),
        "tokens": _tokens(row),
        "inbox": pending_inbox_count(row.cwd, row.name),
        "build": "on" if (uuid and build_enabled(uuid)) else "off",
        "updated": _iso(row.status_ts),
        "preview": row.preview,
    }


def dashboard(cwd: str) -> list[dict]:
    """The full monitor dashboard for a workspace (SB ``get_dashboard``, DESIGN §4d).

    Same roster as the agent registry (ghost/folded rows excluded), with the monitor's
    per-row status corrections applied. Reads the file substrate directly, so it works
    whether or not a monitor TUI is live; an empty workspace yields ``[]``.
    """
    cwd_abs = str(Path(cwd).expanduser().resolve())
    return [_dashboard_row(r) for r in list_named_agents_in_cwd(cwd_abs)]


def list_agents(cwd: str) -> list[dict]:
    """The workspace's per-agent dashboard (ghosts/forks excluded, like the agent registry)."""
    cwd_abs = str(Path(cwd).expanduser().resolve())
    return [_agent_row(r) for r in list_named_agents_in_cwd(cwd_abs)]


def get_status(cwd: str, agent: str) -> dict | None:
    """One agent's row (``status`` / ``inbox`` / ``last_updated``), or None if not found."""
    cwd_abs = str(Path(cwd).expanduser().resolve())
    for r in list_named_agents_in_cwd(cwd_abs):
        if r.name == agent:
            return {k: v for k, v in _agent_row(r).items() if k != "name"}
    return None


def list_workspaces(granted_workspaces: list[str]) -> list[dict]:
    """The workspaces this device exposes — ``{cwd, label}`` each (device-scoped)."""
    return [{"cwd": w, "label": Path(w).name or w} for w in granted_workspaces]
