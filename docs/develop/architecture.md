# Architecture

This page shows how mv2's source is laid out, what runs where, and how a launch, a message and an agent's identity move through the code. Read [concepts](../concepts.md) first for the terms.

## Package map

Everything lives in `src/multiverse/`.

| Package or module | Holds |
| --- | --- |
| `cli.py` | The `mv2` command line. `COMMANDS` lists every command, and each maps to a module in `tools/`. |
| `tools/` | One module per command, each with `configure(parser)` and `run(...)`. The dashboard calls the same `run` functions. `runtime_update.py` is for harness integrations, not a command. |
| `universe/` | Everything on disk. The package resolves universes and walks subverses. Its modules are `agents` (agent records), `mailbox` (inbox, read position, delivery tracking), `templates` (install and refresh), `catalog` (skills, commands, workflows, bootstrap), `relocate` and `trash`. |
| `runtime/` | Everything that runs. Its modules are `lifecycle` (spawn, resume, wake, readiness, listing, kill), `native` (runs adapter specs, delivers messages, keeps receipts), `rpc` (JSON-RPC transport and launch relay), `recovery` (reconnecting idle Codex terminals) and `runner` (the process in each agent's pane). |
| `terminal/` | tmux: commands, pane tags, liveness from panes, and the status line script tmux runs. |
| `harnesses/` | Adapters. `types.py` is the contract and `base.py` holds the shared argv rendering. There is one package each for `claude`, `codex` and `pi`, plus `legacy` for schema-1 definitions. See [harness adapters](harness-adapters.md). |
| `plugins/` | The plugin system: discovery, manifests, trust, `plugins.json` and per-plugin settings, the loader and its failure boundaries, the `mv` API, launch contributions (`harness.py`), the event journal (`events.py`) and the plugin host (`host.py`). Bundled plugins live in `plugins/builtin/`. See [plugins](plugins.md). |
| `compaction/` | Only a stub for Claude agents launched before tree compaction became a plugin. Compaction itself is the `plugins/compaction` submodule. |
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

`runtime.native` imports `runtime.lifecycle` inside one function, to wake a stopped agent. That import is the only cycle.

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
| Status line (`terminal/statusline.py`) | tmux, while a client is attached | Renders the agent's tmux bar. It is read-only and uses only the standard library. |
| Wrapper cleanup and recovery worker | The runner, detached | The cleanup removes the pane after the native process exits. The worker reconnects an idle Codex terminal after a relay loss. |
| `python -m multiverse.compaction` | The Claude mod and its hooks | Builds summary trees, installs a compaction, and refuses Claude's own. |

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

Adapters only describe a launch. They return a `CommandSpec` and never touch tmux or runtime files. Core owns processes, tmux, records and locks. A failed launch keeps its record with the error, so `mv2 resume` can retry.

## How a message flows

```text
mv2 send AGENT                                      tools/send.py, under .send.lock
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

The recipient sees `Message from NAME:` followed by the message, as built by `native.framed_text`. The attempt is saved as `submitting` before the native call, so a crash can't turn an uncertain send into a resend. [Harness adapters](harness-adapters.md#delivery-results-and-receipts) lists the states.

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
tmux unreachable                                   -> unknown
```

Window and session names are only labels, so renaming them changes nothing. Every command that signals or captures a pane checks the tags first (`tmux.owned`).

The native process gets `MV2_UNIVERSE`, `MV2_AGENT_ID`, `MV2_AGENT_DIR`, `MV2_LAUNCH_ID`, `MV2_CLI`, `MV2_PYTHON`, `MV2_CONTROL_SOCKET` and `MV2_TMUX_SOCKET`. See [environment](../reference/environment.md) for each one. Callbacks from the agent (`report`, `inbox`, `session-bind`, `runtime_update`) compare `MV2_LAUNCH_ID` with `runtime.json` and refuse an outdated launch. [Session isolation](../design/session-isolation.md) explains the limits of these checks.

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
