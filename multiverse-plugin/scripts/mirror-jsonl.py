#!/usr/bin/env python3
"""Hook: mirror live JSONL → full JSONL (the always-uncompacted history).

Wired to UserPromptSubmit, Stop, SessionEnd. Each fire walks the live
JSONL, skips rows up to and including the marker uuid (last row already
mirrored), and appends every subsequent row to full.jsonl. Bootstraps
on first call: if full.jsonl is missing, the live JSONL is copied
verbatim and the marker is set to its last uuid.

Marker is tracked by uuid (not byte offset) so the mirror keeps working
after compaction has rewritten live.jsonl with a different prefix and
possibly fewer rows. Sentinel: if we don't find marker_uuid in live (the
live was compacted such that the boundary was rewritten), we append
nothing and wait for the compaction widget to update the marker.

Stdlib-only on purpose — invoked per hook fire, so we keep the import
cost down.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from src.atomic_files import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_text,
    exclusive_file_lock,
)


def _multiverse_dir() -> Path:
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.multiverse"))


def _claude_projects_dir() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "projects"
    return Path(os.path.expanduser("~/.claude/projects"))


def _slug(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _live(uuid: str, cwd: str) -> Path:
    return _claude_projects_dir() / _slug(cwd) / f"{uuid}.jsonl"


def _full_dir() -> Path:
    return _multiverse_dir() / "full_jsonls"


def _full(uuid: str) -> Path:
    return _full_dir() / f"{uuid}.jsonl"


def _marker(uuid: str) -> Path:
    return _full_dir() / f"{uuid}.marker"


def _read_input() -> dict:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _last_uuid(path: Path) -> str | None:
    last = None
    try:
        with path.open() as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    d = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                u = d.get("uuid")
                if isinstance(u, str):
                    last = u
    except OSError:
        return None
    return last


def _sync(uuid: str, cwd: str) -> None:
    lock = _full_dir() / f".{uuid}.sync.lock"
    with exclusive_file_lock(lock):
        _sync_unlocked(uuid, cwd)


def _sync_unlocked(uuid: str, cwd: str) -> None:
    live = _live(uuid, cwd)
    if not live.is_file():
        return

    _full_dir().mkdir(parents=True, exist_ok=True)
    full = _full(uuid)

    if not full.exists():
        # Bootstrap: copy verbatim, set marker = last uuid.
        atomic_write_bytes(full, live.read_bytes())
        last = _last_uuid(full)
        if last:
            atomic_write_text(_marker(uuid), last)
        return

    # Load full's existing uuids so we can detect (and heal)
    # parentUuid pointers that would otherwise leave full's chain
    # broken — e.g. across a compaction boundary, where the previous
    # live tip is a synthetic uuid that never made it into full.
    full_uuids: set[str] = set()
    full_last_uuid: str | None = None
    try:
        with full.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = d.get("uuid")
                if isinstance(u, str):
                    full_uuids.add(u)
                    full_last_uuid = u
    except OSError:
        pass

    try:
        marker = _marker(uuid).read_text().strip() or None
    except OSError:
        marker = None

    last_appended = marker
    found = marker is None
    with live.open() as src, full.open("a") as dst:
        for line in src:
            stripped = line.strip()
            if not stripped:
                continue
            if not found:
                try:
                    d = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if d.get("uuid") == marker:
                    found = True
                continue
            try:
                d = json.loads(stripped)
            except json.JSONDecodeError:
                # Not JSON — append verbatim and skip parentUuid healing.
                dst.write(line if line.endswith("\n") else line + "\n")
                continue
            # Heal parentUuid: if the parent isn't already in full,
            # repoint to full's current tail so chain-walking can reach
            # this row.
            parent = d.get("parentUuid")
            if isinstance(parent, str) and parent and parent not in full_uuids:
                d["parentUuid"] = full_last_uuid
            u = d.get("uuid")
            if isinstance(u, str):
                full_uuids.add(u)
                full_last_uuid = u
                last_appended = u
            dst.write(json.dumps(d) + "\n")
    if last_appended and last_appended != marker:
        atomic_write_text(_marker(uuid), last_appended)


def main() -> None:
    hook = _read_input()
    sid = hook.get("session_id")
    cwd = hook.get("cwd") or os.getcwd()
    if not sid:
        return
    _sync(sid, cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the user's prompt
        sys.stderr.write(f"mirror-jsonl: {e}\n")
        sys.exit(0)
