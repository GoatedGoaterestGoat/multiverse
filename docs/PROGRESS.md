# Progress

Tracking what's been built, what we've learned, and what's next.

## Status at a glance

| Phase | Status | Notes |
|:------|:-------|:------|
| Phase 0: Foundation | 🟡 In progress | Repo set up, plugin scaffolding done |
| Phase 1: Branching conversations | ⚪ Not started | |
| Phase 2: Context management | 🟡 Partial | Context folding works (manual + auto-resume) |
| Phase 3: Parallel agents | ⚪ Not started | |
| Phase 4: Inter-agent communication | ⚪ Not started | |
| Phase 5: UI | ⚪ Not started | |
| Phase 6: Polish & distribution | ⚪ Not started | |

## What's been built

### Context-fold MCP tool (standalone)

Location: `context-fold-mcp/`

The original Python MCP server with two tools:
- **`context_info(session_id, cwd)`** — Returns a numbered table of contents of the conversation with sizes and previews
- **`context_fold(summary, start, end, session_id, cwd)`** — Folds a range of messages into an agent-written summary, creating a new session JSONL

Implementation details:
- Reads/writes Claude Code session JSONL files at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
- Smart target expansion: paired tool_use/tool_result entries pulled in automatically
- Request group expansion: if any entry in a `requestId` group is targeted, all are included
- Synthetic `[Acknowledged]` message inserted when needed for role alternation
- `parentUuid` chain re-linked after deletions
- Fold operation traces (the `context_info` calls used to decide what to fold) removed from the new session
- Non-destructive: original session preserved, new session has a new ID

Demo artifacts:
- `context-original.md` — 248 messages, ~42,954 tokens
- `context-folded.md` — same session with messages 20–75 folded → 160 messages, ~31,515 tokens

### Multiverse plugin (current)

Location: `multiverse-plugin/`

Packages the context-fold tool as a proper Claude Code plugin with **automatic session resumption** via a small wrapper script.

**Components:**

| File | Purpose |
|:-----|:--------|
| `.claude-plugin/plugin.json` | Plugin manifest |
| `.mcp.json` | MCP server config (uses `uv run`) |
| `pyproject.toml` | Python package definition |
| `src/server.py` | MCP server with `context_info` and `context_fold` tools |
| `src/{types,session,rewriter}.py` | Core JSONL parsing and rewriting (carried over from standalone) |
| `hooks/hooks.json` | PostToolUse hook config (matches `context_fold`) |
| `scripts/on-context-fold.sh` | Hook script — returns `{continue: false}` when control file is set |
| `bin/multiverse` | Wrapper CLI — runs `claude` in a loop, dispatches control actions |
| `README.md` | Plugin documentation |

**How the auto-resume works** (the key innovation over the standalone MCP server):

1. Each `multiverse` invocation creates a per-session temp dir via `mktemp` and exports `MULTIVERSE_CONTROL=<temp>/control.json`
2. `claude` is spawned, inheriting the env var (which propagates to MCP server processes and hooks)
3. When the agent calls `context_fold`, the MCP tool writes a JSON action to `$MULTIVERSE_CONTROL`
4. The PostToolUse hook detects the non-empty control file and returns `{"continue": false}`
5. Claude exits cleanly
6. The wrapper reads the action and dispatches:
   - For `resume_folded`: sends a continuation prompt via `claude -p "Continue..." --resume <new_id>`, then resumes interactively with `claude --resume <new_id>`
7. Loop continues — the user is back in an interactive session with the folded context

**Concurrency handling**: each wrapper instance has its own `mktemp`-generated control file, so multiple concurrent `multiverse` sessions never collide.

**Graceful degradation**: if the wrapper isn't used (plain `claude --plugin-dir ./multiverse-plugin`), the MCP tool detects the missing env var and falls back to printing `Run: claude --resume <new_id>` — the user resumes manually.

## What we learned

### About `/compact` (from claude-code-src exploration)

- `/compact` is implemented in `commands/compact/compact.ts` and `services/compact/compact.ts`
- It works by **operating inside the `query()` async generator** — no session restart involved
- The `CompactionResult` is built (`buildPostCompactMessages()`), each message is `yield`ed to the REPL, and `setMessages()` hot-swaps `messagesRef.current`
- The same generator instance continues with the compacted context for the next API call
- A compact boundary message with `parentUuid: null` severs the old chain on disk while in-memory state continues seamlessly
- There's also a "session memory compaction" fast path that uses pre-extracted insights instead of an API summarization call

### About `--resume` behavior

- In **interactive mode**, `claude --resume <id>` always waits for user input regardless of the last message type in the JSONL
- The REPL loads the conversation as `initialMessages` and just displays it — no auto-response logic
- In **print mode**, `claude -p "prompt" --resume <id>` does send the prompt and gets a response, then exits
- There's no `--initial-prompt` or `--auto-continue` flag for interactive mode
- The `-c`/`--continue` flag just resumes the most recent conversation; it doesn't auto-respond

### About plugin capabilities and limits

What plugins **can** do via the public API:
- Intercept tool calls (PreToolUse, PostToolUse) and modify input/output
- Stop the session entirely (`continue: false`)
- Add context (`additionalContext`)
- Provide MCP tools, skills, agents, hooks, LSP servers
- Read the session JSONL via `transcript_path` from hook input
- Inherit environment variables from the parent claude process

What plugins **cannot** do:
- Hot-swap the in-memory message array (no access to `messagesRef`, `setMessages`, or the `query()` generator)
- Restart a session from within a hook (no `restart` action)
- Wrap the `claude` process itself (plugins run *inside* claude, not around it)
- Replace the live conversation context the way `/compact` does

The wrapper script approach is the only way to achieve auto-resume from a plugin, because the restart inherently requires an external process.

### About MCP tool naming in hooks

- Hook matchers use the tool name as it appears in claude
- For MCP tools provided by plugins, `context_fold` (substring) appears to match — the matcher is regex-style
- The full prefixed name format may vary; using the bare tool name as the matcher is the most portable approach

## What's next

### Immediate (Phase 0 → Phase 2 completion)

- [ ] Test the plugin end-to-end with `claude --plugin-dir ./multiverse-plugin`
- [ ] Test the wrapper auto-resume flow with `bin/multiverse`
- [ ] Verify the MCP tool name matcher (`context_fold`) actually fires the hook — may need to adjust if Claude Code uses a fully-qualified name like `mcp__context-fold__context_fold`
- [ ] Add a `recall` MCP tool for expanding folded content back (Phase 2 roadmap item)
- [ ] Decide on context manager model strategy (Haiku vs same-as-agent)

### Phase 1: Branching conversations

- [ ] Conversation tree data structure on top of JSONL `parentUuid` links
- [ ] "Branch from here" / "Merge back" / "Restore without revert" operations
- [ ] CLI slash commands as plugin skills

### Phase 3: Parallel agents

- [ ] Use Claude Agent SDK to spawn parallel sessions
- [ ] Wire up git worktree management (`isolation: "worktree"` in agent definitions)
- [ ] Add `spawn_agent` action to the wrapper dispatch loop

### Phase 4: Inter-agent communication

- [ ] Asker/responder protocol implementation
- [ ] `agentA-agentB.jsonl` mailbox files
- [ ] Add `send_message` action to the wrapper dispatch loop
- [ ] Context assembly logic per the rules in the root README

### Phase 5: UI

- [ ] Web/TUI for timeline visualization
- [ ] Branch/merge controls
- [ ] Live tail of agent conversations

### Phase 6: Distribution

- [ ] Plugin marketplace setup
- [ ] CHANGELOG, semver discipline
- [ ] User-facing installation docs

## Open questions

These are unresolved design decisions worth discussing before committing to implementations:

1. **Tool name matching**: does `context_fold` match `mcp__context-fold__context_fold` in hook matchers, or do we need the fully-qualified name? (Test required.)
2. **Wrapper distribution**: ship `bin/multiverse` as part of the plugin (and ask users to add it to PATH), or as a separate install?
3. **Control file action vocabulary**: should we standardize the JSON schema for actions now, or let it grow organically as we add features?
4. **Context manager model**: cheap+fast (Haiku) for summarization, or same model as the agent for consistency?
5. **Recall mechanism**: how should the agent un-fold previously folded content? Re-fetch from the original session, or store summaries with pointers back?
