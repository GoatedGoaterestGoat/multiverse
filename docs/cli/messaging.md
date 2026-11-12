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

When a recipient is missing here but exists in a [related universe](agents.md#find-and-refer-to-agents), its error names the universe and suggests `-u PATH` before `send`. Several matches are listed without selecting one; nothing is sent to them until you choose the universe explicitly.

If the sender's record is unreadable or damaged, the sender line keeps the message's saved name, or `user` if none was saved. An unreadable or damaged sender universe uses its saved path in that line. The message stays readable and deliverable.

## Choose a delivery

```sh
mv2 send worker --delivery follow-up --text 'When you finish, also update the changelog.'
mv2 send worker --delivery inbox-only --text 'FYI: old meeting notes are archived.'
```

| Delivery | Use it for | Arrives |
| --- | --- | --- |
| `steer` (default) | Urgent messages relevant to the current work | At the next safe point in the current turn. If the agent is idle, it starts a turn |
| `follow-up` | Tasks, needed replies and results the recipient is waiting for | After the current turn ends |
| `inbox-only` | FYIs requiring no action and no reply your work depends on | When the agent runs `mv2 inbox`, unless upgraded to follow-up as described below |

Choose delivery explicitly. Use `inbox-only` only when both hold: the recipient needn't act on the message, and none of your work waits on a reply, even if it reads its inbox much later. Otherwise use `follow-up`, or `steer` when urgent and immediately relevant to the recipient's current work. Keep asks short: the question plus only what is needed to answer. Put background and side notes in your notes, or inbox-only if both tests pass.

A follow-up reply arrives as a prompt, not in the inbox. Await it by ending your turn, rather than with `mv2 inbox --wait`. Report status through `mv2 report`.

When you send as an agent, read a recipient's queued follow-ups before messaging them. If that recipient's follow-ups are still queued in your harness, `send` refuses your steer or follow-up before saving it. End turns to receive queued messages; a backlog may take several turns to reach that recipient's messages. Use `--force` to send anyway. With several recipients, only those whose follow-ups are queued are refused; the others get the message, each with its own result line. JSON includes each recipient's result and lists the refused names in `blocked_recipients`, both in their result and at the top level. Renaming an agent does not change the match. Queued steers do not block a send, even if the harness puts them in its after-turn queue. Inbox-only and `--queue-only` sends are never refused by this check. Sends by a person or plugin are not checked.

With `--force`, an agent's send still reads its queue once, including for inbox-only sends. If a complete report shows no queued follow-ups from a recipient, a note says you don't need `--force`. An unknown queue gets no such note and the forced send proceeds. JSON includes the reminder as `force_note` for that recipient.

The check reads only mv2's own queued messages, never typed prompts or another client's input. If the harness cannot report its queue, or the read takes longer than 2 s, the send proceeds. It adds a note only for recipients whose follow-ups to you remain unconfirmed in this conversation, or when that delivery history cannot be read. Messages retained across a resume count when their saved delivery records prove the same conversation and storage. No note appears when the ledger rules out such a follow-up. A listed follow-up still counts when coverage is incomplete. A message absent from the queue is not proof that it was delivered; use `deliver --check` for delivery receipts.

Every admitted message is saved to the recipient's inbox before mv2 tries to deliver it, so nothing is lost when delivery fails. A `steer` or `follow-up` message to a [focused](#focus-an-agent) agent is saved as an `inbox-only` message instead. A `steer` or `follow-up` message to a stopped or dormant agent resumes it (with a harness that supports waking: Claude Code, Codex and Pi; a custom command harness doesn't), with the message as its first prompt. An `inbox-only` send wakes an agent only if it upgrades to a follow-up as described below.

If a wake is refused, `send` and `deliver --check` show the complete reason, including the exact record paths and recovery instructions. Reflected message text remains hidden.

If a name or UUID prefix matches several agents, the error lists their names
and full UUIDs. Use one full UUID; nothing is saved to an ambiguous recipient.

A running agent without a live connection can't receive `steer` or `follow-up` messages. Inbox-only still requires both tests above; if action or a needed reply cannot wait, attach to its terminal instead. See [live connection and launch-only](../concepts.md#live-connection-and-launch-only).

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
| `NAME is currently focused. Your message has been delivered as an inbox message instead.` | The agent is [focused](#focus-an-agent), so the message was saved to its inbox | Nothing. Don't resend it |

A delivered message has not necessarily been read or answered. Wait for a reply, or check the agent's phase with `mv2 list`.

When an agent sends a message and has unread inbox messages from that recipient, its output ends with `Reminder: you have unread messages from NAME.` This applies to every delivery mode, including saved messages whose native delivery fails, and to each matching recipient in a batch. The check peeks before sending and leaves messages unread; like `inbox --peek`, it uses complete rows before an unfinished tail. A busy or unreadable inbox skips the reminder.

If the recipient reports that it is blocked on the sending agent, an inbox-only send becomes a follow-up. The sender's output says why before its usual delivery result. The recipient gets the original message text under the usual sender header. Update your reported status when that dependency is resolved. This applies to `--queue-only` too; explicit steer and follow-up sends keep their timing. Each recipient in a batch is checked separately. A focused recipient still holds the upgraded message in its inbox as an intended follow-up: `mv2 held` reads it after focus ends, and the focus-end note counts it. The sender's output says that the follow-up was scheduled and held, rather than delivered natively; the held message keeps its original text. Reading it never submits it again. If the recipient is launch-only and cannot take a follow-up now, or its follow-up availability cannot be checked, the send stays inbox-only and says so; it remains available to `mv2 inbox`. A stopped agent that supports waking still resumes with the follow-up. If the follow-up is definitely refused before delivery, the original text stays readable in `mv2 inbox` and cannot be retried natively. This also applies when startup fails after send returned. The send output says it tried a follow-up and saved the message in the inbox, without claiming delivery. If delivery is not confirmed, the original is also readable in the inbox with a note that it may also have arrived as a follow-up. Queue acceptance, a pending wake and an unknown result are not delivery confirmation: keeping the input readable takes priority over avoiding a possible duplicate between the conversation and inbox. The native attempt is not sent again. A confirmed delivery needs no unread inbox copy; `mv2 deliver AGENT --check` can record that confirmation. This fallback applies only to blocked-on upgrades, not explicit steer or follow-up sends.

The match follows the recipient's agent ID across renames and moves. People, plugin senders and `--json` output get no reminder.

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
Codex checks up to 100 queued entries and the latest 20 full turns, with a 64 MiB
limit per reply. Large tool output within that limit does not prevent receipt
confirmation. A missing receipt, larger reply or transport failure leaves
consumption unconfirmed; checking never resends input or loads an old conversation.
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

An agent whose reply needs the user's answer focuses itself before it ends its turn. While it is focused, new messages from other agents can't start new work and bury the question.

```sh
mv2 focus --turn              # until its next turn ends
mv2 focus                     # until mv2 unfocus
mv2 focus worker --for 20m    # the user only: for a time such as 5m, 20m, 1h or 1h30m, at most 1d
mv2 unfocus                   # end it, and say what waits in the inbox
```

While an agent is focused:

- `steer` and `follow-up` messages to it are saved as `inbox-only` messages, and the sender is told so. Nothing reaches its harness.
- `inbox-only` messages are saved as usual.
- `listen` is refused, and a `listen` already running [reports nothing until the focus ends](#listen-for-quiet-messages).
- `deliver --retry` to it is refused until it unfocuses. `deliver --check` still works.
- What you type in its terminal reaches it as usual: a focus holds back only mv2's messages.
- A `steer` or `follow-up` sent before the focus began is still delivered if the harness had already accepted it, or if it waited for the agent to finish starting. The turn it starts counts toward a `--turn` focus.

`mv2 focus` says when the agent's harness already holds follow-ups from mv2, which the focus won't stop. It reads them the way [send does](../design/queued-prompts.md), counting follow-ups only. With `--turn`, it also says that the turn they start will likely end the focus before the user answers, and suggests a focus until `mv2 unfocus` instead. When mv2 can't read the queue and a follow-up may be queued, it says it couldn't check, and why. The dashboard's `f` doesn't check.

Only the user can focus an agent for a time: inside an agent, `focus --for` is refused. `unfocus` prints how many unread inbox messages wait, and how many of them arrived during the focus.

A turn focus ends when the agent's status next goes to idle from working, or from waiting that began inside a turn. A command's own dialog you open while the agent is idle, such as Claude Code's `/model` picker, shows `waiting` but isn't a turn, so closing it doesn't count:

- **An agent that focuses itself, or one you focus while it is working,** finishes its current turn first. The focus ends with the turn after it, which takes your answer.
- **One you focus while it is waiting on you or idle** has its focus end with the turn that takes your answer: for a permission prompt, that is the current turn.
- **Any turn end counts,** including a turn the harness starts by itself, for example when a background task finishes, and one that delivers a message accepted before the focus. While background work runs, focus until `mv2 unfocus` instead.

A turn focus needs a harness that reports its status over a live connection, so `focus --turn` is refused while the agent's status is `unknown`. Focus until `mv2 unfocus` then.

Inside an agent, `focus` and `unfocus` act on the agent itself, and an agent can't change another agent's focus. You can name any agent, as in `mv2 focus worker --for 1h`, or press `f` in the [dashboard](../reference/dashboard.md#message-focus-kill-and-resume). `mv2 list`, `mv2 inspect` and the agent's tmux bar show who is focused and until when.

## Read held messages

```sh
mv2 held
```

Inside an agent session, `held` shows the unread `steer` and `follow-up` messages that the agent's last focus held, however that focus ended, and marks only those read. Other unread messages stay unread for `mv2 inbox`. `held` is refused while the agent is focused.

A held message's header says what it was sent as, such as `Message from lead (steer, held while you were focused):`. Plain `mv2 inbox` shows held messages too, so after `mv2 inbox`, `held` has nothing left to show.

When a focus ends with held messages unread, the agent is told in these cases:

| The focus ends | The agent is told |
| --- | --- |
| The agent runs `mv2 unfocus` | In its output: `You received 2 steer or follow-up messages during your focus. Use mv2 held to check only those messages.` |
| A `--turn` focus ends with a turn, whoever set it | The same text, in a `follow-up` from mv2 that starts a turn. A `listen` that is running reports instead |

A timed focus that runs out, and a focus the user ends, tell the agent nothing. The held messages still wait for `held` and `inbox`.

## Read an inbox

Agents check their inbox at startup, between steps and before finishing. You can read any agent's inbox too.

```sh
mv2 inbox                              # inside an agent: its unread inbox-only messages
mv2 inbox --agent worker --peek        # read without marking them read
mv2 inbox --agent worker --all --last 20
mv2 inbox --agent worker --all --since 2h
mv2 inbox --wait --timeout 5m          # wait for the next inbox-only message
```

Plain `inbox` shows unread `inbox-only` messages, including [held](#read-held-messages) ones and the original input of unconfirmed blocked-on upgrades, and marks them read once printed. Ordinary explicit steer and follow-up messages are not repeated there. A blocked-on upgrade can appear in both the conversation and inbox when delivery has not been confirmed. `--all` shows the full history, and `--json` adds the delivery details. When nothing matches, the line names the filter, such as `No messages since 2026-10-17T00:00:00Z.` or `No messages in the last 2h.`

An unreadable or damaged delivery ledger is reported with its full file path and
a repair/reset hint, including for `inbox --all` and `--peek`. Back it up before
repairing or resetting it. Check uncertain deliveries before retrying; resetting
bookkeeping is not evidence that the agent never received an older message.

`--wait` without `--timeout` waits indefinitely. When a timeout runs out, `inbox` prints a line and exits 1.

## Listen for quiet messages

When you have finished your work and are waiting, run `"$MV2_CLI" listen` using your harness's background-shell tool. Stop that shell if new work arrives meanwhile. After reading and handling the messages, start it again when you are waiting.

Inside an agent session, `mv2 listen` waits for unread inbox-only messages without marking them read. It exits immediately if any are already waiting. Ordinary steers and follow-ups do not end the wait; the unread inbox copy of a blocked-on upgrade does.

On success it prints `inbox message from NAME`, or `N unread inbox messages` when several wait, and exits 0. Run `mv2 inbox` to read them. It waits without a time limit unless you add `--timeout 30s` or another duration. Timeout prints one line and exits 1.

While you are [focused](#focus-an-agent), `listen` is refused. To listen while you wait for the user, start it before `mv2 focus`. It then reports nothing until the focus ends, and a `--timeout` that runs out meanwhile waits for the end too. If anything is unread then, it prints a line such as `3 unread inbox messages (including 2 steer/follow-up messages held during focus mode)` and exits. Otherwise it keeps waiting. A listener that reports this way replaces the follow-up note a `--turn` focus would send.

The listener belongs to the launch in which it started, before waiting for any lock. It exits nonzero with a reason when that launch ends or is replaced, or its identity or process cannot be verified, focused or not. It does not attach to a replacement launch, including when a shared conversation keeps its identity. Busy launch or inbox locks do not suspend the timeout or launch checks. Before reporting unread messages, it checks the launch's processes again.

The harness decides how to notify you when the background shell exits. Claude starts a turn for its background-task notification. Codex shows command completion but does not start a new turn after the current turn ends; check the result and inbox at your next turn. mv2 does not send a separate wake message.

## Report progress

```sh
mv2 report --phase working --summary 'Checking the launch permissions'
mv2 report --phase done --summary 'Fix on branch fix-login, tests pass'
```

The phases are `starting`, `working`, `blocked` and `done`. The phase and summary show in `mv2 list` and in the dashboard. Only a report whose phase is `done` includes the unread inbox count, for example `Reported done. You have 2 unread inbox messages.` Other reports say the phase, such as `Reported working.` or `Reported blocked on helper.`, with the guidance below when it applies. Counting never marks messages read.

Every accepted report records its time in `reported_at`, including a report of the same phase or one that updates only the summary. It measures how current the reported work is, rather than how long the agent has been in that phase. Unrelated record changes do not reset it. Older agents gain the timestamp with their next report; `mv2 list --json --columns name,reported_at` reads it.

Name one dependency when reporting blocked:

```sh
mv2 report --phase blocked --blocked-on reviewer --summary 'Waiting for the review'
mv2 report --phase blocked --blocked-on qa-claude --blocked-on-universe qa
mv2 report --phase blocked --blocked-on user --summary 'Waiting for a decision'
mv2 report --phase blocked --blocked-on self --summary 'Waiting for my background shell'
```

An agent target accepts the same name, full ID or unique ID prefix as `send`. Use `--blocked-on-universe` for another universe or a subverse path; without it, the target is in the reporter's universe. `user` and `self` are special sources. To name an agent literally called user or self, use `agent:user` or `agent:self`, or its ID. An unknown or ambiguous target refuses the report without changing the previous status. Reporting blocked on your own agent is the same as `self`.

If the target is missing here but found in a related universe, the error suggests `--blocked-on-universe PATH` for each match. This keeps your report in its own universe while naming the dependency elsewhere.

The dependency follows the agent's ID, so renaming it or reusing its old name cannot redirect a follow-up. The display finds moves within the known universe trees; a target that cannot be found shows its saved name with `gone`. For a move outside those trees, report the target's new universe to update its displayed address. Incoming messages still match by ID. A new `--phase` report clears the previous dependency unless it also supplies `--blocked-on`, including another plain `--phase blocked`. Updating only the summary keeps it.

When an agent reports and has unread inbox messages from its blocked-on agent, its output ends with `Reminder: you have unread messages from NAME.` This is the same nonblocking peek as send's reminder: it never marks anything read, and a busy or damaged inbox skips it.

A blocked report also gives guidance for its dependency:

- With `--blocked-on user`, an agent that is not focused is encouraged to enable focus while waiting for the user's decision. A current focus suppresses this note.
- With `--blocked-on self`, a summary without the word `ETA` is encouraged to give the process's ETA. Matching is case-insensitive, so `eta: 5m` counts and `beta build` does not. Updating only the summary uses the new text for this check.
- Without a dependency, the report encourages naming one with `--blocked-on`: an agent, `user`, or `self`.

Text `mv2 list` always starts a blocked agent's summary with what it's waiting for, such as `Waiting for reviewer: needs the API diff`, and its Phase says how long ago it last reported, such as `blocked (50m)` or `working (10m)`; `--json` includes `blocked_on` and `reported_at` as fields. In the dashboard, Phase shows the age, and the `blocked_on` view option adds the dependency to the summary, in its own colour, without adding a column. It doesn't show when Summary is hidden. Both bundled templates turn `blocked_on` on.

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

To run the harness's own commands, such as `/compact` or `/model`, use [mv2 slash](harness-commands.md). To answer the permission prompt or question a `waiting` agent is stopped at, use [mv2 answer](waiting-prompts.md).

```sh
mv2 ask worker --question 'Which file holds the parser?'
```

`ask` prints the answer. It answers from the agent's own conversation and adds nothing to it. When there is no answer, `ask` prints why and exits 1.

Claude Code needs a live connection. Right after a start, a resume or `/clear`, Claude can't answer until the agent finishes its next turn.

Codex answers from a private copy, with or without a live connection. See [Codex's requirements](../use/harnesses.md#codexs-questions-and-one-shot-runs) for supported releases, models and logins.

## Scripts

Except for `listen`, commands on this page take `--json` and then print the full record. With several recipients, `send --json` returns `recipients` and an overall `ok`.

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
