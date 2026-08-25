# Multiverse Plugin for Claude Code

Multi-agent orchestration for Claude Code: parallel sessions, inter-agent messaging, and frontier-coherent context compaction.

## What it does

Three things, all designed to scale past the limits of a single `claude` process:

**1. Multi-session monitor.** A Textual TUI launched via the `multiverse` command tracks every claude session in a workspace. Each session runs in its own detached tmux session under the hood — you can attach to any one, detach back with `Ctrl-b d`, spawn new ones with a name, resume dormant ones from disk, kill ones you're done with. Status (`idle` / `working` / `waiting` / `listening` / `dormant` / `ended` / `compacting`) is driven by hooks, the MCP server, and cross-monitor lock files. Each multiverse instance is scoped to one workspace (cwd).

**2. Inter-agent communication.** Agents in the same workspace can discover each other (`list_agents`), send each other durable messages with full tool access on the receiving side (`message_agent`), check or wait on their own inbox mid-turn (`check_inbox` / `monitor_inbox`), fire quick ephemeral side-questions (`ask_agent`), or delegate fresh work to a brand-new subagent (`spawn_agent`, with user-brokered naming). Plus TUI keybindings to ask all agents the same question at once (`a`) and peek at any agent's pending inbox (`i`). Claude receives queued messages through hooks; Codex explicitly claims them with `check_inbox`, prompted by push notifications and wrapper resumes when necessary. Ephemeral questions use native Claude or Codex forks that don't disturb the target's main session; spawns pop a naming modal in the monitor before the new session is created.

**3. Frontier-coherent compaction.** A bottom-up agentic compaction engine (vendored from `compaction/`) builds a summary tree per turn — a tree where every internal node carries an LLM-written summary of its children — then expands those trees to a user-chosen total token budget allocated proportionally per turn. Trees are pre-built in the background on every `UserPromptSubmit` so a manual compaction usually has nothing left to wait for. The monitor exposes a `c` action that pops a Compact widget showing total uncompacted tokens, asks for a budget, drives the build/expand pipeline with progress + log tail, and atomically replaces the live JSONL. Privileged-mode tree construction reads original chain-of-thought via signature decryption (no `ANTHROPIC_API_KEY` needed — uses the same `claude` subscription credential).

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Codex installed and authenticated if you plan to run Codex sessions; Claude-only
  operation does not require it
- Python 3.11+
- A system package manager for `multiverse setup` to install the rest: Homebrew (macOS), `apt`/`dnf`/`pacman` (Linux). If you'd rather install manually, the deps are [uv](https://docs.astral.sh/uv/), [jq](https://jqlang.github.io/jq/), and [tmux](https://github.com/tmux/tmux) ≥ 3.2.

## Installation

### As a plugin (single-session, no monitor)

```bash
# Load during development
claude --plugin-dir ./multiverse-plugin

# Or install from a marketplace (when available)
claude plugin install multiverse@your-marketplace
```

In this mode the MCP tools load, but the inter-agent tools need the `MULTIVERSE_*` env vars the monitor injects (so they error standalone), and there's no monitor TUI. Background auto tree-building still works via hooks if you create the marker file yourself.

### With the wrapper (full feature set: monitor + auto-resume + inter-agent)

The `multiverse` wrapper launches the Textual TUI that manages claude sessions and gives the plugin access to the `MULTIVERSE_*` env vars that the inter-agent tools need.

```bash
# 1. Clone the repo (anywhere — paths resolve relative to the script)
git clone https://github.com/Silicon23/Multiverse.git

# 2. Add the bin directory to your PATH (e.g. in ~/.zshrc)
export PATH="/path/to/Multiverse/multiverse-plugin/bin:$PATH"

# 3. One-time setup: installs tmux, jq, uv (prompting before each),
#    runs `uv sync`, and reports Codex CLI/plugin readiness.
multiverse setup

# 4. Launch the monitor in a workspace
cd /path/to/your/project
multiverse
```

`multiverse setup` is idempotent — re-run it anytime to sync Python deps after a
pull. A missing Codex CLI or plugin bundle is reported without failing setup, so a
Claude-only installation remains usable.

**Updating.** The plugin is under active development; to pull the latest changes from origin:

```bash
multiverse update   # git pull inside the clone
multiverse setup    # re-run if Python deps changed (pyproject.toml/uv.lock)
```

Any arguments passed to `multiverse` (other than a subcommand like `setup`/`update`) are forwarded as default `claude` args to every session spawned from the TUI. The monitor auto-loads the plugin into each session via `claude --plugin-dir <plugin_root>`, so you don't need to install the plugin globally for dev work.

### Machine-local config (`bin/multiverse.local`)

`bin/multiverse` sources an optional, **gitignored** file at `multiverse-plugin/bin/multiverse.local` before launching the monitor — the one place for host-specific settings, so they survive `git pull` and never get committed. Each knob defaults to something sensible when the file is absent (or when the file leaves it alone), so a checkout with no `multiverse.local` behaves out of the box.

| Variable | Default | What it does |
|:---------|:--------|:-------------|
| `MULTIVERSE_CLAUDE_BIN` | `claude` | The Claude binary the whole stack invokes — monitor-spawned interactive sessions, the `ask_agent` fork path, and compaction merge calls. Point it at an alternate build (e.g. a company-packaged `claude`) without editing tracked files. |
| `MULTIVERSE_CODEX_BIN` | `codex` | The Codex binary used for interactive sessions, native naming, and ephemeral `ask_agent` app-server forks. |
| `CODEX_HOME` | *(unset → `~/.codex`)* | Standard Codex configuration and session root. Set it when Codex uses a non-default home so discovery and resume target the same native sessions. |
| `MULTIVERSE_CODEX_PLUGIN_ROOT` | sibling `plugins/multiverse-codex` | Codex plugin bundle injected into wrapped sessions. Override it for packaged installs or a separate development checkout. |
| `MULTIVERSE_EXTRA_PLUGIN_DIRS` | *(empty)* | Colon-separated extra plugin dirs loaded into **interactive sessions** (each becomes its own repeated `--plugin-dir`), on top of the multiverse plugin itself. The fork/compaction calls deny all tools, so extra plugins are not loaded there. |
| `CLAUDE_CONFIG_DIR` | *(unset → `~/.claude`)* | Standard Claude Code config dir. The plugin reads transcripts from `$CLAUDE_CONFIG_DIR/projects`. Set it here if your `claude` build uses a non-default config dir and doesn't export this variable itself, so the monitor lists the right sessions. |
| `CLAUDE_CODE_MAX_RETRIES` | `30` | Rate-limit / transient-error retry count for the underlying claude CLI (its own default is 10). Multiverse tends to saturate rate limits — many active agents plus a background tree-build worker per session, all hitting one account — so claude's 10-retry / ~8-min ceiling burns through fast under a real traffic wave. This raises the ceiling. Set to `10` to restore claude's default; only takes effect on rate-limit / 5xx / connection failures, so a healthy request never touches it. |
| `CLAUDE_CODE_UNATTENDED_RETRY` | `1` | Enables claude's persistent-retry mode: backoff caps at 5min per attempt, total wait at 6 hours, with a heartbeat yield every 30s so the session isn't declared idle mid-wait. Ideal for background tree-build workers and long-running agents where waiting through a traffic wave beats failing outright. Feature-gated inside the claude CLI itself — harmless no-op on builds that don't have it compiled in. Set to `0` to opt out. |

Example — use alternate Claude and Codex installations and load an extra local
Claude plugin:

```bash
# multiverse-plugin/bin/multiverse.local — gitignored, per-machine.
export MULTIVERSE_CLAUDE_BIN="my-claude-build"
export MULTIVERSE_CODEX_BIN="my-codex-build"
export MULTIVERSE_EXTRA_PLUGIN_DIRS="/path/to/another-plugin"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.my-claude}"
export CODEX_HOME="${CODEX_HOME:-$HOME/.my-codex}"
```

These values are exported by `bin/multiverse` and propagated into every spawned session (the monitor adds them to each `tmux new-session -e` env), so a single edit reaches the monitor, interactive sessions, the inter-agent fork path, and compaction.

The launcher also atomically snapshots effective Codex configuration to
`$MULTIVERSE_ROOT/codex-env.sh`. From a later terminal with neither `CODEX_HOME`
nor `MULTIVERSE_CODEX_BIN` configured, it restores the saved home, binary, and
binary PATH before discovering or resuming sessions. Explicit environment values
or `bin/multiverse.local` settings take precedence and refresh the snapshot.

## Usage

### Monitor TUI

Run `multiverse` in your project directory. The TUI lists every multiverse session in this workspace, plus dormant (named-but-not-running) claude sessions you could resume.

| Key | Action |
|:----|:-------|
| `n` | New session — name-only prompt; cwd is fixed at the workspace where you launched `multiverse`. Starts in `--permission-mode auto` by default (see below). |
| `R` | Resume — pick from claude sessions in the workspace (sessions already wrapped by a live/ended multiverse session are hidden). Also starts in auto mode. |
| `c` | Compact the highlighted session (only allowed while it's `idle` / `ended` / `dormant`) — see "Frontier-coherent compaction" below. On a row whose compaction you detached with `d`, re-opens that screen instead |
| `v` | Visualize the highlighted session as a navigable HTML forest view — chain-walked turns with cached trees show frontier highlighting against the live JSONL; uncached turns show as a flat leaf listing. Output is `/tmp/multiverse-viz-<uuid>.html`, auto-opened via `open` / `xdg-open` when available. |
| `b` | Toggle background auto tree-build for the highlighted session — off by default; flip on per-session for the ones you expect to compact. State shown in the dashboard's "Build" column. |
| `a` | Ask all named agents a temporary question (one-shot, context-aware, not persisted) |
| `i` | View the highlighted agent's pending inbox — a read-only peek at queued `message_agent` messages. Does **not** consume them; Claude hooks or Codex's `check_inbox` still own delivery. |
| `Enter` | Live rows → attach (drops into the tmux session); ended/dormant rows → resume in place. On a `compacting` row: re-opens that compaction screen if you detached it with `d`, otherwise refused (it's held by another monitor) |
| `k` | Live/ended session → kill (tmux kill + remove state dir), no prompt. Dormant session → confirm dialog, then deletes its JSONL from disk. Refused while the row is `compacting` |
| `r` | Manual refresh |
| `q` | Quit the monitor — sessions keep running in tmux |

**Detach** from an attached session with the normal tmux prefix: `Ctrl-b d`.

The monitor attaches through a local PTY relay that answers tmux's DA1, DA2, and
XTVERSION capability probes before they reach the outer terminal. This prevents
delayed xterm.js replies such as `0;276;0c` from being mistaken for keyboard input
and inserted into an agent's prompt after an attach or reconnect.

**Auto permission mode by default.** Every session `create_session` spawns — new (`n`), resumed (`R` / `Enter` on a dormant/ended row, including right after compaction, which claude treats as a new session since it's a freshly-written live JSONL), auto-resumed via a push-notification `message_agent` call, or spawned via `spawn_agent` — starts with `--permission-mode auto` unless the caller already passed an explicit `--permission-mode` (e.g. via `multiverse --permission-mode plan`), which is left untouched.

**Dashboard columns**: `ID · Name · Inbox · Build · Status · Tokens · Updated · Preview`. The Inbox column shows `📩 N` when an agent has pending `message_agent` messages — press `i` to peek at them without consuming them. For Claude sessions, the Build column shows `on` (green) / `off` for the per-session auto tree-build toggle; Codex sessions show `-` because they do not build compaction trees — see [Frontier-coherent compaction](#frontier-coherent-compaction). The Tokens column shows the current LIVE jsonl's token count (`full_jsonl.live_stats`, mtime-cached) — what the session would actually cost to resume right now, reflecting any past compaction; it's not the FULL/uncompacted total shown in the Compact widget. The Preview column shows the first user message from each session's transcript.

The scroll wheel works natively inside attached sessions — the monitor sets per-session `tmux mouse on`, so you scroll the pane's history without entering tmux copy-mode (`Ctrl-b [`). To select text, hold Shift while dragging (terminal-native selection).

**Workspace scope**: each `multiverse` instance is bound to the cwd it was launched from. Sessions in other workspaces are managed by other monitor instances independently. The dashboard surfaces both **multiverse-wrapped** sessions (live or ended) and **dormant named claude sessions** in this cwd that aren't currently wrapped.

**Sessions survive the monitor**. State lives under `~/.multiverse/sessions/<mv-id>/`. If you `q`uit the monitor, sessions keep running in their tmux sessions; the next `multiverse` launch picks them back up.

### Inter-agent communication

Several MCP tools are available to any agent running under the wrapper. They default to your own workspace, but `list_agents`, `ask_agent`, and `message_agent` all accept a `working_directory` argument to reach agents in other workspaces.

**`list_agents(max_agents=15)`** — discover other agents in this workspace. Returns named agents (live, ended, dormant) sorted by status priority. Your own entry is marked with `← this is you`. Long lists are truncated; pass a larger `max_agents` if you need the full roster.

**`message_agent(target_name, message, working_directory="")`** — send a durable message. Use this for instructions, in-depth questions, or anything where the recipient should be able to use tools to respond.

- Claude recipients get hook-injected reminders; Codex recipients call `check_inbox`
- Recipient has full tool access (Read, Bash, Grep, etc.) and can take their time
- Persists in the recipient's conversation history
- Fire-and-forget — the tool returns as soon as the message is queued; you don't block on a reply
- To reply, the recipient simply calls `message_agent` back at the original sender (no separate respond tool)
- **Cross-workspace**: pass `working_directory=<abs path>` to message an agent in another workspace (like `ask_agent`). The delivered message carries the sender's workspace, and the recipient is told to reply with `working_directory=<sender's workspace>` — so replies route back correctly.

**Delivery is always active** — the message is queued in the recipient's inbox, then the recipient is woken so it gets picked up without waiting for the user. What the wake looks like depends on the recipient's state:

| Recipient state | What happens |
|:----------------|:-------------|
| dormant / ended | Recipient is **auto-resumed** (started for you) and processes the message on startup — no user needed. |
| idle / working — **Claude**, composer on screen | Recipient is **pinged via its terminal** (tmux `send-keys`) to check its inbox immediately — *whether or not a user is attached*. Any typed-but-unsent draft is saved, cleared, and restored around the send, so an attached user loses nothing. |
| idle / working — **Claude**, composer not on screen | Remains queued. No composer means a permission prompt or question modal is up, and keystrokes would answer the modal instead of typing a prompt. |
| idle / working — **Codex**, composer focused | Recipient is **pinged via its terminal** (bracketed paste + Enter) to check its inbox immediately, whether or not a user is attached. During a working turn this steers the agent at its next tool boundary. Any typed-but-unsent draft is saved, cleared, and restored around the send. |
| idle / working — **Codex**, composer not focused | Remains queued. No keystrokes are sent to a picker, approval dialog, or other active terminal surface. |
| listening (in `monitor_inbox`) | No wake needed — it's already watching its inbox and picks the message up within ~1s. |
| starting | Remains queued; no terminal input is sent while the runtime TUI is still starting. |
| waiting (blocked on a permission/input prompt) | Not woken — a terminal ping can't reach a blocked pane. The message remains queued until the user resolves the prompt. |
| compacting (locked) | Not woken — the pane is killed for the duration. The message remains queued until compaction finishes and the session resumes. |

The states that can't be woken still receive the message through their normal delivery path (Claude's inbox hooks; Codex's `check_inbox`). The tool result reports exactly which path was taken (woke a dormant agent, pinged a live one, left it queued and why, etc.).

**At most one outstanding prompt.** Reading an inbox drains *all* of it, so a second "check your inbox" prompt sent before the agent acted on the first is wasted — it burns a turn to arrive at an empty inbox. A marker under `~/.multiverse/wake_pending/<encoded-cwd>/<name>` records that a prompt was accepted by the target's composer and not yet acted on; while it's set, further messages are queued without typing anything (route `skipped_wake_outstanding`).

- **Set only on a confirmed submit**, never on "we ran some tmux commands". The marker is a promise to stop pinging, so a wake still sitting unsent in the composer would stop delivery to that agent permanently — and the self-heal below can't rescue that, since nothing is being delivered and so the inbox never empties. Codex confirms its own submit (it waits for the prompt to leave the composer). Claude does not: `send_prompt_via_tmux` returns once its tmux commands succeed, so `message_agent` adds the postcondition itself — re-capture the pane and require the composer to hold exactly the restored draft (empty if there was none), polled briefly for TUI render latency. Unconfirmed, or a failed ping, leaves the marker unset and the next message pings again.
- **Cleared on real consumption only.** `deliver-inbox.py` for Claude (its hook path), `check_inbox` for either runtime — Codex's only path. Deliberately *not* `monitor_inbox`, which detects without consuming: clearing there would let a redundant prompt through in the window between "agent told a message exists" and "agent reads it".
- **Self-heals.** A marker with an empty inbox behind it is stale and gets retired, so an agent killed after being pinged doesn't go permanently unpingable.
- **Serialized per recipient.** The check, the ping, and the mark run under an `flock` on the recipient (`wake_delivery_lock`), because every sender is a separate MCP process. Without it two concurrent senders both read "nothing outstanding" and both ping. The lock spans the terminal I/O rather than reserving the marker up front: if a winner's ping fails, the loser re-checks after that outcome is known and pings for real, instead of skipping behind a prompt that never landed.

Net effect: a burst of five messages to a busy agent costs it one interruption, not five, and no message is dropped.

> The old `push_notification` argument is retired — delivery is always active now. Passing it (either spelling) is silently ignored.

**`ask_agent(target_name, question, timeout_seconds=60)`** — send a quick ephemeral question. Use this for facts the target already knows.

- Target answers from their existing context only — **no tools available**
- Target's main session is NOT involved (no notification, no entry in their conversation history)
- Blocking — returns the answer inline, or `'<agent>' did not respond within Ns` on timeout
- Works regardless of target state (idle, working, dormant) — uses the target runtime's native fork mechanism without disturbing its main process

**`check_inbox()`** — read your pending inbox messages on demand, mid-turn, without waiting for a hook to fire. Returns the queued messages (plain-text tool result) or `"You have no pending inbox messages."`, and **clears the inbox** just like a hook delivery. Use it when you just sent a `message_agent` and suspect a reply landed, or want to drain anything that piled up during a long task.

**`monitor_inbox(timeout_seconds=300)`** — block until the next inbox message arrives, then signal you to read it. Use it after you've finished work but are expecting replies — park here instead of busy-looping on `check_inbox`. Returns `"You have a new inbox message…"` once one lands (does **not** consume it — call `check_inbox` next), or a timeout notice after the deadline (default 5 min). Returns immediately if messages are already pending. The wait is interruptible: the user can press Esc to prompt you, ending the call cleanly. While parked, the agent's dashboard status reads `listening` (cyan) — so the user and other agents (via `list_agents`) can tell it's waiting for messages, not busy.

**`spawn_agent(instruction, name="")`** — start a brand-new subagent with a fresh context and hand it a starting task. Use this when you want to delegate to a clean slate, not bother an existing agent.

- `instruction` becomes the subagent's first user message, auto-submitted on startup
- The subagent has **no memory of your conversation** — include any relevant context directly in `instruction` if it matters
- User brokers the creation: the monitor pops a widget showing the caller runtime, your instruction, a suggested name (your `name` arg, or random if unset/taken), and a Claude/Codex selector defaulted to the caller's runtime. User accepts, edits, chooses the child runtime, or rejects
- Blocking, **no timeout** — the request waits until the user resolves it. Returns `"Spawned agent with name '<chosen>'"` on acceptance; on reject returns an error and no subagent is created. The caller can interrupt the wait (Esc) to cancel the request. Orphaned requests (caller hard-killed without cleaning up) are reaped by a caller-liveness check, not a timeout.
- **Rejections can carry a message.** The approval widget has an optional reason box; Esc (or Enter from that box) rejects and quotes what's typed there back to the requesting agent — `Spawn request rejected by the user with the message "…"`. Without it the agent only learns "rejected" and its usual next move is to ask again. A bare Esc omits the clause entirely (`Spawn request rejected by the user`), so an unexplained rejection reads no differently than it always did.
- Concurrent spawn requests from multiple agents queue in the monitor — one modal at a time, FIFO
- If you're attached to any multiverse-managed tmux session when a spawn request arrives, the monitor force-detaches you back to itself so you see the modal; it reattaches you to the same session automatically once the queue clears

**TUI: `a` — ask all agents.** Press `a` in the monitor to fan out a single question to every named agent in the workspace. Same mechanism as `ask_agent` but multi-target. Useful for broadcast discovery: "did anyone work on the auth refactor?".

When to pick which:

| Use case | Tool |
|:---------|:-----|
| "What are you working on?" / "Did you implement X?" | `ask_agent` (or `a` for all) |
| "Please review the PR at /path/to/branch" | `message_agent` |
| "Find me which agent owns the auth code" | `list_agents` then `ask_agent` |
| "Run tests and tell me what failed" (delegate to an existing agent) | `message_agent` |
| "Investigate the migration failure from scratch" (fresh context, no distractions) | `spawn_agent` |
| "I sent a message and want to wait for the reply" | `monitor_inbox` then `check_inbox` |
| "Did a reply land while I was working?" | `check_inbox` |

### Frontier-coherent compaction

When a conversation grows large, frontier-coherent compaction shrinks it **whole-session and user-driven**. You pick a total token budget; the plugin distributes it proportionally across every turn, expands each turn's pre-built summary tree to its allocated share, and atomically replaces the live JSONL with the compacted version. The original transcript is preserved in a parallel "full" JSONL that the live one is mirrored from on every hook fire.

**Two-JSONL architecture.** Every session has two on-disk JSONLs:

| Path | Role |
|:-----|:-----|
| `~/.claude/projects/<slug>/<uuid>.jsonl` | **LIVE** — what claude reads/writes. Compaction rewrites this. |
| `~/.multiverse/full_jsonls/<uuid>.jsonl` | **FULL** — append-only mirror of the uncompacted history. The source of truth for token counts, turn counts, and tree building. |
| `~/.multiverse/full_jsonls/<uuid>.marker` | uuid of the last live row already mirrored to full. Tracking by uuid (not byte offset) so the mirror keeps working after compaction has rewritten live with a different prefix. |

A `mirror-jsonl.py` hook wired into `UserPromptSubmit` / `Stop` / `SessionEnd` keeps full caught up with live. First-time fire (legacy session) bootstraps full from a verbatim copy of live.

**Auto-build worker — opt-in per session.** Each tree-build is a privileged `claude --resume` call against a synthetic JSONL — non-trivial latency and rate-limit cost, so it's **off by default**. Flip it on per session with `b` in the dashboard. When enabled (marker file at `~/.multiverse/auto_build/<uuid>`), every `UserPromptSubmit` on that session fires `spawn-tree-builder.py`, which detaches a worker that walks the full JSONL and builds a summary tree for every completed turn (skipping the in-progress one). Trees cache to `~/.multiverse/trees/<uuid>/<turn_id>.json`; subsequent fires only build newly-completed turns. With auto-build on, by the time you trigger a manual compaction the trees are usually already done; without it, the Compact widget spawns an `--all-turns` worker on demand and you wait through the full build.

**Compact widget (`c`).** Pick the highlighted row, press `c`. The widget:

1. Loads stats from the full JSONL — total uncompacted tokens, turn count.
2. Asks for a token budget. The placeholder suggests ~50% of the total.
3. On submit:
   - Acquires a cross-monitor lock at `~/.multiverse/compacting/<uuid>.json` (other monitors render the row as `compacting` and refuse `Enter` / `k` actions on it).
   - Kills the session's tmux pane (state dir kept; the multiverse name and identity survive).
   - Syncs full from live one more time.
   - Waits for any in-flight auto-worker to finish (its log tail streams into the widget).
   - Spawns an `--all-turns` worker if any turn is still missing a tree (cache-aware: only builds the gaps).
   - Loads each turn's tree, allocates the total budget across turns proportionally to each turn's full token count, expands each tree to its slice, and renders the new compacted JSONL via `compact_forest_to_session_jsonl`.
   - Atomically replaces live (`tempfile` + `os.replace`); resets the mirror marker so subsequent appends are mirrored from the new boundary.
   - Releases the lock.
4. The dashboard refreshes; the row reverts to `ended` / `dormant`. `Enter` resumes from the compacted state seamlessly.

Surviving thinking blocks are emitted verbatim with their original signatures (which validate server-side via subscription auth, so no `ANTHROPIC_API_KEY` needed). Summary nodes emit as `<compacted-context entries="N">…</compacted-context>` text blocks so the live agent can distinguish summarized regions from native output.

**Compaction is non-destructive on the original.** The full JSONL is never compacted — only appended to. Re-compacting a session always reaches back to the full uncompacted history, regardless of how many prior compactions happened. Aborting a compaction (Esc during the running phase) stops at the next safe checkpoint without leaving live in a half-written state (the rewrite is atomic).

**Detaching from a running compaction (`d`).** Esc aborts; `d` just puts the screen away. The build keeps running, the lock stays held (so the row still reads `compacting` and still refuses attach / resume / kill everywhere), and `Enter` or `c` on that row re-opens the same screen with its log and progress intact. If summarization finishes while you're detached, the scheduler picker is *held* rather than thrown over the dashboard — you get a notification, and the picker appears when you come back. If the whole run finishes while you're away, the verdict arrives as a notification and the screen is retired, so a later `c` starts fresh.

This is one screen moving, not a shared one: a compaction detached in one monitor can only be re-opened from that same monitor, and a lock held by a *live* monitor still blocks every other one. Only a **dead** holder's lock is reclaimable (see `list_locked`), which is what makes a compaction survive quitting the monitor mid-build.

### With vs without the wrapper

| Setup | Inter-agent comms | Multi-session monitor | Compaction (manual `c`) | Auto tree-build |
|:------|:---|:---|:---|:---|
| `multiverse` wrapper | ✅ all tools work | ✅ Textual TUI + tmux | ✅ widget drives the full pipeline | ✅ opt-in per session via `b` |
| Plain `claude --plugin-dir …` | ❌ MCP tools error (env vars unset) | ❌ | ❌ no UI to trigger from | ✅ if you create the marker file at `$MULTIVERSE_ROOT/auto_build/<uuid>` by hand |

The inter-agent tools require `MULTIVERSE_SESSION_NAME` and `MULTIVERSE_SESSION_CWD` env vars, which are only set when the monitor spawns the session via `tmux new-session -e`. Auto tree-building only needs the hooks, so it works in plain `--plugin-dir` mode too — you just have to toggle it on yourself.

## How it works

### State layout

```
~/.multiverse/
├── sessions/<mv-id>/
│   ├── meta.json     — cwd, created_at, claude_args, name, optional resumed_from
│   ├── status.json   — {"status": "idle"|"working"|"waiting"|"ended"|…, "ts": …}
│   └── claude_uuid   — current claude session UUID (captured by SessionStart hook)
├── inbox/<encoded-cwd>/<recipient-name>/<message-id>.json
│                     — pending inter-agent message; consumed (deleted) by the
│                       deliver-inbox hook on first delivery as a system-reminder
├── spawn_requests/
│   ├── <req-id>.json           — agent-initiated subagent spawn request
│   └── <req-id>.resolved.json  — user's decision (O_EXCL-atomic so racing
│                                 monitors can't double-resolve)
├── full_jsonls/
│   ├── <claude-uuid>.jsonl     — append-only mirror of the uncompacted
│   │                             session history. Source of truth for tree
│   │                             building and total-token counts.
│   └── <claude-uuid>.marker    — uuid of the last live row already mirrored
├── trees/<claude-uuid>/
│   ├── <turn-id>.json          — per-turn summary tree (privileged-mode build)
│   ├── .worker.lock            — PID of the running tree-build worker, if any
│   └── worker.log              — append-only worker log (tailed live by the
│                                 Compact widget)
├── compacting/<claude-uuid>.json
│                               — cross-monitor compaction lock (O_EXCL with
│                                 stale-PID recovery). Holding monitor's PID
│                                 in body. Other monitors render the row as
│                                 ``compacting`` and refuse Enter / Kill.
├── auto_build/<claude-uuid>    — empty marker file; presence means
│                                 background auto tree-build is enabled for
│                                 this session. Toggled with ``b`` in the
│                                 dashboard. Default state is OFF (no file).
└── logs/inter-agent/<YYYY-MM-DD>.jsonl
                                — one line per message_agent call, push-
                                  notification routing outcome, and inbox
                                  delivery (hook- or check_inbox-triggered).
                                  Files older than 7 days are deleted the
                                  next time anything is logged.
```

Sessions survive monitor restarts — the TUI rebuilds its view from disk every refresh. Inbox files survive everything; they wait until a Claude hook or an explicit `check_inbox` claim consumes them.

### Architecture

```
┌────────────────────────────────────────────────────────┐
│  multiverse (bin/multiverse)                           │
│  Thin bash launcher — execs `uv run python -m          │
│  src.monitor`, exports MULTIVERSE_ORIG_CWD=$PWD        │
│  and MULTIVERSE_PLUGIN_ROOT=<plugin_root>              │
└──────────────────┬─────────────────────────────────────┘
                   │ launches
┌──────────────────▼─────────────────────────────────────┐
│  Monitor TUI (src/monitor.py, Textual)                 │
│  - Workspace-scoped: filters sessions to cwd           │
│  - Lists multiverse sessions + dormant claude sessions │
│  - Spawns via tmux new-session -d -s multiverse-<id>   │
│    -e MULTIVERSE_SESSION_NAME=<name> -e ...            │
│  - Attaches via App.suspend() + a probe-safe PTY relay │
│  - `a` key → ask_agents fan-out to all named sessions  │
│  - `c` key → Compact widget (CompactScreen) drives     │
│    CompactRunner: lock, kill, sync, build trees,       │
│    expand to budget, atomically replace live JSONL     │
└──────────────────┬─────────────────────────────────────┘
                   │ tmux new-session -d
┌──────────────────▼─────────────────────────────────────┐
│  tmux session: multiverse-<id>                         │
│  └─ multiverse-session (bin/multiverse-session)        │
│     └─ claude --plugin-dir $MULTIVERSE_PLUGIN_ROOT …   │
│        ├─ MCP server (multiverse):                     │
│        │    list_agents, message_agent, ask_agent,     │
│        │    check_inbox, monitor_inbox, spawn_agent     │
│        ├─ Hooks (SessionStart/UserPromptSubmit/Stop/…):│
│        │    set-status, record-claude-uuid,            │
│        │    deliver-inbox, on-notification,            │
│        │    mirror-jsonl, spawn-tree-builder           │
│        ├─ Inter-agent inbox: hook checks               │
│        │  ~/.multiverse/inbox/<cwd>/<name>/, injects   │
│        │  any pending messages as system-reminders     │
│        └─ Mirror: hook keeps                           │
│           ~/.multiverse/full_jsonls/<uuid>.jsonl in    │
│           sync with the live JSONL (uuid-marker delta) │
└────────────────────────────────────────────────────────┘

           Detached, runs alongside the tmux session:
┌────────────────────────────────────────────────────────┐
│  Auto tree-build worker (scripts/tree-builder-worker)  │
│  - Spawned by spawn-tree-builder.py on UserPromptSubmit│
│    IF $MULTIVERSE_ROOT/auto_build/<uuid> exists        │
│    (per-session opt-in; toggle with `b`).              │
│    Lockfile keeps it singleton.                        │
│  - Runs build_tree_priv_cli.py per turn — invokes      │
│    `claude --resume <synthetic>` against a JSONL we    │
│    write into ~/.claude/projects/<slug>/<id>.jsonl.    │
│    Subscription auth, no API key needed                │
│  - Caches each turn's tree at                          │
│    ~/.multiverse/trees/<uuid>/<turn-id>.json           │
│  - MULTIVERSE_TREE_BUILDER_ACTIVE env var prevents     │
│    recursion when synthetic sub-claudes inherit hooks  │
└────────────────────────────────────────────────────────┘
```

### Per-session env vars (set by the monitor on `tmux new-session -e`)

| Env var | Used by |
|:--------|:--------|
| `MULTIVERSE_ROOT=<state_dir>` | canonical `~/.multiverse` — pinned to an absolute path so the monitor and every spawned process agree even when `HOME` differs between them |
| `MULTIVERSE_SESSION_ID=<mv-id>` | identifies the multiverse session |
| `MULTIVERSE_SESSION_NAME=<name>` | inter-agent tools (caller identity) |
| `MULTIVERSE_SESSION_CWD=<cwd>` | inter-agent tools (workspace scope) |
| `MULTIVERSE_STATUS=…/status.json` | hook scripts |
| `MULTIVERSE_RUNTIME=claude|codex` | selects runtime-specific lifecycle, discovery, and launch behavior |
| `MULTIVERSE_PLUGIN_ROOT=<plugin_dir>` | inner script (passes `--plugin-dir`) |
| `MULTIVERSE_CLAUDE_BIN=<binary>` | inner script + fork/compaction paths — which Claude binary to run (default `claude`; see [Machine-local config](#machine-local-config-binmultiverselocal)) |
| `MULTIVERSE_CODEX_BIN=<binary>` | Codex runner, native naming, and ephemeral app-server forks |
| `MULTIVERSE_CODEX_PLUGIN_ROOT=<plugin_dir>` | Codex hook and MCP bundle injected into wrapped Codex sessions |
| `MULTIVERSE_EXTRA_PLUGIN_DIRS=<dir:dir>` | inner script — extra `--plugin-dir` entries for interactive sessions |
| `CLAUDE_CONFIG_DIR` / `CLAUDE_LOCAL_DATA_DIR` / `PATH` | propagated when set so spawned sessions resolve transcripts under the same Claude config dir (and find the same binary) the monitor used |
| `CODEX_HOME` | propagated when set so Codex discovery, resume, and spawned sessions use the same native session root |

### MCP server: tools

The single MCP server (named `multiverse` in `.mcp.json`) exposes six tools:

| Tool | Purpose |
|:-----|:--------|
| `list_agents(max_agents)` | Discover other agents in the workspace |
| `message_agent(target_name, message, working_directory)` | Send a durable message (inbox-delivered); always actively wakes the recipient; `working_directory` targets another workspace |
| `check_inbox()` | Read + clear pending inbox messages on demand |
| `monitor_inbox(timeout_seconds)` | Block until the next inbox message arrives (async, interruptible; shows as `listening`) |
| `ask_agent(target_name, question, timeout_seconds)` | Ephemeral side-question (forked subprocess) |
| `spawn_agent(instruction, name)` | Spawn a fresh subagent with the instruction as its first user message; user-brokered naming and runtime selection in the monitor |

### Hooks

Wired to claude lifecycle events; all of them are no-ops outside the multiverse wrapper (they detect the absence of `MULTIVERSE_*` env vars and exit).

| Hook | Script(s) | Purpose |
|:-----|:----------|:--------|
| `SessionStart` | `set-status.sh idle`, `record-claude-uuid.sh`, `deliver-inbox.py` | Initial status, capture claude UUID, deliver any pending inbox messages |
| `UserPromptSubmit` | `set-status.sh working`, `deliver-inbox.py`, `mirror-jsonl.py`, `spawn-tree-builder.py` | Update status, deliver new inbox messages, mirror live → full JSONL, spawn the auto tree-build worker (skips the in-progress last turn) |
| `Notification` | `on-notification.sh` | Set `waiting` for blocking notifications (permission prompts) — filters out informational ones like `idle_prompt` |
| `PostToolUse` (any) | `set-status.sh working` | Status update during tool use |
| `Stop` | `set-status.sh idle`, `deliver-inbox.py`, `mirror-jsonl.py` | Mark idle; deliver any pending inbox via `decision: "block"` (which triggers another turn — the post-task wakeup mechanism); mirror the just-completed turn's rows into full JSONL |
| `SessionEnd` | `set-status.sh ended`, `mirror-jsonl.py` | Mark ended; final mirror sync so full captures any rows written between Stop and shutdown |

### Communication protocol

The monitor and the sessions it spawns coordinate entirely through **files** under `~/.multiverse/`, never sockets or stdin/stdout:

- **Status** — hook scripts write `~/.multiverse/sessions/<mv-id>/status.json`; the monitor polls every tick.
- **Inter-agent messages** — `message_agent` writes `~/.multiverse/inbox/<encoded-cwd>/<recipient-name>/<message-id>.json` atomically; Claude's `deliver-inbox.py` hook or either runtime's `check_inbox` tool atomically consumes it.
- **Spawn requests** — `spawn_agent` writes `~/.multiverse/spawn_requests/<req-id>.json`; the monitor pops a naming modal and writes `<req-id>.resolved.json` (`O_EXCL`).
- **Compaction lock** — `~/.multiverse/compacting/<uuid>.json` (`O_EXCL`) both prevents concurrent compaction and drives the `compacting` status badge.

Every writer uses tmp+rename (or `O_EXCL`) so readers never see a partial file.

### What happens during a compaction

1. User selects a session row (must be `idle` / `ended` / `dormant`) and presses `c`
2. `CompactScreen` opens and runs `total_token_count` + `total_turn_count` against the full JSONL in a worker thread; stats render once loaded. `compaction.parser.parse_session` is the underlying counter.
3. User types a token budget and hits Enter
4. `CompactRunner` (in a worker thread) executes the state machine:
   1. `acquire_lock` writes `~/.multiverse/compacting/<uuid>.json` with `O_EXCL`. If another monitor already holds it, fail closed. Stale-PID locks (holder dead) are reaped on contention.
   2. If the session is wrapped by a multiverse session, kill its tmux pane only — the state dir stays so the multiverse name and identity survive across the compaction.
   3. Run `sync_full` to bring the full JSONL up to live (no-op if the mirror hooks have been firing; bootstraps a verbatim copy for legacy sessions that never had a full JSONL).
   4. If a tree-build worker is already running for this session (its lockfile PID is alive), wait for it — its `worker.log` is tailed live into the widget's RichLog. Auto-workers always skip the in-progress last turn, so we'll need a follow-up.
   5. Count cached trees vs forest size. If any are missing, spawn `tree-builder-worker.py --all-turns`. The worker is cache-aware: it skips turns whose `<turn-id>.json` already exists, so this is typically just the last turn.
   6. Once every turn has a tree, transition to `expanding`: load each tree from cache, run `proportional(forest, total_budget)` to allocate per-turn budgets ∝ each turn's full token count, expand each tree to its slice via `expand_to_budget`, and call `compact_forest_to_session_jsonl` to render the new compacted session JSONL.
   7. Atomically replace live (`tempfile.mkstemp` in the same directory + `os.replace`). Original `custom-title` is preserved so the dashboard row keeps its name.
   8. Reset the mirror marker to the new live's last uuid so subsequent `mirror-jsonl.py` calls pick up only post-compaction additions correctly.
   9. `release_lock` in a `finally`.
5. Dashboard refresh removes the `compacting` override; the row reverts to `ended` (or `dormant` if no mv wrapper) and is resumable normally. Resuming reads the new compacted JSONL — the live agent sees a mix of native thinking blocks (verbatim with original signatures) and `<compacted-context entries="N">…</compacted-context>` summary text blocks.

If the user presses Esc during the running phase, the runner sets a cancellation flag and bails at the next checkpoint — but it does **not** kill any in-flight `claude --resume <synthetic>` subprocess that's mid-merge (that would leave a half-built tree). Worst case: one in-flight build finishes, then the runner exits.

### What happens during a `message_agent` call

1. `message_agent(target, message, working_directory=…)` writes `~/.multiverse/inbox/<target-cwd>/<target>/<id>.json` (atomic tmp+rename), where `<target-cwd>` is `working_directory` if given (cross-workspace) else the sender's own cwd. For a cross-workspace send it also stamps `from_cwd` (the sender's workspace) so the recipient can reply. The inbox write always happens before any auto-resume, so Claude's SessionStart hook and Codex's wrapper-side resume prompt both observe it.
2. **Wake routing** (always attempted — there is no queue-only mode):
   - dormant/ended → `_auto_resume` spawns detached tmux with either `claude --resume <uuid>` or `codex resume <id>`; the runtime wrapper supplies the startup inbox prompt.
   - idle/working + **Claude** → gated on the composer, not on attachment. `send_prompt_via_tmux(..., require_composer=True)` captures the pane (`-J -e`), and `claude_input_box()` reports `(visible, draft)`. If the composer is on screen we deliver even to an attached user, preserving any draft: backspace-clear it → literal `send-keys` the wake prompt + Enter (its prompt hook injects the message) → type the draft back, unsent. If the composer is *not* on screen (permission prompt / question modal), nothing is typed and the message remains queued (`passive_fallback_no_composer`).
   - idle/working + **Codex** → attachment does not affect routing. `_wake_via_tmux` requires tmux's cursor to be inside the visible composer, snapshots the exact draft, clears and verifies it, bracket-pastes and verifies the wake prompt, uses Enter (submit while idle, steer at the next tool boundary while working), confirms the prompt cleared, then restores and verifies the original draft. Any failed check leaves the message queued; an unavailable composer receives no keystrokes.
   - listening → no action; the recipient's `monitor_inbox` poll picks it up within ~1s.
   - starting → no ping while the runtime TUI is starting; the message remains queued.
   - waiting (blocked on a permission/input prompt) → no ping; it would land on the prompt as random keys, so we don't try. Remains queued.
   - compacting (`is_locked(uuid)` — checked before any state-based branching) → no wake; the pane is killed for the duration. Remains queued.
3. Tool returns a result describing which path was taken (woke dormant / pinged live / left queued because attached-user / waiting / compacting / etc.).
4. For a Claude recipient, the next firing hook (`SessionStart` if just resumed, `Stop` after the current turn, or `UserPromptSubmit` on the next input or a push ping) runs `deliver-inbox.py`, which:
   - Reads all pending messages in the recipient's inbox
   - Formats them as one `<system-reminder>` block
   - Returns the right injection JSON for the firing event (`additionalContext` for SessionStart/UserPromptSubmit, `decision: "block"` with the body as `reason` for Stop)
   - **Deletes** the consumed files (so subsequent hook fires don't re-inject)
5. A Codex recipient calls `check_inbox`, which atomically claims the same files and returns their contents as the tool result. Push and resume prompts cause that call; without a push, the agent or user can request it explicitly.
6. The recipient may use tools to act on the message and reply via `message_agent` back at the sender.

**Wakeup on resume.** SessionStart's `additionalContext` is buffered in claude's REPL but doesn't autonomously trigger an API call — without intervention the model wouldn't engage with the message until a human attached and typed something. To bridge that gap, `bin/multiverse-session` checks the inbox at the top of every loop iteration and, if non-empty, appends a positional-arg prompt (`"You have N pending inbox messages…"`) to the interactive `claude` launch. claude auto-submits a positional arg as its first user message, so the model engages with the just-delivered inbox `additionalContext` immediately, inside the interactive session — permission prompts surface normally and an attaching user can grant them. (This replaced an earlier print-mode prime call, `claude -p … --resume`: print mode is headless, so a model that tried to call a tool while processing the inbox would block on a permission prompt with no way to answer it, hanging the whole wrapper.) Applies uniformly across every resume path: user-initiated Enter on a dormant row and `message_agent` auto-resume.

Codex uses the parallel `multiverse-codex-session` behavior: if inbox files are pending at launch, it appends a positional resume prompt telling Codex to call `check_inbox`. The hook does not consume the files; the tool call remains the exactly-once delivery point.

### What happens during an `ask_agent` call

1. `ask_agent(target, question)` looks up the target's runtime and native session ID.
2. Claude spawns `claude -p --resume <uuid> --fork-session --session-id <fresh> --tools "" --max-turns 1 -p "<wrapped question>"`.
3. Codex starts an app-server, calls `thread/fork` with `ephemeral: true`, then submits one `turn/start` to the returned thread. The fork is never materialized as a rollout file.
4. The Codex fork disables tool-bearing features, MCP servers, apps, hooks, web search, filesystem access, and network access. If the event stream still reports an action item, Multiverse fails the ask and terminates the process instead of accepting the result.
5. Both subprocesses receive an environment with all `MULTIVERSE_*` variables stripped, so side-question lifecycle activity cannot overwrite the caller's wrapper state.
6. The target's main session is unaffected regardless of whether it is idle, working, or dormant. Claude's temporary JSONL is deleted; Codex's in-memory ephemeral thread disappears with its dedicated app-server process.
7. The tool returns the single final answer inline, or `'<agent>' did not respond within Ns` on timeout.

### What happens during a `spawn_agent` call

1. `spawn_agent(instruction, name)` writes `~/.multiverse/spawn_requests/<req-id>.json` (atomic tmp+rename) with the instruction, preferred default name (caller's `name` arg if free, else random), caller runtime, and caller tmux session id
2. Tool fires `tmux display-message` on the caller's tmux pane so a user attached there sees "'<caller>' wants to spawn a subagent — detach (Ctrl-b d) to approve"
3. Tool (async) polls `<req-id>.resolved.json` every 250ms, **indefinitely** — no timeout. A user Esc on the tool call cancels the wait: `CancelledError` propagates through the `finally` that cleans up the request file, and the monitor dismisses any open modal once that file is gone.
4. All cwd-matching monitors scan `spawn_requests/` on each 1s tick and enqueue new, unresolved requests (skipping any whose caller tmux session no longer exists — orphan reaping without a timeout). When a monitor has no modal open and its queue is non-empty, it pops the next request's approval widget — seeded with the default name and caller runtime, and showing the caller runtime plus instruction preview
5. User chooses Claude or Codex and hits Enter to accept (writes `{status: "accepted", name: "<chosen>", runtime: "<chosen-runtime>"}` to `<req-id>.resolved.json`, `O_EXCL`-atomic) or Esc to reject
6. First monitor to write the resolution wins via `O_EXCL`; other monitors detect the external resolution on their next tick and dismiss their modal. If their user had already been typing, they get a notification that another monitor accepted first
7. Tool reads the resolution. On accept → `create_session(cwd, name=chosen, runtime=chosen_runtime, initial_prompt=instruction)` writes the instruction to a state-dir file (`initial_prompt.txt`) and passes its path via `MULTIVERSE_INITIAL_PROMPT_FILE`. Claude receives it as an auto-submitted positional prompt. A fresh Codex TUI receives `/rename <name>` first and then the instruction through bracketed paste, so its live thread title and persistent index agree before work begins. The instruction never travels on a tmux/shell command line — no shlex escaping, no `/bin/sh` re-parse, no ARG_MAX limit — so long, apostrophe-heavy, multi-line instructions arrive intact. On reject → returns an error, no subagent created
8. Tool cleans up both files on exit

**Auto-detach/reattach:** if the user is attached to any multiverse-managed tmux session when step 1 writes a request file, the monitor's sidecar guard (`_SpawnAttachGuard`) detects it within 1s. The attach relay reports its tmux client PID to the guard; the guard resolves that PID to the exact tmux client and retries `tmux detach-client -t <client>` until it succeeds, forcing this monitor back to the dashboard without detaching other terminals. Textual's event loop is paused during attach, so this external detach is the only way to wake the modal. Once the spawn queue fully drains (all modals accepted/rejected), the monitor auto-reattaches the user to the tmux session they were booted out of. Skipped with a heads-up if that session was killed while they were away.

The subagent starts from a **fresh prompt cache** with no memory of the caller's conversation. If context matters, the caller must put it in `instruction`.

### Key design decisions

- **Workspace scope.** Each multiverse instance is bound to one cwd. This keeps inter-agent tools well-defined (you can't accidentally message an agent in a different project) and matches the natural unit of work.
- **Names are first-class.** Sessions without names don't show on the dashboard and aren't reachable by inter-agent tools. Naming is the primary way agents identify each other.
- **Two messaging modes for two use cases.** Durable (`message_agent`, full tools, persistent) vs ephemeral (`ask_agent`, no tools, blocking, fork-based). Use the cheap one for fact-gathering, the heavy one for delegation.
- **No "respond" tool.** Replies are just messages back at the sender. This avoids async-mailbox complexity (no ask records, no response routing, no waiter state). The cost: the sender doesn't know automatically when a response arrives — but their next inbox check picks it up like any other message.
- **Fork doesn't interrupt the target.** `ask_agent` and the `a` ask-all fork the target's native session. Claude uses `--fork-session`; Codex uses an ephemeral app-server `thread/fork`. Neither path writes to the target's main conversation.
- **Ask-forks stay out of discovery.** A Claude `--fork-session` JSONL inherits the origin's `custom-title`, so its UUID is registered under `~/.multiverse/forks/` before spawn and removed after cleanup (`src/forks.py`); dashboard discovery skips registered UUIDs and GC removes orphans. Codex requests `ephemeral: true`, which never materializes a rollout file and therefore needs no discovery registry entry.
- **Graceful degradation.** Without the wrapper the plugin still loads, but the inter-agent tools error (they need the `MULTIVERSE_*` env vars the monitor injects) and there's no monitor UI. Auto tree-building works via hooks alone if you create the marker file by hand.
- **Concurrent-safe.** Each session has its own state directory and its own env vars (injected via `tmux new-session -e`); nothing leaks between sessions.
- **Cross-process path coherence.** All state paths — including `MULTIVERSE_ROOT` (the `~/.multiverse` directory itself) — flow as absolute paths via `tmux -e` rather than being derived from `HOME` in each process. This matters in deployments where the monitor and the spawned claude processes have different `HOME` values (e.g. containers where a wrapper sets `HOME=/weka/...` for claude so `~/.claude` lands on persistent storage); without an explicit `MULTIVERSE_ROOT` the inbox and spawn-request directories silently diverge between the two sides.
- **Inbox delivery claims, it doesn't just read-then-delete.** `claim_pending_messages` renames each message file to a claimant-unique path before reading/deleting it — `unlink()` alone was measured to occasionally let two racing callers both report success under heavy thread contention, which is exactly the failure mode that matters here (two live sessions sharing an inbox both delivering the same message). `rename()` of a shared source to two different destinations never double-succeeded across thousands of trials, so that's the actual claim step; delete is just cleanup afterward, no longer contested.
- **A session's `host` field guards against reaping a session that's alive on a different host.** `tmux_session_exists()` is inherently local — it can't see a different host's tmux server. In a shared-`MULTIVERSE_ROOT` deployment (multiple containers/hosts on the same network storage), a session created on host A looks "ended" from host B's monitor purely because host B's tmux doesn't have it, not because it's actually dead. Reaping-then-recreating under that false premise (the Enter-to-resume flow, and `message_agent`'s push-notification auto-resume) creates an invisible zombie: the old process keeps running with no meta dir anywhere, silently sharing its inbox with the new session it got replaced by — the mechanism behind duplicate message/notification delivery. `create_session` now stamps the creating host into `meta.json`; `foreign_host_still_reported_live()` refuses to treat "not found in local tmux" as proof of death when the recorded host differs from the current one, trusting only the durable, hook-persisted `status.json` value (`"ended"` is written exclusively by the real process's own `SessionEnd` hook, wherever it runs) instead.
- **Two JSONLs, one source of truth.** Compaction rewrites *live* but the *full* JSONL is append-only and never compacted. Token counts, turn counts, and tree building always read from full — so re-compacting after a prior compaction reaches back to the original uncompacted history rather than compounding summaries.
- **Marker by uuid, not byte offset.** The mirror tracks "last live row already mirrored" by uuid. Byte offsets break the moment compaction shrinks live; uuid lookup degrades gracefully (an unreachable marker just means "append nothing this round" rather than corrupting full).
- **Cross-monitor compaction lock is the row's status.** No separate display state — the same `~/.multiverse/compacting/<uuid>.json` file that prevents Enter / Kill is also what triggers the `compacting` status badge in every monitor's dashboard refresh. One mechanism, no drift.
- **Kill-tmux-only during compaction.** The Compact widget kills the session's tmux pane but leaves the multiverse state dir intact, so the session keeps its name and identity through the compaction. (`sessions.kill_session` does the destructive full reap; the Compact path uses a softer kill function inlined into the widget.)
- **Auto-build is opt-in per session, off by default.** Each tree-build is a privileged `claude --resume` call — non-trivial cost. Most sessions never reach compaction, so the default is OFF and the user flips it on (`b`) for the ones they expect to compact. Whether or not auto-build was running, the manual Compact path doesn't *require* pre-built trees: if the auto-worker hasn't finished, the widget waits; if it never ran, the widget spawns a fresh `--all-turns` worker on demand.
- **Vendored compaction package.** `vendor/compaction/` is a copy of the standalone `compaction/` repo that lives at the monorepo root. Compaction logic moves between the two during development; the vendored copy is the canonical version used by the plugin. `compaction/` itself is gitignored — kept on disk as a scratch workspace.

## Plugin structure

```
multiverse-plugin/
├── .claude-plugin/
│   └── plugin.json           # Plugin manifest
├── .mcp.json                 # MCP server config (registers `multiverse` server)
├── pyproject.toml            # Python package: MCP server, monitor, inter-agent tools
├── bin/
│   ├── multiverse            # Launcher — execs the Textual monitor
│   └── multiverse-session    # Per-session inner loop (runs inside each tmux session)
├── hooks/
│   └── hooks.json            # Hook config
├── scripts/
│   ├── set-status.sh         # Status writers (idle/working/waiting/ended)
│   ├── on-notification.sh    # Notification hook — filters by notification_type
│   ├── record-claude-uuid.sh # SessionStart hook — captures claude session UUID
│   ├── deliver-inbox.py      # Inter-agent message delivery (3 hooks: SessionStart/UserPromptSubmit/Stop)
│   ├── mirror-jsonl.py       # Live → full JSONL mirror hook (UserPromptSubmit/Stop/SessionEnd)
│   ├── spawn-tree-builder.py # UserPromptSubmit hook: detach a tree-build worker
│   ├── tree-builder-worker.py# Long-running worker: build a summary tree per completed turn
│   └── build_tree_priv_cli.py# Per-turn build entry point (privileged-mode constructor via claude CLI)
├── src/
│   ├── __init__.py
│   ├── server.py             # MCP server: all six tools
│   ├── sessions.py           # Multiverse session data layer (Session, DashboardRow, list/build/find,
│   │                         #  applies compacting status override from list_locked();
│   │                         #  still surfaces legacy folded JSONLs as ghost rows)
│   ├── inbox.py              # Inter-agent message inbox (write/list/consume)
│   ├── ask_agents.py         # Forked-subprocess ask machinery (used by ask_all + ask_agent)
│   ├── forks.py              # On-disk registry of in-flight ask-fork UUIDs (hides them from dashboard + list_agents; GCs orphans)
│   ├── spawn_requests.py     # Filesystem protocol for agent-initiated spawn_agent requests
│   ├── paths.py              # Canonical ~/.multiverse resolution (MULTIVERSE_ROOT > MULTIVERSE_STATUS > HOME)
│   ├── full_jsonl.py         # Two-JSONL helpers: sync_full / bootstrap / token+turn counts
│   ├── compact_state.py      # Cross-monitor compacting lock (O_EXCL with stale-PID recovery)
│   ├── compact_runner.py     # Compaction state machine (lock → kill → sync → build → expand → release)
│   ├── auto_build.py         # Per-session auto tree-build toggle (marker file under auto_build/)
│   ├── monitor.py            # Textual TUI: dashboard, modals, tmux control, CompactScreen
│   └── names.py              # Random name pool for new sessions
├── vendor/
│   └── compaction/           # Vendored copy of the standalone compaction package
│       ├── parser.py         #   JSONL → Forest of Turns
│       ├── preprocess.py     #   per-turn cleanup; off by default
│       ├── constructor.py    #   bottom-up tree builder (privileged + CLI variants)
│       ├── compactor.py      #   expand_to_budget + schedulers (proportional / recency_decay / …)
│       ├── priv_session.py   #   synthetic JSONL builders + multi-turn renderer
│       ├── render.py         #   frontier → message-block / constructor-input rendering
│       ├── tokenize.py       #   token counter
│       └── types.py          #   Leaf, Node, Turn, Forest, Frontier
└── README.md
```

## Extending the plugin

The plugin is designed so new agent capabilities can be added as MCP tools or hook scripts. To add a new tool:

1. **Add the tool** in `src/server.py` with `@mcp.tool()`. The function's docstring becomes the description visible to the model — be specific about when and how to use it.
2. **If the tool needs cross-session state**, add a small data-layer module (like `src/inbox.py`) and write atomically (tmp + rename) so concurrent readers never see partial files.
3. **If the tool needs the target session to react**, wire a hook script that reads the relevant state and returns `additionalContext` (for SessionStart/UserPromptSubmit) or `decision: "block"` with `reason` (for Stop). All hooks run as separate processes; keep them stdlib-only Python or bash for fast startup.
4. **The full set of `MULTIVERSE_*` env vars** is available to every MCP server process and hook script spawned by a multiverse-managed claude session.

For deeper architectural notes, hidden CLI flags, hook output quirks, and lessons learned along the way, see [`docs/PROGRESS.md`](../docs/PROGRESS.md) in the repo root.

## Authors

- **Silicon23** — [GitHub](https://github.com/Silicon23)
- **shaoxiongduan** — [GitHub](https://github.com/shaoxiongduan)
