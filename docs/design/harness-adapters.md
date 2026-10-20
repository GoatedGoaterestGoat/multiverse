# Harness operations

Multiverse exposes the same operations to its CLI, importable tools and TUI backend:

| Operation | Contract |
| --- | --- |
| `spawn` | Start a native terminal in the saved workspace with bootstrap, optional initial prompt and model override; apply the mv2 agent name where supported. |
| `kill` / `stop` | Cancel native work, terminate the owned native process, wait for exit, then remove its tmux wrapper; never stop a shared daemon. |
| `resume` | Reopen the exact saved conversation, never the latest or a replacement session; reapply the mv2 agent name where supported. |
| `wake` | Resume a stopped/dormant agent with a message as its interactive starting prompt; no native message API is needed for that prompt. |
| `send` with `steer` | Deliver at the next safe boundary, without aborting shell tools; wake an idle session where supported. |
| `send` with `follow-up` | Deliver as the next turn after current work finishes. |
| `ask` | Answer an isolated side question without adding it to the main conversation. |
| `get_usage` | Return measured context and/or cumulative session usage, or an explicit unavailable/error result. |

`harnesses/types.py` defines `HarnessAdapter`, `SessionHandle`, `Message`, results
and the execution boundary. A handle contains the stable Multiverse agent ID,
provider name, exact native ID/session file, saved workspace, and current launch
ID. Core derives it from authoritative properties/runtime records; it does not
maintain another copy of those records. Spawn/resume return `session` in their
result. `list` reads current core identity, process state and reported status after
asynchronous identity binding; these are not adapter operations.
Native ID can be null while a reported session has not registered; operations
must not guess a recent session. Claude receives an assigned UUID, and resumes it
with `--resume` only once Claude has saved a transcript for it (it writes none before
the first message); a session Claude has no transcript for restarts fresh under
the same UUID with `--session-id`, without replaying its task, and says so in the
resume result (the `resume_notice` hook) and the agent's details, where core keeps
it with the launch's notices (never in `fallback_reason`, which is for a plain
launch's cause). The same hook names a caller's `CLAUDE_CONFIG_DIR` that the resume
ignored for the storage recorded at spawn, as Codex names an ignored `CODEX_HOME`. The note tells
"stopped before its first message" from "the saved conversation is missing"
(Claude deletes transcripts after `cleanupPeriodDays`) by what outlives the
transcript: a message to this session whose receipt the mod wrote, context the
mod reported after the agent last moved to this session, or the session in
Claude's prompt history (`history.jsonl`, which Claude prunes too, so its absence
proves nothing). When a launch carrying a wake prompt exits during startup, core
asks `startup_prompt_evidence(session, prompt, since, environment)`: Claude says
True when the session's transcript has a user row with that prompt from the
launch's start on, False when the transcript is missing or complete without one
(the message is then a definite failure that `--retry` resends), and None when it
can't tell (unreadable or damaged rows, a storage folder it can't read, or no aware
start time), which keeps the message uncertain. It reads the launch's own storage:
`session.home`, pinned before the launch started, else the launch environment's
absolute `CLAUDE_CONFIG_DIR` or `HOME`, and never the checking process's, which
could find nothing and wrongly prove the prompt absent. Usage and the context
reading use `session.home` the same way, so a dashboard or CLI running with
another `CLAUDE_CONFIG_DIR` still reads the launch's own transcripts. After a None, core asks the
optional `startup_prompt_evidence_reason` with the same arguments: one line naming
the folder or transcript that can't be read, or the damaged row, and what to fix,
without the command, which core adds. Claude keeps an accepted message only in its in-memory prompt queue until
it reaches the conversation, so one still queued when the process dies is gone. Once
that launch has exited and a later one on the same session has come up idle, core asks
`accepted_message_evidence(session, message_id, text, since, home)`: delivered when the
transcript holds the `queued_command` row carrying the message ID (a mid-turn steer) or
a user row with the text from the accept time on (a follow-up or idle steer), lost only
when every transcript that could hold it was read whole without either, else unknown.
For an older record that kept only the body, a body match stays unknown. Missing reason hooks default to no additional
diagnostic; they cannot change the evidence result.

Core retains the failed launch's exact prompt, start time, conversation identity
and storage context after observing its process exit. `deliver --check` re-asks
the evidence hook with that snapshot, even after another launch or conversation
replaces the current one. True confirms consumption; False makes the message
retryable; None remains uncertain. This path never reconciles the current native
binding or resends input. Older receipts can be qualified from the matching
exited runtime, request and task while those original facts remain available;
missing facts never authorize retry. A new wake clears the preceding snapshot.

Every transcript lookup lists
Claude's folders itself rather than with a glob, which skips unreadable folders
silently. Only a folder that could hold the session counts: Claude names a project
folder after the real path it was launched in, NFC-normalized, with every UTF-16
code unit that isn't an ASCII letter or digit replaced by `-` (past 200 units, the
first 200 plus `-` and a hash). So an unreadable folder blocks when its name,
ignoring case, is the key of the handle's workspace (as recorded or as its real
path) or starts with that key plus `-`, which covers Claude's own worktrees under
the workspace and the hashed long form. Another workspace's unreadable folder is
skipped; with no absolute workspace, every unreadable folder blocks. The handle's
workspace is the launch's own, so a recheck of a failed wake reads that launch's
folder even after the agent moved on. GOAT applies the same rule to its search. The
same scan answers core's `session_storage_evidence(session, home)` for each
candidate home of an agent with no recorded storage: present, absent only when
every folder that could hold the transcript was read, or unknown.
When part of the storage that could hold the session can't be read, resume tries the saved
conversation and usage names the folder, instead of either reading the scan as
proof that nothing is there. Claude takes the bootstrap with `--append-system-prompt` on spawn and on
every resume. Claude Code keeps a conversation's system prompt as recorded at its
first request (`--system-prompt-snapshot`, on by default since 2.1.267), which
would leave a resumed agent with the bootstrap it started with: a plugin's
bootstrap line added or removed since, or a template change, would never reach
it. Resume therefore passes `--system-prompt-snapshot off`, so the current
bootstrap applies; an unchanged one keeps the prompt cache, and a changed one is
cached afresh once. Resuming a Claude agent therefore needs Claude Code 2.1.267
or newer, the first with that flag: the flag is left out where the version is
read and known to be older (an auto launch falling back to plain), but a plain
launch never asks the version, so on an older Claude its resume fails on the
unknown option, which the failed-launch diagnostics show. A Claude harness can
keep Claude's snapshot instead with `"system_prompt_snapshot": "on"` in its
`harness.json` settings (default `"off"`): resume then passes no flag, the
bootstrap applies only to new conversations, and a resumed agent keeps the one it
started with, including bootstrap lines of plugins disabled or removed since.
A `--system-prompt-snapshot` the harness passes itself in `args` or
`resume_args` wins: mv2 then adds none, and never removes it.
Measured on 2.1.301, the flag costs nothing while the bootstrap is unchanged: git
changes, within a session or between stop and resume, do not change the rendered
system prompt. A changed bootstrap re-caches the prompt and the conversation once.
Passing the flag only when the bootstrap changed is not enough, because a later
resume without it returns to the prompt recorded at the first request. Pi owns an
exact session file and its bridge reports its UUID. Codex bridge binds the exact
thread ID and workspace from the native launch reply. Plain Codex uses
prompt-driven `session-bind` with the native `CODEX_THREAD_ID`.

## Ownership and implementation

```
CLI / tools / LocalBackend
          |
    shared orchestration -- identities, inbox, retries, generation locks
          |
    HarnessAdapter operations
          |
    provider-native API / extension, using core execution where needed
```

Each built-in has its own folder under `src/multiverse/harnesses/`:

- `codex/`: launch/resume preferences and native app-server queue/steer protocol.
- `pi/`: launch/resume preferences, interactive TypeScript extension, transcript receipts.
- `claude/`: launch/resume preferences, peer-inbox steering (auto by default) and its Claude mod (`mod/`).
- `legacy/`: lossless compatibility for existing schema-1 local definitions.

`base.py` shares argument rendering and default unsupported operations. Built-ins
do not inherit from the legacy compatibility adapter. Provider-specific settings,
assets and native probe interpretation live in their provider folders. The
registry only resolves the configured implementation. A provider need not inherit
from the rendering base: the contract test uses an independent fourth provider.

Adapters return native results from `send`, `ask` and `get_usage`.
Launch operations return process specifications; core owns actual process/tmux
creation. `spawn(context)` takes launch options; `resume(session, context)` also
takes the exact saved `SessionHandle`. The session supplies the authoritative
agent/native IDs, session file and workspace; launch options cannot override
that identity. `resume()` validates the saved target before returning its command;
core does not create a launch record or start a process if it rejects the target.
The injected executor owns subprocess/RPC deadlines and terminal ownership
checks; the provider interprets native replies. Internal command/RPC plans are
never public send results. There is no new model loop, simulated chat
UI, or fallback that invokes another model to forward a message.

Core owns universes, agent IDs, launch generations, environment scrubbing,
workspace preservation, processes and tmux. `attach`, `inspect` and process
`kill`/`stop` remain core operations; stop first calls the adapter to cancel work hosted outside the terminal. Reported phase/summary are separate
from observed liveness. The report CLI prints one line (`Reported working.`, plus `You have N unread inbox
messages.` when inbox-only messages wait, counted without marking them read)
without echoing the summary or task/properties data; its Python `run()` result
remains the full record. `task` is a compatibility name for the optional initial
prompt, not a scheduler object. It is no longer copied into the status summary.

### Native service, session and terminal attachment

These are separate identities. `SessionHandle` still identifies the exact
conversation and workspace. Its optional `service` is a `NativeServiceHandle`
with a provider-selected `ServiceIdentity` (provider plus canonical storage or
account scope), an existing endpoint and `lifetime="external"`. Its optional
`attachment` is the current `AttachmentSpec`: where work executes (`terminal`
or `service`), readiness (`process` or validated `binding`), recovery policy,
passive status observation and the session-local mouse preference. Claude, Pi
and legacy adapters keep terminal execution and their existing registration
readiness; their service and attachment fields default to `None`.

`SharedServiceSpec` describes only idempotent explicit start/reuse. Core has no
service stop or restart operation. Its optional identity is persisted separately
from launch-local sockets and compared on resume; a changed scope is rejected.
Identity excludes daemon PIDs, so a legitimate restart at the same endpoint and
storage scope does not change it. Old specifications without scope still work
but cannot authorize automatic recovery. Codex supplies canonical `CODEX_HOME`
and any selected `CODEX_SQLITE_HOME`; core never guesses a provider's home.

`RpcProxySpec.binds_session=True` declares a validating launch protocol.
`launch_proxy(settings).incoming(reply)` must return a native ID only after
checking exact identity, workspace and effective requested permissions. A
binding-ready attachment stays `starting` until that callback succeeds. Merely
having an RPC proxy does not authorize readiness or recovery. The usual child
startup check and startup-input acceptance still gate readiness. Each fallback
carries its own attachment policy; automatic recovery has no fallback. Core
applies the final prepared mouse preference before runner startup. A late relay
fallback applies its selected preference under the launch lock after checking
the exact owned pane; clearing an earlier mv2 mouse override restores the
inherited option. Global options and other wrappers are untouched.

The optional `AttachmentProvider` extension leaves the original adapter protocol
sufficient. `attachment_recovery(session)` defaults to `None`; opted-in providers
return a bounded, read-only `RpcSpec` targeting the saved service endpoint. Its
successful result contains `NativeActivity`, binding the native ID, workspace
and service to explicit status, active-work, wait, queue and background-work
observations. Every work flag must be exactly `False` and status must be `idle`;
unknown, malformed, unsupported or mismatched results fail closed. Core owns the
recent proof, activity invalidation, message and child-process checks, locks,
backoff and pane replacement. Neither a service nor a relay alone opts in.
Adapters never mutate runtime files or tmux. Optional `retained_usage` returns an
existing exact-session measurement; core preserves its timestamp on resume.

Older providers returning an RPC proxy without an attachment declaration are
adapted to `legacy-binding`: work ownership is `unknown`, readiness keeps the
original native-ID/callback gate, and recovery is `unavailable`. Saved requests
missing the new fields retain the same behavior. Startup-input acceptance is
still required. This compatibility policy does not claim effective permission
validation or independent service work. To opt into recovery, a provider must
supply the scoped external service, an explicit service-owned binding attachment,
`RpcProxySpec.binds_session=True`, and the read-only typed activity plan. No
legacy relay inference can authorize automatic recovery.

Native RPC method and handshake names also belong to the provider. A request's
`on_success` tuple contains `RpcNotification` objects for core to send after a
successful response. Codex declares its `initialized` notification there; core
has no implicit handshake and never answers unsolicited native requests.

## Lifecycle paths

`tools/spawn.py` and `tools/resume.py` call `agents.start()`. Spawn creates a new
agent UUID and saves its name, prompt, harness, model and workspace. Resume loads
that record, keeps its workspace and exact native identity, and accepts an
optional model override. Under the agent's `.launch.lock`, core refuses another
launch unless observation says the previous one is stopped or dormant. Unknown
liveness also blocks a duplicate launch.

The adapter first validates and describes its command. Core then records a fresh
launch ID, bootstrap, prompt and request; starts/reuses a shared native service
when required; creates a detached tmux session running `runtime/runner.py`; and tags the
pane with universe, agent and launch IDs. The runner waits up to 15 seconds for
core's `ready` file, scrubs inherited identities, opens any relay, launches the
native child, and owns the relay/status watcher until that child exits. A clean
child exit or requested shutdown records `stopped`; other nonzero exits record
`failed`. Bridged launches
remain `starting` until the native reply validates and binds; plain launches
become `running` when the runner starts the child. Setup failures
retain the failed record and clean up a created terminal/socket directory.
Returning from `spawn` means the launch was registered; it does not mean native
authentication, session binding or the first model turn has finished.

`spawn --wait` and `resume AGENT --wait` opt into a readiness wait after
registration (`--wait-timeout SECONDS`, default 60). Core waits outside the launch
lock so native binding can finish. Every child must first survive a two-second
startup check (`runtime.startup_checked`); plain/legacy launches then require a running child.
A supported plain refusal observer must also settle before readiness: a started screen
proceeds, refusal fails with the native reason, and the bounded window ending without
either proceeds with an explicit unconfirmed-screen notice. Unsupported versions keep
process readiness;
bridge launches also require the exact native ID and delivery readiness. For
Codex, `running` means the relay validated the native reply and effective grants.
Claude/Pi wait for their bridge registration capability. A changed generation is
refused. The usual result carries the latest runtime/session plus `ready` and
`ok`; failures return `error` and CLI exit 1. Timeout also sets `timed_out`, keeps
the launch intact and never retries it. This waits for launch readiness, not
completion of the first model turn. The dashboard remains asynchronous.

If a native child exits during that check or later exits nonzero, core captures
its owned pane before publishing the end state. The child keeps its real TTY;
stdout/stderr are not redirected. The saved tail contains at most 40 lines and
8 KiB of UTF-8 text, with terminal controls removed. It lives in
`launches/<uuid>/output-tail.txt`; `runtime.json` records `output_tail_file` and
a `reason` containing the exit code and tail. A missing, inaccessible or replaced
pane produces an explicit output-unavailable reason instead. `list` (including
JSON), `inspect`, the dashboard inspector and `doctor` retain the diagnostic even
after the wrapper disappears. `--wait` returns it as its error. Explicit shutdown
and clean exits after the startup check do not create failure tails. Readiness
cannot guarantee that a child will not fail later.

`tools/kill.py` and its `stop` alias call the same core shutdown operation.
Core verifies the pane's universe, agent and launch tags under the launch lock,
sets `state=ending`, clears observed status, and enables `remain-on-exit` even
for panes launched by older versions. `LIVE` is then `ending` and `STATUS` is
`-`. Native status callbacks cannot make an ending row appear active again.

The adapter's `stop()` cancels work hosted outside the terminal first. Codex
bridge cancels up to 128 queued submissions on the exact thread, interrupts its
active turn and cleans background terminals. A validated already-unloaded
thread needs no cancellation and is never resumed by kill. A failure retains
a still-running terminal and reports an error. If the child has already exited,
core removes its owned wrapper even when native cancellation fails, recording
`native_cancellation.state=unknown` and a retry diagnostic in runtime and the result.
This does not claim that shared-server work stopped; retry stop once the server
is reachable. Unsubmitted startup messages are cancelled locally. The shared daemon and sibling agents remain
running. Stop also works when the pane is gone: under the same launch lock,
core authorizes native cancellation through the saved endpoint and exact bound
thread, without authorizing any pane operation. Results report cancelled
submissions, interrupt requests and background-terminal cleanup; receipt IDs
remain internal. Local adapters rely on termination of their native process and
report that nothing was running for stopped or dormant sessions.

Core then sends SIGTERM only to the recorded native child after checking its
parent against the owned tmux runner. If it has not exited after two seconds,
core rechecks ownership and sends SIGKILL, with another five-second exit bound.
No text or control keys are injected into the pane. Core releases the launch
lock while waiting, so the runner's binding and shutdown callbacks can finish.
Every later mutation checks the original launch again; a concurrent replacement
can never be torn down by the older request.

Once the child exits, `LIVE` is `stopped` while the tmux wrapper still exists.
Core removes that owned wrapper with `kill-pane`, then records `dormant`. A
shutdown error leaves diagnostic evidence and any remaining wrapper for retry.
The stopped stage may be brief during automatic teardown. New launches always
set `remain-on-exit` while the runner closes its relay, status watcher and startup
delivery. After a running native child exits, a short-lived core cleanup process
outside the pane takes the launch lock, verifies the exact generation and pane
ownership, detaches only clients viewing that pane, then removes it and records
`dormant`. Detachment precedes removal so tmux cannot switch those clients into
another agent. The runner lets the native CLI handle SIGINT: Ctrl+C that only
interrupts a turn leaves the runner and wrapper alive. A startup exit before the
two-second check retains its wrapper for diagnostics, except SIGINT/exit 130.
Explicit kill retains its existing teardown path. Cleanup failures retain a
retry diagnostic in `runtime.json.wrapper_cleanup` and `error`; use `kill` to
retry. Conversations and saved native IDs survive teardown. This cleanup does
not cancel work in a shared native daemon; use `stop`/`kill` if such work remains.
Absent or foreign panes are dormant for the recorded launch and never authorize
signals or pane control. Observation errors remain unknown.

Resume accepts stopped and dormant agents, preserves their native identity, and
removes a retained owned wrapper before launching a new one. Move/remove also
accept both end states and clear any retained wrapper; alive, ending and unknown
agents remain ineligible. Killing keeps agent records, files and native history.
The dashboard's `K` (Shift+k) opens a confirmation and calls the same kill tool;
`s` is an alias. Lowercase `k` keeps its navigation binding. The footer shows
`K Kill`, while `d` remains available through the help screen. `r` resumes either
end state. Header and group live counts include agents that are ending.

### Codex bridge resume permissions

Bridge resume accepts `--dangerously-bypass-approvals-and-sandbox`,
`--sandbox` / `-s` (`read-only`, `workspace-write`, `danger-full-access`) and
`--ask-for-approval` / `-a` (`on-request`, `never`) in shared `args` or
`resume_args`. Long `--flag=value` and attached short values are supported.
The adapter removes these flags from the remote resume CLI arguments and the
relay applies their explicit values as `sandbox` and `approvalPolicy` on the
exact `thread/resume` request. Bypass sets `danger-full-access` plus `never`,
with the same precedence as the native CLI. Malformed/duplicate values fail
before launching. Spawn arguments and plain launches retain native CLI behavior.

The old blanket refusal from `81eca5c` forced bypass into `spawn_args`, which
omits it on resume. That allowed a plain full-access session to return through
the bridge with `workspace-write` restrictions, including blocked Unix sockets
and read-only `.git`. Put grants needed on both spawn and resume in `args`.
For a trusted environment where full access is intended, for example:

```json
{"schema": 2, "adapter": "codex", "settings": {
  "inbox_bridge": true,
  "args": ["--dangerously-bypass-approvals-and-sandbox"]
}}
```

No-flag resume is unchanged: the relay leaves native TUI permission fields as
received and does not infer a grant from `spawn_args` or local defaults. This
is not a guarantee that a prior full-access launch stays full-access: native
TUI settings may restore a different policy. Installed Codex 0.159.3 returned
to `workspace-write` when the bypass was removed in an isolated test. Explicit
shared `args` avoids relying on that behavior. Profiles, `--approve-for-me`,
`--add-dir` and writable-root config overrides remain unsupported on bridge
resume; use `spawn_args` for those initial settings or explicitly choose plain.
Auto falls back for incompatible `--profile`, `--add-dir` and `--approve-for-me`
resume flags; required bridge mode rejects them before launch. Writable-root
config overrides on resume remain errors.

Before accepting the native reply, the relay verifies that each requested
permission matches the effective `sandbox` and `approvalPolicy` returned by
app-server. Bridged launches remain in `starting` until validated binding;
new launches save incoming steer/follow-up messages until that validation completes. A
mismatch records an actionable runtime error, closes that client connection,
and prevents new mv2 input for the unvalidated launch. No automatic downgrade,
empty model turn, turn-time grant fallback, or shared-daemon restart is used.

A shared Codex conversation keeps its current native grants. Multiple clients
of that conversation are normal: whichever client drives it, it is the same mv2
agent doing the work. When explicit requested grants differ from the effective
native reply, mv2 reports both values and refuses input for that attachment. Use
the conversation's current grants or deliberately change them through supported
Codex controls before a new mv2 resume. mv2 never forces an unload, detaches an
outside client, or restarts the daemon to change permissions. Manual attachment
does not require an idle conversation; automatic recovery retains its separate
idle-only safety requirements.

Verification uses an installed Codex 0.163.0 TUI, a disposable native home and
a local scripted provider: a plain full-access launch and two successive
explicit bridge resumes kept the same native UUID, wrote inside `.git` and
bound a Unix socket. Existing no-flag native timing tests retain their
workspace-write behavior. Separate protocol tests cover an ignored loaded
thread grant, missing effective permissions, malformed flags, plain argv,
no-flag pass-through, and input refusal before binding. A model-free native
probe also verified idle unsubscribe/unload followed by a changed resume grant
without restarting the daemon.

## Shared tools and delivery results

```sh
mv2 spawn --name worker --harness codex
mv2 spawn --name reviewer --prompt 'Review the current diff.'
mv2 resume worker
mv2 send worker --delivery steer <<'EOF'
Consider this now.
Preserve the user's $variables and `backticks` literally.
EOF
mv2 send worker --delivery follow-up <<'EOF'
Next, inspect the tests.
Report any failures with reproduction steps.
EOF
mv2 ask worker --question 'What would this alternative cost?'
mv2 get-usage worker
mv2 list --json
mv2 capabilities --agent worker --probe
```

For multiline messages, use `mv2 send AGENT <<'EOF'` with a quoted delimiter.
No escaping is needed for quotes, apostrophes, dollar signs or backticks in the
body; use a delimiter that does not appear alone on a message line. Piped or
redirected stdin is read automatically when no `--text`, `--file` or `--stdin`
option is supplied. `--stdin` explicitly reads stdin, `--file PATH` reads a file
and `--text` remains available for short messages. Empty messages are rejected;
terminal stdin without a message option fails clearly instead of waiting.

The corresponding Python modules have `run(...)` functions and standalone entry
points, e.g. `python -m multiverse.tools.ask`. They do not import Textual.
`LocalBackend` delegates to the same tools, with no provider-name branches.
Public capabilities are `spawn`, `resume`, `wake`, `stop`, `send_steer`, `send_follow_up`,
`ask`, `get_usage`, `get_delivery`, and `compaction` (whether this session's
`/compact` uses tree compaction; see [compaction](compaction.md)). The `session_name` capability describes
naming during spawn/resume, not a separate rename operation. Claude and Pi pass
`--name` in both modes. Codex bridge calls `thread/name/set` after validating and
binding the launch reply; plain Codex has no CLI naming flag. Legacy manifests
remain unchanged and report naming unverified. Capabilities describe adapter
support, not confirmation of an individual launch's display name. Support may
depend on configuration and a registered, running native session. Discovery returns supported/unavailable/error plus a reason.

`send` first persists the text, sender, timing and an internal receipt identifier,
then resumes an ended session or calls the running adapter. Its result includes `saved`, `state`, and a delivery
summary without message identifiers. Unavailable or failed native delivery
produces `ok: false` and a nonzero CLI exit while retaining the saved message.
Use recipient-scoped recovery instead of repeating `send`, which creates a new
message.

Steer/follow-up to `stopped` or `dormant` resumes the exact agent, preserving its
workspace, task, native identity and launch preferences. The framed sender line
and text become `LaunchContext.resume_prompt`. Claude appends a positional prompt
to `claude --resume UUID` (never `--print`); Codex appends it after the UUID in
`codex resume`; Pi appends literal text after `--` while reopening its exact
session file. All remain interactive. The `wake` capability is supported by
these adapters in both plain and bridge modes; legacy manifests have no verified
resume-prompt recipe. Dashboard `m` uses the same send path. Inbox-only never
wakes, and a live plain session still reports native delivery unavailable.

Core records the wake attempt before releasing the runner. Surviving the startup
check becomes `accepted` with `delivery_method: resume_prompt` and consumption
unconfirmed. There is no bridge/mod delivery for this first prompt and no
correlatable native receipt for it. Failure before process startup is retryable;
an early exit after the child starts records the native diagnostic as `unknown`
for `deliver AGENT --check`, because the prompt may already have been consumed;
an interrupted/uncertain attempt is never replayed, including by receipt checks.
The saved task is unchanged; each launch retains its own prompt in `task.md`.

New bridge launches hold subsequent messages durably as `pending`, tied to that
launch. A runner thread waits for exact binding, validated Codex permissions or
Claude/Pi extension registration. For a Codex wake it also observes acceptance
of the TUI's first `turn/start`, so queued input cannot race ahead of its CLI
prompt. It then delivers the batch in saved order. Timing modes still apply
inside the native harness. Send and queue draining take `.send.lock` before
`.launch.lock`; wake releases the latter before calling `start`. Readiness waits
hold neither. Failed startup leaves never-submitted input as definite failures;
`deliver AGENT --check` also detects an abandoned launch queue after a runner
crash. Explicit kill cancels pending startup messages. Recovery never replays
accepted or uncertain submissions. Existing runners predating this queue retain
their binding-time refusal until relaunched.

One logical inbox lives in `inbox.jsonl` (immutable messages),
`inbox-state.json` (delivery attempts and receipts), and `inbox.cursor` (the
ordinary read position). These files are core-owned; providers have no mailbox
or retry database.

| Submission state | Meaning |
| --- | --- |
| pending | Saved; no completed submission attempt. |
| submitting | Attempt persisted before native API submission or prompt-bearing process startup; a crash makes acceptance unknown. |
| accepted | Native API accepted, or the resumed interactive process started with its prompt; consumption may remain unconfirmed. |
| failed | Definite rejection or pre-submission failure; explicit retry permitted. |
| launch-only | Final refusal: this launch has no native delivery path. History only; not retried or counted as an open failure. |
| unknown | Submission may have succeeded; never blindly resent, including with `--retry`. |

The immediate tool result `native_queued` maps to accepted; `unavailable`/`error`
map to failed, except a typed `launch_only: true` refusal, which maps to the final
launch-only state. Only old failed receipts with that exact typed flag are
normalized to it; accepted and uncertain input is never reclassified. Supported
stopped-agent wake and failed wake retry stay separate from live delivery.
Transcript confirmation and an answer are independent evidence,
never inferred from acceptance.

```sh
mv2 inbox                         # show unread sender/content blocks and mark them read
mv2 inbox --wait --timeout 30s     # wait for unread inbox-only messages
mv2 inbox --all --last 20          # recent history, oldest first
mv2 inbox --all --since 2h         # history from the last two hours
mv2 inbox --all --json --after 0   # raw history and delivery evidence, paged for scripts
mv2 send worker --delivery inbox-only <<'EOF'
Later, check the workspace configuration.
EOF
mv2 deliver worker --retry        # retry this recipient's definite failures
mv2 deliver worker --check        # reconcile pending/uncertain deliveries, record accepted receipts
```

The CLI prints one `Message from NAME:` block per unread inbox-only message,
with its full text and no metadata. Names are resolved from the sender's agent
record; people appear as `user`, and another universe keeps core's
`(in universe NAME)` wording. An empty page prints `No unread messages.`
Unread pages contain up to 50 messages. The CLI advances the read position only
after output has been written and flushed successfully, while holding the
launch and inbox locks. A failed write or flush leaves the page unread;
concurrent consumers cannot overtake the output. Stale launch callbacks cannot
read or advance the current launch's inbox. Cursor advancement includes skipped
native records and stops at the last complete record in the page.
Python `inbox.run()` still returns and consumes the existing dictionary for
callers such as the dashboard. CLI `--json` preserves that machine output
(sender, text, time and delivery mode on ordinary reads).

`inbox --wait` returns as soon as an unread inbox-only message arrives. It waits
indefinitely unless `--timeout DURATION` is given (seconds, or units `s`, `m`,
`h`, `d`, `w`). It polls file metadata between reads, releases the launch and
inbox locks between checks, and still rejects outdated launches. Native-only
messages do not satisfy the wait. Timeout exits 1 without advancing the read
position: readable output says `Timed out waiting for unread messages.`; JSON
adds `timed_out: true` and `ok: false`. It can combine with `--peek` and `--json`,
but not history options.

`inbox --peek` returns the same unread page (up to 50 messages) without changing the read position; it cannot be combined with `--all` or `--after`.

`inbox --all` prints the full history, including native messages, without changing
the ordinary read position. `--last N` selects the newest positive N messages,
still printed oldest first; `--since TIME` filters by an ISO timestamp or a
relative duration such as `30m` or `2h` (also `s`, `d` and `w`). Timestamps without
a zone use UTC; undated legacy records do not match time filters. Time filtering
precedes `--last`. Both options require `--all`, and `--last` cannot combine with
cursor paging. `inbox --all --json` retains the raw view, including internal
identifiers and delivery evidence, with 50-message pages; `--last` can select a
larger batch. Page with `--after CURSOR`, which requires `--all --json`.
Reading history does not cancel pending native delivery, so viewing a message
there and later receiving it natively can produce a duplicate.

`--queue-only` is a compatibility alias for `inbox-only`. Native-assigned messages
never also appear in ordinary mailbox reads, including failed/pending submissions.
They remain inspectable through `inbox --all`; no automatic switch to manual
handling occurs. Native delivery presents only the core sender line and text.

`send` and `deliver` print short readable outcomes by default, never the message
body or receipt identifiers. Queued input is described as waiting for a turn;
uncertain input tells the sender to check before resending. Confirmation means
the receipt shows the message in the conversation, not that the agent answered.
`--json` preserves the existing delivery records and exit codes.

`send first,second,third` reads its message source once and processes recipients
in order. Repeated names or aliases of one recipient send only once. An invalid
recipient or definite delivery failure does not prevent later recipients from
receiving the message. Batch JSON contains `recipients` (each original record
plus the recipient name) and `ok`, false if any recipient failed; single-recipient
JSON remains unchanged. An unsaved lookup failure is distinguished from a saved
delivery failure. Empty comma-separated names are rejected before any send.

Per-recipient sends serialize persistence and submission. Urgent messages may
overtake native follow-ups. Existing schema-less logs retain manual semantics
without rewriting their contents. Retired per-message read markers and reply
references are ignored; an existing byte cursor remains the read position.
Existing `native-delivery.json` receipts remain readable for compatibility;
accepted or uncertain old attempts are not replayed. New attempts use inbox state.

`deliver AGENT --retry` selects definite failures; `--check` selects pending,
interrupted or unknown submissions, plus messages the current launch accepted
without a recorded receipt, and queries receipts without submitting. Missing
evidence for an accepted message is reported but is not a failure; a resume
prompt has no receipt to correlate and is skipped.
Both preserve exact native session identity, allow a new launch of that same
session, and refuse a different session. Core persists each attempt before the
native call and merges delivery evidence separately from inbox read position.
Pi supports receipt reconciliation against metadata in its exact transcript and
current in-process acceptance records. Missing evidence remains unknown. Codex
checks up to 100 queued submissions and 20 recent turns using internal client
correlation; absence remains unknown and requires investigation. Accepted
submissions are never automatically resubmitted.

The old `native-status` command remains a tool compatibility shim for usage.
Its status operation (formerly `activity`) reports unavailable and points callers to
`list`; there is no native status query or adapter `observe` operation. Adapters expose
feature methods directly, with no `control`, `native_capabilities`, `probes`, or
`NativeContext` conversion layer. Resume validation is part of `resume()`, not
a separate adapter method. Binding and built-in resume use the shared
`normalize_session_id()` UUID helper; it validates an ID without discovering a
session. Pi additionally checks its exact session-file header, while Codex
checks unsupported profile/directory resume overrides before any native RPC
and verifies supported explicit grants against the native launch reply.

## Configuration and compatibility

New universes retain references and preferences, not copied implementations:

```json
{"schema": 2, "adapter": "pi", "settings": {"inbox_bridge": true}}
```

Codex defaults to automatic bridge selection when `settings.inbox_bridge` is
absent. `true` requires the bridge; `false` always uses plain native argv. `null`
is not an accepted setting. Pi retains its opt-in boolean behavior; Claude, like Codex, takes an unset `inbox_bridge` as auto, `true` as required and `false` as plain.
All three bundled Codex templates leave the setting absent, including development.
Existing explicit settings and live universe files are not rewritten.

At each spawn/resume, auto first describes a bridge launch and a plain fallback
with the same bootstrap, task/prompt, model, saved native identity and preferences.
Core reuses the existing idempotent daemon startup and socket checks; one
bounded, read-only `codex --help` check verifies `--remote`. There is no speculative
thread or second service probe. Before a native child starts, unavailable daemon,
CLI flag, configuration RPC or local relay preparation selects plain.
Bridge-incompatible preferences such as `--no-daemon` or resume profile overrides
also select plain in auto, with a reason. Invalid configuration is still rejected.
Explicit `true` fails with a diagnostic for unavailable preparation, and `false`
does not call daemon startup or bridge checks at all.

`runtime.json.native_mode` records `policy` (`auto`, `required`, `plain`), actual
`mode` (`bridge`, `plain`), and nullable `fallback_reason`. The session handle
carries it too. Capabilities report the selection in the spawn/resume reasons;
naming, native usage and the status watcher follow the actual launch mode. The
inspector displays mode and fallback reason. A runner fallback rewrites its launch
request before starting the child, and `--wait` uses the actual selected mode.
Messages queued during relay preparation stay saved with a retryable delivery
failure if that launch falls back to plain; the plain launch itself can be ready.
Selected native storage is retained across resume, including when switching modes.
No fallback occurs after a native child starts or after a relay has bound a thread:
identity, workspace and effective permission validation failures stay loud. The
shared daemon is never stopped or restarted to repair a launch. A later resume
reevaluates availability, so a plain-started session can return on the bridge.

Codex allows different CLI and daemon versions, because their native update paths
are independent. `runtime.json.native_versions` records the `cli` and `daemon`
versions returned during launch preparation; the session handle carries them too.
Capabilities and the inspector display these recorded versions, highlighting a
difference without blocking spawn/resume. Opening either view does not start a
daemon or poll for version changes. Native CLI support, configuration RPC, exact
thread/workspace binding and effective permission checks still apply. Invalid
version metadata is a preparation error. `SharedServiceSpec.require_matching_versions`
defaults to `true` for other services; the Codex adapter explicitly sets it to
`false`. mv2 never updates or restarts the daemon to align versions.

### Codex 0.163.0 compatibility audit

The CLI flags and bridge were rechecked against **0.163.0**. Live fixtures use
`native_safety.install()`, disposable `CODEX_HOME` directories, disabled native
updates, and local scripted providers or no model turn. They never connect to the
developer's daemon. The plugin native fixtures explicitly accept 0.162.1 and
0.163.0; `CONFIG_VERSION = (0, 159, 3)` and `MCP_VERSION = (0, 162, 1)` remain
minimum support floors, not the last tested version.

The audit generated schemas from the official 0.159.3, 0.162.1 and 0.163.0
binaries with `codex app-server generate-json-schema --experimental --out DIR`
and compared them with descriptions and titles removed. Relative to 0.159.3,
0.163.0 adds 12 schema files and changes 41 existing files; relative to 0.162.1 it
adds five and changes 14. Neither comparison removes a schema file.

| Surface used by mv2 | 0.163.0 finding |
| --- | --- |
| Initialize, status and token-usage notifications | Wire shapes unchanged from 0.159.3, including active wait flags and last/total token breakdowns. |
| Thread start, read, turns/list, naming, queue add/list/start/delete, steer, interrupt and terminal cleanup request parameters | Unchanged from 0.159.3. Resume's top-level parameters are unchanged; nested raw history content types evolved. |
| Thread replies | `thread/read` adds optional `readState`; turn data adds optional `rootTurnId`, `partial_answer` message phase and item/error variants since 0.159.3. mv2 reads only the fields it needs and tolerates these additions. |
| CLI | `--remote unix://PATH`, `--cd`, `--add-dir`, `--model`, sandbox/approval flags, bypass, `-c`, and `resume [SESSION_ID] [PROMPT]` remain available. Sandbox values remain `read-only`, `workspace-write`, `danger-full-access`; approval CLI values remain `on-request`, `never`. |
| Config passed by mv2 | `check_for_update_on_startup`, `developer_instructions`, `shell_environment_policy.exclude/set`, `sandbox_workspace_write`, and `tui.status_line` remain supported. |
| Plugin allowlist | All currently allowed config keys, status-line items/aliases and MCP fields remain valid. The new `daybreak` status item is outside mv2's conservative allowlist; no new plugin key is implicitly authorized. |

The [versioned config schema](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/core/config.schema.json)
retains the allowlisted config types and enums and the MCP server config shape.
The [status-line item definitions](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/bottom_pane/status_line_setup.rs)
retain the existing names and aliases. CLI help and native fixtures cover the
launch flags, resume prompt, environment forwarding, naming, update suppression,
status, usage, queue delivery and effective permissions.

Release notes from [0.160.0](https://github.com/openai/codex/releases/tag/rust-v0.160.0)
through [0.163.0](https://github.com/openai/codex/releases/tag/rust-v0.163.0)
include saved-permission restoration, server model-default preservation,
explicit-feature-only daemon compatibility checks and cold-resume permission
fixes. These do not eliminate mv2's relay transforms:
[remote startup](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/startup_orchestration.rs)
still rejects `--add-dir` and writable-root config overrides, and
[remote resume construction](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/app_server_session.rs)
clears sandbox/permission and approval overrides. Bootstrap instructions still
need injection into the thread request. Already-loaded threads can still retain
their existing configuration; the effective-reply checks remain necessary.

Native `/new` and `/clear` request `thread/start`; selecting another conversation
through `/resume` requests `thread/resume` on the existing transport. This follows
the 0.163.0 [slash-command dispatch](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/chatwidget/slash_dispatch.rs),
[event dispatch](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/app/event_dispatch.rs)
and [session lifecycle](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/app/session_lifecycle.rs).
The bridge refuses a second start, a persistent fork or a resume with a different native ID;
it never adopts that conversation. An offline relay regression checks that these
requests cannot reach the daemon, cannot replace the stored binding, and leave
the original connection usable. An isolated 0.163.0 TUI probe also exercised
`/new`, `/clear` and a resume to another native UUID without permission CLI flags:
all displayed the relay refusal, retained the saved UUID and allowed subsequent
native inbox delivery. With explicit launch permission flags, the TUI instead
refuses in-TUI resume during its
[remote permission preflight](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/app/resume_config.rs),
before reaching the relay. Neither path silently changes mv2's conversation.

### Codex shared daemon

`SharedServiceSpec.cwd` optionally selects the service startup directory; other
services retain the launch workspace when it is absent. Codex selects the OS
user's home directory for daemon startup and CLI checks. The native terminal and
thread still use the saved agent workspace, and `CODEX_HOME` still selects native
storage. Workspace-relative executable paths resolve before changing directory.
This keeps a shared daemon usable after its originating worktree or temporary
universe is removed.

Codex also owns a separate `pid-update-loop` process. Restarting only the daemon
does not necessarily replace an existing updater. When diagnosing repeated
shutdowns, inspect both PID records under `CODEX_HOME/app-server-daemon`, each
process's executable and working directory, and the managed package's `current`
link. A stranded 0.159.3 updater was observed forcing hourly restarts of an
already-0.160.0 daemon after its original temporary working directory was deleted.
The native updater selects a forced restart when the managed executable differs
from its own running executable
([native update decision](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/app-server-daemon/src/update_loop.rs#L412)).
Explicit recovery can replace just the verified stale updater, then use native
`daemon start` from a persistent directory to reuse the server and start the
current updater; verify that the daemon PID remains unchanged. mv2 does not
signal or replace native updater processes automatically.

Native tests use isolated Codex homes with
`app-server-daemon/settings.json` setting `updater.autoUpdateEnabled=false` and
top-level `check_for_update_on_startup=false` in `config.toml`. The subprocess
guard rejects native execution without either setting, as well as access to the
default native home/socket. `isolated_home(..., config=...)` writes fixture
configuration together with the TUI setting, so provider configuration cannot
overwrite it accidentally. Tests seed a newer cached release and enable checks
in a trusted project override: an unmodified remote TUI shows the update notice,
while mv2's launch argument suppresses it on promptless resume. Disabling the daemon updater
prevents disposable tests from leaving scheduled helpers behind; daemon stop alone
does not stop the updater. The TUI setting separately suppresses update checks and
interactive update prompts in Codex 0.159.3 and 0.160.0.

Before releasing the native child, core enables tmux's session-scoped `mouse`
option for Codex adapter wrappers on every spawn and resume, including custom
profiles and both plain and bridge modes. With Codex 0.160.0 this lets wheel
events scroll the TUI transcript rather than recall composer history. Global
tmux options, root key bindings, alternate-screen behavior and other sessions
are unchanged; legacy schema-1 definitions keep their existing behavior.
Already-running Codex terminals need a resume to pick up the setting. An
isolated real-TUI test injects wheel events through an attached PTY and verifies
native transcript scrolling, an unchanged composer, and no tmux copy mode on
plain spawn and bridge spawn/resume.

The Codex adapter adds `-c check_for_update_on_startup=false` before `--` on
spawn and resume, including wake prompts, plain mode, bridge mode and plain
fallbacks. This suppresses unattended startup update dialogs and banners even if
the user's configuration enables them. Remote TUI mode honors this local setting;
the relay does not move it into the thread request like `developer_instructions`.
If the applicable `args`, `spawn_args` or `resume_args` already sets that key with
`-c` or `--config`, its value is retained and no default is added. Both separated
and joined forms (such as `--config=check_for_update_on_startup=true`) are recognized.
Prompt text after `--` is never treated as configuration.

Users can explicitly restore native update prompts with
`["-c", "check_for_update_on_startup=true"]` in `settings.args`, or just one
mode's argument list. With that opt-in, ordinary resumes and promptless spawns
can wait for terminal input before binding; a bridge stays `starting` and
`--wait` can time out. Native configuration files and the shared daemon's
separate automatic-update policy are unchanged.

A daemon already running from a deleted directory cannot be repaired by reusing
its socket. Codex 0.159.3 returns `config/read` error -32603, `failed to resolve
feature override precedence: No such file or directory`, even when the requested
workspace exists. The adapter retains this error and adds a restart command with
the configured executable and exact `CODEX_HOME`, from the user's home directory.
Run it explicitly only when other Codex work is idle, then retry resume. Required
bridge mode still fails; auto still records the reason and selects plain. mv2
never restarts the daemon on its own. A disposable native-daemon regression test
removes the original workspace and verifies that configuration reads still work.

Shared preferences are `executable`, `model`, `args`, `spawn_args`, `resume_args`,
`unset_env` and `env`. Codex, Pi and Claude additionally support `inbox_bridge`, and Claude also supports `system_prompt_snapshot` (see the Claude section). Native flags,
models, permissions and workspaces are preserved. Inherited `MV2_*`,
`CODEX_THREAD_ID` and `CLAUDECODE` identities are scrubbed before child execution,
and so is an inherited `CLAUDE_CODE_CHILD_SESSION` (Claude Code sets it for its own
tool subprocesses, and a Claude that inherits it saves no transcript), and a
parent Claude's `CLAUDE_CODE_SSE_PORT` (its IDE connection) and
`CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` (its inbox and
its secret), which an agent spawned from inside Claude inherits; a harness that
sets one in `env` keeps its value.
Bundled defaults also unset `NO_COLOR`: the tmux server keeps the environment of
whatever first started it, often an agent's tool shell, and agent panes are
interactive terminals. Setting `unset_env` replaces this list, which opts back in.

`env` maps variable names to string values that every spawn and resume of the
harness starts with, replacing any inherited value:

```json
{"schema": 2, "adapter": "claude", "settings": {
  "env": {"IS_SANDBOX": "1"},
  "args": ["--dangerously-skip-permissions"]
}}
```

The agent's pane otherwise inherits the tmux server's environment, which is fixed
when that server starts, so `env` is the way to give one harness, in one universe,
its own variables. A name in `env` is no longer removed by the default `unset_env`
list (`NO_COLOR`, for example); naming it in an explicit `unset_env` as well is an
error. Names are `[A-Za-z_][A-Za-z0-9_]*`. mv2 rejects names it manages for every
launch (`MV2_*`, `PATH`, `HOME`, `CODEX_THREAD_ID`, `CLAUDECODE`) and those it reads
itself to find a harness's sessions (`CODEX_HOME` and `CODEX_SQLITE_HOME` for Codex,
`CLAUDE_CONFIG_DIR` for Claude); set those in the environment mv2 runs in. Variables
an adapter sets for its bridge, such as Claude's `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS`,
take precedence. Codex bridge tools run in the shared daemon rather than the
terminal process, so the relay forwards harness and plugin variables in the
session RPC config's `shell_environment_policy.set`. These values do not appear
on argv. Plain Codex tools inherit them from the process under Codex's own
`shell_environment_policy`; explicit native filtering still applies. Normal
exclusive idle resume applies the requested tool environment. Codex can retain
old config when rejoining a thread that is still subscribed or active; mv2 never
forces an unload or service restart to change it. Plugin MCP servers receive
only their declared `env` and `env_vars`; bridge MCP entries cannot reference
launch IDs or attachment/tmux sockets. Stable references do not implicitly
authorize a helper as the conversation's shell: plugins keep their explicit API
permissions (see [the Codex plugin contract](plugins.md#codex)).
Claude's compaction builder runs with the same variables, except those it always
scrubs. Values remain plain text in `harness.json` and each launch's
`request.json`, including its RPC settings.
Schema-1 manifests accept the same `env` object.
No adapter changes installed native configuration. The Claude bridge reads only the
inbox key Claude publishes for its own launch's socket; no other credentials are read.

Existing schema-1 manifests, custom bootstrap and assets remain authoritative;
there is no automatic rewrite or migration. Compare source defaults and transfer
all local preferences before opting into schema 2. Retain schema 1 for custom
behavior schema 2 cannot express. Existing universes are never overwritten.

Codex bridge mode uses the shared native daemon for the current OS user and
`CODEX_HOME`. Spawn/resume invokes `codex app-server daemon start`, reads its JSON
socket address, and connects the native TUI through a private launch relay to that
socket using `--remote unix://...`. Concurrent
monitors use Codex's native startup lock; there is no native app-server process per agent, project,
or dashboard. Opening/closing a dashboard does not start/stop the daemon. Core
never invokes daemon stop, restart, bootstrap, install or update. Codex's own
configured daemon update policy remains authoritative. Codex 0.159.2 can install
its daemon package from the installed CLI during native startup, including a
Homebrew CLI. A CLI/daemon version difference is reported, while actual protocol
checks decide whether the bridge is usable. Codex owns daemon
persistence and native history. No Multiverse-specific Codex home is created.

The selected Codex home is saved for exact resume even if another monitor has a
different environment. Agent workspace, model and initial permissions remain
per-thread. The shared server must not inherit an agent identity: startup scrubs
`MV2_*`, native parent IDs and configured exclusions. Before terminal launch the
adapter reads native effective environment exclusions, preserves them, adds
identity exclusions, and supplies only agent-stable tool identity through the
session RPC. The terminal/runner retains its strict launch identity and private
relay; neither is copied into the conversation. Native configuration files are
never rewritten.
Other daemon process environment (provider credentials, proxies and environment
used by integrations) remains shared; it does not change to match each monitor.
Different process-level environments require separately configured native homes.

Exact thread ID/workspace are checked before queueing or steering. Urgent busy
input uses `turn/steer` with `expectedTurnId`; normal/idle input uses
`thread/queue/add`. A racing busy steer is never converted to a fresh turn.
Codex can leave automatic queue draining paused after an interrupt. The owning
terminal relay publishes interrupt intent before forwarding it, and records
exact-primary turn starts and completions. An interrupted completion labels the
queue paused; a newer start clears it. The public API flattens several abort
reasons, so this label never blocks Codex's own native draining. Old-turn and
side-thread events cannot change the primary's state.

Only a new steer or follow-up submitted by mv2 authorizes an explicit restart.
Core durably claims that message ID under delivery locks and exposes the claim
to that send invocation only. Ordinary handles strip it; receipt checks,
retries, inbox-only messages and idle polling cannot reuse it. After accepting
new idle input, the adapter uses queue/start without an item ID, so the native
FIFO head runs even if an older item came from another client. A busy race or
lost acceptance never retries that start or resubmits text. The returned head's
turn is separate from the new tail's receipt.

Pause state survives an attachment-only resume of the exact conversation. A new
message waking a paused bridge resumes without an interactive prompt, waits for
binding, then queues through normal startup delivery. Thus it cannot jump ahead
of older accepted input. Failed startup leaves it saved for an explicit retry.
Default kill still cancels native queued work, and delivery checks report those
messages as cancelled. There is no background idle queue starter.
Approvals remain unanswered by the delivery client. Core bounds transport
requests, frames and deadlines. Explicit sandbox/approval flags in `args` or
`resume_args` are applied and verified on bridge resume as described above;
without them, native TUI permission fields pass through unchanged. Unsupported
profile/directory resume overrides are diagnosed, never silently removed. Whole-table `shell_environment_policy`
CLI overrides are rejected in bridge mode; individual keys remain supported.
Codex 0.159.2 rejects `--add-dir` with `--remote` and drops the
`developer_instructions` CLI override from remote thread requests. The adapter
therefore returns an optional `RpcProxySpec` in its `CommandSpec`.
A command candidate may also carry `notices`, a tuple of presentation strings.
A plain fallback carries its own notices, independently of the bridge candidate;
these strings never change permissions or launch selection. Core sanitizes and
records only the selected candidate's notices in the launch request and runtime.
Spawn, resume, readiness waits and message wakes return them in text and JSON.
A runner-selected fallback replaces the discarded candidate's notices before
starting the child, preserving independent saved-storage notices; inspect also
shows these notes when an asynchronous fallback happened after the CLI returned. Core serializes
it in the launch request, creates a private 0700 socket directory, and starts the
relay in the runner before starting the TUI. The provider supplies pure request
and reply interpretation; core owns WebSocket connections, backpressure, size
limits, error recording, exact session binding and cleanup.
Interactive relay messages have a separate 64 MiB limit and a two-frame receive
queue: native resume replies include conversation history and can exceed the
1 MiB budget used for small control RPCs. Oversized messages still close the
connection with a runtime diagnostic.

An adapter may supply `for_connection(connection_id, owns_primary)` to give each
relay connection a protocol view. The owner callback reads current ownership;
request correlation belongs to the view, while the adapter may share validated
primary binding state. Core uses that view for request/reply transforms, binding,
refusal and usage observations, and post-bind naming. Its optional `closed()`
hook discards only local memory and runs once even if the upstream connection
fails. Cleanup errors do not change the primary launch's health. Adapters without
this hook retain their existing shared protocol behavior.

A view can raise `LocalProtocolError` to reject one frame without invoking
binding, naming, refusal, usage or launch-error callbacks. It supplies a correlated
string or integer request ID and an explicit reply destination: client for client
requests or native responses, upstream for native server requests. Core checks
the frame's ID and direction before replying; a missing ID means drop, and an
uncorrelated frame or notification gets no fabricated response. Ordinary primary
validation and transport failures keep their existing diagnostics and loss rules.

The relay adds the bootstrap to the TUI's initial `thread/start` or exact `thread/resume`
requests, preserving existing developer instructions. Initial writable roots from
mv2, user arguments and native configuration are combined in the thread request,
so external state directories remain supported without the rejected CLI flag.
Resume applies and verifies explicit sandbox/approval grants when configured.
Other traffic, including approvals,
passes through to the native TUI. A malformed launch request gets an explicit
RPC error and a runtime diagnostic; it is never forwarded without its bootstrap.
The relay records the exact thread ID/workspace from the matching native reply
before passing it to the TUI, so bridge launches do not rely on prompt-driven
`session-bind`. It then names the exact thread after the mv2 agent using a separate
initialized control connection to the upstream server (`thread/name/set`, at most
two requests and three seconds). This also reapplies the name on resume. Naming
errors or timeouts are recorded in `runtime.json` without failing the terminal
launch; unrelated TUI requests, native notifications and approvals retain their
own connection. Existing saved launch requests without a name remain usable.
Switching or persistently forking into a different thread requires a separate
Multiverse launch. The relay closes with the launch and never restarts the shared
daemon. Relay loss stays sticky for that launch; the qualified idle-recovery worker described below can create a fresh launch. Plain launches have no relay. The development
template, like base and testing, uses auto; existing unset profiles also try the bridge on their next launch.

Codex 0.163.0's native `/btw` is a narrow exception. Only the bound terminal's
connection may fork its exact primary into one ephemeral side conversation.
The view keeps the pending fork, side ID and bounded request correlation in
memory until unsubscribe or connection close. It never publishes a side binding,
name, usage, refusal, status, inbox or delivery record. Returning to the exact
primary does not repeat the startup grants, bootstrap or naming. Side input and
host requests retain Codex's normal tools and permissions; there is no execution
isolation, and the side can run mv2 commands as the user. Primary send, receipt and
queue-continuation RPCs still address only the saved primary ID. A native side
error is forwarded locally without failing the primary. Unknown native versions,
side resume and persistent forks remain unavailable. This does not add Codex ask.

The runner records `native_relay.state=ready` after binding. Loss of the bound
TUI's relay connection (or an upstream connection failure before binding) records
`native_relay.state=lost`, with a reason, transport detail and timestamp. A live
agent then shows `STATUS disconnected` in list and the dashboard; the inspector
shows recovery guidance. Secondary-client failures and optional naming errors do
not mark a healthy primary connection lost. An ordinary terminal exit still
briefly produces `LIVE stopped` and `STATUS -`, then automatically detaches
its clients and removes the wrapper (`LIVE dormant`).

Relay loss is sticky for that launch. New sends fail before submission and stay
saved for explicit retry; `--wait` returns failure immediately. Already accepted
messages are never replayed automatically. Exact-thread receipt checks and native
cancellation remain available independently of the relay. Kill the launch, then
resume its saved conversation; if the launch never bound a thread, kill it and
spawn again. Native work may survive a disconnected TUI. The relay never restarts
the daemon or reconnects to conceal a broken terminal session.

### One Codex conversation, one agent

Codex bridge shell calls use the persisted `codex-agent-v1` identity policy,
not the terminal launch ID. The native tool environment contains a stable mode,
universe, full agent UUID, directory, CLI/Python paths and pinned native storage.
`CODEX_THREAD_ID` must match the saved native binding. Missing or mismatched
proof is an error, never an operator fallback. `MV2_LAUNCH_ID` and launch-private
control/tmux addresses are absent. The attachment's `--remote` relay remains
private to that launch; there is no stable raw-native relay. Each new launch requests
`MV2_CLI`, `MV2_PYTHON` and `PATH` from the launching mv2 installation. The
requested map is kept in that launch's `request.json.agent_environment` and the
agent's latest `properties.json.agent_environment`; it is not an attestation of
the environment in an already-loaded shared conversation. Executable paths are
not part of actor authority. A shared native conversation can retain its old
tools while another client remains attached; close those clients, then use a
normal mv2 kill/resume to reload it after moving an installation.

Before a manual resume or completed stop, mv2 waits for the previous launch's
owned relay, status, recovery and startup roles to close. A bounded cleanup
failure remains visible and blocks handover. Per-launch cleanup and connection
records contain role, endpoint and timing metadata, never conversation content;
outside clients and the native daemon are left running. Each role and connection
records its owner's PID and UTC process start once, so PID reuse and unreaped
zombies do not block a handover. Older records use their post-creation timestamp
(or a recorded terminal exit) to rule out later processes; launch start time is
not such proof. Unclaimed recovery roles expire after 60 seconds. Unknown process
identities still block with the PID, command, start time and record path so the
operator can inspect the cause; no cleanup check signals an unrelated process.

`report` (including `status`) and every `inbox` form address that one agent;
`send` records its stable sender identity, including cross-universe sends. These
calls remain valid when an attachment stops, fails or is replaced. Reports use
last-writer-wins semantics; the inbox has one cursor. `inbox --wait` follows
ordinary attachment replacement but rejects a changed binding, native scope,
identity policy or removed agent. Agent calls never clear a launch refusal or
publish native status/usage. Management and delivery commands remain available,
with their exact target launch, pane, native service, permissions and receipt
checks unchanged.

Runner binding/runtime updates and raw attachment control remain launch-strict.
New bridge bootstrap does not ask the model to bind a session. In stable agent
mode, `session-bind` can only verify the identical already-bound native UUID,
without changing properties, timestamps, runtime or events. It cannot establish
or replace a binding. Plain-mode registration retains its existing strict
launch callback. Restart running Codex bridge agents once after updating:
`mv2 kill NAME`, then `mv2 resume NAME`, with no other client attached so Codex
releases and reloads the conversation with clean stable values. An old
conversation environment is refused with this restart guidance; there is no
automatic adoption, legacy authority bypass or redirect of old relay sockets.

A native competing writer is different from same-server sharing. Codex 0.163.0
can return `-32600` with `already has an active writer`; plain TUI shows
`This conversation is open in another app`. The adapter preserves that native
cause as the launch error in list, inspect, wait and send. Plain recognition is
limited to the actual active native refusal widget in an owned visible pane,
with its controls and hidden cursor, at the tested version; quoted history is
not a refusal. Same-server successful rejoin is never labelled in use.

The plain startup observer runs only for a supported native layout, with a
55-second observation window from the launch's start. The default 60-second
readiness wait leaves a five-second margin beyond observation; explicit wait
timeouts are unchanged. At the observation bound, an unrecognized screen
(including an unanswered trust prompt) falls back to process readiness with a
notice instead of blocking the launch. The readiness waiter can settle this
bound while a pane query is still in flight. The observer discards frames
completed after the bound; expiry and refusal publication serialize on the
runtime lock, so a late frame cannot reclassify an unverified startup. It stops sooner after validated
bridge binding, a recognized ordinary composer, native refusal, relay loss or launch exit. Unsupported versions
perform no pane polling; later TUI navigation is outside this startup observer.

Agent data callbacks (`report` and `inbox`) validate the selected properties and
actor or current-launch proof independently of harness configuration. A damaged
harness name or configuration cannot block those callbacks; all other identity
fields still require valid records. Reporting itself does not need runtime data
for its optional event metadata. Launch, terminal and delivery operations retain
their full record validation.

### Successful plain startup observation

An adapter may provide `launch_started(evidence) -> bool` beside its refusal
classifier. Both receive the same current owned-pane snapshot: actual CLI
version, visible text, cursor visibility and integer cursor coordinates. Core
checks refusal first. Only an exact `True` from the startup hook confirms the
additional plain startup condition and ends observation. All process and liveness
readiness checks still apply; screen evidence never establishes a binding,
changes permissions or clears a refusal. If neither classifier settles the screen
within the startup window, process readiness applies with a notice. For tested Codex 0.163.0, the active empty ordinary composer with its
configured status content or legacy context-percentage footer is startup evidence;
the status content need not contain a separator. The exact placeholder may be
clipped using the owned pane's measured width (at least 24 columns). Startup's
hint-only footer and deferred-submission hint are not success evidence. No Codex
config file is read for this classification. The real loading composer is not. Later
navigation after that success is no longer classified as a launch failure.
Unknown layouts retain the bounded startup window and existing relay-loss exit.

The native refusal remains latched for that launch, even if pressing R later
succeeds in Codex. Use `mv2 kill NAME --attachment-only` to end mv2's refused
frontend and await its owned cleanup, then explicitly resume through mv2 to ask
Codex again. This attachment-only exit must
not cancel the conversation's running work; `kill`/`stop` retains its distinct
explicit conversation-wide cancellation semantics. Definitely unsubmitted core startup
messages can fail with the native cause, but a plain frontend's retained wake
prompt may still submit on R: its acceptance stays uncertain until evidence or
frontend exit resolves it. Accepted/uncertain prompts are never replayed.

### Automatic recovery of idle Codex terminals

While a bound bridge is running and idle, the runner keeps a read-only proof for the exact thread and saved workspace, including its latest turn, native queue and background-terminal inventory. Observed activity invalidates it; a proof older than 15 seconds cannot authorize recovery. Startup messages and uncertain submissions prevent recovery. A normal terminal exit never triggers recovery.

On primary relay loss, a qualified launch schedules a detached worker. Transport failures retain a five-check bound against the existing endpoint, with delays of 0, 1, 2, 4 and 8 seconds. Codex restores previously loaded conversations asynchronously after a daemon restart. If a read validates the exact saved thread and workspace but reports `notLoaded`, recovery checks `thread/loaded/list` (including pagination) and reads the exact thread again. Only a fresh, validated `notLoaded` result permits up to 60 seconds of read-only waiting from the first pending observation; it is never treated as idle. If Codex does not restore the conversation within that bound, recovery skips with “Codex did not restore this conversation after its restart”. Once loaded, the same thread must pass the full qualification: idle with no active turn, approval/input wait, queue entries, background terminals or local frontend child processes, followed by a final exact-thread recheck. Unsupported, malformed, busy, unknown or mismatched observations fail closed immediately. Qualification never resumes, subscribes to or starts a conversation. The worker never invokes daemon start, stop, restart or update and never submits, cancels or replays a message. Sessions that cannot qualify retain sticky disconnection and manual recovery guidance.

When qualified, the worker terminates only the owned idle TUI, waits for its runner to exit, and respawns the same dead, tagged pane with a new launch generation. Attached viewers remain in that pane; the TUI redraws its saved conversation, but unsent composer drafts are not recoverable. Normal resume checks the saved workspace, exact native identity and effective requested permissions before accepting new input; automatic recovery cannot downgrade to plain mode. The wrapper status bar is refreshed and the original timestamp of last-known context is retained. A concurrent manual stop or resume cancels the old worker.

`runtime.json.auto_recovery` records checking, restarting, resuming, reconnected, skipped, failed or cancelled, with a reason and attempt count. `automatic_restarts` keeps the last five restart events and their original/new launch identities; no more than two automatic restarts are allowed in five minutes. Failure stops with a visible diagnostic instead of a restart loop. Successful recovery appears as a note in `mv2 list`, `inspect` and `doctor`; doctor reports failures/disconnections as problems and successful recovery separately in `notes`. Receipts remain unchanged. Existing launches without `attachment` and `native_service` runtime records remain ineligible; an explicit resume acquires this runner and its declarations.

Codex bridge context usage comes from the TUI connection's native
`thread/tokenUsage/updated` notifications. The adapter interprets only the exact
thread after its launch reply has validated the ID and workspace; the core relay
passes every frame to the TUI and publishes observations through the runner's
existing runtime update path. No extra subscription or thread resume is performed.
`runtime.json` stores `usage.context` (`tokens`, optional `window` and `percent`),
the native session ID, source and `reported_at`. The adapter's `get_usage` exposes
the reading only for the current launch handle and its exact native thread.
The last reading remains available with its original timestamp after
disconnection or launch end. Resume carries that same thread's newest valid
measurement into `runtime.json.last_usage`, including repeated resumes and
fallback to plain mode. The dashboard keeps showing the count and labels it
`(last known)` in the inspector until a fresh native notification replaces it,
including Codex's resume usage replay. This is retained measurement, not an
estimate. A changed thread ID never inherits another thread's count.

Context tokens are `tokenUsage.last.totalTokens`, not the cumulative `total`:
cached input and reasoning output are already included and are not added again.
The window is native `modelContextWindow`; a missing window stays unknown.
The percentage matches Codex 0.159.2's native TUI: subtract its 12,000-token
baseline from the used count and window, clamp and round the remaining percentage,
then report 100 minus that value as used. Thus the percentage can differ from
raw tokens divided by window. Compaction may reduce the count. Until this thread
has a saved measurement, usage displays N/A; a measured zero is zero.
Plain Codex can display a saved bridge reading but cannot refresh it.
Cumulative usage, cost and rate-limit reporting remain unavailable in this adapter.

Explicit `kill` (or `stop`) holds the recipient's launch lock during native cancellation, deletes its pending native
queue entries, interrupts its exact active turn and cleans its native background
terminals before closing its terminal. Confirmed queue cancellations are recorded
by message ID in the core inbox and cannot be replayed with `deliver --retry`.
Native cancellation is bounded to 128 queued submissions; errors leave a running
terminal attached with a diagnostic. An already-stopped wrapper can still be
removed, with native cancellation reported as unconfirmed. Cancellation requires a bound native session ID and a
reachable server. Closing a terminal by other means only disconnects its client
and can leave native work running. Closing a dashboard or detaching tmux never
calls native stop.

The per-launch runner owns a passive status watcher for bridge launches. Once
an exact native session ID is bound, the Codex adapter describes an initialize
and metadata-only `thread/read` exchange and interprets the snapshot and
`thread/status/changed` notifications. Core owns the connection and writes status
through the launch-checked runtime update path. Status notifications are broadcast
by app-server, so the watcher filters by the exact bound thread ID; it never calls
`thread/resume` to subscribe to turn/item events or reload a thread. The initial
read checks both thread ID and saved workspace before accepting observations.
`active` with no flags maps to `working`; `waitingOnApproval` or
`waitingOnUserInput` maps to `waiting`; `idle` maps to `idle`. Other states,
unbound sessions, watcher transport/protocol failures and plain launches remain `unknown`.
The separate primary-relay loss described above overrides that observation with
`disconnected` while the native process is alive.
The watcher never answers approvals or questions. It closes when the terminal
launch ends and clears its observation on disconnection; it never restarts the
daemon or reconnects automatically. Existing running launches need an explicit
stop/resume to acquire the new runner; status is detected, never agent-reported.

A daemon crash can interrupt every thread it hosts. Status reads and sends to a
live agent never restart it. Qualified idle-terminal recovery reuses only the existing endpoint as described above. Explicit resume, including message-triggered wake
of a stopped/dormant agent, may start the daemon and reloads saved history, not
old live processes/PTYs. One shared
daemon also shares resource pressure and a version upgrade's impact. SSH use is
local to the remote host: run Multiverse and Codex there. Cross-host routing and
shared-filesystem distributed ownership are not implemented. Existing standalone
live sessions are not killed/migrated automatically, and native session IDs must
not be independently adopted by two universes.

Pi's opt-in extension retains the native interactive terminal. It listens on a
core-assigned private Unix socket and receives actual messages with exact native
session and launch IDs. `sendMessage` uses the saved `steer`/`followUp` mode.
There are no polling timers or inbox reads in the extension.
Native acceptance is deduplicated in process; a bounded exact-transcript reader
recognizes receipt metadata in `custom_message.details.ids` across launches.
Pi converts only custom-message content into model input; details stay out of
that input ([native conversion](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/messages.ts)). Socket loss leaves acceptance unknown.
Core owns the private socket directory lifecycle. Pi's separate headless RPC mode
is not substituted for its interactive terminal.

Core frames each message for its recipient: `Message from <sender>:`, with
` (in universe <name>)` when the sender is in another universe, then the text.
Every provider sends `Message.framed` as the message content. Receipt identifiers
remain separate: Codex uses `clientUserMessageId`, Pi persists custom-message
details, and the Claude mod removes its transport tag before presenting text.

### Claude: a terminal-hosted bridge

Claude fits the service, session and attachment model above with one piece
missing on purpose: there is no service.

| Part | Claude |
| --- | --- |
| Native service | None. Core never sees a `SharedServiceSpec`, `native_service` or `rpc_proxy` for Claude: nothing is started, reused or reconnected to. |
| Exact session | The assigned UUID (`--session-id`, later `--resume`). A session never used has no saved conversation and restarts under the same ID. |
| Terminal attachment | The `claude` process in the agent's tmux pane; it does the work (`work_owner` is `terminal`). Claude declares no `AttachmentSpec`, so core starts no recovery worker, status observer or mouse preference for it. |
| Bridge | `--plugin-dir` (the `multiverse` mod), `--settings` and `--messaging-socket-path`: a `unix-lines` inbox hosted by the Claude process. It carries delivery, receipts, `ask`, status and usage reports, `/multiverse`, the status line and compaction. |

The bridge is part of the Claude process, not a service beside it. When the
process ends, the mod and its inbox end with it, so there is nothing external to
reconnect a replacement terminal to, and Claude never takes part in automatic
attachment recovery (`recovery_unavailable` reports that it declared none).
Restarting the same conversation from its transcript, with `mv2 resume` or a
message to a stopped agent, is an ordinary resume. This differs from Codex, whose
shared app-server outlives its terminal. Neither is a general model: a bridge says
what an integration can do, a service says where work lives.

A listing reconciles each row once and passes those same properties and runtime
records to usage measurement. Direct usage calls still reconcile independently.
Connection diagnostics retain their own process identity check; there is no
process-wide identity cache between reads.

Claude's own `/restart` (offered once a newer Claude Code is installed) runs the
new version in the same process, with a command line Claude rebuilds from the
session. Seen on 2.1.302, it keeps `--resume`, `--settings`, every
`--plugin-dir`, the model, the effort, `--append-system-prompt` and the
environment, and turns `--dangerously-skip-permissions` into the session's
permission mode and allowed tools; it drops `--messaging-socket-path`, `--name`
and `--add-dir`. Claude then listens on its own default socket
(`/tmp/cc-socks/<pid>.sock`, listed in `~/.claude/sessions/<pid>.json`) and
publishes the key for that path, while the mod and its status and usage reports
keep working. mv2's own socket then has no key, so the adapter follows Claude to
that socket: it reads `~/.claude/sessions/<pid>.json` for the agent's process and
uses its `messagingSocketPath` and key only when the file belongs to that exact
process: the start the runner recorded when it started Claude is the pid's start
now (so a reused pid fails), the file's `procStart` is that start in Claude's own
format (field 22 of `/proc/<pid>/stat`, in clock ticks since boot, on Linux;
`ps -o lstart=` in UTC on macOS), the process isn't a zombie, and the file's
`sessionId` is the agent's session. Only a running launch with a sessions file is
checked, so a listing runs no process query for stopped agents. For a running
one, each check costs one `ps` on macOS, and on Linux a `/proc` read plus one
`ps` once the file matches.
When it cannot follow (the file is another process's, or the launch was recorded
before mv2 kept the start time), `mv2 list`, `inspect` and `send` say "Claude
relaunched itself (/restart) without mv2's inbox; restart the agent". Restarting
the agent through mv2 (stop, then resume) brings all of mv2's flags back,
including `--name` and `--add-dir`.

`/clear` keeps the process but moves it to a new session ID, and Claude's inbox
drops a message addressed to another session. mv2 follows the move. With the
bridge, the mod reports the new ID when the cleared session starts, but only
once Claude's process file names it: Claude starts that write before the hook
runs without waiting for it, so the mod waits up to 5 s and reports nothing if
the file never changes. Core also reads that file for the exact process
(`observed_binding`, evidence `claude-process-file`) before it lists, measures,
sends to or resumes an agent, so a launch without the bridge follows a `/clear`
too. A report that names another session than the process file shows is
refused. Core then rebinds the agent and records the move in its session history
([session lineage](session-lineage.md)). Messages, usage and resume go to the new session;
the old conversation stays on disk as an earlier generation.

The pieces keep to their own side of the boundary:

- **Adapter** (`harnesses/claude/__init__.py`): launch flags, the inbox socket,
  the peer-token format, the `<<MV2>>` tag, the registered bridge id (`BRIDGE`,
  `claude-mod-v1`), receipts, `ask`, capabilities and readiness. Everything about
  a launch's bridge comes from one pure `Bridge` view (`launched`, `registered`,
  `ready`, `reason`), so capabilities, `send`, `ask` and readiness cannot disagree.
- **Mod** (`mod/`): hooks inside Claude. It reports itself once running
  (`runtime_update --bridge`) and reports status; nothing in it touches tmux.
- **Core**: `write_lines` transport, locks, runtime files, readiness polling and
  tmux. It has no Claude-specific type or name checks; a bridge id is data the
  integration reports.

Readiness has no `AttachmentSpec`: the optional `attachment_ready` hook answers
it. A launch with no inbox (plain, or an auto fallback) is ready once its process
runs. A bridged launch is ready when the mod has registered exactly `BRIDGE` for
this launch, so `spawn --wait` returns, and `send`, `ask` and receipts become
available, only then. Registration compares the exact constant: a well-formed id
reported for another integration (for example `pi-socket-v1`, or an unknown
`other-bridge-v1`) never makes a Claude launch ready or deliverable. The hook
reads only the session record, never the other capabilities (building them reads
`compaction.json`), because core asks on every readiness poll.

Status is whatever the mod reports and is never overwritten: core runs no
observer for Claude and produces no `NativeActivity` for it. A launch that fell
back to plain says so in its `native_mode`, its capabilities and `mv2 inspect`.
A plain launch (`inbox_bridge` false, or an auto launch Claude Code is too old
for) is launch-only by design: `send` and `ask` return `unavailable` with
`{"launch_only": true}` and `This agent is launch-only: REASON`, so core advises
no restart, which could not connect it. A bridged launch whose mod has not
registered carries no flag. With no agent (`mv2 capabilities --harness`), the
adapter answers for the harness's launches: launch-only for `false`, and for auto
or `true` the bridge, checked against the installed version only with `--probe`,
since reading it runs `claude`.

Claude bridge mode steers through the session's own peer inbox. `inbox_bridge` unset is auto: the bridge is used unless the installed
Claude Code is older than 2.1.280 (the oldest the mod was built on; a version that cannot be read does not count), in which case the
launch is plain and `native_mode.fallback_reason` says why. `true` makes that case an error and `false` never loads the mod. Each launch
passes `--messaging-socket-path` (the core-assigned private socket),
`--plugin-dir` for the source-owned `multiverse` mod, `--settings` with
`crossSessionInbound: accept` (Claude otherwise holds peer messages for approval
when the session skips permission prompts) and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`
(mods are early access). `send` reads the key Claude publishes for that socket and
writes one message at `next` priority, addressed to the exact session UUID. Its
framed text sits between `<<MV2 steer id=UUID>>` (or `follow-up`) and `<</MV2>>`:
the mode, which the mod honors, and the message ID for its receipt. The mod acts
only on tagged text and shows the model the text alone:

- A steer arriving mid-turn loses its tags in `session.receive`, so the queued
  message shows clean in the terminal. It merges at the next tool result as a
  queued command, and `session.append` stores that row without Claude's peer
  wrapper, so the model, the transcript and later resumes read the text alone.
- A steer arriving while idle, and every follow-up, is taken in `session.receive`
  and submitted with `$.prompt.submit({ text, asUser: true })`: the model reads it
  bare, as the person's own words, in a turn of its own once the current turn has
  ended. A steer queued mid-turn that no tool result took would start its own turn
  as a wrapped peer prompt, so `prompt.submit` drops it and submits it this way.
  The terminal shows these as ordinary prompts; the transcript names the plugin.

When a message's row enters the conversation, the mod writes
`claude-receipts/<message id>.json` in the agent's directory. `get_delivery`
reads it: `mv2 deliver --check` then records the message as accepted and
transcript-confirmed. A message without a receipt stays `unknown`.

`mv2 ask` sends the question tagged `<<MV2 ask id=UUID>>` over the same inbox.
The mod consumes it in `session.receive`, so nothing enters the conversation,
and answers with `$.model.fork`: one tool-less completion over the session's own
transcript as the main thread last sent it, reusing its prompt cache. It writes
`claude-asks/<id>.json` in the agent's directory, which the adapter waits for (up
to 120 s) and removes. Before the session's first response, or right after
`/clear`, there is nothing to answer from and `ask` reports unavailable. Core
resolves the session under the launch lock but waits for the answer outside it,
so sends and stop are not held.

The mod registers a native `/multiverse` command at session start (listed in the
typeahead with an argument hint, and runnable while a turn is running):

- `/multiverse` or `/multiverse list` prints `mv2 catalog` as command output, with
  no model turn.
- `/multiverse <name> [args]` loads that command, workflow or skill with `mv2
  command` and submits it as the person's own prompt.
- `/multiverse send <agent> <text>` runs `mv2 send` with this session's own
  `MV2_AGENT_ID`, so the recipient reads `Message from <this agent>`. The person
  types it to speak as this agent; to message an agent as themselves they type to
  it directly or run `mv2 send` from a shell. The result is `mv2 send`'s own text,
  shown as printed: what was delivered, or what was saved and why it was not.
- `/multiverse inbox` is a normal `mv2 inbox --json` read (the mod parses the JSON
  and formats it; the CLI's default output is readable text for people): it shows
  this agent's unread inbox-only messages and marks them read. The output is
  ordinary command output, so the model sees it on its next turn under Claude's
  "do not respond" caveat and acts only if asked. (The status line's count uses `mv2 inbox --peek --json` instead,
  which marks nothing read.)

`list`, `send` and `inbox` are reserved: a catalog entry with one of those names is
shadowed. Everything but `list` acts with the person's authority at this terminal
(it sends as this agent, submits a prompt as the person's, or changes what the
agent has read), so it runs only for a prompt the person typed (the composer, or
Remote Control); a plugin, the SDK or a peer can only list. The model can't run
it at all: Claude's Skill tool refuses plugin commands, so agents use `mv2`.

On Claude Code 2.1.292 or newer the launch also loads `claude/typeahead/`, a
second small plugin that fills the prompt box's typeahead while `/multiverse` is
typed: the first word offers `list`, `send`, `inbox` and the catalog's commands,
workflows and skills (`kind/name` when a bare name is shared or reserved); after
`send` it offers this universe's agents, except this one and any row built from a
damaged record, which `send` can't reach (a comma list
completes its last name); a catalog entry's arguments get nothing. It reads `mv2
catalog` and `mv2 list --json` in the background and keeps them five seconds, so
typing never waits on mv2, and a failed read offers no rows. It is separate
because it hooks `prompt.autocomplete`, which older Claude refuses: there the
adapter does not load it, and the mod keeps working without it.

The mod also keeps one status line under the prompt (Claude allows one per plugin;
the person's own `statusLine` command is untouched). Its items come from
`statusline.json` at the universe root, which every template ships and `mv2
refresh` manages (a local edit is kept):

```json
{"schema": 1, "items": {"agent": false, "phase": true, "summary": false, "inbox": true, "compaction": true}}
```

The order of the keys is the order on screen; a missing key keeps the default shown,
an unknown key is ignored, and an unreadable file means the defaults (logged once).
`agent` is the agent's name, plus `(universe)` only for an agent in a subverse (the
root universe is left out, the same rule as the tmux bar), `phase` and `summary` the agent's last `mv2 report`
(shown as one segment, `working: reading the diff`), `inbox` the unread inbox-only
count (only when above zero, `50+` at the cap), and `compaction` the built trees out of
all turns (`trees 70/76`, then ` · building` while a builder runs and any failure notes),
shown only for an agent launched with tree compaction enabled. A 2 s timer compares the size and time of four files (`properties.json`,
the inbox and its cursor, `statusline.json`) with no subprocess, reads only what
changed, and runs `mv2 inbox --peek --json` only when the inbox changed; the line also
refreshes at session start and at each turn's start and end. A running `/compact`'s
progress takes the slot while it lasts and the normal line returns after. Text
reported by other agents is shown with control characters removed and a summary is
shortened.

Plugins add items of their own ([plugins](plugins.md#status-lines)). A launch
declares them in `runtime.json` (`plugins.status_items`: each item's name, plugin
and whether the Claude line shows it by default), and the plugin writes the text to
`state/agents/<id>/status-items/<name>.txt`. A declared item is a key like the core
ones: `statusline.json` places it or turns it on or off, and one the file does not
name shows by its default, after everything else. The timer then also watches
`runtime.json` (status reports rewrite it often; the items are read again only when
they change) and the file of each item that is on; an item that is off is never
read. The text is its first line, cleaned and shortened like a summary, and an
empty or missing file shows nothing. A malformed record, a name that breaks the
manifest rule or is a core name, and a repeated name declare nothing.

The mod also registers the bridge (`BRIDGE`, `claude-mod-v1`) for its launch
(steering, `ask` and receipts are reported unavailable, and `--wait` does not
return, until it has) and reports status through `python -m
multiverse.tools.runtime_update --status`, which is not an `mv2` command: agents
report their phase with `mv2 report` but never set their status: `working` while a main-loop turn runs; `waiting` while an
`AskUserQuestion` or `ExitPlanMode` call, a permission prompt or an MCP
elicitation waits on the person; `idle` otherwise. A permission prompt is matched
to its call through the engine's `ask` verdict and ends when the call finishes, is
denied, or (Bash) draws its progress row; an approved call that runs briefly
without a progress row can read `waiting` until it finishes.

With the bridge, the `compaction` plugin ([compaction](compaction.md); the
`plugins/compaction` submodule), where it is installed and on, adds tree
compaction to Claude launches through its launch contribution:
`MV2_COMPACTION=1`, a `PreCompact` command hook, and its own Claude plugin
directory. That plugin answers the session's `/compact` with mv2's compacted
turns from the compaction plugin's
`install` helper action (`python -m multiverse.plugins run compaction
install`), showing progress and every error in the status line, as notices and
as toasts, and skips with the reason when it cannot. The `PreCompact` hook
refuses Claude's own compaction should it not answer, so a session never falls
back to it. Its `Stop` hook starts the background tree builder with the time the
turn ended. The plugin's Claude source (`sources/claude/`) reads this harness's
transcripts; the adapter itself has no compaction code. A Claude agent launched
before compaction became a plugin still calls `python -m multiverse.compaction
guard` from its settings: that module stays for a release and lets Claude
compact (restart the agent for tree compaction), and `mv2 doctor` names the
running agents that still use it. Compaction settings that cannot be used (invalid, or
builder settings Claude rejects) never stop a launch: that launch runs without
compaction, without the `PreCompact` guard, and records why as its
`native_mode` fallback, which the inspector shows. The `compaction` capability then reports the error, and
`mv2 doctor` names the file; `mv2 compaction` refuses until it is fixed.

Claude usage is measured from the session's own transcript under Claude's
`projects` directory, plus any subagent transcripts: each response's recorded
usage, counted once per response ID. It works with or without the bridge. The
adapter reads each transcript once, then only what was appended. A row that a
compaction stored again (with zero usage) never replaces its response's usage.

An accepted `send` means written to the Claude session inbox; the receipt
confirms delivery. The mod was tested against Claude Code 2.1.303; the mod API
may change between releases. Its offline tests (`mod/tests/*.test.ts`, run with
`claude plugin test src/multiverse/harnesses/claude/mod`) cover status, every
delivery path, `ask` and older tags; the Python suite runs them and `claude
plugin validate` when Claude Code is installed.

## Agent tmux status line

Every new or resumed agent wrapper has one status row and a window named after
the agent. Automatic window renaming and native terminal rename sequences are
disabled for that window. All other options are session-local: mv2 does not
change global tmux options or the user's tmux configuration. Existing wrappers
pick up the setup on resume.

| Harness | Default row (example) |
| --- | --- |
| Codex | `reader · codex · status waiting · phase working · inbox 2` |
| Pi / custom legacy | `reader · pi · phase working · inbox 2` |
| Claude | `reader · claude` |

`agent` means the name, plus `(universe)` only in a subverse (a universe directory
under `subverses/<name>`), matching the Claude mod. The harness segment is the
configured profile name. Inbox is omitted at zero. Codex context is **off by
default**; native status is omitted when unavailable, as in plain mode. When
status and phase have the same value, only the phase segment appears. Claude's
default avoids repeating the mod's phase, inbox and compaction line; a plain
Claude profile can opt into those available tmux items.

The existing universe-root `statusline.json` configures both displays. Keep
`schema: 1` and the top-level Claude `items` unchanged; all three templates add
this independent block after `items`:

```json
"tmux": {
  "refresh_seconds": 2,
  "items": {
    "agent": true,
    "harness": true,
    "status": false,
    "phase": true,
    "summary": false,
    "inbox": true,
    "context": false
  },
  "harnesses": {
    "codex": {"items": {"status": true}},
    "claude": {"items": {"phase": false, "inbox": false}}
  }
}
```

`tmux.items` key order sets display order; missing items are appended in default
order. Harness overrides apply by adapter family (`codex`, `claude`, `pi`), even
for a renamed profile. Built-in family defaults apply after common items, then
explicit family overrides apply. For example, turn off Codex status with
`"codex": {"items": {"status": false}}`, or opt into its context with
`"codex": {"items": {"context": true}}`. Unknown keys are ignored; missing,
partial or invalid configuration falls back to the corresponding defaults.
`refresh_seconds` rounds up and clamps to 1–60 seconds; non-numbers use 2.
`mv2 refresh` manages the template files and preserves local edits. The code
defaults also work with older files that have no `tmux` block.

`phase` and `summary` share a segment, positioned at the first enabled one.
Summary is optional and capped at 60 characters, cutting at a word where
possible. Text has terminal control sequences removed and tmux formats escaped.
`inbox` counts unread inbox-only messages without marking them read, capped at
`50+`; an unread tail over 1 MiB or invalid data shows `inbox ?`. Native-delivered
steer/follow-up messages do not count. Optional `context` shows measured latest
request tokens/window (for example `ctx 82k/200k`), only when available for the
current running launch and, for Codex, its exact thread. No compaction item is
provided in tmux; Claude's mod supplies that information.

The row uses tmux's asynchronous `#()` pull mechanism at `status-interval`, only
while attached. Each refresh starts one Python process with site loading
disabled, reads four small JSON files plus a bounded unread inbox tail if
enabled, and exits. It imports only the standard library, takes no locks,
performs no writes, and contacts neither mv2 nor the native app-server. This
avoids a push publisher in each lifecycle/report/inbox path and also picks up
file edits. Item choices and state changes appear on the next refresh; the
interval itself takes effect on spawn/resume.

A local 40-refresh measurement took a median 49 ms wall time and mean 35 ms CPU
per refresh while the test suite was running (about 1.7% of one CPU core for an
attached agent at two seconds). Detached wrappers do not poll; increasing the
interval reduces the attached cost.

Codex 0.160.x can separately opt into its built-in native footer without editing
`~/.codex/config.toml`. Add these two entries to the Codex harness's existing
`settings.args` array (preserving its other arguments):

```json
"-c", "tui.status_line=[\"thread-name\",\"run-state\",\"context-remaining\"]"
```

This applies to plain and bridge (`--remote`) launches/resumes. The selected
native values update live; the selection is a launch setting. `thread-name`
appears when named, including mv2's bridge-assigned name. Codex's footer accepts
its fixed item names, not arbitrary mv2 phase/inbox text or a shell renderer.
mv2 leaves this native footer choice to the user.

## Implemented capabilities and verification limits

| Operation | Codex bridge | Claude | Pi bridge | Plain/legacy |
| --- | --- | --- | --- | --- |
| Spawn / exact resume | Yes | Yes | Yes | Manifest-dependent |
| Wake stopped/dormant with an interactive resume prompt | Yes | Yes, with or without bridge | Yes | Built-in plain adapters: yes; legacy: unverified |
| Native session name on spawn / resume | `thread/name/set` | `--name`, with or without bridge | `--name`, with or without bridge | Plain Codex unavailable; legacy unverified |
| Explicit stop | Native queue cancellation/interrupt/terminal cleanup | Owned terminal | Owned terminal | Owned terminal |
| Safe-boundary steer | Yes | Bridge: peer inbox + mod | Yes | Unavailable |
| After-turn send / idle wake | Yes | Bridge: follow-up after the turn; idle wake | Yes | Unavailable |
| Observe launch / terminal | Yes | Yes | Yes | Yes |
| Receipt reconciliation | Bounded queue/history lookup | Bridge: mod receipt when the row is stored | Exact transcript/current acceptance | Unavailable |
| Native status | Bridge: working / waiting / idle from app-server | Bridge: working / waiting / idle from the mod | Lifecycle events | Unknown |
| Latest context usage | Native latest-request tokens, window and percentage | Transcript tokens; bridge adds native window and percentage | Unavailable | Claude transcript tokens; Codex retains a saved bridge reading |
| Measured cumulative usage | Unavailable | Session transcript (with or without the bridge) | Unavailable | Unavailable |
| Isolated ask | Unavailable | Bridge: `$.model.fork` over the transcript | Unavailable | Unavailable |

Unsupported usage displays N/A, not zero. The usage representation includes
cache-inclusive input, output, total, optional cache subsets, source, timestamp
and completeness. No token estimation or native-history guessing is performed.
An unsupported ask never becomes ordinary input or launches a separate agent.
Claude steering needs the mod to load; basic lifecycle does not depend on it.

Contract tests cover a provider implementing only the public protocol, shared
CLI/backend dispatch, exact handles across resume, optional prompts, side-question
isolation, saved-message failure/retry and explicit unsupported operations. Native
Codex/Pi timing tests exercise the public `send` tool using installed native TUIs,
local scripted providers and real disposable shell tools. Full suite:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

References: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Pi extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md),
[Claude channels](https://code.claude.com/docs/en/channels-reference),
[Claude Mod types](https://github.com/anthropics/claude-code/blob/main/mods/types/claude-code.d.ts).
