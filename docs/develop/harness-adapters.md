# Harness adapters

This page explains how to add or change a harness. It covers the adapter contract, session modes, bridges, capabilities and delivery receipts. [design/harness-adapters.md](../design/harness-adapters.md) has the full contract and the reasons behind it.

## Pick a kind of harness

| Kind | Gives you | Needs |
| --- | --- | --- |
| Schema-1 command harness | Spawn, resume and stop of any CLI in a pane | One `harness.json`; no code |
| Built-in adapter | Message delivery, status, usage, receipts, a bridge | A package in `src/multiverse/harnesses/` |

Start with a [command harness](#command-harnesses). Write an adapter when you need mv2 to talk to the running session.

## The adapter interface

`HarnessAdapter` in `harnesses/types.py` is the contract. An adapter has four attributes: `name`, `session_mode`, `bootstrap` (extra text appended to the universe bootstrap) and `default_model`. Its methods are:

| Method | Effect | Returns | Must |
| --- | --- | --- | --- |
| `spawn(context)` | `act` | `CommandSpec` | Build the launch for a new session from `context.values` and `context.model`. |
| `resume(session, context)` | `act` | `CommandSpec` | Reopen exactly `session`. Raise `ValueError` if the saved native ID or session file is missing or wrong, and never fall back to the latest session. When `context.resume_prompt` is set, pass it as the first interactive prompt (wake). |
| `stop(session, executor)` | `act` | `NativeResult` | Cancel work running outside the terminal, such as on a shared server. Core then ends the process and the pane. Return `ok` when there is nothing to cancel. |
| `capabilities(session, executor=None)` | `observe` | `dict[str, Capability]` | Give one entry per name in `CAPABILITIES`, with a reason when not `supported`. Stay cheap without an executor. With one (`mv2 capabilities --probe`), you may ask the native side. |
| `send(session, message, executor)` | `act` | `NativeResult` | Deliver `message.framed` to the exact session, at `message.delivery` (`steer` or `follow-up`). |
| `get_delivery(session, message_id, executor)` | `observe` | `NativeResult` | Look up the receipt for one message, without sending anything. |
| `ask(session, question, executor)` | `spend` | `NativeResult` | Answer a side question without adding it to the conversation, or return `unavailable`. |
| `get_usage(session, executor=None)` | `observe` | `NativeResult` | Return measured context and token usage, or `unavailable`. Never estimate. |

`EFFECTS` in `harnesses/types.py` declares these classes for core dispatch.
Core checks the current plugin access limit before calling the adapter, and
before a launch or send writes records. Optional identity, storage, readiness,
receipt and usage evidence methods are `observe`; launching a relay and
continuing a native queue are `act`.

An `observe` call may reconcile core's records with verified native evidence,
such as recording a detected `/clear` in `sessions.jsonl` or a delivery receipt.
It does not start or stop a process, switch a conversation, adopt the caller's
storage, submit a message or mark an inbox read. `deliver --check` is observe;
retrying or dismissing deliveries and consuming an inbox require act. A policy
refusal raises the API's `AccessDenied`, rather than becoming a native delivery
failure. These checks guard plugin API and dispatch mistakes; they are not a
sandbox for Python code.

Core also looks for these optional methods and attributes:

| Method or attribute | Used for |
| --- | --- |
| `family` | Which status line defaults apply: `claude`, `codex`, `pi` or `''` |
| `session_home(environment)` | Resolve an explicit candidate storage home; return `None` when its supplied environment cannot identify one. |
| `session_storage_evidence(session, home)` | Read-only proof that this exact conversation exists under an absolute candidate home: `ok` with `value.present` true or false, or `unknown` with a reason for incomplete evidence. An absent answer adds `value.store_read`: exactly true proves the primary conversation store existed and was read completely (including other existing stores the finder checks); false means it was missing. Recorded candidates require this proof for `--start-fresh`; normal adoption and caller-only destinations do not. A reason may name the missing store. |
| `resume_notice(session, caller_environment, effective_environment)` | A notice about the selected storage or conversation. If it accepts the optional keyword `fresh_start`, core passes false after announcing its own proven fresh start; other notices still apply. |
| `allow_recorded_launch(session)` | Explicitly allow core to verify retained bridge-launch identity for data callbacks; exactly `True` opts in, absent/default is false. The current adapter policy must equal the saved policy; this never grants control authority. |
| `attachment_ready(session)` | Whether a launch is ready to take messages |
| `launch_proxy(settings)` | The protocol of a launch relay, when the spec has `rpc_proxy` |
| `status_watch(session)` | A passive status stream the runner follows |
| `attachment_recovery(session)` | A read-only check that an idle terminal can be reconnected |
| `retained_usage(session, runtime)` | The last usage reading, kept across resume |

Codex checks the native thread UUID in a rollout's metadata header under the candidate's `sessions/` or `archived_sessions/` folder, including reverted rollouts with a separate rollout ID. A matching filename alone is insufficient. Unreadable or malformed metadata, compressed-only or linked storage, entries that disappear during traversal and exhausted scan limits return `unknown`, never absence. Core selects candidate homes and owns adoption and notices; this check starts no native process and changes no files.

### Types

| Type | Fields you use |
| --- | --- |
| `LaunchContext` | `mode`, `values` (the [placeholders](../reference/files.md#schema-1)), `model`, `environment`, `endpoint`, `executor`, `resume_prompt`, `storage_keys` |
| `SessionHandle` | `agent_id`, `harness`, `workspace`, `launch_id`, `native_id`, `session_file`, `state`, `endpoint`, `bridge`, `native_mode` |
| `CommandSpec` | `argv`, `unset_env`, `environment`, `control_endpoint`, `shared_service`, `rpc_proxy`, `native_mode`, `fallback`, `attachment` |
| `NativeResult` | `state` (`ok`, `native_queued`, `pending`, `unavailable`, `error`, `unknown`), `reason`, `value` |
| `Capability` | `state` (`supported`, `unavailable`, `error`), `reason`, `mechanism`, `required` |

For legacy storage adoption, `LaunchContext.storage_keys` lists proven harness storage variables, never `HOME`. A default-storage adoption can retain `HOME` in the launch environment without adding it to this mask. `None` keeps the complete selection used by existing callers; an empty tuple proves none. A listed key absent from `environment` is known unset; an omitted key is unknown and retains native inheritance. Adapters still enforce their own conversation's identity requirements.

### Rules

- **Only describe.** Return specs and results. Core starts processes, owns tmux, writes `runtime.json` and holds the locks. Reach the native side only through the executor: `executor.request`, `executor.rpc` or `executor.write_lines`.
- **No shell.** `argv` runs as given, so prompts and IDs stay literal.
- **Exact sessions.** Every operation targets `session.native_id` or `session.session_file`. Never search for a recent session.
- **Fail closed.** Report `unavailable` with a reason rather than guessing. Core always removes `CODEX_THREAD_ID` and `CLAUDECODE` from the launch environment. A harness's `env` can't set `MV2_*`, `PATH` or `HOME`.

## Session modes

`session_mode` says who chooses the native session ID and how resume finds it.

| Mode | Native ID | Resume | Used by |
| --- | --- | --- | --- |
| `file` | The argv names a file, `{session_file}` (`state/agents/ID/session.jsonl`) | Reopens that file; refuses if it is missing | Pi; the default for schema 1 |
| `assigned` | mv2 sets it to the agent ID at spawn and passes `{session_id}` | Passes the same `{session_id}` | Claude |
| `reported` | The harness chooses it and mv2 binds it | Refuses until bound | Codex |

A `reported` harness binds its ID in one of two ways:

- A bridge returns it from a validating launch relay (`RpcProxySpec.binds_session`).
- A plain launch uses the harness `bootstrap` to tell the agent to run `"$MV2_CLI" session-bind --session-id ID`. Codex does this with `$CODEX_THREAD_ID`.

### Session changes

An adapter can report a verified native session change, such as Claude's `/clear`, without replacing the mv2 agent. Core records the change in the agent's session lineage, keeps the earlier conversations owned by that agent, and follows the current one for delivery, usage and resume. An adapter that reports no changes keeps exact-session behavior. See [session lineage](../design/session-lineage.md).

A bridged Codex conversation can have several clients at once. Its tool configuration requests agent-stable values, so `report` and `inbox` work from any client. An adapter can opt into proof from a retained bridge request when an older loaded conversation lacks the mode marker. Runner calls stay bound to their launch. See [shared conversations](../design/shared-conversations.md).

## Bridge and plain

The user docs call these a **live connection** and **launch-only**. In both, mv2 never types into the terminal.

A **plain** launch is the CLI in its pane. mv2 sees whether it is alive, can stop it, and can resume it with a message as its first prompt. It can't reach the running session. A **bridge** adds an endpoint that core can write to, plus an integration that reports back.

| Harness | Endpoint | Integration | `inbox_bridge` unset |
| --- | --- | --- | --- |
| Claude | `unix-lines`: Claude's peer inbox (`--messaging-socket-path`) | The `multiverse` mod in `claude/mod/`, loaded with `--plugin-dir` | Auto |
| Codex | `unix-websocket`: a launch relay to the shared app-server | `codex/launch.py` and `codex/protocol.py` | Auto |
| Pi | `unix-json`: a socket the extension listens on | `pi/extension.ts`, loaded with `--extension` | Off |

`settings.inbox_bridge` in `harness.json` picks the mode. Unset means the default above, `true` requires the bridge, and `false` launches plain.

When the bridge can't be prepared, an adapter in auto mode returns the plain spec with `native_mode.fallback_reason` set. With the bridge required, it raises instead.

Core can also meet failures later: starting the shared service, a `BridgeUnavailable` from the adapter once the service is up, or the relay. For those, put the plain spec in `CommandSpec.fallback`. The choice is saved in `runtime.json` as `native_mode`, with `policy`, `mode` and `fallback_reason`.

`CommandSpec.startup_retry` describes a separate, single retry after an exited native process fails before startup. Its `StartupRetrySpec` carries only replacement argv and conditional notices. The adapter's `startup_retryable(snapshot)` must return exactly `True` for its specific native diagnostic; core checks exit, readiness and launch ownership before using it. The snapshot includes the exact agent directory and owned-pane text with only the runner's actually printed leading notices removed. Native screen chrome stays in the text; the adapter can recognize a complete diagnostic naming that exact directory without parsing the screen layout. Other command fields stay unchanged. This is not a general retry of native work.

An optional relay view's `primary_event(direction, value)` describes exact-primary turn events after protocol validation: `interrupt`, `started`, or `completed`, with `native_session_id` and `turn_id`; completed events also have `status` (`interrupted` or `finished`). Side traffic and stale turn completions must return nothing. Core records an outgoing interrupt before forwarding it. `SessionHandle.queue_state` carries core's exact-conversation pause and explicit restart context to receipt and continuation operations; it grants no additional authority.

A bridged launch takes messages once it is ready. Until then, core holds them as `pending`, and the runner delivers them in order. Readiness comes from the integration registering itself, or from the relay validating the binding:

```sh
"$MV2_PYTHON" -m multiverse.tools.runtime_update --bridge claude-mod-v2 --status idle
"$MV2_PYTHON" -m multiverse.tools.runtime_update --usage-json '{"context": {"tokens": 82000, "window": 200000}}'
```

Integrations report `--status` (`idle`, `working`, `waiting`, `unknown`) and `--usage-json` the same way. `runtime_update` checks `MV2_LAUNCH_ID` and refuses an outdated launch. The adapter compares the reported bridge ID with its own constant.

## Plugin launch contributions

Plugins can add to a launch: extra plugin directories for Claude Code, extensions for Pi, and allowlisted config, instructions and MCP servers for Codex. Before calling `spawn` or `resume`, lifecycle builds a plan from the plugins that are on (`plugins/harness.py`). The adapter applies the parts it supports, gated by the harness version. `runtime.json` records what was applied, skipped or refused under `plugins`. See [plugins](plugins.md#extension-points).

## Capabilities

`mv2 capabilities --agent NAME` shows what an agent's current launch supports. `--harness NAME` shows the same for a definition. The built-in support is:

| Capability | Claude | Codex | Pi | Schema 1 |
| --- | --- | --- | --- | --- |
| `spawn`, `resume` | Yes | Yes | Yes | Yes (`resume` if defined) |
| `wake` | Yes | Yes | Yes | No |
| `session_name` | `--name` | Bridge: `thread/name/set` | `--name` | No |
| `stop` | Terminal | Bridge: cancels app-server work, then terminal | Terminal | Terminal |
| `send_steer`, `send_follow_up` | Bridge: peer inbox and mod | Bridge: app-server | Bridge: extension socket | No |
| `get_delivery` | Bridge: mod receipt | Bridge: app-server queue and turns | Bridge: extension | No |
| `ask` | Bridge: a model fork over the transcript | No | No | No |
| `get_usage` | Session transcript; the bridge adds window, cost and rate limits | Bridge: app-server context; plain: the last bridge reading | No | No |
| `compaction` | Live connection, when the compaction plugin is on | No | No | No |

`spawn`, `resume`, `send_steer` and `send_follow_up` are marked `required`. Codex checks its app-server only with an executor, so its message capabilities read `unavailable` without `--probe`.

## Delivery results and receipts

`send` saves every message in `inbox.jsonl` first. Core then records the attempt as `submitting` before it calls `adapter.send`. A crash therefore can never turn an uncertain send into a resend. The result maps to the message's submission state in `inbox-state.json`:

| `send` returns | Submission state | Means |
| --- | --- | --- |
| (before the call) | `submitting` | Attempt saved; becomes `unknown` if interrupted |
| `native_queued` or `ok` | `accepted` | The native side took it; not proof it was read |
| `pending` | `pending` | Saved; not yet submitted |
| `unavailable` or `error` | `failed` | Definitely not delivered; `mv2 deliver NAME --retry` may resend |
| `unknown`, or an exception | `unknown` | May have arrived; never resent automatically |

A message cancelled by `mv2 kill` becomes `cancelled`. `mv2 deliver NAME --check` calls `get_delivery` for `pending`, `submitting` and `unknown` messages, and for messages any launch accepted without transcript confirmation. From `get_delivery`, return `native_queued` with `value={'transcript_confirmed': True}` when the conversation holds the message. Return `unknown` when there is no evidence.

An adapter may report a retryable failure after acceptance with `error` and
`value={'definite_failure': True, 'transcript_confirmed': False}`. Core records
it as `failed` and keeps the check in `last_receipt`, without adding a submission
attempt. A check error without that exact flag leaves the acceptance unchanged.
An explicit retry rechecks the failed attempt before sending or waking; a late
confirmation prevents another submission, and unreadable evidence blocks it.
A stopped attempt-scoped recipient resumes with no interactive prompt; the durable startup queue submits the retry through `send` after its bridge is ready. This path disables launch fallback and requires a bridge. Nothing retries automatically. Claude also uses this failure result when its
mod's row-storage deadline expires: this is a deliberate policy exception to
proof of non-delivery, so a manual retry may duplicate a late stored copy.

Adapters that set `delivery_attempt_ids = True` receive a fresh private UUID in
`Message.id` for every actual submission. Core persists it as `native_message_id`
on the attempt and its `delivery_context` before calling `send`, and passes the
same UUID to `get_delivery`. The logical inbox message ID never changes. Old
records without this field keep their original message ID. Each attempt keeps
its submission context and checked `delivery_receipt`; a late confirmation of an older attempt cannot
change the current attempt's state. Transcript fallback for these IDs accepts
only exact-ID or receipt confirmation, never matching text from another attempt.
Legacy text-allocation cohorts include every scoped acceptance under its native ID, including earlier retries, so their receipts reserve their own rows. Missing attempt context leaves same-body text proof unknown. The legacy receipt migration skips these newer records.

| Harness | Receipt |
| --- | --- |
| Claude | The mod writes `claude-receipts/ATTEMPT_ID.json` for delivery and `ATTEMPT_ID.not-delivered.json` for failure. A valid delivery wins when both exist; older records use the message ID. |
| Codex | The adapter searches the thread's queue (100 entries) and recent turns (20) |
| Pi | The extension answers from its transcript metadata and in-process records |

`message.id` is a private UUID for correlating receipts. Show the model only `message.framed`, which is the sender line and the text. Optional `message.sent_at` carries the original aware ISO send timestamp from the inbox row, never the retry or receipt time. Core frames age at submission preparation when it exceeds 10 minutes; older callers may leave this field `None`. A provider that updates the age when it stores the row must preserve the message body and account for that first-line change when reading delivery evidence.

An optional `message_id_evidence(session, message_id, home)` hook supplies
positive evidence for the once-only update migration. Return `ok` with
`delivered: true` only for an exact message ID in that session's transcript under
the supplied recorded home; otherwise return `unknown`. The base implementation
returns unknown. Never match text or infer non-delivery. Core excludes accepted
wake prompts and records with receipts, reads outside the inbox lock, then
commits a confirmation only if the acceptance and saved session identity are
unchanged. It does not connect to a native service or infer a home from the
updating shell.

An optional `accepted_message_evidence(session, message_id, text, since, home,
framed=True, cohort=(), receipts=None, active_acceptances=())` hook returns `ok` with `delivered: true|false`, or `unknown` with a
named reason. Core calls it only after proving the accepting runner and child
have exited and a newer ready, idle launch uses the same session and storage.
The hook reads the accepting workspace and exact saved framing. `cohort` is a
snapshot of `(message_id, framed_text, accepted_at)` for the conversation's
acceptances, including already confirmed or final entries, read under the inbox
lock before calling the adapter. `receipts` maps those IDs to their saved, transcript-confirmed native receipt values from the same snapshot. The adapter also checks its own receipt files, which may not have reached the ledger. Exact prompt receipts reserve their stored occurrences before identical text consumes remaining rows oldest acceptance first. One row cannot confirm two messages, including copies of that row in another file. Exact attachment-ID rows belong only to that ID and consume no anonymous prompt occurrence. Missing row identity or ambiguous reservations keep text evidence unknown. An older same-body
acceptance with unrecorded framing has a `None` text in the cohort: text evidence
stays unknown, while exact-ID evidence may still confirm. Different bodies cannot
share a framed text row. `active_acceptances` lists cohort IDs accepted by the current live launch, from the same locked snapshot. An identical active acceptance without an exact receipt or attachment-ID row blocks anonymous text allocation: an idle status and a negative receipt read cannot exclude later publication. A later check can allocate again after that launch ends or its exact receipt is available. For older records,
`framed=False` supplies only the body, and an unknown workspace is an empty string
so every storage folder remains a candidate. Absence can prove loss, but a body match
cannot confirm delivery without an exact message ID. Unreadable or unfinished
transcripts must not become a negative answer; missing launch or process facts
prevent core from calling the hook. A proved loss is retained
as a final cancelled record with `final_reason: lost`, reported once by `--check`,
and never retried automatically.

The Claude adapter reads transcripts in one place, `harnesses/claude/transcripts.py`.
It finds a session's copies under the recorded storage, with the folder rule,
opens each through `open_regular`, and raises `Unreadable` when part of the
storage that could hold the session can't be read. Usage reads the newest copy
incrementally (`Transcript`). The evidence readers use `raw_rows`: every copy,
every non-empty line parsed or marked `DAMAGED`, fields as stored and never
deduplicated, because they look for copies of one row that disagree. A new
reader goes through the same module.

The Codex transcript provider reads rollouts through `harnesses/codex/transcripts.py`, independently of recovery's storage-absence proof. It validates every session identity header, checks copies by chained byte prefixes, and exposes only complete stored lines from a validated metadata snapshot. The native user-event echo authenticates a model-visible input without creating a duplicate prompt.

Native turn IDs guard end evidence; rollback markers count ordinary native input boundaries, including steers, independently of public turns. Their cut also trims proven pre-input context updates, skipping rollout bookkeeping and native-filtered response rows but preserving the prefix before the first input and stopping at non-context model items. Uncertain rollback or context boundaries fail closed, and earlier-generation readability uses the same checks as a read.

The release model table is generated by `scripts/gen_codex_models.py`; neither provider connects to the daemon or reads account caches. See [Codex's rules](../design/transcript-protocols.md#codex-rollout-rules).

## Command harnesses

A schema-1 `harness.json` runs any CLI without Python code:

```json
{
  "schema": 1,
  "description": "My agent CLI.",
  "spawn": ["my-agent", "--session", "{session_file}", "--system-prompt-file", "{bootstrap_file}", "--", "{task}"],
  "resume": ["my-agent", "--session", "{session_file}", "--system-prompt-file", "{bootstrap_file}"],
  "model_args": ["--model", "{model}"]
}
```

Put it in `harnesses/NAME/harness.json` in a universe or template, then run `mv2 spawn --name worker --harness NAME`. The session mode defaults to `file`, so resume refuses to start when the CLI never wrote `{session_file}`. mv2 inserts `model_args` before `--` when a model is chosen. [Files](../reference/files.md#schema-1) lists every field and placeholder.

A command harness has no bridge. Send it messages with `--delivery inbox-only`, and the agent reads them with `mv2 inbox`, as its bootstrap tells it to.

## Add a built-in adapter

1. Create `src/multiverse/harnesses/NAME/__init__.py` with a subclass of `ConfiguredAdapter` (`harnesses/base.py`). Set `package = Path(__file__).parent`, `family`, `supports_bridge`, `resume_prompt_args` if the CLI takes a prompt on resume, and `storage_env` for variables that locate its native sessions.
2. Add `defaults.json` next to it, with the `spawn` and `resume` argv, `model_args`, `unset_env`, `session` and any `bootstrap`. End the `spawn` argv with the initial prompt. mv2 drops it when there is none.
3. Override the methods your harness supports, and override `capabilities` to match. Decide what an unset `inbox_bridge` means. Claude and Codex treat it as auto.
4. Register the class in `BUILTINS` in `harnesses/__init__.py`.
5. Add `templates/base/harnesses/NAME/harness.json` (`{"schema": 2, "adapter": "NAME", "settings": {}}`) and a short `README.md` for agents.
6. Add any new asset types to `package-data` in `pyproject.toml`. JSON and TypeScript are already included.
7. Record the version you tested in [compatibility](../reference/compatibility.md), and describe the harness in [the harnesses guide](../use/harnesses.md).

## Test an adapter

| Test | How | Example |
| --- | --- | --- |
| Contract | A stand-in adapter that implements only `HarnessAdapter`, patched in for `harnesses.load` | `FourthAdapter` in `tests/test_operations.py` |
| Argv | Build a `LaunchContext` and `SessionHandle`; check model flags, literal prompts and refused resumes | `tests/test_adapters.py` |
| Real launch | A fake executable in `settings.executable` that records its argv, on an isolated tmux socket | `HarnessLaunchTest` in `tests/test_harnesses.py` |
| Integration | The bridge's own offline tests | `claude plugin test` for the mod; `tests/pi_bridge_test.mjs` for Pi |

Tests must never touch a person's own native state, and Codex tests use isolated homes. See [testing](testing.md).

## Transcript reading

The optional `TranscriptProvider` methods are observe: `transcript_view`,
`transcript_bodies` and `transcript_readable`. Without them, transcript reads say
that the harness does not implement the protocol yet. Core pins a reconciled
`SessionHandle` and releases its locks before asking the provider. The supplied
reader exposes only a deadline and `check()` for the pinned identity; it has no
native transport or process controls.

`transcript_view` returns a `TranscriptView`: row facts in stored order, history
indexes in conversation order, the context boundary, tip, gap and `summary`, the ref of the harness's own
compaction summary row the history starts at when what came before was never stored. Facts carry
labels and positions, never bodies. A position changes if preceding bytes change
and stays valid when rows are appended. `transcript_bodies` reads only requested
indexes and raises the content-free `TranscriptRowsChanged` if their bytes no
longer match the view. `transcript_readable` checks a recorded earlier generation.
Bodies are fresh dictionaries on every read; core hands them to the caller without
copying their contents again. Providers may reuse the same immutable view object
while its source, facts and history are unchanged, but must return a new object
whenever any of them changes. Core keeps a bounded cache of that view's analysis
and turn membership, never of session authority or bodies. Identity is checked
before and after each page; a provider's per-row loop can check only the deadline
between those page checks, without re-reading agent records for every row. All reads use the pinned native ID and
storage. An unrecorded home can be resolved from inherited values only inside
that agent's own session.

Core owns the opaque cursor envelope, generation selection, filters, page limits
and turn grouping. Each returned row names its turn at the selected history tip;
an abandoned row has no turn. Commands and their output can interleave with the
rows of a prompt turn, so a caller need not reread each turn to group a history.
Bodies larger than 16 MiB are metadata stubs; metadata-only pages never read bodies. A prompt finishes from a later prompt or timestamped
native end evidence, never from runtime idle status. Commands retain their own
output while later responses remain with the current prompt. Turns, including
the preamble, are listed by their first row in history order. See the
[transcript design](../design/transcript-protocols.md) for the history and
replacement rules, and the [plugin API](plugins.md) for the public read methods.

## One-shot runs

The optional `RunProvider` exposes `run_options(environment=...)` (observe) and
`run_plan(request, environment=..., run=...)` (spend). A plan contains an argv-only
`CommandSpec`, bounded `RunFile` declarations and a pure `interpret(RunOutcome)`
function. It describes no launch, service, relay or attachment. The adapter makes
no files and starts no process. Core checks the model and effort before asking
for a plan; an unverified but accepted model may run. Unsupported harnesses return
an unavailable result without spending.

The environment is a snapshot selected by the caller, including an agent's
proven storage when requested. `None` means unset and an empty string stays empty.
Core removes inherited mv2 and harness session identity after every overlay.
The adapter receives the same snapshot for options, model checking and planning;
it must not read the caller's environment again. Routing beyond storage is the
calling process's and the harness settings', as described in the
[plugin API](plugins.md).

Core chooses a private directory under the initiating user's
`~/.multiverse/runs/PLUGIN/AGENT-OR-HARNESS/UUID`, outside universes and agent
folders. A home managed as a dotfiles repository is allowed; the provider disables
project settings and instructions for the run. Files are relative to that directory, or one declared session file under
the adapter's selected store. That store file names this run's UUID. All files
are created exclusively with private permissions and a metadata-only ownership
journal precedes their contents. Core removes only files with the recorded file
identity and empty folders it created inside that run and its declared store
path; shared nonempty store parents stay put. The plugin and owner namespace
folders above the run directory are removed once empty, only while their recorded
identities still match. A short, bounded lock on the persistent run root coordinates
namespace creation and removal so cleanup cannot unlink a new run's open parent;
no lock spans the model call. Another run keeps these folders nonempty, and the
last run removes them. Older journals without namespace identities leave those
parents alone.

A disposable supervisor owns the child's new process group. One deadline covers
preparation, execution and interpretation; cancellation, timeout and the calling
worker's death stop that group with TERM then KILL and bounded cleanup. The leader
is not reaped until group cleanup, so a reused PID grants no control. Agent and
universe folder identities are captured before the API reads the agent's storage
and checked after that read and the preliminary options probe. The same immutable
snapshot is carried into core and checked before preparation and spawning, while
waiting and before returning; replacing a folder cannot establish new authority
for storage read from its predecessor. No agent lock spans the run, and removal never
recreates the agent folder. A run cannot change its runtime, binding or inbox.

Input and prepared file content are bounded to 32 MiB, stdout to 16 MiB and stderr
to 64 KiB. Overflow is a failure, not a truncated success. Replies stay in memory;
errors carry only a bounded native diagnostic or a content-free core reason.
`RunReply` and `RunFailure` carry elapsed time and any known usage and cost for the
plugin's metadata-only run record. Core never retries. The next run performs a
bounded, silent sweep of leftovers only when all recorded producers are proven
gone and all file ownership still matches. After the supervisor records that a run
ended, the calling dashboard or plugin worker need not exit before cleanup. A
cleanup problem does not discard a successful reply: the journal stays for the
next sweep and `mv2 doctor`, which reports leftovers without deleting them.
