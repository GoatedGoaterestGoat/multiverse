# Architecture

This page shows how mv2's source is laid out, what runs where, and how a launch, a message and an agent's identity move through the code. Read [concepts](../concepts.md) first for the terms.

## Package map

Everything lives in `src/multiverse/`.

| Package or module | Holds |
| --- | --- |
| `cli.py` | The `mv2` command line. `COMMANDS` lists every command, and each maps to a module in `tools/`. |
| `tools/` | One module per command, each with `configure(parser)` and `run(...)`. The dashboard calls the same `run` functions. `runtime_update.py` is for harness integrations, not a command. |
| `universe/` | Everything on disk. The package resolves universes and walks subverses. Its modules are `agents` (agent records), `mailbox` (inbox, read position, delivery tracking), `focus` (whether an agent's steer and follow-up messages wait in its inbox, and until when), `templates` (install and refresh), `catalog` (skills, commands, workflows, bootstrap), `relocate` and `trash`. |
| `runtime/` | Everything that runs. Its modules are `lifecycle` (spawn, resume, wake, readiness, listing, kill), `native` (runs adapter specs, delivers messages, keeps receipts), `rpc` (JSON-RPC transport and launch relay), `recovery` (reconnecting idle Codex terminals) and `runner` (the process in each agent's pane). |
| `terminal/` | tmux: commands, pane tags, liveness from panes, and the status line script tmux runs. |
| `harnesses/` | Adapters. `types.py` is the contract and `base.py` holds the shared argv rendering. There is one package each for `claude`, `codex` and `pi`, plus `legacy` for schema-1 definitions. See [harness adapters](harness-adapters.md). |
| `plugins/` | The plugin system: discovery, manifests, trust, `plugins.json` and per-plugin settings, the loader and its failure boundaries, the `mv` API, launch contributions (`harness.py`), the event journal (`events.py`) and the plugin host (`host.py`). Bundled plugins live in `plugins/builtin/`. See [plugins](plugins.md). |
| `ui/` | The dashboard. See [below](#how-the-dashboard-is-built). |
| `templates/` | The bundled `base` template. See [templates](templates.md). |
| `store.py` | Atomic JSON writes, file locks, IDs and timestamps. |
| `display.py` | Text for people that the CLI and dashboard share: states, labels, usage. |

Dependencies point one way:

1. `store`, `terminal` and `display` import nothing else from mv2.
2. `harnesses` imports `store`.
3. `plugins` imports `store`, `terminal` and `universe`. `universe` and `runtime` record events through it with imports inside functions, so a plugin problem never stops a send or a launch.
4. `universe` imports `store`, `terminal` and the contract types in `harnesses`.
5. `runtime` imports everything above.
6. The entry points sit on top: `tools`, `ui`, `cli`, and `plugins/__main__.py` (the helper actions that harness hooks run).

`runtime.native` imports `runtime.lifecycle` inside one function, to wake a stopped agent, and `runtime.slash` inside snapshot reconciliation to settle pending command receipts. These runtime edges are local imports; adapters never import runtime.

The top-level `runner.py`, `statusline.py` and `recovery.py` are stubs that forward to `runtime.runner`, `terminal.statusline` and `runtime.recovery`. Processes started from an older layout run them by path or module name. Don't add code to them.

## Add a command

1. Add `tools/NAME.py` with `configure(parser)` and `run(root=None, ...)`. Return a value, and raise `ValueError` for errors people should see. Add `format_output(result)` for readable output. `--json` still prints the record.
2. Add the name to `COMMANDS` in `cli.py`. Hyphens become underscores, so `get-usage` is `tools/get_usage.py`. The `list` command is `tools/list_agents.py`.
3. Run `python scripts/gen_cli_docs.py` to regenerate [the CLI reference](../cli/reference.md).

Each tool also runs on its own, as `python -m multiverse.tools.NAME --help`.

## What runs where

mv2 has no daemon of its own. State is kept in files in the universe. Processes find each other through those files and tmux.

| Process | Started by | Does |
| --- | --- | --- |
| `mv2 COMMAND` | You, a script or an agent's shell | Runs one tool and exits. `spawn` and `resume` return once the pane is set up, or wait with `--wait`. |
| Dashboard (`mv2`, `mv2 tui`) | You | Reads the universe and tmux every 1.5 s and calls tools. Agents keep running when it quits. |
| Runner (`runtime/runner.py LAUNCH_DIR`) | `lifecycle.start`, as the command of a new tmux pane | One per launch. Starts the native process and any relay, delivers held messages, watches status, and records the exit. |
| Native process (`claude`, `codex`, `pi`) | The runner | The agent. The Claude mod and the Pi extension run inside it. |
| Codex app-server daemon | `codex app-server daemon start`, from `lifecycle` | Shared by Codex agents using the same Codex home. mv2 starts or reuses it, and never stops or restarts it. |
| Status line (`terminal/statusline.py`) | tmux, while a client is attached | Renders the agent's tmux bar; a bounded helper uses core's nonblocking inbox peek. |
| Wrapper cleanup and recovery worker | The runner, detached | The cleanup removes the pane after the native process exits. The worker reconnects an idle Codex terminal after a relay loss. |

## How a launch flows

`mv2 spawn`, `mv2 resume` and a message to a stopped agent all call `runtime.lifecycle.start()`:

```text
lifecycle.start()                                   CLI process, under .launch.lock
  1. read or create properties.json
  2. adapter  = harnesses.load(root, harness)       harnesses/NAME/harness.json
  3. bootstrap = catalog.bootstrap() + adapter.bootstrap
  4. command  = adapter.spawn(context)  or  adapter.resume(session, context)
                -> CommandSpec: argv, env, unset_env, endpoint, service, relay
  5. write runtime.json; start or reuse the shared service, if the spec names one
  6. tmux new-session -> pane runs runner.py LAUNCH_DIR; tag the pane
  7. write launches/ID/request.json; touch launches/ID/ready

runner.py LAUNCH_DIR                                tmux pane
  8. wait for ready; read request.json
  9. environment: drop inherited MV2_* and unset_env, add the launch's own
 10. start the launch relay if the spec has one (fall back to plain if allowed)
 11. Popen(argv) with no shell; an exit within 2 s fails the launch
 12. once the bridge is ready, deliver messages held for this launch
 13. on exit: record the exit code, and the last 40 lines on failure;
     remove the pane unless the launch failed during startup
```

Shared-service adapters are prepared again after the endpoint is available. Both preparations receive the same proven storage-key selection, so unknown legacy keys keep their inherited behavior in the final connected tool environment.

The runner waits for its child in bounded intervals so it can handle a terminal hangup even when a helper thread receives the signal. A hangup or termination signal records a stop request; registration, startup and child-wait checkpoints act on it outside subprocess's locks. Repeated signals cannot interrupt cleanup. The shutdown path closes the runner's helpers and terminates its own child; shared native services stay running.

Adapters only describe a launch. They return a `CommandSpec` and never touch tmux or runtime files. Core validates socket lengths before asking the adapter, but creates the run directory and performs terminal recovery only after the adapter accepts the launch. Core owns processes, tmux, records and locks. The tmux creation reply binds pane operations to its server PID and start time, separately from the OS proof needed to signal that server. Each bound command checks that identity in the same tmux command group before acting; a replacement server cannot inherit the pane merely by reusing its number. A new spawn refused before native preparation releases its name. Once native preparation begins, a failed launch keeps its record with the error, so `mv2 resume` can retry.

A selected command may describe one argv-only startup retry. The runner considers it only after a nonzero exit before readiness, binding or delivery. A pending screen observation keeps this window open for up to 55 seconds; a recognized composer closes it. A short caller wait may return process readiness, but leaves the runner's observation and retry window open. Commands without supported screen observation keep the two-second startup window. The adapter must recognize the exact diagnostic from that owned pane. Core removes only a matching leading prelude that this runner actually printed before the candidate; other output remains part of the diagnostic check. Under the launch lock, the runner records the selected argv and notices before starting the replacement child. It never retries a live warning or an already-started conversation.

## How a message flows

```text
mv2 send AGENT                                      tools/send.py, under .send.lock
  0. agent sender: inspect its queued follow-ups before any recipient is saved
       matching recipient -> refuse only that recipient, unless --force
       unknown queue      -> continue with an advisory note
  1. mailbox.append_message -> inbox.jsonl          saved and fsynced first
  2. inbox-only: stop; the agent reads it with mv2 inbox
  3. native.send_or_wake
       stopped or dormant  -> lifecycle.start(resume, message as the first prompt)
       bridge starting     -> held as pending; the runner delivers it when ready
       running             -> native.deliver -> adapter.send(session, Message)
  4. the result becomes the message's submission_state in inbox-state.json

adapter.send, per harness
  Claude  JSON lines to the session's peer inbox socket; the mod injects the text
  Codex   JSON-RPC to the app-server: steer or queue on the exact thread
  Pi      a JSON request to the extension's Unix socket

mv2 deliver AGENT --check -> adapter.get_delivery -> receipt
```

An inbox-only send upgraded because of a blocked-on dependency also saves its original text. A definite `failed` or `launch-only` result commits an inbox fallback and a retry dismissal together in the delivery ledger. The original message ID and native evidence stay in history; no replacement message is appended. Inbox reads include that fallback even if their cursor already passed the native row, and mark it read only after successful output. Deferred startup failures use the same transition. For this tagged upgrade only, pending, accepted, submitting, unknown or otherwise unconfirmed input also has an inbox view with a possible-duplicate note. This view neither changes native tracking nor starts another submission. `native_receipt.transcript_confirmed` equal to true or a valid timezone-bearing `transcript_confirmed_at` suppresses an unread copy; acceptance alone does not. One per-message copy read timestamp survives later native state changes, including deferred failure or proved loss. The original remains a single history row; the copy can overlap delivery in the conversation by design. Waiters and cached counters watch the delivery ledger as well as the log and cursor. The dashboard, inbox command and tmux counter share the mailbox's unread rules. Dashboard and tmux reads request nonblocking inbox locks; the CLI retains its normal launch and inbox locks. The standalone counter forks a short-lived reader with a one-second deadline, without loading the CLI or starting another interpreter. History and ledgers have no size cap. Missing history with saved inbox evidence is damage in every reader. Native retry checks the final disposition after each evidence refresh; a failed tagged upgrade written by a pre-update runner is final even without the new dismissal marker.

The recipient sees `Message from NAME:` followed by the message, as built by `native.framed_text`. The attempt is saved as `submitting` before the native call, so a crash can't turn an uncertain send into a resend. [Harness adapters](harness-adapters.md#delivery-results-and-receipts) lists the states.

## Reading replaced records

Readers of mv2's atomically replaced state explicitly use `store.retry_missing`, `open_replaced`, or a presence predicate that uses the same retry. An `ENOENT` from the filesystem gets at most five attempts, with 5 ms pauses and a 50 ms retry budget. Other errors and invalid content keep the reader's existing policy. The budget bounds retry delays, not an operating-system call that stalls. Generic `store.read` and write operations do not retry. Known-path readers can stop after a missing parent directory corroborates absence: replacing a file does not remove its containing directory. Directory relocation and filesystem outages are separate from this per-record retry.

The final missing-file result stays local to the reader: absent focus is off, an absent cursor is zero, optional ledgers and indexes are empty, and required launch authority is an error. A missing runtime with launch history still enters the independent terminal/process checks before resume or wake; it is not proof of a never-launched agent. Inbox ledgers do not inherit runtime-only authority or size requirements; their descriptor-sized snapshot may grow with message history. Exclusive reservations, answer pickup, locks, native operations, and filesystem mutations are never replayed by this helper.

Listings and doctor check their optional records in shared retry rounds, then use that operation's absence snapshot for diagnostics. The list, doctor, inspect and plugin-summary CLI commands include universe discovery and optional metadata in the same batch. Diagnostic discovery follows only the requested universe topology, never agent launch or artifact directories. Each topology walk checks sibling universe markers together in fresh retry rounds, so abandoned subverse folders do not each add a delay. Cold plugin-load bookkeeping keeps its separate fresh read; it is not part of the diagnostic absence snapshot. First-use migration reads manifest filenames without loading unrelated plugin switches. Events with no subscribing manifest also need no switch read. Repeated diagnostic reads try the file again, but do not repeat the delay for an absence already observed in that operation. Fresh launch, delivery, permission and locked write reads never consult this snapshot. Universe discovery batches candidate markers in precedence order; a recovered nearer universe still wins. First-use migration returns the resolved root so the command does not repeat the discovery walk. Inbox readers batch those checks while holding their inbox lock. A file observed present still uses its normal reader; unreadable files retain their diagnostics. No absence snapshot carries into a launch, delivery, answer, or locked session reconciliation.

Delivery helpers also batch the optional cursor and ledger checks within each inbox lock. Each acquisition takes a fresh snapshot; neither absence nor parsed delivery state carries across locks. Files that were present still get their normal replacement retries if they disappear at the read. This avoids separate waits for the cursor, current ledger and legacy ledger without changing their meaning.

Repeated polls distinguish a hint from authority. A listener's optimistic focus hint never sleeps, but its locked check before output still retries replacement gaps. Cleanup waiting also uses a direct hint about cleanup and runner state, then a fresh retrying check with one full role scan before returning or refusing at its deadline. A transport's fresh connection UUID has no earlier role record before its opening publication; closing still reads its saved metadata normally. Storage discovery batches missing request names across retained launch folders, without following symlinks, and reads recovered or present requests normally. Bootstrap construction does not load plugin switches when no manifest declares a bootstrap contribution; extensions that cannot fit the requested harness mode also need no grant read. Fitting extensions and plugin dispatch keep their fresh access checks. Recovery monitors read delivery state once, after the read-only native observation and under the launch lock, before publishing idle proof; reconnect admission still checks again.

The session lineage journal, `sessions.jsonl`, is appended and flushed, never atomically replaced. Its parser and presence checks return immediately when the file is absent; permission and I/O errors never prove absence. Every later authority check reads it afresh, so a newly appended transition is visible without a negative cache. Replaced properties and runtime records keep their retries.

Tmux's optimistic observation does not wait for a missing registry entry: it falls through to the locked recovery path, which retries before deciding what server authority exists. Cleanup does not retry either, but a missing read never permits deleting an identity record. It may prune only its own verified lock; removing the identity still needs a positively read record whose original process is proven gone.

One-shot run placement checks an ancestor's universe marker only when the destination falls within that ancestor's resolved `state`, `subverses`, or `trash` folder. Other ancestors cannot forbid that placement, so they need no marker read. Relevant markers retain their replacement retries, including when a protected folder is a symbolic link.

Plugin event workers recheck the journal ID before dispatching a batch. If the journal was created or replaced during the read, the worker defers the batch to its next poll instead of recording progress under the wrong journal.

## How identity works

| ID | Where it lives |
| --- | --- |
| Universe ID | `universe.json` `id` |
| Agent ID | `state/agents/ID/properties.json` `id` |
| Launch ID | `runtime.json` `launch_id`; new for every spawn and resume |
| Native session ID | `properties.json` `native_session_id`: Claude's assigned UUID, Codex's thread, Pi's session |

Each agent pane carries three tmux pane options: `@mv2_universe_id`, `@mv2_agent_id` and `@mv2_launch_id`. `terminal.tmux.observe()` lists panes with these fields, and counts a pane only when all three match the agent's records:

```text
runtime.json tmux.pane --> pane missing           -> dormant
                       --> tags don't match        -> dormant
                       --> tags match, pane dead    -> stopped
                       --> tags match, ending_at    -> ending
                       --> tags match               -> alive
tmux unreachable, verified server alive             -> recover socket, observe again
recovery unavailable or server identity uncertain    -> unknown
```

New tmux and native sockets live under the private `~/.multiverse/run` directory, or `MV2_RUN_DIR`. Core validates native socket length before native preparation. The runtime records the resolved run directory; the runner and damaged-runtime recovery qualify the endpoint against the same saved launch environment. Older requests keep their `/tmp` endpoint and the legacy tmux server remains in discovery.

Socket recovery checks the original server process before signalling it and is serialized per canonical address. The answering server supplies its PID and start epoch with the pane tags, so an unchanged healthy server needs no process queries. New launches retain its process identity, and the per-user registry covers future callers in other universes. Legacy launches can prove the server through their verified runner's parent. A conflicting answering server is never displaced.

Window and session names are only labels, so renaming them changes nothing. Every command that signals or captures a pane checks the tags first (`tmux.owned`).

An unbound tmux backend permits only server-wide observation and creation, plus fixed metadata reads used to prove a legacy pane or the calling terminal. All other commands, including new command names and targeted reads, require a server PID and start epoch before they can run. Damaged-runtime discovery checks the exact pane tags and retains that server identity in its recovered record and backend. Attach binds the originating terminal too: client operations and the delayed return to the dashboard refuse a replacement server even when it reuses the old pane number.

The runner and native frontend receive the agent's IDs and this launch's `MV2_LAUNCH_ID`, `MV2_CONTROL_SOCKET` and `MV2_TMUX_SOCKET`. Connected Codex tools instead receive agent-stable identity, including `MV2_IDENTITY_MODE`, without those three launch-specific values. `report`, `inbox` and the sender of `send` validate that identity against the exact native thread and saved storage scope. `session-bind` can only confirm the existing binding in this mode. An older loaded conversation without the mode marker can use these data callbacks only after the adapter opts in and core proves its retained bridge request. The explicit data route also covers sender identity and verify-only binding. Runner updates and control calls retain their launch checks. Child spawning accepts explicit stable agent proof and finds its tmux socket and run directory in the same verified current runtime; a retained-launch compatibility caller still needs the current launch identity for this operation. See [environment](../reference/environment.md) and [session isolation](../design/session-isolation.md).

A plain terminal's `session-bind` normally claims its conversation directly. If its sandbox denies writing the universe's session-claim lock, including through a read-only filesystem, an exact current-launch caller can leave a bounded request in its own launch folder. Its runner applies the same claim under the universe lock and returns a correlated result; the command confirms the saved native ID before reporting success. A stale, ending or conflicting launch is refused. The handoff creates neither an agent folder nor a launch folder, and needs no broader sandbox grant. Operator binding and bridge identity verification keep their existing paths.

Each launch requests current `MV2_CLI`, `MV2_PYTHON` and `PATH` values from the launching installation. Core records that request in `request.json.agent_environment` and the agent's latest `properties.json.agent_environment`. Those maps do not attest to a shared native conversation's cached environment. Executable paths are not part of agent authority. See [environment](../reference/environment.md) for cached tool settings and using the current installation.

## How the dashboard is built

`ui/app.py` holds the Textual app: layout, bindings, refreshing from the backend and moving between scopes. Its table (`fleet.py`), tree editing (`editing.py`) and agent commands (`commands.py`) are mixins of the app, so their actions stay app actions. `backend.py` is the dashboard's only path to the tools. File and tmux work runs in Textual workers, off the event loop.

Each part that a view can extend has a registry:

| To add | Register in |
| --- | --- |
| An agent column | `ui/columns.py`: a `Column` (or a subclass overriding `agent` or `group_value`) in `COLUMNS` |
| A local view widget type | `ui/panels.py`: a `Panel` with `parse` and `compose` in `PANELS` |
| A line in the agent inspector | `ui/inspector.py`: a function in `AGENT_FACTS` |
| A color | `ui/theme.py`; `app.tcss` reads it as `$mv-NAME` |
| A settings screen | `ui/settings.py`: `Field`s on a `FieldScreen` |

The other modules are:

- `tree.py` turns a snapshot into folder rows.
- `views.py` reads a universe's `views/default.json`.
- `drafts.py` holds the inline new-agent and new-subverse rows.
- `inspector.py`, `screens.py`, `widgets.py` and `compaction.py` are the remaining widgets and screens.

`examples/tui_demo.py` has `DemoBackend`, an in-memory backend with the same methods as `LocalBackend`. The demo, the screenshot renderer and `tests/test_tui.py` run the real app against it.
