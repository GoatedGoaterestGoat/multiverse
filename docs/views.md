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

The agent inspector shows the Codex native mode (`bridge` or `plain`) and its
selection policy. If auto fell back to plain during launch, it also shows the
reason. It also shows the CLI and daemon versions recorded at launch preparation,
highlighting differences without blocking the bridge. Opening the inspector does
not retry bridge startup, poll versions or change the mode.

For early or nonzero native exits, the inspector shows the saved launch error
and terminal tail (at most 40 lines / 8 KiB), including after the tmux wrapper
has gone. It labels this as saved launch output, not a live capture. The same
diagnostic is available through `list`, `inspect` and `doctor`.

A lost Codex terminal relay shows `STATUS disconnected` in red while `LIVE`
still describes the process. The inspector shows the connection failure and
recovery guidance: kill the launch, then resume its saved conversation. New
messages are refused and remain saved for explicit retry. Native receipt checks
and cancellation can still reach the exact thread if the daemon is available.

`K` (Shift+k) opens the kill confirmation dialog; `s` is an alias. Lowercase
`k` still moves up, and `j`/`h`/`l` keep their navigation behavior. The footer
shows `K Kill`; `d` still deletes to trash and appears in the `?` help screen.
Kill is enabled for alive, ending, stopped and dormant agent rows. A dormant
agent's confirmation says that only native work is cancelled; no terminal remains
to close. It is unavailable for groups and unknown terminal state. `r` resumes a stopped or
dormant agent.

`m` sends to the selected agent, including stopped and dormant rows. For Claude,
Codex and Pi it wakes an ended agent in its saved conversation with the message
as the interactive starting prompt. The status line reports startup or pending
bridge delivery; saved/accepted does not mean read or answered. Further messages
to a starting bridge wait for readiness. Live plain sessions report unavailable.

During kill, `STATUS` immediately becomes `-` and `LIVE` becomes `ending` while
native work and the agent process are terminated. Once the native process exits,
`LIVE` is `stopped` while its tmux wrapper remains; after wrapper removal it is
`dormant`. The stopped stage can be brief during automatic teardown. A native
session exit automatically detaches clients viewing the agent pane, then removes
the wrapper and becomes dormant. Ctrl+C that only interrupts native work leaves
the session attached. Startup failures and older runners can retain stopped
wrappers; kill, resume, move and removal still clean them up.
If native cancellation fails after the child has exited, `K`/`s` still removes
the owned wrapper. The status line reports that the terminal closed but native
cancellation is unconfirmed; retry stop when the native service is reachable.
Ending agents still count as live in the header and groups, and cannot be moved
or removed. The last reported phase and summary are preserved.

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
columns are `name`, `harness`, `live`, `status` (`activity` in older views still works), `context` (`tokens` in older views still works; the latest request's tokens against the model's window), `cost` (off by default), `phase`, `summary`, `id`,
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

## Compaction settings

`c` opens the tree-compaction settings of the selected scope: the `compaction.json` at
its root, and the scope's agents. One screen edits the shared settings (`enabled`, the
default scheduler with its budget, image mode and parameters) and a section per
harness (its own `enabled`, default scheduler, and tree builder settings). A blank
field means "use the default", which each field shows as its placeholder. A save
writes the file as it was with only the edited fields changed; clearing a field deletes
its key. Ctrl+Enter saves, Escape closes and discards, and a rejected value is shown on
the screen's error line without closing it. If `compaction.json` cannot be used, the
reason shows at the top.

Enter on an agent in the table opens its screen: status, whether it follows the default
scheduler or has its own, and a per-turn plan preview (share, raw or summarized, tree
built or missing) that follows the fields as they are edited, without saving. Ctrl+Enter
saves the scheduler or returns the agent to the default. See [compaction.md](compaction.md)
for what the settings mean. The key is not in the footer, which is full.
