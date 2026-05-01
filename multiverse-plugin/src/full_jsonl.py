"""Two-JSONL architecture: live (compactable) ↔ full (always-uncompacted).

Each claude session has:
  ~/.claude/projects/<slug>/<uuid>.jsonl  — LIVE: what claude reads/writes.
  ~/.multiverse/full_jsonls/<uuid>.jsonl   — FULL: append-only mirror, never
                                             compacted. Source of truth for
                                             tree building and total-token
                                             counts displayed at compaction
                                             time.
  ~/.multiverse/full_jsonls/<uuid>.marker  — uuid of the last live row that
                                             was mirrored to full. Used by
                                             the mirror to skip rows it has
                                             already copied. Tracking by
                                             uuid (not byte offset) so the
                                             marker survives compaction
                                             rewriting live.

Mirror sync (incremental):
  walk live.jsonl → skip rows up to and including <marker_uuid> → append
  the rest to full.jsonl → set marker = last appended row's uuid.

Bootstrap (legacy support):
  if full.jsonl is missing, copy live.jsonl verbatim and set
  marker = last uuid in the copy.

The fast-path mirror that runs as a hook on every UserPromptSubmit / Stop /
SessionEnd is implemented stdlib-only in scripts/mirror-jsonl.py to keep
hook-fire latency low. This module is the richer version used by the
compaction widget — it leans on `compaction.parser` for token/turn counts.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .paths import multiverse_dir

# Make the vendored compaction package importable. The compaction widget
# wants per-turn token counts and total turn counts; both live in the
# parser. We do the path injection at module import time so callers don't
# have to repeat it.
_VENDOR = str(Path(__file__).resolve().parent.parent / "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _full_dir() -> Path:
    return multiverse_dir() / "full_jsonls"


def full_jsonl_path(claude_uuid: str) -> Path:
    return _full_dir() / f"{claude_uuid}.jsonl"


def marker_path(claude_uuid: str) -> Path:
    return _full_dir() / f"{claude_uuid}.marker"


def cwd_slug(cwd: str) -> str:
    """Claude Code's project-slug rule: replace any non-alphanumeric with `-`."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def live_jsonl_path(claude_uuid: str, cwd: str) -> Path:
    return Path.home() / ".claude" / "projects" / cwd_slug(cwd) / f"{claude_uuid}.jsonl"


# ---------------------------------------------------------------------------
# Marker I/O
# ---------------------------------------------------------------------------

def read_marker(claude_uuid: str) -> str | None:
    try:
        v = marker_path(claude_uuid).read_text().strip()
        return v or None
    except OSError:
        return None


def write_marker(claude_uuid: str, uuid: str) -> None:
    p = marker_path(claude_uuid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(uuid)


def last_uuid_in(path: Path) -> str | None:
    """Return the uuid of the last row in a JSONL file (or None if empty)."""
    last: str | None = None
    try:
        with path.open() as f:
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
                    last = u
    except OSError:
        return None
    return last


# Backwards-compat alias for early callers that used the underscored name.
_last_uuid_in = last_uuid_in


# ---------------------------------------------------------------------------
# Sync + bootstrap
# ---------------------------------------------------------------------------

def sync_full(claude_uuid: str, cwd: str) -> int:
    """Sync live.jsonl into full.jsonl; bootstrap if missing.

    Returns the number of rows appended (0 means up-to-date or no live).
    """
    full_dir = _full_dir()
    full_dir.mkdir(parents=True, exist_ok=True)

    live = live_jsonl_path(claude_uuid, cwd)
    full = full_jsonl_path(claude_uuid)

    if not live.is_file():
        return 0

    if not full.exists():
        # Bootstrap: full is identical to live.
        full.write_bytes(live.read_bytes())
        last = _last_uuid_in(full)
        if last:
            write_marker(claude_uuid, last)
        return _row_count(full)

    marker = read_marker(claude_uuid)
    last_appended = marker
    found = marker is None
    appended = 0
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
            appended += 1
            try:
                d = json.loads(stripped)
                u = d.get("uuid")
                if isinstance(u, str):
                    last_appended = u
            except json.JSONDecodeError:
                pass
    if last_appended and last_appended != marker:
        write_marker(claude_uuid, last_appended)
    return appended


def _row_count(path: Path) -> int:
    n = 0
    try:
        with path.open() as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        pass
    return n


# ---------------------------------------------------------------------------
# Token / turn counts (from full.jsonl, syncing first)
# ---------------------------------------------------------------------------

def _parse_full(claude_uuid: str, cwd: str):
    sync_full(claude_uuid, cwd)
    full = full_jsonl_path(claude_uuid)
    if not full.exists():
        return []
    from compaction.parser import parse_session  # noqa: E402
    return parse_session(str(full))


def total_token_count(claude_uuid: str, cwd: str) -> int:
    """Sum every leaf token (including hoisted user prompts) across all
    turns in the FULL jsonl. This is the uncompacted cost the live agent
    would pay if no compaction were applied."""
    forest = _parse_full(claude_uuid, cwd)
    total = 0
    for turn in forest:
        if turn.user_prompt is not None:
            total += turn.user_prompt.tokens
        for leaf in turn.leaves:
            total += leaf.tokens
    return total


def total_turn_count(claude_uuid: str, cwd: str) -> int:
    return len(_parse_full(claude_uuid, cwd))


def per_turn_tokens(claude_uuid: str, cwd: str) -> list[tuple[str, int]]:
    """Return [(turn_id, total_tokens_in_turn), ...] from full.jsonl,
    where total_tokens_in_turn includes the user_prompt leaf."""
    out: list[tuple[str, int]] = []
    for turn in _parse_full(claude_uuid, cwd):
        t = turn.user_prompt.tokens if turn.user_prompt else 0
        t += sum(l.tokens for l in turn.leaves)
        out.append((turn.turn_id, t))
    return out
