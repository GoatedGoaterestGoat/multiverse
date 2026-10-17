# Future work

Ideas that have been discussed and agreed on but are not scheduled yet. Each entry says what it is, why it is wanted, and what is known about it. When an item is scheduled, it moves to a design doc or a PR and leaves this list.

## Plugins

- **Sandboxed plugins.** Plugin code runs inside mv2's processes, so effect classes and permissions are enforced only at mv2's API, plus a best-effort Python audit hook that is not a security boundary. Real isolation means running plugins in separate, sandboxed processes that reach mv2 only through an API. The plugin host's worker processes are the starting point. Until then, trust (`mv2 plugins trust`) is the security line.
- **Numeric spend caps.** Access levels (observe, spend, act) are the main control. A cap on how much a plugin may spend (runs per hour, tokens, money) can be added later if someone needs it.
- **Startup cost.** Built-in commands read plugin manifests only, never plugin code or the trust hash. After PR #20 they cost 20 to 30 ms more than with plugins off, measured with a 47 MB universe plugin. Agents call `send` and `report` constantly, so keep measuring it.
- **The known-plugins table as data.** Core's table of known checkout plugins (`compaction` and its old `compaction.json`) moves to a data file kept with the submodules, so core never names a plugin. Planned for the first transcript-protocols PR.

## Harness adapter protocols

The transcript protocols (reading rows by cursor, labels, turns, model metadata, the one-shot run) are designed in their own document and ship Claude-only first.

- **Codex and Pi versions of the transcript protocols.** These need a dedicated Codex agent. For Codex's one-shot run, tools have to be switchable off verifiably (0.162.1 can't), then it runs on a private app-server sharing the user's login.
- **Install by seeding a new session.** A generic fallback for harnesses without an in-process compaction hook: start a new session from the compacted rows and rebind the agent to it. It costs a restart and the agent's in-memory state.
- **Interrupt:** stop the current turn without ending the session. Today the only option is kill.
- **Pending prompt:** read what a waiting agent is waiting on (a permission request, a question). Answering it remotely, for example approving from remote control, comes after that, and is security-sensitive.
- **Native compaction trigger:** ask the harness to compact now, where it supports it.
- **Fork:** start a new agent from another agent's conversation so far.
- **Live rename,** and switching model or effort mid-session.
- **Normalized conversation entries** (API 1.12 in the transcript design): a harness-neutral view of rows, plus cross-harness conversion.

## Harnesses

- **Codex `/new` and `/resume` inside a running agent.** On Codex 0.163.0 the bridge relay refuses both, so the agent keeps its conversation and nothing is lost. If a later Codex lets the terminal switch conversations, record the change in the session lineage as Claude's `/clear` does.
- **Plugin worker liveness outside tmux.** Covered in PR #20 by workers checking their parent. Revisit if the host ever runs without tmux by design.
