# multiverse for pi

The pi-side half of the multiverse plugin. Where Claude Code gets an MCP
server plus a set of hook scripts, pi gets one extension.

```bash
pi --extension /path/to/multiverse-plugin/pi-extension/index.ts
```

`bin/multiverse-session` passes this automatically for sessions whose harness
is `pi`, so you only run it by hand when testing outside the monitor.

## What it does

| | |
|---|---|
| **Tools** | `list_agents`, `message_agent`, `ask_agent`, `check_inbox`, `monitor_inbox`, `spawn_agent` — the same six the Claude Code MCP server exposes, with the same semantics |
| **Status** | Writes `status.json` from pi's lifecycle events, so pi sessions appear in the monitor dashboard alongside Claude ones |
| **Inbox** | Delivers inter-agent messages into the conversation, including into a session that is idle at the prompt |
| **Compaction** | Answers `session_before_compact` with a frontier-expanded summary from the summary-tree cache |
| **Commands** | `/mv-inbox`, `/mv-agents` |

## How it reaches multiverse state

Through `mvctl` (`src/mvctl.py`), a CLI over the exact functions the Claude
Code MCP server calls. The inbox protocol, spawn brokering, ask-forks, and the
tree cache are implemented once, in Python; the extension does not reimplement
any of them. One short-lived process per tool call, which is invisible next to
a model round trip, in exchange for one implementation instead of two.

The extension prefers the plugin's own `.venv/bin/python` over `uv run`: `uv
run` re-resolves the lockfile on every invocation, which turns a ~40 ms exec
into a ~400 ms one on the interactive path.

## Three things that are better here than on Claude Code

**Messages reach a live agent directly.** `pi.sendMessage(..., { triggerTurn:
true })` delivers into a running session. Claude Code has no equivalent, which
is why its push-notification path types a wake prompt into the recipient's
tmux pane with `send-keys` — and skips that entirely when a user is attached,
because it would overwrite what they were typing. Here it is a poll plus a
direct send, with no terminal manipulation and no attached-user carve-out.

**Compaction is an appended entry, not a file rewrite.** pi stores compaction
as one node in an append-only tree; the entries it replaces stay in the file.
Claude Code needs multiverse's two-JSONL mirror (full + live + uuid marker)
to get the same property. On pi there is no mirror at all, and re-compaction
regenerates from the original turns rather than compacting a previous summary.

**One process.** Claude Code spawns a hook process per lifecycle event; here
every event is a callback.

## Delivery is peek → send → ack

`pi.sendMessage` is fire-and-forget — it returns `void` and surfaces failures
as extension errors rather than throwing. So the extension does not claim a
message before sending it: it peeks (non-destructive), sends, then acks. A
crash between send and ack costs a duplicate delivery on the next poll, which
is strictly better than a claim-then-send ordering that can lose a message
when the send is rejected.

Delivery happens at `agent_settled` (anything that arrived mid-turn) and from
a 2-second poll while the agent is idle (anything that arrives at the prompt).
It deliberately does **not** happen at `session_start`: a session started with
an initial prompt — which is how `spawn_agent` hands a subagent its work —
submits that prompt right after `session_start` returns, and a delivery that
triggers its own turn first makes pi reject the initial prompt outright.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `MULTIVERSE_COMPACT_RATIO` | `0.3` | Fraction of the pre-compaction context to target |
| `MULTIVERSE_COMPACT_MIN` | `4000` | Budget floor, so a small compaction still produces something usable |
| `MULTIVERSE_COMPACT_MAX` | `120000` | Budget ceiling |
| `MULTIVERSE_PI_BIN` | `pi` | Which pi binary the stack invokes |

pi decides *when* to compact (its own `reserveTokens` / `keepRecentTokens`
settings, or `/compact`); multiverse decides *what* the compacted context is.
If the plan fails for any reason the handler returns nothing and pi falls back
to its own compaction — the session still gets compacted, just flatly.

## Standalone behavior

Without the monitor's `MULTIVERSE_*` environment the inter-agent tools are not
registered at all, rather than registered and always failing: the state
directory they coordinate through does not exist, so there is nothing useful
for the model to call. Status writing and the compaction hook still work.

## Tests

`tests/test_pi_extension.sh` drives the extension end-to-end against a
scriptable mock provider — no network, no credentials. It checks that pi
accepts the tool schemas, that a tool call round-trips through `mvctl`, that
lifecycle events write `status.json`, and that a queued inter-agent message
reaches the conversation.
