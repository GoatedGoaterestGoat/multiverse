#!/usr/bin/env python3
"""Delete cached compaction trees for turns that contain content whose
extraction changed after commits 3a2ce55 (queued_command) and 077b1b0
(inter-agent hook_additional_context).

Why: those fixes added new Leaf-producing branches to
``parser._classify_and_extract`` for attachment rows that were
previously dropped by ``SKIP_TYPES``. Trees built BEFORE the fixes
were summarized without ever seeing that content — the summarizing
LLM didn't have queued_command prompts or inter-agent messages in
its input, so the resulting Node summaries don't reflect them. Unlike
the leaf.tokens correction rewrite-image-tokens.py handles, this
isn't a patch-in-place: the missing content was never in the tree
JSON in any form. Only remedy is a rebuild with the corrected parser.

This script finds affected turns and deletes their cached tree
files. The next compaction run's tree-builder-worker is
cache-aware — it'll only rebuild the turns whose files are gone.

Detection: parse each session's full.jsonl with the CURRENT parser
(so the corrected _classify_and_extract runs) and record turn_ids
that contain at least one Leaf sourced from a queued_command
attachment or an inter-agent hook_additional_context. The lightweight
way to identify those leaves without adding a new Leaf.kind is to
re-scan the raw JSONL and cross-reference source_uuids of user_text
leaves against attachment-row uuids of the two relevant kinds.

Usage:
    python3 multiverse-plugin/scripts/rebuild-affected-trees.py --session <uuid>
    python3 multiverse-plugin/scripts/rebuild-affected-trees.py --all
    python3 multiverse-plugin/scripts/rebuild-affected-trees.py --all --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN / "vendor"))

from compaction.parser import parse_session  # noqa: E402


def _multiverse_dir() -> Path:
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.multiverse"))


def _find_affected_source_uuids(full_path: Path) -> set[str]:
    """Scan the raw JSONL for attachment rows whose extraction changed.
    Returns the set of row uuids for queued_command and inter-agent
    hook_additional_context rows on chain (chain filtering happens in
    parse_session; we're conservative here and include all such rows,
    then intersect with what the parser actually surfaced)."""
    affected: set[str] = set()
    try:
        with full_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "attachment":
                    continue
                att = d.get("attachment")
                if not isinstance(att, dict):
                    continue
                att_type = att.get("type")
                uuid = d.get("uuid") or ""
                if not uuid:
                    continue
                if att_type == "queued_command":
                    affected.add(uuid)
                elif att_type == "hook_additional_context":
                    content = att.get("content")
                    if isinstance(content, list):
                        joined = "\n".join(
                            c if isinstance(c, str) else str(c)
                            for c in content
                        )
                    elif isinstance(content, str):
                        joined = content
                    else:
                        joined = ""
                    if "[Multiverse Inter-Agent]" in joined:
                        affected.add(uuid)
    except OSError:
        pass
    return affected


def _affected_turn_ids(
    full_path: Path,
) -> tuple[list[str], int, int]:
    """Return (turn_ids_affected, n_affected_leaves, total_turns).

    A turn is affected if the parser now emits at least one Leaf
    whose ``source_uuids`` intersect the set of attachment-row uuids
    we care about — meaning that leaf was previously dropped, so the
    turn's cached tree summarized without that content.
    """
    affected_uuids = _find_affected_source_uuids(full_path)
    if not affected_uuids:
        return [], 0, 0
    try:
        forest = parse_session(str(full_path))
    except Exception:  # noqa: BLE001
        return [], 0, len(affected_uuids) and 0
    turn_ids: list[str] = []
    n_leaves = 0
    for t in forest:
        touched = False
        for leaf in t.leaves:
            if any(u in affected_uuids for u in leaf.source_uuids or []):
                touched = True
                n_leaves += 1
        if touched:
            turn_ids.append(t.turn_id)
    return turn_ids, n_leaves, len(forest)


def _process_session(session_uuid: str, dry_run: bool) -> tuple[int, int]:
    """Returns (turn_files_removed, affected_leaves_seen)."""
    root = _multiverse_dir()
    full = root / "full_jsonls" / f"{session_uuid}.jsonl"
    cache_root = root / "trees" / session_uuid
    if not full.is_file():
        print(f"  {session_uuid[:8]}: no full.jsonl at {full}")
        return 0, 0
    if not cache_root.is_dir():
        print(f"  {session_uuid[:8]}: no cache dir at {cache_root}")
        return 0, 0

    turn_ids, n_leaves, n_turns = _affected_turn_ids(full)
    if not turn_ids:
        print(f"  {session_uuid[:8]}: {n_turns} turns, no affected content")
        return 0, 0

    removed = 0
    still_missing = 0
    for turn_id in turn_ids:
        tree_path = cache_root / f"{turn_id}.json"
        if not tree_path.is_file():
            still_missing += 1
            continue
        if dry_run:
            continue
        try:
            tree_path.unlink()
            removed += 1
        except OSError as e:
            print(f"    {session_uuid[:8]}: failed to unlink "
                  f"{turn_id[:8]}: {e}")

    verb = "would remove" if dry_run else "removed"
    extra = ""
    if still_missing:
        extra = (
            f", {still_missing} affected turn(s) had no cached tree "
            f"(will be built fresh)"
        )
    print(
        f"  {session_uuid[:8]}: {n_turns} turns, "
        f"{len(turn_ids)} affected ({n_leaves} affected leaves); "
        f"{verb} {len(turn_ids) - still_missing if dry_run else removed} "
        f"tree file(s){extra}"
    )
    if dry_run:
        return len(turn_ids) - still_missing, n_leaves
    return removed, n_leaves


def main() -> int:
    p = argparse.ArgumentParser()
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--session", help="claude_uuid to process")
    grp.add_argument("--all", action="store_true",
                     help="process every session in $MULTIVERSE_ROOT/trees/")
    p.add_argument("--dry-run", action="store_true",
                   help="report affected trees without deleting")
    args = p.parse_args()

    root = _multiverse_dir() / "trees"
    if not root.is_dir():
        print(f"no trees dir at {root}", file=sys.stderr)
        return 1

    if args.session:
        _process_session(args.session, args.dry_run)
    else:
        tot_removed = 0
        tot_leaves = 0
        for sess_dir in sorted(root.iterdir()):
            if sess_dir.is_dir():
                r, l = _process_session(sess_dir.name, args.dry_run)
                tot_removed += r
                tot_leaves += l
        verb = "Would remove" if args.dry_run else "Removed"
        print(
            f"\nGrand total: {verb} {tot_removed} tree file(s) "
            f"across all sessions ({tot_leaves} previously-invisible "
            f"leaves will be visible after rebuild)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
