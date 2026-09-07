"""Data layer for multiverse session state.

Pure read-only helpers shared by the monitor TUI and the MCP server. No
Textual or subprocess dependencies — anything that *spawns* (create_session,
kill_session) lives in monitor.py instead.

The data sources are:
  ~/.multiverse/sessions/<mv-id>/  — per-session state written by the monitor
                                     and the SessionStart hook
  ~/.claude/projects/<encoded-cwd>/<claude-uuid>.jsonl
                                   — claude's own session transcripts
  ~/.codex/sessions/**/rollout-<timestamp>-<codex-id>.jsonl
                                   — codex's own session transcripts
  ~/.codex/session_index.jsonl    — codex's user-assigned thread names
  ~/.pi/agent/sessions/**.jsonl   — pi's native session transcripts
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import forks
from .atomic_files import atomic_write_json, exclusive_file_lock
from .agent_descriptions import read_description, write_description
from .compact_state import list_locked
from .hosts import (
    HOST_FOREIGN,
    HOST_FOREIGN_STALE,
    HOST_LOCAL,
    current_host,
    heartbeat_is_stale,
    touch_heartbeat,
    touch_heartbeat_throttled,
)
from .paths import (
    claude_projects_dir,
    codex_home,
    encode_cwd as _encode_cwd,
    multiverse_dir,
)
from .pi_transcript import list_pi_sessions_in


MULTIVERSE_DIR = multiverse_dir()
STATE_DIR = MULTIVERSE_DIR / "sessions"
CLAUDE_PROJECTS_DIR = claude_projects_dir()
CODEX_HOME = codex_home()
TMUX_PREFIX = "multiverse-"
RUNTIME_CLAUDE = "claude"
RUNTIME_CODEX = "codex"
RUNTIME_PI = "pi"
RUNTIMES = (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_PI)
_STARTING_LIVENESS_GRACE_SECONDS = 10.0
_CODEX_RUNTIME_TERM_TIMEOUT_SECONDS = 2.0
_CODEX_RUNTIME_KILL_TIMEOUT_SECONDS = 1.0
_PANE_RUNTIME_TERM_TIMEOUT_SECONDS = 2.0
_PANE_RUNTIME_KILL_TIMEOUT_SECONDS = 1.0
_CLAIM_DIR_NAME = "session_claims"
_ENDING_MARKER_NAME = "ending"

# Custom-title stamped on the throwaway transcripts our OWN compaction
# machinery writes (vendor/compaction/priv_session.py::_metadata_row). The
# constructor spawns one `claude --resume ... -p <merge instruction>` per
# merge step; each gets its own claude session id and lands in the project
# dir looking exactly like a dormant agent. It is never a real session, so
# it is hidden from the dashboard rather than offered as something to
# resume. (e033737 already stops those subprocesses from hijacking the
# session pointer or writing junk mirrors; this hides the transcript they
# legitimately leave behind.)
_INTERNAL_TITLES = frozenset({"priv_compaction"})


def normalize_runtime(runtime: Optional[str]) -> str:
    """Validate and normalize a persisted or user-selected runtime."""
    value = (runtime or RUNTIME_CLAUDE).strip().lower()
    if value not in RUNTIMES:
        raise ValueError(
            f"Unsupported runtime '{runtime}'. Expected one of: {', '.join(RUNTIMES)}"
        )
    return value


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
    """Extract title (last ``custom-title``), cwd (first ``cwd`` field), a
    preview (first non-system user message), and whether the transcript is a
    Claude-Code-native background fork, from a claude JSONL.

    Returns ``{"title": Optional[str], "cwd": Optional[str], "preview": str,
    "bg_fork": bool}``. Results are cached by mtime, so scanning dormant
    sessions every second is cheap once warm.

    ``bg_fork`` — Claude Code stamps ``sessionKind: "bg"`` on rows it writes
    while running a session as a background agent (spawned as
    ``--session-id <new> --fork-session --resume <parent.jsonl>`` under a
    ``claude bg-pty-host`` supervisor; this happens automatically when e.g. a
    ``run_in_background`` Bash task completes while the session is detached).
    Such a fork copies the parent's transcript verbatim — custom-title
    included — so without this flag it surfaces as a second dormant agent
    with the same name as its parent.

    We classify on the LAST ``sessionKind``-bearing row rather than "any bg
    row", so a fork that is later attached interactively (and resumes writing
    plain rows) stops being treated as one. Rows inherited from the parent
    carry no ``sessionKind``, so they neither set nor clear the verdict.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"title": None, "cwd": None, "preview": "", "bg_fork": False,
                "n_rows": 0}

    cached = _METADATA_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]

    title: Optional[str] = None
    cwd: Optional[str] = None
    preview: Optional[str] = None
    last_kind: Optional[str] = None
    n_rows = 0
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n_rows += 1
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
                kind = raw.get("sessionKind")
                if kind:
                    last_kind = str(kind)
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
        "bg_fork": last_kind == "bg",
        # Row count, used to tell an ahead fork from a redundant one without a
        # second pass. Free here since we already walked every line.
        "n_rows": n_rows,
    }
    _METADATA_CACHE[str(path)] = (mtime, result)
    return result


def is_native_bg_fork(path: Path) -> bool:
    """True if this claude JSONL is a Claude-Code-native background fork.
    See ``_read_session_metadata``'s ``bg_fork`` note. mtime-cached."""
    return bool(_read_session_metadata(path).get("bg_fork"))


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


_CODEX_METADATA_CACHE: dict[str, tuple[float, dict]] = {}
_CODEX_DISCOVERY_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CODEX_DISCOVERY_TTL = 2.0


def _read_codex_names() -> dict[str, str]:
    """Return the last user-assigned name recorded for each Codex thread."""
    names: dict[str, str] = {}
    index_path = CODEX_HOME / "session_index.jsonl"
    try:
        with index_path.open() as fh:
            for line in fh:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = raw.get("id")
                name = raw.get("thread_name")
                if not isinstance(session_id, str):
                    continue
                if isinstance(name, str) and name:
                    names[session_id] = name
                elif name is None or name == "":
                    names.pop(session_id, None)
    except OSError:
        pass
    return names


def _read_codex_session_metadata(path: Path) -> dict:
    """Extract stable dashboard fields from a Codex rollout JSONL."""
    # Imported lazily to preserve this module's lightweight import path while sharing
    # the exact synthetic-input filtering used by SecondBrain transcript delivery.
    from .codex_transcript import user_text_from_record

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"id": None, "cwd": None, "preview": ""}

    cached = _CODEX_METADATA_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]

    session_id: Optional[str] = None
    cwd: Optional[str] = None
    preview: Optional[str] = None
    ancestor_ids: list[str] = []
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = raw.get("type")
                payload = raw.get("payload") or {}
                if typ == "session_meta" and isinstance(payload, dict):
                    # A resumed rollout can embed the ancestor's session_meta after
                    # its own. The first metadata row identifies this rollout; later
                    # rows are transcript history and must not replace its ID/cwd.
                    if session_id is None:
                        value = payload.get("id")
                        if isinstance(value, str):
                            session_id = value
                    else:
                        value = payload.get("id")
                        if (
                            isinstance(value, str)
                            and value != session_id
                            and value not in ancestor_ids
                        ):
                            ancestor_ids.append(value)
                    if cwd is None:
                        value = payload.get("cwd")
                        if isinstance(value, str):
                            cwd = value
                elif preview is None:
                    value = user_text_from_record(raw)
                    if value.strip():
                        preview = " ".join(value.split())
                        if len(preview) > 80:
                            preview = preview[:79] + "…"
                if session_id and cwd and preview is not None:
                    break
    except OSError:
        pass

    result = {
        "id": session_id,
        "cwd": cwd,
        "preview": preview or "(no user message)",
        "ancestor_ids": ancestor_ids,
    }
    _CODEX_METADATA_CACHE[str(path)] = (mtime, result)
    return result


def list_codex_sessions_in(
    cwd: str,
    *,
    include_unnamed: bool = False,
    include_ancestors: bool = False,
) -> list[dict]:
    """Return Codex sessions under ``cwd``, newest first.

    Codex stores the stable thread id and cwd in rollout metadata, while
    explicit user-facing names live in ``session_index.jsonl``. Unnamed
    threads are omitted unless ``include_unnamed`` is true. Resumed rollouts
    embed their prior thread metadata; named ancestors are omitted by default
    so one logical conversation does not appear multiple times. Internal
    callers that need the complete lineage can pass ``include_ancestors``.
    """
    def visible(items: list[dict]) -> list[dict]:
        selected = items
        if not include_ancestors:
            superseded = {
                ancestor_id
                for item in selected
                if item.get("name")
                for ancestor_id in item.get("ancestor_ids") or ()
            }
            selected = [item for item in selected if item.get("id") not in superseded]
        if not include_unnamed:
            selected = [item for item in selected if item.get("name")]
        return [dict(item) for item in selected]

    try:
        cwd_abs = str(Path(cwd).expanduser().resolve())
    except OSError:
        return []

    now = time.monotonic()
    cached = _CODEX_DISCOVERY_CACHE.get(cwd_abs)
    if cached and now - cached[0] < _CODEX_DISCOVERY_TTL:
        return visible(cached[1])

    names = _read_codex_names()
    sessions_dir = CODEX_HOME / "sessions"
    by_id: dict[str, dict] = {}
    if sessions_dir.is_dir():
        for path in sessions_dir.rglob("rollout-*.jsonl"):
            meta = _read_codex_session_metadata(path)
            session_id = meta.get("id")
            if not session_id or meta.get("cwd") != cwd_abs:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            found = {
                "id": session_id,
                "preview": meta.get("preview") or "(no user message)",
                "mtime": st.st_mtime,
                "size": st.st_size,
                "name": names.get(session_id),
                "ancestor_ids": list(meta.get("ancestor_ids") or ()),
            }
            previous = by_id.get(session_id)
            if previous is None or found["mtime"] > previous["mtime"]:
                by_id[session_id] = found
    out = list(by_id.values())
    out.sort(key=lambda d: d["mtime"], reverse=True)
    _CODEX_DISCOVERY_CACHE[cwd_abs] = (now, out)
    return visible(out)


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


def all_session_names_in_cwd(cwd: str) -> set[str]:
    """Return wrapped and dormant native session names in ``cwd``.

    Fresh name assignment uses this broader set so all runtimes share one
    namespace. Resume flows use ``existing_names_in_cwd`` instead because the
    dormant session being resumed is allowed to reclaim its own native name.
    """
    names = existing_names_in_cwd(cwd)
    names.update(
        str(info["name"])
        for info in list_claude_sessions_in(cwd)
        if info.get("name")
    )
    names.update(
        str(info["name"])
        for info in list_codex_sessions_in(cwd)
        if info.get("name")
    )
    names.update(
        str(info["name"])
        for info in list_pi_sessions_in(cwd)
        if info.get("name")
    )
    return names


@dataclass
class Session:
    id: str
    cwd: str
    created_at: float
    status: str = "unknown"
    status_ts: float = 0.0
    resumed_from: Optional[str] = None
    name: Optional[str] = None
    runtime: str = RUNTIME_CLAUDE
    claude_uuid: Optional[str] = None
    codex_session_id: Optional[str] = None
    pi_session_id: Optional[str] = None
    # claude UUIDs that this multiverse session has superseded via
    # context_fold. Each entry: {"uuid": str, "folded_at": float}.
    superseded_uuids: list[dict] = field(default_factory=list)
    # Hostname that created this session (see current_host()). None for
    # sessions created before this field existed. Used to tell whether a
    # local "tmux doesn't have this session" observation is meaningful —
    # it isn't, when MULTIVERSE_ROOT is shared network storage and the
    # session was actually spawned on a different host's tmux server.
    host: Optional[str] = None
    # Name of the fresh agent that took over this agent's work. Present only
    # when status is ``retired`` and persisted in meta.json so every registry
    # consumer can direct callers to the replacement.
    successor: Optional[str] = None

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
    def runtime_pid_path(self) -> Path:
        return self.dir / "runtime_pid"

    @property
    def ending_marker_path(self) -> Path:
        return self.dir / _ENDING_MARKER_NAME

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def effective_claude_uuid(self) -> Optional[str]:
        """Best-effort live claude UUID.

        Prefers the value captured by the SessionStart hook; falls back to
        ``resumed_from`` (valid for resumed sessions, which keep their UUID
        without ``--fork-session``).

        ``claude_uuid`` is written by the record-claude-uuid.sh SessionStart
        hook, which records whatever session_id it is handed by ANY claude
        process that starts with this session's MULTIVERSE_STATUS in env.
        A short-lived helper process — one launched with
        ``--no-session-persistence``, or whose transcript is unlinked on exit
        — therefore leaves the pointer aimed at a UUID that has no JSONL on
        disk. The session then reads as 0 tokens / empty preview while its
        real transcript shows up as a SEPARATE dormant row, i.e. the same
        agent appears twice.

        So: a recorded UUID that names no JSONL is not trusted. We fall back
        to ``resumed_from`` when that one does resolve. This self-heals the
        display without needing to know which helper clobbered the pointer,
        and is inert whenever the recorded UUID is real (the common case) —
        a brand-new non-resumed session still returns its own UUID even
        before its JSONL exists, because there is nothing to fall back to.

        Also distrusted: a UUID naming a Claude-Code-native background fork.
        Those forks inherit the parent's MULTIVERSE_* env, so their
        SessionStart fires this session's record-claude-uuid.sh hook and the
        pointer swings onto the throwaway fork — leaving the row reading the
        fork's transcript instead of the session the user is attached to.
        """
        if self.runtime != RUNTIME_CLAUDE:
            return None
        recorded = self.claude_uuid
        if (
            recorded
            and self.resumed_from
            and recorded != self.resumed_from
            and self._live_jsonl_exists(self.resumed_from)
            and not self._is_trustworthy(recorded)
        ):
            return self.resumed_from
        return recorded or self.resumed_from

    def _jsonl_for(self, uuid: str) -> Path:
        return CLAUDE_PROJECTS_DIR / _encode_cwd(self.cwd) / f"{uuid}.jsonl"

    def _live_jsonl_exists(self, uuid: str) -> bool:
        try:
            return self._jsonl_for(uuid).is_file()
        except OSError:
            return False

    def _is_trustworthy(self, uuid: str) -> bool:
        """A recorded UUID is trustworthy unless its transcript is absent or
        is a Claude-Code-native background fork."""
        if not self._live_jsonl_exists(uuid):
            return False
        return not is_native_bg_fork(self._jsonl_for(uuid))

    @property
    def runtime_session_id(self) -> Optional[str]:
        """Best-effort native session/thread id for this runtime."""
        if self.runtime == RUNTIME_CODEX:
            return self.codex_session_id or self.resumed_from
        if self.runtime == RUNTIME_PI:
            return self.pi_session_id or self.resumed_from
        return self.effective_claude_uuid


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
    runtime: str = RUNTIME_CLAUDE
    session: Optional[Session] = None
    session_id: Optional[str] = None
    claude_uuid: Optional[str] = None
    preview: str = ""
    # True if this row represents a JSONL that was superseded by a
    # context_fold. Ghost rows display in the dashboard but are filtered
    # out of the inter-agent registry (list_agents/message_agent/ask_agent).
    is_ghost: bool = False
    folded_at: Optional[float] = None
    description: Optional[str] = None

    @property
    def is_dormant(self) -> bool:
        return self.session is None

    @property
    def is_live(self) -> bool:
        stopped = ("ended", "retiring", "retired")
        return (
            self.session is not None
            and self.status not in stopped
            and self.session.status not in stopped
        )

    @property
    def row_key(self) -> str:
        if self.session is not None:
            return f"mv:{self.session.id}"
        return f"{self.runtime[:2]}:{self.session_id or ''}"


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


_TERMINAL_STATUSES = ("ended", "ending", "retiring", "retired")


def write_status_path(path: Path, status: str, ts: Optional[float] = None) -> None:
    """Atomically persist the common Multiverse status shape.

    The guards below are read-then-write, so they are only sound while a
    cross-process lock is held. Each status write is its own short-lived
    process — six Claude hooks, or one detached ``mvctl status`` per pi
    lifecycle event — and unlocked they interleave as
    ``idle: read (not ended) → ended: write → idle: write``, which is exactly
    the stale-``idle``-over-``ended`` outcome the ended guard exists to stop.
    ``atomic_write_json`` makes each write indivisible; it does not order two
    of them against each other.

    Best-effort, matching ``inbox.wake_delivery_lock``: if the lock cannot be
    taken (odd filesystem, no fcntl) the body still runs, degrading to the
    unordered behaviour rather than to no status at all. Only ACQUISITION
    failure is swallowed — see that function for why the ExitStack shape
    matters rather than a ``try`` around the protected body.
    """
    with ExitStack() as stack:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(
                exclusive_file_lock(path.with_name(path.name + ".lock"))
            )
        except OSError:
            pass  # unlocked fallback — see above
        # Proof of life for readers on other hosts, recorded before the
        # guards below can return early: reaching this function at all means
        # this session's own process is running, which is exactly what a
        # dashboard that cannot see this host's tmux needs to know.
        touch_heartbeat(path.parent)
        # Dashboard-initiated teardown owns the status until tmux is gone.
        # Runtime hooks can still fire while SIGTERM is being handled; without
        # this marker a Stop/SessionEnd hook could replace ``ending`` with
        # idle/ended before the teardown operation has actually finished.
        if status not in ("ending", "retired") and (
            path.parent / _ENDING_MARKER_NAME
        ).is_file():
            return
        # ``retired`` is permanent. The handoff has stopped this wrapper and
        # assigned a successor; neither late runtime hooks nor dashboard
        # teardown may move it into another state. During compaction the row is
        # rendered as ``compacting`` from the external lock, without changing
        # this durable value.
        if status != "retired" and _status_is(path, "retired"):
            return
        # Only the handoff owner may finish retirement. Cancellation removes
        # this temporary wrapper instead of reviving it with a status write.
        if status not in ("retiring", "retired") and _status_is(path, "retiring"):
            return
        # ``ended`` is terminal with respect to live states. A session whose
        # status.json says ended never goes live again — resuming builds a new
        # session dir — so a live status arriving afterwards is always stale.
        if status not in _TERMINAL_STATUSES and _status_is_ended(path):
            return
        atomic_write_json(
            path,
            {"status": status, "ts": time.time() if ts is None else ts},
        )


def _status_is_ended(path: Path) -> bool:
    return _status_is(path, "ended")


def _status_is(path: Path, expected: str) -> bool:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh).get("status") == expected
    except (OSError, ValueError, AttributeError):
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _proc_cmdline(pid: int) -> Optional[str]:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _runtime_pid(s: Session) -> Optional[int]:
    raw_pid = _read_text_stripped(s.runtime_pid_path)
    if not raw_pid:
        return None
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _codex_runtime_pid_matches(s: Session, pid: int) -> bool:
    """Return whether ``pid`` is the Codex process owned by ``s``.

    PID files outlive processes, so liveness alone is unsafe: the OS may
    recycle the number for an unrelated process. Every wrapped Codex command
    contains an MCP environment override with the unique Multiverse session
    ID. Require that identity before treating the PID as live or signaling its
    process group. Foreign-host PID files are never compared to local PIDs.
    """
    if s.runtime != RUNTIME_CODEX:
        return False
    if is_foreign_host(s):
        return False
    if not _pid_alive(pid):
        return False
    command = _proc_cmdline(pid)
    if command is None:
        return False
    identities = (
        f'MULTIVERSE_SESSION_ID="{s.id}"',
        f"MULTIVERSE_SESSION_ID='{s.id}'",
        f"MULTIVERSE_SESSION_ID={s.id}",
    )
    return any(identity in command for identity in identities)


def _wait_for_codex_runtime_exit(s: Session, pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _codex_runtime_pid_matches(s, pid):
            return True
        time.sleep(0.05)
    return not _codex_runtime_pid_matches(s, pid)


def _signal_codex_runtime_group(
    s: Session,
    pid: int,
    expected_pgid: int,
    sig: signal.Signals,
) -> bool:
    """Signal an owned Codex process group after rechecking PID identity."""
    if not _codex_runtime_pid_matches(s, pid):
        return False
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return False
    if pgid != expected_pgid or pgid <= 1 or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise RuntimeError(
            f"failed to signal Codex runtime process group {pgid}: {exc}"
        ) from exc
    return True


def _terminate_codex_runtime(s: Session, pid: int, pgid: int) -> None:
    """Stop an owned Codex writer, escalating to SIGKILL if necessary."""
    if _wait_for_codex_runtime_exit(s, pid, 0.5):
        return
    _signal_codex_runtime_group(s, pid, pgid, signal.SIGTERM)
    if _wait_for_codex_runtime_exit(
        s, pid, _CODEX_RUNTIME_TERM_TIMEOUT_SECONDS
    ):
        return
    _signal_codex_runtime_group(s, pid, pgid, signal.SIGKILL)
    if _wait_for_codex_runtime_exit(
        s, pid, _CODEX_RUNTIME_KILL_TIMEOUT_SECONDS
    ):
        return
    raise RuntimeError(
        f"Codex runtime PID {pid} is still alive; retained session state"
    )


def _tmux_pane_runtime_group(s: Session) -> tuple[int, int] | None:
    """Return the live pane PID and its foreground runtime process group.

    The wrapper command is tmux's pane process and both runtime launchers keep
    Claude/Codex in that same foreground process group. Resolving the group from
    tmux avoids trusting a stale PID file and gives Claude, which historically did
    not persist its child PID, the same explicit teardown as Codex.
    """
    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                s.tmux_name,
                "#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        pane_pid = int(result.stdout.strip())
        pane_pgid = os.getpgid(pane_pid)
    except (OSError, TypeError, ValueError):
        return None
    if pane_pid <= 1 or pane_pgid <= 1 or pane_pgid == os.getpgrp():
        return None
    return pane_pid, pane_pgid


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_process_group_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_group_alive(pgid):
            return True
        time.sleep(0.05)
    return not _process_group_alive(pgid)


def _terminate_tmux_pane_runtime(
    s: Session, pane_pid: int, pane_pgid: int
) -> None:
    """Terminate a tmux-owned Claude/Codex group before destroying its pane."""
    # Re-read tmux immediately before signaling. If the pane was replaced or exited
    # after discovery, do not risk signaling a recycled process group.
    if _tmux_pane_runtime_group(s) != (pane_pid, pane_pgid):
        return
    try:
        os.killpg(pane_pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"failed to signal {s.runtime} runtime process group {pane_pgid}: {exc}"
        ) from exc
    if _wait_for_process_group_exit(pane_pgid, _PANE_RUNTIME_TERM_TIMEOUT_SECONDS):
        return

    # The group is still alive, so its identity cannot have been recycled. Escalate
    # before tmux is killed to preserve the runtime-first ordering requested by `k`.
    try:
        os.killpg(pane_pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"failed to kill {s.runtime} runtime process group {pane_pgid}: {exc}"
        ) from exc
    if _wait_for_process_group_exit(pane_pgid, _PANE_RUNTIME_KILL_TIMEOUT_SECONDS):
        return
    raise RuntimeError(
        f"{s.runtime.capitalize()} runtime process group {pane_pgid} is still alive; "
        "retained session state"
    )


def _mark_session_ended(s: Session) -> None:
    now = time.time()
    try:
        s.ending_marker_path.unlink(missing_ok=True)
        write_status_path(s.status_path, "ended", now)
    except OSError:
        pass
    s.status = "ended"
    s.status_ts = now


def mark_session_ending(s: Session, *, force: bool = False) -> None:
    """Persist the non-interactive state used during dashboard teardown.

    The marker is created before the status write so a racing runtime hook
    either wins before us (and is immediately overwritten by ``ending``) or
    sees the marker and leaves the teardown-owned status alone.

    ``force`` records the user's explicit force-kill decision IN the marker,
    because the process that acts on it is not this one: teardown runs
    detached, so an intent held only in memory here would be lost by the time
    ``kill_session`` needs it, and a foreign force-kill would refuse itself.
    """
    now = time.time()
    if force:
        s.ending_marker_path.write_text("force\n")
    else:
        s.ending_marker_path.touch(exist_ok=True)
    try:
        write_status_path(s.status_path, "ending", now)
    except Exception:
        try:
            s.ending_marker_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    s.status = "ending"
    s.status_ts = now


def ending_was_forced(s: Session) -> bool:
    """True when the pending teardown carries an explicit force-kill decision."""
    try:
        return s.ending_marker_path.read_text().strip() == "force"
    except OSError:
        return False


def _is_owned_here(s: Session) -> bool:
    """Identity half of ``may_declare_dead``, cheap enough to ask first.

    Deliberately consults no tmux: a session that is not ours to judge should
    not cost a subprocess to dismiss, and asking about another remote's tmux
    name risks matching a local session that happens to reuse it.
    """
    return bool(s.host) and not is_foreign_host(s)


def may_declare_dead(s: Session, *, visible_here: bool) -> bool:
    """Whether THIS machine is entitled to write ``ended`` for ``s``.

    ``visible_here`` is the caller's own tmux observation, and it decides
    which evidence applies: a session this host can see is one it is itself
    keeping alive in the record, so the heartbeat says nothing new; a
    session it cannot see is one only somebody else could be vouching for.

    Liveness correction rests on a single local observation — ``tmux
    has-session`` — and that observation only ever means "not on this
    machine". Where a ``MULTIVERSE_ROOT`` is shared between remotes, a
    dashboard sees every session in the workspace but can see only its own
    tmux server, so "I cannot find it" is the normal, expected state for every
    session belonging to another remote. Writing ``ended`` on that basis
    reaches across the mount and marks a running agent dead, and because
    ``ended`` is terminal the damage does not heal when the real host comes
    back: its own hooks are refused, and the user is left resuming a session
    that never stopped.

    So death is declared only from evidence that this machine actually has:

    * **A session nobody here can see, that some machine still can, is never
      ours to bury.** This is the load-bearing one, and it is deliberately
      answered without comparing identities. Callers reach it only after
      their own tmux has come up empty, which already establishes that THIS
      machine is not the one keeping the heartbeat warm — so a fresh
      heartbeat is somebody else's, whatever host id it happens to carry.
      Asking "is that id different from mine?" instead is what failed in
      practice: two containers reported the identical hostname, each read its
      own id in the other's heartbeat, dismissed it as an echo, and buried a
      running agent.
    * **An unstamped session is never ours to bury.** A missing ``host``
      means ownership was never recorded, not that it is ours by default —
      and the default is the one that reaches across the mount.
    * **A session recorded to another host is never ours to bury**, the case
      ``is_foreign_host`` covers. Kept as a second line of defence, since it
      is the only one that still works when a remote has no dashboard and its
      agents are idle enough to write no heartbeats at all.

    What this gives up is immediacy: a crashed LOCAL session keeps its last
    status until its own final heartbeat goes stale, because until then this
    host cannot distinguish its own seconds-old signal from another
    machine's. A session that was never stamped with a host keeps it until
    force-killed. Both are visible and recoverable; the alternative was
    silent data loss on a machine we cannot see.
    """
    if not _is_owned_here(s):
        return False
    if visible_here:
        # Our own tmux holds it, so any recent heartbeat is ours and adds
        # nothing. Whatever else the caller knows (a Codex runtime whose
        # PID is gone) decides on its own.
        return True
    return heartbeat_is_stale(s.dir)


def _correct_local_liveness(s: Session) -> bool:
    """Persist ``ended`` when local process evidence proves a row is stale.

    Only ever for a session this host is entitled to bury — see
    ``may_declare_dead``; a foreign host's tmux server is unobservable here,
    so its durable status remains authoritative. New sessions also receive a
    short grace period between reserving their state directory and creating
    tmux. Codex runners record the foreground Codex PID, allowing correction
    even when an abnormal wrapper leaves its tmux pane behind.
    """
    if s.status in ("ended", "retiring", "retired") or not _is_owned_here(s):
        return False

    within_startup_grace = (
        s.status == "starting"
        and time.time() - max(s.created_at, s.status_ts) < _STARTING_LIVENESS_GRACE_SECONDS
    )
    tmux_exists = tmux_session_exists(s.tmux_name)
    if not may_declare_dead(s, visible_here=tmux_exists):
        return False
    runtime_pid = _runtime_pid(s) if s.runtime == RUNTIME_CODEX else None
    runtime_is_owned = bool(
        runtime_pid and _codex_runtime_pid_matches(s, runtime_pid)
    )
    # Runtime-first teardown deliberately leaves a short interval where Codex
    # has exited but its tmux session still exists. Keep rendering ``ending``
    # for that whole interval instead of correcting it prematurely to ended.
    if s.status == "ending" and tmux_exists:
        return False
    if not tmux_exists:
        if within_startup_grace:
            return False
        # A detach must not turn a live Codex writer into an ended/resumable
        # row. Keeping the durable status prevents a second writer from being
        # started against the same native thread; dashboard kill can still
        # terminate this orphan safely using the recorded identity.
        if runtime_is_owned:
            return False
        _mark_session_ended(s)
        return True

    if s.runtime != RUNTIME_CODEX:
        return False
    if runtime_pid is None:
        # Backward compatibility for sessions launched before runtime PID
        # tracking was introduced. Their runner still writes `ended` itself.
        return False
    if runtime_is_owned:
        return False
    if within_startup_grace:
        return False

    _mark_session_ended(s)
    return True


def load_session(session_id: str) -> Optional[Session]:
    d = STATE_DIR / session_id
    meta = _read_json(d / "meta.json")
    if meta is None:
        return None
    raw_superseded = meta.get("superseded_uuids") or []
    superseded = [e for e in raw_superseded if isinstance(e, dict) and e.get("uuid")]
    try:
        # ``feat/pi-harness`` persisted this choice as ``harness`` and wrote
        # pi's native id to ``claude_uuid``. Accept both legacy locations so
        # sessions created while developing the feature survive the merge.
        runtime = normalize_runtime(meta.get("runtime") or meta.get("harness"))
    except ValueError:
        return None
    legacy_session_id = _read_text_stripped(d / "claude_uuid")
    runtime_session_id = _read_text_stripped(d / "runtime_session_id")
    s = Session(
        id=session_id,
        cwd=meta.get("cwd", "?"),
        created_at=meta.get("created_at", 0.0),
        resumed_from=meta.get("resumed_from"),
        name=meta.get("name"),
        runtime=runtime,
        claude_uuid=(legacy_session_id if runtime == RUNTIME_CLAUDE else None),
        codex_session_id=(runtime_session_id if runtime == RUNTIME_CODEX else None),
        pi_session_id=(
            runtime_session_id or legacy_session_id
            if runtime == RUNTIME_PI
            else None
        ),
        superseded_uuids=superseded,
        host=meta.get("host"),
        successor=meta.get("successor"),
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
            _correct_local_liveness(s)
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
    "ending": 1,
    "retiring": 1,
    "unknown": 2,
    "ended": 3,
    "dormant": 4,
    "retired": 5,
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
    """Compute named wrapped and dormant runtime rows for ``cwd``.

    ``sessions`` is expected to already be filtered to ``cwd``.

    Deduplicates: a native session is considered "wrapped" (and skipped from
    the dormant list) if any multiverse session has it as its current runtime
    ID or ``resumed_from``, or, for Claude, if it's listed in ``hide_uuids``
    (used for in-flight ask-all fork JSONLs).
    """
    # Reap stale ask-fork registry entries (and any orphaned fork JSONLs)
    # before we read the registry below.
    forks.gc()

    rows: list[DashboardRow] = []

    wrapped_uuids: set[str] = set(hide_uuids or ())
    wrapped_codex_ids: set[str] = set()
    wrapped_pi_ids: set[str] = set()
    # Ask-fork JSONLs inherit the origin's custom-title, so without this
    # they'd surface as phantom dormant agents. The on-disk fork registry
    # covers forks spawned by ANY process (TUI ask-all AND agent-initiated
    # ask_agent) — hiding them from the dashboard and from list_agents.
    wrapped_uuids |= forks.active_uuids()
    for s in sessions:
        u = s.effective_claude_uuid
        if u:
            wrapped_uuids.add(u)
        if s.runtime == RUNTIME_CODEX:
            if s.runtime_session_id:
                wrapped_codex_ids.add(s.runtime_session_id)
            if s.resumed_from:
                wrapped_codex_ids.add(s.resumed_from)
        elif s.runtime == RUNTIME_PI:
            if s.runtime_session_id:
                wrapped_pi_ids.add(s.runtime_session_id)
            if s.resumed_from:
                wrapped_pi_ids.add(s.resumed_from)

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
    codex_sessions = list_codex_sessions_in(
        cwd,
        include_unnamed=True,
        include_ancestors=True,
    )
    codex_by_id = {info["id"]: info for info in codex_sessions}
    pi_sessions = list_pi_sessions_in(cwd, include_unnamed=True)
    pi_by_id = {info["id"]: info for info in pi_sessions}
    superseded_codex_ids = {
        ancestor_id
        for info in codex_sessions
        if info.get("name")
        for ancestor_id in info.get("ancestor_ids") or ()
    }
    pending_codex_ids = list(wrapped_codex_ids)
    while pending_codex_ids:
        info = codex_by_id.get(pending_codex_ids.pop())
        if not info:
            continue
        for ancestor_id in info.get("ancestor_ids") or ():
            if ancestor_id in wrapped_codex_ids:
                continue
            wrapped_codex_ids.add(ancestor_id)
            pending_codex_ids.append(ancestor_id)

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
        # Look up the native transcript preview for either runtime.
        preview = ""
        u = s.effective_claude_uuid
        if u and project_dir.is_dir():
            jsonl = project_dir / f"{u}.jsonl"
            if jsonl.exists():
                preview = _read_session_metadata(jsonl).get("preview") or ""
        elif s.runtime == RUNTIME_CODEX and s.runtime_session_id:
            info = codex_by_id.get(s.runtime_session_id)
            if info:
                preview = info.get("preview") or ""
            preview_ancestors: list[str] = []
            if s.resumed_from and s.resumed_from != s.runtime_session_id:
                preview_ancestors.append(s.resumed_from)
            if info:
                preview_ancestors.extend(info.get("ancestor_ids") or ())
            for ancestor_id in preview_ancestors:
                if preview and preview != "(no user message)":
                    break
                ancestor = codex_by_id.get(ancestor_id)
                if ancestor:
                    preview = ancestor.get("preview") or preview
        elif s.runtime == RUNTIME_PI and s.runtime_session_id:
            info = pi_by_id.get(s.runtime_session_id)
            if info:
                preview = info.get("preview") or ""
        rows.append(
            DashboardRow(
                name=s.name,
                cwd=s.cwd,
                status=s.status,
                status_ts=s.status_ts,
                runtime=s.runtime,
                session=s,
                session_id=s.runtime_session_id,
                claude_uuid=s.effective_claude_uuid,
                preview=preview,
            )
        )

    # Titles held by a NON-fork transcript. A Claude-Code-native background
    # fork is only a duplicate while the session it forked from still exists;
    # if that parent is gone the fork IS the agent, and hiding it would make
    # the agent vanish from the dashboard entirely. So suppression is keyed on
    # a title collision with a real (non-fork) transcript, not on being a fork.
    # Uses only mtime-cached metadata, so this second pass costs no extra I/O.
    nonfork_titles: set[str] = set()
    nonfork_rows: dict[str, int] = {}
    if project_dir.is_dir():
        for path in project_dir.glob("*.jsonl"):
            m = _read_session_metadata(path)
            t = m.get("title")
            if t and not m.get("bg_fork"):
                nonfork_titles.add(t)
                nonfork_rows[t] = max(nonfork_rows.get(t, 0), m.get("n_rows", 0))

    if project_dir.is_dir():
        for path in project_dir.glob("*.jsonl"):
            uuid = path.stem
            if uuid in wrapped_uuids:
                continue
            meta = _read_session_metadata(path)
            title = meta.get("title")
            if not title:
                continue
            if title in _INTERNAL_TITLES:
                continue
            # Claude-Code-native background forks copy their parent's
            # transcript (custom-title included), so listing one alongside its
            # parent shows the same agent twice. Multiverse already hides its
            # own ask_agent forks via ~/.multiverse/forks/; this is the
            # equivalent for the ones Claude Code spawns itself. Suppress only
            # while the parent is still around — a fork whose parent is gone is
            # the agent's only transcript and must stay visible.
            #
            # And only when the fork adds nothing. A fork routinely runs AHEAD
            # of the session it forked from (that is the whole point — Claude
            # Code spawns it to carry the agent forward), and hiding an ahead
            # fork silently parks the dashboard on a stale branch: observed on
            # viz-server, whose visible row sat 19 turns and ~59k tokens behind
            # its hidden fork. So an ahead fork is SHOWN, tagged so it is not
            # mistaken for a duplicate. Row count is the ahead/behind proxy —
            # already computed by the cached metadata pass, no extra I/O.
            if meta.get("bg_fork") and title in nonfork_titles:
                if meta.get("n_rows", 0) <= nonfork_rows.get(title, 0):
                    continue  # redundant: parent has at least as much
                display_title = f"{title} (bg fork)"
            else:
                display_title = title
            recorded_cwd = meta.get("cwd")
            if recorded_cwd and recorded_cwd != cwd:
                continue
            try:
                st = path.stat()
            except OSError:
                continue

            ghost_ts = ghost_map.get(uuid)
            if ghost_ts is not None:
                display_name = (
                    f"{display_title} (folded {_format_fold_time(ghost_ts)})"
                )
                status_ts = ghost_ts or st.st_mtime
            else:
                display_name = display_title
                status_ts = st.st_mtime

            rows.append(
                DashboardRow(
                    name=display_name,
                    cwd=recorded_cwd or cwd,
                    status="dormant",
                    status_ts=status_ts,
                    runtime=RUNTIME_CLAUDE,
                    session=None,
                    session_id=uuid,
                    claude_uuid=uuid,
                    preview=meta.get("preview") or "",
                    is_ghost=ghost_ts is not None,
                    folded_at=ghost_ts,
                )
            )

    for info in codex_sessions:
        session_id = info["id"]
        if (
            session_id in wrapped_codex_ids
            or session_id in superseded_codex_ids
            or not info.get("name")
        ):
            continue
        rows.append(
            DashboardRow(
                name=info["name"],
                cwd=cwd,
                status="dormant",
                status_ts=info["mtime"],
                runtime=RUNTIME_CODEX,
                session=None,
                session_id=session_id,
                preview=info.get("preview") or "",
            )
        )

    for info in pi_sessions:
        session_id = info["id"]
        if session_id in wrapped_pi_ids or not info.get("name"):
            continue
        rows.append(
            DashboardRow(
                name=info["name"],
                cwd=info.get("cwd") or cwd,
                status="dormant",
                status_ts=info["mtime"],
                runtime=RUNTIME_PI,
                session=None,
                session_id=session_id,
                preview=info.get("preview") or "",
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

    for row in rows:
        if not row.is_ghost:
            row.description = read_description(row.cwd, row.name, root=STATE_DIR.parent)

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


def wrapped_runtime_session_ids(sessions: list[Session]) -> set[tuple[str, str]]:
    """Native ids already represented by multiverse session metadata."""
    out: set[tuple[str, str]] = set()
    for session in sessions:
        session_id = session.runtime_session_id
        if session_id:
            out.add((session.runtime, session_id))
        if (
            session.runtime in (RUNTIME_CODEX, RUNTIME_PI)
            and session.resumed_from
            and session.resumed_from != session_id
        ):
            out.add((session.runtime, session.resumed_from))
    return out


def list_named_agents_in_cwd(cwd: str) -> list[DashboardRow]:
    """Named Multiverse and dormant runtime sessions for ``cwd``.

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


def _claim_path(kind: str, value: str) -> Path:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return STATE_DIR.parent / _CLAIM_DIR_NAME / f"{kind}-{digest}.claim"


def _session_claim_specs(
    cwd: str,
    runtime: str,
    name: Optional[str],
    native_session_id: Optional[str],
) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    if name:
        specs.append(("name", f"{cwd}\0{name}"))
    if native_session_id:
        specs.append(("native", f"{runtime}\0{native_session_id}"))
    return sorted(specs)


def _acquire_claim(path: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            # symlink creation is atomic and its target carries the owner ID;
            # unlike an O_EXCL JSON file, there is no visible empty-file window.
            path.symlink_to(session_id)
        except FileExistsError:
            try:
                owner = os.readlink(path)
            except OSError:
                owner = ""
            if owner and (STATE_DIR / owner).exists():
                raise RuntimeError(
                    f"Session is already claimed by Multiverse wrapper {owner}"
                )
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        return
    raise RuntimeError("Could not acquire Multiverse session claim")


def _release_claim(path: Path, session_id: str) -> None:
    try:
        owner = os.readlink(path)
    except OSError:
        return
    if owner != session_id:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _assert_no_existing_wrapper_claim(
    session_id: str,
    cwd: str,
    runtime: str,
    name: Optional[str],
    native_session_id: Optional[str],
) -> None:
    """Catch wrappers created before claim files were introduced."""
    if not STATE_DIR.exists():
        return
    for entry in STATE_DIR.iterdir():
        if not entry.is_dir() or entry.name == session_id:
            continue
        meta = _read_json(entry / "meta.json")
        if not meta or meta.get("cwd") != cwd:
            continue
        if name and meta.get("name") == name:
            raise RuntimeError(
                f"Agent name '{name}' is already claimed by wrapper {entry.name}"
            )
        if not native_session_id:
            continue
        try:
            existing_runtime = normalize_runtime(
                meta.get("runtime") or meta.get("harness")
            )
        except ValueError:
            continue
        if existing_runtime != runtime:
            continue
        existing_native_id = _read_text_stripped(entry / "runtime_session_id")
        if not existing_native_id and existing_runtime == RUNTIME_PI:
            existing_native_id = _read_text_stripped(entry / "claude_uuid")
        if not existing_native_id:
            existing_native_id = meta.get("resumed_from")
        if existing_native_id == native_session_id:
            raise RuntimeError(
                f"{runtime} session '{native_session_id}' is already claimed "
                f"by wrapper {entry.name}"
            )


def _release_session_claims(s: Session) -> None:
    for kind, value in _session_claim_specs(
        s.cwd,
        s.runtime,
        s.name,
        s.runtime_session_id,
    ):
        _release_claim(_claim_path(kind, value), s.id)


def create_retired_session_record(
    cwd: str,
    name: str,
    runtime: str,
    native_session_id: str,
    successor: str,
) -> Session:
    """Claim a dormant native session and persist it as a retired agent.

    Unlike :func:`create_session`, this creates no tmux session and starts no
    runtime. The wrapper record keeps the predecessor in the dashboard and
    hides the same native transcript from dormant discovery, while preserving
    its native ID for ask_agent and Claude compaction.
    """
    if not successor.strip():
        raise ValueError("A retired agent must have a successor")
    return _create_handoff_session_record(
        cwd, name, runtime, native_session_id, "retired", successor.strip()
    )


def create_retiring_session_record(
    cwd: str, name: str, runtime: str, native_session_id: str
) -> Session:
    """Reserve a dormant predecessor before starting its ephemeral handoff.

    Name/native claims prevent another dashboard or message wake from resuming
    it. No runtime is started and no native transcript is modified.
    """
    return _create_handoff_session_record(
        cwd, name, runtime, native_session_id, "retiring", None
    )


def _create_handoff_session_record(
    cwd: str, name: str, runtime: str, native_session_id: str,
    status: str, successor: Optional[str],
) -> Session:
    runtime = normalize_runtime(runtime)
    cwd_abs = str(Path(cwd).expanduser().resolve())
    name = name.strip()
    native_session_id = native_session_id.strip()
    if not name:
        raise ValueError("A handoff agent must have a name")
    if not native_session_id:
        raise ValueError("A handoff agent must have a native session ID")
    if name == successor:
        raise ValueError("A retired agent's successor must be a different agent")

    sid = short_id()
    directory = STATE_DIR / sid
    directory.mkdir(parents=True, exist_ok=False)
    claim_paths: list[Path] = []
    try:
        for kind, value in _session_claim_specs(
            cwd_abs, runtime, name, native_session_id
        ):
            path = _claim_path(kind, value)
            _acquire_claim(path, sid)
            claim_paths.append(path)
        _assert_no_existing_wrapper_claim(
            sid, cwd_abs, runtime, name, native_session_id
        )
        now = time.time()
        host = current_host()
        # Readers discover a wrapper through meta.json. Publish its guard
        # first so nobody observes an interactive/unknown handoff record.
        write_status_path(directory / "status.json", status, now)
        atomic_write_json(
            directory / "meta.json",
            {
                "cwd": cwd_abs,
                "created_at": now,
                "runtime": runtime,
                "host": host,
                "name": name,
                "resumed_from": native_session_id,
                "successor": successor,
                "handoff_owner_pid": os.getpid() if status == "retiring" else None,
            },
        )
    except Exception:
        for path in claim_paths:
            _release_claim(path, sid)
        shutil.rmtree(directory, ignore_errors=True)
        raise

    return Session(
        id=sid,
        cwd=cwd_abs,
        created_at=now,
        status=status,
        status_ts=now,
        resumed_from=native_session_id,
        name=name,
        runtime=runtime,
        claude_uuid=(native_session_id if runtime == RUNTIME_CLAUDE else None),
        codex_session_id=(native_session_id if runtime == RUNTIME_CODEX else None),
        pi_session_id=(native_session_id if runtime == RUNTIME_PI else None),
        host=host,
        successor=successor,
    )


def remove_retired_session_record(session: Session) -> None:
    """Roll back an uncompleted handoff without touching the native session."""
    if session.status != "retired":
        raise ValueError("Only retired session records can be rolled back")
    shutil.rmtree(session.dir, ignore_errors=True)
    _release_session_claims(session)


def remove_retiring_session_record(session: Session) -> None:
    """Cancel a pending handoff, exposing the untouched dormant transcript."""
    fresh = load_session(session.id)
    if fresh is None:
        return
    if fresh.status != "retiring":
        raise ValueError("Only retiring session records can be cancelled")
    shutil.rmtree(session.dir)
    _release_session_claims(session)


def recover_abandoned_handoffs(cwd: str) -> None:
    """Release reservations left by a dead local dashboard, never another host."""
    cwd = str(Path(cwd).expanduser().resolve())
    for session in list_sessions():
        if (session.cwd != cwd or session.status != "retiring"
                or session.host != current_host()):
            continue
        meta = _read_json(session.meta_path) or {}
        owner = meta.get("handoff_owner_pid")
        if isinstance(owner, int) and owner > 0 and not _pid_alive(owner):
            remove_retiring_session_record(session)


def create_session(
    cwd: str,
    claude_args: list[str],
    resumed_from: Optional[str] = None,
    name: Optional[str] = None,
    initial_prompt: Optional[str] = None,
    runtime: str = RUNTIME_CLAUDE,
) -> Session:
    """Spawn a multiverse-managed runtime session in detached tmux.

    Used by the monitor (interactive: new session / resume from picker) and
    by the MCP server (programmatic: auto-resume dormant agent for inter-
    agent comms).
    """
    runtime = normalize_runtime(runtime)
    cwd_abs = str(Path(cwd).expanduser().resolve())
    if not Path(cwd_abs).is_dir():
        raise ValueError(f"Not a directory: {cwd_abs}")
    plugin_root = os.environ.get("MULTIVERSE_PLUGIN_ROOT")
    if not plugin_root:
        raise RuntimeError("MULTIVERSE_PLUGIN_ROOT not set — launch via bin/multiverse")

    sid = short_id()
    d = STATE_DIR / sid
    d.mkdir(parents=True, exist_ok=False)
    claim_paths: list[Path] = []
    try:
        for kind, value in _session_claim_specs(
            cwd_abs, runtime, name, resumed_from
        ):
            path = _claim_path(kind, value)
            _acquire_claim(path, sid)
            claim_paths.append(path)
        _assert_no_existing_wrapper_claim(
            sid, cwd_abs, runtime, name, resumed_from
        )
    except Exception:
        for path in claim_paths:
            _release_claim(path, sid)
        shutil.rmtree(d, ignore_errors=True)
        raise

    # Default every spawned session to auto permission mode — a brand new
    # session, and a resume after compaction (which claude treats as a new
    # session since it's a freshly-written live JSONL), otherwise both start
    # in claude's default (prompt-for-everything) mode. Skipped if the
    # caller already specified a mode, so an explicit `multiverse
    # --permission-mode plan` (or similar) isn't silently overridden.
    if runtime == RUNTIME_CLAUDE and not any(
        a == "--permission-mode" for a in claude_args
    ):
        claude_args = [*claude_args, "--permission-mode", "auto"]

    # Pin the model for multiverse-started CLAUDE sessions (this whole block
    # is gated on RUNTIME_CLAUDE — codex sessions are unaffected). Keeps all
    # claude sessions on a consistent model regardless of the user's
    # settings.json /model default (which /model may set to a bare alias).
    # Configurable via MULTIVERSE_CLAUDE_MODEL (bin/multiverse defaults it to
    # opus[1m]); set a full id in bin/multiverse.local for a packaged claude
    # that rejects bare aliases. Skipped if the caller already passed --model.
    if runtime == RUNTIME_CLAUDE and not any(
        a == "--model" for a in claude_args
    ):
        claude_model = os.environ.get("MULTIVERSE_CLAUDE_MODEL")
        if claude_model:
            claude_args = [*claude_args, "--model", claude_model]

    # Pin the model for multiverse-started CODEX sessions too. Codex's model
    # flag is a global option, so prepend it before a possible `resume`
    # subcommand. An explicit long/short option remains authoritative.
    if runtime == RUNTIME_CODEX and not any(
        a in ("--model", "-m") or a.startswith("--model=")
        for a in claude_args
    ):
        codex_model = os.environ.get("MULTIVERSE_CODEX_MODEL")
        if codex_model:
            claude_args = ["--model", codex_model, *claude_args]

    now = time.time()
    meta: dict[str, Any] = {
        "cwd": cwd_abs,
        "created_at": now,
        "runtime": runtime,
        "host": current_host(),
    }
    meta[f"{runtime}_args"] = claude_args
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
    if runtime == RUNTIME_CLAUDE:
        for i, arg in enumerate(claude_args):
            if arg == "--resume" and i + 1 < len(claude_args):
                resume_uuid = claude_args[i + 1]
                break
    if resume_uuid:
        jsonl_path = CLAUDE_PROJECTS_DIR / _encode_cwd(cwd_abs) / f"{resume_uuid}.jsonl"
        ancestors = detect_fold_ancestors(jsonl_path) if jsonl_path.exists() else []
        if ancestors:
            meta["superseded_uuids"] = ancestors

    try:
        # A new agent never inherits the description of a previously deleted
        # agent with the same name. Resumes keep the logical agent's profile.
        # Do this before launching the runtime, which may set its description
        # as soon as it starts. The name claim above excludes competing spawns.
        if name and not resumed_from and not resume_uuid:
            write_description(cwd_abs, name, "", root=STATE_DIR.parent)
        atomic_write_json(d / "meta.json", meta)
        write_status_path(d / "status.json", "starting", now)
    except Exception:
        for path in claim_paths:
            _release_claim(path, sid)
        shutil.rmtree(d, ignore_errors=True)
        raise

    # When a caller (e.g. spawn_agent) wants the new runtime session to start
    # with a specific initial prompt, write the prompt to a file for its runner.
    # Claude consumes it as an argv positional; fresh Codex sessions submit it
    # through the live TUI after setting their native thread name. This keeps the
    # potentially long, multiline prompt off the tmux/shell command line.
    initial_prompt_file: Optional[Path] = None
    if initial_prompt:
        initial_prompt_file = d / "initial_prompt.txt"
        try:
            initial_prompt_file.write_text(initial_prompt)
        except OSError:
            for path in claim_paths:
                _release_claim(path, sid)
            shutil.rmtree(d, ignore_errors=True)
            raise

    runner_name = {
        RUNTIME_CLAUDE: "multiverse-session",
        RUNTIME_CODEX: "multiverse-codex-session",
        RUNTIME_PI: "multiverse-pi-session",
    }[runtime]
    inner = str(Path(plugin_root) / "bin" / runner_name)

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
        "-e", f"MULTIVERSE_RUNTIME={runtime}",
        "-e", f"MULTIVERSE_HOST_ID={meta['host']}",
        "-e", f"MULTIVERSE_PLUGIN_ROOT={plugin_root}",
    ]

    codex_plugin_root = os.environ.get("MULTIVERSE_CODEX_PLUGIN_ROOT")
    if not codex_plugin_root:
        codex_plugin_root = str(
            Path(plugin_root).parent / "plugins" / "multiverse-codex"
        )
    if runtime in (RUNTIME_CODEX, RUNTIME_PI):
        cmd += [
            "-e", f"MULTIVERSE_RUNTIME_SESSION_ID_PATH={d / 'runtime_session_id'}",
        ]
    if runtime == RUNTIME_CODEX:
        cmd += [
            "-e", f"MULTIVERSE_CODEX_PLUGIN_ROOT={codex_plugin_root}",
            "-e", f"MULTIVERSE_RUNTIME_PID_PATH={d / 'runtime_pid'}",
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
    # Base set always forwarded into the tmux-spawned session, plus any
    # extra keys named in MULTIVERSE_FORWARD_ENV (space-separated). The
    # extra-keys hook lets a machine forward environment-specific auth /
    # config vars (e.g. a packaged claude's persistent-token auth) by
    # setting MULTIVERSE_FORWARD_ENV in bin/multiverse.local — without
    # hardcoding those names in tracked code.
    base_env_keys = (
        "CLAUDE_CONFIG_DIR", "CLAUDE_LOCAL_DATA_DIR", "CODEX_HOME", "PATH",
        "MULTIVERSE_CLAUDE_BIN", "MULTIVERSE_EXTRA_PLUGIN_DIRS",
        "MULTIVERSE_CODEX_BIN", "MULTIVERSE_CODEX_MODEL",
        "MULTIVERSE_CODEX_REASONING_EFFORT",
        # ask_agent's Codex side-fork reads this in whichever process makes
        # the call — the monitor OR an agent's own MCP server — so it has to
        # reach spawned sessions too, not just the monitor.
        "MULTIVERSE_CODEX_ASK_NO_CONFIG_OVERRIDES",
        "MULTIVERSE_PI_BIN",
        "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR",
        # Rate-limit retry tuning — see bin/multiverse for defaults +
        # rationale. Propagated so interactive claude sessions inside tmux
        # honor them, not just subprocess-inherited workers.
        "CLAUDE_CODE_MAX_RETRIES", "CLAUDE_CODE_UNATTENDED_RETRY",
    )
    extra_env_keys = tuple(os.environ.get("MULTIVERSE_FORWARD_ENV", "").split())
    for env_key in base_env_keys + extra_env_keys:
        val = os.environ.get(env_key)
        if val:
            cmd += ["-e", f"{env_key}={val}"]

    if name:
        cmd += ["-e", f"MULTIVERSE_SESSION_NAME={name}"]
    if initial_prompt_file is not None:
        cmd += ["-e", f"MULTIVERSE_INITIAL_PROMPT_FILE={initial_prompt_file}"]
    # Queue the persistence option in the same tmux invocation as creation.
    # Running a second `tmux set-option` command would race a user's global
    # destroy-unattached=on setting: the brand-new `-d` session has no clients
    # and could be destroyed before that second process reaches the server.
    cmd += [
        inner_cmd,
        ";",
        "set-option",
        "-t",
        tmux_name,
        "destroy-unattached",
        "off",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as e:
        # tmux's stderr is the real diagnostic — callers (e.g. spawn_agent)
        # were previously seeing only the CalledProcessError repr, which
        # makes it nearly impossible to tell `tmux: error connecting…` from
        # `bin/multiverse-session: not found` from `session already exists`.
        if isinstance(e, subprocess.CalledProcessError):
            detail = (e.stderr or "").strip() or (e.stdout or "").strip() or str(e)
        else:
            detail = str(e)
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", tmux_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        for path in claim_paths:
            _release_claim(path, sid)
        shutil.rmtree(d, ignore_errors=True)
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

    # Firsthand: notify the SecondBrain connector of a fresh agent creation. Only fires
    # for genuinely new agents (resumed_from is None); resume flows — from the dashboard,
    # from SB's own auto-resume-with-prompt, from any programmatic resume — are NOT
    # creation events and skip this. No-op unless the workspace has connected to SB before
    # (seeded gate applied in the recorder). Never breaks the spawn flow.
    if resumed_from is None and name:
        try:
            from . import sb_notify
            sb_notify.record_agent_created(cwd_abs, name, initial_prompt, sid)
        except Exception:  # noqa: BLE001 — never break session creation
            pass

    return Session(
        id=sid,
        cwd=cwd_abs,
        created_at=now,
        status="starting",
        status_ts=now,
        resumed_from=resumed_from,
        name=name,
        runtime=runtime,
        host=meta["host"],
    )


def is_foreign_host(s: Session) -> bool:
    """True when ``s`` was created on a different machine than this one.

    Says nothing about whether it is still alive over there — see
    ``foreign_host_still_reported_live`` and ``hosts.heartbeat_is_stale``.

    Compares against this module's ``current_host`` rather than delegating to
    ``hosts.host_is_foreign``, so that patching ``sessions.current_host``
    still governs every host decision made in this module. Delegating reads
    the name from the other module's globals, where such a patch is invisible
    — and a host guard that silently ignores the identity it was given is
    both untestable and the exact shape of bug this file exists to prevent.
    """
    return bool(s.host) and s.host != current_host()


def session_host_state(s: Session) -> str:
    """Shared vocabulary for a session: local / foreign / foreign_stale."""
    if not is_foreign_host(s):
        return HOST_LOCAL
    return HOST_FOREIGN_STALE if heartbeat_is_stale(s.dir) else HOST_FOREIGN


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

    Also returns False when local tmux CAN see the session, even though
    the recorded host doesn't match. A hostname is not a stable machine
    identity: macOS reports a different one depending on the network it's
    attached to (mDNS ``Silicon23.local`` vs DHCP ``MacBookPro.lan`` for
    the same laptop), so a session created right here can come back
    looking foreign after nothing more than a network change — and then
    refuse to be killed or resumed on the very machine running it. A tmux
    server is per-machine, so its seeing the session settles the question
    regardless of what string was recorded. This does not weaken the
    shared-``MULTIVERSE_ROOT`` case the guard exists for: a session
    spawned on host A never shows up in host B's tmux.
    """
    if not is_foreign_host(s):
        return False
    if tmux_session_exists(s.tmux_name):
        return False
    fresh = load_session(s.id)
    if fresh is None:
        return False  # already reaped by someone else; nothing to protect
    return fresh.status not in ("ended", "retiring", "retired")


def retiring_agent_message() -> str:
    """Shared refusal while a predecessor's handoff is pending."""
    return "This agent is being retired, please try again later"


def retired_agent_message(name: str, successor: Optional[str]) -> str:
    """Shared refusal text for every channel that can prompt an agent."""
    message = f"'{name}' cannot be messaged because it has retired."
    if successor:
        message += f" Consider messaging its successor '{successor}' instead."
    return message


def mark_session_retired(s: Session, successor: str) -> None:
    """Persist a stopped agent's permanent retired state and successor.

    The handoff implementation is responsible for stopping the runtime and
    freezing its full transcript before calling this helper. Metadata lands
    first so no observer can see ``retired`` without also knowing where to
    redirect messages.
    """
    successor = successor.strip()
    if not successor:
        raise ValueError("A retired agent must have a successor")
    if successor == s.name:
        raise ValueError("A retired agent's successor must be a different agent")
    meta = _read_json(s.meta_path)
    if meta is None:
        raise RuntimeError(f"Session metadata is missing for '{s.name or s.id}'")
    meta["successor"] = successor
    meta.pop("handoff_owner_pid", None)
    atomic_write_json(s.meta_path, meta)
    now = time.time()
    # Handoff teardown may have used the ordinary dashboard `ending` guard.
    # Retirement supersedes that transient state and becomes the permanent
    # guard itself.
    s.ending_marker_path.unlink(missing_ok=True)
    write_status_path(s.status_path, "retired", now)
    s.status = "retired"
    s.status_ts = now
    s.successor = successor


def _require_dormant_codex_thread(thread_id: str) -> None:
    """Do not delete a shared rollout while any tracked writer may own it.

    Read persisted sessions directly, without correcting liveness or trusting
    this host's tmux/PIDs as evidence about a different host.
    """
    try:
        directories = list(STATE_DIR.iterdir())
    except FileNotFoundError:
        return
    for directory in directories:
        if not directory.is_dir():
            continue
        session = load_session(directory.name)
        if (session is None or session.runtime != RUNTIME_CODEX
                or session.runtime_session_id != thread_id):
            continue
        active = session.status not in ("ended", "retired")
        if not is_foreign_host(session):
            pid = _runtime_pid(session)
            active = active or tmux_session_exists(session.tmux_name) or bool(
                pid and _codex_runtime_pid_matches(session, pid)
            )
        if active:
            raise RuntimeError(
                f"Cannot delete Codex thread '{thread_id}': session "
                f"'{session.name or session.id}' on host "
                f"'{session.host or current_host()}' may still be running "
                f"(status: {session.status}). Stop it on its owning host first."
            )


def delete_codex_thread(thread_id: str, timeout_seconds: float = 12.0) -> None:
    """Permanently delete a dormant Codex thread through the app server."""
    if not thread_id:
        raise ValueError("Codex thread ID is required")
    _require_dormant_codex_thread(thread_id)
    # Reuse the interactive protocol client that powers Codex ask_agent.
    # Closing stdin immediately after a batch write can make the real app
    # server exit before it processes thread/delete.
    from .ask_agents import (
        _CodexProtocolError,
        _JsonLineProcess,
        _wait_for_response,
    )

    codex_bin = os.environ.get("MULTIVERSE_CODEX_BIN", "codex")
    deadline = time.monotonic() + timeout_seconds
    client: Optional[_JsonLineProcess] = None
    try:
        client = _JsonLineProcess(
            [codex_bin, "app-server", "--listen", "stdio://"],
            os.getcwd(),
            dict(os.environ),
        )
        client.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "multiverse", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        _wait_for_response(client, 1, deadline)
        client.send({"method": "initialized"})
        # App-server startup can take time; a remote may have resumed the
        # thread after the first check. Recheck just before the destructive RPC.
        _require_dormant_codex_thread(thread_id)
        client.send(
            {
                "id": 2,
                "method": "thread/delete",
                "params": {"threadId": thread_id},
            }
        )
        _wait_for_response(client, 2, deadline)
    except TimeoutError as exc:
        raise RuntimeError("Codex thread deletion timed out") from exc
    except (OSError, _CodexProtocolError) as exc:
        raise RuntimeError(f"Codex thread deletion failed: {exc}") from exc
    finally:
        if client is not None:
            client.close()

    _CODEX_METADATA_CACHE.clear()
    _CODEX_DISCOVERY_CACHE.clear()


def kill_session(s: Session, *, force: bool = False) -> None:
    """Tear down the owned runtime first, then tmux, then release state.

    A session belonging to another host is reaped as STATE ONLY: its state
    directory and name claims are released, and nothing local is signalled.
    Its recorded PID and tmux name describe processes on a machine we cannot
    reach, and a PID number is reusable — signalling one here would kill
    whatever local process happens to hold that number now. Local tmux cannot
    see a foreign session either, so there is nothing of its to kill here even
    in principle.

    ``force`` is the user's explicit "that remote is gone" decision, taken in
    the dashboard's force-kill confirmation. Without it, a foreign session
    whose durable status still reports it alive is refused rather than reaped,
    because its state dir is the only thing keeping a still-running remote
    agent visible — and reaping it while a replacement is created under the
    same name is exactly how an invisible zombie sharing one inbox is made.
    A foreign session whose own ``SessionEnd`` hook already wrote ``ended``
    needs no force: it really is over, wherever it ran.
    """
    if is_foreign_host(s):
        if not force and foreign_host_still_reported_live(s):
            raise RuntimeError(
                f"session {s.id} belongs to host {s.host} and its status there "
                "does not say ended; refusing to reap it from this host"
            )
        shutil.rmtree(s.dir, ignore_errors=True)
        _release_session_claims(s)
        return

    tmux_exists = tmux_session_exists(s.tmux_name)
    pane_runtime = _tmux_pane_runtime_group(s) if tmux_exists else None
    runtime_pid: Optional[int] = None
    runtime_pgid: Optional[int] = None
    candidate_pid = _runtime_pid(s) if s.runtime == RUNTIME_CODEX else None
    if candidate_pid and _codex_runtime_pid_matches(s, candidate_pid):
        try:
            candidate_pgid = os.getpgid(candidate_pid)
        except OSError:
            candidate_pgid = None
        if (
            candidate_pgid is not None
            and candidate_pgid > 1
            and candidate_pgid != os.getpgrp()
        ):
            runtime_pid = candidate_pid
            runtime_pgid = candidate_pgid

    try:
        if runtime_pid is not None and runtime_pgid is not None:
            # Prefer Codex's recorded child identity when available. Unlike the pane
            # fallback, this also handles an orphaned Codex writer after tmux vanished.
            _terminate_codex_runtime(s, runtime_pid, runtime_pgid)
        elif pane_runtime is not None:
            _terminate_tmux_pane_runtime(s, *pane_runtime)
    finally:
        # Always make the second teardown attempt, even if runtime termination raised.
        # In that case state remains intact and the caller still receives the error.
        if tmux_exists:
            subprocess.run(
                ["tmux", "kill-session", "-t", s.tmux_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    shutil.rmtree(s.dir, ignore_errors=True)
    _release_session_claims(s)
