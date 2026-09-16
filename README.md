# Multiverse 2.0 — first working slice

Local universes, short prompts, ordinary shell tools. Models decide how to coordinate; a small CLI handles file integrity and terminal identity. The dashboard uses Textual; the orchestration core uses the Python standard library. This is a standalone implementation alongside V1, with no imports from it, no daemon and no MCP server.

The base bootstrap is under 200 words, plus agent identity and paths. It supplements the harness's normal prompt. Skills, workflows and commands stay on disk until needed. This follows [Pi's minimal, extensible approach](https://github.com/earendil-works/pi/tree/main/packages/coding-agent); it doesn't require using Pi.

## Try it

Requires Python 3.11+, tmux, and an installed/configured Codex, Claude Code or Pi CLI. Codex is the default. CLI flags were checked against Codex 0.154.0 and Claude Code 2.1.231; the optional Pi integration is tested against 0.84.3. macOS and Linux are the intended platforms; this version was exercised on macOS with tmux 3.6a.

From the repository root:

```sh
uv sync
./bin/mv2 tui --demo
```

The demo has a project universe, PR review and CAD subverses, nested verification, and sample agents. Its actions operate entirely in memory; it needs no tmux, model account or existing universe. The checkout launcher automatically uses `multiverse-v2/.venv` after setup.

For real work:

```sh
./bin/mv2 init /path/to/project
./bin/mv2 -u /path/to/project spawn --name worker --task 'Inspect this project and summarize its architecture.'
./bin/mv2 -u /path/to/project spawn --name reviewer --harness claude --task 'Review the current diff.'
./bin/mv2 -u /path/to/project tui
```

Or install with `pip install .` to put `mv2` on PATH, including its Textual dependency. No agent is launched just by opening the dashboard or initializing a universe.

```sh
mv2 -u /path/to/project subverse pr-review
mv2 -u /path/to/project/.multiverse/subverses/pr-review spawn --name reviewer --task 'Review the current branch using the review workflow.'
mv2 -u /path/to/project list --recursive
mv2 -u /path/to/project/.multiverse/subverses/pr-review send reviewer --text 'Focus on correctness and regression risks.'
```

`-u` selects a universe. Inside a spawned agent it defaults to `$MV2_UNIVERSE`; outside an agent it searches the current directory and ancestors. Cross-universe commands use an explicit `-u`. Agent references accept a full UUID, an unambiguous UUID prefix, or a unique name. Ambiguity is an error.

## Individual tools

Each action lives in `src/multiverse/tools/` with its own argument definitions
and importable `run` function. `mv2` remains a small compatibility dispatcher;
the TUI backend calls the same tool functions directly. Shared file, terminal
identity and launch mechanics remain in `store.py`, `tmux.py`, `agents.py` and
`runner.py`.

With the package installed, run a tool without the dispatcher:

```sh
python -m multiverse.tools.list_agents -u /path/to/project --recursive --json
python -m multiverse.tools.spawn -u /path/to/project --name worker --task 'Review the code.'
python -m multiverse.tools.inspect -u /path/to/project worker
```

For a checkout, use its Python environment with `PYTHONPATH=src`.
Run modules with `python -m`, rather than executing their files by path.
Tool names match CLI commands except `list` is `list_agents`, and hyphens become
underscores (`session_bind`, `runtime_update`). Every tool supports `--help`.
Python callers use, for example,
`spawn.run(root, name="worker", task="Review the code.")` or
`list_agents.run(root, recursive=True, json=True)`; results are returned and
errors raised, while shell entry points format output and exit codes.

This is a behavior-preserving extraction. `send`, `inbox` and `ack` still use the
durable file protocol; no terminal keystroke delivery is introduced.

## Files and ownership

```text
project/.multiverse/
  universe.json                 # ID, name, workspace and template provenance
  bootstrap.md                  # short automatic instructions
  harnesses/codex/              # argv manifest, short session registration prompt
  harnesses/claude/             # argv manifest, assigned native session identity
  harnesses/pi/                 # argv manifest, optional native bridge
  skills/coordination/          # entry.json metadata + SKILL.md
  commands/help/                # entry.json metadata + prompt.md
  workflows/review/             # entry.json metadata + prompt.md
  views/default.json            # local dashboard title, accent and columns
  state/                        # local, ignored by Git
    agents/<uuid>/
      properties.json           # identity, task, reported phase and summary
      runtime.json              # launch generation, host, pane, observed activity
      inbox.jsonl               # append-only messages with IDs and reply references
      inbox.cursor              # acknowledged byte offset
      session.jsonl             # native Pi conversation
      launches/<uuid>/          # exact bootstrap, task and argv for each launch
  subverses/pr-review/           # the same structure, recursively
```

Everything belongs to the universe, including its live state. Definitions and runtime have separate directories so a clean template install can exclude old conversations and processes. JSON describes current state, JSONL records messages, and Markdown holds prompts. Lock files and temporary files are implementation details.

`init --template NAME` looks for `~/.multiverse/templates/NAME`, then a bundled template; an explicit directory also works. `subverse NAME --template PATH` uses the same installer. It copies definitions, generates fresh IDs recursively, and starts with empty state. It does not silently update existing universes. Templates are trusted local files and may contain executable adapters; symlinks are rejected.

Subverses inherit the parent's workspace **at creation**, using a relative path. They receive their own clean definitions and IDs, with no implicit ongoing prompt inheritance. `subverse NAME --workspace /path/to/worktree` can target a worktree. The universe remains in the parent tree when that worktree is removed; `doctor` reports the missing workspace. A universe can have a manager agent, but none is mandatory or auto-created.

## The Textual dashboard

The dashboard is one compact hierarchy. The root universe and every nested subverse appear as colored, collapsible group rows, with their agents underneath. Vertical guides and rounded branch ends connect each group to its children; the lines follow the visible tree when folding or searching. All descendants are loaded together; there is no sidebar or local/all toggle. A skill supplies instructions to an agent; a subverse gives a group its own configuration, workflows, agent state and display identity. It behaves like a folder in the UI.

The [r/unixporn Catppuccin setups](https://www.reddit.com/r/unixporn/comments/1fao619/) informed the muted palette. Group accents and indentation distinguish subverses without separate navigation panels.

![Compact Textual hierarchy with colored subverse groups](docs/screenshots/dashboard.png)

One-line headers and footer hints leave the terminal for agents. There are no action buttons. Search opens with `/`, includes folded descendants, and keeps matching ancestors visible. Escape clears it and restores the existing folds. `v` opens full details. At smaller widths, secondary columns disappear; the demo's entire hierarchy fits at 60×20 with the inspector closed. `NO_COLOR` is respected.

Pause over a row for 300 ms to inspect it: current work, harness/model, observed activity, reported phase, and a live pane capture. The inspector sits to the right at 120 columns and wider, or becomes a short drawer below the list. Keyboard selection opens the same view; `p` toggles it and Escape dismisses it. Moving into the inspector keeps it open for reading and scrolling. Hovering never changes the selected agent or the target of actions. Groups show counts and recent work instead of a terminal. Captures refresh with the dashboard (normally every 1.5 seconds), retain your scroll position when reading older output, and discard results from a previous agent or launch.

`n` inserts a draft row in the selected group, following the original Multiverse monitor's inline creation pattern. Type over the suggested name; Tab cycles through name, harness, initial prompt, and optional model. Harness opens an inline selector populated from that group's `harnesses/*/harness.json`: arrows choose, Enter confirms, or click an option. Codex is preferred, then Claude, then the first configured adapter. It is not a free-text field. The initial prompt is the first instruction sent to the agent, not a scheduled job or workflow name. Enter in a text field creates the agent; Ctrl+Enter submits from any field, Escape discards, and Shift+Enter or paste allows multiline prompts. Creation failures retain the draft for correction. No agent starts before submission. Messages and subverse creation still use compact forms.

Filesystem and tmux work run outside the UI event loop. Stable agent keys preserve selection across refreshes. New agents and subverses belong to the selected group; selecting an agent targets its group. Drafts and dialogs capture the intended universe and recipient before submitting an action. Rendering and I/O are separated in `tui.py` and `dashboard.py` for testing.

### Agent identity

The TUI reads `properties.json` to discover agents and their reported work. It reconciles `runtime.json` with `tmux list-panes` to observe whether each launch is alive. Each pane carries three tags: universe ID, agent ID and launch ID. A readable window/session name is only a label. Renaming it does not change identity; a reused pane ID cannot impersonate an old launch. Unknown or inaccessible state is shown as unknown.

`LIVE` comes from tmux, `ACTIVITY` comes from the harness, and `PHASE`/`SUMMARY` come from the model. A live agent can report `done`; a stopped agent's last task can still say `working`. The dashboard preserves that distinction. It never fabricates progress by reading terminal text.

The dashboard is a reader and command client. Quit it and agents continue. The default tmux server is named `multiverse-v2`, separate from existing sessions; `MV2_TMUX_SOCKET` can select an explicit socket. Attach/inspect/stop verify pane ownership first. `stop` terminates the pane and its running work.

| Key | Action |
| --- | --- |
| ↑/↓ or k/j | Select a row |
| ←/h, →/l | Fold / parent; expand / first child |
| Enter / Space | Enter attaches to a live agent; either key folds a group |
| /, Escape | Search all groups; clear search |
| n / m | Spawn in selected group / message selected agent |
| g | Create a subverse inside the selected group |
| s / r | Stop after confirmation / resume |
| v / p | Full details / show or hide inspector |
| ? / q | Keyboard help / quit dashboard |
| F5 / Ctrl-p | Refresh / command palette |
| Tab / Shift-Tab | Move between inline draft fields or form fields |
| ↑/↓ in the harness field | Choose a configured harness |
| Enter / Escape in a draft | Confirm harness or create / discard the new agent |
| Escape / Ctrl-Enter | Cancel / submit a form |

Mouse: pause over a row to inspect; click a group to fold it, click an agent to select it, or double-click to attach. The footer key hints are clickable, including form submission and cancellation.

Detach from an attached agent with tmux's usual binding (normally Ctrl-b d). Each universe can customize `views/default.json`:

```json
{
  "title": "PR Review",
  "accent": "#cba6f7",
  "columns": ["name", "live", "phase", "summary"]
}
```

The title and accent style its group row and agents in the parent hierarchy. The base template uses `"accent": "auto"` to assign subverse colors consistently from their IDs; an explicit hex color overrides it. The root view chooses the shared table columns; opening the TUI directly at a subverse uses that subverse's column configuration. Arbitrary view plugins and dashboard links remain follow-up work.

## Messages, commands and prompts

`send` serializes concurrent appends under a file lock. Plain Codex, Claude and Pi adapters follow the short bootstrap: check the inbox at startup, between work steps and before finishing. **A queued message does not automatically wake an idle agent.** `inbox` peeks; `ack --cursor N` acknowledges the returned batch. Queued, read, and answered are different states. A crash around acknowledgement can cause duplicate delivery; IDs and `--reply-to` support correlation. This is a local, cooperative file protocol, not a security boundary or a distributed queue.

`catalog` reads only capability metadata. `command review` prints the matching prompt without executing it. `/multiverse list` and `/multiverse review ...` are prompt conventions. Native CLIs may intercept slash commands before the model sees them; send ordinary text such as `Please run /multiverse list`, or use `mv2 catalog` in a shell. Names must match exactly; qualify an ambiguous name as `workflows/review`. Workflow control flow remains prose interpreted by the model.

There are no added model tools. Agents use `$MV2_CLI` through their ordinary shell tool. Pi's bundled `extension.ts` is **optional and disabled by default**. Adding `"--extension", "{harness_dir}/extension.ts"` to its spawn/resume arrays enables automatic idle inbox delivery, lifecycle observations and a native `/multiverse` command. Those extras require native integration; launching a harness does not.

## Harness definitions

Adding a harness means adding `harnesses/NAME/harness.json` in that universe. No Python branch, TypeScript plugin or code registration is needed. Use ordinary JSON for configuration, Markdown for prompts and JSONL for append-only inboxes.

Each manifest declares `schema: 1`, `spawn` and optional `resume` argv arrays, optional `model_args`, `bootstrap` text, `unset_env` names and a docs URL. Arguments are passed directly to the process, never through a shell. Model flags are inserted before a `--` terminator when present, so prompts beginning with a hyphen remain literal. Available substitutions:

| Value | Meaning |
| --- | --- |
| `{name}`, `{agent_id}`, `{workspace}` | Agent name, UUID and working directory |
| `{agent_dir}`, `{harness_dir}` | Runtime files and local adapter definition |
| `{task}`, `{task_file}` | Initial prompt text or its saved file |
| `{bootstrap}`, `{bootstrap_file}` | Short bootstrap text or its saved file |
| `{bootstrap_json}` | Quoted bootstrap string, also suitable as a TOML string value |
| `{model}` | Explicit model override; omitted flags use the native default |
| `{session_file}`, `{session_id}` | File-based session path or recorded native UUID |

`session` defaults to `file` (Pi). `assigned` gives the harness the agent UUID at launch (Claude). `reported` requires the native UUID to be recorded through `mv2 session-bind` (Codex). Claude resumes that assigned UUID. Codex's startup prompt asks it to register `$CODEX_THREAD_ID` before working; resume fails clearly if this has not happened, rather than choosing another agent's session. See the [Codex adapter](src/multiverse/templates/base/harnesses/codex/README.md) and [Claude adapter](src/multiverse/templates/base/harnesses/claude/README.md) for native permission prompts, session recovery and integration limits. Activity remains `unknown` unless reported; liveness still comes from tagged tmux panes.

Existing universes own their copies of definitions. To add these adapters to an older universe, copy the `codex` and `claude` directories from `src/multiverse/templates/base/harnesses/` into its `harnesses/` directory and add the inbox checkpoint instruction from the base bootstrap. Do not overwrite custom definitions as part of a package update.

## Verification and next slices

```sh
.venv/bin/python -m unittest discover -s tests -v
./bin/mv2 -u /path/to/project doctor
```

The Textual tests use `App.run_test()` and Pilot to exercise real widgets, mouse clicks, keystrokes, forms, cancellation, recursive folding, search through folded groups, correctly scoped creation, stable selection, failure reporting, hover delays, stale captures, inline draft preservation, slow refreshes, and resizing down to 60×20. They need no terminal, sockets or model:

```sh
.venv/bin/python -m unittest discover -s tests -p test_tui.py -v
.venv/bin/python scripts/render_tui.py
```

The renderer exports actual SVG screens to `/tmp/multiverse-previews` for visual review, including the unified hierarchy, live inspector, narrow layouts and inline creation. Additional tests cover concurrent inbox writers, interrupted records, recursive installs, ambiguous commands, real tmux launch/rename/stop/resume and pane ownership, Textual attach/detach/exit, Codex/Claude argv and exact resume with fake executables, session registration guards, and real Pi delivery/resume with its optional extension and a local fake provider. They use temporary universes and isolated tmux sockets, with no network or model spend. Authenticated Codex/Claude model turns are not exercised. Pi/tmux checks skip when their binaries are absent. A sandbox that denies Unix sockets must allow the terminal integration tests explicitly.

`doctor` is a basic local diagnostic, not the proposed TVA evaluator. The next slices are optional native delivery integrations, richer views and artifact links, explicit template upgrades/inheritance, discovery across unrelated project roots, and reusable workflow/TVA evaluation templates. There is no scheduler, remote agent transport, automatic manager hierarchy, background ask primitive, or urgent mid-turn delivery in this slice.

## Development checkout

Run `uv sync --locked`, then `. scripts/dev-env.sh` in the main checkout to use
the local `.multiverse` and `.run/tmux.sock`. Launch `mv2 tui`. Worktrees share Git
history; runtime state and evidence are ignored. See `.multiverse/DEVELOPMENT.md`
for this machine’s active agents and migration conventions.
