#!/usr/bin/env python3
"""One-shot migration: rewrite image-leaf ``tokens`` in cached trees.

Applied after fixing count_image_tokens (commit 49b353f) so cached trees
carry correct image sizing.

Approach: for each leaf that HAS a non-empty ``images`` list, set
``tokens = count_tokens(content.text) + Σ count_image_tokens(image)``
using the current (post-fix) tokenize.py. This is naturally idempotent —
running twice yields the same result — so no schema markers or heuristic
guards are needed. Text tokens follow the parser's convention
(``count_tokens`` of the content text), which is what the parser used
originally for text-plus-image leaves, so recomputing text does not
change it.

Leaves without an ``images`` field or with an empty list are left
untouched. Trees built before image-tracking existed (before commit
adba1a8) fall in that bucket — their image blocks were never stored,
so there is nothing here to recompute from. Those need a full rebuild
(delete + re-run compaction so the ``--all-turns`` worker rebuilds).

No structural changes to the tree JSON. Node.tokens (summary text) and
Node.delta are untouched. Delta regenerates on load in Node.__post_init__.

Usage:
    python3 multiverse-plugin/scripts/rewrite-image-tokens.py --session <uuid>
    python3 multiverse-plugin/scripts/rewrite-image-tokens.py --all
    python3 multiverse-plugin/scripts/rewrite-image-tokens.py --all --dry-run
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

from compaction.tokenize import count_image_tokens, count_tokens  # noqa: E402


def _multiverse_dir() -> Path:
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.multiverse"))


def _patch_leaf(leaf: dict, stats: dict) -> None:
    """Set ``leaf["tokens"]`` to the correct value for image-bearing
    leaves. Idempotent by construction — computes the answer from the
    leaf's own text + images, so running twice yields the same result.
    Leaves without an ``images`` field or with an empty images list are
    left untouched; we can't retroactively count what isn't stored."""
    if not isinstance(leaf, dict):
        return
    if leaf.get("type") != "leaf":
        return
    stats["leaves_seen"] += 1
    images = leaf.get("images") or []
    if not images:
        return
    old_tokens = leaf.get("tokens")
    if not isinstance(old_tokens, int):
        return
    content = leaf.get("content")
    text = ""
    if isinstance(content, dict):
        t = content.get("text")
        if isinstance(t, str):
            text = t
    new_tokens = max(1, count_tokens(text) + sum(
        count_image_tokens(img) for img in images
    ))
    if new_tokens != old_tokens:
        leaf["tokens"] = new_tokens
        stats["leaves_patched"] += 1
        stats["tokens_delta"] += new_tokens - old_tokens


def _walk_tree(node: dict, stats: dict) -> None:
    """Recurse a serialized tree. Node children can be Nodes or Leaves."""
    if not isinstance(node, dict):
        return
    t = node.get("type")
    if t == "leaf":
        _patch_leaf(node, stats)
    elif t == "node":
        stats["nodes_seen"] += 1
        for c in node.get("children") or []:
            _walk_tree(c, stats)


def _migrate_one_file(path: Path, dry_run: bool) -> dict:
    stats = {
        "leaves_seen": 0,
        "leaves_patched": 0,
        "nodes_seen": 0,
        "tokens_delta": 0,
        "error": None,
    }
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        stats["error"] = str(e)
        return stats

    # user_prompt is stored in leaf-shape at the top level.
    up = data.get("user_prompt")
    if isinstance(up, dict):
        _patch_leaf(up, stats)

    # Flat leaves list (raw per-row leaves for this turn).
    for leaf in data.get("leaves") or []:
        _patch_leaf(leaf, stats)

    # Constructed tree (Node with recursive children).
    tree = data.get("tree")
    if isinstance(tree, dict):
        _walk_tree(tree, stats)

    if stats["leaves_patched"] == 0 or dry_run:
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
    root = _multiverse_dir() / "trees" / session_uuid
    if not root.is_dir():
        print(f"  {session_uuid[:8]}: no cache dir at {root}")
        return 0, 0, 0
    trees = sorted(root.glob("*.json"))
    if not trees:
        print(f"  {session_uuid[:8]}: no tree files")
        return 0, 0, 0
    total_leaves = total_patched = total_delta = 0
    total_nodes = 0
    n_touched = 0
    for p in trees:
        s = _migrate_one_file(p, dry_run)
        if s["error"]:
            print(f"    {p.name[:12]}…: {s['error']}")
            continue
        total_leaves += s["leaves_seen"]
        total_patched += s["leaves_patched"]
        total_delta += s["tokens_delta"]
        total_nodes += s["nodes_seen"]
        if s["leaves_patched"] > 0:
            n_touched += 1
    verb = "would patch" if dry_run else "patched"
    print(
        f"  {session_uuid[:8]}: {len(trees)} tree file(s), "
        f"{total_leaves:,} leaves, {total_nodes:,} nodes; "
        f"{verb} {total_patched:,} image leaf(s) across {n_touched} "
        f"file(s) ({total_delta:+,} tokens delta)"
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
            f"\nGrand total: {verb} {totals[1]:,} image leaves across "
            f"{totals[0]} file(s), {totals[2]:+,} tokens delta."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
