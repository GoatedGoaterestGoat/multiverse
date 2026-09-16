# Testing and development templates

Two bundled templates provide clean, reusable workspaces. They launch no agents
and run no checks during installation:

```sh
mv2 init /path/to/test-workspace --template testing
mv2 init /path/to/dev-workspace --template development
# Or add a department to an existing universe:
mv2 -u /path/to/universe subverse tests --template testing --workspace /path/to/test-worktree
```

Both include the plain declarative Codex adapter, the existing coordination
skill, and review workflow. Additional adapters can be copied from the base
template when needed. No adapter code or extension is required. Installed
universes own their definitions; later package updates do not silently replace
custom prompts or harness settings.

The automatic bootstrap adds only a pointer to `testing-role` or
`development-role`. `mv2 catalog` discovers metadata; `mv2 command NAME` loads
the exact workflow when relevant. Detailed task instructions remain on disk.

| Template | Lazy workflows | Evidence forms |
| --- | --- | --- |
| testing | testing-role, reproduce, retest, review | bug-report.md, retest-report.md |
| development | development-role, implement, handoff, review | change-report.md |

Copy a blank form under the universe's `artifacts/` directory for each case.
Record exact revisions, commands, exit codes, and evidence locations. Templates
contain no fabricated successful results or live session state. Testing owns
independent verification of a committed fix; development supplies the commit
and local checks. An assignment must explicitly authorize additional workers.

Each `views/default.json` has distinct title, columns and queue label, with a
markdown working agreement, an initially empty table, and links to the forms.
Add rows of strings to the table's `rows` array and relative file paths to the
artifacts widget as work progresses. These widgets follow the local view schema;
they display saved definitions and never schedule work or infer verification.
Keep linked artifacts inside the universe. A plain file reader can use the forms
and workflows even when the dashboard lacks the widget renderer.

For launch failures or ambiguous agent labels, see [launch recovery](launch-recovery.md).
Honor the assigned workspace, commit identity, socket and session binding rules.
Evidence should distinguish mocked checks, real terminal interaction, and actual
model turns. Durable sends remain queued until a recipient reads their inbox.
