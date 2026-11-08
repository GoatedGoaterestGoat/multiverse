# Waiting prompts

When an agent's status is `waiting`, it is blocked on a prompt in its terminal: a permission prompt, a question it
asked, a plan to approve, or an MCP server's form. `mv2 answer` shows that prompt with the choices the harness
offers, and answers it through the harness's own answer path, so the agent gets what it would get from the person
in its terminal.

```text
mv2 answer NAME                                   # the open prompts: ID, kind, the call, the choices
mv2 answer NAME ID --choice allow [--message TEXT]
mv2 answer NAME ID --answer "QUESTION=LABEL" ...  # one per question; LABEL may be typed text where allowed
mv2 answer NAME ID --answer "QUESTION=LABEL" --note "QUESTION=TEXT"   # a note where the question takes one
mv2 answer NAME ID --stdin                        # the answer as JSON on standard input
mv2 answer NAME --result ID                       # an earlier answer's outcome, as it stands now
```

Plugins use `agent.waiting_prompts()`, `agent.answer_prompt()` and `agent.prompt_result()`.

## Who, and when

- **The person or a plugin.** The person answers from a terminal that isn't an agent's, or from the dashboard. A
  plugin reads at observe and answers at act, the level of messaging. Agents can't read or answer prompts: an
  approval carries the person's authority. As with settings commands, this guards against mistakes, since an
  agent's shell carries its identity.
- **Only now.** Only an agent whose status is `waiting`, and only a prompt open at that moment. Nothing is answered
  ahead of time or held for a later prompt.
- **Every grant the harness offers**: once, for the session, always. A choice that saves a rule to the person's
  settings runs only when the person answers, and its outcome says it saved one. A plugin gets the others.
- **Focus doesn't block an answer.** Answering is attention to the agent; it buries nothing.
- In auto mode, the classifier deciding before any prompt opens is intended.

## The prompt

A read returns every prompt open now, each with an opaque ID. Claude can show several at once (three parallel reads
ask "1 of 3"); the questions of one prompt share its ID. The ID names one native request in one launch and one
conversation, so a later prompt, a new launch or a switched conversation never matches it.

Each prompt gives its kind (`permission`, `question`, `plan`, `form`), the tool and a one-line summary, the call's
details (bounded; the transcript keeps them whole), and either its choices or its questions. A permission's choices
are the decisions the harness offers, in its order, with mv2's own labels: mv2 doesn't claim to repeat the
terminal's words. A question has its options, whether it takes several, and whether typed text is accepted.
A prompt mv2 can't answer is listed with the reason, ending "answer it in the terminal".

A read's coverage is `complete`, or `unknown` with the reason when the harness reports the agent waiting but mv2
knows no prompt for it, such as a prompt the harness keeps from plugins.

## The answer

Core and the adapter check before anything is sent: the caller may answer, the agent is `waiting`, the prompt is open
in a fresh read and answerable, the choice is offered (and a rule-saving one comes from the person), and every
question is answered with existing labels, one unless it takes several, typed text only where allowed. A refused
answer sends nothing. A valid one is written once, exclusively, so a second answer to the same prompt is refused.

A question can take a note beside its answer (Codex's questions). An offered label may carry one; typed text
plus a note is refused as ambiguous; a note alone answers only a question with no offered labels; a choice
alone, such as decline, carries none. A note for a secret question comes only through standard input or the
dashboard, as a secret answer does, and mv2 never records a note it sent.

| Outcome | Meaning |
| --- | --- |
| `answered` | mv2's answer took. `saved_rule` when it chose a decision that saves a rule: the harness writes it, and may warn if it can't. |
| `person` | The person answered first; `person_answer` holds their choice, labels and typed answers when the harness shows them: never a secret, never a refusal's message. |
| `sent_first` | mv2's answer went first from the agent's terminal and nothing seen contradicts it, but the harness doesn't confirm which answer it took (Codex). Counted as success. |
| `gone` | The prompt closed another way: Esc, the turn ended or was interrupted, the agent stopped. |
| `submitted` | Sent; the outcome isn't known yet. Read it with `--result`. |
| `unknown` | Sent; mv2 can't tell which answer took. |
| `refused` | Nothing was sent; the reason says why and what to do. |

**Saying why.** An outcome other than `answered` says why, as far as mv2 can tell (the user, 2026-11-07). A known
cause is stated as fact, with a `cause` the dashboard and plugins can read: `person` (answered in the terminal),
`replaced` (the agent now waits on another prompt; its ID is given), `interrupted` (Esc, or the turn was
interrupted), `turn_ended`, `stopped` (the launch ended), `switched` (the conversation changed), and for a refusal
`not_waiting`, `not_offered`, `person_only` and the like. When mv2 can't tell, the text says what is unconfirmed and
gives the likely explanations as possibilities, never as findings. It always keeps "nothing was sent" apart from
"sent, possibly taken", and never suggests sending a sent answer again.

**A later observation.** `sent_first` ends the wait, but a later `--result` reports what the harness observed
since: if an effect contradicting mv2's decision shows up afterwards, the outcome becomes `unknown` with cause
`contradicted`. An interruption, turn end, conversation switch or relay loss before a corresponding item effect
also makes it `unknown`, with that cause. Resolution alone cannot exclude cancellation. A later unrelated event
does not erase an already observed effect. Nothing is ever sent again.

**Stale answers.** An answer names its prompt by the ID from a read, never "the agent's current prompt". If that
prompt has closed or been replaced, the answer is `gone` (or `person`), even while the agent waits on a newer
prompt: it is never applied to the newer one or retried against it. A fresh `waiting` status only makes an agent
eligible; the prompt's identity is what keeps a late answer off the next prompt.

A sent answer is never sent again: the wait and `--result` only read. After the agent stops, an answer the
in-session part never took is `gone`, definitely not applied; one it took without writing an outcome is `unknown`. Each answer is recorded in the agent's folder
with who answered (`person` or `plugin NAME`), the prompt's kind and tool, the choice or the labels chosen, and the
outcome. Events carry only the kind, tool, caller and outcome: no command, path or answer text.

## The adapter's part

`waiting_prompts` (observe) returns the open prompts and the coverage. `answer_prompt` (act) checks an answer
again and has core write it. `prompt_outcome` (observe) reads the outcome and never sends. The `answer` capability
says whether a launch supports them, or why not.

Both harnesses work the same way. The part of mv2 inside the session publishes each open prompt as a file in the
agent's folder. Core writes the answer (`prompts/ID.answer.json`) exclusively, readable only by the person's
account, and records the answer beside it before anything is sent. The in-session part, still holding the prompt,
takes the answer, checks it again (a rule-saving choice only when core marked it the person's), answers, removes the
file and writes the outcome. It removes the prompt's own file when the prompt closes, and never applies an answer to
any prompt but the one it names.

### Claude

The mod publishes a prompt from `classic.PermissionRequest` and waits for its answer. The hook's input names no
call, so the mod pairs the prompt with the asked call whose tool and exact input it carries; Claude's prompts can
arrive in any order (three parallel reads, say). When no asked call matches, or two identical calls match, mv2 can't
tell which call the prompt is for: it is listed, to be answered in the terminal, and never held. The wait doesn't
count against the hook's budget, leaves nothing on screen if the person presses Esc (Claude shows only its own
"Interrupted"), and stops the moment the call resolves: the tool ran or was denied, or the turn ended. Claude shows its
dialog and runs the hook at once, and whichever answers first wins.

The mod's answer is the hook's decision. A permission offers `allow`, `allow_session` (one per session-scoped
suggestion Claude makes), `allow_always` (a suggestion saved to the person's user, project or local settings, so for
the person only) and `deny` with a message. A question takes its answers, several labels joined by ", ". There is no
"deny and stop": Claude stores a hook's interrupting deny like the person's No, so mv2 couldn't tell whose answer
took. Claude's screen and transcript read "Allowed by PermissionRequest hook", and mv2's deny lets the turn go on,
where a person's No stops it. Both are accepted.

The outcome comes from what Claude stores: its row for a hook's decision, when that decision is the one mv2 sent, or
the person's choice on the call's result. Rows that disagree (another decision, two for one call, a hook's and the
person's both, another conversation's row) leave it unknown, with cause `contradicted`. The mod's events don't carry those rows, so while a prompt is open the adapter reads
only the mod's small files; once the prompt has ended, it reads the call's rows from that
conversation's own transcript in the launch's recorded storage, from where it ended when the prompt opened, at most 16 MiB, matching the exact tool call. Esc on a permission is stored like the person's No, so it is `person` with choice
`deny`; `gone` with cause `interrupted` is a turn aborted with nothing stored. Claude hides from hooks that an ask
lets hooks only deny (auto mode's fallback, an ask a plugin raised) and silently ignores an allow there, so an allow
with no decision stored within 5 seconds is `unknown` (cause `not_taken`), saying the prompt may still wait in the
terminal.

The person's own `PermissionRequest` hooks run first; the mod holds only a prompt they leave undecided. It never
holds an ask that has no dialog, such as a background subagent's: Claude denies those unless a hook answers, and
holding one would block the subagent. A subagent's ask is listed, to be answered in the terminal: a foreground subagent's ask and a background one's
reach the hook alike, and nothing says whether Claude drew a dialog for it. Two questions
with the same text in one call can't be told apart, so that prompt is answered in the terminal too.

A plan to approve (`ExitPlanMode`) is answered the same way: approve, which leaves plan mode with edits still asked
for, as the person's "Yes, manually approve edits"; or keep planning, with a message saying what to change. Claude
doesn't offer a hook its "use auto mode" choice, so mv2 doesn't either.

MCP forms are listed while open, to be answered in the terminal: Claude runs `classic.Elicitation` before it draws
the form, and holding it to wait for mv2 would hide the form from the person.

The path is checked live at each Claude Code version check. A version newer than the last check lists its prompts
but doesn't answer them until it is checked.

### Codex

Only a launch on the verified bridge, with the Codex CLI and daemon at a checked version, and only the conversation
the launch is bound to. A `/btw` side conversation's prompts and a review's are listed, to be answered in the
terminal, and so is every prompt while the relay can't yet tell the conversation's mode (it joined in the middle of
a turn), until a turn starts that it saw begin. Another thread's requests, such as a subagent's, aren't listed; while
the agent waits on one, the read's coverage is unknown.

The bridge's relay already carries the conversation's requests (command, file change and permission approvals, the
agent's questions, MCP forms) and the terminal's replies. It publishes each open request with a handle for this
launch and conversation, the decisions the terminal offers in its order, and for a file change the exact diff from
its item, or lists the request as not answerable without it. It closes a request on the terminal's reply (keeping
the person's choice, never a secret), on Codex's notice that the request is resolved, when the turn ends, or when the
launch or conversation changes, and keeps a short record of how each closed.

mv2's answer is Codex's own typed reply to that request, checked twice against what the request offers, since a
malformed first reply would use the request up. It goes up the terminal's own connection, through the one gate the
terminal's replies pass, never another connection to the daemon and never a keystroke, steer or prompt. So the relay
knows which reply went first:

- **The terminal's:** `person`, with the choice the terminal sent; mv2's answer is never sent.
- **mv2's:** Codex says neither whose reply it took nor that it took one: another client attached to Codex outside
  mv2, or a cancellation, can use the request up, and no effect afterwards tells them apart (allowing once and for
  the session start the same item; an identical reply looks the same). So the outcome is `sent_first`: mv2's answer
  went first from the agent's terminal, the request was still open, and nothing seen contradicts it. Its reason names
  what was seen (the command ran, the file change applied) and that Codex doesn't confirm mv2's choice, or its scope,
  was the one taken. A cancellation, a lost connection, no resolution, or an effect contradicting mv2's decision is
  `unknown`. A contradiction, or cancellation before a corresponding item effect, seen after the wait ended
  turns a later `--result` into `unknown` with the observed cause.

A late terminal reply after mv2's is dropped. A choice that saves a rule (an execution or network policy amendment,
whether it allows or denies, or an MCP approval's "always allow") is the person's only; "for this session" is not
saved. Only the terminal's options are offered: not an amendment the terminal hides, nor a file change's decline.
MCP forms are answerable where the terminal could fill them in; URL and device-verification requests, and number or
several-choice fields, are listed, to be answered in the terminal. A question's note is sent as Codex encodes it.

A secret answer or note lives only in the answer file until the relay sends it, then the file is removed; mv2's
records, events and logs never hold it. Codex itself hands the answer to the model and may log it, as when the
person types it.

### Pi

Not available: `answer` reports it unavailable.
