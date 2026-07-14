# Multiverse Codex Plugin

Codex-side lifecycle and MCP integration for the Multiverse wrapper.

The plugin bundles:

- lifecycle hooks that record the Codex thread ID and dashboard status;
- the existing Multiverse MCP server, exposed to Codex sessions;
- native thread naming for Multiverse-managed Codex sessions.

During repository development, `multiverse-codex-session` injects these same
hook and MCP definitions into the wrapped Codex process. Installing the plugin
is therefore not required to exercise the wrapper from this checkout.
