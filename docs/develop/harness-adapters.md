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

| Method | Returns | Must |
| --- | --- | --- |
| `spawn(context)` | `CommandSpec` | Build the launch for a new session from `context.values` and `context.model`. |
| `resume(session, context)` | `CommandSpec` | Reopen exactly `session`. Raise `ValueError` if the saved native ID or session file is missing or wrong, and never fall back to the latest session. When `context.resume_prompt` is set, pass it as the first interactive prompt (wake). |
| `stop(session, executor)` | `NativeResult` | Cancel work running outside the terminal, such as on a shared server. Core then ends the process and the pane. Return `ok` when there is nothing to cancel. |
| `capabilities(session, executor=None)` | `dict[str, Capability]` | Give one entry per name in `CAPABILITIES`, with a reason when not `supported`. Stay cheap without an executor. With one (`mv2 capabilities --probe`), you may ask the native side. |
| `send(session, message, executor)` | `NativeResult` | Deliver `message.framed` to the exact session, at `message.delivery` (`steer` or `follow-up`). |
| `get_delivery(session, message_id, executor)` | `NativeResult` | Look up the receipt for one message, without sending anything. |
| `ask(session, question, executor)` | `NativeResult` | Answer a side question without adding it to the conversation, or return `unavailable`. |
| `get_usage(session, executor=None)` | `NativeResult` | Return measured context and token usage, or `unavailable`. Never estimate. |

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

| Harness | Receipt |
| --- | --- |
| Claude | The mod writes `claude-receipts/MESSAGE_ID.json` in the agent directory when the message enters the conversation |
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
