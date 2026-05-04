#!/usr/bin/env python3
"""Heal existing ``full.jsonl`` files in place.

Mirror used to append rows verbatim, including their original
``parentUuid``. Across a compaction boundary the previous live tip is
a synthetic uuid (written by ``compact_forest_to_session_jsonl``) that
never lived in full, so the post-compaction rows mirrored into full
have a parent pointer to a uuid not present in the file. Chain-walking
from the latest tip stops at the broken pointer, leaving CompactScreen
showing only the post-compaction sub-conversation.

This script walks each full.jsonl forward, tracks every uuid seen so
far, and rewrites any ``parentUuid`` that points outside the seen set
to the last-seen uuid in file order. The result is a single contiguous
chain that chain-walks back to the file's first row.

Idempotent — if the chain is already intact, the file's contents are
unchanged.

Usage:
    python3 multiverse-plugin/scripts/heal-full-jsonl.py [--root PATH]
        [--session UUID]   # operate on one session only
        [--dry-run]        # report changes without writing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def heal_one(path: Path, dry_run: bool) -> tuple[int, int]:
    """Heal one full.jsonl. Returns (rows_seen, parent_rewrites)."""
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

    # Atomic rewrite: tmp in same dir + rename.
    fd, tmp = tempfile.mkstemp(prefix=".heal_", suffix=".jsonl",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(out_lines)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rows_seen, rewrites


def _multiverse_dir() -> Path:
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.multiverse"))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=None,
                   help="$MULTIVERSE_ROOT override")
    p.add_argument("--session", default=None,
                   help="heal only this claude_uuid (default: all)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    root = Path(args.root) if args.root else _multiverse_dir()
    full_dir = root / "full_jsonls"
    if not full_dir.is_dir():
        print(f"no full_jsonls dir at {full_dir}", file=sys.stderr)
        return 1

    if args.session:
        targets = [full_dir / f"{args.session}.jsonl"]
    else:
        targets = sorted(full_dir.glob("*.jsonl"))

    total_rewrites = 0
    for f in targets:
        if not f.is_file():
            print(f"skip {f.name}: not a file")
            continue
        rows, rw = heal_one(f, args.dry_run)
        total_rewrites += rw
        marker = "DRY-RUN" if args.dry_run else ("rewrote" if rw else "ok")
        print(f"  {f.stem[:8]}  rows={rows:>5}  parent_rewrites={rw:>4}  [{marker}]")
    print(f"\n{'would have rewritten' if args.dry_run else 'rewrote'} "
          f"{total_rewrites} parent pointer(s) total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
