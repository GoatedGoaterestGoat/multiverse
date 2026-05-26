#!/usr/bin/env python3
"""Hook + CLI: heal live JSONL chain breaks in place.

Wired to SessionStart. Walks the live JSONL forward and rewrites any
``parentUuid`` that points at a uuid not present anywhere earlier in
the file to point at the immediately-preceding uuid in file order.
That restores chain continuity from the tip back to the first row,
which is what claude code chain-walks on resume to display history.

When live's chain is broken, claude code chain-walks from the latest
tip back, hits a missing parent, stops there — the agent reports
seeing nothing of its prior conversation. After heal, every row is
reachable.

Idempotent: a well-formed file produces zero rewrites and the file
content is functionally unchanged (modulo whitespace normalization
from re-``json.dumps``). Run on every SessionStart, it's a free no-op
on files that are already fine.

Stdlib-only — fast startup, every SessionStart pays the import cost.

CLI usage (ad-hoc):
    heal-live-jsonl.py --session <uuid> --cwd <cwd>
    heal-live-jsonl.py --all-projects       # heal every live JSONL
                                              under $CLAUDE_CONFIG_DIR/projects
    heal-live-jsonl.py --dry-run            # report changes without
                                              writing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
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


def _slug(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _live_path(uuid: str, cwd: str) -> Path:
    return _claude_projects_dir() / _slug(cwd) / f"{uuid}.jsonl"


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


def heal_one(path: Path, dry_run: bool = False) -> tuple[int, int]:
    """Heal one live JSONL. Returns ``(rows_seen, parent_rewrites)``.

    On a well-formed file this is a no-op (zero rewrites, file
    untouched). When rewrites happen, the new content is written
    atomically via tempfile + ``os.replace``, with the original's
    file mode preserved.
    """
    if not path.is_file():
        return 0, 0

    rewrites = 0
    rows_seen = 0
    seen: set[str] = set()
    last_uuid: str | None = None
    out_lines: list[str] = []
    with path.open() as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                out_lines.append(line)
                continue
            rows_seen += 1
            try:
                d = json.loads(stripped)
            except json.JSONDecodeError:
                out_lines.append(line if line.endswith("\n") else line + "\n")
                continue
            parent = d.get("parentUuid")
            if (isinstance(parent, str) and parent
                    and parent not in seen
                    and last_uuid is not None):
                d["parentUuid"] = last_uuid
                rewrites += 1
            u = d.get("uuid")
            if isinstance(u, str):
                seen.add(u)
                last_uuid = u
            out_lines.append(json.dumps(d) + "\n")

    if dry_run or rewrites == 0:
        return rows_seen, rewrites

    orig_mode = path.stat().st_mode
    fd, tmp = tempfile.mkstemp(prefix=".heal_", suffix=".jsonl",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as out:
            out.writelines(out_lines)
        os.chmod(tmp, stat.S_IMODE(orig_mode))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rows_seen, rewrites


def _hook_mode(hook: dict) -> int:
    sid = hook.get("session_id")
    cwd = hook.get("cwd") or os.getcwd()
    if not sid:
        return 0
    live = _live_path(sid, cwd)
    if not live.is_file():
        # SessionStart fires before the live exists on first run; nothing
        # to heal yet. Silent no-op.
        return 0
    try:
        rows, rewrites = heal_one(live, dry_run=False)
    except OSError as e:
        sys.stderr.write(f"heal-live-jsonl: {e}\n")
        return 0
    if rewrites > 0:
        # Surface only when we actually did work — keeps the
        # SessionStart hook chain quiet for well-formed sessions.
        sys.stderr.write(
            f"heal-live-jsonl: healed {rewrites} broken parent pointer(s) "
            f"in {live.name} (rows={rows})\n"
        )
    return 0


def _cli_mode(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Heal chain breaks in a live JSONL.")
    p.add_argument("--session", help="claude_uuid of the session to heal")
    p.add_argument("--cwd",
                   help="cwd of the session (required with --session)")
    p.add_argument("--all-projects", action="store_true",
                   help="heal every live JSONL under $CLAUDE_CONFIG_DIR/"
                        "projects (every slug dir)")
    p.add_argument("--dry-run", action="store_true",
                   help="report rewrites without modifying files")
    args = p.parse_args(argv)

    targets: list[Path] = []
    if args.session:
        if not args.cwd:
            p.error("--cwd is required with --session")
        targets = [_live_path(args.session, args.cwd)]
    elif args.all_projects:
        root = _claude_projects_dir()
        if root.is_dir():
            for slug_dir in sorted(root.iterdir()):
                if slug_dir.is_dir():
                    targets.extend(sorted(slug_dir.glob("*.jsonl")))
    else:
        p.error("specify --session/--cwd or --all-projects")

    total_rewrites = 0
    for f in targets:
        if not f.is_file():
            print(f"  skip {f}: not a file")
            continue
        rows, rw = heal_one(f, args.dry_run)
        total_rewrites += rw
        verb = "DRY-RUN" if args.dry_run else ("rewrote" if rw else "ok")
        print(f"  {f.parent.name}/{f.stem[:8]}…  rows={rows:>5}  "
              f"parent_rewrites={rw:>4}  [{verb}]")
    verb = "would have rewritten" if args.dry_run else "rewrote"
    print(f"\n{verb} {total_rewrites} parent pointer(s) total")
    return 0


def main() -> int:
    # Dual-mode dispatch. Hook invocations have a JSON payload on stdin
    # and no CLI args; ad-hoc runs from a shell have CLI args (or
    # nothing, in which case argparse will print --help).
    if len(sys.argv) > 1:
        return _cli_mode(sys.argv[1:])
    hook = _read_input()
    if hook.get("session_id"):
        return _hook_mode(hook)
    # No args, no JSON payload — print help and exit.
    return _cli_mode(["--help"])


if __name__ == "__main__":
    sys.exit(main())
