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

Every `message_agent(...)` call uses active delivery. An unattended live Codex
terminal is prompted to call `check_inbox` only when its input composer is
focused. A typed-but-unsent draft is saved, cleared, and restored around that
prompt. If another terminal surface is active, no keys are sent and the message
stays queued. A dormant or ended Codex agent is resumed with the inbox prompt as
its first user turn. Starting, approval-waiting, and user-attached sessions are
also left queued.

The same durable inbox supports Codex-to-Codex, Codex-to-Claude, and
Claude-to-Codex messages. Cross-workspace messages retain the sender workspace
so `check_inbox` can tell the recipient how to route a reply. Internal delivery
logs include caller and recipient runtimes, but the agent-facing registry and
message payload format remain runtime-neutral.

During repository development, `multiverse-codex-session` injects these same
hook and MCP definitions into the wrapped Codex process. Installing the plugin
is therefore not required to exercise the wrapper from this checkout.

## Agent discovery

`list_agents()` uses one workspace registry for Claude and Codex sessions, so
callers from either runtime can discover every named agent. Its text format is
unchanged from the Claude-only tool: entries include the agent name and status,
but intentionally do not expose which runtime backs an agent.

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
