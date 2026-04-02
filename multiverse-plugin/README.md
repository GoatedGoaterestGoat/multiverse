# Multiverse Plugin for Claude Code

Context management for Claude Code sessions — selective folding with seamless session resumption.

## What it does

When your Claude Code conversation grows large, you lose context quality and hit token limits. The `/compact` command helps, but it's all-or-nothing. Multiverse gives you **granular control**: fold specific message ranges into summaries you write yourself, keeping what matters and discarding what doesn't.

**Context folding** replaces a range of conversation messages with a concise summary. The agent identifies which messages to fold (using `context_info`), writes a summary (it already knows the content), and the session seamlessly resumes with the optimized context.

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- [uv](https://docs.astral.sh/uv/) (Python package runner)
- [jq](https://jqlang.github.io/jq/) (JSON processor) — only needed for the `multiverse` wrapper
- Python 3.11+

## Installation

### As a plugin (recommended)

```bash
# Load during development
claude --plugin-dir ./multiverse-plugin

# Or install from a marketplace (when available)
claude plugin install multiverse@your-marketplace
```

### With the wrapper (enables auto-resume)

The `multiverse` wrapper is a small shell script that sits around `claude` and handles the stop/resume cycle automatically. Without it, the plugin still works — you just resume manually.

```bash
# Make the wrapper executable
chmod +x multiverse-plugin/bin/multiverse

# Add to your PATH (e.g. in ~/.zshrc)
export PATH="/path/to/multiverse-plugin/bin:$PATH"

# Use it instead of `claude`
multiverse --plugin-dir ./multiverse-plugin
```

All arguments are passed through to `claude`. The wrapper is transparent — if no context folding happens, it behaves identically to running `claude` directly.

## Usage

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

| Setup | Behavior after fold |
|:------|:---|
| `multiverse` wrapper | Session stops, resumes automatically with folded context. Claude continues immediately. |
| Plain `claude` | Session continues. Claude tells you to run `claude --resume <session_id>`. |

## How it works

### Architecture

```
┌─────────────────────────────────────────────────┐
│  multiverse wrapper (bin/multiverse)             │
│  Runs claude, monitors control file, dispatches  │
│  actions (resume, future: spawn, send, etc.)     │
└──────────────────┬──────────────────────────────┘
                   │ spawns
┌──────────────────▼──────────────────────────────┐
│  Claude Code session                             │
│                                                  │
│  ┌─────────────────────────────────────────┐    │
│  │  MCP Server (context-fold)               │    │
│  │  Tools: context_info, context_fold       │    │
│  │  Reads/writes session JSONL files        │    │
│  │  Writes control actions when folding     │    │
│  └─────────────────────────────────────────┘    │
│                                                  │
│  ┌─────────────────────────────────────────┐    │
│  │  PostToolUse Hook (on-context-fold.sh)   │    │
│  │  Detects control action was written      │    │
│  │  Returns {continue: false} to stop       │    │
│  └─────────────────────────────────────────┘    │
└──────────────────┬──────────────────────────────┘
                   │ exits
┌──────────────────▼──────────────────────────────┐
│  multiverse wrapper reads control file           │
│  Action: resume_folded                           │
│  1. claude -p "Continue..." --resume <new_id>    │
│  2. claude --resume <new_id>  (interactive)      │
└─────────────────────────────────────────────────┘
```

### Communication protocol

The wrapper and plugin communicate through a **control file** — a JSON file in a per-session temp directory. Each wrapper instance gets its own file, so concurrent sessions never collide.

```
MULTIVERSE_CONTROL=/tmp/multiverse-XXXXXX/control.json
```

The MCP server writes actions:
```json
{"action": "resume_folded", "new_session_id": "abc-123-..."}
```

The hook detects the action and stops the session. The wrapper reads the action and dispatches it.

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
- **Graceful degradation.** Without the wrapper, everything still works — you just resume manually.
- **Concurrent-safe.** Each wrapper instance uses its own temp directory via `mktemp`.

## Plugin structure

```
multiverse-plugin/
├── .claude-plugin/
│   └── plugin.json          # Plugin manifest
├── bin/
│   └── multiverse           # Wrapper CLI script
├── hooks/
│   └── hooks.json           # PostToolUse hook config
├── scripts/
│   └── on-context-fold.sh   # Hook handler
├── .mcp.json                # MCP server config
├── pyproject.toml            # Python package (MCP server)
├── src/
│   ├── __init__.py
│   ├── types.py             # JSONL entry types
│   ├── session.py           # Session file discovery and I/O
│   ├── rewriter.py          # Core folding algorithm
│   └── server.py            # MCP server (context_info, context_fold)
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
