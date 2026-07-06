"""Data layer for multiverse session state.

Pure read-only helpers shared by the monitor TUI and the MCP server. No
Textual or subprocess dependencies — anything that *spawns* (create_session,
kill_session) lives in monitor.py instead.

The data sources are:
  ~/.multiverse/sessions/<mv-id>/  — per-session state written by the monitor
                                     and the SessionStart hook
  ~/.claude/projects/<encoded-cwd>/<claude-uuid>.jsonl
                                   — claude's own session transcripts
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import forks
from .compact_state import list_locked
from .paths import claude_projects_dir, multiverse_dir


MULTIVERSE_DIR = multiverse_dir()
STATE_DIR = MULTIVERSE_DIR / "sessions"
CLAUDE_PROJECTS_DIR = claude_projects_dir()
TMUX_PREFIX = "multiverse-"


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


# Suffix written by build_dashboard_rows onto ghost row display names,
# matching every format _format_fold_time produces (HH:MM, "Mon DD",
# YYYY-MM-DD). Strip it on read so a JSONL whose custom-title was
# corrupted by a previous resume of a ghost row (which passed the
# already-suffixed display name to `claude --name`) still resolves to
# the underlying clean title — and dashboard renders don't double-suffix.
_FOLD_SUFFIX_RE = re.compile(r" \(folded [^)]+\)$")


def strip_fold_suffix(name: Optional[str]) -> Optional[str]:
    """Remove trailing ``(folded …)`` markers, idempotently."""
    if name is None:
        return None
    while True:
        new = _FOLD_SUFFIX_RE.sub("", name)
        if new == name:
            return name
        name = new


def _extract_user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                return str(block.get("text", ""))
    return ""


# mtime-keyed cache of (title, cwd, preview) per JSONL path. Dashboard refresh
# hits this every second; re-reading unchanged files would be wasteful.
_METADATA_CACHE: dict[str, tuple[float, dict]] = {}


def _read_session_metadata(path: Path) -> dict:
    """Extract title (last ``custom-title``), cwd (first ``cwd`` field), and
    a preview (first non-system user message) from a claude JSONL.

    Returns ``{"title": Optional[str], "cwd": Optional[str], "preview": str}``.
    Results are cached by mtime, so scanning dormant sessions every second is
    cheap once warm.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"title": None, "cwd": None, "preview": ""}

    cached = _METADATA_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]

    title: Optional[str] = None
    cwd: Optional[str] = None
    preview: Optional[str] = None
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = raw.get("type")
                if typ == "custom-title":
                    ct = raw.get("customTitle")
                    if ct:
                        title = strip_fold_suffix(str(ct))
                if cwd is None:
                    c = raw.get("cwd")
                    if c:
                        cwd = str(c)
                if preview is None and typ == "user":
                    msg = raw.get("message") or {}
                    if msg.get("role") == "user":
                        text = _extract_user_text(msg.get("content")).strip()
                        if text and not text.startswith("<"):
                            text = text.replace("\n", " ")
                            if len(text) > 80:
                                text = text[:79] + "…"
                            preview = text
    except OSError:
        pass

    result = {
        "title": title,
        "cwd": cwd,
        "preview": preview or "(no user message)",
    }
    _METADATA_CACHE[str(path)] = (mtime, result)
    return result


def read_claude_session_name(path: Path) -> Optional[str]:
    """Return the user-assigned custom title of a claude session JSONL.

    Matches claude-code's ``extractLastJsonStringField`` behavior by returning
    the last ``custom-title`` entry. Does NOT fall back to ``ai-title`` — per
    feature spec, only user-set names count.
    """
    return _read_session_metadata(path).get("title")


def list_claude_sessions_in(cwd: str) -> list[dict]:
    """Return claude sessions under ``cwd``, newest first.

    Each dict has: uuid, preview, mtime, size, name (from custom-title, or None).
    """
    cwd = cwd.strip()
    if not cwd:
        return []
    project_dir = CLAUDE_PROJECTS_DIR / _encode_cwd(cwd)
    if not project_dir.is_dir():
        return []
    out: list[dict] = []
    for path in project_dir.glob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        meta = _read_session_metadata(path)
        out.append(
            {
                "uuid": path.stem,
                "preview": meta.get("preview") or "(no user message)",
                "mtime": st.st_mtime,
                "size": st.st_size,
                "name": meta.get("title"),
            }
        )
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def existing_names_in_cwd(cwd: str) -> set[str]:
    """Return the set of multiverse session names already assigned in ``cwd``.

    Includes ended sessions — names are permanent per session since any
    session can be resumed later and must re-attach with the same name.
    """
    out: set[str] = set()
    if not STATE_DIR.exists():
        return out
    try:
        cwd_abs = str(Path(cwd).expanduser().resolve())
    except OSError:
        return out
    for entry in STATE_DIR.iterdir():
        if not entry.is_dir():
            continue
        meta = _read_json(entry / "meta.json")
        if not meta:
            continue
        if meta.get("cwd") != cwd_abs:
            continue
        name = meta.get("name")
        if name:
            out.add(str(name))
    return out


@dataclass
class Session:
    id: str
    cwd: str
    created_at: float
    status: str = "unknown"
    status_ts: float = 0.0
    resumed_from: Optional[str] = None
    name: Optional[str] = None
    claude_uuid: Optional[str] = None
    # claude UUIDs that this multiverse session has superseded via
    # context_fold. Each entry: {"uuid": str, "folded_at": float}.
    superseded_uuids: list[dict] = field(default_factory=list)
    # Hostname that created this session (see current_host()). None for
    # sessions created before this field existed. Used to tell whether a
    # local "tmux doesn't have this session" observation is meaningful —
    # it isn't, when MULTIVERSE_ROOT is shared network storage and the
    # session was actually spawned on a different host's tmux server.
    host: Optional[str] = None

    @property
    def tmux_name(self) -> str:
        return f"{TMUX_PREFIX}{self.id}"

    @property
    def dir(self) -> Path:
        return STATE_DIR / self.id

    @property
    def status_path(self) -> Path:
        return self.dir / "status.json"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def effective_claude_uuid(self) -> Optional[str]:
        """Best-effort live claude UUID.

        Prefers the value captured by the SessionStart hook; falls back to
        ``resumed_from`` (valid for resumed sessions, which keep their UUID
        without ``--fork-session``).
        """
        return self.claude_uuid or self.resumed_from


@dataclass
class DashboardRow:
    """One row in the monitor's session table.

    Represents either a multiverse-wrapped session (``session is not None``)
    or a dormant named claude session on disk (``session is None``, populated
    ``claude_uuid``).
    """

    name: str
    cwd: str
    status: str
    status_ts: float
    session: Optional[Session] = None
    claude_uuid: Optional[str] = None
    preview: str = ""
    # True if this row represents a JSONL that was superseded by a
    # context_fold. Ghost rows display in the dashboard but are filtered
    # out of the inter-agent registry (list_agents/message_agent/ask_agent).
    is_ghost: bool = False
    folded_at: Optional[float] = None

    @property
    def is_dormant(self) -> bool:
        return self.session is None

    @property
    def is_live(self) -> bool:
        return self.session is not None and self.status not in ("ended",)

    @property
    def row_key(self) -> str:
        if self.session is not None:
            return f"mv:{self.session.id}"
        return f"cl:{self.claude_uuid}"


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _read_text_stripped(path: Path) -> Optional[str]:
    try:
        v = path.read_text().strip()
        return v or None
    except OSError:
        return None


def load_session(session_id: str) -> Optional[Session]:
    d = STATE_DIR / session_id
    meta = _read_json(d / "meta.json")
    if meta is None:
        return None
    raw_superseded = meta.get("superseded_uuids") or []
    superseded = [e for e in raw_superseded if isinstance(e, dict) and e.get("uuid")]
    s = Session(
        id=session_id,
        cwd=meta.get("cwd", "?"),
        created_at=meta.get("created_at", 0.0),
        resumed_from=meta.get("resumed_from"),
        name=meta.get("name"),
        claude_uuid=_read_text_stripped(d / "claude_uuid"),
        superseded_uuids=superseded,
        host=meta.get("host"),
    )
    st = _read_json(s.status_path)
    if st:
        s.status = st.get("status", "unknown")
        s.status_ts = float(st.get("ts", 0) or 0)
    return s


def list_sessions() -> list[Session]:
    if not STATE_DIR.exists():
        return []
    out: list[Session] = []
    for entry in sorted(STATE_DIR.iterdir()):
        if not entry.is_dir():
            continue
        s = load_session(entry.name)
        if s is not None:
            out.append(s)
    out.sort(key=lambda s: s.created_at)
    return out


def tmux_session_exists(name: str) -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


# Ranking for dashboard sort: lower = higher in the table.
_STATUS_SORT = {
    "compacting": 0,
    "working": 0,
    "waiting": 0,
    "listening": 1,
    "starting": 1,
    "idle": 1,
    "unknown": 2,
    "dormant": 3,
    "ended": 4,
}


def _format_fold_time(ts: float) -> str:
    """Format a fold timestamp for display in `(folded …)` suffixes.

    Today → ``HH:MM``; same year → ``MMM DD``; older → ``YYYY-MM-DD``.
    """
    if not ts:
        return "?"
    try:
        dt = datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return "?"
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%b %d")
    return dt.strftime("%Y-%m-%d")


def build_dashboard_rows(
    sessions: list[Session],
    cwd: str,
    hide_uuids: Optional[set[str]] = None,
) -> list[DashboardRow]:
    """Compute the dashboard row list for ``cwd``: named multiverse sessions
    in that cwd + dormant named claude sessions in that cwd.

    ``sessions`` is expected to already be filtered to ``cwd``.

    Deduplicates: a claude JSONL is considered "wrapped" (and skipped from
    the dormant list) if any multiverse session has it as ``claude_uuid`` or
    ``resumed_from``, or if it's listed in ``hide_uuids`` (used for in-flight
    ask-all fork JSONLs).
    """
    # Reap stale ask-fork registry entries (and any orphaned fork JSONLs)
    # before we read the registry below.
    forks.gc()

    rows: list[DashboardRow] = []

    wrapped_uuids: set[str] = set(hide_uuids or ())
    # Ask-fork JSONLs inherit the origin's custom-title, so without this
    # they'd surface as phantom dormant agents. The on-disk fork registry
    # covers forks spawned by ANY process (TUI ask-all AND agent-initiated
    # ask_agent) — hiding them from the dashboard and from list_agents.
    wrapped_uuids |= forks.active_uuids()
    for s in sessions:
        u = s.effective_claude_uuid
        if u:
            wrapped_uuids.add(u)

    # Map each superseded claude UUID to its fold timestamp. Used to render
    # those JSONLs as `Name (folded HH:MM)` ghost rows instead of plain
    # dormants, and to keep them out of the inter-agent registry.
    #
    # Two sources, in priority order:
    #   1. ``superseded_uuids`` on live multiverse sessions — populated at
    #      fold time (server.py's _record_superseded_uuid) and at resume
    #      time (create_session's self-heal). Cheap and authoritative when
    #      present.
    #   2. The synthetic ``model: "context-fold"`` markers embedded in
    #      every post-fold JSONL by the rewriter. Durable across container
    #      migrations / state-dir loss / monitor restarts on a clean
    #      MULTIVERSE_ROOT — the JSONL itself is the source of truth.
    #      mtime-cached so the per-tick cost is negligible after the
    #      initial scan.
    ghost_map: dict[str, float] = {}
    for s in sessions:
        for entry in s.superseded_uuids:
            uid = entry.get("uuid") if isinstance(entry, dict) else None
            if not uid:
                continue
            ts = entry.get("folded_at") if isinstance(entry, dict) else None
            try:
                ghost_map[uid] = float(ts or 0.0)
            except (TypeError, ValueError):
                ghost_map[uid] = 0.0

    project_dir = CLAUDE_PROJECTS_DIR / _encode_cwd(cwd)

    if project_dir.is_dir():
        for path in project_dir.glob("*.jsonl"):
            for entry in detect_fold_ancestors_cached(path):
                uid = entry.get("uuid") if isinstance(entry, dict) else None
                if not uid or uid in ghost_map:
                    continue
                ts = entry.get("folded_at") if isinstance(entry, dict) else None
                try:
                    ghost_map[uid] = float(ts or 0.0)
                except (TypeError, ValueError):
                    ghost_map[uid] = 0.0

    for s in sessions:
        if not s.name:
            continue
        # Look up the live session's JSONL preview if we know the claude UUID.
        preview = ""
        u = s.effective_claude_uuid
        if u and project_dir.is_dir():
            jsonl = project_dir / f"{u}.jsonl"
            if jsonl.exists():
                preview = _read_session_metadata(jsonl).get("preview") or ""
        rows.append(
            DashboardRow(
                name=s.name,
                cwd=s.cwd,
                status=s.status,
                status_ts=s.status_ts,
                session=s,
                claude_uuid=s.effective_claude_uuid,
                preview=preview,
            )
        )

    if project_dir.is_dir():
        for path in project_dir.glob("*.jsonl"):
            uuid = path.stem
            if uuid in wrapped_uuids:
                continue
            meta = _read_session_metadata(path)
            title = meta.get("title")
            if not title:
                continue
            recorded_cwd = meta.get("cwd")
            if recorded_cwd and recorded_cwd != cwd:
                continue
            try:
                st = path.stat()
            except OSError:
                continue

            ghost_ts = ghost_map.get(uuid)
            if ghost_ts is not None:
                display_name = f"{title} (folded {_format_fold_time(ghost_ts)})"
                status_ts = ghost_ts or st.st_mtime
            else:
                display_name = title
                status_ts = st.st_mtime

            rows.append(
                DashboardRow(
                    name=display_name,
                    cwd=recorded_cwd or cwd,
                    status="dormant",
                    status_ts=status_ts,
                    session=None,
                    claude_uuid=uuid,
                    preview=meta.get("preview") or "",
                    is_ghost=ghost_ts is not None,
                    folded_at=ghost_ts,
                )
            )

    # Override status to "compacting" for any row whose claude_uuid holds
    # an active compaction lock. Cross-monitor: every monitor reads from
    # ~/.multiverse/compacting/ on each refresh, so the locked rows render
    # uniformly across monitors and the action handlers refuse to start /
    # resume / kill them.
    locked = list_locked()
    if locked:
        for r in rows:
            if r.claude_uuid and r.claude_uuid in locked:
                r.status = "compacting"

    # Sort: non-ghosts first by status then recency, ghosts always last
    # (regardless of how recent the underlying fold was — they're stale
    # forks the user shouldn't have to scroll past).
    rows.sort(
        key=lambda r: (
            1 if r.is_ghost else 0,
            _STATUS_SORT.get(r.status, 9),
            -(r.status_ts or 0),
        )
    )
    return rows


def wrapped_claude_uuids(sessions: list[Session]) -> set[str]:
    """UUIDs of claude sessions already wrapped by a multiverse session.

    Used by the resume widget to hide live sessions from the picker.
    """
    out: set[str] = set()
    for s in sessions:
        u = s.effective_claude_uuid
        if u:
            out.add(u)
    return out


def list_named_agents_in_cwd(cwd: str) -> list[DashboardRow]:
    """Named multiverse + dormant claude sessions for ``cwd``.

    The agent registry seen by inter-agent tools (list_agents, ask_agent).
    Returns dashboard rows because the same dedup/scoping logic applies —
    but callers shouldn't care which kind backed each row.
    """
    cwd_abs = str(Path(cwd).expanduser().resolve())
    sessions = [s for s in list_sessions() if s.cwd == cwd_abs]
    rows = build_dashboard_rows(sessions, cwd=cwd_abs)
    # Ghosts (pre-fold JSONLs) are display-only — they must not be reachable
    # by list_agents / message_agent / ask_agent.
    return [r for r in rows if not r.is_ghost]


def find_agent_by_name(cwd: str, name: str) -> Optional[DashboardRow]:
    """Look up a single agent by name within ``cwd``."""
    for row in list_named_agents_in_cwd(cwd):
        if row.name == name:
            return row
    return None


def short_id() -> str:
    return secrets.token_hex(4)


# mtime-keyed cache for detect_fold_ancestors. Dashboard refresh scans every
# JSONL in the project dir on every tick; without caching, that's
# O(MB-per-jsonl × tick-rate). Only re-scan when mtime changes.
_FOLD_ANCESTOR_CACHE: dict[str, tuple[float, list[dict]]] = {}


def detect_fold_ancestors_cached(jsonl_path: Path) -> list[dict]:
    """mtime-cached wrapper around ``detect_fold_ancestors``."""
    try:
        mtime = jsonl_path.stat().st_mtime
    except OSError:
        return []
    key = str(jsonl_path)
    cached = _FOLD_ANCESTOR_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    result = detect_fold_ancestors(jsonl_path)
    _FOLD_ANCESTOR_CACHE[key] = (mtime, result)
    return result


def detect_fold_ancestors(jsonl_path: Path) -> list[dict]:
    """Scan a claude JSONL for synthetic context_fold marker entries and
    return the chain of pre-fold session UUIDs they reference.

    The fold rewriter (src/rewriter.py) appends an assistant entry with
    ``model: "context-fold"`` whose tool_use ``input.session_id`` is the
    pre-fold session UUID. These markers are copied across subsequent
    folds, so a single JSONL can carry the entire ancestry. Used at
    ``create_session`` time so resuming a folded session re-establishes
    ghost annotations on the dashboard without manual bookkeeping.
    """
    out: list[dict] = []
    seen: set[str] = set()
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") != "assistant":
                    continue
                msg = e.get("message") or {}
                if msg.get("model") != "context-fold":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    inp = block.get("input") or {}
                    pre_fold = inp.get("session_id")
                    if not isinstance(pre_fold, str) or pre_fold in seen:
                        continue
                    seen.add(pre_fold)
                    folded_at = 0.0
                    ts_str = e.get("timestamp", "") or ""
                    if ts_str:
                        try:
                            iso = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
                            folded_at = datetime.fromisoformat(iso).timestamp()
                        except (TypeError, ValueError):
                            folded_at = 0.0
                    out.append({"uuid": pre_fold, "folded_at": folded_at})
    except OSError:
        pass
    return out


def create_session(
    cwd: str,
    claude_args: list[str],
    resumed_from: Optional[str] = None,
    name: Optional[str] = None,
    initial_prompt: Optional[str] = None,
) -> Session:
    """Spawn a multiverse-managed claude session in a detached tmux session.

    Used by the monitor (interactive: new session / resume from picker) and
    by the MCP server (programmatic: auto-resume dormant agent for inter-
    agent comms).
    """
    sid = short_id()
    d = STATE_DIR / sid
    d.mkdir(parents=True, exist_ok=True)

    cwd_abs = str(Path(cwd).expanduser().resolve())
    if not Path(cwd_abs).is_dir():
        raise ValueError(f"Not a directory: {cwd_abs}")

    # Default every spawned session to auto permission mode — a brand new
    # session, and a resume after compaction (which claude treats as a new
    # session since it's a freshly-written live JSONL), otherwise both start
    # in claude's default (prompt-for-everything) mode. Skipped if the
    # caller already specified a mode, so an explicit `multiverse
    # --permission-mode plan` (or similar) isn't silently overridden.
    if not any(a == "--permission-mode" for a in claude_args):
        claude_args = [*claude_args, "--permission-mode", "auto"]

    now = time.time()
    meta: dict[str, Any] = {
        "cwd": cwd_abs,
        "created_at": now,
        "claude_args": claude_args,
        "host": current_host(),
    }
    if resumed_from:
        meta["resumed_from"] = resumed_from
    if name:
        meta["name"] = name

    # If this is a resume of a folded JSONL, recover the full pre-fold
    # ancestry from the synthetic context_fold markers and seed
    # superseded_uuids so the dashboard ghost-tags those JSONLs. Without
    # this, every container restart / monitor restart would lose ghost
    # annotation as soon as the original folding multiverse session is
    # reaped.
    resume_uuid: Optional[str] = None
    for i, arg in enumerate(claude_args):
        if arg == "--resume" and i + 1 < len(claude_args):
            resume_uuid = claude_args[i + 1]
            break
    if resume_uuid:
        jsonl_path = CLAUDE_PROJECTS_DIR / _encode_cwd(cwd_abs) / f"{resume_uuid}.jsonl"
        ancestors = detect_fold_ancestors(jsonl_path) if jsonl_path.exists() else []
        if ancestors:
            meta["superseded_uuids"] = ancestors

    (d / "meta.json").write_text(json.dumps(meta))
    (d / "status.json").write_text(json.dumps({"status": "starting", "ts": now}))

    # When a caller (e.g. spawn_agent) wants the new claude session to start
    # with a specific initial prompt, write the prompt to a file and let
    # multiverse-session inject it as an argv positional at exec time. This
    # keeps the (potentially long, apostrophe-heavy, multi-line) prompt off
    # the tmux/shell command line entirely — no shlex round-trip, no
    # ARG_MAX risk, no /bin/sh re-parsing.
    initial_prompt_file: Optional[Path] = None
    if initial_prompt:
        initial_prompt_file = d / "initial_prompt.txt"
        initial_prompt_file.write_text(initial_prompt)

    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
    if not plugin_root:
        raise RuntimeError("MULTIVERSE_PLUGIN_ROOT not set — launch via bin/multiverse")
    inner = str(Path(plugin_root) / "bin" / "multiverse-session")

    inner_cmd = shlex.join([inner, *claude_args])

    tmux_name = f"{TMUX_PREFIX}{sid}"
    cmd = [
        "tmux", "new-session", "-d",
        "-s", tmux_name,
        "-c", cwd_abs,
        "-e", f"MULTIVERSE_ROOT={MULTIVERSE_DIR}",
        "-e", f"MULTIVERSE_SESSION_ID={sid}",
        "-e", f"MULTIVERSE_SESSION_CWD={cwd_abs}",
        "-e", f"MULTIVERSE_STATUS={d / 'status.json'}",
        "-e", f"MULTIVERSE_PLUGIN_ROOT={plugin_root}",
    ]

    # Propagate the user's Claude install env so the inner `claude`
    # process resolves transcripts under the same projects dir the
    # monitor used to find this session. Without this, a session
    # spawned by a monitor with CLAUDE_CONFIG_DIR set would inherit
    # only the tmux server's env (typically captured at server start
    # under a different terminal) and write to the wrong directory —
    # making cross-terminal resume produce a fresh JSONL instead of
    # picking up the existing one. PATH carries the bin dir of the
    # caller's `claude` so the inner shell finds the same binary.
    for env_key in ("CLAUDE_CONFIG_DIR", "CLAUDE_LOCAL_DATA_DIR", "PATH",
                    "MULTIVERSE_CLAUDE_BIN", "MULTIVERSE_EXTRA_PLUGIN_DIRS"):
        val = os.environ.get(env_key)
        if val:
            cmd += ["-e", f"{env_key}={val}"]

    if name:
        cmd += ["-e", f"MULTIVERSE_SESSION_NAME={name}"]
    if initial_prompt_file is not None:
        cmd += ["-e", f"MULTIVERSE_INITIAL_PROMPT_FILE={initial_prompt_file}"]
    cmd.append(inner_cmd)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        # tmux's stderr is the real diagnostic — callers (e.g. spawn_agent)
        # were previously seeing only the CalledProcessError repr, which
        # makes it nearly impossible to tell `tmux: error connecting…` from
        # `bin/multiverse-session: not found` from `session already exists`.
        detail = (e.stderr or "").strip() or (e.stdout or "").strip() or str(e)
        raise RuntimeError(f"tmux new-session failed: {detail[:600]}") from e

    # Per-session mouse mode: scroll wheel scrolls the pane history naturally
    # (no Ctrl-b [ to enter copy mode). Scoped to this session so the user's
    # global tmux config is untouched. Selecting text now goes through tmux
    # copy-mode — hold Shift while dragging for terminal-native selection
    # (iTerm2/Alacritty/Kitty/etc.).
    subprocess.run(
        ["tmux", "set-option", "-t", tmux_name, "mouse", "on"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return Session(
        id=sid,
        cwd=cwd_abs,
        created_at=now,
        status="starting",
        status_ts=now,
        resumed_from=resumed_from,
        name=name,
        host=meta["host"],
    )


def current_host() -> str:
    return socket.gethostname()


def foreign_host_still_reported_live(s: Session) -> bool:
    """True if ``s`` looks safe to reap ONLY because tmux can't see it —
    and that "can't see it" signal isn't trustworthy for this session.

    Liveness is normally read off local tmux (``tmux_session_exists``),
    which only ever sees tmux servers on the CURRENT host. That's a
    problem in a deployment ``MULTIVERSE_ROOT`` docs already describe as
    supported: multiple containers/hosts sharing one network-mounted
    state directory. A session spawned on host A is invisible to host
    B's local tmux — host B's monitor would conclude "ended" when the
    session is actually alive on host A, still updating the SAME shared
    status.json via its own real hooks.

    The one cross-host-trustworthy signal is the hook-persisted status
    value itself: "ended" is written ONLY by the SessionEnd hook of the
    actual live process, wherever it runs. Anything else read fresh from
    disk (idle/working/waiting/starting) means something, somewhere, is
    still alive and updating it.

    Callers use this to gate the "reap this ended-looking session's state
    dir before spawning a replacement under the same name" step (the
    monitor's Enter-to-resume flow, and message_agent's push-notification
    auto-resume). Skipping this check lets a foreign, still-alive session
    get its state dir deleted out from under it while a brand-new session
    is created under the identical name — an invisible zombie (no meta
    dir anywhere, so no dashboard shows it) that keeps running, keeps
    firing its own hooks, and keeps polling/consuming the same shared
    inbox as the new session — the mechanism behind duplicate message
    delivery.

    Returns False (safe to reap) when ``s.host`` is unset (legacy session
    predating this field, or ``current_host()`` failed at creation time)
    or matches this host — local tmux is authoritative for those, same
    as before this function existed.
    """
    if not s.host or s.host == current_host():
        return False
    fresh = load_session(s.id)
    if fresh is None:
        return False  # already reaped by someone else; nothing to protect
    return fresh.status != "ended"


def kill_session(s: Session) -> None:
    """Tear down a multiverse session: kill tmux + remove state dir."""
    if tmux_session_exists(s.tmux_name):
        subprocess.run(
            ["tmux", "kill-session", "-t", s.tmux_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        shutil.rmtree(s.dir)
    except OSError:
        pass
