# Harness operations

Multiverse exposes the same operations to its CLI, importable tools and TUI backend:

| Operation | Contract |
| --- | --- |
| `spawn` | Start a native terminal in the saved workspace with bootstrap, optional initial prompt and model override; apply the mv2 agent name where supported. |
| `kill` / `stop` | Cancel native work, terminate the owned native process, wait for exit, then remove its tmux wrapper; never stop a shared daemon. |
| `resume` | Reopen the exact saved conversation, never the latest or a replacement session; reapply the mv2 agent name where supported. |
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
must not guess a recent session. Claude receives an assigned UUID; Pi owns an
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
- `claude/`: launch/resume preferences, opt-in peer-inbox steering and its Claude mod (`mod/`).
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
from observed liveness. `task` is a compatibility name for the optional initial
prompt, not a scheduler object. It is no longer copied into the status summary.

## Lifecycle paths

`tools/spawn.py` and `tools/resume.py` call `agents.start()`. Spawn creates a new
agent UUID and saves its name, prompt, harness, model and workspace. Resume loads
that record, keeps its workspace and exact native identity, and accepts an
optional model override. Under the agent's `.launch.lock`, core refuses another
launch unless observation says the previous one is stopped or dormant. Unknown
liveness also blocks a duplicate launch.

The adapter first validates and describes its command. Core then records a fresh
launch ID, bootstrap, prompt and request; starts/reuses a shared native service
when required; creates a detached tmux session running `runner.py`; and tags the
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
lock so native binding can finish. Plain/legacy launches require a running child;
bridge launches also require the exact native ID and delivery readiness. For
Codex, `running` means the relay validated the native reply and effective grants.
Claude/Pi wait for their bridge registration capability. A changed generation is
refused. The usual result carries the latest runtime/session plus `ready` and
`ok`; failures return `error` and CLI exit 1. Timeout also sets `timed_out`, keeps
the launch intact and never retries it. This waits for launch readiness, not
completion of the first model turn. The dashboard remains asynchronous.

`tools/kill.py` and its `stop` alias call the same core shutdown operation.
Core verifies the pane's universe, agent and launch tags under the launch lock,
sets `state=ending`, clears observed status, and enables `remain-on-exit` even
for panes launched by older versions. `LIVE` is then `ending` and `STATUS` is
`-`. Native status callbacks cannot make an ending row appear active again.

The adapter's `stop()` cancels work hosted outside the terminal first. Codex
bridge cancels up to 128 queued submissions on the exact thread, interrupts its
active turn and cleans background terminals. A validated already-unloaded
thread needs no cancellation and is never resumed by kill. A failure retains
the terminal and reports an error. The shared daemon and sibling agents remain
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
set `remain-on-exit`; Ctrl+C or a native exit therefore retains a stopped pane.
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
resume; use `spawn_args` for those initial settings.

Before accepting the native reply, the relay verifies that each requested
permission matches the effective `sandbox` and `approvalPolicy` returned by
app-server. Bridged launches remain in `starting` until validated binding;
`send` refuses steer/follow-up input while the launch is still binding. A
mismatch records an actionable runtime error, closes that client connection,
and prevents new mv2 input for the unvalidated launch. No automatic downgrade,
empty model turn, turn-time grant fallback, or shared-daemon restart is used.

Codex 0.159.3 applies overrides when loading a thread but can retain the existing
grant when rejoining an already-loaded thread. If validation fails, stop the
mv2 agent, close any other clients subscribed to that exact thread, let it
become inactive and wait for native unload before resuming. The default delay
is 60 seconds, configurable by native `thread_unload_delay_secs`; both no
subscribers and inactivity must hold, and a new subscription cancels the timer.
The native daemon and other threads stay running. See the
[app-server protocol](https://learn.chatgpt.com/docs/app-server) and the
[0.159.3 unload implementation](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/app-server/src/request_processors/thread_lifecycle.rs).

Verification uses an installed Codex 0.159.3 TUI, a disposable native home and
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
mv2 send worker --text 'Consider this now' --delivery steer
mv2 send worker --text 'Next, inspect the tests' --delivery follow-up
mv2 ask worker --question 'What would this alternative cost?'
mv2 get-usage worker
mv2 list --json
mv2 capabilities --agent worker --probe
```

The corresponding Python modules have `run(...)` functions and standalone entry
points, e.g. `python -m multiverse.tools.ask`. They do not import Textual.
`LocalBackend` delegates to the same tools, with no provider-name branches.
Public capabilities are `spawn`, `resume`, `stop`, `send_steer`, `send_follow_up`,
`ask`, `get_usage`, and `get_delivery`. The `session_name` capability describes
naming during spawn/resume, not a separate rename operation. Claude and Pi pass
`--name` in both modes. Codex bridge calls `thread/name/set` after validating and
binding the launch reply; plain Codex has no CLI naming flag. Legacy manifests
remain unchanged and report naming unverified. Capabilities describe adapter
support, not confirmation of an individual launch's display name. Support may
depend on configuration and a registered, running native session. Discovery returns supported/unavailable/error plus a reason.

`send` first persists the text, sender, timing and an internal receipt identifier,
then calls the adapter. Its result includes `saved`, `state`, and a delivery
summary without message identifiers. Unavailable or failed native delivery
produces `ok: false` and a nonzero CLI exit while retaining the saved message.
Use recipient-scoped recovery instead of repeating `send`, which creates a new
message.

One logical inbox lives in `inbox.jsonl` (immutable messages),
`inbox-state.json` (delivery attempts and receipts), and `inbox.cursor` (the
ordinary read position). These files are core-owned; providers have no mailbox
or retry database.

| Submission state | Meaning |
| --- | --- |
| pending | Saved; no completed submission attempt. |
| submitting | Attempt persisted before calling native API; a crash makes acceptance unknown. |
| accepted | Native API accepted; may still be queued inside the harness. |
| failed | Definite rejection or pre-submission failure; explicit retry permitted. |
| unknown | Submission may have succeeded; never blindly resent, including with `--retry`. |

The immediate tool result `native_queued` maps to accepted; `unavailable`/`error`
map to failed. Transcript confirmation and an answer are independent evidence,
never inferred from acceptance.

```sh
mv2 inbox                         # show unread inbox-only messages and mark them read
mv2 inbox --all                    # browse full raw history and delivery evidence
mv2 send worker --text 'Later' --delivery inbox-only
mv2 deliver worker --retry        # retry this recipient's definite failures
mv2 deliver worker --check        # reconcile pending/uncertain native receipts
```

Ordinary inbox reads return sender, text, time and delivery mode, without message
identifiers. They advance the read position under the inbox lock, including
across skipped native records, stopping at the last complete record in the page.
Concurrent readers cannot consume the same manual message twice. Stale launch
callbacks cannot read or advance the current launch's inbox. An interrupted
caller can recover already-consumed text from history.

`inbox --all` is a non-consuming raw view, including internal identifiers and
native delivery evidence. Page through it with `--after CURSOR`; ordinary reads
page forward automatically. Diagnostics and recovery use the non-consuming view.
Reading history does not cancel pending native delivery, so viewing a message
there and later receiving it natively can produce a duplicate.

`--queue-only` is a compatibility alias for `inbox-only`. Native-assigned messages
never also appear in ordinary mailbox reads, including failed/pending submissions.
They remain inspectable through `inbox --all`; no automatic switch to manual
handling occurs. Native delivery presents only the core sender line and text.

Per-recipient sends serialize persistence and submission. Urgent messages may
overtake native follow-ups. Existing schema-less logs retain manual semantics
without rewriting their contents. Retired per-message read markers and reply
references are ignored; an existing byte cursor remains the read position.
Existing `native-delivery.json` receipts remain readable for compatibility;
accepted or uncertain old attempts are not replayed. New attempts use inbox state.

`deliver AGENT --retry` selects definite failures; `--check` selects pending,
interrupted or unknown submissions and queries receipts without submitting.
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

Shared preferences are `executable`, `model`, `args`, `spawn_args`, `resume_args`,
and `unset_env`. Codex, Pi and Claude additionally support `inbox_bridge`. Native flags,
models, permissions and workspaces are preserved. Inherited `MV2_*`,
`CODEX_THREAD_ID` and `CLAUDECODE` identities are scrubbed before child execution.
Bundled defaults also unset `NO_COLOR`: the tmux server keeps the environment of
whatever first started it, often an agent's tool shell, and agent panes are
interactive terminals. Setting `unset_env` replaces this list, which opts back in.
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
Homebrew CLI. A CLI/daemon version mismatch fails with a diagnostic; it is never
repaired by disrupting other clients. Codex owns daemon
persistence and native history. No Multiverse-specific Codex home is created.

The selected Codex home is saved for exact resume even if another monitor has a
different environment. Agent workspace, model and initial permissions remain
per-thread. The shared server must not inherit an agent identity: startup scrubs
`MV2_*`, native parent IDs and configured exclusions. Before terminal launch the
adapter reads native effective environment exclusions, preserves them, adds
identity exclusions, and passes the current launch's `MV2_*` values through
per-thread shell configuration. Native configuration files are never rewritten.
Other daemon process environment (provider credentials, proxies and environment
used by integrations) remains shared; it does not change to match each monitor.
Different process-level environments require separately configured native homes.

Exact thread ID/workspace are checked before queueing or steering. Urgent busy
input uses `turn/steer` with `expectedTurnId`; normal/idle input uses
`thread/queue/add`. Races return errors without fresh-turn or resume fallback.
Approvals remain unanswered by the delivery client. Core bounds transport
requests, frames and deadlines. Explicit sandbox/approval flags in `args` or
`resume_args` are applied and verified on bridge resume as described above;
without them, native TUI permission fields pass through unchanged. Unsupported
profile/directory resume overrides are diagnosed, never silently removed. Whole-table `shell_environment_policy`
CLI overrides are rejected in bridge mode; individual keys remain supported.
Codex 0.159.2 rejects `--add-dir` with `--remote` and drops the
`developer_instructions` CLI override from remote thread requests. The adapter
therefore returns an optional `RpcProxySpec` in its `CommandSpec`. Core serializes
it in the launch request, creates a private 0700 socket directory, and starts the
relay in the runner before starting the TUI. The provider supplies pure request
and reply interpretation; core owns WebSocket connections, backpressure, size
limits, error recording, exact session binding and cleanup.
Interactive relay messages have a separate 64 MiB limit and a two-frame receive
queue: native resume replies include conversation history and can exceed the
1 MiB budget used for small control RPCs. Oversized messages still close the
connection with a runtime diagnostic.

The relay adds the bootstrap to the TUI's `thread/start` and exact `thread/resume`
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
Switching or forking into a different thread requires a separate
Multiverse launch. The relay closes with the launch and never restarts the shared
daemon or reconnects automatically. Plain launches have no relay. The development
template enables the bridge by default; existing universes must opt in explicitly.

Codex bridge context usage comes from the TUI connection's native
`thread/tokenUsage/updated` notifications. The adapter interprets only the exact
thread after its launch reply has validated the ID and workspace; the core relay
passes every frame to the TUI and publishes observations through the runner's
existing runtime update path. No extra subscription or thread resume is performed.
`runtime.json` stores `usage.context` (`tokens`, optional `window` and `percent`),
the native session ID, source and `reported_at`. The adapter's `get_usage` exposes
the reading only for that exact launch and session. The last reading remains
available with its timestamp after disconnection or launch end; a new launch
waits for its own native notification, including Codex's resume usage replay.

Context tokens are `tokenUsage.last.totalTokens`, not the cumulative `total`:
cached input and reasoning output are already included and are not added again.
The window is native `modelContextWindow`; a missing window stays unknown.
The percentage matches Codex 0.159.2's native TUI: subtract its 12,000-token
baseline from the used count and window, clamp and round the remaining percentage,
then report 100 minus that value as used. Thus the percentage can differ from
raw tokens divided by window. Compaction may reduce the count. Until a measurement
arrives, usage displays N/A; a measured zero is zero. Plain Codex, cumulative usage, cost and
rate-limit reporting remain unavailable in this adapter.

Explicit `kill` (or `stop`) holds the recipient's launch lock during native cancellation, deletes its pending native
queue entries, interrupts its exact active turn and cleans its native background
terminals before closing its terminal. Confirmed queue cancellations are recorded
by message ID in the core inbox and cannot be replayed with `deliver --retry`.
Native cancellation is bounded to 128 queued submissions; errors leave the terminal
attached with a diagnostic. This requires a bound native session ID and a
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
unbound sessions, transport/protocol failures and plain launches remain `unknown`.
The watcher never answers approvals or questions. It closes when the terminal
launch ends and clears its observation on disconnection; it never restarts the
daemon or reconnects automatically. Existing running launches need an explicit
stop/resume to acquire the new runner; status is detected, never agent-reported.

A daemon crash can interrupt every thread it hosts. Multiverse does not restart
it from status reads or sends and never automatically resumes conversations.
Explicit resume reloads saved history, not old live processes/PTYs. One shared
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

Claude bridge mode steers through the session's own peer inbox. Each launch
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

The mod also registers the `claude-mod-v1` bridge for its launch (steering is
reported unavailable until it has) and reports status through `python -m
multiverse.tools.runtime_update --status`, which is not an `mv2` command: agents
report their phase with `mv2 report` but never set their status: `working` while a main-loop turn runs; `waiting` while an
`AskUserQuestion` or `ExitPlanMode` call, a permission prompt or an MCP
elicitation waits on the person; `idle` otherwise. A permission prompt is matched
to its call through the engine's `ask` verdict and ends when the call finishes, is
denied, or (Bash) draws its progress row; an approved call that runs briefly
without a progress row can read `waiting` until it finishes.

Claude usage is measured from the session's own transcript under Claude's
`projects` directory, plus any subagent transcripts: each response's recorded
usage, counted once per response ID. It works with or without the bridge. The
adapter reads each transcript once, then only what was appended.

An accepted `send` means written to the Claude session inbox; the receipt
confirms delivery. The mod was tested against Claude Code 2.1.286; the mod API
may change between releases. Its offline tests (`mod/tests/*.test.ts`, run with
`claude plugin test src/multiverse/harnesses/claude/mod`) cover status, every
delivery path, `ask` and older tags; the Python suite runs them and `claude
plugin validate` when Claude Code is installed.

## Implemented capabilities and verification limits

| Operation | Codex bridge | Claude | Pi bridge | Plain/legacy |
| --- | --- | --- | --- | --- |
| Spawn / exact resume | Yes | Yes | Yes | Manifest-dependent |
| Native session name on spawn / resume | `thread/name/set` | `--name`, with or without bridge | `--name`, with or without bridge | Plain Codex unavailable; legacy unverified |
| Explicit stop | Native queue cancellation/interrupt/terminal cleanup | Owned terminal | Owned terminal | Owned terminal |
| Safe-boundary steer | Yes | Bridge: peer inbox + mod | Yes | Unavailable |
| After-turn send / idle wake | Yes | Bridge: follow-up after the turn; idle wake | Yes | Unavailable |
| Observe launch / terminal | Yes | Yes | Yes | Yes |
| Receipt reconciliation | Bounded queue/history lookup | Bridge: mod receipt when the row is stored | Exact transcript/current acceptance | Unavailable |
| Native status | Bridge: working / waiting / idle from app-server | Bridge: working / waiting / idle from the mod | Lifecycle events | Unknown |
| Latest context usage | Native latest-request tokens, window and percentage | Transcript tokens; bridge adds native window and percentage | Unavailable | Claude transcript tokens; otherwise unavailable |
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
