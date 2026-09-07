"""Fresh ownership checks for local Codex TUI operations."""

from __future__ import annotations

import json
import re

from . import sessions
from .atomic_files import atomic_create_json, atomic_write_json

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")
_DRAFT_FILE = "codex_wake_draft.json"


def local_session(tmux_name: str, *, startup: bool = False) -> sessions.Session | None:
    """Reload ownership before touching a pane; missing metadata fails closed.

    Never use local tmux visibility to override a foreign host identity. The
    same tmux name (or PID) can exist independently on two hosts.
    """
    if not tmux_name.startswith(sessions.TMUX_PREFIX):
        return None
    session_id = tmux_name[len(sessions.TMUX_PREFIX):]
    if not _SESSION_ID.fullmatch(session_id):
        return None
    try:
        session = sessions.load_session(session_id)
        if session is None or session.runtime != sessions.RUNTIME_CODEX:
            return None
        if sessions.is_foreign_host(session):
            return None
        allowed = ("starting", "idle", "working") if startup else ("idle", "working")
        if session.status not in allowed or session.ending_marker_path.exists():
            return None
        return session
    except (OSError, ValueError, TypeError):
        return None


def pending_draft(session: sessions.Session) -> dict | None:
    """A displaced draft awaiting restoration; malformed records fail closed."""
    try:
        record = json.loads((session.dir / _DRAFT_FILE).read_text())
    except FileNotFoundError:
        return None
    if (not isinstance(record, dict)
            or record.get("session_id") != session.id
            or record.get("created_at") != session.created_at
            or not isinstance(record.get("text"), str)
            or type(record.get("submitted")) is not bool):
        raise ValueError("Invalid Codex wake draft recovery record")
    return record


def save_draft(session: sessions.Session, text: str) -> bool:
    """Persist before clearing the composer; never replace an unresolved draft."""
    return atomic_create_json(session.dir / _DRAFT_FILE, {
        "session_id": session.id, "created_at": session.created_at,
        "text": text, "submitted": False,
    }, mode=0o600)


def mark_draft_submitted(session: sessions.Session) -> None:
    record = pending_draft(session)
    if record is not None:
        record["submitted"] = True
        atomic_write_json(session.dir / _DRAFT_FILE, record, mode=0o600)


def clear_draft(session: sessions.Session) -> None:
    (session.dir / _DRAFT_FILE).unlink(missing_ok=True)
