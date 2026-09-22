# Harness operations

Multiverse exposes the same operations to its CLI, importable tools and TUI backend:

| Operation | Contract |
| --- | --- |
| `spawn` | Start a native terminal in the saved workspace; the initial prompt is optional. |
| `stop` | Cancel native work where the terminal is only a client, then close the owned terminal; never stop a shared daemon. |
| `resume` | Reopen the exact saved conversation, never the latest or a replacement session. |
| `send` with `steer` | Deliver at the next safe boundary, without aborting shell tools; wake an idle session where supported. |
| `send` with `follow-up` | Deliver as the next turn after current work finishes. |
| `ask` | Answer an isolated side question without adding it to the main conversation. |
| `get_usage` | Return measured cumulative session usage, or an explicit unavailable/error result. |

`harnesses/types.py` defines `HarnessAdapter`, `SessionHandle`, `Message`, results
and the execution boundary. A handle contains the stable Multiverse agent ID,
provider name, exact native ID/session file, saved workspace, and current launch
ID. Core derives it from authoritative properties/runtime records; it does not
maintain another copy of those records. Spawn/resume return `session` in their
result. `list` reads current core identity, process state and reported activity after
asynchronous identity binding; these are not adapter operations.
Native ID can be null while a reported session has not registered; operations
must not guess a recent session. Claude receives an assigned UUID; Pi owns an
exact session file and its bridge reports its UUID. Codex currently still uses
`session-bind` with the native `CODEX_THREAD_ID`; automatic startup identity
capture is not claimed.

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
- `claude/`: native lifecycle; required native delivery is currently unavailable.
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
that identity. Core validates the saved target before launching. The injected
executor owns subprocess/RPC deadlines and terminal ownership checks; the provider interprets native replies. Internal command/RPC
plans are never public send results. There is no new model loop, simulated chat
UI, or fallback that invokes another model to forward a message.

Core owns universes, agent IDs, launch generations, environment scrubbing,
workspace preservation, processes and tmux. `attach`, `inspect` and process
`stop` remain core operations; stop first calls the adapter to cancel work hosted outside the terminal. Reported phase/summary are separate
from observed liveness. `task` is a compatibility name for the optional initial
prompt, not a scheduler object. It is no longer copied into the status summary.

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
`ask`, `get_usage`, and `get_delivery`. Support may depend on configuration and a registered,
running native session. Discovery returns supported/unavailable/error plus a reason.

`send` first persists the message with a stable UUID, sender and optional
`reply_to`, then calls the adapter. Its result includes `saved`, `state`, and
`native_delivery`. Unavailable or failed native delivery produces `ok: false`
and a nonzero CLI exit while retaining the saved message and its ID. Do not
repeat `send` to retry that same message: that creates a new UUID.

One logical inbox lives in `inbox.jsonl` (immutable messages) and
`inbox-state.json` (delivery attempts, receipts and per-ID acknowledgements).
The state sidecar is core-owned; providers have no mailbox or retry database.

| Submission state | Meaning |
| --- | --- |
| pending | Saved; no completed submission attempt. |
| submitting | Attempt persisted before calling native API; a crash makes acceptance unknown. |
| accepted | Native API accepted; may still be queued inside the harness. |
| failed | Definite rejection or pre-submission failure; explicit retry permitted. |
| unknown | Submission may have succeeded; never blindly resent, including with `--retry`. |

The immediate tool result `native_queued` maps to accepted; `unavailable`/`error`
map to failed. Transcript confirmation, explicit acknowledgement, and a correlated
reply are independent evidence, never inferred from acceptance.

```sh
mv2 inbox                         # unread manual messages only
mv2 inbox --all                    # full history, delivery attempts and acknowledgements
mv2 ack --message-id UUID          # acknowledge exactly this message (repeatable)
mv2 send worker --text 'Later' --delivery inbox-only
mv2 deliver worker --message-id UUID --retry  # definite failure only
mv2 deliver worker --message-id UUID --check  # reconcile without submitting
```

`--queue-only` is a compatibility alias for `inbox-only`. Native-assigned messages
never also appear in ordinary mailbox reads, including failed/pending submissions.
They remain inspectable through `inbox --all`. No automatic switch to manual
handling occurs. Each native message contains its ID, sender, text, delivery mode,
and reply correlation; the agent acknowledges the ID after handling it.

Per-recipient sends serialize persistence and submission. Urgent messages may
overtake native follow-ups; acknowledgements therefore use individual IDs.
Legacy cursor ack still works for manual messages only and cannot clear native
messages it skipped. Existing schema-less logs retain manual semantics without
rewriting their contents. Existing `native-delivery.json` receipts remain readable for compatibility;
accepted or uncertain old attempts are not replayed. New attempts use inbox state.

Retries preserve exact native session identity, allow a new launch of that same
session, and refuse a different session. Core persists the attempt before the
native call and merges receipts without overwriting concurrent acknowledgements.
Pi supports receipt reconciliation against its exact transcript and current
in-process acceptance records. Missing evidence remains unknown. Codex checks up to 100 queued submissions and 20 recent turns by exact client
message ID; absence remains unknown and requires investigation. Accepted submissions are never automatically resubmitted.

The old `native-status` command remains a tool compatibility shim for usage.
Its activity operation reports unavailable and points callers to `list`; there
is no native activity query or adapter `observe` operation. Adapters expose
feature methods directly, with no `control`, `native_capabilities`, `probes`, or
`NativeContext` conversion layer. `validate_session_id` explicitly validates an
ID; it does not discover a session.

## Configuration and compatibility

New universes retain references and preferences, not copied implementations:

```json
{"schema": 2, "adapter": "pi", "settings": {"inbox_bridge": true}}
```

Shared preferences are `executable`, `model`, `args`, `spawn_args`, `resume_args`,
and `unset_env`. Codex and Pi additionally support `inbox_bridge`. Native flags,
models, permissions and workspaces are preserved. Inherited `MV2_*`,
`CODEX_THREAD_ID` and `CLAUDECODE` identities are scrubbed before child execution.
No adapter reads credentials or changes installed native configuration.

Existing schema-1 manifests, custom bootstrap and assets remain authoritative;
there is no automatic rewrite or migration. Compare source defaults and transfer
all local preferences before opting into schema 2. Retain schema 1 for custom
behavior schema 2 cannot express. Existing universes are never overwritten.

Codex bridge mode uses the shared native daemon for the current OS user and
`CODEX_HOME`. Spawn/resume invokes `codex app-server daemon start`, reads its JSON
socket address, and connects the native TUI with `--remote unix://<returned socket>`. Concurrent
monitors use Codex's native startup lock; there is no server per agent, project,
or dashboard. Opening/closing a dashboard does not start/stop the daemon. Core
never invokes daemon stop, restart, bootstrap, install or update. Codex's own
configured daemon update policy remains authoritative. A missing
managed standalone installation or a CLI/daemon version mismatch fails with a
diagnostic; it is never repaired by disrupting other clients. Codex owns daemon
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
requests, frames and deadlines. Remote resume inherits native permissions: put
initial permission/profile flags in `spawn_args`. Unsupported resume overrides
are diagnosed, never silently removed. Whole-table `shell_environment_policy`
CLI overrides are rejected in bridge mode; individual keys remain supported.
Plain compatibility launches do not acquire bridge controls.

Explicit `stop` holds the recipient's launch lock, deletes its pending native
queue entries, interrupts its exact active turn and cleans its native background
terminals before closing its terminal. Confirmed queue cancellations are recorded
by message ID in the core inbox and cannot be replayed with `deliver --retry`.
Stop is bounded to 128 native queued submissions; errors leave the terminal
attached with a diagnostic. This requires a bound native session ID and a
reachable server. Closing a terminal by other means only disconnects its client
and can leave native work running. Closing a dashboard or detaching tmux never
calls native stop.

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
There are no polling timers, inbox reads, or acknowledgements in the extension.
Native acceptance is deduplicated in process; a bounded exact-transcript reader
recognizes persisted IDs across launches. Socket loss leaves acceptance unknown.
Core owns the private socket directory lifecycle. Pi's separate headless RPC mode
is not substituted for its interactive terminal.

## Implemented capabilities and verification limits

| Operation | Codex bridge | Claude | Pi bridge | Plain/legacy |
| --- | --- | --- | --- | --- |
| Spawn / exact resume | Yes | Yes | Yes | Manifest-dependent |
| Explicit stop | Native queue cancellation/interrupt/terminal cleanup | Owned terminal | Owned terminal | Owned terminal |
| Safe-boundary steer | Yes | Unavailable | Yes | Unavailable |
| After-turn send / idle wake | Yes | Unavailable | Yes | Unavailable |
| Observe launch / terminal | Yes | Yes | Yes | Yes |
| Receipt reconciliation | Bounded queue/history lookup | Unavailable | Exact transcript/current acceptance | Unavailable |
| Native activity | Unknown | Unknown | Lifecycle events | Unknown |
| Measured cumulative usage | Unavailable | Unavailable | Unavailable | Unavailable |
| Isolated ask | Unavailable | Unavailable | Unavailable | Unavailable |

Unsupported usage displays N/A, not zero. The usage representation includes
cache-inclusive input, output, total, optional cache subsets, source, timestamp
and completeness. No token estimation or native-history guessing is performed.
An unsupported ask never becomes ordinary input or launches a separate agent.
Claude delivery remains a required integration gap: installed native feature gates
can prevent channels/Mods from loading. Basic lifecycle does not depend on them.

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
