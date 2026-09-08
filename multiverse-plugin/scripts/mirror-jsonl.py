#!/usr/bin/env python3
"""Hook: mirror live JSONL → full JSONL (the always-uncompacted history).

Wired to UserPromptSubmit, Stop, SessionEnd. Appends complete, unconsumed
records using the shared src.mirror_sync implementation. Its validated byte
cursor includes UUID-less trailing metadata, preventing repeated hook fires
from duplicating it. An unchanged live file requires no JSONL reads.
Bootstraps by copying complete live records verbatim.

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

from src.atomic_files import exclusive_file_lock  # noqa: E402


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


def _sync(uuid: str, cwd: str) -> None:
    lock = _full_dir() / f".{uuid}.sync.lock"
    with exclusive_file_lock(lock):
        _sync_unlocked(uuid, cwd)


def _sync_unlocked(uuid: str, cwd: str) -> None:
    from src.mirror_sync import sync_mirror
    sync_mirror(_live(uuid, cwd), _full(uuid), _marker(uuid))


def main() -> None:
    # Never mirror a tree-build merge subprocess. The constructor spawns
    # `claude --resume <uuid> -p <instruction>` for each merge step; that
    # subprocess gets its OWN fresh claude session_id (custom-title
    # "priv_compaction") and inherits this plugin, so its hooks fire with
    # the PARENT session's MULTIVERSE_* env. Without this guard we create a
    # junk full_jsonls/<priv_compaction_uuid>.jsonl holding just the merge
    # prompt — which surfaces on the dashboard as the agent's full history
    # collapsing to a single turn. Same env flag spawn-tree-builder.py
    # already uses as its recursion guard.
    if os.environ.get("MULTIVERSE_TREE_BUILDER_ACTIVE"):
        return
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
