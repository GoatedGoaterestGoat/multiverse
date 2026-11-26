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
      startup-task-*.md        # private retry copies of tasks refused before launch
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
| `state/startup-task-*.md` | mv2 | Private copies (mode `0600`) of tasks preserved when preparation refuses a spawn. The refusal names the file to pass to `mv2 spawn --task-file`. You can remove the copy once the task starts or you decide to discard it. |
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

mv2 briefly retries missing reads of its replaced plugin records, including settings, host status, event cursors, progress and caches. This avoids treating a network filesystem's replacement gap as defaults or a first subscription. After the retry budget, each record keeps its normal missing-file behavior. Other errors, malformed content, plugin source files and append-only journals are not retried.

### plugins.json

This file says which plugins are on in this universe. A missing file, or a plugin it doesn't name, means the plugin's default. The default is off, except for bundled plugins marked on, such as typeahead.

Subverses don't inherit the file. `mv2 plugins enable`, `disable` and `grant` and the dashboard's `P` write it. An unusable file turns every plugin off in that universe until it's repaired, so a torn file never turns on a plugin you turned off or gives back access you took away; `mv2 doctor` says why. A missing file means the defaults.

```json
{"schema": 1, "enabled": {"compaction": true, "remote": false}, "access": {"compaction": "spend"},
 "access_ceiling": null, "theme": null, "events": {"retain_days": 7, "max_mb": 32}}
```

| Field | Meaning |
| --- | --- |
| `schema` | Always `1`. |
| `enabled` | Plugin name to `true` (on) or `false` (off). |
| `access` | Plugin name to the access level granted here: `observe`, `spend` or `act`. A plugin that is on with none recorded has what it asks for. |
| `access_ceiling` | No plugin here has more than this level: `observe` makes the universe read-only for plugins. `null` (the default) sets no ceiling. |
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
| `host/` | In a top-level universe: `host.lock`, `status.json` and `host.log` for the plugin host, and `turn-wake`, the pipe that wakes its turn projection lane. |
| `registry.json` | Each plugin that's on here: the event types it subscribes to, the source that found it, and, for one found only in your own environment (`MV2_PLUGIN_PATH`, a package found only through your `PYTHONPATH`), its folder. Every process here records the plugin's events from it, and finds it there, at the content pinned for it when you turned it on (`mv2 plugins trust NAME` approves an edit). One only an agent's own environment finds is marked `private`: its event types are recorded for it, never its folder. Written when you turn a plugin on or off and when you list the plugins. |
| `events.log` | Events that could not be recorded. |
| `migrate-failed.json`, `not-installed-notified.json` | Settings conversions that failed, and notices already given about a plugin that isn't installed. |

`state/events/` holds the event journal. It contains `.jsonl` segments named by their first event's number, `seq`, which holds the next number, `journal-id`, and its second copy `journal-id.copy` (so a lost or unreadable `journal-id` is not taken for a new journal). A journal that was removed and started again gets a new `journal-id`, so the host knows its events are new. `spill/` holds events a process recorded while the journal was busy, one file per process, until the next writer merges them in (a file being merged is renamed `.merging-NAME-ID.jsonl`, with its progress beside it in `...offset`, including when its next row was recorded, so the next merge goes on from there; links and files that aren't regular are never written or followed). mv2 writes the journal only while a plugin that is on here subscribes to events. It prunes the journal to the `events` limits in `plugins.json`.

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
| `adapter` | `codex`, `claude`, `pi` or `agy`. |
| `description` | Shown by `mv2 catalog`. |
| `settings` | An object in which every key is optional. Unknown keys are errors. |

| Setting | Meaning |
| --- | --- |
| `executable` | Command to run instead of `codex`, `claude`, `pi` or `agy`. |
| `model` | Default model for agents spawned without `--model`. |
| `model_names` | Optional map from exact recorded model spellings to nonempty, single-line display names. It changes labels only; see [display names](../use/harnesses.md#model-display-names). |
| `models` | Optional nonempty object from a catalog model ID or alias to `{}` or `{"efforts": ["low", "high"]}`. An effort list is nonempty, distinct and limited to offered levels. Unknown entries refuse launches and runs; see [model choices](../use/harnesses.md#narrow-the-available-choices). |
| `args` | Extra arguments for spawn and resume, inserted after the executable and before the prompt. |
| `spawn_args`, `resume_args` | Extra arguments for spawn only or resume only, after `args`. |
| `env` | Variables (name to string) set for every spawn and resume. It can't set `MV2_*`, `PATH`, `HOME`, `CODEX_THREAD_ID`, `CLAUDECODE`, or the storage variables mv2 reads (`CODEX_HOME` and `CODEX_SQLITE_HOME` for Codex, `CLAUDE_CONFIG_DIR` for Claude). Pi's can set `PI_CODING_AGENT_DIR`, which mv2 does not use to locate Pi sessions: spawn records that value, and a resume keeps the recorded one and names a changed setting or shell value it ignored; `mv2 resume --start-fresh` takes the current one. |
| `unset_env` | Variables removed from the agent's environment. It replaces the default list, `NO_COLOR`. A variable named in `env` is kept. `CODEX_THREAD_ID`, parent Claude session markers and parent Antigravity CLI session markers ([environment](environment.md)) are always removed when inherited. |
| `inbox_bridge` | The live connection (the bridge). For Codex, Claude and Antigravity CLI, unset connects if it can and otherwise launches launch-only, `true` requires the connection, and `false` never connects. For Pi, it is off unless `true`. |
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
| `{agent_dir}`, `{harness_dir}`, `{launch_dir}` | The agent's state directory, `harnesses/NAME/` and this launch's directory under `launches/UUID/` |
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
| `properties.json` | Identity and reported work: `id`, `universe_id`, `name`, `harness`, `model`, `task`, `phase`, `summary`, `workspace`, `native_session_id`, timestamps. `reported_at` is the UTC time of the latest accepted report, including a summary-only report; other updates change `updated_at` without changing it. `model_before_edit` keeps the current launch's model/effort fallback when `set-model` changes the next-resume choice; it applies only to its matching `launch_id`. `native_storage_environment` retains proven storage variable values; `null` means explicitly unset on future launches. A missing legacy key remains unknown, not unset. A legacy adoption also keeps its proven `HOME` when a default storage path depends on it. Explicit missing-conversation recovery keeps every old and replacement scope in `native_session_homes` before retiring the old binding. A plain recovery records `native_service_identity: null` (no shared service); this is valid for later recoveries, while malformed non-null service identities refuse. |
| `runtime.json` | The current launch: `launch_id`, `state`, `status` (and `in_turn` while a turn that started working hasn't ended, so a wait outside any turn doesn't count as a turn end), `host`, `tmux` (socket, session, pane, and `server` with its socket, PID, process start token and tmux start epoch), `native_mode`, `native_versions`, `plugins` (what each plugin added to this launch, or why it was left out, and the launch's plugin `problems`, which `mv2 inspect` shows), and the last `reason` or `error`. |
| `inbox.jsonl` | Every message, one JSON object per line: `id`, `at`, `from`, `text`, `delivery` (`steer`, `follow-up` or `inbox-only`). A message a focus held is `inbox-only` and adds `intended` (`steer` or `follow-up`) and `focus` (the focus's `id`). mv2's note about a focus's held messages is a `follow-up` with `focus_note` (that focus's `id`). The file is append-only. |
| `inbox.cursor` | Byte offset up to which `mv2 inbox` has read inbox-only messages. |
| `inbox-read.json` | Messages past `inbox.cursor` that `mv2 held` has read, by message ID. |
| `inbox-state.json` | Delivery attempts and receipts per message. `failed_inbox_copy` marks a newly recorded failure or cancellation for inbox reading if its conversation is still eligible. For an adapter using attempt IDs, each submission stores its `native_message_id` and checked `delivery_receipt`; the logical inbox message ID stays unchanged. |
| `inbox-unsubmitted/UUID.json` | Negative proof for a newly saved message blocked by damaged delivery state before submission. New records carry `failed_inbox_copy`. A later submission removes the proof. |
| `focus.json` | While the agent is [focused](../cli/messaging.md#focus-an-agent): `id`, `since`, `until` (a time, or `null`), `turns` (turn ends still to come, or `null`) and `by` (`agent` or `user`). |
| `focus-last.json` | The latest focus, kept after it ends: `id`, `since`, `held` (messages it held), `report` (`listener` once a listener's line printed, `note` once the turn-end note was saved, or `null`) and `note` (the note's message ID). |
| `slash/UUID.json` | A [harness command](../cli/harness-commands.md): its request, original caller and launch, and correlated outcome. Model and effort readbacks update `properties.json` before a successful outcome is returned. Events contain the command name, state and caller, never arguments or output. |
| `slash/pending.json` | A bounded index of unfinished command IDs. Listing and resume settle available receipts without scanning command history; the index is removed when empty. |
| `session.jsonl` | The native conversation, for Pi and other harnesses that use `{session_file}`. |
| `sessions.jsonl` | The agent's session lineage: verified native session changes, such as Claude's `/clear`, and explicit missing-conversation retirements. A retirement keeps the old ID owned and records the replacement storage before properties change; reported IDs remain unbound until the new conversation binds. Written only after a change. See [session lineage](../design/session-lineage.md). |
| `NAME/` | A plugin's state for this agent. The compaction plugin's `compaction/` holds `config.json` (the agent's own scheduler), `trees/`, `status.json` and `build.log`. |
| `status-items/NAME.txt` | The text of a plugin's status-line item for this agent. |
| `claude-usage.json` | Claude only: the agent's usage indexes, so a new process parses only what Claude wrote since (per transcript its inode, the offset read to, a SHA-256 of those bytes, the latest time and turn, each response's counted usage; no transcript text). Written by any process that reads usage; taken over only when the digest matches. Safe to delete. |
| `claude-receipts/`, `claude-asks/` | Delivery receipts and `mv2 ask` answers, for the Claude bridge only. `<attempt-id>.json` records delivery; `<attempt-id>.not-delivered.json` records a reported failure. A valid delivery takes precedence if both files exist. |
| `agy-receipts/` | Delivery receipts for the Antigravity CLI bridge only: metadata and a SHA-256 hash of the framed message, never raw message text. |
| `launches/UUID/` | One directory per spawn or resume. |
| `launches/UUID/.mv2-listen-proof` | Private file (mode `0600`) exchanging a fresh launch check between `mv2 listen` and an opted-in runner. The runner removes it on orderly exit. The next launch of that agent removes a leftover file if the previous launch is proven stopped and the file still has its recorded identity. Checks against a leftover file go unanswered. |

`runtime.json` temporarily keeps a reported `working` or `idle` as
`deferred_stop_status` when a stopping launch still has a waiting status.
A newer `waiting` or `unknown` report clears the deferral. A refused
cancellation applies the deferred report to the continuing launch.

`last_reported_status` and `status_changed_at` track the last known harness
status and when it changed. The time comes from the harness callback,
including a report whose effect on the displayed status is deferred
during a stop. Other runtime updates leave the time alone.

`journal_status` briefly holds the latest transition that still needs to enter
the agent's ordered event queue. When the queue cannot accept it before another
event arrives, `journal-status-N.json` holds the older event and
`journal_status_spill` records the order in which to replay it. A later report
or the plugin host can recover these saved events. When a status follows
relay metadata, its reservation waits for `journal-fence-ID.json` to confirm
the earlier batch. That file can hold the batch itself if the ordinary outbox
write failed. The host admits both in one ordered write. A private
`journal-relay-ID.json` preserves known predecessors if the launch ends
before the companion reaches the queue. A recorded conversation switch can
also be replayed from its lineage row before events for the new conversation.
If it cannot be published yet, its position is retained ahead of later
reports in the same runtime event queue. An unreadable lineage can delay
those journal entries while the current runtime status and exit still record.
When both the conversation pointer and its lineage were unavailable, a switch
waits for the relay to secure the earlier batch under its original binding.
Status subscriptions are checked when the runtime status commits, so a later
subscription cannot publish an older status outside the ordered queue.
Native prompt and turn edges received during an outage also keep the launch
and conversation they were reported in.

For a bridged conversation switch, `runtime.json` also holds a `navigation`
record with its request correlation and admission token. `checking` and
`admitted` block new submission until a definite result; an unknown result
stays fenced. `session_generation` distinguishes a switch away and back within
one launch. `queue_history` retains each earlier conversation's pause state,
while `queue_state` names the current one. Resume preserves these pause records.
When `queue_state` carries `paused_message_ids`, list and inspect count only
accepted, unconfirmed delivery records whose mv2 submission IDs are in that
list. An empty list records that no submissions are held.
Input saved during the fence has `navigation_wait` in `inbox-state.json`; a
resolved switch can submit it once. Before an explicit resume replaces a stopped
launch, pending input still carrying that deferral becomes a reported failure
for `deliver --retry`, keeping its message ID and target. This works even if the
old runtime marker is already gone; resume never infers new delivery authority
for accepted, submitting or uncertain attempts.

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
| Adapter launch files | Private (`0600`) files declared by an adapter, such as Antigravity CLI's `AGENTS.md`, `settings.json`, `.agents/hooks.json`, and ephemeral handoff files (`agy-ls.json`) removed when the runner exits normally (a force-killed runner leaves them until cleanup). |

## subverses/ and trash/

A subverse is `subverses/NAME/`, a complete universe with its own definitions, state and subverses.

`trash/` holds entries named by UTC time and name, such as `20261015T183726-worker-1a2b3c4d`. For an agent, the entry name also includes the start of its UUID. Each entry has a `.removed.json` record with `kind` (`agent`, `subverse` or `refresh`), `name`, `origin` and `removed_at`.

To restore an agent or subverse, move its entry back to its `origin`. `mv2 trash` lists the entries across the tree, and `mv2 trash --empty` deletes them.

## Outside the universe

| Path | Meaning |
| --- | --- |
| `~/.multiverse/templates/NAME/` | Your own templates, found by `init --template NAME` before the bundled ones. |
| `~/.multiverse/plugins/NAME/` | Your own plugins, which every universe can turn on. |
| `~/.multiverse/trusted-plugins.json` | The universe plugins you trusted, the plugins `mv2 plugins install` placed, and the folders a universe records: each one's path, the SHA-256 of its files, when, and the event types those files follow, which the journal reads instead of hashing anything. Written by `mv2 plugins trust`, `untrust`, `install`, `update`, `remove`, and by turning on a plugin a universe records. Removing it starts over with nothing trusted; one that can't be read stops the plugins checked against it and is never written over. |
| `~/.multiverse/plugin-installs.json` | What `mv2 plugins install` placed in `~/.multiverse/plugins/`: each plugin's source, the ref asked for, the commit, the folder inside the repository, the SHA-256 of its files and what it could do then. `update` and `remove` act only on what it names. A universe's own installs (`--here`) are in its `state/plugin-installs.json`. |
| `~/.multiverse/plugin-staging/` | Plugins fetched by `mv2 plugins install` or `update`, waiting for your yes. Anything left there for a day is removed. A universe's own installs stage in its `state/plugin-staging/`. |
| `~/.multiverse/plugin-cache/SHA256/` | The copy of a trusted universe plugin that actually runs, with `.mv2-files`, the files it was made with. A copy whose listed files were edited doesn't run. A copy nothing trusts and nothing used for 14 days is removed. |
| `~/.multiverse/plugin-secrets/` | Plugins' secrets where there's no system keychain: `secrets.json` (0600, in a 0700 folder) holds their values by universe id, plugin and key, and `index.json` says which are set and in which store, never a value. With a keychain, the values are there instead, under the service `mv2 plugin secret`. |
| `~/.multiverse/plugin-packages/KEY/` | A plugin's Python packages, installed with your yes (`site/`, put on its import path after mv2's own) and `.mv2-packages.json`, what was installed. KEY comes from the plugin's name, its requirements file and the Python. A folder nothing used for 14 days is removed. |
| `~/.multiverse/plugin-fingerprints.json`, `~/.multiverse/pycache/`, `~/.multiverse/installed-plugins.json` | Caches: universe plugins' hashes by file sizes and times, Python bytecode for plugins imported from a universe's own directory, and the pip-installed plugins of each Python environment. They are safe to delete. |
| `~/.multiverse/plugin-load-timeouts.json` | Plugins whose load ran past 10 s, by their files. Later processes wait for such a plugin at most 0.5 s, and a launch leaves it out while it still loads. The entry goes when the plugin loads, its files change, or 10 minutes pass. It is safe to delete. |
| `~/.multiverse/tmux-servers/` | Server identity records and per-socket recovery locks, keyed by the canonical socket path. The records retain the socket, PID, process start token and tmux start epoch; they contain no prompts. A locked caller removes its touched record and lock once that server is proven gone, or a lone lock when no record was written. Waiters recheck the lock's identity before entering. Live, uncertain and damaged records remain; there is no directory-wide sweep. |
| `~/.multiverse/run/` | Private runtime directory (mode `0700`), overridden by `MV2_RUN_DIR`. `tmux.sock` serves new agent terminals unless `MV2_TMUX_SOCKET` selects another address. `native/LAUNCH/rpc.sock` is a launch's private bridge socket; its launch folder is removed on exit. See [Environment](environment.md). |
| Legacy tmux server `multiverse-v2`, `/tmp/mv2-native-LAUNCH/` | Older launches keep these saved addresses until stopped and resumed. Discovery still checks the old server. |
| Codex, Claude, Antigravity CLI and Pi storage | Native conversations stay where each harness keeps them (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `~/.gemini/antigravity-cli`); mv2 never moves or deletes them. |
