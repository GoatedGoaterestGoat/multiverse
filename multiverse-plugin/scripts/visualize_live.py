"""Visualize the current live JSONL of a session.

For each turn (chain-walked):
  - If the turn has a cached tree at
    ``$MULTIVERSE_ROOT/trees/<uuid>/<turn_id>.json``, render the tree
    with frontier highlighting matching what's currently in the live
    JSONL: ``in_frontier`` for tree nodes whose content (or model
    summary) appears in the live's compacted blocks, ``absorbed``
    for descendants of an in-frontier summary node, ``expanded_past``
    otherwise.
  - If no cached tree, render the turn's leaves as a flat list (no
    "Tree" details, no merge history).

Usage:
    PYTHONPATH=multiverse-plugin python3 \\
        multiverse-plugin/scripts/visualize_live.py SESSION_ID CWD \\
        [--out PATH]
"""

from __future__ import annotations

import argparse
import html as h
import json
import re
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN))
sys.path.insert(0, str(_PLUGIN / "vendor"))
sys.path.insert(0, str(_PLUGIN / "scripts"))

from compaction.parser import parse_session  # noqa: E402
from compaction.types import Leaf, Node  # noqa: E402
from src.full_jsonl import live_jsonl_path  # noqa: E402
from src.paths import multiverse_dir  # noqa: E402
from visualize import (  # noqa: E402
    CSS,
    render_history,
    render_node,
    render_user_prompt,
)
from visualize_forest import (  # noqa: E402
    EXTRA_CSS,
    PAGE_TEMPLATE,
    _annotate_states,
    render_turn_section,
)


# Same shape that scripts/build_tree_priv_cli.py writes to its tree.json
def _reconstruct_tree(d: dict):
    if d.get("type") == "leaf":
        return Leaf(
            kind=d["kind"],
            content=d.get("content", {}),
            tokens=d["tokens"],
            preview=d.get("preview", ""),
            source_uuids=d.get("source_uuids", []),
        )
    if d.get("type") == "node":
        return Node(
            id=d["id"],
            children=[_reconstruct_tree(c) for c in d.get("children", [])],
            summary=d.get("summary", ""),
            tokens=d["tokens"],
            preview=d.get("preview", ""),
        )
    raise TypeError(f"unknown tree node type: {d.get('type')!r}")


# Match the **Compacted summary (N entries):** prefix our renderer adds
# (vendor/compaction/render.py:_render_summary_block).
_SUMMARY_HEADER_RE = re.compile(
    r"^\*\*Compacted summary \((\d+) entries\):\*\*\s*\n+(.*)$",
    re.DOTALL,
)


def _index_tree(tree_root):
    """Build content-keyed indexes over a tree so we can match live
    JSONL leaves back to the tree's leaves and Node summaries.

    Returns (by_text, by_thinking_sig, by_tool_use_id, by_tool_result_id,
             by_summary). Each maps the matching key to a list of nodes
             (lists handle the unlikely duplicate-content case)."""
    by_text: dict[str, list] = {}
    by_thinking_sig: dict[str, list] = {}
    by_tool_use_id: dict[str, list] = {}
    by_tool_result_id: dict[str, list] = {}
    by_summary: dict[str, list] = {}

    def walk(node):
        if isinstance(node, Leaf):
            kind = node.kind
            if kind == "asst_text":
                by_text.setdefault(
                    node.content.get("text", ""), []
                ).append(node)
            elif kind == "thinking":
                sig = node.content.get("signature", "")
                if sig:
                    by_thinking_sig.setdefault(sig, []).append(node)
            elif kind == "tool_use":
                tu = node.content.get("tool_use_id", "")
                if tu:
                    by_tool_use_id.setdefault(tu, []).append(node)
            elif kind == "tool_result":
                tu = node.content.get("tool_use_id", "")
                if tu:
                    by_tool_result_id.setdefault(tu, []).append(node)
        elif isinstance(node, Node):
            by_summary.setdefault(node.summary.strip(), []).append(node)
            for c in node.children:
                walk(c)

    if tree_root is not None:
        walk(tree_root)
    return (
        by_text, by_thinking_sig, by_tool_use_id, by_tool_result_id,
        by_summary,
    )


def _compute_frontier_ids(live_turn, tree_root) -> set[int]:
    """Return id() of tree leaves/nodes that appear in the live turn's
    content. The live turn's leaves are the result of parse_session on
    the live JSONL — synthetic UUIDs, but content matches the tree."""
    if tree_root is None:
        return set()

    (by_text, by_thinking_sig, by_tool_use_id, by_tool_result_id,
     by_summary) = _index_tree(tree_root)

    frontier: set[int] = set()
    used = set()  # consumed leaf id()s, so duplicate-content asst_texts
                  # don't all point at the same tree leaf

    def take(cands):
        for c in cands:
            if id(c) not in used:
                used.add(id(c))
                frontier.add(id(c))
                return True
        return False

    for leaf in live_turn.leaves:
        kind = leaf.kind
        if kind == "thinking":
            sig = leaf.content.get("signature", "")
            take(by_thinking_sig.get(sig, []))
        elif kind == "tool_use":
            tu = leaf.content.get("tool_use_id", "")
            take(by_tool_use_id.get(tu, []))
        elif kind == "tool_result":
            tu = leaf.content.get("tool_use_id", "")
            take(by_tool_result_id.get(tu, []))
        elif kind == "asst_text":
            text = leaf.content.get("text", "")
            m = _SUMMARY_HEADER_RE.match(text)
            if m:
                # Compacted summary block — match against Node summaries.
                summary_body = m.group(2).strip()
                take(by_summary.get(summary_body, []))
            else:
                take(by_text.get(text, []))
        # user_text and tool_exchange aren't part of native tree-leaf
        # matching for in-place visualization.
    return frontier


def _render_no_tree_section(idx: int, turn) -> str:
    """Render a turn that has no cached tree as a flat list of leaves."""
    user_prompt_dict = None
    if turn.user_prompt is not None:
        user_prompt_dict = {
            "kind": turn.user_prompt.kind,
            "content": turn.user_prompt.content,
            "tokens": turn.user_prompt.tokens,
            "preview": turn.user_prompt.preview,
        }
    user_prompt_html = render_user_prompt({"user_prompt": user_prompt_dict})

    leaves_html = []
    for leaf in turn.leaves:
        leaf_dict = {
            "type": "leaf",
            "kind": leaf.kind,
            "tokens": leaf.tokens,
            "preview": leaf.preview,
            "content": leaf.content,
            "source_uuids": leaf.source_uuids,
        }
        leaves_html.append(render_node(leaf_dict, depth=0, total_leaves=1))
    leaves_block = "\n".join(leaves_html) or (
        '<div class="user-prompt-empty">(no leaves)</div>'
    )

    leaves_full_tok = sum(l.tokens for l in turn.leaves)
    return f"""\
<section class="turn" id="turn-{idx}">
  <h2>Turn {idx} <span class="dim">· {h.escape(turn.turn_id[:12])}…</span></h2>
  <div class="turn-meta">
    <b>leaves:</b> {len(turn.leaves)}
    <span class="sep">·</span> <b>verbatim cost:</b> {leaves_full_tok:,}t
    <span class="sep">·</span> <b>(no cached tree — flat leaf listing)</b>
  </div>
  {user_prompt_html}
  <details open class="turn-tree">
    <summary>Leaves</summary>
    <div class="tree-body">
      {leaves_block}
    </div>
  </details>
</section>
"""


def _render_summary_banner(
    n_turns: int, n_with_tree: int, n_without_tree: int,
    in_front: int, absorbed: int, expanded_past: int,
) -> str:
    return f"""\
<div class="budget-summary">
  <b>Live JSONL view:</b> {n_turns} turn(s)
  &middot; {n_with_tree} with cached tree
  &middot; {n_without_tree} without
  <div class="legend-frontier">
    <span class="swatch sw-frontier"></span>in frontier ({in_front})
    <span class="swatch sw-absorbed"></span>absorbed ({absorbed})
    <span class="swatch sw-expanded"></span>expanded past ({expanded_past})
  </div>
</div>
"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("session_id")
    p.add_argument("cwd")
    p.add_argument("--out", default=None,
                   help="output path (default: /tmp/multiverse-viz-<sid8>.html)")
    args = p.parse_args()

    live = live_jsonl_path(args.session_id, args.cwd)
    if not live.is_file():
        print(f"live JSONL missing at {live}", file=sys.stderr)
        return 1

    cache_dir = multiverse_dir() / "trees" / args.session_id

    forest = parse_session(str(live))
    print(f"parsed {len(forest)} turns from {live}", file=sys.stderr)

    sections: list[str] = []
    n_with = n_without = 0
    in_front_total = absorbed_total = expanded_total = 0
    total_verbatim = 0
    total_root = 0

    for idx, turn in enumerate(forest):
        leaves_full_tok = sum(l.tokens for l in turn.leaves)
        total_verbatim += leaves_full_tok
        cache_path = cache_dir / f"{turn.turn_id}.json"
        tree_data = None
        if cache_path.is_file():
            try:
                tree_data = json.loads(cache_path.read_text())
            except Exception:
                tree_data = None

        if tree_data is None or tree_data.get("tree") is None:
            sections.append(_render_no_tree_section(idx, turn))
            n_without += 1
            continue

        n_with += 1
        tree_root = _reconstruct_tree(tree_data["tree"])
        frontier_ids = _compute_frontier_ids(turn, tree_root)
        _annotate_states(tree_data["tree"], tree_root, frontier_ids)
        # Tally state labels for the banner.
        def _walk_states(d):
            nonlocal in_front_total, absorbed_total, expanded_total
            s = d.get("_state")
            if s == "in_frontier":
                in_front_total += 1
            elif s == "absorbed":
                absorbed_total += 1
            elif s == "expanded_past":
                expanded_total += 1
            for c in d.get("children", []):
                _walk_states(c)
        _walk_states(tree_data["tree"])

        root_tok = tree_data["tree"].get("tokens", 0)
        total_root += root_tok
        sections.append(render_turn_section(idx, tree_data, None))

    toc_items = "\n    ".join(
        f'<li><a href="#turn-{i}">turn {i} '
        f'({len(t.leaves)} leaves, '
        f'{sum(l.tokens for l in t.leaves):,}t)</a></li>'
        for i, t in enumerate(forest)
    )

    banner = _render_summary_banner(
        n_turns=len(forest),
        n_with_tree=n_with, n_without_tree=n_without,
        in_front=in_front_total, absorbed=absorbed_total,
        expanded_past=expanded_total,
    )

    html_text = PAGE_TEMPLATE.format(
        css=CSS,
        extra_css=EXTRA_CSS,
        n_turns=len(forest),
        plural="s" if len(forest) != 1 else "",
        total_verbatim=total_verbatim,
        total_root=total_root,
        session=h.escape(str(live)),
        budget_summary=banner,
        toc_items=toc_items,
        turn_sections="\n".join(sections),
    )

    out = args.out or f"/tmp/multiverse-viz-{args.session_id[:8]}.html"
    Path(out).write_text(html_text)
    print(f"wrote {out}", file=sys.stderr)
    print(f"  with-tree turns: {n_with}", file=sys.stderr)
    print(f"  without-tree turns: {n_without}", file=sys.stderr)
    print(f"  state cells: in_frontier={in_front_total} absorbed={absorbed_total} "
          f"expanded_past={expanded_total}", file=sys.stderr)
    print(out)  # stdout — for the caller
    return 0


if __name__ == "__main__":
    sys.exit(main())
