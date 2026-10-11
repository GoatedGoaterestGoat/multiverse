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
  as a Claude mod of its own, a `--settings` hook, environment, a Pi extension or
  Codex config. It is gated on the harness's version.

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
| **harness extension** | The part of a plugin that runs inside or alongside a harness: a Claude plugin directory (a "mod"), a Pi extension, Codex config. |
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
  only typeahead), so installing a plugin never turns it on anywhere.
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
mv2 plugins migrate               # move pre-plugin settings (compaction.json) to their plugin files
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
  version from its manifest. A plugin that changes the shape provides
  `migrate(old) -> new`, which `mv2 plugins migrate` applies (the old file goes
  to trash). Core never guesses.
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
| Plugin host | Long-lived, optional | Services (the remote control's server) and event handlers. |
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
command is resolved from `argv[1]` before argparse is built, and `mv2 --help`
lists plugin commands from their manifests.

### Seeing lifecycle events from any process

Lifecycle events happen in different processes. A spawn happens in the CLI or
the dashboard, a status change in the mod's `runtime_update` subprocess, an exit
in the runner, a delivery in whichever process sent. So events are **recorded
where they happen and delivered where plugins run**:

- Core appends each event to the universe's journal under `state/events/`. It
  has a sequence number from a lock-protected counter. Appending is best
  effort: a failed append is logged and never fails the operation that caused
  it. A universe writes a journal only while a plugin that is on there has event
  handlers, so a universe that uses none writes nothing.
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
| `agent.created` | `lifecycle._record` (new record) | name, harness, model |
| `agent.launched` | `lifecycle.start` (launch registered) | launch, mode (`spawn`/`resume`/`wake`), native mode, plugins contributed |
| `agent.ready` | `update_runtime`: state becomes `running` with the bridge registered, or the plain launch's startup check passed | launch |
| `agent.status` | `update_runtime`: `status` changes | from, to (`working`/`waiting`/`idle`/`unknown`/`disconnected`) |
| `agent.exited` | the runner, at its final `update_runtime` | launch, state (`stopped`/`failed`), exit code, reason |
| `agent.killed` | `lifecycle.kill` | launch, whether native cancellation was confirmed |
| `agent.reported` | `agents.report` | phase, summary |
| `agent.moved`, `agent.removed` | `relocate`, `trash` | from, to |
| `message.saved` | `mailbox.append_message` | message id, sender, recipient, delivery, length (no text) |
| `message.delivered` | `mailbox.update_message`: accepted or receipt | message id, route, transcript-confirmed |
| `message.failed` | `mailbox.update_message`: definite failure | message id, reason |
| `message.read` | `mailbox.inbox` advancing the cursor | count |
| `universe.created` | `templates.create` | name, template |
| `refresh.applied` | `templates.refresh_apply` | files changed |
| `PLUGIN.*` | `mv.events.emit` from a plugin | anything JSON (namespaced to its name) |

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

### The plugin host

`mv2 plugins host start` runs one host per top-level universe tree in a tmux
session that mv2 owns and tags like an agent pane. Its PID and lock are under
`state/plugins/host/`. `stop`, `status` and the dashboard header (`host ✓`) all
use it. It starts only when a plugin that is on declares a service or event
handler. `mv2 plugins enable` offers to start it, and the dashboard starts it
on open if it is needed and not running. It never needs to run for compaction,
status items or anything else in this document except handlers and services. A
crashing handler is logged and skipped. A crashing service is restarted with
backoff, at most 3 times in 5 minutes, then left stopped with the reason in
`mv2 plugins`.

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

Plugin utilities: `mv.state_dir` (`state/plugins/NAME/` of the universe in
scope), `mv.agent_state_dir(agent)` (`state/agents/<id>/NAME/` by default; a
plugin may claim a different name with the `state` manifest field, which is how
compaction keeps `state/agents/<id>/compaction/`), `api.files.write_json` and
`api.files.lock` (the store's atomic writes and locks), and `mv.log` (to
`state/plugins/NAME/plugin.log`).

### Extension points

All are methods on `mv`. Each is listed in `extends`, and each has a fixed rule
for several plugins using it at once.

| Extension point | Registers | When several plugins use it |
| --- | --- | --- |
| `mv.cli.command(name, configure, run, cli_run=None)` | An `mv2 NAME` command with the `tools/` shape: `configure(parser)`, `run(root, **args)` returning a record, an optional `cli_run` for readable text | Core command names are reserved. Two plugins with one name: both refused, and `mv2 plugins` says so. |
| `mv.catalog.directory(path)` | `skills/`, `commands/`, `workflows/` under the plugin | The universe's own entry of the same name shadows a plugin's. Two plugins' entries are both listed as `kind/name@plugin`. `/multiverse` and the typeahead see them. |
| `mv.bootstrap.line(text)` | One line appended to the agent bootstrap | Plugin lines follow the base bootstrap in plugin-name order, at most 2 lines and 300 characters per plugin, so the short bootstrap stays short. |
| `mv.doctor.check(fn)` | `fn(universe)` returns problems as sentences | All run. Each is listed under its plugin. |
| `mv.launch.contribute(harness_family, fn)` | `fn(LaunchInfo)` returns a `Contribution` (see [Harness extensions](#harness-extensions)) | All run in plugin-name order. Contributions merge by fixed rules. A plugin that fails or conflicts is left out of that launch, with the reason recorded, and the launch goes on. |
| `mv.capabilities.add(name, fn)` | A capability key, `fn(session, info)` returns `api.Capability` | Core keys are reserved. One owner per key. |
| `mv.usage.adjust(fn)` | `fn(session, usage)` returns usage with the context replaced and `estimated_reason` set | Applied in plugin-name order, after the adapter. Compaction is the only user so far. |
| `mv.statusline.item(name, claude=…, tmux=…)` | A status-line item (see [Status lines](#status-lines)) | Each name has one owner. The order and on/off come from `statusline.json`, as today. |
| `mv.dashboard.column(column)` | A `Column` subclass, under `NAME` or `plugin:NAME` | Core keys are reserved. Short names work while they are unique, and the `plugin:name` form always works. |
| `mv.dashboard.panel(type, panel)` | A view widget type for `views/*.json` | The same naming rule. A plugin panel is code, so it shows only where its plugin is on. Elsewhere it shows "needs plugin X". |
| `mv.dashboard.inspector_fact(fn)` | A function in `AGENT_FACTS` | Core facts first, then plugins in name order. Each fact is wrapped so an error shows as one line. |
| `mv.dashboard.theme(colors)` | A full or partial palette by `theme.py` name | Only the theme `plugins.json` names is applied. |
| `mv.dashboard.key(key, label, action, scope="universe")` | A key binding. The action gets the selected row and scope. | Core keys win. Between plugins, `plugins.json` `keys` decides, and otherwise both are refused with a note in `?` help. |
| `mv.dashboard.screen(name, "module:Class")` | A Textual screen, pushed by a key or the command palette | Namespaced by plugin. |
| `mv.dashboard.settings(name, fields_fn, save_fn)` | A `FieldScreen` over one of the plugin's config files | The same pattern as compaction's `c` screen today. |
| `mv.events.on(type, fn)` | A handler run in the host | All handlers for a type run, each in its own error boundary. |
| `mv.events.emit(type, data)` | A `NAME.type` event in the journal | Namespaced. |
| `mv.service(start, stop)` | A long-lived service in the host | Each plugin has its own services. |
| `mv.harness.extension(family, key, factory)` | A harness-specific object that the plugin's own code looks up, such as compaction's `CompactionSource` for Claude | Private to the plugin. Other plugins cannot see it. |
| `mv.helper(action, fn)` | An action for `python -m multiverse.plugins run NAME ACTION` | Private to the plugin. |

Every call into a plugin is wrapped. An exception or a timeout (2 s for
synchronous points such as launch, capabilities, usage, doctor, inspector and
status) drops that contribution for that call. It is logged to
`state/plugins/NAME/errors.log`, `mv2 plugins` and `doctor` show it, and the
dashboard header counts it (`1 plugin error`). No plugin error can stop a
launch, a send, `list` or the dashboard.

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
    codex_config: Mapping[str, str] = {}           # -c key=value
    problem: str | None = None                     # "Launched without compaction: …"
    mode: str | None = None                        # e.g. "without compaction", shown in native_mode
```

- **Merging.** An environment key set by two plugins is a conflict, and both
  are left out of that launch. Core identity variables always win, as
  adapter variables do today. Claude settings deep-merge, with the adapter's
  `crossSessionInbound` first and hook arrays concatenated.
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
- `needs_bridge`: only when the mod is loaded. A plain launch skips it with the
  reason.
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

### Pi and Codex

- **Pi:** `harnesses.pi` entries are extension files, added as `--extension
  PATH` beside mv2's own `extension.ts` when the bridge is on. A Pi extension
  can do what mv2's does, and it reaches mv2 the same way, through the CLI and
  the helper.
- **Codex:** `plugins-codex` is checking what the installed Codex offers
  (config keys, its `notify` program, hooks, MCP, app-server notifications)
  and will replace this paragraph with a contract backed by evidence. For now:
  Codex has no extension mechanism mv2 can load into the TUI.
  Codex-side behaviour is limited to `codex_config` (`-c key=value`), and the
  user's explicit harness `args` still win, as for `check_for_update_on_startup`.
  Events cover the rest: status comes from the app-server through the runner
  and reaches plugins as `agent.status`. Hooks into the relay protocol are not
  part of v1.

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
file to trash. Until a universe is migrated, the plugin reads `compaction.json`
where there is no new file, and an `enabled` there counts while `plugins.json`
says nothing. So no universe changes behaviour before it is migrated, and
`doctor` reminds the user to migrate. The base template ships compaction off,
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
| `harnesses/claude/compaction/` (transcript, merge, render, images, `ClaudeCompaction`) | The plugin's Claude source, registered with `mv.harness.extension("claude", "compaction", …)`. |
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
- **Updating.** `mv2 update` fetches without submodules today, and it must not
  silently move code that running agents load. After its fast-forward it runs
  `git submodule update` for the submodules that are already initialized, never
  `--init`. It refuses, with the reason, when an initialized submodule has local
  changes or commits that the new pin would drop, the same rules it applies to
  the main checkout. `update.py` is `lifecycle`'s, so this is agreed with it.
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

| Phase | Lands | Behaviour change |
| --- | --- | --- |
| **1. Foundation** | `multiverse.plugins` (discovery, manifest, `plugins.json`, trust, loader, failure boundaries), `api` v1 client and utilities, `mv2 plugins` list/info/enable/disable/check/trust, extension points for CLI, catalog, doctor, dashboard columns/panels/inspector/theme/keys/screens/settings, `testing` helpers, `hello-column` | None without plugins. `cli.py`'s "no plugin discovery" note goes. |
| **2. Harness side** | Launch contributions, Claude and Pi plugin directories with version gating, `runtime.json` `plugins` record, capabilities and usage points, status-item files read by the mod and the tmux bar, typeahead as a bundled plugin | None visible. Typeahead loads as before. |
| **3. Compaction** | Steps 2 to 8 above, with the transition period for running Claude agents, the settings migration, and finally its own repository as the `plugins/compaction` submodule | None until a universe is migrated, and then the same behaviour from `plugins.json`. Running agents keep working, and stubs and the mod's old hooks are removed after they relaunch. |
| **4. Events and host** | Event journal with segments and retention, emitters, the plugin host, `notifier`, `cost-item`, and `remote` in its own repository (off by default) | A journal under `state/events/`, only in universes with a plugin that handles events, pruned to 7 days and 32 MiB by default. |
| **5. Distribution** | Entry points, `mv2 plugins new` scaffold, per-subverse UI with plugin panels in portals, API 1.0 frozen | None |

Phases 1 to 3 are the ones that matter for the compaction goal. Phase 4 can run
alongside phase 3, since it touches different code (`universe/`, `runtime/`
emitters and a new host), if the user wants the remote control sooner.

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
  their own repositories, included as optional submodules.
- **Codex:** a Codex agent, `plugins-codex`, owns the Codex side of harness
  extensions.

Still open (recommendation first):

1. **Repositories.** Which GitHub owner (the same as mv2's,
   `GoatedGoaterestGoat`?), which names (`mv2-compaction`, `mv2-remote`), private
   or public, and whether to keep compaction's history (recommended)?
2. **Universe plugins.** Allow `<universe>/plugins/` with per-user trust pinned
   to content (recommended)? Or allow only bundled, checkout, personal and
   installed plugins?
3. **Plugin host.** Run it as a long-lived tmux-hosted process, started on
   demand (recommended, so the remote control and notifier work without the
   dashboard)? Or run handlers only while the dashboard is open?
4. **Order.** Foundation, then harness side, then compaction, then
   events/host/remote (recommended)? Or bring the remote control forward?
5. **Permissions.** Are declared permissions enforced at the API facade, with
   no sandbox, acceptable?
