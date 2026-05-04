#!/usr/bin/env python3
"""UserPromptSubmit hook: kick off a detached background tree-builder worker.

When the user submits a prompt, this hook spawns a detached worker that
builds compaction trees (via `compaction/scripts/build_tree_priv_cli.py`)
for every turn of the current session EXCEPT the in-progress one the
user just submitted. Trees are cached at
~/.multiverse/trees/<session_id>/<turn_id>.json so subsequent fires are
incremental — only newly-completed turns get built.

Stdlib-only and non-blocking: the hook reads the input, checks the
recursion sentinel and the per-session lockfile, and Popen-s the worker
with `start_new_session=True`. Total tax per fire is dominated by Python
startup (~30ms).

Recursion guard: if MULTIVERSE_TREE_BUILDER_ACTIVE is set in env, the
hook is a no-op. The worker sets that env var before invoking
`claude --resume <synthetic>` for each merge step, so the synthetic
sub-claude (which might inherit the multiverse plugin if it's installed
at user scope) won't re-enter this hook.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


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


def _cwd_slug(cwd: str) -> str:
    return cwd.replace("/", "-")


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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> None:
    if os.environ.get("MULTIVERSE_TREE_BUILDER_ACTIVE"):
        return  # inside a worker subprocess; do not recurse

    hook = _read_input()
    session_id = hook.get("session_id")
    cwd = hook.get("cwd") or os.getcwd()
    if not session_id:
        return

    source = (
        _claude_projects_dir() / _cwd_slug(cwd) / f"{session_id}.jsonl"
    )
    if not source.is_file():
        return  # session not persisted yet (e.g. very first prompt)

    cache_root = _multiverse_dir() / "trees" / session_id
    cache_root.mkdir(parents=True, exist_ok=True)

    lockfile = cache_root / ".worker.lock"
    if lockfile.exists():
        try:
            pid = int(lockfile.read_text().strip())
            if _alive(pid):
                return  # a worker is already chewing on this session
        except (ValueError, OSError):
            pass  # stale or unreadable — overwrite below

    plugin_root = Path(
        os.environ.get("CLAUDE_PLUGIN_ROOT")
        or Path(__file__).resolve().parent.parent
    )
    worker = plugin_root / "scripts" / "tree-builder-worker.py"
    if not worker.is_file():
        return

    log_path = cache_root / "worker.log"
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(worker), str(source), session_id, cwd],
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )
    finally:
        log_f.close()

    try:
        lockfile.write_text(str(proc.pid))
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the user's prompt
        sys.stderr.write(f"spawn-tree-builder: {e}\n")
        sys.exit(0)
