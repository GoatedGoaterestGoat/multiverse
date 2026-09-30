# Local dashboard views

Each universe can define `views/default.json`. Enter a selected descendant group
to open its portal: the title, agent columns and widgets now belong to that
universe. Space or the left/right arrows fold and expand the compact hierarchy.
A single group click folds; a double-click enters. Backspace or the breadcrumb
returns to the prior scope, restoring its selection, folds, search and scroll.
History is session-local and can span any number of levels, including direct
jumps to a grandchild. The initial dashboard scope is the navigation boundary.

Tab moves from the hierarchy into local tables and artifact buttons. Tables
support arrow keys, horizontal scrolling and mouse selection; artifact buttons
open with Enter or a click. Esc closes a preview. Views refresh with the fleet.
The transient agent/group inspector replaces the local panel while open; `p` or
Esc returns to the local view. When attaching from a dashboard inside the same
tmux server, default `Ctrl-b L` returns to its previous session; `Ctrl-b d`
detaches the entire tmux client.

```json
{
  "title": "Testing · reproduction queue",
  "accent": "#f9e2af",
  "columns": ["name", "live", "phase", "summary"],
  "widgets": [
    {"type": "markdown", "title": "Current pass", "text": "**Scope:** portal navigation and local views."},
    {"type": "table", "title": "Verification queue",
     "columns": ["ID", "Status", "Owner", "Evidence"],
     "rows": [["UI-01", "retest", "testing-ui", "portal.log"]]},
    {"type": "artifacts", "title": "Evidence",
     "items": [{"label": "Portal transcript", "path": "artifacts/portal.log",
                "description": "Exact keys, revision and terminal output"}]}
  ]
}
```

`title`, `accent` and `columns` retain their existing meanings. Supported agent
columns are `name`, `harness`, `live`, `activity` (shown as "Status"; the key is unchanged), `phase`, `summary`, `id`,
`universe`, `task`, and `updated_at`. The name column is always shown. Narrow
terminals omit optional agent columns and stack the local view below the fleet.

`widgets` is optional and ordered. The initial declarative schema has three types:

| Type | Fields | Behavior |
| --- | --- | --- |
| `markdown` | `title`, `text` | Render a short Markdown overview, metrics or instructions. Inline links are not automatically opened. |
| `table` | `title`, `columns` (nonempty string array), `rows` (arrays matching column count) | Browse a queue, simulated positions, constraints or test results. Cells are literal text. |
| `artifacts` | `title`, `items` containing `label`, `path`, optional `description` | Select a file for a preview and an explicit link to its default viewer. |

Artifact paths are relative to the **universe directory**, not its workspace.
Absolute paths, traversal outside that directory, escaped symlinks, missing files
and non-files produce an inline error. Copy preview artifacts into the scope's
`artifacts/` directory. UTF-8 text previews are limited to 256 KiB; Markdown files
render as Markdown. SVG/HTML have a source preview and a file link for viewing in
a browser. Binary or larger artifacts only offer the file link. The view schema
does not execute commands, load Python or mutate queue data. Opening a link is an
explicit user action handled by the system default viewer.

For a mockups portal, use a Markdown brief and artifact cards for `.md`, `.svg`
and `.html` previews; use table widgets for fictional market data or CAD checks.
For a testing portal, lead with a reproduction/fix/retest table and evidence
links. These are different local surfaces, independent of the agent hierarchy.

Unknown or malformed widgets display a local error while valid siblings and the
hierarchy remain usable. Limits keep local views small: 32 widgets, 16 columns,
500 rows per table, 100 artifacts per widget, 32 KiB per Markdown block, and
2,000 characters per cell/description. Invalid JSON displays a configuration
error with the default hierarchy columns. Missing configuration uses defaults.
