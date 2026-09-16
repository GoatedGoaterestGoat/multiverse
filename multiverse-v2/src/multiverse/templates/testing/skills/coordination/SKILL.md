---
name: coordination
description: Coordinate agents within a universe or across subverses.
---

Use `"$MV2_CLI" --help` for the CLI. Your current universe and identity are already in the environment.

- `spawn --name worker --harness codex --task 'initial prompt'` starts an independent agent. Use `claude` for Claude Code or another name from the harness catalog.
- `list --recursive` finds agents in this universe and its descendants. Use `-u /absolute/universe/path` before a command to target another universe. IDs are authoritative; unique names or ID prefixes also work.
- `send AGENT --text 'message'` durably queues input. Plain adapters check their inbox at work checkpoints; a queued message does not wake an idle agent. Pi's optional extension provides automatic delivery. Use `--reply-to MESSAGE_ID` to correlate answers.
- `inspect AGENT` captures its pane. `attach AGENT` opens its terminal. Use tmux detach to return.
- `resume AGENT` resumes its native session after it stops. `stop AGENT` terminates its terminal and running work; use only when appropriate.
- `inbox` peeks at unread messages; after reading, `ack --cursor N` acknowledges the returned byte offset. Check at startup, between work steps and before finishing. The optional Pi extension handles this automatically. Acknowledgment does not mean completed work. After a crash, duplicate delivery is possible; message IDs let you recognize it.
- Set your own status with `status --phase working|blocked|done --summary '...'`. Properties are self-reported. The TUI shows process observations separately.

Harness instructions live in `harnesses/<name>/`. If an adapter fails, inspect its manifest, docs and pane. Do not type into a pane based only on its window name. Match the recorded universe, agent and launch tags first. Avoid sending keys into an active model turn or an unknown shell.

Workflows are prompts. Read the selected workflow, delegate bounded work, review evidence, and choose the next step. No scheduler decides for you. A manager is an ordinary agent with an appropriate task.
