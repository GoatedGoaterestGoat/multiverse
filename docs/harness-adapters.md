# Harness operations

Multiverse exposes the same operations to its CLI, importable tools and TUI backend:

| Operation | Contract |
| --- | --- |
| `spawn` | Start a native terminal in the saved workspace; the initial prompt is optional. |
| `resume` | Reopen the exact saved conversation, never the latest or a replacement session. |
| `send` with `steer` | Deliver at the next safe boundary, without aborting shell tools; wake an idle session where supported. |
| `send` with `follow-up` | Deliver as the next turn after current work finishes. |
| `ask` | Answer an isolated side question without adding it to the main conversation. |
| `get_usage` | Return measured cumulative session usage, or an explicit unavailable/error result. |
| `observe` | Report launch/session state and available native activity; unknown remains unknown. |

`harnesses/types.py` defines `HarnessAdapter`, `SessionHandle`, `Message`, results
and the execution boundary. A handle contains the stable Multiverse agent ID,
provider name, exact native ID/session file, saved workspace, and current launch
ID. Core derives it from authoritative properties/runtime records; it does not
maintain another copy of those records. Spawn/resume return `session` in their
result. `observe` exposes the current handle after asynchronous identity binding.
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

Adapters return native results from `send`, `ask`, `get_usage` and `observe`.
Launch operations return process specifications; core owns actual process/tmux
creation. The injected executor owns subprocess/RPC deadlines and terminal
ownership checks; the provider interprets native replies. Internal command/RPC
plans are never public send results. There is no new model loop, simulated chat
UI, or fallback that invokes another model to forward a message.

Core owns universes, agent IDs, launch generations, environment scrubbing,
workspace preservation, processes and tmux. `attach`, `inspect` and process
`stop` remain shared terminal operations. Reported phase/summary are separate
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
mv2 observe worker
mv2 capabilities --agent worker --probe
```

The corresponding Python modules have `run(...)` functions and standalone entry
points, e.g. `python -m multiverse.tools.ask`. They do not import Textual.
`LocalBackend` delegates to the same tools, with no provider-name branches.
Public capabilities are `spawn`, `resume`, `send_steer`, `send_follow_up`, `ask`,
`get_usage`, and `observe`. Support may depend on configuration and a registered,
running native session. Discovery returns supported/unavailable/error plus a reason.

`send` first persists the message with a stable UUID, sender and optional
`reply_to`, then calls the adapter. Its result includes `saved`, `state`, and
`native_delivery`. Unavailable or failed native delivery produces `ok: false`
and a nonzero CLI exit while retaining the saved message and its ID. Do not
repeat `send` to retry that same message: that creates a new UUID.

- `native_queued`: native transport accepted it; reading and an answer are unconfirmed.
- `pending`: the registered native bridge will consume it; delivery is unconfirmed.
- `unavailable`: no implementation is available for this session/mode.
- `error`: the attempt failed; acceptance may be ambiguous.

`--queue-only` retains the old append-only behavior without invoking the adapter.
It does not pause an already registered inbox consumer: a Pi bridge or an agent
checking its inbox can still consume that record. Explicit retry is a recovery
operation: `mv2 deliver worker --message-id UUID --retry`. An ambiguous/accepted
attempt is not automatically replayed. Recovery also retains `--mode native-queue`
for Codex's plain CLI queue, whose timing is unspecified; this is not advertised
as steering or follow-up. The old `native-status` command is a compatibility shim;
new callers use named operations.

Core serializes delivery against resume and stores receipts with exact recipient
native ID and launch generation. Inbox acknowledgement rechecks generation under
the launch lock. Queue acceptance never implies acknowledgement or completion.

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

Codex bridge mode starts an owned native app-server and connects the native TUI
to its private Unix WebSocket. Exact thread ID/workspace are checked before
queueing or steering. Urgent busy input uses `turn/steer` with `expectedTurnId`;
normal/idle input uses `thread/queue/add`. Races return errors without fresh-turn
or resume fallback. Approvals remain unanswered by the delivery client. Core
bounds transport requests, frames and deadlines and cleans up its service on exit.
Remote resume inherits native permissions: put initial permission/profile flags
in `spawn_args`. Custom resume overrides rejected by native Codex are diagnosed,
never silently removed. Plain compatibility launches do not acquire bridge controls.

Pi's source extension is opt-in and retains the interactive TUI. After exact
session/launch registration it polls every 1.5 seconds and uses native
`sendMessage` with `steer` or `followUp`. Only exact-session on-disk custom-message
IDs allow acknowledgement. It scans bounded pages beyond unsupported records,
acknowledges only contiguous persisted prefixes, and deduplicates persisted IDs
on resume. In-memory acceptance alone is insufficient. Asynchronous failures
stay unread; a crash before a receipt can still cause duplicate delivery.

## Implemented capabilities and verification limits

| Operation | Codex bridge | Claude | Pi bridge | Plain/legacy |
| --- | --- | --- | --- | --- |
| Spawn / exact resume | Yes | Yes | Yes | Manifest-dependent |
| Safe-boundary steer | Yes | Unavailable | Yes | Unavailable |
| After-turn send / idle wake | Yes | Unavailable | Yes | Unavailable |
| Observe launch / terminal | Yes | Yes | Yes | Yes |
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
