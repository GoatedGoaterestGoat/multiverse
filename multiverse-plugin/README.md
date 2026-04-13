# Multiverse Plugin for Claude Code

Context management and multi-session orchestration for Claude Code — selective context folding plus a Textual TUI that manages parallel claude sessions.

## What it does

Two things, both designed to scale past the limits of a single `claude` process:

**1. Context folding.** When your conversation grows large, you lose context quality and hit token limits. `/compact` is all-or-nothing. Multiverse gives you **granular control**: fold specific message ranges into agent-written summaries, keeping what matters and discarding what doesn't. The agent picks what to fold (using `context_info`), writes the summary, and the session resumes automatically with the optimized context.

**2. Multi-session monitor (POC).** Running more than one claude at a time usually means juggling terminals. The `multiverse` command now launches a **Textual TUI** that tracks every session's status (`idle` / `working` / `waiting-for-permission` / `ended`), lets you attach to any of them, detach back with `Ctrl-b d`, spawn new ones, and resume old ones from disk. Each session runs in its own detached tmux session under the hood.

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- [uv](https://docs.astral.sh/uv/) (Python package runner) — resolves the Python deps including `textual`
- [jq](https://jqlang.github.io/jq/) (JSON processor) — needed by the wrapper scripts
- [tmux](https://github.com/tmux/tmux) ≥ 3.2 — each claude session runs inside a detached tmux session
- Python 3.11+

## Installation

### As a plugin (recommended)

```bash
# Load during development
claude --plugin-dir ./multiverse-plugin

# Or install from a marketplace (when available)
claude plugin install multiverse@your-marketplace
```

### With the wrapper (enables auto-resume + multi-session monitor)

The `multiverse` wrapper launches a Textual TUI that manages claude sessions. Without it, the plugin still works — you just run a single `claude` at a time and resume manually after folding.

```bash
# One-time: install Python deps (textual, mcp, etc.)
cd multiverse-plugin && uv sync && cd -

# Make the wrappers executable
chmod +x multiverse-plugin/bin/multiverse multiverse-plugin/bin/multiverse-session

# Add to your PATH (e.g. in ~/.zshrc)
export PATH="/path/to/multiverse-plugin/bin:$PATH"

# Launch the monitor
multiverse
```

Any arguments passed to `multiverse` are forwarded as default `claude` args to every session spawned from the TUI. The monitor auto-loads the plugin into each session via `claude --plugin-dir <plugin_root>`, so you don't need to install the plugin globally for dev work.

## Usage

### Monitor TUI

Run `multiverse` to launch the TUI. Each row is one claude session; statuses are driven by hooks (`SessionStart`, `UserPromptSubmit`, `Notification`, `PostToolUse`, `Stop`, `SessionEnd`).

| Key | Action |
|:----|:-------|
| `n` | New session — prompts for a working directory (defaults to where you launched `multiverse`) |
| `R` | Resume — browses `~/.claude/projects/<encoded-cwd>/*.jsonl` with first-prompt previews; Enter on a row resumes it |
| `Enter` | Attach to the highlighted session (drops into its tmux session) |
| `k` | Kill the highlighted session — tmux kill + remove state dir |
| `r` | Manual refresh |
| `q` | Quit the monitor — sessions keep running in tmux |

**Detach** from an attached session with the normal tmux prefix: `Ctrl-b d`. You land back in the monitor.

**De-dup**: if you pick a session UUID in the resume screen that's already running as a multiverse session, the monitor attaches to the existing instance instead of forking a new one.

**State** lives under `~/.multiverse/sessions/<mv-id>/` — one directory per session with `meta.json`, `status.json`, and `control.json`. Sessions survive monitor restarts (the TUI rebuilds its state from disk on launch).

### Automatic (agent-initiated)

The agent can use context folding autonomously when it detects the context is getting large or polluted. The MCP tools are available to Claude automatically:

1. **`context_info`** — Returns a numbered table of contents of the conversation with message types, sizes, and previews. The agent uses this to decide what to fold.

2. **`context_fold`** — Folds a range of messages into a summary. The agent writes the summary itself (it already has the full context).

### Manual (user-initiated)

Ask Claude to fold specific parts of the conversation:

> "Fold messages 20-75 — they were the debugging session for the auth bug, which is now fixed."

Or let Claude decide:

> "The context is getting large. Review it and fold anything that's no longer relevant."

### With vs without the wrapper

| Setup | Behavior after fold | Multi-session |
|:------|:---|:---|
| `multiverse` monitor | Session stops, resumes automatically with folded context. Claude continues immediately. | Yes — TUI + tmux |
| Plain `claude` | Session continues. Claude tells you to run `claude --resume <session_id>`. | No |

## How it works

### Architecture

```
┌───────────────────────────────────────────────────────┐
│  multiverse (bin/multiverse)                          │
│  Thin bash launcher — execs `uv run python -m         │
│  src.monitor`, preserves $PWD as MULTIVERSE_ORIG_CWD  │
└──────────────────┬────────────────────────────────────┘
                   │ launches
┌──────────────────▼────────────────────────────────────┐
│  Monitor TUI (src/monitor.py, Textual)                │
│  - Lists sessions from ~/.multiverse/sessions/        │
│  - Spawns via `tmux new-session -d -s multiverse-<id>│
│    -e MULTIVERSE_… -c <cwd> multiverse-session …`    │
│  - Attaches via `App.suspend()` + `tmux attach-…`    │
└──────────────────┬────────────────────────────────────┘
                   │ tmux new-session -d
┌──────────────────▼────────────────────────────────────┐
│  tmux session: multiverse-<id>                        │
│  └─ multiverse-session (bin/multiverse-session)      │
│     └─ claude --plugin-dir $MULTIVERSE_PLUGIN_ROOT …  │
│        ├─ MCP server: context_info, context_fold     │
│        ├─ Hooks: status writers + on-context-fold    │
│        └─ On context_fold: writes control.json,      │
│           hook returns {continue:false}, claude      │
│           exits, inner loop dispatches resume_folded │
└───────────────────────────────────────────────────────┘
```

Per-session state under `~/.multiverse/sessions/<mv-id>/`:

| File | Written by | Read by |
|:-----|:-----------|:--------|
| `meta.json` | monitor on spawn | monitor on startup/refresh |
| `status.json` | `scripts/set-status.sh` via hooks | monitor poll loop (every 1s) |
| `control.json` | MCP `context_fold` tool | `multiverse-session` loop |

### Communication protocol

The monitor and each session's plugin instance communicate through a **control file** — a per-session JSON file whose path is passed via `tmux new-session -e`:

```
MULTIVERSE_CONTROL=~/.multiverse/sessions/<mv-id>/control.json
MULTIVERSE_STATUS=~/.multiverse/sessions/<mv-id>/status.json
MULTIVERSE_PLUGIN_ROOT=/path/to/multiverse-plugin
MULTIVERSE_SESSION_ID=<mv-id>
```

The MCP server writes fold actions:
```json
{"action": "resume_folded", "new_session_id": "abc-123-..."}
```

The hook detects the action and stops the session; `multiverse-session` reads the action and dispatches it, staying inside the same tmux session. Separately, the status hooks write `{"status": "working", "ts": ...}` to `$MULTIVERSE_STATUS` on each hook event and the monitor polls those files.

### Control actions

| Action | Description | Fields |
|:-------|:-----------|:-------|
| `resume_folded` | Resume with folded context | `new_session_id` |
| *(future)* `spawn_agent` | Spawn a parallel agent session | `prompt`, `worktree` |
| *(future)* `send_message` | Inject a message into another session | `session_id`, `message` |

### What happens during a fold

1. Agent calls `context_info` to get the conversation table of contents
2. Agent identifies messages to fold and writes a summary
3. Agent calls `context_fold(summary, start, end, session_id, cwd)`
4. MCP tool creates a **new** session JSONL with the folded content (original preserved)
5. MCP tool writes a `resume_folded` action to the control file
6. PostToolUse hook detects the action and returns `{continue: false}`
7. Session stops cleanly
8. Wrapper reads the action, sends a continuation prompt, resumes interactively

### Key design decisions

- **Agent writes its own summaries.** The agent already has the full context and knows what's important. No external summarization model needed.
- **Non-destructive.** Original session JSONL is never modified. A new session is created with a new ID.
- **Graceful degradation.** Without the wrapper, everything still works — you just run single sessions and resume manually.
- **Concurrent-safe.** Each session has its own directory under `~/.multiverse/sessions/`; env vars are injected via `tmux new-session -e` so nothing leaks between sessions.
- **One multiverse session per claude UUID.** The resume screen records `resumed_from` in `meta.json`; a second resume of the same UUID attaches to the existing instance instead of spawning a fork. Branching (deliberate fork from an existing session) is a separate action on the roadmap.
- **Dev-mode plugin loading.** The inner script passes `--plugin-dir $MULTIVERSE_PLUGIN_ROOT` to every `claude` invocation, so the plugin works without a marketplace install.

## Plugin structure

```
multiverse-plugin/
├── .claude-plugin/
│   └── plugin.json           # Plugin manifest
├── bin/
│   ├── multiverse            # Launcher — execs the Textual monitor
│   └── multiverse-session    # Per-session inner loop (runs inside each tmux session)
├── hooks/
│   └── hooks.json            # Hook config: status writers + context_fold handler
├── scripts/
│   ├── set-status.sh         # Called by status hooks with idle/working/waiting/ended
│   └── on-context-fold.sh    # Stops the session after a fold so the inner loop can dispatch
├── .mcp.json                 # MCP server config
├── pyproject.toml             # Python package (MCP server + monitor)
├── src/
│   ├── __init__.py
│   ├── types.py              # JSONL entry types
│   ├── session.py            # Session file discovery and I/O
│   ├── rewriter.py           # Core folding algorithm
│   ├── server.py             # MCP server (context_info, context_fold)
│   └── monitor.py            # Textual TUI: session table, modals, tmux control
└── README.md
```

## Extending the plugin

The wrapper is designed to be extensible. To add a new action:

1. **Define the action** in the MCP server or a new MCP tool:
   ```python
   _write_control_action({"action": "your_action", "field": "value"})
   ```

2. **Add a hook** (if the action needs to stop the session) in `hooks/hooks.json`

3. **Add a case** to the wrapper's dispatch loop in `bin/multiverse`:
   ```bash
   your_action)
     FIELD=$(jq -r '.field' "$MULTIVERSE_CONTROL")
     # ... handle it
     ;;
   ```

The `MULTIVERSE_CONTROL` environment variable is available to all hooks and MCP server processes spawned by the Claude session.

## Authors

- **Silicon23** — [GitHub](https://github.com/Silicon23)
- **shaoxiongduan** — [GitHub](https://github.com/shaoxiongduan)
