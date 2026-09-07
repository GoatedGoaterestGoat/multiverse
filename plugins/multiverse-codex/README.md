# Multiverse Codex Plugin

Codex-side lifecycle and MCP integration for the Multiverse wrapper.

The plugin bundles:

- lifecycle hooks that record the Codex thread ID and dashboard status;
- the existing Multiverse MCP server, exposed to Codex sessions;
- native thread naming for Multiverse-managed Codex sessions.

## Shared tool guidance

Codex and Claude load the same Multiverse MCP server. Tool names, schemas,
response semantics, and agent-facing usage guidance therefore come from the same
`src/server.py` definitions. The Codex plugin does not duplicate those
instructions; only runtime-specific lifecycle and inbox behavior is documented
here.

## Inbox tools

Codex inbox delivery is explicitly pull-based. Pending messages remain in the
shared durable inbox until the recipient calls `check_inbox`; lifecycle hooks
do not consume or inject them.

- `check_inbox()` atomically claims all pending messages and returns their
  contents. Concurrent callers cannot receive the same message twice, and an
  empty inbox returns `You have no pending inbox messages.`
- `monitor_inbox(timeout_seconds=300)` waits asynchronously for a message
  without consuming it. The Multiverse dashboard reports `listening` during
  the wait. When it returns, call `check_inbox()` to read the message.

All tools in the existing `multiverse` MCP namespace use a Codex client timeout
of `3153600000` seconds (100 years at 365 days/year), in both the bundled MCP
configuration and fresh/resumed wrapper launches. Tool-specific deadlines still
apply: `monitor_inbox` defaults to 300 seconds but accepts longer waits, such as
`monitor_inbox(timeout_seconds=86400)` for one day. User cancellation is unchanged.
Restart/resume an existing managed Codex session to pick up the client timeout.

Every `message_agent(...)` call uses active delivery. An unattended live Codex
terminal is prompted to call `check_inbox` only when its input composer is
focused. A typed-but-unsent draft is saved, cleared, and restored around that
prompt. If another terminal surface is active, no keys are sent and the message
stays queued. A dormant or ended Codex agent is resumed with the inbox prompt as
its first user turn. Starting and approval-waiting sessions remain queued.
Attached sessions are delivered to whenever the normal composer is focused;
attachment by itself does not prevent delivery.

A retired agent is permanently stopped after handing off to a saved successor.
`message_agent` rejects it without writing an inbox file and names that successor;
`ask_agent` remains available through its read-only ephemeral native-thread fork.
While its handoff is pending, an agent is `retiring` and cannot receive messages
or new prompts either. `message_agent` rejects it without queueing anything:
`This agent is being retired, please try again later`.

The same durable inbox supports Codex-to-Codex, Codex-to-Claude, and
Claude-to-Codex messages. Cross-workspace messages retain the sender workspace
so `check_inbox` can tell the recipient how to route a reply. Internal delivery
logs include caller and recipient runtimes, but the agent-facing registry and
message payload format remain runtime-neutral.

During repository development, `multiverse-codex-session` injects these same
hook and MCP definitions into the wrapped Codex process. Installing the plugin
is therefore not required to exercise the wrapper from this checkout.

## Approval mode

The wrapper starts every fresh or resumed Codex session with `--approve-for-me`.
Codex routes approval requests through its automatic reviewer and uses the
`workspace-write` sandbox for this preset. The CLI rejects combining
`--approve-for-me` with `--sandbox danger-full-access`, so the wrapper does not
add a separate sandbox flag.

## Agent discovery

`list_agents()` uses one workspace registry for Claude and Codex sessions, so
callers from either runtime can discover every named agent. Entries include
the agent name, status, and Description (`None` when unset), but intentionally
do not expose which runtime backs an agent.

`set_description(description)` sets only the calling agent's short work/role
label: at most 60 Unicode characters on one line. It can summarize responsibilities,
completed tasks, ongoing implementations, or expertise to help others delegate
at a glance. Agents should update it at their discretion as their work changes.
Over-limit calls return an explicit error without changing the previous value;
an empty string clears it. New agents start without a description; resumes retain
it. While unset, every inter-agent tool result includes a reminder to set one
when ready. The same description appears in the dashboard's single-line column.

## Ephemeral questions

`ask_agent()` and the dashboard's ask-all action can target Codex threads as
well as Claude sessions. A Codex target is cloned through the app-server
`thread/fork` method with `ephemeral: true`; one question is submitted to that
in-memory fork and the final answer is returned to the caller. The target's
interactive thread is neither interrupted nor changed.

Side-question forks run with tool-bearing Codex features, hooks, MCP servers,
apps, web search, filesystem access, and network access disabled. Multiverse
also rejects the result and terminates the fork if an action item appears in
the event stream. All caller-scoped `MULTIVERSE_*` variables are stripped from
the fork process. The same primitive powers MCP `ask_agent`, mixed-runtime
dashboard ask-all, SecondBrain `ask_agent`, and first-connect workspace
summaries.
