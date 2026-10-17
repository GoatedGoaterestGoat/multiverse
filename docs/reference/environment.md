# Environment variables

mv2 reads and sets the variables below. You set only a few of them, and mv2 sets the rest inside every agent session.

## Variables you set

| Variable | What it does |
| --- | --- |
| `MV2_UNIVERSE` | The universe to use when a command has no `-u`. Without it, mv2 searches the current directory and its parents. When no universe is found and no command is given, `mv2` creates one in the current directory, unless `-u` or `MV2_UNIVERSE` is set. |
| `MV2_TMUX_SOCKET` | The path of the tmux socket for new agent terminals. Without it, agents run on the tmux server named `multiverse-v2` (`tmux -L multiverse-v2`). Existing agents stay on the socket recorded in their `runtime.json`. |
| `NO_COLOR` | The dashboard shows no color. Agents don't inherit it; see [NO_COLOR and agents](#no_color-and-agents). |

In a development checkout, `. scripts/dev-env.sh` exports `MV2_UNIVERSE`, `MV2_TMUX_SOCKET`, `MV2_CLI` and `MV2_PYTHON` for the checkout's own universe. See [Contributing](../develop/contributing.md).

## Variables mv2 sets in agent sessions

mv2 removes every inherited `MV2_*` variable before it starts an agent, then sets these. A harness's `env` setting can't override them.

| Variable | Value | Used for |
| --- | --- | --- |
| `MV2_UNIVERSE` | The agent's universe directory | The default universe for every `mv2` command the agent runs. |
| `MV2_AGENT_ID` | The agent's UUID | The default `--agent` for `inbox`, `report` and `session-bind`. It also marks messages from `send` as from this agent. |
| `MV2_AGENT_DIR` | `state/agents/UUID/` in the universe | Where the Claude mod, the Pi extension and tree compaction read and write the agent's files. |
| `MV2_LAUNCH_ID` | This launch's UUID | `inbox`, `report` and `session-bind` refuse with "Outdated launch" when it doesn't match `runtime.json`. This stops a replaced process from updating a resumed agent. |
| `MV2_CLI` | The `mv2` executable: the checkout's `bin/mv2`, else the running `mv2`, else `mv2` on `PATH` | Agents run `"$MV2_CLI" COMMAND`. Its directory is also put first on the agent's `PATH`. |
| `MV2_PYTHON` | The Python interpreter running mv2 | The Claude mod and the Pi extension report status, usage and compaction with it. |
| `MV2_CONTROL_SOCKET` | `/tmp/mv2-native-LAUNCH/rpc.sock` | The private socket of this launch's bridge. The Pi extension listens on it. |
| `MV2_TMUX_SOCKET` | The socket of the agent's own tmux server | Agents that spawn agents put them on the same server. |
| `MV2_COMPACTION` | `1` | The compaction plugin sets it for connected Claude launches while compaction is on. Its compaction hook acts only then. See [plugin variables](#plugin-variables). |

Outside an agent session, leave `MV2_AGENT_ID` and `MV2_LAUNCH_ID` unset. If they are set, `send` signs messages as that agent, and `inbox` reads that agent's messages.

## Plugin variables

| Variable | What it does |
| --- | --- |
| `MV2_PLUGINS` | `off` (or `0`, `false`, `no`) turns on safe mode. No plugin loads at all, bundled ones included, in that command or dashboard or in the launches it makes. Agents don't inherit it. See [plugins](../use/plugins.md#safe-mode). |
| `MV2_PLUGIN_PATH` | Directories to find plugins in, separated by `:`. Each one is a plugin or a folder of plugins. Plugins found here come before every other source and need no trust. Agents don't inherit it. See [making plugins](../develop/plugins.md#quickstart). |
| `MV2_<NAME>`, `MV2_<NAME>_*` | Plugin NAME's launch additions set these in agent sessions. NAME is the plugin's name upper-cased, with hyphens as underscores, as in the compaction plugin's `MV2_COMPACTION`. A plugin can set no other `MV2_*` variable. |

Plugin code finds its universe and agent from `MV2_UNIVERSE`, `MV2_AGENT_DIR` and `MV2_AGENT_ID`. A helper action, `"$MV2_PYTHON" -m multiverse.plugins [-u UNIVERSE] run PLUGIN ACTION`, works in the `-u` universe. Without `-u`, it uses `MV2_UNIVERSE`, and otherwise the universe the current directory is in. `mv2 plugins host start` starts the plugin host on the tmux server that `MV2_TMUX_SOCKET` names, or else on `multiverse-v2`.

## NO_COLOR and agents

The bundled harnesses remove `NO_COLOR` from agent environments. The tmux server keeps the environment of whatever first started it. That is often an agent's tool shell, which sets `NO_COLOR`. Agent panes, however, are interactive terminals.

To pass `NO_COLOR` to a harness anyway, set it in that harness's `env`, or replace its `unset_env` list. See [harness.json](files.md#schema-2).

## Other variables

| Variable | What mv2 does with it |
| --- | --- |
| `CODEX_HOME`, `CODEX_SQLITE_HOME` | Reads them from its own environment to find Codex sessions and the shared Codex app-server. Set them where mv2 runs, because a harness's `env` can't set them. |
| `CLAUDE_CONFIG_DIR` | Reads it from its own environment to find Claude transcripts. Set it where mv2 runs, because a harness's `env` can't set it. |
| `CODEX_THREAD_ID`, `CLAUDECODE` | Always removes them from agent environments, so an agent started from inside Codex or Claude Code doesn't inherit its parent's identity. |
| `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` | Sets it to `1` for Claude bridge launches, because the multiverse mod needs it. |
| `TMUX`, `TMUX_PANE` | `mv2 attach` reads them to return to the dashboard pane it was started from. |
