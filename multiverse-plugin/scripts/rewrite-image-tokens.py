#!/usr/bin/env python3
"""One-shot migration: rewrite image-leaf `tokens` fields in cached trees.

Applied after fixing count_image_tokens (commit 49b353f) so cached trees
use the corrected image sizing. No tree structure changes and no
re-summarization — just swap the image portion of each affected leaf's
tokens field. Node.delta regenerates itself on load (it's ``init=False``
on the ``Node`` dataclass and computed in ``__post_init__``).

Uses the OLD formula (``len(base64_data) / 1.44`` for base64 sources,
flat 1600 for url/file_id) to compute how much to subtract, then adds
the new formula's number. Text tokens are untouched — the migration
doesn't need to know how they were originally counted.

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

from compaction.tokenize import count_image_tokens  # noqa: E402


# Bump when the image-token formula changes again. Trees carrying a
# marker >= this value are treated as already migrated.
_SCHEMA_VERSION = 2
_SCHEMA_KEY = "image_tokens_schema_version"


def _multiverse_dir() -> Path:
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.multiverse"))


def _old_count_image_tokens(block: dict) -> int:
    """The pre-fix formula from tokenize.py: base64 length / 1.44 for
    inline images, flat 1600 for url/file_id. Used only to know how
    much to SUBTRACT from the stored tokens before adding the corrected
    count in."""
    if not isinstance(block, dict):
        return 0
    source = block.get("source")
    if not isinstance(source, dict):
        return 0
    data = source.get("data")
    if isinstance(data, str) and data:
        return max(1, int(len(data) / 1.44))
    return 1600


def _patch_leaf(leaf: dict, stats: dict) -> None:
    if not isinstance(leaf, dict):
        return
    if leaf.get("type") != "leaf":
        return
    stats["leaves_seen"] += 1
    images = leaf.get("images") or []
    if not images:
        return
    old_img = sum(_old_count_image_tokens(img) for img in images)
    new_img = sum(count_image_tokens(img) for img in images)
    old_tokens = leaf.get("tokens", 0)
    if not isinstance(old_tokens, int):
        return
    # Guard against double-migration. Under the pre-fix formula the leaf
    # was sized ``tokens = text_tokens + old_img``, so ``tokens`` had to
    # be strictly greater than ``old_img``. If the stored value has
    # already dropped below ``old_img``, the image tokens can't still
    # be in there under the old formula — the leaf must have been
    # migrated already. Skip to keep this script idempotent even on
    # files that predate the schema-version marker.
    if old_tokens <= old_img:
        stats["leaves_skipped_already_migrated"] += 1
        return
    new_tokens = max(1, old_tokens - old_img + new_img)
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
        "leaves_skipped_already_migrated": 0,
        "nodes_seen": 0,
        "tokens_delta": 0,
        "error": None,
        "already_marked": False,
    }
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        stats["error"] = str(e)
        return stats

    # Idempotency guard 1: version marker on the file itself.
    if data.get(_SCHEMA_KEY, 0) >= _SCHEMA_VERSION:
        stats["already_marked"] = True
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

    if dry_run:
        return stats

    # Always stamp the version marker, even if nothing was patched.
    # That way a file we've inspected once will short-circuit on any
    # future run under the same schema version.
    data[_SCHEMA_KEY] = _SCHEMA_VERSION

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
    n_already_marked = 0
    n_skipped_leaves = 0
    for p in trees:
        s = _migrate_one_file(p, dry_run)
        if s["error"]:
            print(f"    {p.name[:12]}…: {s['error']}")
            continue
        if s.get("already_marked"):
            n_already_marked += 1
            continue
        total_leaves += s["leaves_seen"]
        total_patched += s["leaves_patched"]
        total_delta += s["tokens_delta"]
        total_nodes += s["nodes_seen"]
        n_skipped_leaves += s.get("leaves_skipped_already_migrated", 0)
        if s["leaves_patched"] > 0:
            n_touched += 1
    verb = "would patch" if dry_run else "patched"
    extra = ""
    if n_already_marked:
        extra += f", {n_already_marked} file(s) already marked, skipped"
    if n_skipped_leaves:
        extra += (
            f", {n_skipped_leaves} leaf/leaves skipped (tokens < old_img → "
            f"already migrated)"
        )
    print(
        f"  {session_uuid[:8]}: {len(trees)} tree file(s), "
        f"{total_leaves:,} leaves, {total_nodes:,} nodes; "
        f"{verb} {total_patched:,} image leaf(s) across {n_touched} "
        f"file(s) ({total_delta:+,} tokens delta){extra}"
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
