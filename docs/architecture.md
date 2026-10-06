# Source layout

`src/multiverse` is split by what code is about: what is stored in a universe,
what runs, the terminal it runs in, and how people see it.

| Package or module | Holds |
| --- | --- |
| `universe/` | Everything on disk. The package itself resolves universes and walks the subverse tree. `agents` holds agent records (names, the runtime record, reported phase and summary); `mailbox` holds each agent's message log, read position and delivery tracking; `templates` installs and refreshes definitions; `catalog` lists skills, commands and workflows and builds the bootstrap; `relocate` and `trash` move, remove and keep removed things. |
| `runtime/` | Everything that runs. `lifecycle` launches (spawn, resume, wake), waits for readiness, lists agents with their liveness, and kills; `native` executes adapter instructions and delivers messages; `rpc` is the JSON-RPC transport and launch relay; `recovery` reconnects idle sessions; `runner` is the process inside each agent's tmux pane. |
| `terminal/` | tmux: commands, pane ownership tags, liveness from panes, and the status line script tmux runs. |
| `harnesses/` | Adapters: one package per built-in harness, behind the contracts in `harnesses/types.py`. |
| `compaction/` | Tree compaction, independent of any one harness. |
| `tools/` | One module per `mv2` command. The dashboard calls the same functions. |
| `ui/` | The dashboard (see below). |
| `display.py` | Text for people that the CLI and the dashboard share: summaries, labels, usage. |
| `store.py` | Atomic JSON writes, file locks, IDs and timestamps. |
| `cli.py` | The `mv2` command line, built from `tools/`. |

Dependencies point one way: `store` and `terminal` depend on nothing else here;
`universe` on those; `runtime` on `universe`, `terminal` and `harnesses`; `tools`
and `ui` on everything below them. Waking a stopped agent is the one place
`runtime.native` reaches back into `runtime.lifecycle`, inside the function.

## The dashboard

`ui/app.py` holds the Textual app: layout, bindings, refreshing from the backend
and moving between scopes. Its table (`fleet.py`), tree editing (`editing.py`)
and agent commands (`commands.py`) are mixins of the app, so their actions stay
app actions. `backend.py` is the only place the dashboard reaches the tools.

Each part a view can extend is a registry:

| To add | Register in |
| --- | --- |
| An agent column | `ui/columns.py`: a `Column` (or subclass overriding `agent` or `group_value`) in `COLUMNS` |
| A local view widget type | `ui/panels.py`: a `Panel` with `parse` and `compose` in `PANELS` |
| A line in the agent inspector | `ui/inspector.py`: a function in `AGENT_FACTS` |
| A color | `ui/theme.py`; `app.tcss` reads it as `$mv-NAME` |
| A settings screen | `ui/settings.py`: `Field`s on a `FieldScreen` |

Other modules: `tree.py` turns a snapshot into folder rows; `views.py` reads a
universe's `views/default.json`; `drafts.py` holds the inline new-agent and
new-subverse rows; `inspector.py`, `screens.py`, `widgets.py` and
`compaction.py` are the remaining widgets and screens.

## Old paths kept for running agents

Three top-level files are stubs for agents launched before this layout:
`runner.py` (runners rerun their own path for wrapper cleanup),
`statusline.py` (tmux sessions run it by path) and `recovery.py` (runners start
`python -m multiverse.recovery`). Each forwards to its new module. Remove them
once no agent launched before the change is still running or stopped with its
wrapper; a resume relaunches with the new paths.

Stubs cover only the paths that start fresh processes. A runner that was already
running keeps the old modules it loaded, and some of its later imports
(automatic reconnect, wake bookkeeping, startup message delivery) would load the
new code into that process, which cannot work with the old `store` and
`universe` already in memory. After pulling this change, kill and resume each
running agent once. Its conversation and records are kept.
