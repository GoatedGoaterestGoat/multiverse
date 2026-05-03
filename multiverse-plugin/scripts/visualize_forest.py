"""Render a forest of compaction trees as a single HTML page.

Per-turn sections stacked vertically, each showing:
  - Header (turn id, leaf count, full token cost, status).
  - Verbatim user-prompt banner (preserved separator, never compacted).
  - The compaction tree with nested collapsible nodes.
  - The merge history table.

Top-level overview lists every turn with quick stats and links into the
per-turn sections. If `--budget` is given, an extra summary shows the
per-turn budget allocation under the chosen scheduler and the resulting
frontier size per turn.

Usage:
    python3 scripts/visualize_forest.py \\
        --tree 0=/tmp/tree_turn0_priv.json \\
        --tree 1=/tmp/tree_turn1_priv.json \\
        [--budget 15000 --scheduler recency_decay] \\
        --out /tmp/forest.html
"""

from __future__ import annotations

import argparse
import html as h
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from compaction.compactor import (
    SCHEDULERS, expand_to_budget, last_k_raw, recency_decay,
)
from compaction.types import Leaf, Node

# Reuse the per-tree renderer pieces.
from visualize import CSS, render_history, render_node, render_user_prompt


def reconstruct(d):
    if d["type"] == "leaf":
        return Leaf(kind=d["kind"], content=d.get("content", {}),
                    tokens=d["tokens"], preview=d.get("preview", ""),
                    source_uuids=d.get("source_uuids", []))
    return Node(id=d["id"], children=[reconstruct(c) for c in d["children"]],
                summary=d.get("summary", ""), tokens=d["tokens"],
                preview=d.get("preview", ""))


def parse_tree_arg(s: str) -> tuple[int, str]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"--tree expects TURN_IDX=PATH, got {s!r}")
    idx_s, path = s.split("=", 1)
    return int(idx_s), path


def resolve_scheduler(name: str, half_life: float, last_k: int):
    if name == "recency_decay":
        return lambda f, b: recency_decay(f, b, half_life=half_life)
    if name == "last_k_raw":
        return lambda f, b: last_k_raw(f, b, k=last_k)
    return SCHEDULERS[name]


def _annotate_states(
    tree_dict: dict,
    live_node,
    frontier_obj_ids: set[int],
    inherited: str | None = None,
) -> None:
    """Walk the JSON tree and the live tree in parallel; set `_state` on
    each JSON dict to one of:

      * "in_frontier" — IS in the chosen frontier; surfaces in the
        compacted output. The live agent will see this content (verbatim
        leaf or summary node, depending on type).
      * "absorbed" — descendant of an in_frontier ancestor. Compressed
        away into that ancestor's summary; the live agent never sees this.
      * "expanded_past" — neither in frontier nor under one. Default for
        nodes that the algorithm walked through; their children carry the
        actual surviving content.
    """
    if inherited == "absorbed":
        tree_dict["_state"] = "absorbed"
        if tree_dict.get("type") == "node":
            for cd, ch in zip(tree_dict.get("children", []),
                                getattr(live_node, "children", []) or []):
                _annotate_states(cd, ch, frontier_obj_ids, "absorbed")
        return

    if id(live_node) in frontier_obj_ids:
        tree_dict["_state"] = "in_frontier"
        if tree_dict.get("type") == "node":
            for cd, ch in zip(tree_dict.get("children", []),
                                getattr(live_node, "children", []) or []):
                _annotate_states(cd, ch, frontier_obj_ids, "absorbed")
        return

    tree_dict["_state"] = "expanded_past"
    if tree_dict.get("type") == "node":
        for cd, ch in zip(tree_dict.get("children", []),
                            getattr(live_node, "children", []) or []):
            _annotate_states(cd, ch, frontier_obj_ids, None)


def render_turn_section(idx: int, tree_data: dict, frontier_marker: set | None) -> str:
    tree = tree_data.get("tree") or {
        "type": "leaf", "kind": "?", "tokens": 0, "preview": "(no tree)",
        "content": {},
    }
    n_leaves = tree_data.get("n_leaves", 0)
    n_merges = len([h_ for h_ in tree_data.get("history", []) if "error" not in h_])
    model = tree_data.get("model", "?")
    turn_id = tree_data.get("turn_id", "?")

    # Patch in the user-prompt banner using the per-tree renderer.
    user_prompt_html = render_user_prompt(tree_data)
    tree_html = render_node(tree, depth=0, total_leaves=n_leaves)
    history_rows = render_history(tree_data.get("history", []))

    leaves_full_tok = sum(l.get("tokens", 0) for l in tree_data.get("leaves", []))
    root_tok = tree.get("tokens", 0) if tree.get("type") == "node" else tree.get("tokens", 0)
    compression = (leaves_full_tok / root_tok) if root_tok else 0
    comp_str = f"{compression:.1f}×" if compression else "—"

    return f"""\
<section class="turn" id="turn-{idx}">
  <h2>Turn {idx} <span class="dim">· {h.escape(str(turn_id)[:12])}…</span></h2>
  <div class="turn-meta">
    <b>leaves:</b> {n_leaves}
    <span class="sep">·</span> <b>verbatim cost:</b> {leaves_full_tok:,}t
    <span class="sep">·</span> <b>merges:</b> {n_merges}
    <span class="sep">·</span> <b>root:</b> {root_tok}t (~{comp_str} compression)
    <span class="sep">·</span> <b>model:</b> {h.escape(str(model))}
  </div>
  {user_prompt_html}
  <details open class="turn-tree">
    <summary>Tree</summary>
    <div class="tree-body">
      {tree_html}
    </div>
  </details>
  <details class="turn-history">
    <summary>Merge history ({n_merges})</summary>
    <table>
      <thead><tr>
        <th>step</th><th>span</th><th>[start:end)</th><th>tok</th><th>elapsed</th><th>summary</th>
      </tr></thead>
      <tbody>{history_rows}</tbody>
    </table>
  </details>
</section>
"""


PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Forest visualization</title>
<style>
{css}
{extra_css}
</style>
</head>
<body>

<header class="forest-header">
  <h1>Forest <span class="dim">{n_turns} turn{plural}</span></h1>
  <div class="meta">
    <div><b>total verbatim cost:</b> {total_verbatim:,}t (sum of leaf-token costs)</div>
    <div><b>total root cost:</b> {total_root:,}t (sum of root summary tokens)</div>
    <div><b>session:</b> <code>{session}</code></div>
  </div>
  {budget_summary}
</header>

<nav class="forest-toc">
  <h3>Turns</h3>
  <ol>
    {toc_items}
  </ol>
</nav>

<main>
{turn_sections}
</main>

<script>
document.querySelectorAll('details > summary').forEach(s => {{
  s.addEventListener('click', e => {{
    if (!e.altKey) return;
    e.preventDefault();
    const det = s.parentElement;
    const open = det.open;
    det.open = !open;
    det.querySelectorAll('details').forEach(d => d.open = !open);
  }});
}});
</script>

</body>
</html>
"""


EXTRA_CSS = """
.forest-header {
  background: var(--bg);
  padding: 18px 0 10px 0;
  border-bottom: 1px solid #ddd;
}
/* Override the per-tree visualizer's sticky `header` rule: with 161
   turns + a budget table, a sticky forest header can grow past the
   viewport height and pin over every section, making the page look
   unscrollable. */
header.forest-header {
  position: static;
}
.forest-header .meta { font-size: 12px; color: var(--fg-dim); }
.forest-header .meta div { margin: 2px 0; }
.forest-toc {
  background: var(--bg-soft);
  padding: 10px 16px;
  border-radius: 6px;
  margin: 12px 0 18px 0;
  font-size: 12px;
}
.forest-toc h3 { margin: 0 0 4px 0; font-size: 12px; color: var(--fg-dim); font-weight: 600; }
.forest-toc ol { margin: 0; padding-left: 20px; }
.forest-toc a {
  text-decoration: none;
  color: var(--c-node);
  font-family: var(--mono);
}
.forest-toc a:hover { text-decoration: underline; }
.turn {
  margin: 32px 0;
  padding: 16px 18px;
  border: 1px solid #eee;
  border-radius: 8px;
  background: var(--bg);
}
.turn h2 {
  margin: 0 0 6px 0;
  font-size: 18px;
  font-weight: 600;
  border-bottom: 1px solid #eee;
  padding-bottom: 6px;
}
.turn-meta {
  font-size: 12px;
  color: var(--fg-dim);
  margin-bottom: 12px;
}
.turn-meta .sep { margin: 0 5px; }
.turn-tree, .turn-history {
  margin: 14px 0;
  border-left: none;
  padding: 0;
}
.turn-tree > summary, .turn-history > summary {
  font-weight: 600;
  font-size: 13px;
  color: var(--fg);
  background: var(--bg-soft);
  padding: 6px 10px;
  border-radius: 4px;
  cursor: pointer;
}
.tree-body { padding-top: 6px; }
.budget-summary {
  margin: 10px 0;
  padding: 8px 12px;
  background: #f4f8f4;
  border-left: 3px solid #6a6;
  border-radius: 4px;
  font-size: 12px;
}
.budget-summary table { font-family: var(--mono); font-size: 11px; }
.budget-summary th, .budget-summary td {
  padding: 2px 8px; text-align: left; border-bottom: 1px solid #e0e7e0;
}

/* Frontier-state highlighting (only set when --budget is given). */
.s-in-frontier > summary {
  background: #e7f5e7 !important;
  border-left: 3px solid #4a4;
  padding-left: 8px;
  font-weight: 600;
}
.s-in-frontier > .summary-text { background: #f0f8f0; }
.s-absorbed { opacity: 0.42; }
.s-absorbed > summary { color: var(--fg-dim); }
.s-absorbed > .summary-text { background: #f7f7f7; color: #999; }
/* expanded-past: keep default appearance — these are tree-walk
   intermediates and don't need extra emphasis. */

.legend-frontier {
  font-size: 11px; color: var(--fg-dim);
  margin-top: 6px;
}
.legend-frontier .swatch {
  display: inline-block; width: 10px; height: 10px;
  border-radius: 2px; vertical-align: middle; margin: 0 4px 0 8px;
}
.legend-frontier .sw-frontier { background: #e7f5e7; border: 1px solid #4a4; }
.legend-frontier .sw-absorbed { background: #f0f0f0; opacity: 0.5; }
"""


def render_budget_summary(budget_alloc: dict[str, int],
                          frontier_sizes: dict[str, tuple[int, int]],
                          total_budget: int, scheduler: str) -> str:
    """Render the per-turn budget allocation table.

    The table itself is wrapped in ``<details>`` (collapsed by default)
    — at 161 turns it would otherwise expand the sticky header past the
    viewport height, which makes the whole page appear unscrollable.
    """
    rows = []
    used_total = 0
    for tid, b in budget_alloc.items():
        front_size, front_tok = frontier_sizes.get(tid, (0, 0))
        used_total += front_tok
        rows.append(
            f"<tr><td>{h.escape(tid[:12])}…</td><td>{b:,}t</td>"
            f"<td>{front_size}</td><td>{front_tok:,}t</td></tr>"
        )
    n = len(budget_alloc)
    return f"""\
<div class="budget-summary">
  <b>Budget:</b> {total_budget:,}t total &middot; scheduler: <code>{h.escape(scheduler)}</code>
  &middot; {n} turn{'s' if n != 1 else ''} &middot; <b>used:</b> {used_total:,}t
  <details class="budget-table">
    <summary>per-turn allocation table ({n} rows)</summary>
    <table>
      <thead><tr><th>turn</th><th>alloc</th><th>frontier size</th><th>used</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </details>
  <div class="legend-frontier">
    <span class="swatch sw-frontier"></span>in frontier (survives in compacted output)
    <span class="swatch sw-absorbed"></span>absorbed into a parent summary (compressed away)
  </div>
</div>
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tree", action="append", type=parse_tree_arg, default=[],
                   help="--tree TURN_IDX=PATH (repeatable)")
    p.add_argument("--budget", type=int, default=None,
                   help="if set, also show per-turn budget allocation")
    p.add_argument("--scheduler", choices=sorted(SCHEDULERS.keys()),
                   default="recency_decay")
    p.add_argument("--half-life", type=float, default=5.0)
    p.add_argument("--last-k", type=int, default=3)
    p.add_argument("--out", default="/tmp/forest.html")
    args = p.parse_args()

    if not args.tree:
        print("error: at least one --tree TURN_IDX=PATH is required", file=sys.stderr)
        return 1

    tree_args = sorted(args.tree, key=lambda x: x[0])
    trees: list[tuple[int, dict]] = []
    for idx, path in tree_args:
        with open(path) as f:
            trees.append((idx, json.load(f)))

    # Optional budget allocation
    budget_summary = ""
    if args.budget is not None:
        from compaction.types import Turn
        turns = []
        live_roots: list[object | None] = []
        for idx, td in trees:
            t = Turn(turn_id=td.get("turn_id", f"turn-{idx}"),
                     user_prompt=None, leaves=[])
            t.root = reconstruct(td["tree"]) if td.get("tree") else None
            turns.append(t)
            live_roots.append(t.root)

        sched = resolve_scheduler(args.scheduler, args.half_life, args.last_k)
        alloc = sched(turns, args.budget)
        frontier_sizes = {}
        for (idx, td), turn, live_root in zip(trees, turns, live_roots):
            front = expand_to_budget(turn.root, alloc.get(turn.turn_id, 0)) if turn.root else []
            frontier_sizes[turn.turn_id] = (
                len(front), sum(getattr(x, "tokens", 0) for x in front),
            )
            # Annotate the JSON tree with per-entry state so the renderer
            # can highlight in-frontier entries.
            if live_root is not None and td.get("tree") is not None:
                _annotate_states(td["tree"], live_root, {id(x) for x in front})
        budget_summary = render_budget_summary(
            alloc, frontier_sizes, args.budget, args.scheduler
        )

    total_verbatim = sum(
        sum(l.get("tokens", 0) for l in td.get("leaves", []))
        for _, td in trees
    )
    total_root = sum(
        td.get("tree", {}).get("tokens", 0) if td.get("tree") else 0
        for _, td in trees
    )
    session = trees[0][1].get("session", "?") if trees else "?"

    toc_items = "\n    ".join(
        f'<li><a href="#turn-{idx}">turn {idx} '
        f'({td.get("n_leaves", 0)} leaves, '
        f'{sum(l.get("tokens", 0) for l in td.get("leaves", [])):,}t)</a></li>'
        for idx, td in trees
    )

    sections = "\n".join(
        render_turn_section(idx, td, None) for idx, td in trees
    )

    html_text = PAGE_TEMPLATE.format(
        css=CSS,
        extra_css=EXTRA_CSS,
        n_turns=len(trees),
        plural="s" if len(trees) != 1 else "",
        total_verbatim=total_verbatim,
        total_root=total_root,
        session=h.escape(str(session)),
        budget_summary=budget_summary,
        toc_items=toc_items,
        turn_sections=sections,
    )

    Path(args.out).write_text(html_text)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
