# Environment variables

mv2 reads and sets the variables below. You set only a few of them. The runner and native frontend receive launch identity; connected Codex tools use agent-stable identity instead.

## Variables you set

| Variable | What it does |
| --- | --- |
| `MV2_UNIVERSE` | The universe to use when a command has no `-u`. Without it, mv2 searches the current directory and its parents. When no universe is found and no command is given, `mv2` creates one in the current directory, unless `-u` or `MV2_UNIVERSE` is set. |
| `MV2_RUN_DIR` | Absolute private runtime directory, default `~/.multiverse/run` (mode `0700`). New launches use `tmux.sock` and `native/LAUNCH/rpc.sock` inside it. Set a shorter path if the complete native socket address exceeds 103 bytes on macOS or 107 on Linux. |
| `MV2_TMUX_SOCKET` | Overrides the tmux socket for new agent terminals; otherwise it is `MV2_RUN_DIR/tmux.sock`. It does not move native bridge sockets. Existing agents stay on the socket recorded in their `runtime.json`. |
| `NO_COLOR` | The dashboard shows no color. Agents don't inherit it; see [NO_COLOR and agents](#no_color-and-agents). |

In a development checkout, `. scripts/dev-env.sh` exports `MV2_UNIVERSE`, `MV2_TMUX_SOCKET`, `MV2_CLI` and `MV2_PYTHON` for the checkout's own universe. See [Contributing](../develop/contributing.md).

Older launches keep their saved tmux and `/tmp/mv2-native-LAUNCH/rpc.sock` addresses until stopped and resumed. Discovery checks the old `tmux -L multiverse-v2` server too, so an upgrade never treats its running agents as absent.

## Variables mv2 sets in agent sessions

mv2 removes every inherited `MV2_*` variable before it starts an agent, then sets these. A harness's `env` setting can't override them.

| Variable | Value | Used for |
| --- | --- | --- |
| `MV2_UNIVERSE` | The agent's universe directory | The default universe for every `mv2` command the agent runs. |
| `MV2_AGENT_ID` | The agent's UUID | The default `--agent` for `inbox`, `report` and `session-bind`. It also marks messages from `send` as from this agent. |
| `MV2_AGENT_DIR` | `state/agents/UUID/` in the universe | Where the Claude mod, the Pi extension and tree compaction read and write the agent's files. |
| `MV2_LAUNCH_ID` | This launch's UUID; older connected Codex tools may retain it | Launch-scoped callbacks refuse an earlier launch's UUID with "Outdated launch". Runner updates and control calls stay launch-scoped. |
| `MV2_IDENTITY_MODE` | `codex-agent-v1` in connected Codex tools | Makes `report`, `inbox` and the sender of `send` validate the agent, native thread and saved storage scope. Missing or incomplete proof is refused; it never becomes an operator call. |
| `MV2_CLI` | The `mv2` executable: the checkout's `bin/mv2`, else the running `mv2`, else `mv2` on `PATH` | Agents run `"$MV2_CLI" COMMAND`. Its directory is also put first on the agent's `PATH`. |
| `MV2_PYTHON` | The Python interpreter running mv2 | The Claude mod and the Pi extension report status, usage and compaction with it. |
| `MV2_SOURCE` | The import root of the running mv2 package | Python callbacks prepend it to their child's `PYTHONPATH`, so they use the same checkout or installation. The agent's own `PYTHONPATH` is unchanged. |
| `MV2_CONTROL_SOCKET` | `MV2_RUN_DIR/native/LAUNCH/rpc.sock`; current launches omit it from connected Codex tools | The private socket of this launch's bridge. The Pi extension listens on it. Older loaded Codex conversations may retain an obsolete value; it does not authorize a current control call. |
| `MV2_RUN_DIR` | This launch's resolved runtime directory; omitted from connected Codex tools | Child launches read it from the verified current runtime, alongside its tmux socket, rather than from cached tool settings. |
| `MV2_TMUX_SOCKET` | The agent's tmux socket; current launches omit it from connected Codex tools | Identifies the frontend's server. Older loaded conversations may retain it. Child launches use the verified calling agent's runtime record to find its current server. |
| `MV2_COMPACTION` | `1` | The compaction plugin sets it for connected Claude launches while compaction is on. Its compaction hook acts only then. See [plugin variables](#plugin-variables). |

Connected Codex tools keep the same agent identity when another Codex client drives the conversation. Current launches do not pass `MV2_LAUNCH_ID`, `MV2_CONTROL_SOCKET`, `MV2_TMUX_SOCKET` or `MV2_RUN_DIR`. Codex supplies `CODEX_THREAD_ID`; mv2 also checks the saved native storage paths. A `/btw` side conversation inherits these settings, but its different thread ID cannot authorize it as the main agent. It can still run unscoped mv2 commands as the operator; this identity check is not an execution sandbox. A child agent still launches on its parent's tmux server, using the parent's current runtime record rather than a socket saved in the conversation. If that record is missing or damaged, spawning a child names the file to restore; it never falls back to another server. An older runtime without a recorded run directory uses the caller's current `MV2_RUN_DIR` or the default for new native sockets.

Each launch requests `MV2_CLI`, `MV2_PYTHON`, `MV2_SOURCE` and `PATH` from the mv2 installation starting it. These paths are tool settings, not identity proof. The saved maps describe requests, not measured values in a loaded conversation. Codex can retain an idle conversation for up to 30 minutes after its last client leaves by default, so restarting the frontend does not guarantee new tool settings.

Use the current mv2 installation for callbacks from an older Codex bridge conversation. Its retained launch request and the current thread, storage, workspace and agent records must all match. This permits the agent's own `report`, `inbox`, `focus`, `unfocus`, sending as the agent and verification of the existing binding; child spawning through this compatibility route and all runner/control callbacks keep their launch checks. A separate old mv2 checkout still runs its old code and is not covered. See [upgrade identity proof](../design/shared-conversations.md#upgrade-running-codex-bridge-agents).

Outside an agent session, leave `MV2_AGENT_ID`, `MV2_AGENT_DIR`, `MV2_LAUNCH_ID` and `MV2_IDENTITY_MODE` unset. In an agent context, `send` signs as that agent and `inbox` reads its inbox. Copying only part of the identity fails validation.

An operator can read `inbox --agent NAME` and write `report --agent NAME` even
when the runtime record is damaged. This does not grant an agent caller any
authority or repair the runtime record.

## Plugin variables

| Variable | What it does |
| --- | --- |
| `MV2_PLUGINS` | `off` (or `0`, `false`, `no`) turns on safe mode. No plugin loads at all, bundled ones included, in that command or dashboard or in the launches it makes. Agents don't inherit it. See [plugins](../use/plugins.md#safe-mode). |
| `MV2_EFFECTS` | `observe`, `spend` or `act`: no plugin in that command or dashboard has more than this access level, whatever it was granted. Any other value counts as `observe`. Agents don't inherit it. See [plugins](../use/plugins.md#access). |
| `MV2_PLUGIN_PATH` | Directories to find plugins in, separated by `:`. Each one is a plugin or a folder of plugins. Plugins found here come before every other source and need no trust. Agents don't inherit it. See [making plugins](../develop/plugins.md#quickstart). |
| `MV2_<NAME>`, `MV2_<NAME>_*` | Plugin NAME's launch additions set these in agent sessions. NAME is the plugin's name upper-cased, with hyphens as underscores, as in the compaction plugin's `MV2_COMPACTION`. A plugin can set no other `MV2_*` variable. |

Plugin code finds its universe and agent from `MV2_UNIVERSE`, `MV2_AGENT_DIR` and `MV2_AGENT_ID`. A helper action, `"$MV2_PYTHON" -m multiverse.plugins [-u UNIVERSE] run PLUGIN ACTION`, works in the `-u` universe. Without `-u`, it uses `MV2_UNIVERSE`, and otherwise the universe the current directory is in. `mv2 plugins host start` starts the plugin host on the tmux server that `MV2_TMUX_SOCKET` names, or else on `MV2_RUN_DIR/tmux.sock`.

## NO_COLOR and agents

The bundled harnesses remove `NO_COLOR` from agent environments. The tmux server keeps the environment of whatever first started it. That is often an agent's tool shell, which sets `NO_COLOR`. Agent panes, however, are interactive terminals.

To pass `NO_COLOR` to a harness anyway, set it in that harness's `env`, or replace its `unset_env` list. See [harness.json](files.md#schema-2).

## Other variables

| Variable | What mv2 does with it |
| --- | --- |
| `CODEX_HOME`, `CODEX_SQLITE_HOME` | Reads them from its own environment to find Codex sessions and the shared Codex app-server. Set them where mv2 runs, because a harness's `env` can't set them. |
| `CLAUDE_CONFIG_DIR` | Reads it from the spawning environment to choose Claude's storage. Set it where mv2 runs, because a harness's `env` can't set it. A resume keeps the recorded value or absence; if it ignores a different value, its result names the kept storage and the ignored override. |
| `PI_CODING_AGENT_DIR` | Records its value or absence at spawn and replays it on resume, including in other harnesses that may spawn Pi. Set it where mv2 runs, or in Pi's harness `env`, whose value spawn records instead. A resume keeps the recorded value or absence, and names a changed setting or shell value it ignored; `mv2 resume --start-fresh` takes the current setting. |
| `CODEX_THREAD_ID`, `CLAUDECODE` | Removes the parent's values before a launch. Codex supplies its own `CODEX_THREAD_ID` to its tools. |
| `CLAUDE_CODE_CHILD_SESSION` | Always removes it when inherited. Claude Code sets it for its own tool subprocesses, and a Claude agent that inherits it saves no transcript. A harness's `env` can still set it. |
| `CLAUDE_CODE_SSE_PORT`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN` | Always removes them when inherited: an agent spawned from inside Claude would get the parent's IDE port, and its inbox and secret. A harness's `env` can still set them. |
| `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_CODE_BRIDGE_SESSION_ID`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_PID`, `CLAUDE_EFFORT` and other parent-session markers | Removes inherited Claude identity, worktree and invoked-skill markers for every harness, including from a tmux server started inside Claude. A harness's `env` can still set them. `CLAUDE_CODE_TMPDIR` passes through, because it is usually your own setting. |
| `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` | Sets it to `1` for Claude bridge launches, because the multiverse mod needs it. Removes an inherited one from plain launches, unless the harness's `env` sets it. |
| `TMUX`, `TMUX_PANE` | `mv2 attach` reads them to return to the dashboard pane it was started from. |

The launch records every built-in harness's storage variables and replays them
on resume, including variables for other harnesses an agent may spawn. They come
from the spawning command, not the tmux server. Connected Codex tool shells
receive these launch-selected values and exclude absent storage keys from the
inherited daemon environment. A value you explicitly set in Codex's
`shell_environment_policy.set` can supply a variable the launch left absent;
mv2 does not replace absence with an empty value. A recorded non-empty launch
value still wins. In a direct launch, an absent variable is explicitly unset in
the child. Older snapshots missing another harness's variable use a
recorded launch request if it has that value; otherwise they keep that key unknown
and inherit it as before. Unknown is different from a recorded unset selection.
Claude preserves both the exact `CLAUDE_CONFIG_DIR` value, including its alias
spelling on resume, and whether it was unset; setting it to a default-looking path can change where Claude reads its
login and global settings. Codex selects canonical storage paths for its
frontend, service and tools. The native child receives the launching command's
`HOME`, and storage variables recorded as absent are explicitly
removed, so an older tmux server cannot supply a different home. A resume that
would change the saved conversation's storage, including a plain launch whose
saved path now follows a different symlink, is refused before its launch records
change. The error shows both paths; restore the original storage at the saved
path, or spawn a new agent for the new storage. A hand-restored spelling of the
same directory can be normalized against the retained canonical home or launch
request; this does not permit a saved path to point at a different directory.
If a legacy Codex spelling, including a hand-restored snapshot, has no retained
canonical home, mv2 first proves the conversation in that candidate home. It
canonicalizes the adopted storage once, then refuses later alias retargets.
Claude keeps its literal variable while comparing the canonical conversation
home for the same refusal.
Resume notices also name ignored overrides of another harness's recorded storage
variables, so a child harness keeps the expected home.

Older Claude and Codex agents may lack a storage snapshot. Existing proven
Codex service scopes or explicit launch requests remain usable. Otherwise mv2
looks for that exact conversation in candidate homes from every retained launch
request, the saved storage map, session home and service scope, the calling shell
(or its harness default), and inherited values from tmux servers the
records name. A request's value or explicit absence overrides that server's
value for the same variable; a server cannot add a different home for it. Missing
or damaged runtime authority does not discard this storage evidence or restore
permission to control an old process. An unreadable or damaged retained request
blocks adoption and `--start-fresh`; recovery requires readable absence in every
candidate, even when the latest launch failed before publishing its request.

A missing recorded storage root or conversation store is unknown for `--start-fresh`, since its volume may be unmounted or its folder renamed. The recorded home must have a readable `projects/` store for Claude or `sessions/` store for Codex. Codex also reads `archived_sessions/` when it exists.

Restore the missing folder, or create it to confirm that its conversation is absent. An existing empty store counts as absent. Only a caller-selected destination that no retained candidate names may lack these folders.

An old request recording neither a storage location nor `HOME` adds no location
of its own; mv2 still checks the calling shell and the terminal servers.
Those are the server in `runtime.json` and every server a retained request
names in `MV2_TMUX_SOCKET`, only for storage variables that launch inherited
rather than recorded. They stay evidence when `runtime.json` is lost or a later
launch failed before it had a terminal. A server you explicitly select also
supplies caller-origin candidates, which need no existing store unless retained
evidence names the same home. No other server is queried; the caller's harness default uses the caller's `HOME`,
alongside retained storage evidence.
Each terminal variable is read whole, including embedded
newlines and trailing whitespace; unreadable or ambiguous escaped output blocks adoption. The adapter
reads Claude's session transcript or Codex's
rollout metadata; it starts no native process. Exactly one home must contain the
conversation, with absence proven in every other candidate. mv2 then records the
proven values, including the HOME used for a default storage path, and says which
home it adopted. Unknown foreign-harness keys stay
unknown; adoption does not force them unset.

If no candidate, multiple candidates or unreadable evidence prevents proof,
resume refuses before changing the launch records and lists the candidate paths.
Resume from a shell whose `CLAUDE_CONFIG_DIR` or `CODEX_HOME` names the home
holding the conversation; restore read access or resolve duplicate copies first
when the error names those problems. A current environment alone is never proof
that a legacy conversation used that storage.

If every candidate is readable and none holds the conversation, mv2 can start
fresh in the calling shell's storage only when its retained records prove no
native child ever started, or no launch was published, and show no acceptance
or turn evidence. This applies to an assigned session ID, such as Claude's; a
reported native ID already proves a native conversation existed. The resume
result says once that it is starting fresh, including the selected storage. If the agent might have run a turn, mv2
refuses: restore the missing conversation, or remove the agent with
`mv2 remove NAME` and spawn a new one. Missing history alone is not proof that
the conversation was unused.
