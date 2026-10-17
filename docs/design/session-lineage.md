# Native session changes

An mv2 agent keeps its own identity when its native conversation changes. The
current native ID remains in `properties.json`. A verified change appends a
transition to the agent's `sessions.jsonl`; the first assigned or bound session
has no transition and is generation zero.

This is an optional adapter contract. Existing adapters keep their exact
conversation behavior unless they implement `observed_binding`. In particular,
this does not enable Codex `/new` or switching to another thread through mv2's
relay.

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
agent is allowed.

Ownership claims are serialized across the containing universe tree. The key is
the provider, canonical storage home and native ID. Earlier generations remain
reserved to their agent. When a legacy record has no known storage home, a
potentially matching claim is refused conservatively. Initial binding, assigned
session publication, rebind, and moves use the same namespace lock. Moving an
agent checks its earlier generations as well as its current ID.

A damaged journal belonging to another agent blocks a claim only when its
readable bytes mention the requested ID, including a torn row. Unrelated damage
does not block new agents. A journal that cannot be read or safely inspected
still refuses the claim, naming the agent and file to repair. Historical storage
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

## Messages across a session change

Messages address the agent. When a saved message has definitely not been
accepted, delivery follows the agent's current conversation if its original
target is in this same harness and storage home's verified lineage. This also
applies to messages held during startup and to an explicit retry of a definite
failure, including a wake prompt. Core preserves the original inbox record and
adds `session_changes` to its delivery state: the old and new targets, when it
was moved, and the journal transition IDs and reasons (such as `clear`). A
conversation outside that lineage is never substituted.

Accepted messages and submissions whose outcome is uncertain are never moved
or resent. `deliver --check` can look up their receipts against the original
owned conversation after a session change. A missing receipt stays uncertain;
it does not prove non-delivery and does not authorize a resend. Each adapter
still decides what historical receipt evidence it can provide.
