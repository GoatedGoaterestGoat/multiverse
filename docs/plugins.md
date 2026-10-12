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
  "api": "1.0",
  "module": "plugin",
  "default": "off",
  "extends": ["cli", "launch", "capabilities", "usage", "doctor", "statusline",
              "dashboard.key", "dashboard.screen"],
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
| `permissions` | What it asks to do (see [Trust and safety](#trust-and-safety)). Shown on enable. The API object it receives has only these capabilities. |
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
  "theme": null,
  "keys": {}
}
```

- `plugins.json` is the **one** switch for whether a plugin is on in this
  universe. A plugin's settings file never has a top-level `enabled` of its
  own. It may still turn parts of itself off (compaction can be off for one
  harness), but turning the plugin on or off is always here.
- A plugin is on where `enabled` says `true`. Where the file says nothing, its
  manifest's `default` applies. Only bundled plugins can default to `on` (today
  only typeahead), so installing a plugin never turns it on anywhere. One
  exception keeps upgrades invisible: a plugin whose pre-plugin settings file
  (its manifest's `settings.legacy`, such as `compaction.json`) is still in a
  universe is on there until `plugins.json` says otherwise. Its settings then
  decide what it does, exactly as that file did before.
- A `plugins.json` naming a plugin that is not installed is not an error for
  launches: they go on without it, and `mv2 plugins` and `doctor` say it is
  missing and how to get it.
- There is no live inheritance, the same rule as settings and views. A
  subverse made from a template gets the template's `plugins.json`. Otherwise
  it has its own file or the defaults.
- `theme` picks one theme plugin. `keys` remaps or frees a dashboard key when
  two plugins, or a plugin and the user, want the same one.

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
mv2 plugins trust my-plugin       # trust a universe plugin at its current content (below)
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
| **Dashboard** | theme, keys, screens, columns, panels, inspector lines | The dashboard loads every plugin that is on anywhere in the tree it shows. A key or screen acting on a scope (`c` on a subverse) works only where that scope enables the plugin, and otherwise says so. A column a view names shows `—` for agents whose universe has the plugin off. The theme and global keys come from the top scope the dashboard opened. |
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
  `plugins.json` where it says nothing yet. `mv2 plugins migrate` shows what
  would change, and `--apply` does it, checking the result first and keeping
  the old file in the universe's trash. Core never guesses a migration.
- **Checked.** The manifest names a JSON Schema. Core validates against it, and
  the plugin may add its own checks (`check(settings)`, like compaction's
  harness builder checks). An unusable file never stops a launch: the plugin
  runs as if off for that universe, and `mv2 plugins`, `doctor` and the
  inspector say why, as `compaction.json` does today.
- **Edited safely.** `mv.settings(universe)` returns the parsed, defaulted
  settings and a `problem`. `mv.settings.edit(universe, changes)` writes the file
  as it was with only the changed keys, the rule `ui/settings.py` already
  follows, after the schema and the plugin's checks accept the whole result.
- **Shown.** A dashboard Plugins screen (command palette, and a key if the user
  binds one) lists plugins with an on/off toggle per scope. Enter opens a
  plugin's settings: its own screen if it registered one (compaction's `c`
  screens), or otherwise a `FieldScreen` generated from its schema, for plain
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
| Plugin helper | One call | `python -m multiverse.plugins run NAME ACTION`, which harness extensions call, as the mod calls `python -m multiverse.compaction install` today. |
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
the command), and `mv2 --help` lists plugin commands from their manifests. The
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
  across host restarts. Handlers must be idempotent. Each event says when it
  happened, so a handler can skip stale ones. The notifier ignores events older
  than five minutes after a restart.
- The dashboard may tail the journal too, to refresh sooner than its 1.5 s poll.
  It need not.

Events are emitted at the few places that already own each change, so no
caller can forget one:

| Event | Emitted by | Data |
| --- | --- | --- |
| `agent.created` | `lifecycle.start`, once a new record is published | harness, model |
| `agent.launched` | `lifecycle.start`, after request.json is written and before the runner is released (so `ready` and `exited` never come first) | mode (`spawn`, `resume` or `wake`), native mode, plugins loaded |
| `agent.ready` | `lifecycle.note_ready`, called by the runner after its startup check and by startup delivery once done; it uses `wait_ready`'s own predicate and a per-launch marker, so it is once per launch | none |
| `agent.status` | `update_runtime`, when `status` changes (after its lock) | from, to |
| `agent.exited` | the runner, once, where the process's end is known | state (`stopped`/`failed`), exit code, reason |
| `agent.killed` | `lifecycle.kill` | native cancellation state |
| `agent.reported` | `agents.report` | phase, summary |
| `agent.moved` | `relocate.move_agent`, in both the source and the destination journal | from, to |
| `agent.removed` | `relocate.remove_agent`, in its universe's journal | trash entry |
| `message.saved` | `mailbox.append_message` | message id, delivery, length, sender's name/agent/plugin (never the text) |
| `message.delivered` | `mailbox.update_message`, once per stage: `accepted`, then `confirmed` when the transcript confirms it | message id, stage |
| `message.failed` | `mailbox.update_message`, on a definite failure only (unknown and submitting are not failures) | message id, reason |
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
  handler whose cursor points into a deleted segment gets one `events.gap` event
  (how many events and what time span it missed), then continues from the
  oldest kept event. Uninstalling a plugin deletes its cursor.
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
- **Stopping.** It exits once no plugin needs it, or on `mv2 plugins host
  stop`, and stops its workers first.
- **Isolation.** The supervisor itself loads no plugin code. Each plugin with
  long-running work gets its own worker process, which loads that plugin
  alone. A plugin that blocks, leaks memory or crashes affects only its own
  worker: it is restarted with backoff, at most 3 times in 5 minutes, then left
  stopped with the reason in `mv2 plugins` and the dashboard header.
- **Events.** The supervisor tails the journals (see above) and hands each
  worker the events its plugin subscribed to, keeping each plugin's cursor.
- Disabling one plugin stops only its worker. Turning it off and on again gives
  a worker that was left stopped a fresh start.

Most plugins never touch the host: a column, a command, a status item, a launch
contribution, a settings screen or a harness extension need none of this.

## The API

`from multiverse.plugins import api`. A plugin imports nothing else from
`multiverse`. Internals (`runtime`, `universe`, `tools`, `harnesses`) can then
keep changing, as they did in PR #16, without breaking plugins.

### Versioning

- `api.VERSION = (1, 0)`. Adding something raises the minor version. Removing
  or changing something raises the major version.
- mv2 supports the current major and the one before it for at least one
  release. A plugin on the old major loads with a deprecation note in
  `mv2 plugins`.
- Everything in `api` has a docstring contract and contract tests in mv2's own
  suite, the way `harnesses/types.py` is the adapter contract today.
- Results are frozen dataclasses with the fields a plugin needs, plus `.record`,
  the tool's full record, for anything the dataclass leaves out. `.record` is
  not covered by the version, and the docs say so.

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
  `state/agents/<id>/compaction/` already fits.
- `mv.agent_at(directory)` and `mv.current_agent()` (from `MV2_AGENT_DIR`, else
  `MV2_AGENT_ID`) find an agent from a helper or a hook.
- `Agent.session()` gives the `SessionInfo` that capability and usage
  contributions receive (native ID, launch, state, native mode, the launch's
  plugins record).
- `mv.harness_info(universe, harness)` gives a harness's family, executable,
  `unset_env`, whether its bridge is on, and its settings.
- `api.files` has `read_json`, `write_json` (atomic), `lock` (exclusive, across
  processes) and `now`.
- `mv.helper.command(name, *args, universe=)` is the argv that runs one of the
  plugin's helper actions, for example to start a detached worker. A helper
  action owns its stdin and stdout, and its return value is the exit code.
- `mv.log(text, universe)` writes to `state/plugins/NAME/plugin.log`.

Dashboard screens run in the dashboard process, where the plugin is loaded:
they call the plugin's own functions directly, off the event loop with
Textual's thread workers, as the dashboard's own screens do.

### Extension points

All are methods on `mv`. Each is listed in `extends`, and each has a fixed rule
for several plugins using it at once.

| Extension point | Registers | When several plugins use it |
| --- | --- | --- |
| `mv.cli.command(name, configure, run, cli_run=None)` | An `mv2 NAME` command with the `tools/` shape: `configure(parser)`, `run(root, **args)` returning a record, an optional `cli_run` for readable text | Core command names are reserved. Two plugins with one name: both refused, and `mv2 plugins` says so. |
| `catalog` (no call: listing it in `extends` is enough) | The plugin's own `skills/`, `commands/` and `workflows/`, laid out as in a universe, found without importing its code | The universe's own entry of the same kind and name shadows a plugin's. `name@plugin` or `kind/name@plugin` always reaches a plugin's entry, even a shadowed one. Two plugins with the same name are both listed, and a bare name is then ambiguous. `/multiverse` and the typeahead see them. |
| `mv.bootstrap.line(text)` | One line appended to the agent bootstrap | Plugin lines follow the base bootstrap in plugin-name order, at most 2 lines and 300 characters per plugin, so the short bootstrap stays short. |
| `mv.doctor.check(fn)` | `fn(universe)` returns problems as sentences | All run. Each is listed under its plugin. |
| `mv.launch.contribute(family, fn)` | `fn(LaunchInfo)` returns an `api.Contribution` or None, within 2 s (see [Harness extensions](#harness-extensions)). The manifest's `harnesses` adds plugin directories and extensions with no code. | All run in plugin-name order. Contributions merge by fixed rules, and the harness's own settings and environment never change. A plugin that fails or conflicts is left out of that launch, with the reason recorded, and the launch goes on. |
| `mv.capabilities.add(name, fn)` | A capability key: `fn(SessionInfo)` returns `api.Capability(state, reason, mechanism)` | Core keys are reserved. One owner per key. |
| `mv.usage.adjust(fn)` | `fn(SessionInfo, usage)` returns usage, for example with the context replaced and marked estimated | Applied in plugin-name order, after the adapter. Compaction is the only user so far. |
| `statusline` in the manifest, and `mv.statusline.write(agent, name, text)` | A status-line item the manifest declares (`{"trees": {"claude": true, "tmux": false}}`), with its text written by whatever process knows it (see [Status lines](#status-lines)) | Each name has one owner, and core's names are refused. The order and on/off come from `statusline.json`, as today. |
| `mv.dashboard.column(key, title, width, value, compact=, flexible=, group=)` | An agent column: `value(row)` for agent rows, `group(row)` for subverse rows | Core keys are reserved. A view can name it `KEY` while that is unique, and `plugin:KEY` always works. A view keeps a plugin column it names even while the plugin is off, and shows `—` for rows whose universe has it off. A short name that later becomes ambiguous shows `—`, and the clash is listed on the Plugins screen. |
| `mv.dashboard.panel(type, panel)` | A view widget type for `views/*.json` | The same naming rule. A plugin panel is code, so it shows only where its plugin is on. Elsewhere it shows "needs plugin X". |
| `mv.dashboard.inspector_fact(fn)` | A function in `AGENT_FACTS` | Core facts first, then plugins in name order. Each fact is wrapped so an error shows as one line. |
| `mv.dashboard.theme(colors)` | A full or partial palette by `theme.py` name | Only the theme `plugins.json` names is applied. |
| `mv.dashboard.key(key, label, action, scope="universe")` | A key binding. The action gets the selected row and scope. | Core keys win. Between plugins, `plugins.json` `keys` decides, and otherwise both are refused with a note in `?` help. |
| `mv.dashboard.screen(name, "module:Class")` | A Textual screen, pushed by a key or the command palette | Namespaced by plugin. |
| `mv.dashboard.settings(name, fields_fn, save_fn)` | A `FieldScreen` over one of the plugin's config files | The same pattern as compaction's `c` screen today. |
| `events` in the manifest, and `mv.events.on(type, fn)` | `fn(api.Event)` run in the plugin's host worker for each event of a type or pattern (`agent.*`) the manifest's `events` subscribes to. The subscription is what makes core record that type at all. | All handlers for a type run, each in its own error boundary, with a 60 s budget. An `events.gap` event says what was pruned before it could be read. |
| `mv.events.emit(kind, data, universe=, agent=)` | A `PLUGIN.kind` event in a universe's journal | Namespaced to the plugin. |
| `mv.service(start, stop=None)` | `start(api.ServiceContext)` runs in a thread of the plugin's worker until it returns or `context.stopping` is set; `stop()` runs when the host stops it | Each plugin has its own worker process. A worker whose service crashes is restarted at most 3 times in 5 minutes. |
| `mv.helper.action(name, fn)` | `fn(arguments)` returns an exit code, for `python -m multiverse.plugins run PLUGIN NAME ARGS…`, which a plugin's harness code runs with `$MV2_PYTHON` | Private to the plugin. |

Every call into a plugin is wrapped. An exception drops that contribution for
that call. It is logged to `state/plugins/NAME/errors.log`, `mv2 plugins` and
`doctor` show it, and the dashboard header counts it (`1 plugin error`). No
plugin error can stop a launch, a send, `list` or the dashboard.

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
    mode: str | None = None                        # e.g. "without compaction", shown in native_mode
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
  unusable or conflicted) is also a problem of the launch, which Claude shows as
  its fallback reason in the inspector and capabilities. A plugin that merely
  does not fit (version, bridge, a plain launch) is recorded only. `mv2 doctor`
  says when a plugin's pre-plugin settings file is still in a universe but the
  plugin is off there or in safe mode.
- **Recording.** What each plugin contributed, and why one was left out, goes in
  `runtime.json` under `plugins`:
  `{"compaction": {"version": "1.0.0", "state": "loaded"}, "typeahead": {"state": "skipped", "reason": "needs Claude Code 2.1.292; this is 2.1.290"}}`.
  The inspector, `mv2 inspect` and `capabilities` read it there. A `problem` is
  also appended to `native_mode.fallback_reason`, so today's "Launched without
  compaction: …" and the `bridge without compaction` mode stay exactly as they
  are.
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
are in [lab-notes/plugins-codex.md](../lab-notes/plugins-codex.md).

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
  OAuth client secrets, only references to the user's environment. A stdio
  server that needs the agent's identity gets it passed explicitly, since a
  shared daemon does not inherit the TUI's environment. This is the only v1 way
  for a plugin to add MCP config, and `config.mcp_servers` is refused.

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
and `src/multiverse/compaction/protocol.py`.

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
  facts it already prints (`python -m multiverse.compaction status`):
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
   `mv2 plugins trust NAME` records the plugin's path and a SHA-256 of its files
   in `~/.multiverse/trusted-plugins.json`, outside every universe. Agents cannot
   reach it through `mv2`, though nothing stops a process with the user's
   permissions from writing it. Any change to the files means trust is needed
   again (like direnv's `allow`). Until then the plugin is listed as
   `untrusted` and never imported.
3. **Permissions are declared and enforced at the API.** The `mv` object a
   plugin receives has only what its manifest asks for. Without
   `messages.send` there is no `send`. This catches mistakes and makes a
   plugin's reach reviewable. It is not a security boundary against a plugin
   that imports `subprocess`, and `mv2 plugins info` says that. The permissions
   are `agents.read`, `agents.control`, `messages.send`, `messages.ask`,
   `messages.read`, `universe.write`, `network.listen`, `process.spawn`,
   `harness.claude`, `harness.pi`, `harness.codex` and `models.<harness>`, the
   last one for a plugin that spends model tokens, as compaction's merges do.
   `mv2 plugins enable` lists them in words ("can send messages to any agent in
   this universe as plugin notifier").
4. **Failures are contained.** As described under [Extension points](#extension-points),
   a failing plugin loses its own contribution and nothing else.
5. **Safe mode.** `MV2_PLUGINS=off` (or `mv2 --no-plugins`) loads no plugin at
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

- **Off unless enabled,** per universe, by the user. `enable` prints what it
  exposes and the address. It runs only in the host.
- **Loopback only.** It binds `127.0.0.1` (or a Unix socket under
  `state/plugins/remote/`, mode 0600). It refuses any other address in v1. Remote
  access is the user's own SSH tunnel (`ssh -L`), as the docs explain.
- **A token for every request.** 32 random bytes from `secrets`, made on enable,
  stored at `state/plugins/remote/token` (0600; `state/` is already
  Git-ignored), sent as `Authorization: Bearer …` and compared in constant time.
  `mv2 remote token --rotate` (the plugin's own command) replaces it. Failed attempts are delayed
  and counted in the log.
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
  text) goes to `state/plugins/remote/audit.jsonl`. Messages arrive as `Message
  from remote`, so agents and people can tell.

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
file to trash. Until a universe is migrated, the plugin counts as on wherever
`compaction.json` exists and `plugins.json` says nothing, and reads its
settings, `enabled` included, from that file. So no universe changes behaviour
before it is migrated, and `doctor` reminds the user to migrate. The base template ships compaction off,
with its builder settings written out in `plugin-settings/compaction.json`. The
development template turns it on in `plugins.json`.

A universe that turns compaction on without the plugin installed (its
submodule not initialized) launches as if it were off: Claude's own `/compact`
works, and `mv2 plugins` and `doctor` say how to install it. That is safe
because the `PreCompact` guard is the plugin's own launch contribution, so it
is absent too.

### What moves where

| Today | Becomes |
| --- | --- |
| `compaction/` (model, config, schedule, store, build, pipeline, protocol, `__main__`) | The plugin's own package, unchanged. `__main__` actions become `mv.helper` actions. |
| `harnesses/claude/compaction/` (transcript, merge, render, images, `ClaudeCompaction`) | The plugin's Claude source. The plugin picks the source by the agent's harness family; `CompactionSource` stays its internal seam. |
| `mod/hooks/compaction.ts` (`session.compact`, `classic.Stop`, `classic.SessionStart`, `session.start`) | The plugin's `claude/` directory, a Claude plugin of its own. `/compact` progress uses its own status slot. |
| The mod's `compaction` status item, read through `compactionLine()` | `status-items/compaction.txt`, written by the plugin, read by the mod like any item |
| Adapter `_with_bridge`: `MV2_COMPACTION=1`, the `PreCompact` guard in `--settings`, `bridge without compaction` | A launch contribution: environment, `claude_settings` with the guard, `problem` and `mode` |
| Adapter `compaction_state`, `compaction_defaults`, `compaction()` | The plugin's own functions over its Claude extension |
| Adapter `_compaction_capability`; `base.py` giving every harness "no tree compaction"; `CAPABILITIES` listing it | `mv.capabilities.add("compaction", …)`, which answers for every harness, with the same texts |
| Adapter `latest_compaction()` and `get_usage`'s estimated context | `mv.usage.adjust`, with the same fields (`estimated`, `estimated_at`, `estimated_from`) |
| `display.py` "estimated after compaction: conversation only" | Generic in core: it shows `estimated_reason` from the adjuster |
| `tools/compaction.py`, `cli.py`'s `compaction` | `mv.cli.command("compaction", …)`, with the same arguments and output |
| `tools/doctor.py` `_compaction` | `mv.doctor.check` |
| `ui/compaction.py`, the four backend methods, `action_compaction`, the `c` binding, the help line | `mv.dashboard.key("c", …)` and `mv.dashboard.screen`. The screens are unchanged, on `FieldScreen`. |
| Templates' `compaction.json` | `plugins.json` and `plugin-settings/compaction.json` (above) |
| `tools/doctor.py`'s check of `compaction.json` | The plugin's `check(settings)`, which core's settings validation runs |
| The `statusline.json` item, the mv2-help skill, the Claude README | The item is unchanged. The skill and README text say compaction is a plugin and how to install it. |
| `tests/test_compaction.py`, `test_claude_compaction.py`, `test_tui_compaction.py`, `mod/tests/compaction.test.ts` | The plugin repository's `tests/`, on `multiverse.plugins.testing` |

Stays in core: the Claude transcript usage rule that a row stored again with
zero usage never replaces its response's usage. It applies to Claude's own
compaction too.

### Steps

Each step keeps the full suite, `test_compaction.py`,
`test_claude_compaction.py` (with `DashboardBackendTest`),
`test_tui_compaction.py` and the mod's `compaction.test.ts` passing, with only
import paths changing in tests:

1. **Extension points first, each with a core user.** Launch contributions,
   capabilities, usage, doctor, CLI commands, dashboard keys and screens, and
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
   - New launches load the plugin's own `claude/` directory and set
     `MV2_COMPACTION_PLUGIN=1`.
   - The mod keeps its compaction hooks for one transition period, active only
     when `MV2_COMPACTION=1` and `MV2_COMPACTION_PLUGIN` is unset, so exactly
     one plugin answers `/compact` in every session, old or new.
   - The mod's hooks and the plugin's are the same code. The plugin's is
     validated in a scratch copy on the oldest running Claude (2.1.290 at the
     time of writing) before it is written to the checkout.
   - Once `mv2 doctor` reports no agent launched before the split (it can tell
     from `runtime.json`'s `plugins` record), the mod's copy is removed.
5. **Settings.** `plugins.json` becomes the switch, `plugin-settings/compaction.json`
   the settings, and `mv2 plugins migrate` converts universes, with the legacy
   file still read until it does (above).
6. **Move the files** into `src/multiverse/plugins/builtin/compaction/` while it
   is still in this repository. Old module paths stay
   as forwarding stubs, as `runner.py` and `statusline.py` did after PR #16:
   `python -m multiverse.compaction install|background|status|worker|guard` is
   called by running mods, by the `PreCompact` command in their settings, and
   by the worker spawn. The stubs go once no agent from before the move is
   left.
7. **Use only `api`.** The plugin stops importing internals: it finds its
   agent through `mv.universe().agent()` and the session through the agent, and
   uses `api.files` for writes and locks. Anything it still needs is added to
   the API, or the plugin keeps a marked exception that is removed before
   API 1.0 is final.
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
- An unusable settings file (or legacy `compaction.json`) never blocks a launch. The launch runs without
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
    compaction/    -> github.com/OWNER/mv2-compaction   (submodule, pinned commit)
    remote/        -> github.com/OWNER/mv2-remote       (submodule, pinned commit)
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
  `pip install git+https://github.com/OWNER/mv2-compaction` works for an mv2
  installed with pip. Both routes load the same code.
- **Updating.** `mv2 update` moves the plugins that are initialized with the
  checkout and never fetches or initializes one that is not. Before the fetch,
  again before the merge, and again before each plugin moves, each one must be
  exactly at the commit mv2 pins, with no tracked changes or conflicts
  (untracked files are fine). It moves with `git submodule update --checkout`,
  one plugin at a time, with nested fetches off, whatever the user's update
  mode is. `--check` shows which plugins would move. A plugin that cannot move
  leaves the rest of the update in place (code and environment) and says how
  to repair it. New plugins are reported as available (`mv2 plugins install
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
  and `claude plugin validate` for Claude directories.
- `multiverse.plugins.testing` gives `unittest` helpers on mv2's own test
  harness:
  - `PluginTestCase` makes a temporary universe with the plugin enabled, an
    isolated tmux socket and fake harness executables. It never touches the
    user's Codex daemon or config, under the same guard as the main suite.
  - `self.emit(event)` delivers an event to the plugin's handlers in process.
  - `self.launch("claude")` returns the merged command a launch would run, with
    no process started.
  - `self.dashboard()` runs the dashboard with `App.run_test()` and the plugin
    loaded, for Pilot tests of its columns, keys and screens.
  - `self.status_item(agent, name)` reads an item as the tmux bar would.
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
| **4. Distribution** | Checkout plugins (`plugins/*` submodules), Python entry points, `mv2 plugins install`, `mv2 update` updating initialized submodules, `mv2 plugins migrate`, per-subverse UI with plugin panels in portals, API 1.0 frozen | None |
| **5. Compaction** | Steps 2 to 8 of [Moving compaction](#moving-compaction-into-a-plugin), with the transition period for running Claude agents, the settings migration, and finally its own repository as the `plugins/compaction` submodule | None until a universe is migrated, and then the same behaviour from `plugins.json`. Running agents keep working, and stubs and the mod's old hooks are removed after they relaunch. |
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
  optional submodules. Their names are not chosen yet.
- **Codex:** a Codex agent, `plugins-codex`, owns the Codex side of harness
  extensions. Its contract is in [Codex](#codex).
- **Universe plugins:** allowed, loaded only once trusted at their exact
  content.
- **Order:** the whole mv2 side first; compaction and the remote control last
  ([Phases](#phases)).
- **Permissions:** declared and enforced at the API, with no sandbox.
- **The host:** a long-running host, as the general place for any plugin's
  event handlers and services, not shaped by one plugin
  ([The plugin host](#the-plugin-host-where-long-running-plugin-code-lives)).
  The dashboard often runs unattended for hours, but it cannot be the host:
  agents keep working when it is closed, it may be showing another universe,
  and plugin code must not stall its screen. It starts the host instead when a
  plugin needs one.

Still open:

1. The two repositories' names.
2. Whether to keep compaction's history in its repository (recommended: yes,
   with `git filter-repo`).
