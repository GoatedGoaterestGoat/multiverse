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
the first message); a session stopped before then restarts fresh under the same
UUID with `--session-id`, without replaying its task, and says so in the agent's
details. Pi owns an
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
from observed liveness. The report CLI prints one line (`Reported working: <summary>`)
without echoing task/properties data; its Python `run()` result remains the full
record. `task` is a compatibility name for the optional initial
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
lock so native binding can finish. Every child must first survive a two-second
startup check (`runtime.startup_checked`); plain/legacy launches then require a running child;
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
| unknown | Submission may have succeeded; never blindly resent, including with `--retry`. |

The immediate tool result `native_queued` maps to accepted; `unavailable`/`error`
map to failed. Transcript confirmation and an answer are independent evidence,
never inferred from acceptance.

```sh
mv2 inbox                         # show unread inbox-only messages and mark them read
mv2 inbox --all                    # browse full raw history and delivery evidence
mv2 send worker --text 'Later' --delivery inbox-only
mv2 deliver worker --retry        # retry this recipient's definite failures
mv2 deliver worker --check        # reconcile pending/uncertain deliveries, record accepted receipts
```

Ordinary inbox reads return sender, text, time and delivery mode, without message
identifiers. They advance the read position under the inbox lock, including
across skipped native records, stopping at the last complete record in the page.
Concurrent readers cannot consume the same manual message twice. Stale launch
callbacks cannot read or advance the current launch's inbox. An interrupted
caller can recover already-consumed text from history.

`inbox --peek` returns the same unread page (up to 50 messages) without changing the read position; it cannot be combined with `--all` or `--after`.

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
is not an accepted setting. Claude and Pi retain their opt-in boolean behavior.
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
Homebrew CLI. A CLI/daemon version difference is reported, while actual protocol
checks decide whether the bridge is usable. Codex owns daemon
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
`thread/queue/add`. A racing busy steer is never converted to a fresh turn.
Codex 0.159.3 and 0.160.0 can leave automatic queue draining paused after an
interrupt: a later `queue/add` accepts a message while the thread remains idle.
This reproduces through direct native RPC, independently of mv2. For a thread
observed idle before submission, mv2 checks the queue after acceptance. If the
new message is its only entry, mv2 asks `thread/queue/start` to start that exact
entry. It never resubmits the text, skips older work or restarts a thread. Native
queue start rejects active/pending turns. If auto-draining already took the
entry, no start is needed. A rejected explicit start preserves acceptance and
reports a diagnostic for `deliver AGENT --check`, rather than making it retryable.
If other queued work exists, mv2 preserves it and reports that an interrupted
queue may need native continuation. Receipt checks never start or replay work.
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
template, like base and testing, uses auto; existing unset profiles also try the bridge on their next launch.

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
live agent never restart it. Explicit resume, including message-triggered wake
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

The mod registers a native `/multiverse` command at session start (listed in the
typeahead with an argument hint, and runnable while a turn is running):

- `/multiverse` or `/multiverse list` prints `mv2 catalog` as command output, with
  no model turn.
- `/multiverse <name> [args]` loads that command, workflow or skill with `mv2
  command` and submits it as the person's own prompt.
- `/multiverse send <agent> <text>` runs `mv2 send` with an empty `MV2_AGENT_ID`,
  so the recipient reads `Message from user`: the person typed it, so it is not
  attributed to this agent.
- `/multiverse inbox` shows this agent's unread inbox-only messages with `mv2 inbox
  --peek`, which marks nothing read. Like any local command output, the text also
  reaches the model on its next turn, under Claude's "do not respond" caveat.

`list`, `send` and `inbox` are reserved: a catalog entry with one of those names is
shadowed. Sending and submitting speak as the person, so they run only for a prompt
the person typed (the composer, or Remote Control); a plugin, the SDK or a peer
can only read.

The mod also keeps one status line under the prompt (Claude allows one per plugin;
the person's own `statusLine` command is untouched). Its items come from
`statusline.json` at the universe root, which every template ships and `mv2
refresh` manages (a local edit is kept):

```json
{"schema": 1, "items": {"agent": false, "phase": true, "summary": false, "inbox": true, "compaction": true}}
```

The order of the keys is the order on screen; a missing key keeps the default shown,
an unknown key is ignored, and an unreadable file means the defaults (logged once).
`agent` is the name and universe, `phase` and `summary` the agent's last `mv2 report`
(shown as one segment, `working: reading the diff`), `inbox` the unread inbox-only
count (only when above zero, `50+` at the cap), and `compaction` the built trees out of
all turns (`trees 70/76`, then ` · building` while a builder runs and any failure notes),
shown only for an agent launched with tree compaction enabled. A 2 s timer compares the size and time of four files (`properties.json`,
the inbox and its cursor, `statusline.json`) with no subprocess, reads only what
changed, and runs `mv2 inbox --peek` only when the inbox changed; the line also
refreshes at session start and at each turn's start and end. A running `/compact`'s
progress takes the slot while it lasts and the normal line returns after. Text
reported by other agents is shown with control characters removed and a summary is
shortened.

The mod also registers the `claude-mod-v1` bridge for its launch (steering is
reported unavailable until it has) and reports status through `python -m
multiverse.tools.runtime_update --status`, which is not an `mv2` command: agents
report their phase with `mv2 report` but never set their status: `working` while a main-loop turn runs; `waiting` while an
`AskUserQuestion` or `ExitPlanMode` call, a permission prompt or an MCP
elicitation waits on the person; `idle` otherwise. A permission prompt is matched
to its call through the engine's `ask` verdict and ends when the call finishes, is
denied, or (Bash) draws its progress row; an approved call that runs briefly
without a progress row can read `waiting` until it finishes.

With the bridge, a universe's `compaction.json` can enable tree compaction for
the Claude harness ([compaction](compaction.md)). The launch then sets
`MV2_COMPACTION=1` and adds a `PreCompact` command hook. The mod answers the
session's `/compact` with mv2's compacted turns from `python -m
multiverse.compaction install`, showing progress and every error in the status
line, as notices and as toasts, and skips with the reason when it cannot. The
`PreCompact` hook refuses Claude's own compaction should the mod not answer, so a
session never falls back to it. The mod's `Stop` hook starts the background tree
builder with the time the turn ended. The adapter's `compaction(session)` returns
the Claude implementation of the compaction protocol (`claude/compaction/`).
A `compaction.json` that cannot be used (invalid, or builder settings Claude
rejects) never stops a launch: that launch runs without compaction, without the
`PreCompact` guard, and records why as its `native_mode` fallback, which the
inspector shows. The `compaction` capability then reports the error, and
`mv2 doctor` names the file; `mv2 compaction` refuses until it is fixed.

Claude usage is measured from the session's own transcript under Claude's
`projects` directory, plus any subagent transcripts: each response's recorded
usage, counted once per response ID. It works with or without the bridge. The
adapter reads each transcript once, then only what was appended. A row that a
compaction stored again (with zero usage) never replaces its response's usage.

An accepted `send` means written to the Claude session inbox; the receipt
confirms delivery. The mod was tested against Claude Code 2.1.288; the mod API
may change between releases. Its offline tests (`mod/tests/*.test.ts`, run with
`claude plugin test src/multiverse/harnesses/claude/mod`) cover status, every
delivery path, `ask` and older tags; the Python suite runs them and `claude
plugin validate` when Claude Code is installed.

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
