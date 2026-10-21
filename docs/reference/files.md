# Files

Everything mv2 keeps for a universe lives in its `.multiverse/` directory. You edit the definition files. mv2 owns the identity, state and trash.

## Layout

```text
project/
  .multiverse/
    universe.json              # identity, name and workspace
    template-files.json        # fingerprints of installed definitions, for mv2 refresh
    bootstrap.md               # instructions every agent starts with
    plugins.json               # which plugins are on here
    plugin-settings/NAME.json  # each plugin's settings for this universe
    plugins/NAME/              # this universe's own plugins, run once trusted
    statusline.json            # Claude status line and tmux status bar items
    .gitignore                 # ignores state/ and trash/
    harnesses/NAME/
      harness.json             # adapter and launch settings
      README.md                # notes on the adapter
    skills/NAME/
      entry.json               # catalog description
      SKILL.md                 # the skill
    commands/NAME/
      entry.json
      prompt.md
    workflows/NAME/
      entry.json
      prompt.md
    views/default.json         # dashboard title, accent, columns and widgets
    artifacts/                 # optional files that view widgets show
    state/
      agents/UUID/             # one directory per agent
      plugins/NAME/            # each plugin's logs and state for this universe
      events/                  # the event journal, while a plugin here subscribes to events
    subverses/NAME/            # a nested universe with this same layout
    trash/STAMP-NAME/          # removed agents and subverses, files replaced by refresh
```

## Who edits what

| Path | Owner | Notes |
| --- | --- | --- |
| `bootstrap.md`, `plugins.json`, `plugin-settings/`, `plugins/`, `statusline.json`, `harnesses/`, `skills/`, `commands/`, `workflows/`, `views/`, `artifacts/` | You | Definition files. `init` and `subverse` install them from a template; `mv2 refresh` updates the ones you haven't edited. |
| `universe.json` | mv2 | You may edit `name`, `description` and `workspace`. |
| `template-files.json` | mv2 | Written by `init`, `subverse` and `refresh --apply`. |
| `state/` | mv2 | Records of agents and their messages. Git ignores it. Don't edit it while agents run. |
| `subverses/` | mv2 | Created by `subverse`, and changed by `move` and `remove`. |
| `trash/` | mv2 | Filled by `remove` and `refresh --apply`, and emptied by `trash --empty`. Git ignores it. |

`mv2 refresh` compares every file except `universe.json`, `template-files.json`, `state/`, `trash/`, `subverses/` and hidden files. Files whose names start with `.` and end in `.lock` are locks, so leave them alone. See [updating](../use/updating.md) for refresh, and [templates](../develop/templates.md) for writing templates.

## universe.json

| Field | Meaning |
| --- | --- |
| `schema` | Always `1`. |
| `id` | UUID generated at install. Never change it: agents and tmux panes are tagged with it. |
| `name` | Display name. `init` uses `--name` or the directory name; `subverse` uses `NAME`. |
| `description` | Shown on the universe's dashboard row. |
| `workspace` | Directory new agents work in, relative to this directory or absolute. Agents keep the workspace they were spawned with. |
| `created_at` | Install time. |
| `template` | Template name or path the universe was installed from. |

## template-files.json

| Field | Meaning |
| --- | --- |
| `schema` | Always `1`. |
| `template` | Template that `refresh` compares with by default. |
| `source` | Path of this universe inside the template; empty for the template's root. |
| `files` | Each definition file's `sha256:` fingerprint when installed or last refreshed. |

## bootstrap.md

This is the short text every agent gets at spawn and resume, alongside the harness's own system prompt. mv2 adds the universe name and ID, the agent name and ID, the universe directory and the agent's state directory. A running agent keeps the text it launched with.

## Plugins

These files record which plugins are on, their settings and their state. See [plugins](../use/plugins.md).

### plugins.json

This file says which plugins are on in this universe. A missing file, or a plugin it doesn't name, means the plugin's default. The default is off, except for bundled plugins marked on, such as typeahead.

Subverses don't inherit the file. `mv2 plugins enable` and `disable` and the dashboard's `P` write it. An unusable file stops nothing: every plugin is at its default, and `mv2 doctor` says why.

```json
{"schema": 1, "enabled": {"compaction": true, "remote": false}, "theme": null, "events": {"retain_days": 7, "max_mb": 32}}
```

| Field | Meaning |
| --- | --- |
| `schema` | Always `1`. |
| `enabled` | Plugin name to `true` (on) or `false` (off). |
| `theme` | The plugin whose dashboard colors to use, or `null`. |
| `events` | How long the event journal keeps closed segments: `retain_days` (default `7`) and `max_mb` (default `32`). |

If a newer mv2 wrote fields that this one doesn't know, they are kept and ignored.

### plugin-settings/NAME.json

This file holds one plugin's settings for this universe, in the shape its schema gives. `"schema"` is the plugin's own settings version. When the file is missing, `mv2 plugins enable` installs the plugin's defaults. `mv2 plugins settings NAME --set KEY=VALUE` edits it.

If the file breaks the plugin's schema or checks, the plugin acts as off here until the file is fixed.

Tree compaction's settings are in `plugin-settings/compaction.json`, which the compaction plugin configures; see its [README](../../plugins/compaction/README.md). An older `compaction.json` at the universe root is converted the next time an `mv2` command runs here. The old file then moves to `trash/`. To convert it right away, run `mv2 plugins migrate --apply`.

### plugins/NAME/

This directory holds a plugin that the universe carries itself, such as a repository's `.multiverse/plugins/NAME/`. It is never imported until you run `mv2 plugins trust NAME`, which trusts this copy even when a bundled or checkout plugin has the same name. Any change to its files needs trust again.

### state/plugins/

mv2 and the plugins write here, so leave it alone.

| Path | Meaning |
| --- | --- |
| `NAME/` | The plugin's own state, such as remote's `token` and `audit.jsonl`. |
| `NAME/errors.log` | The plugin's recorded errors with tracebacks, rotated to `errors.log.1` at 1 MiB. |
| `NAME/plugin.log` | Lines the plugin logs. |
| `NAME/events.cursor` | The last event the plugin's handlers finished in this universe. |
| `NAME/events.cursor.journal` | The journal (`journal-id`) that cursor belongs to, so a journal that started again is delivered from its start. Written after the cursor: an interrupted write delivers again, never skips. Older mv2 ignores it. |
| `NAME/handlers.json` | The last event each of the plugin's handlers finished, so a restarted worker never gives a handler an event twice. |
| `host/` | In a top-level universe: the plugin host's `host.lock`, `status.json` and `host.log`. |
| `events.log` | Events that could not be recorded. |
| `migrate-failed.json`, `not-installed-notified.json` | Settings conversions that failed, and notices already given about a plugin that isn't installed. |

`state/events/` holds the event journal. It contains `.jsonl` segments named by their first event's number, `seq`, which holds the next number, `journal-id`, and its second copy `journal-id.copy` (so a lost or unreadable `journal-id` is not taken for a new journal). A journal that was removed and started again gets a new `journal-id`, so the host knows its events are new. mv2 writes the journal only while a plugin that is on here subscribes to events. It prunes the journal to the `events` limits in `plugins.json`.

## statusline.json

This file sets the items on the Claude status line and on every agent's tmux status bar. Key order is display order. A missing key keeps its default, and an unusable file means mv2 uses the defaults. Changes show on the next refresh.

| Key | Meaning |
| --- | --- |
| `schema` | Always `1`. |
| `items` | Items on the Claude status line: `agent`, `phase`, `summary`, `inbox`, `compaction`. On by default: `phase`, `inbox`, `compaction`. |
| `tmux.refresh_seconds` | How often tmux redraws the bar, 1 to 60 (default `2`). It is read at spawn and resume. |
| `tmux.items` | Items on the tmux bar: `agent`, `harness`, `focus`, `status`, `phase`, `summary`, `inbox`, `context`. On by default: `agent`, `harness`, `focus`, `phase`, `inbox`. |
| `tmux.harnesses.ADAPTER.items` | On/off overrides for one adapter (`codex`, `claude`, `pi`). The built-in overrides turn `status` on for Codex, and `phase` and `inbox` off for Claude. Change those here, not in `tmux.items`. |

A plugin's items, such as the compaction plugin's `compaction`, are keys in `items` and `tmux.items`, like the core items. They show only for agents launched while their plugin was on. The plugin says whether each item is on by default.

## harnesses/NAME/harness.json

Each directory under `harnesses/` is a harness you can pass to `spawn --harness NAME`. See [Harnesses](../use/harnesses.md).

### Schema 2

Schema 2 points at a built-in adapter and adjusts it:

```json
{
  "schema": 2,
  "adapter": "claude",
  "description": "Claude Code; prompt-led coordination and an assigned native session UUID.",
  "settings": {"model": "opus", "env": {"IS_SANDBOX": "1"}}
}
```

| Field | Meaning |
| --- | --- |
| `schema` | `2`. |
| `adapter` | `codex`, `claude` or `pi`. |
| `description` | Shown by `mv2 catalog`. |
| `settings` | An object in which every key is optional. Unknown keys are errors. |

| Setting | Meaning |
| --- | --- |
| `executable` | Command to run instead of `codex`, `claude` or `pi`. |
| `model` | Default model for agents spawned without `--model`. |
| `args` | Extra arguments for spawn and resume, inserted after the executable and before the prompt. |
| `spawn_args`, `resume_args` | Extra arguments for spawn only or resume only, after `args`. |
| `env` | Variables (name to string) set for every spawn and resume. It can't set `MV2_*`, `PATH`, `HOME`, `CODEX_THREAD_ID`, `CLAUDECODE`, or the storage variables mv2 reads (`CODEX_HOME` and `CODEX_SQLITE_HOME` for Codex, `CLAUDE_CONFIG_DIR` for Claude). Pi's can set `PI_CODING_AGENT_DIR`, which mv2 does not use to locate Pi sessions: spawn records that value, and a resume keeps the recorded one and names a changed setting it ignored; `mv2 resume --start-fresh` takes the current one. |
| `unset_env` | Variables removed from the agent's environment. It replaces the default list, `NO_COLOR`. A variable named in `env` is kept. `CODEX_THREAD_ID` and a parent Claude session's markers ([environment](environment.md)) are always removed when inherited. |
| `inbox_bridge` | The live connection (the bridge). For Codex and Claude, unset connects if it can and otherwise launches launch-only, `true` requires the connection, and `false` never connects. For Pi, it is off unless `true`. |
| `system_prompt_snapshot` | Claude only. `"off"`, the default, gives a resumed agent the current bootstrap. `"on"` keeps the system prompt Claude recorded at the conversation's first request, so a bootstrap change reaches only new conversations. A `--system-prompt-snapshot` flag in `args` or `resume_args` overrides it. |

### Schema 1

Schema 1 describes any command-line agent without a built-in adapter:

| Field | Meaning |
| --- | --- |
| `schema` | `1`. |
| `description` | Shown by `mv2 catalog`. |
| `spawn` | Required. The argument array for a new agent, with the executable as its first item. No shell is involved. |
| `resume` | Argument array to continue a saved agent. Without it, resume is unavailable. |
| `model_args` | Arguments added when a model is set, before a `--` in the argv if there is one. |
| `model` | Default model for agents spawned without `--model`. |
| `session` | How the native session is found: `file` (default; `{session_file}`), `assigned` (the agent UUID, given at spawn) or `reported` (the agent runs `mv2 session-bind`). |
| `bootstrap` | Extra text added after `bootstrap.md`. |
| `env`, `unset_env` | As in schema 2. |
| `docs` | Link to the agent's CLI docs. |

Arguments may use these substitutions:

| Value | Meaning |
| --- | --- |
| `{name}`, `{agent_id}`, `{workspace}` | Agent name, UUID and working directory |
| `{agent_dir}`, `{harness_dir}` | The agent's state directory and `harnesses/NAME/` |
| `{task}`, `{task_file}` | Initial prompt, or the file it is saved in |
| `{bootstrap}`, `{bootstrap_file}` | Bootstrap text, or the file it is saved in |
| `{bootstrap_json}` | Bootstrap as a quoted JSON string, also valid as a TOML string |
| `{model}` | The model, or empty |
| `{session_file}`, `{session_id}` | `state/agents/UUID/session.jsonl`, or the recorded native session ID |
| `{adapter_dir}` | mv2's built-in adapter package with the same name as this harness |

## Catalog entries

`skills/`, `commands/` and `workflows/` hold what `mv2 catalog` lists and `mv2 command NAME` prints. The directory name is the entry's name.

| File | Meaning |
| --- | --- |
| `entry.json` | `{"description": "..."}`, the line `mv2 catalog` shows. |
| `skills/NAME/SKILL.md` | The skill an agent reads when it needs it. Front matter is optional, and mv2 doesn't read it. |
| `commands/NAME/prompt.md`, `workflows/NAME/prompt.md` | The prompt `mv2 command NAME` prints. |

## views/default.json

This file sets how the dashboard shows this universe. Every field is optional. See [dashboard reference](dashboard.md#views).

| Field | Meaning |
| --- | --- |
| `title` | Name on the universe's row. It defaults to `universe.json` `name`. |
| `accent` | `#rrggbb`, or `auto` for a color picked from the universe ID. |
| `columns` | Agent columns, in order: `name`, `harness`, `live`, `status`, `focus`, `context`, `cost`, `phase`, `summary`, `id`, `universe`, `task`, `updated_at`. `activity` and `tokens` are aliases of `status` and `context`. `name` always shows. The default is `name`, `harness`, `live`, `status`, `context`, `phase`, `summary`. The universe the dashboard opens at sets the columns for the whole tree. |
| `widgets` | Up to 32 panels shown when you open this universe. |

| Widget `type` | Fields |
| --- | --- |
| `markdown` | `title`, `text` (up to 32 KiB) |
| `table` | `title`, `columns` (up to 16 strings), `rows` (up to 500 arrays, one cell per column) |
| `artifacts` | `title`, `items` (up to 100 of `label`, `path`, `description`); `path` is relative to the universe directory |

## state/agents/UUID/

Each agent has one directory, named by its UUID. mv2 writes everything in it.

| Path | Meaning |
| --- | --- |
| `properties.json` | Identity and reported work: `id`, `universe_id`, `name`, `harness`, `model`, `task`, `phase`, `summary`, `workspace`, `native_session_id`, timestamps. `native_storage_environment` retains proven storage variable values; `null` means explicitly unset on future launches. A missing legacy key remains unknown, not unset. A legacy adoption also keeps its proven `HOME` when a default storage path depends on it. Explicit missing-conversation recovery keeps every old and replacement scope in `native_session_homes` before retiring the old binding. A plain recovery records `native_service_identity: null` (no shared service); this is valid for later recoveries, while malformed non-null service identities refuse. |
| `runtime.json` | The current launch: `launch_id`, `state`, `status`, `host`, `tmux` (socket, session, pane), `native_mode`, `native_versions`, `plugins` (what each plugin added to this launch, or why it was left out, and the launch's plugin `problems`, which `mv2 inspect` shows), and the last `reason` or `error`. |
| `inbox.jsonl` | Every message, one JSON object per line: `id`, `at`, `from`, `text`, `delivery` (`steer`, `follow-up` or `inbox-only`). The file is append-only. |
| `inbox.cursor` | Byte offset up to which `mv2 inbox` has read inbox-only messages. |
| `inbox-state.json` | Delivery attempts and receipts per message. |
| `inbox-unsubmitted/UUID.json` | Negative proof for a newly saved message blocked by damaged delivery state before submission. Removed before a later submission; never inferred for older uncertain messages. |
| `focus.json` | While the agent is [focused](../cli/messaging.md#focus-an-agent): `since`, `until` (a time, or `null`), `turns` (turn ends still to come, or `null`) and `by` (`agent` or `user`). |
| `session.jsonl` | The native conversation, for Pi and other harnesses that use `{session_file}`. |
| `sessions.jsonl` | The agent's session lineage: verified native session changes, such as Claude's `/clear`, and explicit missing-conversation retirements. A retirement keeps the old ID owned and records the replacement storage before properties change; reported IDs remain unbound until the new conversation binds. Written only after a change. See [session lineage](../design/session-lineage.md). |
| `NAME/` | A plugin's state for this agent. The compaction plugin's `compaction/` holds `config.json` (the agent's own scheduler), `trees/`, `status.json` and `build.log`. |
| `status-items/NAME.txt` | The text of a plugin's status-line item for this agent. |
| `claude-receipts/`, `claude-asks/` | Delivery receipts and `mv2 ask` answers, for the Claude bridge only. |
| `launches/UUID/` | One directory per spawn or resume. |

Each launch directory holds:

| File | Meaning |
| --- | --- |
| `bootstrap.md` | The bootstrap text this launch received. |
| `task.md` | The initial or wake prompt. |
| `request.json` | The argv and environment the runner started. |
| `registration.json` | Progress while the launch is being prepared, which keeps the runner waiting. |
| `ready` | Marker that lets the runner start. |
| `output-tail.txt` | The terminal's last 40 lines (8 KiB at most) after an early or nonzero exit. |
| `native-service.log` | Output of the Codex app-server when this launch started it. |
| `cleanup.json`, `roles/` | Whether this launch's own connections and helpers have closed. A resume or a finished `kill` waits for them. |
| `.connections.lock` | Serializes connection records with cleanup inspection. Readers use an existing lock without creating one. An empty regular lock left by an older inspection is not startup evidence. |
| `native-refusal.json` | A native refusal recorded for this launch, such as Codex's "open in another app". |

## subverses/ and trash/

A subverse is `subverses/NAME/`, a complete universe with its own definitions, state and subverses.

`trash/` holds entries named by UTC time and name, such as `20261015T183726-worker-1a2b3c4d`. For an agent, the entry name also includes the start of its UUID. Each entry has a `.removed.json` record with `kind` (`agent`, `subverse` or `refresh`), `name`, `origin` and `removed_at`.

To restore an agent or subverse, move its entry back to its `origin`. `mv2 trash` lists the entries across the tree, and `mv2 trash --empty` deletes them.

## Outside the universe

| Path | Meaning |
| --- | --- |
| `~/.multiverse/templates/NAME/` | Your own templates, found by `init --template NAME` before the bundled ones. |
| `~/.multiverse/plugins/NAME/` | Your own plugins, which every universe can turn on. |
| `~/.multiverse/trusted-plugins.json` | The universe plugins you trusted: each one's path, the SHA-256 of its files and when. Written by `mv2 plugins trust` and `untrust`. |
| `~/.multiverse/plugin-cache/SHA256/` | The copy of a trusted universe plugin that actually runs. A copy nothing trusts and nothing used for 14 days is removed. |
| `~/.multiverse/plugin-fingerprints.json`, `~/.multiverse/pycache/`, `~/.multiverse/installed-plugins.json` | Caches: universe plugins' hashes by file sizes and times, Python bytecode for plugins imported from a universe's own directory, and the pip-installed plugins of each Python environment. They are safe to delete. |
| `~/.multiverse/plugin-load-timeouts.json` | Plugins whose load ran past 10 s, by their files. Later processes wait for such a plugin at most 0.5 s, and a launch leaves it out while it still loads. The entry goes when the plugin loads, its files change, or 10 minutes pass. It is safe to delete. |
| tmux server `multiverse-v2` | Agent terminals (`tmux -L multiverse-v2`), unless `MV2_TMUX_SOCKET` names another socket. See [Environment](environment.md). |
| `/tmp/mv2-native-LAUNCH/` | Private socket directory for a bridge launch. |
| Codex, Claude and Pi storage | Native conversations stay where each harness keeps them (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`); mv2 never moves or deletes them. |
