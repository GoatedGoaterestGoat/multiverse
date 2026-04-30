"""Render a saved tree JSON (from build_tree.py) into a static HTML page.

Layout: nested collapsible panels via <details>/<summary>. Each internal
node shows its summary text and children indented below; each leaf shows
its preview, with a click-to-expand pre block for the full content. Color
bands on the left edge indicate node type.

A merge-history log is appended at the end so you can read the order in
which merges happened.

Usage:
    python3 scripts/visualize.py TREE.json [--out tree.html]
"""

from __future__ import annotations

import argparse
import html as h
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Tree -> HTML
# ---------------------------------------------------------------------------

def render_node(node: dict, depth: int = 0, total_leaves: int = 1) -> str:
    if node["type"] == "leaf":
        return render_leaf(node, depth)
    return render_internal(node, depth, total_leaves)


def render_leaf(node: dict, depth: int) -> str:
    kind = node.get("kind", "?")
    tokens = node.get("tokens", 0)
    preview = h.escape(node.get("preview", ""))

    # Build full-content section based on leaf kind
    content = node.get("content") or {}
    body_text = _leaf_body(kind, content)
    body = (
        f'<pre class="leaf-body">{h.escape(body_text)}</pre>'
        if body_text
        else ""
    )

    return f"""\
<details class="leaf k-{h.escape(kind)}" data-depth="{depth}">
  <summary>
    <span class="badge leaf-badge">leaf</span>
    <span class="kind">{h.escape(kind)}</span>
    <span class="tokens">{tokens}t</span>
    <span class="preview">{preview}</span>
  </summary>
  {body}
</details>
"""


def render_internal(node: dict, depth: int, total_leaves: int) -> str:
    nid = node.get("id", "?")
    tokens = node.get("tokens", 0)
    span = _span_size(node)
    summary = h.escape(node.get("summary", ""))
    preview = h.escape(node.get("preview", ""))
    children = node.get("children", [])

    children_tokens = sum(_total_tokens(c) for c in children)
    ratio = (children_tokens / tokens) if tokens else 0
    ratio_str = f"{ratio:.1f}x" if ratio else "-"

    children_html = "".join(
        render_node(c, depth + 1, total_leaves) for c in children
    )

    return f"""\
<details class="node" data-depth="{depth}" open>
  <summary>
    <span class="badge node-badge">#{nid}</span>
    <span class="span">span={span}</span>
    <span class="tokens">{tokens}t</span>
    <span class="ratio" title="children tokens / summary tokens">{ratio_str}</span>
    <span class="preview">{preview}</span>
  </summary>
  <div class="summary-text">{summary}</div>
  <div class="children">
    {children_html}
  </div>
</details>
"""


def _span_size(node: dict) -> int:
    if node.get("type") == "leaf":
        return 1
    return sum(_span_size(c) for c in node.get("children", []))


def _total_tokens(node: dict) -> int:
    """Sum of leaf tokens under this node (for compression-ratio display)."""
    if node.get("type") == "leaf":
        return node.get("tokens", 0)
    return sum(_total_tokens(c) for c in node.get("children", []))


def _leaf_body(kind: str, content: dict) -> str:
    if kind == "user_text" or kind == "asst_text" or kind == "thinking":
        return content.get("text", "")
    if kind == "tool_exchange":
        name = content.get("name", "?")
        inp = content.get("input", {})
        result = content.get("result_text", "")
        is_error = content.get("is_error")
        marker = "ERROR" if is_error else "OK"
        return f"{name}({json.dumps(inp, indent=2)})\n\n--- {marker} ---\n{result}"
    if kind == "tool_use":
        name = content.get("name", "?")
        inp = content.get("input", {})
        return f"{name}({json.dumps(inp, indent=2)})"
    if kind == "tool_result":
        return content.get("text", "")
    return json.dumps(content, indent=2)


# ---------------------------------------------------------------------------
# Whole-page assembly
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Turn {turn_idx} tree</title>
<style>
{css}
</style>
</head>
<body>

<header>
  <h1>Turn {turn_idx} <span class="dim">tree</span></h1>
  <div class="meta">
    <div><b>session:</b> <code>{session}</code></div>
    <div><b>turn id:</b> <code>{turn_id}</code></div>
    <div><b>leaves:</b> {n_leaves} &middot; <b>merges:</b> {n_merges} &middot; <b>model:</b> {model}</div>
  </div>
</header>

<section class="legend">
  <span class="badge leaf-badge">leaf</span>
  <span class="badge node-badge">#N</span> internal node
  <span>&middot;</span>
  <span class="ratio-legend">Nx</span> = children tokens / summary tokens (compression)
  <span>&middot;</span>
  <span>colors: <span class="dot k-user_text"></span>user
   <span class="dot k-asst_text"></span>assistant
   <span class="dot k-thinking"></span>thinking
   <span class="dot k-tool_exchange"></span>tool</span>
</section>

{user_prompt_html}

<main>
{tree_html}
</main>

<section class="history">
<h2>Merge history</h2>
<table>
<thead><tr>
<th>step</th><th>span</th><th>[start:end)</th><th>tok</th><th>elapsed</th><th>summary</th>
</tr></thead>
<tbody>
{history_rows}
</tbody>
</table>
</section>

<script>
// Click a node header to also collapse all descendants (alt-click).
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

CSS = """
:root {
  --c-leaf: #999;
  --c-node: #06c;
  --c-tool: #c60;
  --c-think: #6a6;
  --c-user: #c06;
  --c-asst: #36c;
  --bg: #fff;
  --bg-soft: #fafafa;
  --bg-summary: #f4f6fa;
  --fg: #222;
  --fg-dim: #666;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 13px;
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg);
  margin: 0;
  padding: 0 32px 64px 32px;
  max-width: 1400px;
}
header {
  position: sticky; top: 0;
  background: var(--bg);
  padding: 16px 0 12px 0;
  border-bottom: 1px solid #eee;
  z-index: 10;
}
h1 { margin: 0 0 8px 0; font-size: 22px; font-weight: 600; }
.dim { color: var(--fg-dim); font-weight: 400; }
.meta { font-size: 12px; color: var(--fg-dim); }
.meta div { display: inline-block; margin-right: 18px; }
.legend {
  font-size: 11px;
  color: var(--fg-dim);
  padding: 8px 0;
  border-bottom: 1px solid #eee;
  display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
}
.dot {
  display: inline-block; width: 10px; height: 10px;
  border-radius: 50%; margin: 0 4px 0 8px; vertical-align: middle;
}
main { padding: 16px 0; }

details {
  margin: 3px 0;
  padding: 0 0 0 12px;
  border-left: 3px solid var(--c-leaf);
}
details.node { border-left-color: var(--c-node); }
details.k-tool_exchange, details.k-tool_use, details.k-tool_result { border-left-color: var(--c-tool); }
details.k-thinking { border-left-color: var(--c-think); }
details.k-user_text { border-left-color: var(--c-user); }
details.k-asst_text { border-left-color: var(--c-asst); }

summary {
  cursor: pointer;
  padding: 3px 6px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 12px;
  list-style: none;
  display: flex;
  gap: 8px;
  align-items: baseline;
  white-space: nowrap;
  overflow: hidden;
}
summary::-webkit-details-marker { display: none; }
summary:hover { background: var(--bg-soft); }
.badge {
  font-weight: 600;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  background: #eef;
  color: #036;
  flex-shrink: 0;
}
.leaf-badge { background: #eee; color: #555; }
.node-badge { background: #def; color: #036; }
.kind { color: var(--fg-dim); flex-shrink: 0; }
.tokens { color: var(--fg-dim); font-size: 11px; flex-shrink: 0; }
.span { color: var(--fg-dim); font-size: 11px; flex-shrink: 0; }
.ratio {
  color: #060; font-weight: 600; font-size: 11px; flex-shrink: 0;
}
.ratio-legend {
  background: #efe; color: #060; padding: 1px 5px; border-radius: 3px;
  font-weight: 600; font-family: var(--mono);
}
.preview {
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1; min-width: 0;
}
.summary-text {
  font-style: italic;
  color: #234;
  background: var(--bg-summary);
  border-radius: 4px;
  padding: 6px 10px;
  margin: 4px 0 6px 0;
  font-size: 12px;
}
.leaf-body {
  background: #f7f7f8;
  border-radius: 4px;
  padding: 8px 10px;
  margin: 4px 0;
  font-family: var(--mono);
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 360px;
  overflow: auto;
}
.children { padding: 0; }

.k-user_text > summary { background: rgba(204, 0, 102, 0.04); }
.k-asst_text > summary { background: rgba(51, 102, 204, 0.04); }

.history {
  margin-top: 32px;
  border-top: 1px solid #eee;
  padding-top: 16px;
}
.history h2 { font-size: 16px; margin: 0 0 8px 0; }
.history table { border-collapse: collapse; width: 100%; font-family: var(--mono); font-size: 12px; }
.history th, .history td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; vertical-align: top; }
.history th { color: var(--fg-dim); font-weight: 500; }
.history td.summary-cell { white-space: pre-wrap; word-break: break-word; max-width: 800px; }

.user-prompt {
  margin: 16px 0 24px 0;
  border: 1px solid #e7c0d3;
  border-left: 4px solid var(--c-user);
  background: #fff7fb;
  border-radius: 6px;
  padding: 10px 14px;
}
.user-prompt-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--c-user);
  margin-bottom: 6px;
  display: flex;
  gap: 10px;
  align-items: center;
}
.user-prompt-label .pill {
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.02em;
  text-transform: none;
  color: var(--fg-dim);
  background: #fff;
  border: 1px solid #e7c0d3;
  padding: 1px 6px;
  border-radius: 10px;
}
.user-prompt-text {
  font-family: var(--mono);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  max-height: 320px;
  overflow: auto;
  color: var(--fg);
}
.user-prompt-empty {
  font-size: 11px;
  color: var(--fg-dim);
  font-style: italic;
}
"""


def render_history(history: list[dict]) -> str:
    rows = []
    for h_ in history:
        if "error" in h_:
            rows.append(
                f"<tr><td>{h_.get('step', '?')}</td><td>-</td><td>-</td>"
                f"<td>-</td><td>-</td>"
                f"<td class='summary-cell'>ERROR: {h.escape(h_['error'])}</td></tr>"
            )
            continue
        span = h_.get("span", h_.get("end", 0) - h_.get("start", 0))
        sumtok = h_.get("summary_tokens", "?")
        elapsed = h_.get("elapsed_s", "?")
        rows.append(
            f"<tr>"
            f"<td>{h_['step']}</td>"
            f"<td>{span}</td>"
            f"<td>[{h_['start']}:{h_['end']})</td>"
            f"<td>{sumtok}</td>"
            f"<td>{elapsed}{'s' if elapsed != '?' else ''}</td>"
            f"<td class='summary-cell'>{h.escape(h_['summary'])}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def render_user_prompt(data: dict) -> str:
    """Render the verbatim user prompt that bounded this turn.

    The prompt is hoisted out of the tree (it's a separator, not a
    compactable leaf), so we display it as a banner above the tree.
    """
    prompt = data.get("user_prompt")
    if prompt is None:
        return (
            '<section class="user-prompt">'
            '<div class="user-prompt-label">User prompt'
            ' <span class="pill">verbatim · not compacted</span></div>'
            '<div class="user-prompt-empty">'
            '(no user prompt for this turn — orphan content before any prompt)'
            '</div></section>'
        )
    text = (prompt.get("content") or {}).get("text", "")
    tokens = prompt.get("tokens", 0)
    return (
        '<section class="user-prompt">'
        '<div class="user-prompt-label">User prompt'
        f' <span class="pill">verbatim · not compacted · {tokens}t</span></div>'
        f'<pre class="user-prompt-text">{h.escape(text)}</pre>'
        '</section>'
    )


def render_page(data: dict) -> str:
    tree = data.get("tree") or {"type": "leaf", "kind": "?", "tokens": 0, "preview": "(no tree)", "content": {}}
    total_leaves = data.get("n_leaves", 1)
    tree_html = render_node(tree, depth=0, total_leaves=total_leaves)

    return PAGE_TEMPLATE.format(
        turn_idx=data.get("turn_idx", "?"),
        session=h.escape(str(data.get("session", ""))),
        turn_id=h.escape(str(data.get("turn_id", ""))),
        n_leaves=data.get("n_leaves", 0),
        n_merges=len([h_ for h_ in data.get("history", []) if "error" not in h_]),
        model=h.escape(str(data.get("model", "?"))),
        css=CSS,
        user_prompt_html=render_user_prompt(data),
        tree_html=tree_html,
        history_rows=render_history(data.get("history", [])),
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("tree_json", help="path to tree JSON from build_tree.py")
    p.add_argument("--out", default=None, help="output HTML path")
    args = p.parse_args()

    with open(args.tree_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    out = args.out or str(Path(args.tree_json).with_suffix(".html"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_page(data))

    sys.stderr.write(f"wrote {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
