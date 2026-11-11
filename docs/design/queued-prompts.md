# Queued prompts

A harness can report which of mv2's messages it holds for an agent right now: messages mv2 handed over that are
waiting for the agent's turn to end, not yet in its conversation. mv2 uses that report for one thing so far: an agent
reads the follow-ups another agent sent it before writing back.

## Read the follow-ups first

When agent A sends agent B a steer or a follow-up while B's follow-ups to A are still queued in A's harness, mv2
refuses the send:

> Your message to B is not delivered because you have unread messages from B queued as follow-ups. You will see them
> once you end this turn. It is recommended that you read B's messages first before messaging them. If you still want
> to message B, resend with --force.

Nothing is saved or sent. `--force` sends anyway. Follow-ups matter more than inbox messages and may change what A
does, so A reads them first; the reminder about unread inbox messages from B is unchanged.

The user's decisions (2026-11-10):

- **Follow-ups only.** B's queued steers don't count: a steer reaches A at its next tool result, which is the send
  command's own, so A sees it right after sending anyway. A steer that Codex holds in its after-turn queue, because A
  was idle when it was sent, is still a steer.
- **Steer and follow-up sends only.** An inbox-only send, `--queue-only` included, is never refused. Neither is a
  person's send or a plugin's: only an agent's.
- **An unknown queue doesn't block.** When mv2 can't tell what A's harness holds (a plain launch, a Codex agent
  without a verified bridge, a failed or slow read), the send goes ahead. If mv2's own records hold a follow-up from B
  to A that isn't confirmed yet in this conversation, or mv2 can't read its records, a line says mv2
  couldn't check whether B's follow-ups are still queued, and why; with none, nothing of B's can be queued, and
  there's no line. The refusal is an aid, not a guard.
- **Only mv2's messages are reported.** Never the person's typed prompts, another client's input or the harness's own
  notices.
- **Nothing agent-facing changes.** No skill or bootstrap describes it; agents learn it from the refusal itself.

For a send to several agents, each recipient gets its own outcome, as with any send to several: one whose
follow-ups to A are still queued is refused with that sentence, and the others get the message. A resend to the
refused ones with `--force` then reaches only them. The check reads mv2's records of A's messages first, then A's
harness, once, before anything is saved and within the two-second bound, and changes nothing: not A's queue, not
any receipt, not any read mark.

## The report

`runtime.queued_prompts.report(root, agent)` returns a `QueueReport`: a state (`ok`, or `unavailable` with the
reason), a coverage, the time it was read, and the entries in the harness's order. Each entry names one of mv2's
messages and, where the harness has one, its own queue entry.

- **Coverage** is `complete` when every mv2 message the harness can enumerate for this launch and conversation is
  listed, and `unknown` with the reason otherwise. Each harness says what it can enumerate.
- **Presence is evidence; absence isn't.** A listed message was queued when read. A message no longer listed may
  have entered the conversation, been dropped or be in transit, and the report never says which: delivery receipts
  keep that job.
- **mv2's records decide what an entry is.** Core keeps only entries whose ID is one of mv2's messages to this agent
  in the current conversation (its native ID, storage and workspace), from this launch or, since a harness queue can
  outlive a resume, an earlier one; it takes each one's sender and mode from its own record of that message, never
  from the harness or from text. An earlier launch's message counts only when its saved delivery record proves the
  same conversation, storage and workspace; one of mv2's IDs without that proof makes the report `unknown` instead
  of refusing. An unknown ID is dropped, never guessed.
- **Read-only and bounded.** Reading never starts, resumes, reorders or deletes anything. The whole read, connection
  included, gives up after two seconds and reports `unknown`.

## The adapter's part

`queued_prompts(session, executor)` (observe) returns `NativeResult('ok', value={'entries': [...], 'coverage': ...,
'reason': ..., 'observed_at': ...})`, or `unavailable` with the reason. Capability `queued` says whether a launch
supports it.

### Claude

mv2's Claude plugin already tracks every mv2 message Claude has taken but not yet stored. It writes them to
`claude-queue.json` in the agent's folder, with the launch and session they belong to, rewriting it when Claude takes
a message and when its delivery receipt or not-delivered record is written. It is complete from a launch's first
load; after the plugin reloads it is `unknown` until the agent has been idle, since the reloaded plugin can't know
what the one before it handed over. A message Claude took but didn't store in time (during `/compact` or a dialog,
say) isn't listed, since it may never come, but it keeps the report `unknown` until the agent has settled idle after
a later turn. The adapter reads it only for the current launch and conversation; a file from
another launch or conversation, or a plugin that doesn't write one, is `unknown`. The person's typed prompts are
never written.

### Codex

Not available yet: the report is `unavailable`, so sends go ahead, with the note only where the rule above calls for
it (an unconfirmed follow-up from the recipient, or records mv2 can't read).

### Pi

Not available: `unavailable`.
