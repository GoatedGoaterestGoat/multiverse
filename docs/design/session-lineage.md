# Native session changes

An mv2 agent keeps its own identity when its native conversation changes. The
current native ID remains in `properties.json`. A verified change appends a
transition to the agent's `sessions.jsonl`; the first assigned or bound session
has no transition and is generation zero.

This is an optional adapter contract. Existing adapters keep their exact
conversation behavior unless they implement `observed_binding`. Codex's
`/new`, `/clear` and `/resume`, typed in a running bridge terminal, are followed
through the relay (see [Codex conversation switches](#codex-conversation-switches)).

## Provider observations

`SessionHandle.home` is the canonical native storage home recorded from the
launch's environment, when the adapter implements `session_home(environment)`.
`SessionHandle.pid` is the child PID from the same runtime snapshot;
`SessionHandle.pid_started` is its start time captured by the runner immediately
after starting that unreaped child. These fields are optional and appended to
the handle; older providers need no changes. The shared
`runtime.process.process_started(pid)` helper returns a UTC ctime string with
whitespace collapsed, or `None` when unknown.

An adapter can implement `observed_binding(session) -> ObservedBinding | None`.
An observation contains `native_id`, `home`, and a short `evidence` description.
It must identify the exact launch process: its saved start, current process
start and native record's start must agree where PIDs can be reused. Comparing
only a native record with the current process cannot distinguish a reused PID.
`execve` preserves the original PID and start. Launches recorded before the
start field existed have unknown process identity; adapters must not adopt one
from the current process and should advise restarting when healing needs it.
Missing, malformed, or uncertain evidence returns
`None`. Searching native history or selecting the newest transcript is not
evidence. Core does not parse provider process files or probe conversations.

Core reconciles observations before native delivery, message target capture,
side questions, usage reads, listing and resume. An observed change uses reason
`detected`. A launch-guarded integration callback can provide a more precise
reason such as `clear`. Resume rereads the binding after taking its launch lock,
so an earlier clear cannot be overwritten by a stale resume snapshot. A UI
measurement that needs a change does not wait behind a busy launch lock: it
reports the identity as temporarily unavailable instead of reading the old
conversation.

The adapter may also implement `connection_problem(session) -> str | None` for
cheap, read-only diagnostics. Core calls it only for live listing rows. A relay
loss takes precedence; an optional diagnostic failure cannot remove the row.
List, inspect and doctor use the same connection error.

## Durable changes and ownership

`universe.agents.rebind_session(root, reference, previous, session_id, reason,
launch_id, *, home=None)` requires the exact current launch and an expected
previous ID. A stale launch, mismatched previous ID, changed storage home, or
conversation owned by another agent raises `ValueError`. Repeating an already
completed transition makes no new journal row or event. Reasons are short names
rather than a closed enumeration, so future integrations can report `new`,
`resume`, `seeded`, or `fork`.

Each JSON-lines row has these fields:

```json
{"schema": 1, "id": "transition UUID", "at": "UTC ISO timestamp", "harness": "configured harness name", "home": "/canonical/native/home", "launch": "launch UUID", "from": "previous native UUID", "to": "new native UUID", "reason": "clear"}
```

The journal is authoritative. Core appends and flushes a complete row before
atomically replacing the properties pointer. If the process dies between those
writes, the next reconciliation or resume finishes that pointer change without
appending another row. A torn, malformed or conflicting journal is an error;
core never guesses a replacement ID from it. Transcript readers can use each
row's `from`, `home`, `at` and `reason` to locate earlier generations. No transcript
path or cursor is stored. Returning to an earlier session owned by this same
agent is allowed, except for an ID explicitly retired by missing-conversation recovery.

`resume --start-fresh` is a separate, explicit boundary. Only after every
candidate was readable and the old conversation absent, core appends a schema-2
`fresh-start` row with reason `missing-conversation`. Its `home` and `from` retain
the old claim; `replacement` records the new storage selection and pointer before
the properties pointer changes. Before that append, the properties record's
`native_session_homes` includes both the old and replacement scopes, so a crash
cannot let a peer skip the new journal as unrelated storage. An assigned harness
gets a new UUID. A reported or file-based
harness has `to: null` until its new conversation binds, when a schema-2 `bind`
row completes the pointer. Append-first recovery uses this saved replacement,
never the recovering shell's environment. Retired IDs remain owned and cannot be
bound again. Delivery never follows a transition across this retirement boundary.

Ownership claims are serialized across the containing universe tree. The key is
the provider, canonical storage home and native ID. Earlier generations remain
reserved to their agent. When a legacy record has no known storage home, a
potentially matching claim is refused conservatively. Initial binding, assigned
session publication, rebind, and moves use the same namespace lock. Moving an
agent checks its earlier generations as well as its current ID.

A damaged journal belonging to another agent blocks a claim only when its
readable bytes mention the requested ID, including a torn row. Unrelated damage
does not block new agents. A journal that cannot be read or safely inspected
still refuses a potentially overlapping or unknown namespace, naming the agent
and file to repair. Explicitly different providers, or the same known provider
with disjoint recorded storage homes (including any retired scopes), are excluded before reading journals.
Missing or malformed scope is never proof of separation. Historical storage
homes keep the absolute scope recorded at the transition; reading history never
follows a parent directory that became a symlink later.

Lock order is agent launch locks, then tree namespace locks in path order, then
properties locks. A claim never acquires another agent's launch lock while it
holds the namespace lock. Its scan reads journal tips without repairing other
agents. A conflicting observed switch leaves the original binding intact,
records `runtime.binding_conflict`, refuses native input, and appears in the
normal diagnostics. Verified evidence that the terminal returned to an owned
conversation clears that conflict.

After durable persistence, core emits `agent.session` with the exact launch and
`from`, `to`, `reason`. As with other plugin events, recording is best effort and
contains no conversation content.

Usage remains scoped to the current native conversation. A rebind does not add
earlier generations' tokens, cost or context to the new session's measurement.
The journal preserves history for explicit transcript or accounting features.


## Codex conversation switches

On [checked Codex releases](../reference/compatibility.md#checked-harness-versions) with mv2's bridge, the person can switch the agent's
conversation from its terminal: `/new` and `/clear` start a fresh thread, and
`/resume` returns to an earlier one. The agent, launch, workspace, Codex
storage, grants and inbox stay; only its current conversation changes, and mv2
follows it as it follows Claude's `/clear`. Earlier threads stay claimed by the
agent and readable.

**What counts as a switch.** Codex has no event that says which thread its
terminal shows. mv2 follows only the explicit navigation requests the checked
Codex terminal sends on the agent's own bound connection: a persistent start of
a fresh thread in the same workspace (`/new`, or `/clear` with its own source),
or a full resume of a different thread (`/resume`), and only after Codex's
matching success reply. Background attachments, reconnects, queue loading,
`/btw` side threads, review threads and subagents are never a switch. Another
Codex version, a plain or fallback launch, or a request shape the checked
version doesn't send is refused in the terminal with the reason, and the
conversation stays as it was.

**Which threads.** `/resume` returns only to a thread in this agent's own
lineage that isn't retired and has the same storage home, and only after the
adapter's `navigation_target` finds that exact thread's rollout under that
recorded home, with the same proof `TranscriptInfo.earlier` uses for an earlier
thread's readability (anything but a clear yes refuses). A
thread outside the lineage, one another agent claims, a side or subagent
thread, or one whose rollout can't be proven is refused. `/new` or `/clear`
choosing another folder or worktree is refused.

**Permissions.** The validated primary reply establishes this launch's complete
native grant set, including approval policy and reviewer, sandbox roots and
network/tmp restrictions, profile provenance and runtime workspace roots.
`/new` and `/clear` use that policy exactly, including after ordinary reattach;
they never infer an agent root or merge wider request/configuration roots.
An earlier-thread resume keeps its native request permissions. Every selection
reply must match the validated grant set before core commits it. Only that
commit publishes the selected thread's permission metadata.

Incomplete or unmappable grants disable navigation without rejecting ordinary
startup or `/btw`. An observed current-thread permission change or malformed
settings update also disables navigation until an ordinary `mv2 resume` verifies
the current grants. Native settings notifications omit runtime workspace roots.
Codex 0.169.0 also requires an ordinary root conversation: parent, attached and
fork relations must be explicitly null, with no feature thread source. Each
initial, selected or reselected reply must expose exactly one built-in `local`
environment. Its native cwd and complete runtime root set must match the known
workspace and validated five-field grant envelope. Missing/null, empty, multiple,
remote, malformed or conflicting selections do not establish navigation grants.
The built-in local ID is reserved by native environment construction, configuration
and registration; it cannot name a registered remote executor.

Native snapshots project the Ready primary environment's effective permission
profile and roots. Public root environment requests use thread-owned configuration;
inherited and feature provenance is excluded rather than guessing hidden Pending
or Failed owner state. New and clear pin the existing environment parameter to the
verified local cwd and roots alongside the copied grants. Resume keeps the saved
request semantics and must confirm the full reply before committing.

Current-thread settings notifications must report that same canonical selection
and an unchanged permission projection. Emptying or changing the selection
invalidates grants; a later matching notification or reselection cannot revive
authority or replace the launch ceiling. Earlier checked releases retain their
qualified omission/null behavior. These checks do not establish freshness beyond
observations on this launch or cover inherited owner updates.

A complete, unchanged permission projection in the same workspace only preserves
an already-valid snapshot; it cannot create or revive one. Other threads'
notifications cannot replace that policy.

The bound is this launch's validated grants and changes mv2 observes. Another
client can narrow runtime roots without a notification. A later `/new` or
`/clear` can restore those unobserved roots up to the launch ceiling, never beyond
it. Run `mv2 resume` to re-read the current grants before navigating if another
client changed them. mv2 does not issue an extra admission RPC to discover such
changes.

**When.** Only while the agent is idle. A switch is refused while a turn or
input is running or its state is unknown; while another switch, a `/btw` side
thread or an unsettled mv2 command is in flight; and while the current thread's
Codex queue holds anything (paused entries and other clients' items included)
or an mv2 message Codex accepted, is submitting, or whose outcome is uncertain
may still run there. Input that already ran and is confirmed in the transcript
doesn't block. Codex runs an idle thread's queue whether or not a terminal is
attached, so input left behind would run unseen in a conversation the agent has
left. The refusal says why and what to do; nothing is interrupted, cancelled,
started or replayed to make a switch possible.

**The transaction.** Core begins a switch under the agent's launch lock: it
checks the exact current launch, conversation and storage home, and writes a
durable switching marker. Every mutating path (sends, ask, recovery, command
runs) sees the marker and waits or is refused for the moment. After Codex's
validated success reply, core appends the transition to `sessions.jsonl`
(reason `new`, `clear` or `resume`) before it changes `properties.json` and the
agent's stable identity, keeps every earlier claim, and only then lets the
success reach the terminal. A definite refusal clears the marker and changes
nothing. A lost reply, a disconnect, a malformed success, a claim conflict or a
failed commit leaves the conversation uncertain: current input, reads and
operations stay fenced, and mv2 names the recovery: close the agent's
terminal, then `mv2 resume` it, which restores the conversation the journal
records. It never guesses which thread is current, and never creates or
archives a thread to fix it.

An explicit resume marks still-pending input saved during the old switch as
not delivered, with `mv2 deliver AGENT --retry` advice. This also covers a
resolved switch whose delivery flush never finished. The messages keep their
original IDs and targets; retry follows the resumed conversation through the
usual lineage checks, once, and does not restart a paused native queue. Resume
does not change accepted, submitting or uncertain attempts. These failures
remain retryable even if preparing the replacement terminal fails.

**The limit of this evidence.** Once Codex has accepted a navigation request,
the requested conversation is authoritative. The terminal can still fail to
load that thread's history after the reply and stay on the old one; mv2 can't
see that. Input the old thread would then take is refused before it reaches
Codex; the checked releases may then exit rather than show the refusal. Closing the
terminal (if it is still open) and running `mv2 resume` restores the recorded
conversation. The reverse can show too: Codex's `/clear` resets the
terminal's view before it sends its request, so a refused or uncertain
`/clear` can leave an empty view while the recorded conversation is still the
old one. mv2 refuses the request, which the terminal may not show, and
follows the record, not the view; closing the terminal and running `mv2
resume` restores the recorded conversation's view. Following the terminal's
actual view would need a Codex event that says which thread it shows.

**After a switch.** Context use reads as unknown until the new thread reports
it. Focus belongs to the agent and stays; the switch itself isn't a turn end.
The next `mv2 resume` uses the current thread. Grants pass as they were: a
spawn's explicit sandbox and approval flags are carried as the equivalent
native settings, so Codex's in-session resume accepts them; a shape that can't
be mapped exactly is refused with a diagnosis, never widened.

## Transcript generations

`universe.sessions.generations(directory, properties)` reads the journal against
that same properties snapshot. A generation is its harness, native ID and saved
home together. Each non-null `from` ends one; a fresh start's replacement home
belongs to its `to`, and a later `bind` ends none. There are no known earlier
generations without a journal. A returned-to generation appears once, at its
latest departure, and the current scope is excluded.

The helper preserves historical home strings without following today's links;
an unknown old home stays unknown. It returns only identities, departure reasons
and times. The transcript provider decides whether those conversations are still
readable. A damaged or conflicting journal keeps its named repair error rather
than yielding partial history.

## Messages across a session change

Messages address the agent. When a saved message has definitely not been
accepted, delivery follows the agent's current conversation if its original
target is in this same harness and storage home's verified lineage. This also
applies to messages held during startup and to an explicit retry of a definite
failure, including a wake prompt. Core preserves the original inbox record and
adds `session_changes` to its delivery state: the old and new targets, when it
was moved, and the journal transition IDs and reasons (such as `clear`). A
conversation outside that lineage is never substituted. On a Codex switch only
input mv2 knows was never submitted, or definitely failed, follows the new
thread; input Codex accepted, queued or is submitting, or whose outcome is
uncertain, stays on its original thread and is never moved or run twice.
`deliver --check` names the original conversation and reads its receipts
through a read-only path that never loads or resumes that thread.

Accepted messages and submissions whose outcome is uncertain are never moved
or resent. `deliver --check` can look up their receipts against the original
owned conversation after a session change. A missing receipt stays uncertain;
it does not prove non-delivery and does not authorize a resend. Each adapter
still decides what historical receipt evidence it can provide.
