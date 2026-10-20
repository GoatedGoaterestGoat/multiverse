---
name: coordination
description: Coordinate agents within a universe or across subverses.
---

Use `"$MV2_CLI" --help` for the CLI. Your current universe and identity are already in the environment.

Send messages with a quoted heredoc; stdin is read automatically when piped or redirected:

```sh
"$MV2_CLI" send AGENT <<'EOF'
Please inspect the current diff.
Keep quotes, apostrophes, $variables and `backticks` literal.
EOF
```

The quoted delimiter needs no escaping inside the message. Use a delimiter that does not occur alone on a message line. `--text` remains useful for short messages, `--file PATH` reads a file and `--stdin` explicitly reads stdin. Empty messages are errors.

- `spawn --name worker --harness codex --task 'initial prompt'` starts an independent agent. Add `--wait` to spawn or resume when a script must send immediately after launch; `--wait-timeout SECONDS` defaults to 60. Read the result; timeout leaves the launch running, so inspect it before retrying. Scripts can request the full record with `--json` and check `ready`/`ok`. Use `claude` for Claude Code or another name from the harness catalog.
- `list --recursive` finds agents in this universe and its descendants. Use `-u /absolute/universe/path` before a command to target another universe. Use the unique agent names shown by `list`; scripts can request full records with `--json`.
- `send AGENT` saves and attempts delivery, printing one short result without echoing your message. `send first,second,third` messages several agents, with one result each; failure for one does not block the others. Saved or accepted is not read or answered. `deliver AGENT --retry` retries definite failures; `deliver AGENT --check` checks uncertain sends and confirms accepted ones without sending again. Both print short results; `--json` keeps full machine records. Never blindly resend an uncertain message.
- Choose each message's delivery by how urgent it is, and name it: the CLI's default is `steer`.
  - `--delivery inbox-only` for almost everything: progress notes, FYIs, context, anything that can wait until the recipient next checks its inbox. It never interrupts or wakes anyone.
  - `--delivery follow-up` when you want a quick reply: a question that blocks your work, or the results of a task the recipient gave you, which it is waiting for. It arrives when the recipient's current turn ends, or at once if it is idle, and wakes it if it is stopped.
  - `--delivery steer` only when the message is urgent and immediately relevant to the recipient's current work, such as work that is wrong or harmful. It interrupts at the recipient's next pause.

  Status updates sent as steer or follow-up pile up on a lead and bury its own work and its questions to the user. Report your status with `report` instead; `list` and the dashboard show it. Put several updates in one message rather than sending each.
- `focus` keeps your question on top until the user answers. `focus --turn` lasts until your next turn ends (mid-turn, the one after this), `focus --for 20m` for a time (at most 1d), and `focus` alone until `unfocus`. While you are focused, steer and follow-up messages to you are refused and not saved, and their senders are told to send inbox-only. Inbox-only messages still arrive. `unfocus` ends it and says how many unread inbox messages wait. You change only your own focus; the user can change anyone's. When `send` says a recipient is focused, send `--delivery inbox-only` if it can wait, or hold the message. Do not work around it.
- Steer/follow-up to a stopped or dormant Claude, Codex or Pi agent resumes its exact conversation with your message as the interactive starting prompt, including plain launches. Inbox-only never wakes. Starting with a message does not prove it was read. Further messages to a starting bridge stay saved and are delivered when ready; startup failures stay saved for explicit retry. A live plain session still reports native delivery unavailable. Check `capabilities --agent AGENT` for `wake` support.
- `inspect AGENT` captures its pane. `attach AGENT` opens its terminal. Use tmux detach to return.
- `resume AGENT` resumes a stopped or dormant agent in its saved native session. `kill AGENT` cancels native work, ends its process and removes its tmux wrapper; `stop AGENT` is an alias. Both work after the pane is gone to cancel surviving shared-server work, and report what was cancelled. Use shutdown only when appropriate. Live states progress through ending, stopped (wrapper retained) and dormant (wrapper gone).
- `inbox` prints unread inbox-only messages as sender/content blocks and marks them read only after successful output. Pages contain up to 50 messages; repeat until `No unread messages.` to drain them. `inbox --peek` leaves messages unread. `inbox --wait` waits for an unread inbox-only message; add `--timeout 30s` or `2m` to limit it. Timeout exits 1 without marking messages read. It cannot combine with history options. Native-delivered messages arrive directly with their sender and text. `inbox --all` browses readable history without consuming it; add `--last 20` or `--since 2h` (or an ISO timestamp) for recent messages. Together they filter by time, then select the newest messages and print them oldest first. A history message may still arrive natively. Scripts can use `--json`; cursor paging requires `--all --json --after CURSOR`.

- Report your own phase with `report --phase working|blocked|done --summary '...'`. The CLI prints one line such as `Reported working.`; when you have unread inbox messages it adds how many (`You have 2 unread inbox messages.`), and `mv2 inbox` reads them. `done` means your current task is finished, so what waits in your inbox comes next: after `report --phase done` the line reads `Reported done. You have 2 unread inbox messages: read them now with mv2 inbox and address them before you finish.` Do that before you end your turn. Properties are self-reported. The TUI shows process observations separately.

- `update --check` fetches and checks the running mv2 source checkout; `update` fast-forwards its current branch from its upstream and syncs its environment with `uv sync --locked` when available. Tracked changes, missing upstreams and local commits are refused; no branch changes or stashing. Use `--json` for scripts. Running agents and the dashboard need relaunching to use the new code; `refresh --apply` updates installed universe template files.

Harness instructions live in `harnesses/<name>/`. If an adapter fails, inspect its manifest, docs and pane. Do not type into a pane based only on its window name. Match the recorded universe, agent and launch tags first. Avoid sending keys into an active model turn or an unknown shell.

Workflows are prompts. Read the selected workflow, delegate bounded work, review evidence, and choose the next step. No scheduler decides for you. A manager is an ordinary agent with an appropriate task.
