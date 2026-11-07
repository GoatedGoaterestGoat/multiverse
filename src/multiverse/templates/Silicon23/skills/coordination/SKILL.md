---
name: coordination
description: Coordinate agents within a universe or across subverses.
---

Use `"$MV2_CLI" --help` for the CLI. Your current universe and identity are already in the environment.

Send messages with a quoted heredoc; stdin is read automatically when piped or redirected:

```sh
"$MV2_CLI" send AGENT --delivery follow-up <<'EOF'
Please inspect the current diff.
Keep quotes, apostrophes, $variables and `backticks` literal.
EOF
```

The quoted delimiter needs no escaping inside the message. Use a delimiter that does not occur alone on a message line. `--text` remains useful for short messages, `--file PATH` reads a file and `--stdin` explicitly reads stdin. Empty messages are errors.

- `spawn --name worker --harness codex --task 'initial prompt'` starts an independent agent. Add `--wait` to spawn or resume when a script must send immediately after launch; `--wait-timeout SECONDS` defaults to 60. Read the result; timeout leaves the launch running, so inspect it before retrying. Scripts can request the full record with `--json` and check `ready`/`ok`. Use `claude` for Claude Code or another name from the harness catalog.
- `list --recursive` finds agents in this universe and its descendants. Use `-u /absolute/universe/path` before a command to target another universe. Use the unique agent names shown by `list`; scripts can request full records with `--json`.
- `send AGENT` saves and attempts delivery, printing one short result without echoing your message. `send first,second,third` messages several agents, with one result each; failure for one does not block the others. Saved or accepted is not read or answered. `deliver AGENT --retry` retries definite failures; `deliver AGENT --check` checks uncertain sends and confirms accepted ones without sending again. Both print short results; `--json` keeps full machine records. Never blindly resend an uncertain message.
- Choose delivery explicitly; the CLI's default is `steer`.
  - Use `--delivery inbox-only` only when both hold: the recipient needn't act on the message (an FYI), and none of your work waits on a reply, even if it reads its inbox much later. Inbox-only never interrupts or wakes anyone.
  - Otherwise use `--delivery follow-up`: tasks, replies your work needs, or results the recipient is waiting for. It arrives after the current turn, or at once if idle, and wakes stopped agents. It arrives as a prompt, not in the inbox. To await a follow-up reply, end your turn; `inbox --wait` never sees it.
  - Use `--delivery steer` only when urgent and immediately relevant to the recipient's current work. It interrupts at the next safe pause.

  Keep asks short: the question plus only what is needed to answer. Put background and side notes in your notes, or inbox-only if both tests pass.

  Status updates sent as steer or follow-up pile up on a lead and bury its own work and its questions to the user. Report your status with `report` instead; `list` and the dashboard show it. Put several updates in one message rather than sending each. Ask the user directly for decisions; don't ask another agent to relay your question or the answer.
- `focus` keeps your question on top until the user answers. `focus --turn` lasts until your next turn ends (mid-turn, the one after this), and `focus` alone until `unfocus`. Only the user can focus for a time. While you are focused, steer and follow-up messages to you wait in your inbox; `held` reads only those, once the focus ends. `unfocus` ends it and says what waits. A question tool that waits for the answer within your turn needs no focus. `--turn` counts any turn end, including one your harness starts when a background task finishes, so while background work runs, use `focus` alone instead. Do the same if `focus --turn` is refused because your harness reports no status. To listen while you wait for the user, start `listen` before `focus`. You change only your own focus; the user can change anyone's. When `send` says a recipient is focused, your message is in its inbox: do not resend it.
- Steer/follow-up to a stopped or dormant Claude, Codex or Pi agent resumes its exact conversation with your message as the interactive starting prompt, including plain launches. Inbox-only never wakes. Starting with a message does not prove it was read. Further messages to a starting bridge stay saved and are delivered when ready; startup failures stay saved for explicit retry. A live plain session still reports native delivery unavailable. Check `capabilities --agent AGENT` for `wake` support.
- `inspect AGENT` captures its pane. `attach AGENT` opens its terminal. Use tmux detach to return.
- `resume AGENT` resumes a stopped or dormant agent in its saved native session. `kill AGENT` cancels native work, ends its process and removes its tmux wrapper; `stop AGENT` is an alias. Both work after the pane is gone to cancel surviving shared-server work, and report what was cancelled. Use shutdown only when appropriate. Live states progress through ending, stopped (wrapper retained) and dormant (wrapper gone).
- `inbox` prints unread inbox-only messages as sender/content blocks and marks them read only after successful output. Pages contain up to 50 messages; repeat until `No unread messages.` to drain them. `inbox --peek` leaves messages unread. `inbox --wait` waits for an unread inbox-only message (never a steer or follow-up); add `--timeout 30s` or `2m` to limit it. Timeout exits 1 without marking messages read. It cannot combine with history options. Native-delivered messages arrive directly with their sender and text. `inbox --all` browses readable history without consuming it; add `--last 20` or `--since 2h` (or an ISO timestamp) for recent messages. Together they filter by time, then select the newest messages and print them oldest first. A history message may still arrive natively. Scripts can use `--json`; cursor paging requires `--all --json --after CURSOR`.

When you are done and waiting, run `"$MV2_CLI" listen` in a background shell. It exits when an unread inbox-only message arrives; run `"$MV2_CLI" inbox` to read it, then start listen again when you have dealt with it. If new work arrives meanwhile, stop that background shell and start it again when you are done and waiting.

- Report your own phase with `report --phase working|blocked|done --summary '...'`. The CLI prints one line such as `Reported working.`; when you have unread inbox messages it adds how many (`You have 2 unread inbox messages.`), and `mv2 inbox` reads them. `done` means your current task is finished, so what waits in your inbox comes next: after `report --phase done` the line reads `Reported done. You have 2 unread inbox messages: read them now with mv2 inbox and address them before you finish.` Do that before you end your turn. Properties are self-reported. The TUI shows process observations separately.

- `update --check` fetches and checks the running mv2 source checkout; `update` fast-forwards its current branch from its upstream and syncs its environment with `uv sync --locked` when available. Tracked changes, missing upstreams and local commits are refused; no branch changes or stashing. Use `--json` for scripts. Running agents and the dashboard need relaunching to use the new code; `refresh --apply` updates installed universe template files.

Harness instructions live in `harnesses/<name>/`. If an adapter fails, inspect its manifest, docs and pane. Do not type into a pane based only on its window name. Match the recorded universe, agent and launch tags first. Avoid sending keys into an active model turn or an unknown shell.

Workflows are prompts. Read the selected workflow, delegate bounded work, review evidence, and choose the next step. No scheduler decides for you. A manager is an ordinary agent with an appropriate task.
