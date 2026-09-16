# Claude Code

The adapter is JSON only. CLI flags were checked against Claude Code 2.1.231 and the [official reference](https://code.claude.com/docs/en/cli-reference). It launches the installed `claude` CLI with its account, model defaults, permissions and project context, appending the universe bootstrap to its normal system prompt. No plugin, hook or MCP server is required.

The Multiverse agent UUID is assigned as Claude's native session UUID with `--session-id`. Resume uses that exact UUID with `--resume`; it never selects a recent session from a shared workspace. Native sessions must remain on disk for resume to work. The inherited `CLAUDECODE` nesting marker is removed because this is an independent tmux agent.

The model checks and acknowledges its durable inbox at work checkpoints. A message does not automatically wake an idle model; attach to ask it to check if necessary. Activity stays `unknown` without an explicit report; terminal liveness is still observed through tagged tmux panes. `/multiverse` is a prompt convention, not a registered Claude slash command: send `Please run /multiverse list` as ordinary text, or run `mv2 catalog` in a shell.

Use `mv2 inspect AGENT`, `runtime.json` and `claude --help` when troubleshooting. Attach for native login, workspace trust or permission prompts. Launch/resume argv and identity are integration-tested with a fake executable; authenticated model turns are not part of the offline test suite.
