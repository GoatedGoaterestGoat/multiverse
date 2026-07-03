#!/usr/bin/env python3
"""Sync cached-tree leaf tokens (and images) from a fresh parse of the
full JSONL. Fixes two issues in one pass:

  1. Cached trees built pre-adba1a8 have no ``images`` field on their
     leaves at all — the parser didn't track image blocks yet, so
     image tool_results were stored with ``tokens=0``.
  2. Cached trees built between adba1a8 and the (W×H)/750 fix (commit
     49b353f) have images stored but with wildly inflated tokens
     computed via the old ``len(base64_data) / 1.44`` heuristic.

Approach: for each cached tree, fresh-parse the full JSONL, match
each cached Leaf to its fresh counterpart by ``source_uuids`` (with a
positional fallback), and copy over ``tokens`` and ``images``.
Everything else — tree structure, Node summaries, merge history — is
untouched. ``Node.delta`` regenerates on load (it's ``init=False`` on
the dataclass and computed in ``__post_init__`` from child tokens).

Because the fix reads from fresh parse and writes the same value each
run, it's idempotent by construction — no schema-version marker, no
per-leaf heuristic guard, no risk of double-migration corrupting a
previously-migrated tree.

Usage:
    python3 multiverse-plugin/scripts/rewrite-image-tokens.py --session <uuid>
    python3 multiverse-plugin/scripts/rewrite-image-tokens.py --all
    python3 multiverse-plugin/scripts/rewrite-image-tokens.py --all --dry-run

The summaries in cached trees are NOT re-generated. That's safe
because the pre-adba1a8 constructor's step 0 already passed source
rows verbatim to the merge agent (per adba1a8's commit message:
"Step 0 already passed source rows verbatim"), so image content
propagated into the earliest text summaries and rode up through
later merges as prose.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN / "vendor"))

from compaction.parser import parse_session  # noqa: E402


def _multiverse_dir() -> Path:
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.multiverse"))


def _sync_leaf(cached: dict, fresh) -> tuple[bool, int]:
    """Overwrite cached leaf's ``images`` and ``tokens`` from fresh.
    Returns (changed, tokens_delta)."""
    if not isinstance(cached, dict):
        return False, 0
    changed = False
    fresh_images = list(fresh.images or [])
    fresh_tokens = int(fresh.tokens)

    # Normalize both sides: missing key and explicit ``[]`` both mean
    # "no images", so they should compare equal.
    cached_images = cached.get("images") or []
    if cached_images != fresh_images:
        # Only write the field when non-empty — keeps the JSON small
        # for the many text-only leaves that will never have images.
        if fresh_images:
            cached["images"] = fresh_images
        elif "images" in cached:
            cached.pop("images", None)
        changed = True

    old_tokens = cached.get("tokens")
    if not isinstance(old_tokens, int) or old_tokens != fresh_tokens:
        cached["tokens"] = fresh_tokens
        # tokens_delta reports change vs. what was on disk. If the field
        # was missing or non-int, treat as if it was 0 for reporting.
        prev = old_tokens if isinstance(old_tokens, int) else 0
        return True, fresh_tokens - prev

    return changed, 0


def _build_uuid_map(turn) -> dict:
    """Map source-uuid → fresh Leaf, covering user_prompt and every
    assistant-side leaf."""
    out: dict = {}
    if turn.user_prompt:
        for u in turn.user_prompt.source_uuids or []:
            out[u] = turn.user_prompt
    for leaf in turn.leaves:
        for u in leaf.source_uuids or []:
            out[u] = leaf
    return out


def _find_fresh(cached_leaf: dict, uuid_map: dict, positional):
    """Match a cached leaf to a fresh Leaf. Prefer source-uuid match;
    fall back to the positional Leaf if the cache carries no uuids
    (some very old serializer output)."""
    for u in cached_leaf.get("source_uuids") or []:
        if u in uuid_map:
            return uuid_map[u]
    return positional


def _migrate_one_file(path: Path, fresh_turn, dry_run: bool) -> dict:
    stats = {
        "leaves_seen": 0,
        "leaves_patched": 0,
        "tokens_delta": 0,
        "error": None,
        "unmatched_leaves": 0,
    }
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        stats["error"] = str(e)
        return stats

    uuid_map = _build_uuid_map(fresh_turn)
    file_changed = False

    # 1. user_prompt (top-level, leaf-shape).
    up = data.get("user_prompt")
    if isinstance(up, dict) and fresh_turn.user_prompt:
        stats["leaves_seen"] += 1
        changed, delta = _sync_leaf(up, fresh_turn.user_prompt)
        if changed:
            stats["leaves_patched"] += 1
            stats["tokens_delta"] += delta
            file_changed = True

    # 2. Flat ``leaves`` list — positional pairing with fresh_turn.leaves,
    # with source-uuid tiebreak for safety.
    flat = data.get("leaves") or []
    for i, cl in enumerate(flat):
        if not isinstance(cl, dict):
            continue
        stats["leaves_seen"] += 1
        positional = (
            fresh_turn.leaves[i] if i < len(fresh_turn.leaves) else None
        )
        fresh = _find_fresh(cl, uuid_map, positional)
        if fresh is None:
            stats["unmatched_leaves"] += 1
            continue
        changed, delta = _sync_leaf(cl, fresh)
        if changed:
            stats["leaves_patched"] += 1
            stats["tokens_delta"] += delta
            file_changed = True

    # 3. Recursive tree walk. Each Leaf inside carries source_uuids so
    # uuid-map lookup works; positional fallback unused here since
    # there's no meaningful global index.
    def walk(node):
        nonlocal file_changed
        if not isinstance(node, dict):
            return
        t = node.get("type")
        if t == "leaf":
            stats["leaves_seen"] += 1
            fresh = _find_fresh(node, uuid_map, None)
            if fresh is None:
                stats["unmatched_leaves"] += 1
                return
            changed, delta = _sync_leaf(node, fresh)
            if changed:
                stats["leaves_patched"] += 1
                stats["tokens_delta"] += delta
                file_changed = True
        elif t == "node":
            for c in node.get("children") or []:
                walk(c)

    tree = data.get("tree")
    if isinstance(tree, dict):
        walk(tree)

    if not file_changed or dry_run:
        return stats

    fd, tmp = tempfile.mkstemp(prefix=".rewrite_", suffix=".json",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return stats


def _migrate_session(session_uuid: str, dry_run: bool) -> tuple[int, int, int]:
    """Returns (files_touched, leaves_patched, tokens_delta)."""
    cache_root = _multiverse_dir() / "trees" / session_uuid
    full_path = _multiverse_dir() / "full_jsonls" / f"{session_uuid}.jsonl"
    if not cache_root.is_dir():
        print(f"  {session_uuid[:8]}: no cache dir at {cache_root}")
        return 0, 0, 0
    if not full_path.is_file():
        print(
            f"  {session_uuid[:8]}: no full.jsonl at {full_path}; "
            f"cannot sync (nothing to sync FROM)"
        )
        return 0, 0, 0

    try:
        forest = parse_session(str(full_path))
    except Exception as e:  # noqa: BLE001
        print(f"  {session_uuid[:8]}: parse_session failed: {e}")
        return 0, 0, 0
    turns_by_id = {t.turn_id: t for t in forest}

    trees = sorted(cache_root.glob("*.json"))
    if not trees:
        print(f"  {session_uuid[:8]}: no tree files")
        return 0, 0, 0

    total_leaves = total_patched = total_delta = 0
    total_unmatched = 0
    n_touched = 0
    n_no_matching_turn = 0
    for p in trees:
        turn_id = p.stem
        fresh_turn = turns_by_id.get(turn_id)
        if fresh_turn is None:
            n_no_matching_turn += 1
            continue
        s = _migrate_one_file(p, fresh_turn, dry_run)
        if s["error"]:
            print(f"    {p.name[:12]}…: {s['error']}")
            continue
        total_leaves += s["leaves_seen"]
        total_patched += s["leaves_patched"]
        total_delta += s["tokens_delta"]
        total_unmatched += s["unmatched_leaves"]
        if s["leaves_patched"] > 0:
            n_touched += 1

    verb = "would patch" if dry_run else "patched"
    extra = ""
    if n_no_matching_turn:
        extra += (
            f", {n_no_matching_turn} tree(s) skipped (turn_id not in "
            f"current chain — stale from earlier revert state)"
        )
    if total_unmatched:
        extra += (
            f", {total_unmatched} leaf/leaves in cache had no matching "
            f"fresh leaf (source_uuids diverged)"
        )
    print(
        f"  {session_uuid[:8]}: {len(trees)} tree file(s), "
        f"{total_leaves:,} leaves; {verb} {total_patched:,} leaf(s) "
        f"across {n_touched} file(s) ({total_delta:+,} tokens delta)"
        f"{extra}"
    )
    return n_touched, total_patched, total_delta


def main() -> int:
    p = argparse.ArgumentParser()
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--session", help="claude_uuid to migrate")
    grp.add_argument("--all", action="store_true",
                     help="migrate every session in $MULTIVERSE_ROOT/trees/")
    p.add_argument("--dry-run", action="store_true",
                   help="report changes without writing")
    args = p.parse_args()

    root = _multiverse_dir() / "trees"
    if not root.is_dir():
        print(f"no trees dir at {root}", file=sys.stderr)
        return 1

    if args.session:
        _migrate_session(args.session, args.dry_run)
    else:
        totals = [0, 0, 0]
        for sess_dir in sorted(root.iterdir()):
            if sess_dir.is_dir():
                t = _migrate_session(sess_dir.name, args.dry_run)
                for i, v in enumerate(t):
                    totals[i] += v
        verb = "would patch" if args.dry_run else "patched"
        print(
            f"\nGrand total: {verb} {totals[1]:,} leaves across "
            f"{totals[0]} file(s), {totals[2]:+,} tokens delta."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())