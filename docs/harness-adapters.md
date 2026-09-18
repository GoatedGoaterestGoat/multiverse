# Native harness adapters

Multiverse launches native terminals. It does not run a replacement conversation
loop. `agents.py`, `runner.py`, `tmux.py`, `store.py`, and `native.py` own process
execution, launch generations, environment isolation, terminal ownership, durable
inboxes and delivery receipts. Each built-in has its own source package under
`src/multiverse/harnesses/{codex,claude,pi}/`; future native assets belong there.
`legacy/` interprets existing local definitions without rewriting them.

`harnesses/types.py` defines `HarnessAdapter`, `LaunchContext`, `CommandSpec`,
`NativeContext`, `Capability`, `NativeResult`, `RpcSpec`, and `ServiceSpec`. Adapters produce specifications
or observations. They never create tmux panes, execute subprocesses, or mutate
Multiverse state. Core executes bounded native probes/commands without a shell.
Launch, resume, normal follow-up and urgent steering are marked `required` in capability discovery.
Claude normal/urgent delivery remains a required integration blocker. Codex and Pi
require their declarative inbox bridge to enable native delivery; plain compatibility
launches explicitly report missing required controls.
Unknown capabilities return unavailable; unavailable differs from a failed probe
or operation. Optional observation failures remain data and do not break snapshots.

## Configuration and compatibility

New universes contain only a reference and preferences:

```json
{
  "schema": 2,
  "adapter": "pi",
  "settings": {"model": "your-model", "inbox_bridge": true}
}
```

Supported settings: `executable`, `model`, `args`, `spawn_args`, `resume_args`,
`unset_env`, and the boolean `inbox_bridge` for Codex and Pi. Argument settings are string arrays,
inserted before the prompt/session terminator. Explicit launch models override
saved agent models on resume, which override the configured default. Workspace
is owned by core: resume preserves the recorded agent worktree even if the
universe default changes. `CODEX_THREAD_ID`, `CLAUDECODE`, and inherited `MV2_*`
identity are scrubbed before executing a child. Other native environment settings
remain inherited; credentials are never read by adapters.

Schema-1 local manifests continue to work unchanged, including custom executable
paths, permission flags, models, bootstrap text, assets and arbitrary metadata.
They use the legacy adapter and do not acquire new optional capabilities by name.
There is no automatic rewrite, deletion, migration, or native configuration change.
To opt into schema 2, compare the source package's `defaults.json` with the local
manifest, transfer every non-default preference explicitly, and retain the old
manifest/assets until tested. Keep schema 1 for custom behavior that schema 2
cannot express. Never overwrite an existing universe directory with a template.

Codex binds its reported UUID, Claude receives an assigned UUID, and Pi uses the
agent's exact `session.jsonl`. Missing native IDs or missing/invalid Pi session
files refuse resume; no latest-session or fresh-agent fallback exists. The Pi
bridge also binds its actual native UUID and refuses a different session file or
ID. Native terminal attach remains the ordinary `mv2 attach AGENT` operation.

## Capability matrix

“Unavailable” is intentional, not a zero measurement. Support below describes
interactive native TUI mode. The Codex bridge launches its native TUI and native
app-server together; it does not resume a parallel session behind the TUI.

| Operation | Codex | Claude | Pi plain | Pi with registered bridge | Legacy |
| --- | --- | --- | --- | --- | --- |
| Launch / exact resume | Yes | Yes | Yes | Yes | Manifest-dependent |
| Native steering at a safe boundary | With bridge, exact `expectedTurnId` | Unavailable | Unavailable | Yes, `deliverAs: steer` | Unavailable |
| After-turn follow-up | With bridge, native after-turn queue | Unavailable | Unavailable | Yes, `followUp` | Unavailable |
| Queue, timing unspecified | Installed `queue --help` probe + exact bound UUID | Unavailable | Unavailable | Unavailable | Unavailable |
| Wake idle native session | With bridge, native queued input | Unavailable | Unavailable | Yes, `triggerTurn` | Unavailable |
| Native activity | Unavailable | Unavailable | Unavailable | Lifecycle events | Unavailable |
| Cumulative measured usage | Unavailable | Unavailable | Unavailable | Unavailable | Unavailable |
| Native turn interrupt | Unavailable | Unavailable | Unavailable | Unavailable | Unavailable |
| Isolated ask / BTW | Unavailable | Unavailable | Unavailable | Unavailable | Unavailable |

Pi bridge support is advertised only after registration for a running launch,
and is removed on native shutdown. Installation or version mismatches leave
capabilities unavailable until that handshake succeeds. Codex queue is restricted
to ordinary local launches; custom argument/remote/profile routing is unavailable
until it has a verified implementation. Native permission, credential or transport
failures remain operation errors, with diagnostics. A successful queue command
confirms only acceptance, never steering timing, read status, wake, or an answer.

Claude Mods are early access and are not required for lifecycle. The published
`$.prompt.submit`, `$.session.usage`, and `$.model.fork` APIs do not establish that
a bridge is installed in a running native TUI. Isolated fork can also return null.
This release does not implement that bridge. Starting an unrelated Codex app-server
or Pi RPC process does not attach controls to an existing interactive TUI. No
terminal keystrokes or tool cancellation simulate unsupported native operations.

## Codex native controller

`settings.inbox_bridge: true` asks core to own a native app-server for this launch.
The source adapter specifies `codex app-server --listen unix://...` and the native
TUI's matching `--remote`; core creates a private short socket directory, starts
both processes with the scrubbed agent environment, monitors failures, and cleans
up the owned service process group on terminal exit/stop. Nothing starts, stops,
or reconfigures the user's shared daemon. Custom `--remote` / `--no-daemon` flags
conflict explicitly with this mode; other preferences remain in native argv.
On native remote resume, Codex restores its saved permissions and rejects explicit
permission overrides. The adapter therefore does not repeat its own initial
`--add-dir` grant on resume. Put initial permission flags in `spawn_args`, not
`args`; custom resume permission/profile flags are rejected with a diagnostic and
are never removed or rewritten. Other saved preferences and exact native IDs stay
intact. Older Codex versions that lack this interface can retain plain lifecycle mode.

The adapter's bounded RPC plan reads the exact thread and saved workspace on the
recorded launch endpoint. It refuses unloaded/mismatched targets. Urgent delivery
uses `turn/steer` with the observed active turn ID; normal delivery uses
`thread/queue/add`, whose native dispatcher starts only when idle. Idle urgent
input uses the same queue. Stable inbox IDs become `clientUserMessageId`.
A finalization race returns an error without silently starting or steering another
turn; an explicit retry rereads state. No resume, interrupt, approval response,
or model/permission override is sent by the delivery client.

Core's local Unix WebSocket transport limits each plan to 16 requests, a deadline,
and 1 MiB frames. A successful native receipt remains `native_queued`, not read or
answered. Discovery is explicit and bounded; dashboard snapshots do not connect.
`codex app-server proxy` is not used: the installed proxy's raw control-socket
transport does not speak the Unix WebSocket API used here.

## Shared tools and message semantics

```sh
mv2 capabilities --agent AGENT --probe
mv2 capabilities --harness pi
mv2 send AGENT --text 'Please inspect the regression' --delivery steer
mv2 send AGENT --text 'After your current task, inspect this' --delivery follow-up
mv2 deliver AGENT --message-id MESSAGE_UUID
# Explicitly accept unspecified native queue timing for a supported Codex session:
mv2 deliver AGENT --message-id MESSAGE_UUID --mode native-queue
mv2 native-status AGENT --operation usage
```

The same importable `tools` modules back the CLI and `LocalBackend`. Each can run
standalone with `python -m multiverse.tools.capabilities` (or `deliver`,
`native_status`, etc.). No UI or Textual import is required for these tools.

`send` only durably appends; its default requested timing is `steer`. It preserves
the stable message UUID, sender and `reply_to`. `deliver` addresses an existing
unread message belonging to that exact recipient and records an attempt tied to
the native UUID and launch generation. Codex queue success is `native_queued`;
Pi polling is `pending`; neither is read or answered. Queue acceptance never
acks the durable inbox. A repeated accepted or failed/ambiguous native attempt
is not automatically retried: `--retry` explicitly accepts possible duplication.
Resuming cannot change the launch generation while native execution is in flight.
Inbox acknowledgement validates its launch generation under the same launch lock,
so an old callback waiting through resume cannot clear the new launch's inbox.

The Pi bridge must be enabled to provide Pi's required native messaging; plain Pi
retains lifecycle compatibility but reports those required capabilities unavailable.
The bridge polls every 1.5 seconds. It requests native steering during
an active run, allowing Pi to deliver at its safe boundary without aborting shell
tools. Follow-ups wait for the native turn to finish. `sendMessage` itself is
fire-and-forget: the bridge waits for stable IDs in the exact native session's
custom-message entries on disk before acknowledging the batch. It reads only the
exact file incrementally, with bounded per-poll I/O; in-memory entries are insufficient. Persistence confirms
injection, not reading or an answer. On resume, those IDs prevent reinjection of
persisted messages. A queued but unpersisted injection remains unread; the bridge
does not repeatedly inject it during the same process. Native asynchronous errors
appear in Pi diagnostics; restart/resume can retry unread messages. A crash can
still produce duplicates, so receivers should correlate by message ID. Unsupported
modes stay unread. The bridge scans one bounded page per poll, including pages
beyond unsupported records. `inbox --after BYTE_CURSOR` is read-only pagination;
it never acknowledges skipped records. Only a contiguous persisted prefix at the
actual unread frontier can advance acknowledgement. A Pi `deliver --mode` override is unavailable because its bridge
honors the mode already stored in the durable message.

`interrupt`, `ask`, and usage requests explicitly return unavailable. `stop` still
means terminating the owned terminal; it is not a native turn interruption.

## Usage and UI

The usage contract is native-session cumulative measured input/output/total,
source, timestamp, completeness and optional cached-input/cache-write subsets.
Input is cache-inclusive; cache counts are not added twice. Unknown is never zero.
No heuristic token counting, arbitrary recent-session scan or cost estimate is
implemented. Native usage readers are deferred. The compact Tokens column and
inspector show N/A with detailed unavailable/error reason in full details; custom
views remain authoritative, and narrow layouts hide Tokens.

## Extending and testing

Add a source package implementing `HarnessAdapter`, register it in `BUILTINS`, and
add declarative template configuration. Implement only capabilities that have a
working native path; use probes or current-launch registration for conditional
support. Keep native assets in that package and include them in package data.
A generic schema-1 local command remains an alternative for custom launch forms.

`tests/test_adapters.py` covers configuration compatibility, capability/failure
semantics, literal execution, exact recipient/reply identity, retries and usage
rendering. `tests/pi_bridge_test.mjs` runs the shipped TypeScript against event and
CLI fakes, including busy steering, follow-up, finalization, delayed persistence,
ack failures, session changes and stale launches. Installed Pi timing tests run two real four-second shell tools through an offline
provider: steering enters between them, follow-up after turn completion, and neither
tool is aborted. Terminal integration uses stub Codex/Claude executables and an offline Pi mock provider on disposable tmux
servers. Installed Codex native TUI tests use a local scripted HTTP provider for busy timing,
idle wake, exact resume, workspace/bootstrap preservation and owned-service cleanup.
Authenticated Codex/Claude model turns and real-provider busy delivery are not exercised. The installed Codex queue was also probed against a disposable fake
Unix/WebSocket endpoint: it emits `thread/queue/add` only, not `turn/steer`. Run the full suite with:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Native references: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Pi extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md),
[Claude Mod types](https://github.com/anthropics/claude-code/blob/main/mods/types/claude-code.d.ts).
Codex queue semantics are limited to the installed CLI's `codex queue --help`;
the app-server documentation does not establish this CLI queue's timing.

## Required-delivery blockers at this milestone

Claude 2.1.231 has native cross-session messaging, but the documented entry points
are model tools, not an external send CLI. A supported external channel can inject
messages, but it is gated by native provider/authentication, organization policy
and a per-channel development allowlist confirmation. No such bridge is verified
here. Mods are similarly unverified. Plain terminal text cannot reliably prove
that the composer is empty, unmodified, or free of modals, so guarded keystroke
steering is not claimed. See [Claude cross-session delivery](https://code.claude.com/docs/en/cross-session-messaging)
and [channel requirements](https://code.claude.com/docs/en/channels-reference).
