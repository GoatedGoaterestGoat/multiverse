---
name: coordination
description: Coordinate agents within a universe or across subverses.
---

Use `"$MV2_CLI" --help` for the CLI. Your current universe and identity are already in the environment.

- `spawn --name worker --harness codex --task 'initial prompt'` starts an independent agent. Add `--wait` to spawn or resume when a script must send immediately after launch; `--wait-timeout SECONDS` defaults to 60. Check `ready`/`ok`; timeout leaves the launch running, so inspect it before retrying. Use `claude` for Claude Code or another name from the harness catalog.
- `list --recursive` finds agents in this universe and its descendants. Use `-u /absolute/universe/path` before a command to target another universe. IDs are authoritative; unique names or ID prefixes also work.
- `send AGENT --text 'message'` saves and attempts native delivery. Use `--delivery steer` for a safe-boundary steer or `--delivery follow-up` for the next turn. Inspect the returned state: saved/accepted is not read/answered; unavailable/error retains the message. `deliver AGENT --retry` retries that recipient's definite failures. `deliver AGENT --check` reconciles pending or uncertain native deliveries and records receipts for accepted ones, without submitting them. Never blindly resend an uncertain submission.
- Steer/follow-up to a stopped or dormant Claude, Codex or Pi agent resumes its exact conversation with your message as the interactive starting prompt, including plain launches. Inbox-only never wakes. Startup acceptance means the process started; consumption remains unconfirmed. Further messages to a starting bridge return `pending` and are delivered when ready; startup failures stay saved for explicit retry. A live plain session still reports native delivery unavailable. Check `capabilities --agent AGENT` for `wake` support.
- `inspect AGENT` captures its pane. `attach AGENT` opens its terminal. Use tmux detach to return.
- `resume AGENT` resumes a stopped or dormant agent in its saved native session. `kill AGENT` cancels native work, ends its process and removes its tmux wrapper; `stop AGENT` is an alias. Both work after the pane is gone to cancel surviving shared-server work, and report what was cancelled. Use shutdown only when appropriate. Live states progress through ending, stopped (wrapper retained) and dormant (wrapper gone).
- `inbox` shows unread inbox-only messages and advances its read position automatically. Native-delivered messages arrive directly with their sender and text. `inbox --all` is the full raw history, including native delivery evidence; it does not change the ordinary read position. A message viewed in history may still arrive through native delivery. Use `--after CURSOR` to page through that history.

- Report your own phase with `report --phase working|blocked|done --summary '...'`. The CLI prints one line such as `Reported working: Checking launch permissions`. Properties are self-reported. The TUI shows process observations separately.

Harness instructions live in `harnesses/<name>/`. If an adapter fails, inspect its manifest, docs and pane. Do not type into a pane based only on its window name. Match the recorded universe, agent and launch tags first. Avoid sending keys into an active model turn or an unknown shell.

Workflows are prompts. Read the selected workflow, delegate bounded work, review evidence, and choose the next step. No scheduler decides for you. A manager is an ordinary agent with an appropriate task.
