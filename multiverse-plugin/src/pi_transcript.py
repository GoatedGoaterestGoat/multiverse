"""Native pi session discovery used by the mixed-runtime dashboard.

pi stores one JSONL per session.  The first ``session`` row identifies the
workspace and native session id; later ``session_info`` rows carry the current
``/name`` value.  This module intentionally handles discovery only.  pi owns
its transcript lifecycle and native compaction.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def pi_agent_dir() -> Path:
    value = os.environ.get("PI_CODING_AGENT_DIR")
    if value:
        return Path(value).expanduser()
    return Path("~/.pi/agent").expanduser()


def encode_pi_cwd(cwd: str) -> str:
    """Match pi's ``--<cwd with separators mapped to ->--`` directory.

    The path is fully resolved first. pi derives this name from its own
    process cwd, which the OS already reports symlink-free, so the resolved
    form is the only one that can match on disk. Canonicalizing here — the
    single boundary every session-dir derivation goes through — keeps
    ``pi_session_path`` and ``list_pi_sessions_in`` from computing different
    directories for the same workspace when the caller's path is a symlink,
    contains ``..``, or is ``~``-relative.
    """
    try:
        resolved = str(Path(cwd).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = str(Path(cwd).expanduser())
    stripped = re.sub(r"^[/\\]", "", resolved)
    return f"--{re.sub(r'[/\\:]', '-', stripped)}--"


def pi_sessions_dir(cwd: str) -> Path:
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if override:
        return Path(override).expanduser()
    return pi_agent_dir() / "sessions" / encode_pi_cwd(cwd)


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
    return ""


def read_pi_session_metadata(path: Path) -> dict[str, Any]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"id": None, "name": None, "cwd": None, "preview": ""}
    cached = _CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]

    native_id: Optional[str] = None
    name: Optional[str] = None
    cwd: Optional[str] = None
    preview: Optional[str] = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                typ = row.get("type")
                if typ == "session":
                    native_id = row.get("id") or native_id
                    cwd = row.get("cwd") or cwd
                elif typ == "session_info":
                    # Match pi's SessionManager: the latest session_info row is
                    # authoritative, and a missing/blank name explicitly clears
                    # the title. Keeping the previous value would make a name
                    # cleared with `/name` remain reserved in Multiverse.
                    value = row.get("name")
                    name = str(value).strip() if value else None
                elif typ == "message" and preview is None:
                    message = row.get("message") or {}
                    if message.get("role") != "user":
                        continue
                    text = _extract_text(message.get("content")).strip()
                    # Injected context blocks (<system-reminder>, and the
                    # inbox envelopes the extension sends) are not useful user
                    # previews. Only "<" is filtered: custom extension messages
                    # are separate pi entry types, so a normal user prompt
                    # beginning with "[" remains valid.
                    if text and not text.startswith("<"):
                        text = text.replace("\n", " ")
                        preview = text[:79] + "…" if len(text) > 80 else text
    except OSError:
        pass

    result = {
        "id": native_id,
        "name": name,
        "cwd": cwd,
        "preview": preview or "(no user message)",
    }
    _CACHE[str(path)] = (mtime, result)
    return result


def pi_session_path(session_id: str, cwd: str) -> Optional[Path]:
    directory = pi_sessions_dir(cwd)
    if not directory.is_dir():
        return None
    for path in directory.glob(f"*_{session_id}.jsonl"):
        return path
    # Custom/older session layouts may not include the timestamp prefix.
    direct = directory / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    for path in directory.glob("*.jsonl"):
        if read_pi_session_metadata(path).get("id") == session_id:
            return path
    return None


def list_pi_sessions_in(cwd: str, *, include_unnamed: bool = False) -> list[dict]:
    try:
        target_cwd = str(Path(cwd).expanduser().resolve())
    except (OSError, RuntimeError):
        return []
    directory = pi_sessions_dir(target_cwd)
    if not directory.is_dir():
        return []
    flat_override = bool(os.environ.get("PI_CODING_AGENT_SESSION_DIR"))
    sessions: list[dict] = []
    for path in directory.glob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        meta = read_pi_session_metadata(path)
        recorded_cwd = meta.get("cwd")
        if flat_override and recorded_cwd:
            try:
                if str(Path(recorded_cwd).expanduser().resolve()) != target_cwd:
                    continue
            except (OSError, RuntimeError):
                continue
        name = meta.get("name")
        if not include_unnamed and not name:
            continue
        native_id = meta.get("id") or path.stem.split("_", 1)[-1]
        sessions.append(
            {
                "id": str(native_id),
                "name": name,
                "cwd": recorded_cwd or target_cwd,
                "preview": meta.get("preview") or "(no user message)",
                "mtime": st.st_mtime,
                "path": str(path),
            }
        )
    sessions.sort(key=lambda item: item["mtime"], reverse=True)
    return sessions


def delete_pi_session(session_id: str, cwd: str) -> None:
    path = pi_session_path(session_id, cwd)
    if path is None:
        raise FileNotFoundError(f"pi session '{session_id}' was not found")
    path.unlink()
    _CACHE.pop(str(path), None)
