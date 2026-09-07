"""Read-only backfill queries served to SecondBrain (INFRA §10.4).

The connector answers SB's pull queries from Multiverse's file substrate, reusing the
**same session/inbox data layer** the monitor and inter-agent tools use, so Claude,
Codex, and pi agents share one SecondBrain-facing roster.

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
    RUNTIME_CLAUDE,
    RUNTIME_CODEX,
    RUNTIME_PI,
    current_host,
    list_named_agents_in_cwd,
    tmux_session_exists,
)


def _supported_rows(cwd: str) -> list:
    """Named rows exposed through SecondBrain."""
    return [
        row for row in list_named_agents_in_cwd(cwd)
        if row.runtime in (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_PI)
    ]


def _iso(ts: float) -> str:
    """A float epoch (``status_ts``) as an RFC3339 UTC string, or "" if unset."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _corrected_status(row) -> str:  # noqa: ANN001 - DashboardRow
    """The dashboard's displayed status.

    Lifecycle states (``ended`` / ``retired`` / ``retiring``) are left alone: they are
    non-running by definition, so "its tmux is gone" says nothing new. A ``retiring``
    row is a dormant transcript reserved for context generation; rewriting it to
    ``ended`` would hide that reservation from every SB read path at once.

    A live-looking row whose tmux session is gone **on this host** is reported ``ended`` —
    the monitor's own liveness correction. It stays host-aware (``s.host``): a session
    alive on another host in a shared ``MULTIVERSE_ROOT`` deployment is invisible to local
    tmux by construction and must NOT be falsely reported dead.
    """
    s = row.session
    if (
        s is not None
        and row.status not in ("ended", "retired", "retiring")
        and (not s.host or s.host == current_host())
        and not tmux_session_exists(s.tmux_name)
    ):
        return "ended"
    return row.status


def _tokens(row) -> int | str | None:  # noqa: ANN001 - DashboardRow
    """The dashboard's Tokens cell: LIVE jsonl tokens + structural overhead — what the
    Claude session would actually cost to resume right now (reflects any past
    compaction). Codex does not expose an equivalent count, so it uses the monitor's
    neutral ``"-"`` placeholder. None means a Claude transcript is not available yet."""
    if row.runtime != RUNTIME_CLAUDE:
        return "-"
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
    native_id = row.session_id or uuid
    return {
        "id": (
            row.session.id
            if row.session is not None
            else (native_id[:8] if native_id else "")
        ),
        "name": row.name,
        "status": _corrected_status(row),
        "tokens": _tokens(row),
        "inbox": pending_inbox_count(row.cwd, row.name),
        "build": (
            "on"
            if (row.runtime == RUNTIME_CLAUDE and uuid and build_enabled(uuid))
            else "off"
        ),
        "updated": _iso(row.status_ts),
        "preview": row.preview,
    }


def dashboard(cwd: str) -> list[dict]:
    """The full monitor dashboard for a workspace (SB ``get_dashboard``, DESIGN §4d).

    Claude, Codex, and pi rows from the agent registry (ghost/folded rows
    excluded), with the monitor's per-row status corrections applied. Reads the
    file substrate directly, so it works whether or not a monitor TUI is live;
    an empty workspace yields ``[]``.
    """
    cwd_abs = str(Path(cwd).expanduser().resolve())
    return [_dashboard_row(r) for r in _supported_rows(cwd_abs)]


def list_agents(cwd: str) -> list[dict]:
    """The workspace's per-agent dashboard (ghosts/forks excluded, like the agent registry)."""
    cwd_abs = str(Path(cwd).expanduser().resolve())
    return [_agent_row(r) for r in _supported_rows(cwd_abs)]


def get_status(cwd: str, agent: str) -> dict | None:
    """One agent's row (``status`` / ``inbox`` / ``last_updated``), or None if not found."""
    cwd_abs = str(Path(cwd).expanduser().resolve())
    for r in _supported_rows(cwd_abs):
        if r.name == agent:
            return {k: v for k, v in _agent_row(r).items() if k != "name"}
    return None


def list_workspaces(granted_workspaces: list[str]) -> list[dict]:
    """The workspaces this device exposes — ``{cwd, label}`` each (device-scoped)."""
    return [{"cwd": w, "label": Path(w).name or w} for w in granted_workspaces]


def peek_agent(cwd: str, agent_name: str) -> tuple[str, dict]:
    """Capture-pane snapshot of an agent's live tmux screen (SB ``peek_agent``).

    Returns a discriminated tuple ``(kind, payload)`` for the caller to translate
    into a ``QueryResult``:

    * ``("ok", {screen, status})`` — clean-text pane + current agent status.
    * ``("not_found", {message})`` — no agent by that name in the workspace.
    * ``("not_running", {message})`` — dormant/ended/no-tmux; there IS no screen.
    * ``("locked", {message})`` — session is compacting; the pane is torn down.
    * ``("deliver_failed", {message})`` — tmux subprocess failure (rare).

    Contract with the SB ``toolsmith`` agent (2026-08-09):
      * Read-only, permission-exempt (Query, not Action).
      * ``capture-pane -p`` output is CLEAN plain text — we ``rstrip`` each line
        + strip trailing blank lines to remove tmux's pane-width padding, so SB
        gets tight rendered content ready to drop in a fenced block.
      * ``starting`` returns the pane as-is (the startup output IS what it's
        doing right now — we don't wait for readiness).
      * ``waiting`` is the highest-value case: the pane shows the exact
        AskUserQuestion modal / permission prompt live, so SB can read the
        question text + options directly.
    """
    from .compact_state import is_locked  # local import — compact_state pulls textual
    from .sb_actions import capture_pane   # local import — sb_actions has TUI deps
    from .sessions import find_agent_by_name

    target = find_agent_by_name(cwd, agent_name)
    if target is None or target.runtime not in (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_PI):
        return "not_found", {"message": f"no agent named '{agent_name}' in this workspace"}

    status = _corrected_status(target)

    if status in ("dormant", "ended", "retired", "retiring"):
        return "not_running", {"message": f"'{agent_name}' is {status}"}

    if target.claude_uuid and is_locked(target.claude_uuid):
        return "locked", {"message": f"'{agent_name}' is compacting; screen unavailable"}

    tmux_name = target.session.tmux_name if target.session else None
    if not tmux_name or not tmux_session_exists(tmux_name):
        return "not_running", {"message": f"'{agent_name}' has no live tmux session"}

    ok, out = capture_pane(tmux_name)
    if not ok:
        return "deliver_failed", {"message": f"capture-pane failed: {out}"}

    # Strip tmux's pane-width padding: per-line rstrip + drop trailing blank lines.
    lines = [ln.rstrip() for ln in out.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    screen = "\n".join(lines)
    return "ok", {"screen": screen, "status": status}
