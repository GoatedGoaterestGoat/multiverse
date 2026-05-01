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


def _multiverse_dir() -> Path:
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.multiverse"))


def _slug(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _live(uuid: str, cwd: str) -> Path:
    return Path.home() / ".claude" / "projects" / _slug(cwd) / f"{uuid}.jsonl"


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
    live = _live(uuid, cwd)
    if not live.is_file():
        return

    _full_dir().mkdir(parents=True, exist_ok=True)
    full = _full(uuid)

    if not full.exists():
        # Bootstrap: copy verbatim, set marker = last uuid.
        full.write_bytes(live.read_bytes())
        last = _last_uuid(full)
        if last:
            _marker(uuid).write_text(last)
        return

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
            dst.write(line if line.endswith("\n") else line + "\n")
            try:
                d = json.loads(stripped)
                u = d.get("uuid")
                if isinstance(u, str):
                    last_appended = u
            except json.JSONDecodeError:
                pass
    if last_appended and last_appended != marker:
        _marker(uuid).write_text(last_appended)


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
