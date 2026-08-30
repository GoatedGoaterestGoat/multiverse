# multiverse for pi

The pi-side half of the Multiverse plugin. Where Claude Code gets an MCP
server plus a set of hook scripts, and Codex gets its own plugin, pi gets one
extension.

```bash
pi --extension /path/to/multiverse-plugin/pi-extension/index.ts
```

`bin/multiverse-pi-session` — the dedicated runner the monitor execs for a
pi-runtime session — passes this automatically, so you only run it by hand
when testing outside the monitor.

## What it does

| | |
|---|---|
| **Tools** | `list_agents`, `message_agent`, `ask_agent`, `check_inbox`, `monitor_inbox`, `spawn_agent` — the same six every other runtime exposes, with the same semantics |
| **Status** | Writes `status.json` from pi's lifecycle events, so pi sessions appear in the monitor dashboard alongside Claude and Codex ones |
| **Session id** | Records pi's native session id so the monitor can resume it |
| **Inbox** | Delivers inter-agent messages into the conversation, including into a session that is idle at the prompt |
| **Commands** | `/mv-inbox`, `/mv-agents` |

## Compaction is pi's

This extension holds **no compaction code** and does not subscribe to
`session_before_compact`. pi compacts natively, on its own `reserveTokens` /
`keepRecentTokens` settings or on `/compact` — same arrangement as the Codex
runtime. There is nothing here to configure and nothing to keep in sync with
the Claude Code compaction pipeline.

## How it reaches Multiverse state

Through `mvctl` (`src/mvctl.py`), a CLI over the exact functions the MCP
server calls. The inbox protocol, spawn brokering, ask-forks, and the session
registry are implemented once, in Python; the extension does not reimplement
any of them. One short-lived process per tool call — invisible next to a model
round trip — in exchange for one implementation instead of two.

The extension prefers the plugin's own `.venv/bin/python` over `uv run`: `uv
run` re-resolves the lockfile on every invocation, which turns a ~70 ms exec
into a ~400 ms one on the interactive path.

## What pi gives us that Claude Code does not

`pi.sendMessage(..., { triggerTurn: true })` delivers into a running session.
Claude Code has no equivalent, which is why its delivery path types a wake
prompt into the recipient's tmux pane with `send-keys` — and skips that
entirely when a user is attached, because it would overwrite what they were
typing. Here it is a poll plus a direct send: no terminal manipulation, and it
behaves the same whether or not someone is attached.

## Delivery is claim → send → commit

`mvctl claim-inbox` *stages* pending messages and returns their claim names;
only `mvctl commit-inbox --claim <name>` removes them after `pi.sendMessage`
accepts the injection call. A synchronous injection failure releases this
deliverer's claims immediately so the next poll can retry; process death leaves
the staged files for Multiverse's stale-claim recovery. If the commit fails,
the safe failure mode is a duplicate delivery on the next poll.

`pi.sendMessage` is fire-and-forget and provides no later acknowledgement, so
an asynchronous rejection beyond the injection call cannot be observed by the
extension. The claim protocol protects every failure boundary that the API
makes visible without deleting a message before attempting delivery.

Delivery happens at `agent_settled` (anything that arrived mid-turn) and from
a 2-second poll while the agent is idle (anything that arrives at the prompt).
It deliberately does **not** happen at `session_start`: a session started with
an initial prompt — which is how `spawn_agent` hands a subagent its work —
submits that prompt right after `session_start` returns, and a delivery that
triggers its own turn first makes pi reject the initial prompt outright.

`monitor_inbox` suspends the watcher while it is parked, so the poll cannot
claim the very message the tool call is blocking on.

## Timeouts

Every bridge call has a client-side deadline except `spawn_agent`, which has
none: spawn approval waits for a human to answer a modal, and Multiverse
removed the spawn timeout precisely so a slow approval is not an error. A
deadline here would reintroduce it and orphan a request the user is still
looking at.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `MULTIVERSE_PI_BIN` | `pi` | Which pi binary the stack invokes |
| `MULTIVERSE_PLUGIN_ROOT` | *(set by the monitor)* | Where `mvctl` and the extension live |
| `MULTIVERSE_SESSION_NAME` / `MULTIVERSE_SESSION_CWD` | *(set by the monitor)* | Caller identity and workspace scope |

## Standalone behavior

Without the monitor's `MULTIVERSE_*` environment the inter-agent tools are not
registered at all, rather than registered and always failing: the state
directory they coordinate through does not exist, so there is nothing useful
for the model to call. Status writing and session-id recording still run.

## Tests

`tests/test_pi_extension.py` drives the extension end-to-end against a
scriptable mock provider — no network, no credentials, no model. It checks
that pi accepts the tool schemas, that a tool call round-trips through
`mvctl`, that lifecycle events write `status.json` and the native session id,
and that a queued inter-agent message reaches the conversation and is
committed out of the inbox. It skips itself when `pi` is not on PATH.

```bash
python tests/test_pi_extension.py
```
