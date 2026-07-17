# Multiverse Codex Plugin

Codex-side lifecycle and MCP integration for the Multiverse wrapper.

The plugin bundles:

- lifecycle hooks that record the Codex thread ID and dashboard status;
- the existing Multiverse MCP server, exposed to Codex sessions;
- native thread naming for Multiverse-managed Codex sessions.

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

An urgent `message_agent(..., push_notification=True)` wakes an unattended
live Codex terminal with a prompt to call `check_inbox`. A dormant or ended
Codex agent is resumed with that prompt as its first user turn. Starting,
approval-waiting, and user-attached sessions are left queued.

During repository development, `multiverse-codex-session` injects these same
hook and MCP definitions into the wrapped Codex process. Installing the plugin
is therefore not required to exercise the wrapper from this checkout.

## Agent discovery

`list_agents()` uses one workspace registry for Claude and Codex sessions, so
callers from either runtime can discover every named agent. Its text format is
unchanged from the Claude-only tool: entries include the agent name and status,
but intentionally do not expose which runtime backs an agent.
