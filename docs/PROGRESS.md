# Progress

Tracking what's been built, what we've learned, and what's next.

## Status at a glance

| Phase | Status | Notes |
|:------|:-------|:------|
| Phase 0: Foundation | 🟡 In progress | Repo set up, plugin scaffolding done |
| Phase 1: Branching conversations | ⚪ Not started | |
| Phase 2: Context management | 🟢 Substantial | Context folding (legacy) + frontier-coherent compaction with cached privileged tree-build, two-JSONL mirror, monitor-driven Compact + Visualize widgets, per-session auto-build toggle |
| Phase 3: Parallel agents | 🟡 Partial | Multi-session monitor + per-cwd dashboard + dormant session tracking |
| Phase 4: Inter-agent communication | 🟡 Active | User→agents ask-all + agent-callable `list_agents` / `message_agent` / `ask_agent` / `spawn_agent` with file-based inbox delivery and monitor-brokered spawn approval |
| Phase 5: UI | 🟡 Partial | Monitor TUI as the first UI surface; remote browser access via the `w` web share (ttyd-over-monitor + outbound tunnel) |
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

### Multi-session monitor TUI (POC)

Location: `multiverse-plugin/src/monitor.py`, `multiverse-plugin/bin/multiverse`, `multiverse-plugin/bin/multiverse-session`

The wrapper script was rewritten from "single claude in a loop" into a **multi-session orchestrator**. Each claude runs inside its own detached tmux session; a Textual TUI lists them and lets the user attach, detach, spawn, resume, or kill.

**New components:**

| File | Purpose |
|:-----|:--------|
| `bin/multiverse` | Launches the Textual monitor (forwards args as default claude args) |
| `bin/multiverse-session` | Per-session inner loop: runs one claude, dispatches `resume_folded`, passes `--plugin-dir` for dev mode |
| `src/monitor.py` | Textual `App` with a `DataTable` of sessions, modals for new/resume, keybindings |
| `scripts/set-status.sh` | One-liner called by status hooks with `idle`/`working`/`waiting`/`ended` |
| `hooks/hooks.json` | Added `SessionStart`, `UserPromptSubmit`, `Notification`, `PostToolUse` (`.*`), `Stop`, `SessionEnd` handlers alongside the existing `context_fold` hook |

**State layout** (`~/.multiverse/`):

```
sessions/<mv-id>/
  meta.json        — cwd, created_at, claude_args, name, optional resumed_from
  status.json      — {"status": ..., "ts": ...} written by hook scripts
  claude_uuid      — current claude session UUID (SessionStart hook → record-claude-uuid.sh)
  control.json     — context-fold signaling channel

asks/<ask-id>.json — inter-agent ask metadata (from/to/cwd/question/wait_for_response).
                     Persistent until respond_to_agent deletes it; survives crashes.

inbox/<encoded-cwd>/<recipient-name>/<ask-id>.json
                   — pending message file (question or response). Written atomically
                     by ask_agent / respond_to_agent. Consumed (deleted) by the
                     deliver-inbox hook on first delivery.

spawn_requests/<req-id>.json           — agent-initiated subagent spawn request.
spawn_requests/<req-id>.resolved.json — user's naming decision (O_EXCL-atomic so
                                          concurrent monitors can't double-resolve).
                                          Both files cleaned up by the MCP tool
                                          when it returns to the caller.
```

**How it fits together:**

1. `bin/multiverse` preserves the user's `$PWD` as `MULTIVERSE_ORIG_CWD`, then execs `uv run python -m src.monitor`
2. Pressing `n` or `R` in the TUI writes meta/status/control files and runs `tmux new-session -d -s multiverse-<id> -e MULTIVERSE_… -c <cwd> multiverse-session [claude-args...]`
3. Each claude session is launched with `--plugin-dir $MULTIVERSE_PLUGIN_ROOT` so dev-mode hooks fire without needing a marketplace install
4. Hooks write to `$MULTIVERSE_STATUS` (per-session), the TUI polls every second and repaints
5. Attach runs `tmux attach-session` inside `App.suspend()`; detach with `Ctrl-b d` returns to the monitor
6. Resume screen lists existing claude sessions from `~/.claude/projects/<encoded-cwd>/*.jsonl` (newest first, with first-user-message preview) and de-dups: if a live multiverse session already wraps the picked UUID, attach to it instead of spawning a fork

**Keybindings:**

| Key | Action |
|:----|:-------|
| `n` | New session — name-only prompt (cwd is fixed at monitor launch) |
| `R` | Resume — pick from claude sessions in the monitor's cwd (live/ended wraps hidden) |
| `a` | Ask all named agents a temporary question (one-shot, context-aware, not persisted) |
| `Enter` | Live rows → attach; ended/dormant rows → resume |
| `k` | Kill session (tmux kill + remove state dir) |
| `r` | Manual refresh |
| `q` | Quit monitor (sessions keep running in tmux) |

**Dashboard columns**: `ID · Name · Inbox (📩 N) · Status · Updated · Preview`. Inbox shows pending inter-agent messages waiting on this agent's hooks. Preview shows the first non-system user message from the agent's transcript (cached by `(path, mtime)`).

### Dashboard: per-cwd scope + dormant sessions

The monitor is scoped to a single cwd — whichever `MULTIVERSE_ORIG_CWD` was set when `bin/multiverse` was launched. Other multiverse instances manage other cwds independently. The dashboard surfaces two kinds of rows, both filtered to the monitor's cwd:

1. **Named multiverse sessions** (live or ended) — from `~/.multiverse/sessions/<id>/meta.json`. Unnamed sessions are hidden per design.
2. **Dormant named claude sessions** — claude JSONLs in `~/.claude/projects/<encoded-cwd>/` that have a user-assigned `custom-title` and aren't wrapped by any multiverse session. Enter on one of these resumes it (reaping the ended multiverse meta first if needed, so the name is free).

**Dedup** uses each session's `claude_uuid` (hook-captured) with `resumed_from` as a fallback. Reading every JSONL every tick would be expensive — metadata is cached by `(path, mtime)` so dormant scans are cheap once warm.

The resume widget hides wrapped UUIDs (both live and ended) — each claude session therefore has exactly one canonical place in the UI.

### Ask-all: user→agents temporary questions

Location: `multiverse-plugin/src/ask_agents.py`

Press `a` in the monitor to fan out a one-shot question to every named session in the cwd. Each target gets a forked subprocess:

```
claude --plugin-dir $MULTIVERSE_PLUGIN_ROOT --resume <uuid> --fork-session \
       --session-id <fresh-uuid> --tools "" --max-turns 1 -p "<wrapped question>"
```

Responses are collected in parallel (`ThreadPoolExecutor`, max 5) and shown in a scrollable modal with per-agent elapsed time. The fork JSONL is `unlink`ed in a `finally` block after the subprocess exits, and active fork UUIDs are tracked in a locked set so they're hidden from both the dashboard and resume widget for their brief lifetime.

**The wrapped prompt** is a verbatim copy of claude-code's `utils/sideQuestion.ts` — same `<system-reminder>` wording that the built-in `/btw` uses. This is by design: we want the side question to present identically to the model across both paths.

**Cache behavior**: from an external subprocess we can't call claude-code's internal `getLastCacheSafeParams()`, which is what `/btw` uses to guarantee a byte-identical prefix against the live process's last request. The subprocess must rebuild `systemPrompt`/`userContext`/`systemContext` from scratch. If the build is deterministic (no `--agent`, `--system-prompt`, `--append-system-prompt`, or coordinator mode), the rebuilt bytes match → cache hit on the prefix. This is equivalent to `/btw`'s documented fallback path. For dormant sessions the cache is cold anyway, so rebuilding is the only option.

**Tool containment**: `--tools ""` is the documented disable-all sentinel; `--max-turns 1` caps at one assistant turn. Both are backed by the strict side-question prompt. A subprocess timeout (60s default) is the final guard.

### Inter-agent comms (agent-callable MCP tools)

Location: `multiverse-plugin/src/server.py`, `src/inbox.py`, `src/ask_agents.py`, `scripts/deliver-inbox.py`

The MCP server (renamed from `context-fold` → `multiverse`) exposes four agent-callable tools for inter-agent coordination, all scoped to the calling agent's cwd by default. The design splits into **discovery**, **persistent messaging**, **ephemeral side-questions**, and **subagent spawning**:

- **`list_agents(max_agents=15, working_directory="")`** — Returns named agents in the workspace (live, ended, dormant). Calling agent pinned to the top with a `← this is you` marker. Sorted by status priority (live → idle → dormant → ended) and truncated when long. Optional `working_directory` argument lets the caller list agents in a *different* workspace; the self-marker is suppressed in that mode and the summary line includes the workspace path.
- **`message_agent(target_name, message)`** — Persistent, fire-and-forget. Drops a message into the recipient's inbox, returns immediately. Recipient sees it as a system-reminder injected into their next turn (or on resume if dormant — auto-resume kicks in). The recipient has full tool access and the message persists in their conversation history. Replies are done by calling `message_agent` back at the original sender; there is no separate "respond" tool. **Locked to the caller's workspace** — no cross-workspace messaging in v1.
- **`ask_agent(target_name, question, timeout_seconds=60, working_directory="")`** — Ephemeral side-question. Reuses the ask-all fork-session mechanism but for a single named target: spawns `claude -p --resume <uuid> --fork-session --session-id <fresh> --tools "" --max-turns 1`. Blocking — returns the answer inline or `"<agent>" did not respond within Ns` on timeout. Target's main session is NOT involved; nothing persists. Optional `working_directory` argument lets the caller ask an agent in a *different* workspace — the fork subprocess runs at the target's cwd and writes its ephemeral JSONL into the target's claude project dir, so cross-workspace asks are read-only and isolated by construction.
- **`spawn_agent(instruction, name="")`** — Start a brand-new subagent with `instruction` as its first user message. User confirms + names via a monitor-popped modal (300s timeout). Blocking. The subagent has no memory of the caller's conversation — if context is needed, the caller must include it in `instruction`. See the "Agent-initiated subagent spawning" subsection below for the full protocol.

The split is intentional: `message_agent` is for instructions and complex requests to an existing agent (target reads, runs commands, thinks); `ask_agent` is for facts the target already knows (no tools, no context pollution); `spawn_agent` is for delegation to a fresh context.

**Cross-workspace asymmetry**: `list_agents` and `ask_agent` accept an optional `working_directory` so an agent can discover and query agents living in another cwd. `message_agent` deliberately does not — durable messages can carry instructions and tool-using work, so cross-workspace messaging would let one agent direct another to act in a workspace they were never authorized to see. Read-only side-questions are the safer primitive, and that's all v1 allows across workspaces.

**Delivery via inbox + hook injection** (only used by `message_agent` — `ask_agent` is a forked subprocess, separate path):

1. `message_agent` atomically writes (`tmp + rename`) a single JSON file to `~/.multiverse/inbox/<encoded-cwd>/<recipient>/<message-id>.json` containing `{message_id, from, body, ts}`.
2. `scripts/deliver-inbox.py` is wired to **three hooks** on every multiverse-managed claude session: `SessionStart`, `UserPromptSubmit`, `Stop`. On each fire it lists pending files; if any exist, formats them as one `<system-reminder>` block, returns the right injection JSON for the firing event, and **deletes** the consumed files.
3. Whichever hook fires first wins — subsequent hook fires see an empty inbox. Once injected, the system-reminder lives in the recipient's conversation history forever, so re-injection isn't needed.
4. **Stop hook is the post-turn delivery path**: claude-code's hook output schema doesn't expose `additionalContext` for Stop, so we use `decision: "block"` with the message body as `reason`. This becomes a synthetic `"Stop hook feedback:\n…"` user message and triggers another model turn — exactly the wakeup we want when a message arrives mid-task.
5. **Critical write order**: when `message_agent` auto-resumes a dormant target, the inbox file is written **before** spawning so SessionStart fires after the file exists. Reverse order races the file write against tmux startup — SessionStart sees an empty inbox and the message sits unread until UserPromptSubmit fires later (when the user types something).

**Notification filtering**: claude-code's `Notification` hook fires for *both* permission prompts AND idle alerts (`notification_type: "idle_prompt"`, "Claude is waiting for your input"). The original handler set status to `waiting` for any notification, which then overwrote the post-Stop `idle` status on every turn. New `scripts/on-notification.sh` reads the hook input JSON, ignores informational types (`idle_prompt`, `elicitation_*`, `auth_success`, `computer_use_*`), and only sets `waiting` for genuinely blocking notifications.

**Dashboard surfacing**: the monitor table now has an `Inbox` column showing `📩 N` when an agent has pending `message_agent` messages, plus a `Preview` column showing the first user message (cached metadata, populated for both live and dormant rows).

**Earlier ask/respond design (replaced)**: an earlier iteration had `ask_agent` (with `wait_for_response` flag) and `respond_to_agent`, both routed via the inbox plus a persistent `~/.multiverse/asks/<ask-id>.json` ask-record for response routing. That's been retired in favor of the cleaner two-tool split above. The asks/ directory is no longer touched (safe to delete on disk).

### Agent-initiated subagent spawning (`spawn_agent`)

Location: `multiverse-plugin/src/spawn_requests.py`, `src/server.py`, `src/monitor.py`

The fourth inter-agent tool. An agent calls `spawn_agent(instruction, name="")` to start a fresh subagent in the workspace, with its own `instruction` as the new session's first user message. The user retains control — the monitor pops a naming widget that the user must approve before the session is created.

**Flow:**

1. Agent calls `spawn_agent(instruction, name="<preferred>")`.
2. MCP tool writes `~/.multiverse/spawn_requests/<req-id>.json` (atomic tmp+rename) with the instruction, preferred default name, and the caller's tmux session name.
3. Tool fires `tmux display-message` on the caller's pane so a user attached to the caller's tmux session sees "'<caller>' wants to spawn a subagent — detach (Ctrl-b d) to approve in the monitor." — best-effort status-line notification.
4. Tool polls `<req-id>.resolved.json` every 250ms (timeout: 300s).
5. Monitor scans `spawn_requests/` on each 1s tick. Enqueues new cwd-matching, unresolved requests into a per-monitor queue. Only one modal at a time; if multiple requests arrive concurrently they queue behind the active modal. Modal only opens when no other modal is already open.
6. Modal shows `'<from>' wants to spawn a subagent:` + truncated instruction preview + name input seeded with the caller's preferred name (or a random one if that's taken). User hits Enter to accept, Esc to reject.
7. On accept → monitor writes `<req-id>.resolved.json` with `{status: "accepted", name: "<chosen>"}` (`O_EXCL`-atomic — first writer wins across monitors). On Esc/cancel → `{status: "rejected", reason: "cancelled"}`.
8. Tool reads the resolution. If accepted, calls `create_session(cwd, claude_args=[instruction], name=chosen)` — the instruction becomes a positional arg to `claude`, which auto-submits it as the first user message (verified via `claude-code-src/screens/REPL.tsx`'s `initialMessage` effect). Returns `"Spawned agent with name '<chosen>'"` to the calling agent. On reject/timeout, returns an error string.
9. Tool cleans up both files on exit.

**Summarization is deliberately out of scope for v1.** Claude-code's `/compact` is in-process (hot-swaps `messagesRef`) and can't be triggered externally. Instead, the tool's docstring tells the agent to include a targeted summary in the instruction itself if prior context matters — the caller knows its own context best.

**Multi-monitor coordination:** all cwd-matching monitors enqueue the same request and all pop their own modal when their queue is empty. First monitor to write the resolution file wins via `O_EXCL`; losers detect the external resolution on their next tick and dismiss their modal. A monitor whose modal is dismissed before its user has typed anything writes a `rejected` resolution that `O_EXCL`-no-ops. On concurrent acceptance, the losing monitor's user sees a "another monitor already accepted this spawn as 'X'" notification.

**Stale request handling:** the MCP tool writes a timeout resolution on its way out, so any monitor with the modal still open dismisses cleanly. Stale request files (tool crashed before cleanup) are skipped by `list_pending_requests` via the `timeout_seconds` field vs `ts` comparison.

**Context assembly note:** the subagent has zero memory of the caller's conversation. If it needs context, the caller must put it in the `instruction`. This is a hard boundary — principled, not a limitation — and it means spawned subagents start from a clean prompt cache rather than a forked one.

**Auto-detach while attached to tmux** (`_SpawnAttachGuard` in `monitor.py`): Textual's `App.suspend()` pauses the event loop during a tmux attach, so the normal 1s tick can't detect incoming spawn requests while the user is watching an agent work. The fix is a background thread started alongside each `tmux attach-session` — it polls `spawn_requests/` every second and runs `tmux detach-client -s <session>` when one arrives, force-booting the user back to the monitor. When `_attach_to` sees `guard.forced_detach = True`, it sets `_pending_reattach = <tmux-session>`; the next `_tick_spawn_requests` call with an empty queue + no open modal automatically reattaches the user. The reattach re-enters `_attach_to` with a fresh guard, so consecutive spawn bursts keep cycling detach → modal → reattach without duplicating state. `tmux_session_exists` guards against reattaching to a session the user killed from the dashboard meanwhile.

### Frontier-coherent compaction

The major Phase-2 addition. Replaces ad-hoc range-folding with a full pipeline: every turn gets a bottom-up summary tree, the user picks a token budget, and each tree is expanded to its proportional slice. The compacted output goes back into the live JSONL as a resumable session.

**Two-JSONL architecture** (`src/full_jsonl.py`, `scripts/mirror-jsonl.py`):

- `~/.claude/projects/<slug>/<uuid>.jsonl` — LIVE: what claude reads/writes; compaction rewrites this.
- `$MULTIVERSE_ROOT/full_jsonls/<uuid>.jsonl` — FULL: append-only mirror of every row that ever appeared in live. Source of truth for token counts, turn counts, and tree building.
- `$MULTIVERSE_ROOT/full_jsonls/<uuid>.marker` — UUID of the last live row already copied into full. Tracked by uuid (not byte offset) so the mirror keeps working after compaction has rewritten live with a different prefix.

`mirror-jsonl.py` runs on `UserPromptSubmit` / `Stop` / `SessionEnd`. Bootstraps full from live on first call, then incrementally appends new rows. Heals parentUuid pointers when they'd otherwise break the chain (next subsection).

**Privileged tree-build** (`vendor/compaction/`):

Per-turn summary trees built bottom-up by an LLM with one tool — `merge(start, end, summary)` — that collapses a contiguous frontier span into a summary node. The "privileged" path uses `claude --resume` against a synthetic session JSONL we write to `~/.claude/projects/<slug>/<fresh-uuid>.jsonl`. Thinking blocks in the trace are emitted verbatim with their original signatures; server-side decryption validates against the same subscription credential, so the merging LLM sees the actual chain-of-thought when picking merges and writing summaries. No `ANTHROPIC_API_KEY` needed.

Cache: `$MULTIVERSE_ROOT/trees/<claude-uuid>/<turn-id>.json`. Each tree file embeds the full forest + merge history. `scripts/build_tree_priv_cli.py` builds one turn; `scripts/tree-builder-worker.py` orchestrates a forest build.

**Compact widget** (`src/compact_runner.py`, `src/monitor.py:CompactScreen`):

`c` in the dashboard. State machine:

1. `acquire_lock` → cross-monitor exclusive flag at `$MULTIVERSE_ROOT/compacting/<uuid>.json` (O_EXCL with stale-PID recovery). Other monitors render the row as `compacting` and refuse Enter/Kill.
2. Kill tmux only (state dir preserved so the multiverse name + identity survive the compaction).
3. `sync_full` to bring full up to live.
4. Wait for any in-flight auto-worker (tail its log to the UI).
5. Spawn `tree-builder-worker --all-turns` if any turn still missing a tree. Cache-aware, so it only builds the gaps.
6. `EXPANDING`: load each tree from cache, run `proportional` scheduler to allocate per-turn budgets ∝ each turn's full-token count, expand each tree to its slice via `expand_to_budget`, render the new compacted session JSONL via `compact_forest_to_session_jsonl`, atomically replace live, reset the mirror marker.
7. `release_lock`.

UI in input phase shows total uncompacted tokens + turn count + an audit of cached trees + ETA (seeded from any past worker.log build times). Running phase streams worker.log + an updating cached count + ETA with a rolling 20-turn window.

**Visualize action** (`scripts/visualize_live.py`, `src/monitor.py:action_visualize`):

`v` in the dashboard. Chain-walks the live JSONL, renders each turn as HTML — cached trees get the full nested view with frontier highlighting matching what's currently in live; uncached turns show as a flat leaf listing. Output at `/tmp/multiverse-viz-<uuid>.html`, auto-opened via `open`/`xdg-open` when available. Same gate as Compact minus the state restriction (read-only, safe at any status). Built on top of `scripts/visualize.py` + `scripts/visualize_forest.py` which were vendored from `compaction/scripts/`.

**Auto tree-build toggle** (`src/auto_build.py`):

Background builds are **opt-in per session**, off by default. Each build is a privileged `claude --resume` call — expensive enough that defaulting to "always on" wastes credits on sessions that never reach compaction. Marker file at `$MULTIVERSE_ROOT/auto_build/<claude_uuid>`; presence = enabled. `b` in the dashboard toggles for the highlighted row. The "Build" column on the dashboard shows on/off per row. `spawn-tree-builder.py` checks the marker as the first thing after reading the hook input and returns silently if absent.

### Code layout: data layer extracted

Location: `multiverse-plugin/src/sessions.py`

The pure data layer (no Textual, no in-process state) was extracted from `monitor.py` into `sessions.py` so the MCP server can use it without dragging the TUI in. Includes: `Session`, `DashboardRow`, `_read_session_metadata` (with `(path, mtime)` cache), `list_sessions`, `build_dashboard_rows`, `wrapped_claude_uuids`, `list_named_agents_in_cwd`, `find_agent_by_name`, plus `create_session` / `kill_session` (moved from monitor so the MCP server's `ask_agent` / `spawn_agent` can auto-resume dormant targets and create fresh subagents without inverting the dependency direction).

`monitor.py` now only contains UI concerns (`MonitorApp`, all `*Screen` modal classes — including `SpawnRequestScreen`) plus thin wiring.

`spawn_requests.py` is a tiny sibling module for the spawn-request filesystem protocol: atomic request writes, `O_EXCL`-atomic resolution writes (so racing monitors can't double-resolve), and staleness checks. Used by both the MCP server (`spawn_agent` tool) and the monitor's request watcher.

### Remote access — web share (`w`)

The Option-C remote-dashboard MVP (full setup in `multiverse-plugin/`-relative
[`docs/remote-access.md`](remote-access.md)). A monitor keybinding (`w`) toggles
an **ephemeral, per-workspace web share** so the dashboard — including
live-attach into a running agent — is reachable from any browser.

Location: `multiverse-plugin/src/web_share.py` (headless lifecycle), wired into
`src/monitor.py` (`WebShareScreen`, `action_share_web`, `🌐 web: ON` header tag).
`bin/multiverse` sources `$MULTIVERSE_ROOT/web-tunnel.env` and `setup` advises on
the `ttyd` / `cloudflared` deps.

**Architecture** (all co-located on the box — coordination is local tmux + files):

```
browser (xterm.js via ttyd) → outbound tunnel → ttyd -W -i 127.0.0.1 -c mv:<token>
  → tmux new-session -A -s mvweb-<hash> -c <cwd> bin/multiverse
    (a dedicated monitor scoped to that one workspace)
```

- **Live-attach works** here (unlike `textual serve`) because ttyd gives the
  monitor a real PTY, so its `_attach_to` (`with self.suspend(): tmux
  attach-session`) behaves as on a local terminal.
- The share runs **detached, so it survives the local TUI quitting** — close
  your terminal and keep controlling remotely. On-disk record at
  `$MULTIVERSE_ROOT/web_shares/<hash>/record.json`; any later monitor for the cwd
  shows it ON and can stop it (`w` → `s`); `web_share.status()` reaps a share
  whose processes died.
- Pluggable `TunnelProvider`: `quick` (cloudflared quick tunnel + ttyd token,
  zero-setup, default) and `cloudflare` (named tunnel + Access; auth policy set
  in Cloudflare — free SSO or paid mTLS).

**Auth model** (defense in depth): the share is effectively an RCE endpoint (a
writable terminal into a full-tool-access claude + the box env), so auth is
mandatory and the gate must cover the WebSocket. ttyd binds loopback only; TLS
terminates at the tunnel; a per-share token (`ttyd -c`, covers the WS) is
minted/revoked per share; edge identity (Cloudflare Access SSO or mTLS) is the
strong outer gate.

**Decision trail**: overlay VPNs (Tailscale/ZeroTier) were rejected — both need
the *remote* box enrolled in the mesh (can't be promised; ZeroTier also wants
root+TUN). IP-allowlisting was rejected — only the public egress IP is visible
(dynamic/NAT-shared/weak). Landed on Cloudflare named tunnel + Access, with the
ttyd token as a second factor.

**Status**: verified working end-to-end (Ai2 Beaker container → laptop browser,
`https://mv.silicon23.me` via a named tunnel + Google-SSO Cloudflare Access).
Enhancements since the MVP: backbone chosen at `w`-time (Enter = configured /
cloudflare, `q` = always-available token-only quick tunnel); optional ttyd token
for the cloudflare backbone (`MULTIVERSE_WEB_TOKEN` / `t` toggle, default off —
Access is the gate); `-P 30` keepalive for Cloudflare's ~100s idle-WS cutoff;
`--grace-period 3s` for snappy teardown; and **concurrent multi-workspace shares
via path slots** (`MULTIVERSE_WEB_SLOTS="label:port …"` → `https://<host>/<label>/`
under one hostname + one Access app, ttyd `-b /<label>`). Setup persists via an
idempotent script hooked into the container's `env_init`. The one-time Cloudflare
domain/tunnel/Access setup is user-side; see [`docs/remote-access.md`](remote-access.md).

## What we learned

### Remote web access: textual-serve can't attach; Cloudflare mTLS is paid

- `textual serve` / textual-web **cannot** deliver live-attach: `App.suspend()`
  is a documented no-op when served (no controlling TTY), so the monitor's
  suspend + `tmux attach` silently breaks. Wrapping the monitor in **ttyd**
  instead works, because ttyd hands the app a real PTY. That's why the web share
  is ttyd-over-monitor, not `textual serve`.
- Cloudflare Access **mTLS with your own CA is a paid feature** (Business+); the
  free plan can't use a custom CA in Access policies (free-plan users hit
  "maximum certificates reached" with 0 certs). Free Zero Trust **does** include
  Access **SSO / one-time-PIN** identity policies — strong per-user auth, any
  browser, no install. So device-binding (mTLS) is paid; identity-binding (SSO)
  is free. There is no "cloudflared account" — just a (free) Cloudflare account +
  a domain; `cloudflared` is only the connector binary.
- Overlay VPNs (Tailscale/ZeroTier) give device-key auth but require the *remote*
  box to join the mesh — a non-starter when you can't enroll a daemon there (and
  ZeroTier needs root + a TUN device, worse for locked-down containers).

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

### About `--plugin-dir` (from the monitor POC)

- `claude --plugin-dir <path>` loads a single plugin directory (one containing `.claude-plugin/plugin.json`) for the session only — no install step, overrides installed plugins of the same name
- Exactly right for dev iteration; the monitor passes `MULTIVERSE_PLUGIN_ROOT` through `tmux new-session -e` and the inner script prepends `--plugin-dir` to every claude invocation
- Source: `setInlinePlugins()` in `claude-code-src/main.tsx`, `loadSessionOnlyPlugins()` in `claude-code-src/utils/plugins/pluginLoader.ts`

### About long-session resume prompts

- Resuming a long-context session now surfaces a cost-warning prompt ("resume normally / resume from summary")
- That prompt blocks *before* `SessionStart` fires — detached tmux sessions sit silently at the prompt and the monitor's status stays `starting` indefinitely
- Fix: attach, answer the prompt, detach. No programmatic workaround yet

### About Textual `App.suspend()`

- `with self.suspend(): subprocess.run(...)` cleanly hands the terminal to an external process and restores the TUI on return
- Used for `tmux attach-session` — no manual raw-mode juggling required
- Textual 8.x supports this directly

### About `/btw` internals (from claude-code-src/commands/btw + utils/sideQuestion)

- `/btw` is a React-backed slash command (`commands/btw/btw.tsx`), not a CLI-exposed feature — plugins and external subprocesses cannot invoke it directly
- Its actual work is in `utils/sideQuestion.ts` → `runForkedAgent` (`utils/forkedAgent.ts`), which takes a `CacheSafeParams` blob and runs a single-turn, tool-denied API call
- The blob comes from `getLastCacheSafeParams()` — exact `systemPrompt`/`userContext`/`systemContext` bytes captured in stopHooks on the main thread's last request. Reusing them guarantees prompt cache hit
- Fallback (first turn, or prompt-suggestion disabled): rebuild from scratch via `getSystemPrompt` / `getUserContext` / `getSystemContext`. May miss cache if `--agent`/`--system-prompt`/coordinator extras were applied
- **We can only do the fallback path from outside.** `claude -p --resume --fork-session` rebuilds the same way — byte-identical iff the same extras (or lack thereof) apply

### Hidden CLI flags (worth knowing about)

- `--max-turns <N>` — early-exits after N agentic turns. Only works with `--print`. Hidden via `.hideHelp()` in `main.tsx`, so it doesn't show in `claude -h`. Use this to hard-cap one-off subprocess asks instead of fighting with prompts.
- `--tools ""` — disable ALL tools (documented sentinel for the `--tools` variadic). Cleaner than enumerating tool names or using `--permission-mode plan` (which has other side effects).
- `--session-id <uuid>` — explicitly name the session file. Only valid with `--continue`/`--resume` when combined with `--fork-session`. Essential for ask-all-style flows where we need to delete the fork JSONL afterward and therefore need to know its filename up front.

### Textual CSS gotcha — `height` includes padding

- `height: 1; padding-top: 1;` on a `Static` renders as zero visible rows, not one. Textual's box model puts padding *inside* the declared height, so the content gets squeezed to 0.
- Use `height: auto` (and let padding add on top) for any short hint/label with padding. Bit us across all of the modal dialogs — hints were invisible until we noticed.

### SessionStart `additionalContext` is buffered, not autonomously processed

- Confirmed by reading `claude-code-src`: SessionStart's `hookSpecificOutput.additionalContext` is wrapped as an `AttachmentMessage` (type `hook_additional_context`) in `utils/sessionStart.ts:163-172` and added to the in-memory message array via `useDeferredHookMessages.ts:28`. The REPL renders it immediately so a user attaching can see the message in the transcript — but `screens/REPL.tsx:3105` only `await awaitPendingHooks()` *before* `onQuery`, and `onQuery` only fires on user submit. So the model is **not autonomously queried** when SessionStart returns additionalContext — it has to wait for the next user-initiated turn.
- Symptom: a `message_agent` to a dormant recipient auto-resumes the recipient, the inbox file is consumed by the SessionStart hook (so the dashboard `📩 N` drops to 0), but the model doesn't actually engage with the message until a human attaches and types something. The message is in claude's memory and visible in the transcript, but unprocessed.
- Fix: `bin/multiverse-session::prime_inbox_if_needed` runs a print-mode `claude -p "You have N pending messages…" --resume <uuid>` at the top of each loop iteration when inbox is non-empty. Print mode does trigger `onQuery`, so the buffered additionalContext + the synthetic prompt go to the model in a single API call. Model processes the messages, response lands in JSONL, then the interactive claude resumes and surfaces it. Applies to every resume path (user-Enter, `message_agent` auto-resume, post-fold).
- Compared to `UserPromptSubmit`: that hook's additionalContext flows through `processUserInput.ts` and gets appended to `newMessages` directly in the same query — synchronous with the user's input. No buffering issue there.

### Hook output capabilities are hook-event-specific

- The `hookSpecificOutput.additionalContext` field is **NOT** supported on every event. The Zod union in `claude-code-src/types/hooks.ts` only lists it for `PreToolUse`, `UserPromptSubmit`, `SessionStart`, `Setup`, `SubagentStart`, `PostToolUse`, `PostToolUseFailure`, `Notification`. **`Stop` is missing.**
- For Stop hooks, use `decision: "block"` with the message body as `reason`. The runtime renders that as a synthetic user message prefixed with `"Stop hook feedback:\n"` and triggers another model turn — which doubles as a "wake from idle" mechanism for inbox delivery.

### `Notification` hook fires for non-blocking events too

- `notification_type` is the discriminator. Known types: `idle_prompt` (the "Claude is waiting for your input" alert that fires after `messageIdleNotifThresholdMs`), `worker_permission_prompt`, `elicitation_complete`, `elicitation_response`, `auth_success`, `computer_use_enter`, `computer_use_exit`, plus permission-request types.
- Don't use the Notification hook as a generic "agent is blocked" signal — read the type and only react to the genuinely blocking ones, otherwise you'll overwrite `idle` with `waiting` after every turn once the idle timer fires.

### macOS bash 3.2 + `set -u` + empty arrays

- Apple ships bash 3.2 (licensing). Under `set -u`, expanding `"${ARR[@]}"` when `ARR=()` is empty raises "unbound variable" — the script dies before its first command runs.
- Fix: `${ARR[@]+"${ARR[@]}"}` (the canonical "expand only if set" pattern). Works on bash 3.2 and 4+. Bit us in `bin/multiverse-session` — new sessions launched via plain `multiverse` (no claude args) crashed silently and the wrapper wrote `"ended"` immediately on exit.

### `~/.multiverse` diverges silently when HOME differs between parent and child

- Some deployments (e.g. Ai2 Beaker containers) wrap the `claude` CLI in a bash function that sets `HOME=/weka/.../claude_code_N` so claude's own `~/.claude` state lands on persistent storage, while the user's shell (and thus `bin/multiverse` and the monitor) runs with `HOME=/root`. That already requires a `/root/.claude → /weka/.../.claude` symlink for claude-code state to stay coherent.
- The bug: the plugin used to derive `~/.multiverse` from `HOME` in multiple module-level constants (`src/inbox.py`, `src/sessions.py::STATE_DIR`, `scripts/deliver-inbox.py`). So the monitor wrote state to `/root/.multiverse/`, the MCP server wrote state to `/weka/.../claude_code_N/.multiverse/`, and the two never met. Inbox delivery, spawn-request brokering, and the dashboard's inbox counter all silently broke. Session state (`sessions/<id>/...`) happened to work because `MULTIVERSE_STATUS` / `MULTIVERSE_CONTROL` were already being passed as absolute paths via `tmux new-session -e` — but only those two. Internal inconsistency.
- Fix: `src/paths.py::multiverse_dir()` resolves in this order: `$MULTIVERSE_ROOT` → grandparent of `$MULTIVERSE_STATUS` → `~/.multiverse`. `bin/multiverse` exports `MULTIVERSE_ROOT="${MULTIVERSE_ROOT:-$HOME/.multiverse}"` before launching the monitor, and `create_session` propagates it via `tmux -e`. All HOME-derived path constants now route through this. The `scripts/deliver-inbox.py` hook (standalone script) inlines the same three-tier resolution.
- General principle: anything that flows across the monitor ↔ tmux-spawned-process boundary must be an absolute path, not HOME-derived. The plugin already did this for session state; the inbox/spawn-request paths were the outliers.

### Fork subprocesses silently corrupt the caller's state dir if env isn't sanitized

- `ask_agent` (and any future tool that spawns `claude -p --resume … --fork-session` as a raw subprocess) inherits the **caller's** `MULTIVERSE_STATUS` / `MULTIVERSE_SESSION_*` env vars by default. The fork's hooks (`record-claude-uuid.sh`, `set-status.sh`, `deliver-inbox.py`) then fire with those envs and write into the **caller's** state dir — overwriting the caller's `claude_uuid` with the fork's/resumed session's UUID.
- Symptom: caller disappears from the monitor dashboard and a phantom dormant row appears for its real JSONL, because `wrapped_claude_uuids` is now pointing at a different JSONL than the caller actually owns. `claude --resume` outside the monitor still shows the truth; only the multiverse view is wrong.
- Fix: `src/ask_agents.py::_env_without_session_vars()` strips all `MULTIVERSE_*` from the subprocess env before spawning. All hook scripts already no-op when those vars are missing, so stripping is safe.
- **Not affected:** `create_session` (both from the monitor and from `spawn_agent`). That uses `tmux new-session -d -e …`, which properly overrides inherited env in the new session — verified empirically, including the nested-tmux case.
- Recovery after a corrupted claude_uuid: manually write the correct UUID into `~/.multiverse/sessions/<mv-id>/claude_uuid`. The correct UUID is in `meta.json`'s `resumed_from` (if resumed) or in the JSONL file with the matching `customTitle`.

### `uv run <script>` requires the project to be packaged

- Plain `uv run script-name` does not actually invoke `[project.scripts]` entries unless the project has a `[build-system]` (so uv can install a wheel into the venv). Without it, uv silently skips entry-point installation — it warns once at sync time but the entries don't exist at run time.
- Fix: add a minimal hatchling block:
  ```toml
  [build-system]
  requires = ["hatchling"]
  build-backend = "hatchling.build"
  
  [tool.hatch.build.targets.wheel]
  packages = ["src"]
  ```
- After this, `.mcp.json` can dispatch the server via `uv --directory ${CLAUDE_PLUGIN_ROOT} run multiverse-mcp` and the entry actually resolves.

### Claude code's REPL drops markdown `'html'` tokens entirely

`utils/markdown.ts:formatToken` has, at the bottom of the switch:
```ts
case 'def':
case 'del':
case 'html':
  return ''
```

When the markdown lexer (`marked`) sees a balanced HTML-like wrapper such as `<compacted-context entries="N">…</compacted-context>` or `<tool-use name="Bash">{…}</tool-use>`, it produces ONE `'html'` token covering the whole block — content and all — and the renderer outputs nothing. Empty bullet in the UI.

Practical consequence: anything we want the live agent to see in a compacted text block has to be markdown, not XML-ish wrappers. We switched to:
- `**Compacted summary (N entries):**` + plain paragraph for Node summaries
- `**Tool call: \`Bash\`**` + ` ```json` fenced block for tool_use leaves rendered as text
- `**Tool result:**` + ``` fenced block for tool_result leaves rendered as text

Also: surviving `tool_use`/`tool_result`/user-role text leaves should be emitted as NATIVE block types in their proper-role rows, not as markdown text inside an assistant message. Claude code has dedicated renderers (`AssistantToolUseMessage`, `UserToolResultMessage`, `UserTextMessage`) that bypass the markdown pipeline. Markdown is only the fallback for things that can't survive as native blocks (Node summaries, asst_text in an assistant message, embedded-user-message synthetics).

### The cwd-slug bug saga

Claude code's project-slug rule for `~/.claude/projects/<slug>/` is `re.sub(r"[^a-zA-Z0-9]", "-", cwd)` — replace any non-alphanumeric with `-`. Three places in the plugin re-implemented this and TWO of them got it wrong with `cwd.replace("/", "-")` (only replacing slashes):

- `vendor/compaction/priv_session.py:cwd_slug` — used to write synthetic session JSONLs for the merge LLM. With the buggy version, `claude --resume` couldn't find the synthetic for any cwd with non-slash special chars, returned "No conversation found" in ~1s, and every merge call aborted. Subtle because the OK turns (1-leaf or empty) succeeded without ever needing to find the synthetic, and `build_tree_priv_cli.py` exits 0 even when construction aborts — so the worker log just showed "turn done" for each.
- `scripts/spawn-tree-builder.py:_cwd_slug` — used to find the live JSONL to verify the session is persisted. With the buggy version, `source.is_file()` returned False and the hook returned silently, so the worker subprocess never got spawned for any session in a cwd with underscores. We thought auto-build was working on remote because some sessions had worker.log activity — but those were all from manual `c` runs, which build the path directly and bypass the hook.

Both surfaced only on a remote cwd with underscores (`/weka/.../3d_boundingbox_detection/video_data_experiments/droid_pipeline`). Local testing under `/Users/michaelli/Documents/Multiverse` had no underscores so the two rules collided on identical output.

Lesson: when a piece of behavior depends on matching some external tool's path convention, centralize the rule in one place — don't re-implement the heuristic at each call site.

### parentUuid heal across compaction boundaries

When we compact, the new live JSONL ends in a *synthetic* uuid (whatever `compact_forest_to_session_jsonl` generated for the last row). That uuid is unique to live — it never made it into full. After the user resumes and prompts again, the next row claude appends has `parentUuid: <synthetic>`. Mirror copies that row verbatim to full, but full doesn't contain the synthetic — so the chain breaks. Chain-walking from the new tip stops at the broken parent, returning only the post-compaction sub-conversation.

Symptom: CompactScreen on a session that had been compacted once showed wildly wrong token + turn counts.

Fix: heal on write. When mirror is about to append a row whose `parentUuid` isn't in full's existing uuid set, rewrite it to point at full's actual last uuid. The chain stays continuous, the rest of the row's content is preserved verbatim. Applied in both implementations (`scripts/mirror-jsonl.py` for the hook fast path; `src/full_jsonl.py:sync_full` for the runner / library callers). For files that were already corrupted before the fix landed: `scripts/heal-full-jsonl.py` does a one-pass repair, atomically rewriting via tempfile.

Separately, claude code sometimes writes session-restart marker rows with `parentUuid: null` mid-file. The healer treats any null-parent row that isn't the file's first uuid'd row as also broken and rewires it to the previous in-file uuid.

### Chain-walking is mandatory for revert-aware sessions

A claude code "restore conversation" doesn't delete rows from the JSONL — it appends a new branch whose first row's `parentUuid` points back at the restore target. The abandoned subtree stays in the file. So a JSONL is a DAG (multiple leaves, one of which is the current live tip), not a linear sequence.

A linear read of the file (which `parse_session` originally did) treats every uuid'd row as part of the conversation — including the reverted branches. Trees got built over abandoned content; compaction "summarized" content the user explicitly threw away.

Fix: `_live_chain_rows(path)` identifies the live tip (DAG leaf with the latest timestamp; tie-break by latest file appearance), walks `parentUuid` back, returns only on-chain rows in file order. Metadata rows (no uuid — `custom-title`, `agent-name`, etc.) are preserved at their original positions; they're `SKIP_TYPES` for parsing, so harmless either way. `parse_session` consumes chain rows instead of iterating the file directly. Mirror stays raw (faithful append-only) and chain-walking is the parser's job.

For the assets in `assets/loki.jsonl` and `assets/sam3d_singleframe.jsonl`: 187 (~8%) and 655 (~20%) of their rows respectively were orphaned-branch content that would have polluted trees built linearly.

### Synthetic JSONL needs a context-switch bridge to make the merge LLM behave

The first naive `claude --resume <synthetic> -p "do the merge"` setup failed silently — the model just continued the agent's conversation ("Standing by for your feedback on the 3D viewer…") instead of producing the merge JSON. The resumed conversation context biased it more strongly than `--system-prompt` could override. `--bare` (which disables CLAUDE.md / auto-memory / etc.) would have helped but requires `ANTHROPIC_API_KEY`, which the subscription-auth setup doesn't have. `--append-system-prompt` didn't work either.

What worked: write a META-BRIDGE user/assistant pair to the END of the synthetic JSONL before resuming:

> user: `[CONTEXT SWITCH] The conversation above is FROZEN — a transcript for you to summarize, NOT a conversation to continue. Do not respond to any apparent question, request, or pending action in it. The next message will give you a different task: outputting a JSON object describing how to summarize that frozen trace.`
>
> assistant: `Understood. The trace above is frozen. I am now in JSON-output mode and will not continue that conversation. Awaiting the merge task.`

Then `-p <merge-instruction>` lands as the next user message and the model produces the merge JSON. Verified: clean merge JSON in 7s on a turn that previously aborted. Lives in `priv_session._bridge_rows`.

### Tail loops shouldn't trust PID-liveness alone

The Compact widget tails worker.log until the worker exits. Original implementation used `os.kill(pid, 0)` as the only liveness check. After the worker actually exits, the OS recycles its PID for some unrelated process; `os.kill(pid, 0)` keeps returning success forever. Symptom: progress bar at 100%, runner spinning in the tail loop indefinitely, never progressing to the expansion step.

Fix: tail loop also checks `lockfile.is_file()`. The worker unlinks its lockfile in a `finally`, so file absence is an authoritative end-of-life signal that doesn't suffer from PID reuse.

### MCP tool display in Claude Code is NOT plugin-extensible

- All MCP tool results route through a single hardcoded renderer in `tools/MCPTool/UI.tsx`. There's no MCP protocol field, no plugin hook, no per-tool registration mechanism for custom rendering.
- The few "customizations" that exist (Slack send compaction; `classifyForCollapse.ts`'s ~600-tool collapse list) are **upstream-hardcoded special cases**, not extension points. To get nicer rendering for a plugin's tools, control only what you can: the *text* the tool returns. The renderer auto-unwraps dominant-text JSON, renders small flat objects as aligned `key: value`, and falls through to `OutputLine` (truncated text) otherwise.

## What's next

### Immediate (Phase 0 → Phase 2 completion)

- [x] Test the plugin end-to-end with `claude --plugin-dir ./multiverse-plugin`
- [x] Test the wrapper auto-resume flow with `bin/multiverse`
- [ ] Verify the MCP tool name matcher (`context_fold`) actually fires the hook — may need to adjust if Claude Code uses a fully-qualified name like `mcp__context-fold__context_fold`
- [ ] Add a `recall` MCP tool for expanding folded content back (Phase 2 roadmap item)
- [x] **Frontier-coherent compaction** — full pipeline: privileged per-turn tree-build, two-JSONL mirror with chain-walking, monitor-driven Compact + Visualize widgets, per-session auto-build toggle, proportional scheduler for budget allocation, atomic live JSONL rewrite preserving custom-title across the compaction. See "Frontier-coherent compaction" above.
- [ ] Retire the legacy `context_fold` MCP tool once all existing sessions have been migrated forward
- [ ] Improve tree shape: today the merging LLM tends to pick `span [0, N)` and collapse the whole frontier in one merge, producing shallow trees with only two expansion levels (root summary or all leaves). Constraints in the merge tool schema (cap `end - start` or a fraction of frontier) would force hierarchy.
- [ ] Compacting-state UX: today a compacted session ends in status `ended` (we kill tmux but keep the multiverse state dir to preserve the name). User feedback suggests "dormant" would feel cleaner — implies full reap + optional silent re-resume under the same name. Low priority.

### Monitor follow-ups

- [x] Track live claude UUID per multiverse session (via `SessionStart` hook → `claude_uuid` file) so we can dedup against dormant listings
- [x] Surface dormant named claude sessions on the dashboard; let Enter resume them directly
- [x] Hide live/ended wraps from the resume widget — each session has one canonical place in the UI
- [x] Scope each monitor instance to a single cwd (`MULTIVERSE_ORIG_CWD`); drop the CWD column from the dashboard
- [ ] Ended sessions linger on disk until manually killed — auto-reap on monitor start, or add a "purge ended" binding
- [ ] `claude_args` passed to `multiverse` are forwarded verbatim to every new session — probably want per-session args instead
- [ ] No way to trigger context-fold from the monitor itself; it still requires the agent inside a session
- [ ] Attach UX assumes default tmux `Ctrl-b` prefix; no custom keybinding yet
- [ ] Status transitions rely on hooks only — a detached session blocked on the long-resume cost prompt stays at `starting` forever
- [ ] Dedup by `resumed_from`/`claude_uuid` only catches the resume path; branching (explicit new branch off an existing session) will need its own action

### Phase 1: Branching conversations

- [ ] Conversation tree data structure on top of JSONL `parentUuid` links
- [ ] "Branch from here" / "Merge back" / "Restore without revert" operations
- [ ] CLI slash commands as plugin skills
- [ ] Explicit branch action in the monitor (distinct from resume — `resumed_from` dedup should not apply)

### Phase 3: Parallel agents

- [x] Multi-session wrapper + tmux multiplexing (POC)
- [ ] Use Claude Agent SDK to spawn parallel sessions
- [ ] Wire up git worktree management (`isolation: "worktree"` in agent definitions)
- [ ] Add `spawn_agent` action to the wrapper dispatch loop

### Phase 4: Inter-agent communication

- [x] User→agents fan-out (ask-all from the monitor)
- [x] Agent-callable `list_agents` MCP tool (workspace discovery)
- [x] Agent→agent direct ask — `ask_agent` + `respond_to_agent`, file-based inbox delivery
- [x] Auto-resume dormant agents on incoming ask
- [x] Stop-hook wakeup so mid-task asks get picked up after the current turn finishes
- [x] Notification filtering so `idle_prompt` doesn't overwrite `idle` with `waiting`
- [x] Inbox + Preview columns on the dashboard
- [x] Agent-initiated subagent spawning (`spawn_agent`) with monitor-brokered naming widget, queue-coalesced concurrent requests, tmux-pane notification to the caller's user
- [x] Fork-subprocess env leak fix (`ask_agent` no longer writes into the caller's state dir)
- [x] Cross-workspace discovery + ask: optional `working_directory` arg on `list_agents` and `ask_agent` lets an agent in cwd A query agents in cwd B (read-only side-question only — `message_agent` stays workspace-locked)
- [ ] Agent→agents MCP fan-out tool (the agent-callable equivalent of the `a` keybinding) — useful as a discovery prelude before a targeted `ask_agent`
- [ ] Multi-turn asker/responder protocol — `ask_agent` is currently one-shot per ask_id; threading subsequent follow-ups into the same conversation isn't supported
- [ ] Persistent thread mailbox (`agentA-agentB.jsonl`) for ongoing relationships, distinct from the per-message inbox files we have now
- [x] **Resume-without-engagement blind spot**: SessionStart's `additionalContext` is buffered in claude's REPL but doesn't autonomously trigger an API call, so a dormant recipient that gets `message_agent`'d (or any agent the user resumes via Enter) would have inbox messages sitting in memory with no model engagement until a human attached and typed. Fixed by `bin/multiverse-session::prime_inbox_if_needed`: at the top of each loop iteration, if inbox is non-empty AND `--resume <uuid>` is in CLAUDE_ARGS, run a print-mode `claude -p "You have N pending messages…" --resume <uuid>` first. SessionStart fires there too, delivers the inbox as `additionalContext`, and the synthetic prompt forces a model turn that processes both. Response lands in the JSONL; the interactive claude that follows resumes the same UUID and surfaces it to whoever attaches.
- [ ] **Idle-live blind spot** (still open): if recipient B is *live and idle* (not dormant, just waiting), no hook fires until something else triggers one — `message_agent` writes to inbox but B's interactive claude doesn't pick it up until UserPromptSubmit or Stop fires. Workaround: dashboard's `📩 N` indicator, plus the next-time-they-resume the prime above will catch it. Real fix would need a way to nudge a live-idle claude (none exists in the public hook API today).
- [ ] Context assembly logic per the rules in the root README
- [ ] Garbage-collect orphan asks (timed-out, unanswered) — currently `~/.multiverse/asks/<id>.json` lingers if respond is never called

### Phase 5: UI

- [x] **Remote access — web share (`w`)**: ephemeral per-workspace ttyd-over-monitor + outbound tunnel (Cloudflare named/quick), edge auth (Access SSO free / mTLS paid) + optional ttyd token, backbone/token chosen at `w`-time, and concurrent multi-workspace shares via path slots. Verified end-to-end on Beaker. See [`docs/remote-access.md`](remote-access.md).
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
