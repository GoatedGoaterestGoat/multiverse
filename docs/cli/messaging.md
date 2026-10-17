# Messaging

Agents use these commands to message each other, read their inboxes and report progress. You can send to an agent the same way from a shell. To talk to an agent directly, attach to its terminal from the [dashboard](../use/dashboard.md) instead.

## Send a message

```sh
mv2 send worker <<'EOF'
Please review the current diff.
Keep $variables, "quotes" and `backticks` as they are.
EOF
```

A quoted heredoc (`<<'EOF'`) passes the text exactly as written, so nothing needs escaping. You can also give the text inline, from a file or from a pipe, and name several recipients:

```sh
mv2 send worker --text 'Short message.'
mv2 send worker --file notes.md
some-command | mv2 send worker
mv2 send reviewer,tester --text 'The fix is on branch fix-login.'
```

Separate several recipients with commas. Each one gets the message and its own result line. A failure for one doesn't stop the others. The recipient sees the message as `Message from SENDER:` followed by your text.

## Choose a delivery

```sh
mv2 send worker --delivery follow-up --text 'When you finish, also update the changelog.'
mv2 send worker --delivery inbox-only --text 'FYI: CI is red on main.'
```

| Delivery | Use it for | Arrives |
| --- | --- | --- |
| `steer` (default) | Corrections and anything that changes the current work | At the next safe point in the current turn. If the agent is idle, it starts a turn |
| `follow-up` | The next thing to do | After the current turn ends |
| `inbox-only` | Information the agent can read when it's ready | Only when the agent runs `mv2 inbox` |

Every message is saved to the recipient's inbox before mv2 tries to deliver it, so nothing is lost when delivery fails. A `steer` or `follow-up` message to a stopped or dormant agent resumes it, with the message as its first prompt. An `inbox-only` message never wakes an agent.

A running agent without a live connection can't receive `steer` or `follow-up` messages, so send it `inbox-only` messages instead. See [live connection and launch-only](../concepts.md#live-connection-and-launch-only).

## Read the result

`send` prints one line per recipient. Confirmations go to stdout. Failures and uncertain deliveries go to stderr, so redirecting stdout doesn't hide them. These are the common ones:

| Result | Meaning | What to do |
| --- | --- | --- |
| `NAME: message confirmed in its conversation.` | The agent's session recorded it | Nothing |
| `NAME has your message for its next turn.` / `queued for its next turn` | It was accepted and waits for the turn to end | Nothing. Don't resend it |
| `NAME: message accepted; no confirmation yet. Do not resend.` | The session took it, but no confirmation has arrived | Run `mv2 deliver NAME --check` later |
| `NAME was stopped; resuming it with your message.` | The agent is starting with your message as its prompt | Nothing |
| `NAME is starting; your message will be delivered when ready.` | It's waiting for the session to be ready | Nothing |
| `NAME: cannot yet tell whether it received the message.` | Delivery is uncertain | Run `mv2 deliver NAME --check` before sending again |
| `Not delivered to NAME: REASON. Saved; …` | It was definitely not delivered | Fix the reason, then run `mv2 deliver NAME --retry`. For a running agent whose messaging broke, the line says to run `mv2 kill NAME` and `mv2 resume NAME` first |
| `Saved to NAME's inbox; it sees it at its next check.` | It was an `inbox-only` message | Nothing |

A delivered message has not necessarily been read or answered. Wait for a reply, or check the agent's phase with `mv2 list`.

## Check and retry deliveries

```sh
mv2 deliver worker --check    # settle uncertain deliveries, record confirmations
mv2 deliver worker --retry    # resend messages that definitely failed
```

`--retry` resends only definite failures. It never resends messages that were accepted or are uncertain. Don't resend an uncertain message by hand, because the agent may already have it.

## Read an inbox

Agents check their inbox at startup, between steps and before finishing. You can read any agent's inbox too.

```sh
mv2 inbox                              # inside an agent: its unread inbox-only messages
mv2 inbox --agent worker --peek        # read without marking them read
mv2 inbox --agent worker --all --last 20
mv2 inbox --agent worker --all --since 2h
mv2 inbox --wait --timeout 5m          # wait for the next inbox-only message
```

Plain `inbox` shows unread `inbox-only` messages and marks them read once printed. Messages delivered directly already reached the agent's conversation, so `inbox` doesn't repeat them. `--all` shows the full history, and `--json` adds the delivery details.

`--wait` without `--timeout` waits indefinitely. When a timeout runs out, `inbox` prints a line and exits 1.

## Report progress

```sh
mv2 report --phase working --summary 'Checking the launch permissions'
mv2 report --phase done --summary 'Fix on branch fix-login, tests pass'
```

The phases are `starting`, `working`, `blocked` and `done`. The phase and summary show in `mv2 list` and in the dashboard. If the agent has unread inbox-only messages, `report` says how many, so the agent notices them.

## Inside Claude and Pi sessions

With a live connection, Claude Code and Pi have a `/multiverse` command you can type in the agent's terminal:

| Command | Does | Claude | Pi |
| --- | --- | --- | --- |
| `/multiverse` or `/multiverse list` | Show the catalog | yes | yes |
| `/multiverse NAME [args]` | Run that command, workflow or skill as your prompt | yes | yes |
| `/multiverse send AGENT TEXT` | Message another agent as this agent | yes | no |
| `/multiverse inbox` | Show this agent's unread inbox-only messages and mark them read | yes | no |

In Codex and launch-only sessions, ask the agent in words instead, such as "Please run /multiverse list". You can also run `mv2 catalog` yourself.

## Scripts

Every command on this page takes `--json` and then prints the full record. With several recipients, `send --json` returns `recipients` and an overall `ok`.
