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

Agents are told to send `inbox-only` by default, so progress notes and FYIs wait in the inbox. They send `follow-up` for a new task, when they want a quick reply, or to return a task's results, and `steer` only when a message is urgent and immediately relevant to the recipient's current work. A follow-up reply arrives as a prompt, not in the inbox, so an agent waits for one by ending its turn rather than with `mv2 inbox --wait`. Their status goes through `mv2 report`, so a lead isn't interrupted by status updates.

Every message is saved to the recipient's inbox before mv2 tries to deliver it, so nothing is lost when delivery fails. The one exception is a `steer` or `follow-up` message to a [focused](#focus-an-agent) agent, which is refused and not saved. A `steer` or `follow-up` message to a stopped or dormant agent resumes it (with a harness that supports waking: Claude Code, Codex and Pi; a custom command harness doesn't), with the message as its first prompt. An `inbox-only` message never wakes an agent.

If a wake is refused, `send` and `deliver --check` show the complete reason, including the exact record paths and recovery instructions. Reflected message text remains hidden.

If a name or UUID prefix matches several agents, the error lists their names
and full UUIDs. Use one full UUID; nothing is saved to an ambiguous recipient.

A running agent without a live connection can't receive `steer` or `follow-up` messages, so send it `inbox-only` messages instead. See [live connection and launch-only](../concepts.md#live-connection-and-launch-only).

## Read the result

`send` prints one line per recipient. Confirmations go to stdout. Failures and uncertain deliveries go to stderr, so redirecting stdout doesn't hide them. These are the common ones:

| Result | Meaning | What to do |
| --- | --- | --- |
| `NAME: message confirmed in its conversation.` | The agent's session recorded it | Nothing |
| `Sent to NAME; it reads this at its next pause.` | A Claude agent took it; it joins the running turn at the next tool result, or starts a turn if idle | Nothing |
| `NAME has your message for its next turn.` | A `follow-up` was accepted. It waits for the turn to end, or starts one at once if the agent is idle | Nothing. Don't resend it |
| `NAME has your message queued for its next turn.` | It's in a connected Codex agent's queue, waiting for the turn to end | Nothing. Don't resend it |
| `NAME started a turn with your message.` | The agent was idle, so the message started a turn | Nothing |
| `NAME has your message queued; its next turn has not started.` | It's queued, but the turn didn't start | Run `mv2 deliver NAME --check` later; don't resend |
| `NAME: message accepted; no confirmation yet. Do not resend.` | The session took it, but no confirmation has arrived | Run `mv2 deliver NAME --check` later |
| `NAME was stopped; resuming it with your message.` | The agent is starting with your message as its prompt | Nothing |
| `NAME is starting; your message will be delivered when ready.` | It's waiting for the session to be ready | Nothing |
| `NAME: cannot yet tell whether it received the message.` | Delivery is uncertain | Run `mv2 deliver NAME --check` before sending again |
| `Not delivered to NAME: REASON. Saved; …` | Submission was rejected or delivery was reported failed | Fix the reason, then run `mv2 deliver NAME --retry`. A tmux socket outage or server conflict keeps its full diagnostic: resolve that terminal problem, then retry, without restarting the agent. For a running agent whose native connection broke, the line says to run `mv2 kill NAME` and `mv2 resume NAME` first. A harness configured without messaging instead points to inbox-only delivery or attaching; an automatic fallback stays retryable after a bridge resume |
| `Saved to NAME's inbox; it sees it at its next check.` | It was an `inbox-only` message | Nothing |
| `Not sent: NAME is focused until …` | The agent is [focused](#focus-an-agent), so nothing was saved | Send it again with `--delivery inbox-only`, or wait until it unfocuses |

A delivered message has not necessarily been read or answered. Wait for a reply, or check the agent's phase with `mv2 list`.

Failed delivery and check lines go to stderr and exit nonzero; successful lines
go to stdout. With `--json`, the result stays on stdout.

A message prepared more than 10 minutes after it was sent adds its age to the
sender line: `Message from lead (sent 25 min ago):`, or `Message from lead
(in universe review) (sent 25 min ago):`. Whole minutes are shown below an hour,
then hours, then days. Inbox reads, wake preparation and retries use the original
send time. Missing or unusable timestamps add no age. The text of a message
already accepted by a native queue is not rewritten by checking or retrying it.

## Check and retry deliveries

```sh
mv2 deliver worker --check    # settle uncertain deliveries, record confirmations
mv2 deliver worker --retry    # retry messages reported failed
mv2 deliver worker --dismiss  # close failures and unconfirmed messages without resending
```

`--dismiss` first checks existing delivery evidence for each eligible message,
using the same rules as `--check`. It records and counts any newly confirmed
messages and leaves them confirmed, even if you never ran `--check`. It then
makes remaining failures, launch-only refusals, accepted messages without
confirmation, and uncertain submissions final. It counts failures and
unconfirmed messages separately and warns that an unconfirmed message may
still have arrived. It sends nothing, wakes nothing, and does not cancel a
message already held by the native session. Pending startup deliveries and
confirmed messages stay unchanged.

Dismissal keeps the original message and receipt in history, with a dismissed
marker. Dismissed messages no longer appear in `--check` or count as delivery
problems, and a later `--retry` cannot resend them. A late row or receipt does
not reopen them. For example: `1 unconfirmed message dismissed; it may still
have arrived. Kept in history. Nothing resent.` Dismissal does not prove
non-delivery; verify the conversation before deliberately sending a new copy.

`--check` starts with one line of counts, such as `358 confirmed, 23 accepted
without confirmation, 2 not delivered`, then shows details only for messages
that are not confirmed. The counts describe the deliveries checked by this
invocation; an already recorded confirmation does not need another check.
`--json` keeps the complete per-message results, including confirmations.

`--check` reports a cancellation once as not delivered and exits nonzero, with
advice to send a new message if you still want it delivered. Later checks omit
that cancellation, including concurrent checks. It stays in history; neither
`--check` nor `--retry` resends it.

A message outside the recorded conversation lineage is also reported only once
by `--check` or `--retry`. Its saved delivery evidence remains unchanged, but it is final for
retry and no longer counts as a delivery problem in list, inspect or doctor.
`--retry` counts these messages with earlier final refusals, including on their first report, and exits successfully unless another retry fails. It says they cannot be retried. For a conversation retired by `--start-fresh`, the one report says the message never reached the new conversation and advises sending a new message. For other out-of-lineage messages, verify the original conversation before sending a new one.

`--check` also reports failures and exits nonzero while any checked
message failed. A damaged session journal or unloadable harness gives each affected message its own error; the remaining messages are still checked. Record errors name their file. Back up and repair a damaged `sessions.jsonl`, or restore an intact copy, before retrying. A submission needs its authoritative journal even when the saved message targets the current session; mv2 cannot assume that a torn transition left that pointer current. Reading an already saved failure does not need that transition proof. It does not resend them. List, inspect and doctor show how many
messages were not delivered and advise `mv2 deliver NAME --retry or --dismiss`.

Older definite refusals receive the same classification when list, inspect and
doctor count failures; reading does not rewrite the ledger. If `deliver --retry`
finalizes earlier refusals, it says how many and does not resend them. After an
explicit ledger reset, `--check` gives the manual-reading advice for untracked
messages on a running launch-only agent, without repeatedly asking an unsupported
receipt API. This launch-only classification never makes accepted or uncertain
tracking retryable.

A refused live delivery is final when the harness is configured without messaging. It stays in inbox
history as `launch-only`, but is not counted as a failure to retry and does not
make doctor report a problem. Resend with `--delivery inbox-only` for manual
reading, or attach. A new send to a stopped agent still wakes it when its harness
supports that; a failed wake remains retryable when consumption is ruled out.

A plain launch caused by an automatic fallback is temporary: fix its cause, then
resume with a working bridge and run `mv2 deliver NAME --retry`. The same applies
after enabling messaging for an agent originally launched without it. Once the
connection is ready, an explicit `--retry` rechecks even a refusal recorded as
final. For Codex, that retry probes the resumed bridge's exact thread before
reopening the refusal. If delivery is still unavailable, the refusal stays final; `--check` and
ordinary reads never reopen it. Older refusals that lack explicit final evidence are classified by the current harness
setting; accepted and uncertain deliveries are never reopened.

`--retry` resends only messages recorded as failed. It never resends an acceptance that remains unconfirmed or uncertain. Don't resend an uncertain message by hand, because the agent may already have it.

For Claude, a consumed message is reported failed and retryable only when Claude
explicitly drops it. When its row has not been stored after a few seconds while
idle, or by the end of a turn that started after it was taken, it is reported
not confirmed instead, and `--retry` leaves it alone. Claude holds a taken message
while a local command such as `/compact`, or a dialog, holds its turn, and stores
it afterwards, so a retry then would run it twice. Its row confirms it when it
comes. Nothing is resubmitted automatically. Before retrying a failed message,
mv2 checks that attempt's receipt again: a late confirmation prevents a resend,
and unreadable evidence keeps it from retrying. If this retry wakes a stopped agent, it first resumes the
bridge and then submits through the native queue with a fresh attempt ID. Each
actual submission has its own receipt ID, so an old failure cannot fail the new
attempt, and a late old row confirms only the attempt that produced it. History
keeps every attempt. If Claude is waiting at a resume-confirmation dialog, a
message sent meanwhile shows as not confirmed until you finish the dialog and
Claude stores it.

An accepted native queue entry is still eligible for `mv2 deliver NAME --check`
until its message is confirmed in the conversation. The check reports an entry
that is still queued without resending it. `--retry` selects recorded failures;
it never resends an accepted or uncertain submission. Checks normally cover the
current launch. Across a resume, Codex keeps checking saved queue receipts for
the same conversation; it does not search for other older acceptances. Harnesses
with transcript evidence, including Claude, keep older same-session acceptances
visible. Once the accepting process is
proved gone and the same conversation is ready and idle again, a check can use
its transcript to confirm delivery or prove that the message was lost when the
agent stopped. Unreadable or incomplete evidence stays uncertain. A proved loss
is reported once with the message text and advice to send a new message deliberately;
`--retry` does not resend it. If the harness restored a draft, it has not been
submitted. A proven loss emits `message.failed` for plugins. The final result
remains in inbox history. A message aimed outside the agent's recorded session
lineage is reported once as uncheckable, with its saved delivery evidence unchanged. It is never retried automatically.
A message that woke an agent is checked against that launch's own starting
prompt, with the harness's transcript evidence (Claude Code today), on each
`mv2 deliver --check` until it settles; listings and the dashboard never read it.
It is confirmed once its prompt is recorded. Until then `--check` prints
`NAME: message accepted; no confirmation yet. Do not resend.`, also while the
launch still runs without the prompt recorded, because the harness can still be
holding it (Claude's resume dialog, for example). When the evidence can't be read,
it prints `NAME: message accepted; cannot yet tell whether it arrived.` with what
to restore. Only when the launch was proven to have ended before a read that found
no prompt is it reported lost, once, like any other loss. Nothing is recorded
until it settles. A wake made before this check existed, or by a harness without
that evidence (Codex, Pi), is still left out of recurring checks. An uncertain or
failed wake keeps its existing evidence check. No wake prompt is ever resent
automatically.
Identical accepted messages need separate stored copies: each text row confirms
at most one acceptance, oldest first. Any excess acceptance stays unconfirmed;
an exact message-ID receipt confirms only that message.

`mv2 update` makes a one-time pass over older acceptances without receipts.
An exact stored message ID can confirm one; matching text alone cannot. Messages
without that proof and accepted wake prompts keep their previous state. The
pass never sends a message or reports one lost. See [updating](../use/updating.md)
for running the same pass after a manual checkout update.

## Focus an agent

An agent whose reply needs the user's answer focuses itself before it ends its turn. While it is focused, other agents' messages can't start new work and bury the question.

```sh
mv2 focus --turn       # until its next turn ends
mv2 focus --for 20m    # for a time such as 5m, 20m, 1h or 1h30m, at most 1d
mv2 focus              # until mv2 unfocus
mv2 unfocus            # end it, and say how many unread inbox messages wait
```

While an agent is focused:

- `steer` and `follow-up` messages to it are refused. Nothing is saved, `send` exits 1, and its result says to send `--delivery inbox-only` instead.
- `inbox-only` messages are saved as usual.
- `deliver --retry` to it is refused until it unfocuses. `deliver --check` still works.
- What you type in its terminal reaches it as usual: a focus holds back only mv2's messages.
- One exception for now: a `steer` or `follow-up` that was sent before the focus began, to an agent that was still starting, is delivered once the agent is up.

`unfocus` prints how many unread inbox messages wait, and how many of them arrived during the focus.

A turn focus ends when the agent's status next goes from working or waiting to idle:

- **An agent that focuses itself, or one you focus while it is working,** finishes its current turn first. The focus ends with the turn after it, which takes your answer.
- **One you focus while it is waiting on you or idle** has its focus end with the turn that takes your answer: for a permission prompt, that is the current turn.
- **Any turn end counts,** including a turn the harness starts by itself, for example when a background task finishes, and one that delivers a message accepted before the focus. While background work runs, focus with `--for` instead.

A turn focus needs a harness that reports its status over a live connection, so `focus --turn` is refused while the agent's status is `unknown`. Use `--for` then.

Inside an agent, `focus` and `unfocus` act on the agent itself, and an agent can't change another agent's focus. You can name any agent, as in `mv2 focus worker --for 1h`, or press `f` in the [dashboard](../reference/dashboard.md#message-focus-kill-and-resume). `mv2 list`, `mv2 inspect` and the agent's tmux bar show who is focused and until when.

## Read an inbox

Agents check their inbox at startup, between steps and before finishing. You can read any agent's inbox too.

```sh
mv2 inbox                              # inside an agent: its unread inbox-only messages
mv2 inbox --agent worker --peek        # read without marking them read
mv2 inbox --agent worker --all --last 20
mv2 inbox --agent worker --all --since 2h
mv2 inbox --wait --timeout 5m          # wait for the next inbox-only message
```

Plain `inbox` shows unread `inbox-only` messages and marks them read once printed. Messages delivered directly already reached the agent's conversation, so `inbox` doesn't repeat them. `--all` shows the full history, and `--json` adds the delivery details. When nothing matches, the line names the filter, such as `No messages since 2026-10-17T00:00:00Z.` or `No messages in the last 2h.`

An unreadable or damaged delivery ledger is reported with its full file path and
a repair/reset hint, including for `inbox --all` and `--peek`. Back it up before
repairing or resetting it. Check uncertain deliveries before retrying; resetting
bookkeeping is not evidence that the agent never received an older message.

`--wait` without `--timeout` waits indefinitely. When a timeout runs out, `inbox` prints a line and exits 1.

## Report progress

```sh
mv2 report --phase working --summary 'Checking the launch permissions'
mv2 report --phase done --summary 'Fix on branch fix-login, tests pass'
```

The phases are `starting`, `working`, `blocked` and `done`. The phase and summary show in `mv2 list` and in the dashboard. If the agent has unread inbox-only messages, `report` says how many, so the agent notices them. After `--phase done` it also tells the agent to read them now with `mv2 inbox` and address them before it finishes: done means its task is finished, so those messages are its next work.

## Inside Claude and Pi sessions

With a live connection, Claude Code and Pi have a `/multiverse` command you can type in the agent's terminal:

| Command | Does | Claude | Pi |
| --- | --- | --- | --- |
| `/multiverse` or `/multiverse list` | Show the catalog | yes | yes |
| `/multiverse NAME [args]` | Run that command, workflow or skill as your prompt | yes | yes |
| `/multiverse send AGENT TEXT` | Message another agent as this agent | yes | no |
| `/multiverse inbox` | Show this agent's unread inbox-only messages and mark them read | yes | no |

In Codex and launch-only sessions, ask the agent in words instead, such as "Please run /multiverse list". You can also run `mv2 catalog` yourself.

## Ask a side question

```sh
mv2 ask worker --question 'Which file holds the parser?'
```

`ask` prints the answer. It answers from the agent's own conversation and adds nothing to it. Only Claude Code with a live connection supports it. When there is no answer, `ask` prints why and exits 1. Right after a start, a resume or `/clear`, Claude can't answer until the agent finishes its next turn.

## Scripts

Every command on this page takes `--json` and then prints the full record. With several recipients, `send --json` returns `recipients` and an overall `ok`.

If inbox-state.json is damaged, send still saves the new message, names the file,
and makes no native submission. Repair or reset that file before retrying.
Only messages with saved proof that submission never began become retryable;
resetting delivery state does not make older accepted or uncertain messages safe
to resend. Doctor names damaged delivery state and incomplete inbox tails.
Back up an incomplete log before repairing its final line; complete messages
remain readable and are never overwritten.

A wake launch that exits during startup becomes a definite failure only when
the harness's exact conversation record shows no consumption of its starting
prompt. The result says it was saved for explicit retry. If the evidence was
unreadable, restore access to the named storage path and run `mv2 deliver NAME --check`.
It rechecks the original launch's conversation, prompt and start time without
sending anything. Recorded consumption confirms the message; proven absence
makes it retryable. Missing original launch facts or unreadable records keep it
uncertain and explain what to repair. Harnesses without this check stay uncertain.

A send from a legacy launch must carry that agent's exact current launch ID,
just as report does. An outdated launch is refused before any message is saved.
Declared stable conversation identities retain their existing native-session
and storage proof; they do not acquire a replacement terminal's launch ID.
