# Plugins (design)

Status: **proposal**, revised after the user's first decisions (see
[Decisions](#decisions)). Nothing here is implemented yet. It builds on PR #16's layout and registries
([architecture](architecture.md)).

A plugin is a directory of Python and, optionally, harness code that adds to mv2
without editing it:

- **What people see:** the Claude status line and the tmux bar, the dashboard
  (columns, local view panels, inspector lines, theme, keys, screens, settings
  screens), `mv2` commands, catalog entries (skills, commands, workflows) and
  `mv2 doctor` checks.
- **What agents do:** a plugin uses mv2's operations (spawn, resume, kill/stop,
  send with steer, follow-up or inbox-only, ask, inbox, list/inspect, report,
  refresh) through a stable API. It reacts to what happens: an agent launched,
  became ready, changed status or exited; a message was saved, delivered or read;
  an agent reported.
- **What a harness does:** a plugin can add to each launch of a harness, such
  as a Claude mod of its own, a `--settings` hook, environment, a Pi extension, or
  Codex config, instructions and MCP servers. It is gated on the harness's version.

The design has two goals that test it:

1. **Tree compaction stops being built in.** Today it is spread over core,
   the Claude adapter, the mod, two tools, the dashboard and the templates. When
   it is a plugin in its own repository with every behaviour kept, the API is
   good enough.
2. **Sample plugins** that show developers the API: a status-line item, a
   notifier, and a remote control that is off by default, local-only and
   authenticated.

## Terms

| Term | Meaning |
| --- | --- |
| **plugin** | An mv2 plugin: a directory with `plugin.json`. |
| **extension point** | A named registry a plugin adds to, such as `dashboard.column` or `launch`. PR #16's `COLUMNS`, `PANELS`, `AGENT_FACTS`, theme and `FieldScreen` become extension points. |
| **harness extension** | The part of a plugin that runs inside or alongside a harness: a Claude plugin directory (a "mod"), a Pi extension, Codex config, instructions and MCP servers. |
| **plugin host** | An optional long-lived mv2 process that runs plugins' services and event handlers. |
| **API** | `multiverse.plugins.api`, the only mv2 module a plugin imports. It is versioned. |

"Claude plugin" always means Claude Code's own mechanism (`--plugin-dir`),
which mv2's mod and typeahead already use. mv2 plugins can ship Claude plugins.

## A plugin on disk

```text
compaction/
  plugin.json                 # the manifest
  plugin.py                   # register(mv), or a package named by "module"
  compaction/                 # the plugin's own Python (any layout)
  claude/                     # a Claude plugin directory: .claude-plugin/, hooks/, tests/
  skills/ commands/ workflows/   # catalog entries, laid out as in a universe
  settings.schema.json        # what its settings file may hold
  defaults/settings.json      # the settings `mv2 plugins enable` installs when there are none
  tests/                      # the plugin's own tests
```

### The manifest

```json
{
  "schema": 1,
  "name": "compaction",
  "version": "1.0.0",
  "description": "Tree compaction: per-turn summary trees in place of a harness's own summary",
  "api": "1.6",
  "module": "plugin",
  "default": "off",
  "extends": ["cli", "launch", "capabilities", "usage", "doctor", "statusline",
              "dashboard.settings"],
  "permissions": ["agents.read", "harness.claude", "process.spawn", "models.claude"],
  "commands": {"compaction": "Show or set an agent's compaction: scheduler, summary trees, recent errors"},
  "statusline": {"compaction": {"claude": true, "tmux": false}},
  "harnesses": {
    "claude": [{"dir": "claude", "min_version": "2.1.290", "needs_bridge": true}]
  },
  "settings": {"version": 2, "schema": "settings.schema.json", "defaults": "defaults/settings.json",
               "legacy": "compaction.json"}
}
```

| Field | Meaning |
| --- | --- |
| `name` | Lowercase letters, digits and single hyphens, the same rule as a bridge id. It is unique across all loaded plugins and names the plugin's state directory, events and command. |
| `version` | The plugin's own version, shown by `mv2 plugins` and recorded at each launch it contributed to. |
| `api` | The API version it was written for: `"1.0"` means major 1 with at least minor 0. mv2 loads it only when its own API has that major and at least that minor. Otherwise `mv2 plugins` and `doctor` say why. |
| `module` | The Python module (a file or package in the plugin directory) whose `register(mv)` runs. A plugin with no Python, only catalog entries or a Claude plugin, leaves it out. |
| `default` | `on` or `off` where `plugins.json` says nothing. Only bundled plugins may be `on`. |
| `extends` | The extension points it uses. A registration outside this list is refused, so a plugin's footprint is readable without running it. |
| `permissions` | What it asks to do (see [Trust and safety](#trust-and-safety)). Shown on enable. The API refuses calls that need a permission the manifest does not ask for, and harness entries need theirs; `network.listen` and `models.<harness>` are declared only. None of this sandboxes a plugin's own code. |
| `commands`, `statusline` | Declared up front, so `mv2 --help`, `statusline.json` and the typeahead know them without importing plugin code. |
| `harnesses` | Harness extensions, each with the oldest (and optionally newest) harness version it runs on. |
| `settings` | Its settings file (see [Settings](#settings)): the version of its shape, a JSON Schema, the defaults `mv2 plugins enable` installs when a universe has none, and optionally a file it used before it was a plugin, which `mv2 plugins migrate` moves. |

## Discovery and enabling

### Where plugins come from

| Source | Path | Trusted |
| --- | --- | --- |
| Bundled | `src/multiverse/plugins/builtin/NAME/` (typeahead, samples' tests) | Yes: it ships with mv2 and updates with `mv2 update`. |
| Checkout | `plugins/NAME/` at the root of the mv2 checkout: an optional Git submodule, such as compaction and remote (see [Plugins in their own repositories](#plugins-in-their-own-repositories)) | Yes: it is pinned by the mv2 repository and initialized by the user. |
| Personal | `~/.multiverse/plugins/NAME/`, like `~/.multiverse/templates/` | Yes: the user put it there. |
| Installed | Python entry points in the `multiverse.plugins` group (`pip install mv2-something`) | Yes: the user installed it. |
| Universe | `<universe>/plugins/NAME/`, like `skills/` | Only once trusted (below). A template may ship one, and a repository can carry one. |

If two sources have the same name, the universe's copy comes first, then the
personal one, then the installed one, then the checkout's, then the bundled one. `mv2 plugins` shows
which copy is in use and which ones it shadows. A universe plugin that shadows a
bundled one, such as a local fork of compaction, needs trust like any universe
plugin.

### Enabling: `plugins.json`

Each universe and subverse may have a `plugins.json` at its root:

```json
{
  "schema": 1,
  "enabled": {"compaction": true, "remote": false, "notifier": true},
  "theme": null
}
```

- `plugins.json` is the **one** switch for whether a plugin is on in this
  universe. A plugin's settings file never has a top-level `enabled` of its
  own. It may still turn parts of itself off (compaction can be off for one
  harness), but turning the plugin on or off is always here.
- A plugin is on where `enabled` says `true`. Where the file says nothing, its
  manifest's `default` applies. Only bundled plugins can default to `on` (today
  only typeahead), so installing a plugin never turns it on anywhere. A
  pre-plugin settings file (a manifest's `settings.legacy`, such as
  `compaction.json`) turns nothing on and is never read as settings. It is
  converted on first use (below), which writes the switch into `plugins.json`
  where that file had it on.
- A `plugins.json` naming a plugin that is not installed is not an error for
  launches: they go on without it, and `mv2 plugins` and `doctor` say it is
  missing and how to get it.
- There is no live inheritance, the same rule as settings and views. A
  subverse made from a template gets the template's `plugins.json`. Otherwise
  it has its own file or the defaults.
- `theme` picks one theme plugin. A `keys` field, from before plugins lost
  their dashboard keys, is still read and ignored.
- Fields this mv2 does not know (a newer mv2's additions) are kept in the file
  and left alone, and `doctor` notes them. They never make the file unusable,
  so an older mv2 meeting a newer `plugins.json` keeps every plugin's switch.

```sh
mv2 plugins                       # every plugin found: source, version, on/off here, problems
mv2 plugins info compaction       # manifest, permissions, what it registered, last errors
mv2 plugins enable notifier       # writes plugins.json; installs default settings if none; shows permissions
mv2 plugins settings compaction   # where its settings file is, whether it is usable, and its values
mv2 plugins migrate [--apply]     # pre-plugin or older settings to the current version (dry run by default)
mv2 plugins install compaction    # initialize a checkout plugin's submodule (git submodule update --init)
mv2 plugins new my-plugin         # a starter plugin: plugin.json, plugin.py and a test
mv2 plugins events [--prune]      # the event journal: size, oldest event, cursors
mv2 plugins disable notifier
mv2 plugins enable remote -u .multiverse/subverses/ops
mv2 plugins trust my-plugin       # trust this universe's own copy at its current content (below)
mv2 plugins check ./my-plugin     # validate a plugin directory without enabling it
mv2 plugins host start|stop|status
```

Output is short readable text, with `--json` for scripts, as for the other
commands agents run.

### Which universe decides

Extension points act at three scopes. Each scope says whose `plugins.json`
decides:

| Scope | Extension points | Decided by |
| --- | --- | --- |
| **Agent** | launch contributions, harness extensions, capabilities, usage, status-line items, catalog entries, doctor checks | The agent's own universe or subverse. A Claude agent in a subverse that disables compaction gets no compaction, even if the root enables it. |
| **Dashboard** | theme, settings screens, palette actions and screens, columns, panels, inspector lines | The dashboard loads every plugin that is on anywhere in the tree it shows. A settings screen, palette action or screen acting on a scope works only where that scope enables the plugin, and otherwise says so. A column a view names shows `—` for agents whose universe has the plugin off. The theme and the palette entries come from the top scope the dashboard opened. |
| **Host** | services, event handlers | The top-level universe the host runs for. A handler receives events from every scope where its plugin is on. |

### Settings

Every plugin's files have a fixed place, so a person or an agent always knows
where to look, and core can check, edit and refresh them the same way:

```text
<universe>/
  plugins.json                      which plugins are on here; theme, keys, event retention
  plugin-settings/NAME.json         each plugin's settings for this universe (definition file)
  plugins/NAME/                     this universe's own plugin code, if any (trusted per content)
  state/plugins/NAME/               the plugin's universe-wide state: logs, cursors, tokens (ignored by Git)
  state/agents/<id>/NAME/           the plugin's state for one agent, including per-agent settings
```

- **One file per plugin per universe,** `plugin-settings/NAME.json`, never
  inside `plugins.json`. It is a definition file: templates ship it,
  `mv2 refresh` manages it, and a diff touches one plugin. A subverse has its
  own, with no live inheritance.
- **Versioned.** The file starts with `"schema": N`, the plugin's own settings
  version from its manifest. A plugin that changes the shape registers
  `mv.settings.migrate(fn)`: `fn(data, source)` gets the older settings and
  either their version number or `"legacy"` (the pre-plugin file the manifest
  names), and returns the new settings. For `"legacy"` it may return
  `(settings, on)`, which also turns the plugin on or off in that universe's
  `plugins.json`, whatever it said before (the old file is what the person
  chose; a template may have named the plugin already). `mv2 plugins migrate` shows what
  would change, and `--apply` does it, checking the result first and keeping
  the old file in the universe's trash. Core never guesses a migration.
- **Converted on first use.** A pre-plugin file is converted the first time mv2
  starts work in its universe: any `mv2` command for that universe or a parent
  (`init`, `update` and `plugins` excepted) and any plugin helper action.
  - It happens before the command or helper action runs (after the command
    line is parsed, so a plugin command's own option setup may already have
    read its settings). It runs under the universe's
    `state/plugins/.migrate.lock` and is planned again under the lock, so that
    of two processes one converts.
  - It either happens or leaves every file as it was:
    - the plugin's migration runs with that universe as the API's;
    - its result and `plugins.json` are checked before anything changes;
    - the old file is removed last;
    - a step that fails undoes the ones before it.

    When a file was changed meanwhile and cannot be put back, the line says so
    rather than "unchanged".
  - One line on stderr says what changed, so `--json` output is untouched.
  - When nothing is pending, it costs a manifest check and imports no plugin
    code.
  - A conversion that fails leaves the file as it was. It is said once, `mv2
    doctor` reports it, and it is tried again when the file changes. It never
    changes the command's own result.
  - `mv2 update` converts its caller's universe with the code it updated to, in
    a process of its own. It reads that process's JSON and says what was
    converted, partly converted or not converted; that never fails the update.
  - Runners, the status line and the plugin host never convert.
  - **Not installed:** a known optional plugin's old file (core's table has
    `compaction` → `compaction.json`) may wait while the plugin isn't
    installed, for example a checkout without its submodule or without access
    to it. Then `doctor`, Claude launches, `mv2 plugins migrate`, `mv2 update`
    and the first-use step (once per version of the file) say "compaction.json
    is here but the compaction plugin isn't installed: run mv2 plugins install
    compaction, or move compaction.json away to stop using its settings".
    Nothing reads the file meanwhile.
  - **Never in front:** while an old file waits, whatever the plugin's state,
    neither a settings edit nor `mv2 refresh` puts new settings or the
    template's switch in front of it.
- **Checked.** The manifest names a JSON Schema. Core validates against it, and
  the plugin may add its own checks (`check(settings)`, like compaction's
  harness builder checks). An unusable file never stops a launch: the plugin
  runs as if off for that universe, and `mv2 plugins`, `doctor` and the
  inspector say why, as `compaction.json` does today.
- **Edited safely.** `mv.settings(universe)` returns the parsed, defaulted
  settings and a `problem`. `mv.settings.edit(universe, changes)` writes the file
  as it was with only the changed keys, the rule `ui/settings.py` already
  follows, after the schema and the plugin's checks accept the whole result.
- **Shown.** A dashboard Plugins screen (`P`) lists plugins with an on/off
  toggle per scope. Configure opens a plugin's settings: its own screen if it
  registered one (compaction's), offered whether or not the dashboard has
  loaded the plugin yet and also while its settings cannot be used (that is
  where they are fixed), or otherwise a `FieldScreen` generated from its
  schema, for plain
  strings, numbers, booleans and enums.
- **Per-agent settings** (compaction's agent scheduler) are agent state, in
  `state/agents/<id>/NAME/`, set through the plugin's own command or screen.
- **Reserved names.** A plugin may not be named after something an agent
  directory already holds (`launches`, `claude-asks`, `claude-receipts`,
  `status-items`, `events`, …), so `state/agents/<id>/NAME/` never collides.
  Compaction's existing `state/agents/<id>/compaction/` already fits this rule,
  so it needs no move.

### Templates and `mv2 refresh`

- `plugins.json`, `plugin-settings/` and a universe's `plugins/` directory are
  definition files. Templates ship them, and `mv2 refresh` manages them like
  `statusline.json`: local edits are kept, conflicts need `--take`, and
  replaced files go to trash.
- Refresh never updates bundled, personal or installed plugins. They follow mv2,
  the user's directory or pip. Refresh reports a `plugins.json` naming a plugin
  that is not found, or one whose API mv2 cannot load.
- A refreshed universe plugin changes its fingerprint, so it needs
  `mv2 plugins trust` again before it loads (see below).

## Where plugin code runs

mv2 has no daemon. Work happens in several processes, and a plugin is loaded in
each process that needs it:

| Process | Lifetime | Loads plugins for |
| --- | --- | --- |
| `mv2 COMMAND` | One command | The command itself when it is a plugin's. Doctor checks. Launch contributions, since spawn, resume and wake run here. Catalog entries. |
| Dashboard | While open | UI extension points. Launch contributions for launches it starts. Doctor. |
| Plugin host | Long-lived, only while a plugin needs it | Event handlers and services, one worker process per plugin (see [The plugin host](#the-plugin-host-where-long-running-plugin-code-lives)). |
| Plugin helper | One call | `python -m multiverse.plugins run NAME ACTION`, which harness extensions call: compaction's Claude hooks run `run compaction install`, `status` and `background` this way. |
| Agent runner | One launch | Nothing in v1. A plugin cannot crash or slow a runner. Runner-side observers can come later if a real use needs them. |
| Harness (Claude, Pi) | One launch | The plugin's harness extension, in the harness's own language and API. |
| tmux status script | Every 2 s | Nothing. It reads status-item files (below), so it never imports plugin code. |

`register(mv)` runs in every Python process that loads the plugin.
Registration is declarative and cheap: Textual screens and heavy modules are
named by import path (`"compaction.ui:CompactionScreen"`) and imported only in
the process that uses them. So `mv2 list` never imports Textual, and the CLI
starts as fast as now. `mv.process` says which process it is (`cli`,
`dashboard`, `host`, `helper`) for the rare plugin that cares.

`mv2 COMMAND` reads `plugins.json` and the manifests, which are small JSON
files. It imports a plugin's module only when the command needs it: a plugin
command is found from the global options and the action before argparse is
built (`-u` selects the universe, and everything after the action belongs to
the command), and `mv2 --help` lists plugin commands from their manifests.
A built-in command (`send`, `report`, `list`, …) costs nothing measurable for
plugins: it looks up no plugin commands, and the checks it does make (an old
settings file to convert, an event someone subscribes to, a usage adjuster)
read manifests only, never the trust check that hashes a universe plugin's
files. The list of pip-installed plugins is remembered per Python environment
in `~/.multiverse/installed-plugins.json`, scanned again when a package is
installed or removed (the package directories on `sys.path` change). It keeps
the last 4 `sys.path` signatures per Python, the least recently used dropped,
so processes started differently (a runner, the CLI) do not make each other
scan again. The
one exception is `mv2 NAME --help` for a plugin's command: its options come from
its `configure`, so that help imports the plugin. A plugin command that fails
in a way it did not mean to (anything but `ValueError`, `OSError`, `KeyError`
or `RuntimeError`, the errors commands give on purpose) prints `mv2: plugin
NAME: …` and exits 1, with the traceback in its `errors.log`.

### Seeing lifecycle events from any process

Lifecycle events happen in different processes. A spawn happens in the CLI or
the dashboard, a status change in the mod's `runtime_update` subprocess, an exit
in the runner, a delivery in whichever process sent. So events are **recorded
where they happen and delivered where plugins run**:

- Core appends each event to the universe's journal under `state/events/`. It
  has a sequence number from a lock-protected counter. Appending is best
  effort: a failed append is logged and never fails the operation that caused
  it. A universe writes a journal only while a plugin that is on there
  subscribes to that type of event, so a universe that uses none writes nothing.
  Subscriptions are read from manifests, so a plugin that is on but cannot run
  yet (not trusted, say) has its events recorded for when it can; the host
  checks trust before it delivers anything.
- What recording promises about locks: it takes only the journal's lock, waits
  at most 0.2 s for it (then drops the event and logs it), and imports and runs
  no plugin code. The emitters for runtime records, messages and reports
  record after releasing their own persistence lock. Some events are recorded
  while a caller still holds an outer lock (a launch is recorded under its
  launch lock), which is safe because the journal never takes a core lock, so
  the order is always core lock, then journal. Optional work with real cost
  (deciding readiness, which loads the adapter) runs after the send and launch
  locks are released.
- The plugin host tails the journals of every scope in its tree and calls each
  handler in order. It keeps each plugin's cursor in
  `state/plugins/NAME/events.cursor`, so a handler sees each event at least once
  across host restarts.
  - **At least once:** each handler goes through the events in order on its
    own, guarded on its own in each universe. One still running, or out of
    time, holds only itself: its siblings go on, past any number of events,
    and an event a handler finished is never given to it again while a sibling
    catches up, however many polls that takes. A journal whose state was
    removed starts again with a new id (`state/events/journal-id`, with a
    second copy in `journal-id.copy`, so a lost or unreadable id file is not
    taken for a new journal), and only then does the host forget what its
    handlers finished. Beside each plugin's cursor, `events.cursor.journal`
    names the journal it belongs to, so a journal that started again is
    delivered from its start, whatever the old cursor said. The cursor and the
    journal's counter keep their plain-number format, so a plugin host started
    before an update, still running the old code, keeps its place. A call counts one try when it runs out of time and one more for
    every further 60 s it keeps running; after 3 tries the handler is given up
    on for that event, and its next events get calls of their own. A give-up
    is shown by `mv2 plugins`, `doctor` and the Plugins screen for a day
    ("gave up on event N (agent.status) in its handler H"). A worker left with
    two calls that never returned restarts itself (the supervisor starts a
    fresh one, which has no stuck threads). Each worker runs in a process group
    of its own, and the supervisor ends that group with it, so a stuck call's
    child processes (a hung `subprocess.run`) go too; a worker whose supervisor
    died (`kill -9`) ends its own group as it stops. Such restarts are counted
    over a day, however long each run takes: after 3, the plugin is left
    stopped, and `mv2 plugins`, `doctor` and the Plugins screen say its
    handlers kept hanging. What each handler finished is
    kept next to the cursor (`state/plugins/NAME/handlers.json`, for that
    journal), so a fresh worker never gives a handler an event it finished.
    Handlers that are not held read on from their own place, so siblings keep
    going however far behind a stuck handler is. A handler that raises is
    recorded and passed. The cursor on disk moves past an event once every matching
    handler is done with it, so after a restart an event may come again:
    handlers must be idempotent.
  - **Stale events:** each event says when it happened, so a handler can skip
    stale ones. The notifier ignores events older than five minutes after a
    restart.
  - **Where the cursor starts:** a plugin's cursor starts at the journal's end
    when it is turned on (`mv2 plugins enable`, the Plugins screen, or a
    first-use conversion), so events between turning it on and the host's first
    run are delivered then. A plugin turned on some other way starts at the
    journal's end when its worker first runs, and never replays older history.
  - **Polling:** an idle poll reads no segment: the journal's counter says when
    nothing is new.
  - **Logs:** `events.log`, `host.log` and each plugin's `plugin.log` rotate to
    `.1` at 1 MiB; `errors.log` keeps one traceback per call site a minute and
    rotates the same way. Every log, `errors.log` included, is checked,
    rotated and written under a file lock, so two processes never rotate one
    at once and no line or `.1` file is lost.
- The dashboard may tail the journal too, to refresh sooner than its 1.5 s poll.
  It need not.

Events are emitted at the few places that already own each change, so no
caller can forget one:

| Event | Emitted by | Data |
| --- | --- | --- |
| `agent.created` | `lifecycle.start`, once a new record is published | harness, model |
| `agent.launched` | `lifecycle.start`, after request.json is written and before the runner is released (so `ready` and `exited` never come first) | mode (`spawn`, `resume` or `wake`), native mode, plugins loaded |
| `agent.ready` | `lifecycle.note_ready`, called by the runner after its startup check and by startup delivery once done; it uses `wait_ready`'s own predicate and a per-launch marker, so it is once per launch | none |
| `agent.status` | `mutate_runtime` (which `update_runtime` uses), when `status` changes (after its lock) | from, to |
| `agent.exited` | the runner, once: where the process's end is known, or where the runner itself fails first (exit code `null`) | state (`stopped`/`failed`), exit code, reason |
| `agent.killed` | `lifecycle.kill` | native cancellation state |
| `agent.reported` | `agents.report` | phase, summary |
| `agent.session` | `agents._session_event`, after a native session change is durably recorded (`sessions.transition`), a session pointer is recovered, or `--start-fresh` retires a conversation that is gone (`sessions.retire`, reason `missing-conversation`), for the exact launch ([session lineage](session-lineage.md)) | from, to, reason |
| `agent.moved` | `relocate.move_agent`, in both the source and the destination journal | from, to |
| `agent.removed` | `relocate.remove_agent`, in its universe's journal | trash entry |
| `message.saved` | `mailbox.append_message` | message id, delivery, length, sender's name/agent/plugin (never the text) |
| `message.delivered` | `mailbox.update_message`, once per stage: `accepted`, then `confirmed` when the transcript confirms it | message id, stage |
| `message.failed` | `mailbox.update_message`, once each: when the message definitely failed, and when a delivery check proves it was lost when the agent stopped (it is then `cancelled`; see [messaging](../cli/messaging.md)). Unknown and submitting are not failures | message id, reason; `lost_when_stopped: true` for a proven loss |
| `message.read` | `mailbox.inbox`, after a successful read moved the cursor (never peek, history or a failed print) | count of inbox-only messages read |
| `universe.created` | `templates.create`, in its own journal and its parent's | name, path, template |
| `refresh.applied` | `templates.refresh_apply`, after the manifest is written | files written |
| `PLUGIN.*` | `mv.events.emit` from a plugin | anything JSON (namespaced to its name) |

Moving or removing a whole subverse records nothing in its own journal, which
moves with it. Recording is best effort; delivery of what is recorded is at
least once.

```json
{"schema": 1, "seq": 1042, "at": "2026-10-11T18:20:31Z", "type": "agent.status",
 "universe": "0945d9ff-…", "agent": {"id": "22cf6a98-…", "name": "plugins"},
 "launch": "8c1e…", "data": {"from": "working", "to": "waiting"}}
```

Message text is never put in the journal. A handler that needs it reads it with
`messages.read` permission through the API. Events tell plugins what happened.
They cannot change what core does. Changing behaviour goes through the
synchronous extension points below, each of which has a fixed contract.

### Keeping the journal small

The journal is a series of segment files, so cleaning up deletes whole files
and never rewrites one that a reader may have open:

```text
state/events/
  000000001041.jsonl     closed segment, named by its first sequence number
  000000002388.jsonl     the segment being written
  seq                    the next sequence number
```

- **Rolling.** The writer starts a new segment when the current one reaches
  1 MiB or a new UTC day begins. A day's events are then never split across
  more files than they need, and a whole day can be deleted at once.
- **Retention.** `plugins.json` sets it per universe, with defaults:
  `"events": {"retain_days": 7, "max_mb": 32}`. A closed segment is deleted once
  its newest event is older than `retain_days`. While the closed segments
  together exceed `max_mb`, the oldest is deleted. The segment being written is
  never deleted.
- **Who cleans.** Each writer prunes when it starts a new segment, under the
  lock it already holds. That is at most a few times a day, and it needs no
  host. The host also prunes once an hour. `mv2 plugins events --prune` does it
  by hand, and `mv2 plugins events` shows the size, the oldest event and each
  plugin's cursor. `doctor` flags a journal over its limit, which means pruning
  is failing.
- **Readers that fall behind.** Pruning never waits for a cursor, so a stopped
  host or a disabled plugin cannot make the journal grow without bound. A
  plugin whose cursor points into a deleted segment gets one `events.gap` event
  in each of its handlers, whatever types they subscribe to (how many events
  and what time span it missed), then continues from the oldest kept event.
  Turning a plugin on again moves its cursor to the journal's end, so what
  happened while it was off is not delivered later. An uninstalled plugin's
  cursor file stays in the universe's state until the plugin is back.
- **Removal.** A subverse's journal is part of its state, so `mv2 remove` and
  `move` carry it along like the rest.

### The plugin host: where long-running plugin code lives

Everything mv2 runs today is short-lived or tied to something else: a command
ends when it prints, the dashboard runs only while someone has it open, and a
runner lives and dies with one agent. mv2 has no daemon. Most plugin code fits
those processes. A column runs inside the dashboard, a launch contribution
inside the spawn, a status item is written by whoever knows the value.

Two extension points are different, whatever the plugin does with them:

- **`mv.events.on`:** code that should run *because something happened*. An
  agent exited, a message was delivered, an agent reported `blocked`. The
  event happened in some other process (a runner, the mod's `runtime_update`
  subprocess, a `send` from another agent), and nobody is waiting on the
  handler.
- **`mv.service`:** code that should keep running *on its own*. It listens on a
  socket, watches a directory, polls an outside system on a timer, or holds a
  connection open.

Both need a process that is alive when no command is running and the dashboard
is closed. The alternatives each have a real cost:

| Option | Problem |
| --- | --- |
| Run handlers inside the process that caused the event | That process is a runner, a CLI command or the mod's status report. A slow or broken handler would delay or break core operations, in a process whose environment belongs to another agent. |
| Run them only in the dashboard | Nothing happens while it is closed. That is surprising, since agents keep working without it. |
| Each plugin starts its own background process | Every plugin re-solves starting once, locking, logging, restarting after a crash and stopping. `mv2` can neither show nor stop them. |
| **One supervisor that mv2 owns** (proposed) | One more process, but only when a plugin needs one. |

So the host is a general **supervisor** for plugins' long-running work:

- **Starting.** It is needed while some plugin that is on in its universe tree
  extends `events` or `service`, which its manifest says, so no plugin code is
  read to decide. `mv2 plugins host start|stop|status` controls it by hand,
  and `mv2 plugins enable` says when a plugin needs it and it is off. The
  dashboard starts it once on open, and again after the Plugins screen changes
  something, if it is needed and not running. A failure shows on the status
  line and is not retried until the next change. The header shows `· host off`
  in the warning colour while it is needed and not running, and nothing when
  no plugin needs it. On the Plugins screen, `h` starts or stops it. Quitting
  the dashboard never stops it.
- **Where.** It runs in a tmux session that mv2 owns, so it outlives the shell
  that started it. Its lock (held while it runs), status and log are under
  `state/plugins/host/` of the top-level universe. There is one per top-level
  universe tree.
- **Alive.** Its status counts as running while its process is alive and its
  heartbeat, written every few seconds by a thread of its own, is recent, so a
  supervisor busy stopping workers or pruning never looks stopped. A second
  start (another dashboard) finds it, and never overwrites its status.
- **Stopping.** It exits once no plugin needs it, or on `mv2 plugins host
  stop`, and stops its workers first. If the supervisor dies without doing so
  (`kill -9`, the out-of-memory killer, a crash), each worker notices within a
  second that its parent process changed, stops its services and exits, so
  nothing keeps holding a socket or running a handler. The supervisor gives each
  worker its own pid, so this holds even when it died while the worker was
  still starting. It tells a worker has exited without reaping it (`waitid`,
  or a kqueue on macOS before Python 3.13), so it can still end what the worker
  left in its process group.
- **Isolation.** The supervisor itself loads no plugin code. Each plugin with
  long-running work gets its own worker process, which loads that plugin
  alone. A plugin that blocks, leaks memory or crashes affects only its own
  worker: it is restarted with backoff, at most 3 times in 5 minutes, then left
  stopped with the reason in `mv2 plugins` and the dashboard header.
- **Events.** The supervisor tails the journals (see above) and hands each
  worker the events its plugin subscribed to, keeping each plugin's cursor.
- Disabling one plugin stops only its worker. Turning it off and on again gives
  a worker that was left stopped a fresh start. Turning a plugin on or off in
  one more universe, while it stays on elsewhere, restarts its services with
  the new set of universes within about five seconds.

Most plugins never touch the host: a column, a command, a status item, a launch
contribution, a settings screen or a harness extension need none of this.

## The API

`from multiverse.plugins import api`. A plugin imports nothing else from
`multiverse`. Internals (`runtime`, `universe`, `tools`, `harnesses`) can then
keep changing, as they did in PR #16, without breaking plugins.

### Versioning

- `api.VERSION` is `(1, 13)` today. Adding something raises the minor version.
  Removing or changing something raises the major version.
- A plugin written for a newer minor version than this mv2 has is reported as
  "needs plugin API 1.N; update mv2", even when its manifest uses fields,
  extension points or permissions this mv2 does not know yet.
- **Stability.** 1.x is promised stable from 1.8, the version compaction uses
  with nothing but the API (step 7 of
  [Moving compaction](#moving-compaction-into-a-plugin), 4f6701d). Every newer
  version stays backward compatible with it: a plugin written against 1.8, or
  any later 1.x it names in `api`, keeps working. Additions raise the minor;
  anything else needs a new major. The promise covers:
  - the public names of `multiverse.plugins.api` and `multiverse.plugins.ui`
    (that module's docstring lists what it promises), and
    `multiverse.plugins.testing`'s `PluginTestCase`;
  - the manifest fields, `plugins.json`, `plugin-settings/NAME.json`, the
    status-item files and the event record;
  - `python -m multiverse.plugins run NAME ACTION`.

  It does not cover names starting with `_`, a result's `.record` (the tool's
  full record), or anything else in `multiverse`, which a plugin does not
  import.

  1.13 makes two documented changes to it, both the user's decisions: the
  access levels (a call where the plugin is off, or one above its level, is
  refused; see [effect classes and access grants](#effect-classes-and-access-grants-113)),
  and `mv.usage.adjust`, which still runs but can no longer change core's
  measured `context`: changes to it are dropped, and the rest of what an
  adjuster returns applies as before. A plugin shows an estimate of its own in
  a column of its own (`replaces="context"`), never in place of what mv2
  measured.
- mv2 supports the current major and the one before it for at least one
  release. A plugin on the old major loads with a deprecation note in
  `mv2 plugins`.
- Everything in `api` has a docstring contract and contract tests in mv2's own
  suite, the way `harnesses/types.py` is the adapter contract today.
- Results are frozen dataclasses with the fields a plugin needs, plus `.record`,
  the tool's full record, for anything the dataclass leaves out. `.record` is
  not covered by the version, and the docs say so.

### Effect classes and access grants (1.13)

Every operation in the plugin API and the adapter protocol has one effect
class, and each plugin works under the access level a person grants it per
universe ([access levels](../develop/plugins.md#access-levels) lists them call
by call):

- **observe** reads and is safe to cache, retry and run in render paths within
  their time budget. An observe call may bring core's own records up to date
  with what the harness verifiably did, such as recording a `/clear` the
  adapter detected, as listing and usage do. It never starts or stops a
  process, switches a conversation, adopts the caller's storage, or acts for
  the caller.
- **spend** uses tokens, money or time and changes nothing: `ask`, and the
  one-shot run to come.
- **act** changes a session, a launch, an inbox or a universe's files: spawn,
  resume, kill, send, a refresh applied, an inbox read that marks messages
  read, a settings edit, launch contributions and harness extensions.
- A plugin's own state (its state folder, log, status items, its own
  namespaced events, reading its settings) and a universe's own definition
  facts (its name, its harness definitions without an agent) are outside the
  classes, so they never raise for access.

The grant:

1. **A plugin requests a level** in its manifest (`access`: request, minimum,
   why). One without `access` requests `act` with minimum `act`: its reach
   before levels.
2. **A person grants that level or a lower one**, per universe: `mv2 plugins
   enable` asks on a terminal or takes `--grant`, the dashboard's Plugins
   screen asks, and `mv2 plugins grant` changes it. Only these record a grant,
   in `plugins.json` (`access`), with no inheritance. A plugin that is on with
   no grant recorded has its request, so no universe needs a migration write.
   Without a terminal, `enable` records the minimum. From an agent
   (`MV2_AGENT_ID` or `MV2_AGENT_DIR` set), `enable` records at most the
   minimum and `grant` never raises one; both print the command a person runs.
3. **The plugin reads its level** (`mv.access()`) and offers what fits it.
   Below its minimum, enabling is refused with its own reason.
4. **Core enforces it.** A plugin off in a universe has no access there,
   whatever `plugins.json` records. The API refuses a call above the plugin's
   level in the target universe with `api.AccessDenied` (an `api.Error`, never
   an `OSError`, so an `except OSError` written for disk errors doesn't
   swallow it). Core checks the same limit at its entry points, before a launch
   or send writes any record, and before each adapter call, by the protocol's
   `EFFECTS` map in `harnesses/types.py`. A launch contribution from a plugin
   below `act` is left out of the launch with the reason. `access_ceiling` in
   `plugins.json` caps every plugin in a universe, and `MV2_EFFECTS` every
   plugin in one process.
5. **Declared classes:** a command, dashboard action or helper action declares
   its class (`effect=`, `act` when undeclared) and runs only where the
   plugin's level reaches it; columns, panels, inspector lines, capabilities,
   usage adjustments and settings checks run as `observe`; `mv.effect()`
   declares a plugin's own functions. A call above the declared class, made
   inside, is refused. The declared class follows a context variable, not
   threads a plugin starts itself.

Permissions stay checked under the levels: both must pass. A manifest with
`access` and no `permissions` gets the permissions of its requested level.

This is a change to the 1.x promise: a 1.x plugin that called
into a universe where it is off, or acted from a column, panel, inspector line,
capability or usage function, is now refused there. Everything else a 1.12
plugin did keeps working, at `act`. Like permissions and trust, the levels are
a guard against mistakes that makes a plugin's reach readable, not a sandbox.

### Operations: the client

```python
def register(mv: api.Plugin):
    @mv.events.on("agent.status")
    def waiting(event, mv=mv):
        if event.data["to"] == "waiting":
            agent = mv.universe(event.universe_path).agent(event.agent.id)
            notify(f"{agent.name} needs you", agent.summary)
```

| Call | Wraps | Permission |
| --- | --- | --- |
| `mv.universe(path=None)` gives a `Universe`: `.name`, `.path`, `.subverses()`, `.parent` | `universe.resolve`, `tree` | `agents.read` |
| `Universe.agents(recursive=False)`, `.agent(ref)` give `Agent`: `.id`, `.name`, `.harness`, `.live`, `.status`, `.phase`, `.summary`, `.context`, `.universe` | `list_agents.run`, `inspect.run` | `agents.read` |
| `Agent.inspect(lines=30)`: key facts and terminal tail | `inspect.run` | `agents.read` |
| `Agent.usage()`, `Agent.capabilities()` | `get_usage.run`, `capabilities.run` | `agents.read` |
| `Universe.spawn(name, harness=, task=, model=, wait=False)` | `spawn.run` | `agents.control` |
| `Agent.resume(wait=False)`, `Agent.kill()` | `resume.run`, `kill.run` | `agents.control` |
| `Agent.send(text, delivery="steer"|"follow-up"|"inbox-only", sender=None)` gives a `SendResult` | `send.run` | `messages.send` |
| `Agent.ask(question)` | `ask.run` | `messages.ask` |
| `Agent.inbox(peek=True)`, `Agent.history(last=20)` | `inbox.run` | `messages.read` |
| `Agent.report(phase, summary)` (the plugin's own agent only, in a helper the agent runs) | `report.run` | none |
| `Universe.catalog()`, `Universe.command(name, args)` | `catalog`, `command` | none |
| `Universe.refresh(apply=False)` gives the plan | `refresh.run` | `universe.write` to apply |
| `Universe.doctor()` | `doctor.run` | `agents.read` |

`sender` defaults to the plugin, `{"plugin": NAME}`. The recipient reads
`Message from plugin NAME`, never a person or an agent the plugin is not. Every
call raises `api.Error` with a readable message, as tools raise `ValueError`
today, and nothing exits the process.

Plugin utilities:

- `mv.state_dir(universe)`: `state/plugins/NAME/` of a universe.
- `mv.agent_state_dir(agent)`: `state/agents/<id>/NAME/`. Compaction's existing
  `state/agents/<id>/compaction/` already fits. The API never makes an agent's
  own folder: below it, directories are made one level at a time without their
  parents, and files are written without making their directory, so an agent
  removed or moved at any moment (its folder renamed away or deleted) makes a
  late write fail with `api.AgentGone` instead of bringing it back as an orphan.
  No lock against removal is needed (1.10). A universe's root is kept the same
  way: `mv.state_dir`, files in a plugin's state (`state/plugins/...`), `mv.log`
  and mv2's own plugin logs never make it again (`api.UniverseGone`; logs write
  nothing).
- `mv.agent_at(directory)` and `mv.current_agent()` (from `MV2_AGENT_DIR`, else
  `MV2_AGENT_ID`) find an agent from a helper or a hook.
- `Agent.session()` gives the `SessionInfo` that capability and usage
  contributions receive (native ID, launch, state, native mode, the launch's
  plugins record, and the agent's recorded `workspace` (1.11)).
- `mv.harness_info(universe, harness)` gives a harness's family, executable,
  `unset_env`, whether its bridge is on, its settings, and `home`: its own data
  directory as this process's environment names it (Claude's
  `CLAUDE_CONFIG_DIR` or `~/.claude`, Codex's `CODEX_HOME` or `~/.codex`, Pi's
  `PI_CODING_AGENT_DIR` when set).
- `mv.harness_info(universe, harness, agent=AGENT)` (1.12) answers for that
  agent's launches instead, from its records only, because the dashboard or the
  plugin host may run with another environment. `home` is the recorded session
  home, else the storage a resume would replay (the storage map, or the legacy
  launch evidence resume adopts, read through `universe.storage`, which core
  shares). `storage` is the environment that selects that store wherever it is
  replayed: each storage variable's value or `None` for one the launch had
  unset, a relative value made absolute against the agent's workspace, and the
  launch's `HOME` with an unset or empty home variable. Both are `None` when the
  records don't prove them. Only an agent never launched gets this process's
  environment.
- `api.files` has `read_json`, `write_json` (atomic), `lock` (exclusive, across
  processes), `ensure_dir` (1.10) and `now`.
- `api.text.clean(value)` and `api.text.tokens(n)` show text and token counts
  the way the rest of mv2 does.
- `mv.helper.command(name, *args, universe=)` is the argv that runs one of the
  plugin's helper actions, for example to start a detached worker. A helper
  action owns its stdin and stdout, and its return value is the exit code.
- `mv.log(text, universe)` writes to `state/plugins/NAME/plugin.log`.
- Code that mv2 calls without the API object, such as a plugin command's
  `run(root, ...)` or a screen built as `Class(root=, backend=)`, reaches it
  through the plugin's own module: `register(mv)` runs once in every process
  that loads the plugin, before anything calls into it, so keeping `mv` in a
  module variable there is the intended pattern.

Dashboard screens run in the dashboard process, where the plugin is loaded:
they call the plugin's own functions directly, off the event loop with
Textual's thread workers, as the dashboard's own screens do.

### Extension points

All are methods on `mv`. Each is listed in `extends`, and each has a fixed rule
for several plugins using it at once.

| Extension point | Registers | When several plugins use it |
| --- | --- | --- |
| `mv.cli.command(name, configure, run, cli_run=None)` | An `mv2 NAME` command with the `tools/` shape: `configure(parser)`, `run(root, **args)` returning a record, an optional `cli_run` for readable text. A plugin whose settings cannot be used here keeps its command, which runs after one stderr line saying why: it is often where the settings are shown or fixed. | Core command names are reserved, and `mv2 plugins new` refuses them and mv2's own reserved names. Two plugins with one name: both refused, and `mv2 plugins` says so. |
| `catalog` (no call: listing it in `extends` is enough) | The plugin's own `skills/`, `commands/` and `workflows/`, laid out as in a universe, found without importing its code | The universe's own entry of the same kind and name shadows a plugin's. `name@plugin` or `kind/name@plugin` always reaches a plugin's entry, even a shadowed one. Two plugins with the same name are both listed, and a bare name is then ambiguous. `/multiverse` and the typeahead see them. |
| `mv.bootstrap.line(text)` | One line appended to the agent bootstrap | Plugin lines follow the base bootstrap in plugin-name order, at most 2 lines and 300 characters per plugin, so the short bootstrap stays short. |
| `mv.doctor.check(fn)` | `fn(universe)` returns problems as sentences | All run. Each is listed under its plugin. |
| `mv.launch.contribute(family, fn)` | `fn(LaunchInfo)` returns an `api.Contribution` or None, within 2 s (see [Harness extensions](#harness-extensions)). The manifest's `harnesses` adds plugin directories and extensions with no code. | All run in plugin-name order. Contributions merge by fixed rules, and the harness's own settings and environment never change. A plugin that fails or conflicts is left out of that launch, with the reason recorded, and the launch goes on. |
| `mv.capabilities.add(name, fn)` | A capability key: `fn(SessionInfo)` returns `api.Capability(state, reason, mechanism)` | Core keys are reserved. One owner per key. |
| `mv.usage.adjust(fn)` | `fn(SessionInfo, usage)` returns usage | Applied in plugin-name order, after the adapter. Since 1.13 its changes to `context`, core's measured reading, are dropped; the rest applies. Compaction used it until its own Context column. |
| `statusline` in the manifest, and `mv.statusline.write(agent, name, text)` | A status-line item the manifest declares (`{"trees": {"claude": true, "tmux": false}}`), with its text written by whatever process knows it (see [Status lines](#status-lines)) | Each name has one owner, and core's names are refused. The order and on/off come from `statusline.json`, as today. |
| `mv.dashboard.column(key, title, width, value, compact=, flexible=, group=, fallback=, replaces=)` | An agent column: `value(row)` for agent rows, `group(row)` for subverse rows; since 1.13 it can fall back to a core column's cell or stand in for a data column (not Name, Live, Status or Focus, which carry core's attention markers) | Core keys are reserved. Two plugins standing in for one column cancel, and doctor says so. A view can name it `KEY` while that is unique, and `plugin:KEY` always works. A view keeps a plugin column it names even while the plugin is off, and shows `—` for rows whose universe has it off. A short name that later becomes ambiguous shows `—`, and the clash is listed on the Plugins screen. |
| `mv.dashboard.panel(type, render, title=, auto=)` | A view widget type for `views/*.json`. `render(api.PanelContext)` returns `api.Markdown`, `api.Table`, `api.Artifacts` or a list of them: plain data the dashboard draws, refreshed with the agent list, off its event loop within 2 s. The context has the scope's `universe`, the view's `widget` object, the scope tree's `agents` (with `agents.read`) and the `plugins` on there. With `auto=True` it also shows after the view's own widgets wherever the plugin is on, unless the view's `"plugin_panels"` (`"auto"`, `"none"` or a list of types) leaves it out. The older class form, `"module:Class"`, stays for interactive panels. | The same naming rule. It shows only where its plugin is on; elsewhere it shows "needs plugin X". |
| `mv.dashboard.inspector_fact(fn)` | A function in `AGENT_FACTS` | Core facts first, then plugins in name order. Each fact is wrapped so an error shows as one line. |
| `mv.dashboard.theme(colors)` | A full or partial palette by `theme.py` name | Only the theme `plugins.json` names is applied. |
| `mv.dashboard.settings(fields=None, screen=None)` | The plugin's way into the dashboard: its settings, opened from the core plugin settings screen (`P`). Without this call the dashboard builds fields from the plugin's settings schema. `fields`, a list of `api.Field(path, label, kind, options, placeholder, help)`, chooses and labels them, with the same look and checks. `screen="module:Class"` is the plugin's own screen for what fields cannot express, built as `Class(root=scope, backend=app.backend)`; a string it dismisses with becomes the status line. Such a screen builds on `multiverse.plugins.ui` (API 1.8): `FieldScreen`, `Field`, `field`, `convert`, `MISSING` and `BOOL_OPTIONS`, the dashboard's own settings screen and its look, promised for 1.x as that module's docstring lists. Only plugin screens import it, never the command line. | One per plugin. |
| `mv.dashboard.action(name, fn)` | A command palette entry, `PLUGIN: name`, for something that is not a setting. `fn(context)` runs off the event loop and may return a status line. | Namespaced by plugin. Plugins get no dashboard keys: every core key stays core. |
| `mv.dashboard.screen(name, "module:Class")` | A Textual screen opened from the command palette | Namespaced by plugin. |
| `events` in the manifest, and `mv.events.on(type, fn)` | `fn(api.Event)` run in the plugin's host worker for each event of a type or pattern (`agent.*`) the manifest's `events` subscribes to. The subscription is what makes core record that type at all. | All handlers for a type run, each in its own error boundary, with a 60 s budget. An `events.gap` event says what was pruned before it could be read. |
| `mv.events.emit(kind, data, universe=, agent=)` | A `PLUGIN.kind` event in a universe's journal | Namespaced to the plugin. |
| `mv.service(start, stop=None)` | `start(api.ServiceContext)` runs in a thread of the plugin's worker until it returns or `context.stopping` is set; `stop()` runs when the host stops it | Each plugin has its own worker process. A worker whose service crashes is restarted at most 3 times in 5 minutes. |
| `mv.helper.action(name, fn)` | `fn(arguments)` returns an exit code, for `python -m multiverse.plugins run PLUGIN NAME ARGS…`, which a plugin's harness code runs with `$MV2_PYTHON` | Private to the plugin. |

Every call into a plugin is wrapped. An exception drops that contribution for
that call. It is logged to `state/plugins/NAME/errors.log`, `mv2 plugins` and
`doctor` show it, and the dashboard header counts it (`1 plugin error`). No
plugin error can stop a launch, a send, `list` or the dashboard.

Loading has a limit too: a plugin's import and `register(mv)` always run in a
thread of their own and get 10 s. One that takes longer is left out as failed
for now, and the Plugins screen, `mv2 plugins` and `doctor` say so; it never
hangs `mv2`. Its load goes on: what it ends with lands when it ends, so a slow
cold import is loaded then. The hang is also remembered against the plugin's
files (`~/.multiverse/plugin-load-timeouts.json`): for the next 10 minutes,
processes wait for it at most 0.5 s, and a launch leaves it out while it still
loads, saying so in the launch's `plugins` record and its Plugins line; it is
forgotten once it loads, its files change or the 10 minutes pass. A late
result is kept only while it is still the plugin's latest attempt with the
files it started with: after an edit, the new code loads (under a package of
its own), and the old load's result is dropped when it ends. A caller waits
for a plugin still loading only up to a deadline counted from when its load
started: a launch plan waits until it ends; the dashboard's refreshes (columns,
panels, inspector lines) wait at most `loader.REFRESH_WAIT` (0.5 s) in all, so
its first screen is drawn while a slow plugin loads, and the plugin shows on
the refresh after it has. Meanwhile the Plugins screen lists it as `loading`,
and a view widget it provides says it is still loading. A theme plugin is the one loaded before the first
screen, alone, for at most 2 s; one that takes longer leaves the dashboard its
own colors.

What may be slow runs off the dashboard's event loop, with a time budget.
Inspector lines are computed in the inspector's capture worker and appear when
they arrive. Key actions run in a worker thread. Column values are called for
every row on every refresh, so they must be quick and read only the row.
Phase 2 gives launch, capabilities and usage contributions a 2 s budget.

## Harness extensions

### Launch contributions

A launch contribution is how a plugin changes a harness's launch. It is not an
argv rewrite: the adapter asks the plugins that are on for its family and
merges what they return:

```python
@dataclass(frozen=True)
class LaunchInfo:            # what the plugin is told
    universe: Path; agent: AgentInfo; mode: str     # spawn / resume / wake
    family: str; harness: str; version: tuple | None; bridge: bool

@dataclass(frozen=True)
class Contribution:          # what it may return
    environment: Mapping[str, str] = {}            # never MV2_* core identity
    claude_settings: Mapping = {}                  # deep-merged into --settings; hook lists concatenated
    claude_plugin_dirs: tuple[str, ...] = ()       # usually from the manifest instead
    pi_extensions: tuple[str, ...] = ()
    codex: CodexContribution | None = None         # allowlisted config, instructions, MCP servers (see Codex)
    problem: str | None = None                     # "Launched without compaction: …"
    mode: str | None = None                        # e.g. "without compaction", shown as the plugin's
```

- **Merging.** The adapter's own settings and environment never change: a
  plugin that would change them, or set a variable another plugin already set,
  is left out of that launch. A plugin may set `MV2_<ITS NAME>` and
  `MV2_<ITS NAME>_*` (upper case, hyphens as underscores) and no other `MV2_`
  variable, and no value may hold a NUL. Claude settings deep-merge, with
  each plugin's hook entries after the adapter's own. Claude settings reach
  only a bridged launch.
- **Fitting.** Each manifest entry fits a launch (harness version, bridge) on
  its own, and one that does not is recorded as `unfit`. A plugin none of
  whose entries for that harness fit is left out entirely, its contributions
  too, so a guard never comes without the hook it guards. An adapter asks its
  harness for its version only when a plugin there has something for it.
- **Failure.** If plugin discovery itself fails, the launch goes on exactly as
  the adapter made it, and the record says why (`error`). A plugin left out
  because it broke (its contribution raised, timed out, returned something
  unusable or conflicted) is also a problem of the launch, which the inspector's
  Plugins line and `mv2 inspect` show. A plugin that merely
  does not fit (version, bridge, a plain launch) is recorded only. `mv2 doctor`
  says when a plugin's pre-plugin settings file is still waiting to be
  converted, or could not be.
- **Recording.** What each plugin contributed, and why one was left out, goes in
  `runtime.json` under `plugins`:
  `{"compaction": {"version": "1.0.0", "state": "loaded"}, "typeahead": {"state": "skipped", "reason": "needs Claude Code 2.1.292; this is 2.1.290"}}`.
  The inspector, `mv2 inspect` and `capabilities` read it there. Each plugin's
  `problem` and `mode` are in its entry, and the launch's `problems` and `modes`
  lists. Since 1.13 they are shown only as the plugin's, never in
  `native_mode`, which is the harness's own: "Launched without compaction: …"
  is on the Plugins line, and the mode reads `bridge`, not
  `bridge without compaction`. A launch recorded before then still carries
  them in its fallback reason, which the Plugins line doesn't repeat.
- **Timing.** Contributions are read at launch. Turning a plugin on or off, or
  changing its config, needs a restart of the agent, as `compaction.json`'s
  `enabled` does today.

### Claude

A plugin's `harnesses.claude` entries are Claude plugin directories. For each
one, the adapter's `_with_bridge` checks the following and then adds
`--plugin-dir`:

- `min_version` and `max_version` against `claude_version()`. This generalizes
  `TYPEAHEAD_VERSION`: an older Claude refuses a module that hooks an event it
  does not have, so such a directory is skipped and the plugin is recorded as
  skipped with the version.
- `needs_bridge` (default true for Claude): only when the mod is loaded. A
  plain launch loads no Claude plugin directory at all (it has no mod and no
  function hooks) and asks Claude for no version.
- The plugin is on for the agent's universe.

Each directory is its own Claude plugin, with its own `$.ui.status` slot, its
own events (Claude lets each plugin register an event once), and its own
`claude plugin test` and `claude plugin validate`. Harness code reaches mv2 only
through mv2's CLI and `python -m multiverse.plugins run NAME ACTION`, with
`MV2_PYTHON`, `MV2_AGENT_DIR` and `MV2_PLUGIN_<NAME>_DIR` in its environment.
It never reaches mv2 through Claude engine nouns shared between plugins. Those
(`engine.create` with plugin `dependencies`) are early access and depend on the
version, so they are left out until they are known to work on the oldest
Claude still running.

The typeahead becomes the first bundled plugin to use this (`typeahead`,
`min_version 2.1.292`, `default: on`), so version gating is proved on something
small before compaction depends on it.

**Hot reload.** Claude reloads a plugin directory when its files change. That
is good for developing a plugin and dangerous for running agents. A plugin
directory in the live checkout, or in `~/.multiverse/plugins`, reloads into
every running agent that loaded it. So the rule for the mod applies to every
plugin directory: build changes in a scratch copy, validate them on the oldest
running Claude, then write them. `mv2 plugins check` runs `claude plugin
validate` on each Claude directory a plugin has.

### Pi

`harnesses.pi` entries are extension files, added as `--extension PATH` for
plain and bridged launches alike (after mv2's own `extension.ts` when the
bridge is on). `needs_bridge` defaults to false for Pi, since an extension loads
either way. Version bounds are checked against `pi --version` of the selected
executable, asked only when a plugin has something for Pi. A Pi extension
reaches mv2 through the CLI and the helper.

### Codex

Written by `plugins-codex`. Its evidence and the full inventory of config keys
are in [codex-plugins.md](codex-plugins.md).

Codex has native plugins, skills, MCP servers and lifecycle hooks. The checked
CLI has no Pi-style extension and no Claude-style `--plugin-dir`. mv2's first
Codex integration is a declarative launch contribution that handles plain TUI
launches and remote app-server (bridge) launches explicitly. Installing native
plugins and trusting native hooks stay operations Codex owns.

The adapter's documented baseline is 0.159.3, and 0.162.1 is installed. The
pinned source of both versions has everything below, but only 0.162.1 was run.
A field in the config schema shows that Codex recognizes a shape. It does not
prove that the TUI forwards it, or that every shape works.

#### What Codex offers

| Mechanism | What Codex does, and what it means for mv2 |
| --- | --- |
| Launch config | `-c` / `--config key=value` takes dotted paths and TOML values, with a raw-string fallback: model, effort and verbosity, TUI presentation, developer instructions, MCP, hooks, skill selectors, plugin enablement and environment policy. mv2 accepts a small typed allowlist and does not let a plugin change arbitrary config. |
| `notify` | One command argv. After each agent turn, Codex spawns it with one JSON argument: `type: agent-turn-complete`, thread and turn IDs, cwd, the input messages and the last assistant message. It is fire-and-forget: output is discarded, and a spawn error does not fail the turn. It is no general lifecycle API, replaces neither approvals nor compaction, and passes conversation text to its command. The user's notifier is kept. v1 plugins use mv2 events and do not claim this single slot. |
| Lifecycle hooks | `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`. They come from config TOML, `.codex/hooks.json` layers and native plugin bundles. Command and MCP tool handlers exist; `prompt` and `agent` handlers appear in the schema, which does not mean they run. Hooks that are not managed need native review against their current hash, and mv2's trust does not satisfy Codex's. mv2 never writes `hooks.state.*.trusted_hash`, weakens native policy or adds `--dangerously-bypass-hook-trust` for a plugin. |
| MCP | `mcp_servers.NAME`: local stdio (`command`, `args`, `cwd`, `env`/`env_vars`) and HTTP (`url`, headers, environment and auth settings), tool allow and deny lists, timeouts. Native plugin bundles can add servers too. A plugin's own MCP server is the practical way to give the Codex model tools, and it needs no relay callbacks to run them. Authentication stays the user's. |
| Skills and instructions | Native skills are found under the repository's `.agents/skills`, a trusted project's `.codex/skills`, the user's `.agents/skills`, the older `$CODEX_HOME/skills`, system roots and native plugin skills. `skills.config` turns found skills on or off; it cannot register another skill root. `AGENTS.override.md`, `AGENTS.md` and fallback names make up project instructions. `developer_instructions` adds developer text. `model_instructions_file` replaces the built-in model instructions, so it is wrong for plugin defaults. mv2's catalog skills stay available through the bootstrap and the CLI, with no files installed in the user's home or repository. Custom prompts are deprecated in favor of skills and are not a v1 target. |
| Native plugin packages | The checked CLI has `codex plugin add/list/remove` and marketplace management. A package can use `.codex-plugin/plugin.json`, skills, `.mcp.json` and hooks, or the portable Agent Plugins layout. `plugins.<name@marketplace>.enabled` selects a package Codex can resolve; it is not a directory loader. v1 does not install marketplaces or packages, write `.agents` or `.codex` in a workspace, or change `$CODEX_HOME`. A later package contribution must handle installation identity, refresh, trust and exact resume before `dir` is offered. |
| Profiles | `--profile NAME` layers `$CODEX_HOME/NAME.config.toml`. The 0.159.3 loader rejects the older `profile = "NAME"`, although the schema still has `profile`/`profiles`. Profiles belong to the user: plugins neither create nor select them. mv2's bridge resume already rejects profile overrides (auto picks plain, required fails). |

Sources: [0.159.3 config schema](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/core/config.schema.json),
[notify implementation](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/hooks/src/legacy_notify.rs),
[hook discovery and trust](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/hooks/src/engine/discovery.rs),
[native plugin loader](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/core-plugins/src/loader.rs),
[skill roots](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/ext/skills/src/host_roots.rs),
[profile loader](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/core/src/config/mod.rs),
[hooks guide](https://learn.chatgpt.com/docs/hooks),
[native package guide](https://developers.openai.com/plugins/build/plugins),
[deprecated prompts](https://learn.chatgpt.com/docs/custom-prompts).

#### The v1 launch contract

A `harnesses.codex` entry:

```json
{
  "min_version": "0.162.1",
  "max_version": "0.162.1",
  "instructions": "codex/instructions.md",
  "config": {"model_verbosity": "low"},
  "mcp_servers": {
    "tools": {
      "command": "python3",
      "args": ["{plugin_dir}/codex/server.py"],
      "enabled_tools": ["lookup"],
      "startup_timeout_sec": 10
    }
  }
}
```

The example pins the version that was run, not the first version with the
mechanism. Lower bounds widen once native tests pass on those releases. An
entry needs the `harness.codex` permission, and a local MCP command also
`process.spawn`. Relative instruction paths resolve inside the trusted plugin
root. Only documented placeholders are expanded, as whole argv elements, with
no shell. The instruction text and the plugin's provenance are snapshotted at
launch.

- **`config`:** typed values, not argument fragments. At first:
  `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`, and a
  versioned set of TUI presentation keys such as `tui.animations`,
  `tui.alternate_screen`, `tui.notifications`, `tui.notification_method` and
  `tui.status_line`. Each key needs a known version, type and value. A plugin
  that sets an array such as `tui.status_line` supplies the whole array: it is
  not a native status-item API. Reserved to core and the user: model,
  provider and service tier; credentials and endpoints;
  permissions, sandbox and approvals; environment policy; persistence and
  history; project trust; updates; compaction thresholds; features; profile
  selection; and global skill, plugin and hook settings. No plugin gets
  arbitrary extra CLI arguments.
- **`instructions`:** an optional UTF-8 file added to mv2's generated bootstrap,
  in plugin-name order, wrapped with the plugin's name. A user's explicit
  `developer_instructions` still replaces it, as today, and the launch records
  that it did. AGENTS files and model instructions are never replaced. This
  reuses the bootstrap path, so it works in both modes.
- **`mcp_servers`:** servers under adapter-made names, `mv2_<plugin>_<name>`
  (hyphens normalized the same way everywhere; collisions refused). Transport
  fields, timeouts and tool lists are checked against the supported version.
  Startup is optional by default (`required = false`), and approvals and
  authentication stay native. Manifests may not hold inline bearer tokens or
  OAuth client secrets, only references to the user's environment.
  - **Environment:** a local server receives only the `env` and `env_vars` it
    declares, never the launch's whole environment, so harness `settings.env`
    (API keys) and other plugins' variables never reach it.
  - **References stay references:** `env_vars` values remain references in the
    plain launch's command line and process environment. In bridge mode only
    accepted references are resolved, in the session RPC, and never put on
    argv.
  - **Agent identity:** a server that needs it declares `env_vars` such as
    `MV2_AGENT_ID`, `MV2_AGENT_DIR`, `MV2_UNIVERSE`, `MV2_CLI` or `MV2_PYTHON`.
    In bridge mode the launch-specific `MV2_LAUNCH_ID`, `MV2_CONTROL_SOCKET`
    and `MV2_TMUX_SOCKET` are refused: a bridged conversation outlives a
    launch, so its tools carry the agent's identity, never one launch's.
    Refused entries keep their reason in the launch record. `CODEX_HOME`,
    `CODEX_SQLITE_HOME` and `CODEX_THREAD_ID` references are forbidden;
    declaring identity references does not grant an MCP helper the
    conversation shell's authority.
  - **Tool environment:** the variables the harness and plugins set for tools
    go through the bridge session RPC, and through the process environment in
    a plain launch. An exact, normal, exclusive resume of an idle thread
    applies them; rejoining a native thread that is subscribed or active may
    keep its old config, and mv2 never forces an unload or restart to change
    that. Bridge tool identity is per agent, through the session RPC: the same shared
    conversation is one agent. A frontend restart does not guarantee new tool
    settings. Current mv2 can validate a retained bridge launch for data callbacks;
    it does not rewrite the native environment. See
    [upgrade identity proof](shared-conversations.md#upgrade-running-codex-bridge-agents).
  - **Sharing and refusals:** same-server sharing that Codex accepts is
    normal. A separate-writer native refusal is retained as the launch's
    primary error, even if native retry later succeeds; only a successful new
    `mv2 resume` clears it.

  This is the only v1 way for a plugin to add MCP config, and
  `config.mcp_servers` is refused.

**Plain and bridge launches must mean the same.** Plain mode encodes
contributions as `-c` overrides. Bridge mode needs a narrow transform owned by
the adapter that merges the approved session config into the exact
`thread/start` or `thread/resume` request, not into daemon config. The remote
TUI forwards only some keys: `notify`, `mcp_servers`, `hooks`, `skills` and
`plugins` are missing from its forwarding function in both 0.159.3 and 0.162.1
([0.159.3](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/tui/src/app_server_session.rs),
[0.162.1](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/tui/src/app_server_session.rs)),
so `-c mcp_servers…` alone does nothing in a bridge launch. The transform
belongs to the harness-side phase. It is not plugin middleware, and it imports
no plugin Python into the runner.

Evidence, from isolated 0.162.1 probes without a model turn:

- A namespaced local MCP server passed in `thread/start.config` connected:
  `mcpServerStatus/list` reported `runtimeStatus=connected` with its `ping`
  tool.
- A thread given a harmless item with `thread/inject_items`, then resumed with a
  new MCP server in `config`, resumed fine, but that server was absent from its
  MCP inventory.

So v1 promises a new MCP server on spawn only. On resume it can observe the
servers that were kept, but it cannot promise an added or changed one took
effect. Widening that needs its own native proof. The launch records what was
requested against what was observed, and mv2 never restarts or unloads a user's
shared service to force plugin config through.

**Merging and precedence.**

- **Core settings** are resolved separately.
- **Conflicts:** when two plugins give conflicting defaults that can't be
  combined, the conflicting contribution is left out with a diagnostic.
  Plugin-name order never silently picks a winner. Distinct MCP namespaces
  combine.
- **Placement:** the remaining plugin defaults go before the user's `args` and
  `spawn_args`/`resume_args`, never after `--`, and the user's arguments are
  never reordered.
- **User overrides:** mv2 recognizes `-c KEY=VALUE`, `-cKEY=VALUE`,
  `--config KEY=VALUE`, `--config=KEY=VALUE` and native feature aliases. A user
  override of the same key, an ancestor or a descendant suppresses the
  overlapping plugin default.
- **Paths** are read the way that Codex version reads them (0.159.3 splits on
  literal dots), with no invented quoting.
- **Explicit flags** such as `--model` and `--profile` keep their behaviour.
- **Bridge forwarding:** the user's session overrides of a contributed
  namespace are carried as the top layer too, since the TUI may drop them.
- **Precedence in the request:** values in the native request beat plugin
  defaults, and the user's retained explicit values beat both.
- **What stays:** the `check_for_update_on_startup` scan stays authoritative,
  and plugins cannot undo it. Codex still enforces native managed requirements.

**Gating and evidence.**

- **Versions:** `min_version` and `max_version` are inclusive semantic-version
  bounds. An unknown or prerelease version needs explicit support, never a
  string comparison. The selected executable is checked, not a hardcoded
  `codex`.
- **CLI and daemon:** CLI settings gate on the CLI's version, and bridge
  settings also on the daemon's, which mv2 records separately and which may
  differ. A compatible version does not replace protocol validation.
- **Failures:** an unsupported entry is skipped with a reason, and a broken
  plugin never stops the launch. A plain fallback stays valid, and
  contributions are reported again once the mode is chosen.

`runtime.json`'s `plugins` record says, for each plugin:

- its name, version, fingerprint and entry;
- the CLI and daemon versions and the selected mode;
- the accepted instruction snapshot and its hash;
- the config **key names** applied and the MCP server names;
- fields the user's settings suppressed, and why each skip, conflict or version
  failure happened.

It tells `prepared` and `requested` apart from natively `confirmed`: a rendered
argv or a successful thread reply alone proves neither an MCP connection nor a
trusted hook running. Fallback and resume limits are recorded against the
launch. Credentials, hook payloads, conversation text and raw merged config
never go in it.

Native hooks and package directories are **known future integration points**,
not fields v1 silently forwards. A later `hooks` field must combine native hook
sources without erasing the user's handlers, keep native trust and policy, and
show hooks that were skipped, untrusted or disabled. Even then, an mv2-enabled
hook cannot promise enforcement until native execution is verified. A notifier
that only observes already has mv2 events.

#### Relay observations and later requests

Today `runtime/rpc.py`'s `LaunchProxy` forwards native requests, notifications
and replies both ways, with the adapter's launch preparation, exact binding,
naming and usage observation. Separate control RPCs start with experimental API
support and act on the saved thread and workspace. `status.py` reads
server-wide `thread/status/changed` broadcasts, and `usage.py` reads
exact-thread `thread/tokenUsage/updated` on the TUI stream. There is no plugin
subscription or generic RPC API.

| Native surface | Possible plugin use, and who owns it |
| --- | --- |
| `turn/started`, `turn/completed`; `item/started`, `item/completed`; `thread/queue/changed`; `hook/started`, `hook/completed`; `thread/compacted` | Future passive events. They must be observed on the bound TUI connection, since a passive server-wide observer gets no turn or item events. Emitted only after exact binding, filtered by thread, with launch, thread, turn and item identity. A completed turn may have failed or been interrupted. |
| `thread/status/changed`, `thread/tokenUsage/updated` | mv2's existing status and context paths feed the event journal, with normalized metadata, never raw frames. |
| `thread/read`, `thread/turns/list`, `thread/items/list`, `thread/queue/list`, `thread/backgroundTerminals/list` | A later bounded read API for the exact agent: paginated, revalidating the native ID and workspace, enforcing permissions, and never loading or resuming a thread just to look. |
| `thread/queue/add`, `thread/queue/start`, `turn/steer`, `turn/interrupt`, queue deletion, background terminal cleanup | Already owned by send and stop. Plugins use the public agent API, which keeps durable message IDs and the acceptance and receipt rules. No raw way around them. |
| `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`, `item/tool/requestUserInput`, `mcpServer/elicitation/request` | Server **requests** with an `id`, not notifications. The TUI answers them. Observers neither answer nor swallow them. |
| `item/tool/call`, `dynamicTools` | Dynamic tools need a client that answers on the owning connection, which a passive observer cannot be. v1 model tools use MCP. |
| `skills/extraRoots/set`, `plugin/install`, config writes | Real native APIs but not per-agent plugin operations: extra skill roots replace those of the shared skills service, and install and config change native global state. Never exposed through a blanket relay permission. |

The installed `generate-json-schema --experimental` output and the
[0.159.3 method definitions](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/app-server-protocol/src/protocol/common.rs)
list the protocol. The relay already carries these messages but does not hand
them to plugin code.

A later passive observer would have the following, and no plugin imports in the
runner:

- **Isolation:** bounded queues with drop counts, so a slow host cannot stall
  the TUI or an approval.
- **Attribution:** account, skill and MCP notifications without a thread ID are
  never credited to whichever agent has a connection.
- **Content:** conversation and tool payloads need their own read permission
  and stay out of the ordinary journal.

Native `thread/start.selectedCapabilityRoots` (0.159.3 source, 0.162.1 schema)
selects package roots, and its local MCP path has native tests. It may serve a
future bridge-only package loader. It is not a CLI flag, though, and resume has
no matching field. Skill and hook loading, persistence, exact resume and client
feature gating need proof before `harnesses.codex.dir` is offered.

#### Tree compaction for Codex: analysis only

Codex could implement `CompactionSource` one day, but lifecycle signals are not
enough. It needs:

- stable completed-turn IDs from `turn/completed`;
- a whole-conversation reader over paginated native turns and items, including
  tool calls, results and images;
- consistent filtering of earlier install rows;
- a cancellable builder with typed error and rate-limit mapping;
- a renderer that produces a valid Codex conversation with the protocol's
  `messages` and `count`.

Modern threads can have `path=null`, so a guessed rollout JSONL path is not a
transcript contract.

Above all, it needs an atomic native **replacement** of the model-visible
history of the same thread, persisted, with ownership and idle locking. None
exists:

- `thread/compact/start` runs Codex's own compaction, not a tree install.
- `thread/inject_items` appends items; it does not replace them.
- The unstable, Cloud-only `thread/resume.history` is not a general install
  contract.
- Editing SQLite or rollouts behind a loaded daemon, or swapping to a fork,
  would break exact identity and persistence.

Native `PreCompact` can stop a compaction with `continue: false` (a
Claude-style `decision: block` is not accepted for this event). That is useful
but carries no replacement. Native hook trust, disabling, policy and failure
handling must give a verified fail-closed guard for automatic and manual
compaction before mv2 offers tree compaction. Untrusted or missing hooks do not
run and cannot enforce it. Codex's compaction capability stays unavailable.

Sources: [PreCompact implementation and tests](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/hooks/src/events/compact.rs),
[history injection](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/core/src/codex_thread.rs),
[thread resume fields](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/app-server-protocol/src/protocol/v2/thread.rs),
and the compaction plugin's `engine/protocol.py`.

### Status lines

A status-line item is one short text per agent. It must reach two readers that
cannot import plugin code: the mod's Claude line (TypeScript, polling every 2 s
by file size and time) and the tmux bar (a stdlib-only script tmux runs every
2 s). So an item is a file:

```text
state/agents/<id>/status-items/<item>.txt      one line, at most 200 characters
```

- **Writing.** Whatever process knows the value writes it, atomically: a helper,
  a host handler, or the plugin's Claude directory with `$.fs.write`. An empty
  or missing file shows nothing. Compaction writes `compaction.txt` from the
  facts it already prints (its `status` helper action):
  `trees 51/52 · building`.
- **Reading.** The mod adds a declared item's file to the files its timer
  watches. The tmux script reads it only when the item is on for that harness.
  Both clean control characters and shorten the text, as they do for summaries.
- **Names.** An item named after a plugin belongs to that plugin alone, as
  compaction's `compaction` does. A name two plugins declare is shown for
  neither. `mv2 doctor` names both cases.
- **Configuring.** `statusline.json` is unchanged. A plugin item is a key in
  `items` (Claude) or `tmux.items`, and the order of keys is the order on
  screen. Manifests declare item names, so an item a plugin declares is known
  rather than an "unknown key ignored". The `compaction` key already in every
  template's `statusline.json` keeps working.
- **Transient lines.** A plugin's Claude directory can also use its own
  `$.ui.status` slot for something live, such as `/compact`'s progress bar.
  That shows as a second line only while it lasts.

Core's own items (`agent`, `phase`, `summary`, `inbox`, `context`, …) stay
computed as now. Python callables for tmux items are not offered: tmux starts
the script every two seconds, and importing plugins there would be slow and
fragile.

## Trust and safety

Plugins are code that runs with the user's permissions, in the CLI, the
dashboard, the host and the harness. Templates' legacy argv adapters already
are. mv2 does not sandbox Python, and this design does not pretend to. The model
is about consent and blast radius:

1. **Nothing runs unless the user chose it.** Installing a plugin enables it
   nowhere. Only bundled plugins can default to `on`, and today that is only
   typeahead.
2. **Universe plugins need trust.** A repository can carry `.multiverse/plugins/`,
   and opening the dashboard in a fresh clone must not run its code.
   - **Recording trust:** `mv2 plugins trust NAME` trusts this universe's own
     copy of NAME, even when a bundled, checkout or other copy has the same
     name (a local fork of compaction), and says which copy it trusted and
     what it now overrides. It records the copy's path and
     a SHA-256 over every one of its files in
     `~/.multiverse/trusted-plugins.json`, outside every universe. Left out of the
     hash is only what is not part of a plugin and never runs: `.git`, and
     the caches Python and test tools write (`__pycache__`, `.pyc`, `.pyo`,
     `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.coverage`), so running a
     plugin's own tests leaves it trusted. `node_modules` and native libraries
     are included, and `trust` warns when a plugin ships code that can't be
     read as source. A plugin is hashed again only when its files change (by each
     file's size, times and inode, kept in
     `~/.multiverse/plugin-fingerprints.json`), so a large `node_modules` does
     not slow every `mv2` call.
   - **Who can trust:** trust is meant to be the person's decision after
     reviewing the plugin, but nothing technical stops an agent from running
     `mv2 plugins trust`, or any process with the user's permissions from
     writing that file.
   - **Changes:** any change to the files means trust is needed again (like
     direnv's `allow`). Until then the plugin is listed as `untrusted` and
     never imported.
   - **No hiding:** an untrusted universe copy never hides a plugin of the same
     name from another source; a repository's `plugins/typeahead` cannot turn
     the bundled one off. A trusted copy does take precedence, and says so
     wherever the person looks: `mv2 plugins list` and `info`, and the Plugins
     screen ("overrides the bundled typeahead"). Edited after it was trusted,
     it is set aside again and the other copy runs: `mv2 plugins` and `doctor`
     say so, with how to trust it again.
   - **What runs:** never the universe's directory itself, but a copy of
     exactly the trusted bytes in `~/.multiverse/plugin-cache/<sha256>/`. A
     copy no trusted plugin points at, and that no launch or load used for 14
     days, is removed (at trust, untrust and the host's hourly prune), as are
     staging directories a process left behind. It is
     hashed while copied and left out if anything changed meanwhile, and what
     the hash leaves out is not copied either, so a `.pyc` planted beside a
     reviewed `.py` never runs: Python compiles the copy's own. The plugin is imported from that copy and its Claude
     directory loaded from it, so what Claude writes there (`tsconfig.json`,
     types) never touches the trusted plugin. `mv2 plugins check --load` uses
     the copy too, and a process that imports from a universe's plugin
     directory anyway (a developer's `MV2_PLUGIN_PATH`) writes its bytecode to
     `~/.multiverse/pycache`, never into the plugin.
3. **Permissions are declared and enforced at the API.** The `mv` object a
   plugin receives refuses what its manifest does not ask for: without
   `messages.send`, `send` raises `api.Error`. This catches mistakes and makes a
   plugin's reach reviewable. It is not a security boundary against a plugin
   that imports `subprocess`, and `mv2 plugins info` says that. The permissions
   are `agents.read`, `agents.control`, `messages.send`, `messages.ask`,
   `messages.read`, `universe.write`, `network.listen`, `process.spawn`,
   `harness.claude`, `harness.pi`, `harness.codex` and `models.<harness>`.
   `mv2 plugins enable` lists them in words ("can send messages to any agent in
   this universe as plugin notifier").
   - **Enforced by the API:** the `agents.*`, `messages.*` and
     `universe.write` permissions gate the API calls that need them.
   - **Enforced where a manifest uses them:** the `harness.*` permissions
     (harness entries) and `process.spawn` (a Codex MCP server's command).
   - **Declared only:** `network.listen` and `models.<harness>`, the latter
     for a plugin that spends model tokens, as compaction's merges do. mv2 has
     no API for them to gate: a plugin listens or calls a model with its own
     code. They tell the person, when enabling, what the plugin will do, and a
     reviewer can hold the plugin to them.
4. **Failures are contained.** As described under [Extension points](#extension-points),
   a failing plugin loses its own contribution and nothing else.
5. **Safe mode.** `MV2_PLUGINS=off` loads no plugin at
   all, including bundled ones, for debugging. Launches made this way record
   that no plugins were loaded.
6. **Plugins act as themselves.** A plugin's messages say they come from the
   plugin. Its events are namespaced. It cannot report status for an agent:
   `runtime_update` stays for harness integrations.

### What the remote control must do

The remote control is the riskiest sample. It lives in its own repository,
included in mv2's as an optional submodule (see
[Plugins in their own repositories](#plugins-in-their-own-repositories)), and it
is off by default even when present. These rules are requirements, and its
tests check each one:

- **Off unless enabled,** per universe, by the user. `mv2 plugins enable`
  lists what it may do, and `mv2 remote` shows its address, what it allows and
  where its token is. It runs only in the host, with one endpoint per universe
  where it is on.
- **Loopback only.** By default it listens on a Unix socket at
  `state/plugins/remote/remote.sock` (mode 0600, in a 0700 directory). It uses
  `127.0.0.1:PORT` when the settings name one, which is needed where the
  universe's socket path is too long for a Unix socket. It refuses any other
  address. Remote access is the user's own SSH tunnel (`ssh -L`).
- **A token for every request.** 32 random bytes from `secrets`, made when the
  endpoint first starts (or by `mv2 remote token`), stored at
  `state/plugins/remote/token` (0600; `state/` is already Git-ignored), sent as
  `Authorization: Bearer …` and compared in constant time. `mv2 remote token
  --rotate` replaces it, and the old one stops working at once. A failed
  attempt waits a second and is audited.
- **No browser attacks.** It rejects requests carrying `Origin` or `Cookie` and
  sends no CORS headers. A `Host` header other than its own address is refused,
  against DNS rebinding.
- **Least power by default.** The token's scopes are `read` (list, inspect
  summary) and `send` (steer, follow-up, inbox-only). `spawn`, `resume`, `kill`
  and `ask` are off unless `plugin-settings/remote.json` turns them on. It never runs shell
  commands or `mv2 command`.
- **Bounded.** JSON only. Messages are at most 64 KiB, and requests are rate
  limited.
- **Auditable.** Every request (time, peer, route, agent, outcome, never message
  text) goes to `state/plugins/remote/audit.jsonl` (0600). Messages arrive as
  `Message from plugin remote`, so agents and people can tell.

Built in the `mv2-remote-control` repository, mv2's `plugins/remote`
submodule, with a test for each rule above (mv2's suite runs them when the
submodule is initialized) and a run through the real plugin host.

## Moving compaction into a plugin

`compaction` listed everything that knows about compaction today, and checked
the plan. The target is a plugin in its own repository, checked out at
`plugins/compaction/` in mv2's as an optional submodule. It uses only `api`.
Its state stays at `state/agents/<id>/compaction/`, which already fits the
[settings layout](#settings).

### Turning it on, and its settings

Compaction follows the general rule: `plugins.json` turns it on, and its
settings file only configures it.

| Today, `compaction.json` | As a plugin |
| --- | --- |
| Top-level `enabled` | `plugins.json` `"enabled": {"compaction": true}` |
| `harnesses.<name>.enabled` | Kept, and only as an opt-out: `false` turns compaction off for that harness. When the plugin is on, it applies to every harness that supports it (today only Claude). |
| `default` (scheduler, budget, image mode), `harnesses.<name>.default`, `harnesses.<name>.builder` | Unchanged, in `plugin-settings/compaction.json` with `"schema": 2` |
| A missing file means off | A missing settings file means the defaults. Off is decided by `plugins.json`. |
| An agent's own scheduler, `state/agents/<id>/compaction/config.json` | Unchanged |

`mv2 plugins migrate` (and `mv2 refresh --apply`, which offers it) converts each
universe's `compaction.json`. It turns the plugin on in `plugins.json` exactly
where the old file left compaction on for Claude, writes the rest to
`plugin-settings/compaction.json` without the `enabled` keys, and moves the old
file to trash. This happens on first use: the first `mv2` command or plugin
helper in a universe converts it (see [Settings](#settings)), and `mv2 update`
does it for its caller's universe. Until then nothing reads the old file: the
plugin has no switch there, and GOAT refuses to run while a `compaction.json`
waits (Claude's own compaction is used meanwhile), saying so in its capability,
`doctor` and the status line. The base template ships compaction off,
with its builder settings written out in `plugin-settings/compaction.json`. The
development template turns it on in `plugins.json`.

A universe that turns compaction on without the plugin installed (its
submodule not initialized) launches as if it were off: Claude's own `/compact`
works, and `mv2 plugins` and `doctor` say how to install it. That is safe
because the `PreCompact` guard is the plugin's own launch contribution, so it
is absent too.

### What moves where

Done in steps 2 to 6 below. "Before" is where the code was in core; the
plugin's files are under `src/multiverse/plugins/builtin/compaction/`.

| Before | Now |
| --- | --- |
| `compaction/` (model, config, schedule, store, build, pipeline, protocol, `__main__`) | The plugin's `engine/`. The `__main__` actions are `mv.helper` actions. The `-m multiverse.compaction` forwarding stub that served agents launched before the move was removed in 79c8761. |
| `harnesses/claude/compaction/` (transcript, merge, render, images, `ClaudeCompaction`) | The plugin's `sources/claude/`. The plugin picks the source by the agent's harness family; `CompactionSource` stays its internal seam. |
| `mod/hooks/compaction.ts` (`session.compact`, `classic.Stop`, `classic.SessionStart`, `session.start`) | The plugin's `claude/` directory, a Claude plugin of its own. `/compact` progress uses its own status slot. |
| The mod's `compaction` status item, read through `compactionLine()` | `status-items/compaction.txt`, written by the plugin, read by the mod like any item |
| Adapter `_with_bridge`: `MV2_COMPACTION=1`, the `PreCompact` guard in `--settings`, `bridge without compaction` | A launch contribution: environment, `claude_settings` with the guard, `problem` and `mode` |
| Adapter `compaction_state`, `compaction_defaults`, `compaction()` | The plugin's own functions over its Claude extension |
| Adapter `_compaction_capability`; `base.py` giving every harness "no tree compaction"; `CAPABILITIES` listing it | `mv.capabilities.add("compaction", …)`, which answers for every harness, with the same texts |
| Adapter `latest_compaction()` and `get_usage`'s estimated context | `mv.usage.adjust`, with the same fields (`estimated`, `estimated_at`, `estimated_from`); since 1.13 compaction's own Context column instead |
| `display.py` "estimated after compaction: conversation only" | Generic in core: it showed `estimated_reason` from the adjuster, until 1.13 removed core's estimate display |
| `tools/compaction.py`, `cli.py`'s `compaction` | The plugin's `command.py`, registered with `mv.cli.command("compaction", …)`, with the same arguments and output |
| `tools/doctor.py` `_compaction` | `mv.doctor.check` |
| `ui/compaction.py`, the four backend methods, `action_compaction`, the `c` binding, the help line | The plugin's `screens.py`, registered with `mv.dashboard.settings(screen=…)` and opened from the plugin settings screen (`P`); `c` is free. The screens are unchanged, on `FieldScreen`, and the per-agent scheduler opens from inside them. |
| Templates' `compaction.json` | `plugins.json` and `plugin-settings/compaction.json` (above) |
| `tools/doctor.py`'s check of `compaction.json` | The plugin's `check(settings)`, which core's settings validation runs |
| The `statusline.json` item, the mv2-help skill, the Claude README | The item is unchanged. The skill and README text say compaction is a plugin and how to install it. |
| `tests/test_compaction.py`, `test_claude_compaction.py`, `test_tui_compaction.py`, `mod/tests/compaction.test.ts` | The plugin repository's `tests/`, on `multiverse.plugins.testing` |

Stays in core: the Claude transcript usage rule that a row stored again with
zero usage never replaces its response's usage. It applies to Claude's own
compaction too.

### Steps

Progress: all eight steps are done on `plugin-system`:

| Step | Commits |
| --- | --- |
| 2 | 4c8aaa3, b52eac2, e2f5966, d0a29d1, 6371e15, ba97471 |
| 3 | 86aa026 |
| 4 | 6706189 |
| 5 | f82f2bb, 42f9bf0, f7b2ca4 |
| 6 | f1d60ff |
| 7 | 4f6701d |
| 8 | 7721049: `plugins/compaction` at GOAT 5ffb0ed |

After that:
- 79c8761 removed the transition pieces: the mod's copy of compaction's hooks
  and the `-m` stub.
- The first-use conversion of `compaction.json` came in 483a83a.
- bd24532 dropped the legacy read, pinning GOAT f295d43.

Each step keeps the full suite, `test_compaction.py`,
`test_claude_compaction.py` (with `DashboardBackendTest`),
`test_tui_compaction.py` and the mod's `compaction.test.ts` passing, with only
import paths changing in tests:

1. **Extension points first, each with a core user.** Launch contributions,
   capabilities, usage, doctor, CLI commands, dashboard settings and screens, and
   status-item files land with no plugin using them. Typeahead moves to a
   bundled plugin to prove Claude version gating.
2. **Register compaction in place.** A bundled `compaction` plugin whose
   `register` points at the existing modules. Core's direct calls are removed
   one extension point at a time: the adapter stops building the guard and
   environment, `base.py` stops answering the capability, and so on. Files do
   not move yet, so a failure is easy to bisect.
3. **Status item through a file.** The plugin writes `compaction.txt` wherever
   it learns the status (`status`, `background`, after an install). The mod
   reads it, and `compactionLine()` goes.
4. **Split the Claude side, without breaking running agents.** This is the
   hazard `compaction` named. Running agents hot-reload the mod from the
   checkout but keep their launch settings. If `compaction.ts` simply left the
   mod, their `PreCompact` guard would still refuse Claude's own compaction
   while no hook answered: `/compact` would be blocked until a restart. So:
   - New launches loaded the plugin's own `claude/` directory and set a marker
     variable (`MV2_COMPACTION_PLUGIN=1`, dropped in GOAT f808f56 once nothing
     read it).
   - The mod kept its compaction hooks for one transition period, active only
     when `MV2_COMPACTION=1` and that marker was unset, so exactly one plugin
     answered `/compact` in every session, old or new.
   - The mod's hooks and the plugin's are the same code. The plugin's is
     validated in a scratch copy on the oldest running Claude (2.1.290 at the
     time of writing) before it is written to the checkout.
   - Once no launch used it, the mod's copy was removed (79c8761).
5. **Settings.** `plugins.json` becomes the switch, `plugin-settings/compaction.json`
   the settings, and each universe's `compaction.json` is converted on first
   use (above). The legacy read and the rule that kept a plugin on for its old
   file went once that conversion was in (483a83a, then GOAT f295d43).
6. **Move the files** into `src/multiverse/plugins/builtin/compaction/` while it
   is still in this repository. The old module path stayed as a forwarding
   stub, as `runner.py` and `statusline.py` did after PR #16, because running
   mods, the `PreCompact` command in their settings and the worker spawn called
   `python -m multiverse.compaction`. It was removed with the mod's copy once no
   launch used it (79c8761).
7. **Use only `api`.** The plugin stops importing internals: it finds its
   agent through `mv.universe().agent()` and the session through the agent, and
   uses `api.files` for writes and locks, `api.text` and
   `mv.harness_info(...).home` in place of core helpers, and
   `multiverse.plugins.ui` for its screens (all there from API 1.8). When it is
   done, 1.x is promised stable ([Versioning](#versioning)).
8. **Its own repository.** Once it uses only `api` and its tests run on
   `multiverse.plugins.testing`, its directory is split into the new repository
   with its history, and mv2's copy is replaced by the `plugins/compaction`
   submodule (see below). `docs/compaction.md` goes with it, and the README,
   `harness-adapters.md` and `views.md` link to it.

Steps 1 to 7 happen in this repository so each is one commit that is easy to
bisect. Only the finished plugin moves out.

Everything in [compaction.md](compaction.md) is kept, and these points
specifically. `compaction` called them out, and each one has a test:

- It never falls back to Claude's own summary while tree compaction is enabled
  and usable. The hook skips with a reason, the `.catch` skips, and the guard
  exits 2.
- An unusable settings file (or an unconverted `compaction.json`) never blocks a launch. The launch runs without
  the guard and records why.
- The hook stays within its 10 s budget, with the heavy work in a spawned
  Python process. A subagent's compaction and `precompute` are left alone.
- Every message is built and none is kept by handle. Install records are
  derived from the transcript, and `installs/<id>.json` keeps `created_at` for
  the usage estimate.
- Status counts completed turns only. The turn-end build is awaited in
  `classic.Stop`, so a reload cannot cancel it.
- `/compact` arguments are one-off scheduler settings, and free text is
  refused.
- Builder settings are per harness, and a top-level `builder` is refused.
- The install lock and per-turn tree locks are shared by the worker and the
  install.

## Plugins in their own repositories

Compaction and the remote control each get their own repository, versioned on
their own, and mv2's repository includes them as optional Git submodules:

```text
multiverse-v2/
  .gitmodules
  plugins/
    compaction/    -> github.com/GoatedGoaterestGoat/GOAT                 (submodule, pinned commit)
    remote/        -> github.com/GoatedGoaterestGoat/mv2-remote-control   (submodule, pinned commit)
  src/multiverse/plugins/builtin/typeahead/   (stays in mv2: it is part of the mod)
```

- **Optional.** A plain `git clone` leaves `plugins/compaction/` empty, and mv2
  works without it. `git clone --recurse-submodules`, or
  `git submodule update --init plugins/compaction` later, adds it.
  `mv2 plugins install compaction` runs that for a checkout and says what it
  did. An empty submodule directory is not a plugin and is not an error.
- **Found as "checkout" plugins.** Discovery looks in `plugins/*/plugin.json` at
  the root of the running mv2 checkout. That is outside `src/`, so the mv2
  package itself is built and tested the same with or without submodules.
  Checkout plugins are trusted: the mv2 repository pins their exact commit, and
  the user chose to initialize them.
- **Pinned together.** mv2's commit pins each submodule's commit, so a checkout
  always has a combination that was tested together. Each plugin's manifest
  still states its `api` version, and a plugin updated on its own (a newer
  commit checked out in the submodule) is refused with a clear reason if it
  needs a newer API than this mv2 has.
- **Installed without a checkout.** Each plugin repository is also a Python
  package with a `multiverse.plugins` entry point, so
  `pip install git+https://github.com/GoatedGoaterestGoat/GOAT` works for an mv2
  installed with pip. Both routes load the same code.
- **Updating.** `mv2 update` moves the plugins that are initialized with the
  checkout and never fetches or initializes one that is not. Before the fetch,
  again before the merge, and again before each plugin moves, mv2 checks for
  tracked changes and conflicts, and refuses to discard local work (untracked
  files are fine). A clean plugin checkout on a branch is left untouched, with a
  warning only when its commit differs from mv2's pin, so updating mv2 never
  detaches the user's plugin branch. A detached
  checkout ahead of or diverged from mv2's pin still blocks the update, with its
  actual relationship to the pin reported rather than a claim that its commits
  are unpublished. Only clean detached checkouts at or behind an older pin move;
  a checkout left behind is retried even when mv2 itself is current. It moves with
  `git submodule update --checkout`, one plugin at a time, with nested fetches
  off, whatever the user's update
  mode is. Local-path remotes are allowed for that fetch only. `--check` shows
  which plugins would move without fetching them. Each plugin fetch has a
  120-second total budget; use `mv2 update --plugin-fetch-timeout SECONDS` to
  allow longer for a slow remote. mv2 requests the pinned commit first. A server
  that refuses fetching by commit gets one ordinary fetch using the remaining
  budget; a timeout stops fetching immediately. An unavailable or revoked
  remote produces a warning: mv2 and its environment still update, the plugin
  stays at its old commit, and the next update retries it. Restore access or
  uninstall the optional plugin: save any local files first, then run the printed
  `git -C CHECKOUT submodule deinit -f -- plugins/NAME` command to remove its
  checkout. mv2 never runs that command automatically. Other move failures preserve local work and
  report a repair. New plugins are reported as available (`mv2 plugins install
  NAME`), removed ones are left on disk, and a changed URL is shown with the
  `git submodule sync` to apply it. With no plugin submodules, `mv2 update`
  is exactly as before.
- **Hot reload.** A submodule's Claude directory is loaded from the checkout,
  so a submodule update reloads it into running agents, exactly like the mod.
  Changes to a plugin's Claude code follow the mod's rule: build in scratch,
  validate on the oldest running Claude, then commit and update the pin.
- **Tests.** Each plugin repository runs its own suite against mv2 (a
  development dependency on mv2 by Git URL, or the enclosing checkout when it
  is a submodule). mv2's suite runs the tests of initialized checkout plugins
  too, and skips them with a note when they are absent, so a change to core that
  breaks compaction is still caught in a full checkout. Core's own tests of the
  extension points use small fake plugins, so they never depend on a
  submodule.
- **History.** The new repositories are made from mv2's history with
  `git filter-repo` limited to the plugin's paths, both current and earlier
  ones, so `blame` and `log` still explain the code. The remote control has no
  history yet and starts fresh.
- **Working on them.** Commits to a plugin are made in its repository (the
  submodule directory is a checkout of it), and mv2 then commits the new pin.
  An agent working on compaction commits in `plugins/compaction`, under the
  same rules: only its own paths, no branch switches.

## Sample plugins

| Plugin | Shows | Where |
| --- | --- | --- |
| `hello-column` | The smallest plugin: a dashboard column (`uptime`, from `runtime.json`'s start time) and an inspector line. No permissions beyond `agents.read`. | `examples/plugins/hello-column/` |
| `cost-item` | A status-line item for Claude and tmux (`$0.42` from usage), written by a host handler on `agent.status` | `examples/plugins/cost-item/` |
| `notifier` | Events and a service: a desktop notification (`osascript` or `notify-send`) when an agent starts `waiting`, exits with an error, or reports `blocked` or `done`, debounced and skipping stale events | `examples/plugins/notifier/` |
| `remote` | Send to and list agents from outside, under the rules above. Includes a tiny `mv2-remote` client script. | Its own repository; optional submodule `plugins/remote`, off by default |
| `typeahead` | A Claude harness extension with version gating | Bundled, on |
| `compaction` | Everything: CLI, launch, capabilities, usage, settings and migration, status item, dashboard screens, a harness extension, helpers, state | Its own repository; optional submodule `plugins/compaction` |

### Testing your own plugin

- `mv2 plugins new NAME` writes a starter plugin (a column and its test) to
  begin from, and says how to use it while you work on it
  (`MV2_PLUGIN_PATH`).
- `mv2 plugins check PATH` validates the manifest, the API version,
  `extends` against what `register` actually registers, imports in a CLI-like
  process (no Textual), name conflicts with core and with plugins that are on,
  and `claude plugin validate` for Claude directories. It never imports a
  universe's own plugin that is not trusted: that one gets the manifest,
  schema and default-settings checks only, unless `--load` asks for its code.
  A trusted one is checked from its trusted copy.
- `multiverse.plugins.testing.PluginTestCase` gives each test a temporary
  universe made from the base template with the plugin (its `plugin`
  directory) found and turned on. It also gives a temporary home, a temporary
  `CLAUDE_CONFIG_DIR`, a `bin/` directory first on `PATH` and no inherited
  agent identity. Nothing in it starts an agent, tmux or a harness. It runs
  guarded: a write under your real `~/.multiverse` (the one before any test
  moved `HOME`), by the test or by a thread it started, is refused with
  `PermissionError` and fails that test, never a later one. Writes no test can
  be charged with (a test class that never names itself, or a thread that
  outlived its test) are printed at the end and fail the run. A path given
  relative to a directory file descriptor (as `shutil.rmtree` removes files)
  is resolved through that descriptor, so the working directory never decides
  it, and every path is checked as written and as resolved, so a write through
  a link elsewhere that points into the home is refused too. The plugin under test's own directory, and Python's bytecode in a
  personal plugin (`~/.multiverse/plugins/NAME`), are not guarded: a personal
  plugin runs its tests from its own directory. Its helpers:
  - `loaded()`: the plugin loaded and registered, as a process that needs it
    would load it. Its `registrations` hold what `register` added, such as
    columns, handlers, launch contributions and helpers, to call directly.
  - `agent(name, harness=, native_id=, runtime=)`: an agent's records, written
    without starting anything.
  - `claude_transcript(native_id, records)`: a Claude transcript where Claude
    keeps it.
  - `executable(name, script)`: a fake program on `PATH`.
  - `module(name)`: a module of the plugin under test, such as
    `"engine.store"`, as mv2 loaded it for the test (1.9).
  - `write_plugin`, `enable`, `subverse` and `entry`, for tests that need
    other plugins or scopes.
  - `dashboard(size=, root=)` (1.9): the real dashboard on the test's
    universe with the plugins really loaded, used as `async with
    self.dashboard() as d` in a test that is also an
    `IsolatedAsyncioTestCase`. There is no fake backend to keep in step with
    mv2. A small set of actions drives it: `open_plugins()`,
    `open_settings(name)` (P, the plugin, Configure), `run_action(title)`,
    `press`, `until`, `select`, `enter`/`back`, `table()`, `local_view()`,
    `status` and `header`. `multiverse.plugins.dashboard_testing`'s docstring
    lists exactly what is promised; the backend's methods are not.
- `multiverse.plugins.testing.plugin_module(directory, name)` (1.9) imports a
  plugin's module as mv2 does, with no universe and whether or not the plugin is
  on anywhere: for test modules that import the plugin's code at module level.
- `PluginTestCase` loads plugins afresh for each test, so a module of a
  previous test's load is gone. Keep a plugin's relative imports at module
  level: one inside a function (`from .engine import x`) fails when an old
  module object runs it after the reset.
- Claude directories are tested with `claude plugin test DIR`, like the mod.
- The bundled plugins are tested this way in mv2's own suite, so the helpers
  are kept working.

## Phases

The mv2 side comes first and is finished before any plugin is moved or written
against it. Compaction and the remote control come last.

| Phase | Lands | Behaviour change |
| --- | --- | --- |
| **1. Foundation** | `multiverse.plugins`: discovery, manifest, `plugins.json`, `plugin-settings/`, trust, loader, failure boundaries. `api` v1 client and utilities. `mv2 plugins` list/info/enable/disable/settings/check/trust. Extension points for CLI, catalog, bootstrap, doctor, dashboard columns, panels, inspector, theme, keys, screens and settings, plus the Plugins screen. `testing` helpers. The `hello-column` example | None without plugins. `cli.py`'s "no plugin discovery" note goes. |
| **2. Harness side** | Launch contributions, Claude and Pi plugin directories with version gating, the Codex contract (allowlisted config, instructions, MCP servers on spawn, with the bridge's `thread/start` transform), `runtime.json`'s `plugins` record, capabilities and usage points, status-item files read by the mod and the tmux bar, typeahead as a bundled plugin | None visible. Typeahead loads as before. |
| **3. Events and host** | The event journal with segments, retention and emitters. The host supervisor with per-plugin workers. Services. The `cost-item` and `notifier` examples | A journal under `state/events/`, only in universes with a plugin that handles events, pruned to 7 days and 32 MiB by default. |
| **4. Distribution** | Checkout plugins (`plugins/*` submodules), Python entry points, `mv2 plugins install`, `mv2 update` updating initialized submodules, `mv2 plugins migrate`, `mv2 plugins new` | None |
| **5. Compaction** | Steps 2 to 8 of [Moving compaction](#moving-compaction-into-a-plugin): the transition period for running Claude agents, the settings conversion, and its own repository as the `plugins/compaction` submodule. Done. | The same behaviour from `plugins.json` and `plugin-settings/compaction.json`, converted on first use. The transition pieces are gone (79c8761). |
| **6. Remote control** | Its own repository as the `plugins/remote` submodule, under [its rules](#what-the-remote-control-must-do) | None until enabled |

Step 1 of moving compaction (extension points, each with a core user) is the
same work as phases 1 to 3. So by phase 5 the API is complete, and compaction
tests it rather than shaping it.

### Other agents' areas this touches

| Area | Owner | What changes |
| --- | --- | --- |
| `compaction/`, its mod hooks and screens | `compaction` | Moves into the plugin (it offered to move its parts) |
| Claude adapter, mod, typeahead | `claude-support` | Launch contributions and version gating in `_with_bridge`; status-item files in the mod; typeahead as a plugin |
| `runtime/`, `tools/`, `universe/` emitters | `lifecycle` | Event emitters at the chokepoints; plugin commands in `cli.py`; doctor checks |
| `ui/` | `claude-tui` | Registries opened to plugins, keys, the plugin error count in the header |
| tmux bar | `codex-ui` | Status-item files in `terminal/statusline.py` |
| `tools/update.py`, `refresh` | `lifecycle` | Updating initialized submodules; refresh offering `mv2 plugins migrate` |
| Codex side of harness extensions | `plugins-codex` | What a plugin may contribute to Codex launches, with evidence from Codex itself |
| README, template prompts | `mv2-lead` | `plugins.json` and `plugin-settings/` in templates, docs |

## Decisions

Decided by the user on 2026-10-11:

- **API, harness extensions, status lines:** as designed.
- **Events:** the journal as designed, cleaned up periodically
  ([Keeping the journal small](#keeping-the-journal-small)).
- **Enabling and settings:** `plugins.json` is the only on/off switch. Each
  plugin has its own settings file in a fixed layout ([Settings](#settings)),
  and compaction moves from `compaction.json` to it with a migration.
- **Separate repositories:** compaction and the remote control are versioned in
  their own private repositories under `GoatedGoaterestGoat`, included as
  optional submodules: `GOAT` (compaction, keeping its Git history) and
  `mv2-remote-control`. Both are on GitHub, private, with `Silicon23` as a
  collaborator. Both are submodules, `plugins/remote` (0b843bc) and
  `plugins/compaction` (7721049). The user chose to restart the Claude agents
  after compaction's switch rather than keep a transition copy of its Claude
  directory.
- **Codex:** a Codex agent, `plugins-codex`, owns the Codex side of harness
  extensions. Its contract is in [Codex](#codex).
- **Universe plugins:** allowed, loaded only once trusted at their exact
  content.
- **Order:** the whole mv2 side first; compaction and the remote control last
  ([Phases](#phases)).
- **Permissions:** declared and enforced at the API, with no sandbox.
- **API stability:** 1.x is promised stable from 1.8, as of compaction's step 7
  (4f6701d), where compaction uses only the API. A plugin written against 1.8
  keeps working on every newer 1.x ([Versioning](#versioning)).
- **Per-subverse dashboard:** the per-subverse UI (plugin panels in a
  subverse's portal) is `claude-tui`'s to design; it asks for any API it
  needs.
- **The host:** a long-running host, as the general place for any plugin's
  event handlers and services, not shaped by one plugin
  ([The plugin host](#the-plugin-host-where-long-running-plugin-code-lives)).
  The dashboard often runs unattended for hours, but it cannot be the host:
  agents keep working when it is closed, it may be showing another universe,
  and plugin code must not stall its screen. It starts the host instead when a
  plugin needs one.

Decided by the user on 2026-10-13:

- **No plugin hotkeys.** One core key, `P`, opens the plugin settings screen
  for the selected universe or subverse: every plugin with its on/off state,
  errors and what it provides there. Configure opens that plugin's settings,
  as declared fields or its own screen (`mv.dashboard.settings`). Plugin
  actions that are not settings go in the command palette under the plugin's
  name (`mv.dashboard.action`), never on a key, and `c` becomes free.
  `mv.dashboard.key` and `mv.settings.screen` were removed in API 1.6, once
  compaction had moved to `mv.dashboard.settings(screen=...)`.
- **Per-subverse dashboard** (`claude-tui`'s design):
  - Palette entries and screens are built once from the opened root, and say
    "<plugin> is off in <scope>" where it is off.
  - Panels are data-driven, rendered in the snapshot worker under a time
    budget and refreshed with the fleet; the class form stays for interactive
    panels.
  - Auto panels show after a view's own widgets where the plugin is on, with
    an opt-out through `"plugin_panels"`.
  - `listing()["provides"]` says on the `P` screen what each plugin adds there.
