# Making plugins

A plugin is a directory with a `plugin.json` and, usually, Python code. Its `register(mv)` function says what the plugin adds. The code imports only `multiverse.plugins.api`, plus `multiverse.plugins.ui` for a settings screen of its own.

To install plugins and turn them on, see [plugins](../use/plugins.md). The full design, with its reasons, is in [design/plugins.md](../design/plugins.md).

## Quickstart

```sh
mv2 plugins new my-plugin
```

This creates `my-plugin/` with `plugin.json`, `plugin.py` and `tests/test_my_plugin.py`. The manifest lists each extension point the plugin uses in `extends`:

```json
{"schema": 1, "name": "my-plugin", "version": "0.1.0", "description": "", "api": "1.9",
 "module": "plugin", "extends": ["dashboard.column"], "permissions": []}
```

`plugin.py` registers one column:

```python
def shout(row):
    """An example column value: the agent's name in capitals. Column functions must be quick."""
    return str(row.get("name", "")).upper()


def register(mv):
    # Each call below needs its extension point in plugin.json "extends".
    mv.dashboard.column("shout", "Shout", 12, shout)
```

Try it in a scratch universe, and run its tests with the Python that runs mv2 (in a checkout, `.venv/bin/python`):

```sh
export MV2_PLUGIN_PATH="$PWD/my-plugin"
mv2 init ~/scratch && cd ~/scratch
mv2 plugins check "$MV2_PLUGIN_PATH"     # my-plugin: ready.
mv2 plugins enable my-plugin
python -m unittest discover -s "$MV2_PLUGIN_PATH/tests"
```

To see the column, add `"shout"` to `columns` in `.multiverse/views/default.json` and run `mv2`. `mv2 plugins check DIR` checks the manifest, API version, command names and settings files, then imports the plugin and runs `register`. It exits 1 when the plugin can't be used.

`MV2_PLUGIN_PATH` reaches the commands and dashboard you start from that shell. Agents never get it, and the plugin host may not. For a plugin with harness code, event handlers or a service, link it into your personal folder instead: `mkdir -p ~/.multiverse/plugins && ln -s "$PWD/my-plugin" ~/.multiverse/plugins/`.

| `plugin.json` field | Meaning |
| --- | --- |
| `schema` | `1`. |
| `name` | Lowercase letters, digits and single hyphens, starting with a letter, at most 40 characters. It can't be a name mv2 keeps (`state`, `events`, `launches`, …), and `mv2 plugins new` also refuses core command names. The name is used for the plugin's state, events and status-line items. |
| `version`, `description` | The plugin's own version, recorded at each launch it adds to, and one line about it. `mv2 plugins` shows both. |
| `api` | The plugin API it needs, `"MAJOR.MINOR"`; see [versions](#versions). |
| `module` | The module or package in the plugin directory whose `register(mv)` runs. Leave it out for a plugin with no Python. |
| `default` | `"on"` or `"off"` (the default). Only a bundled plugin's `"on"` counts. |
| `extends` | The extension points it uses. Registering for one it doesn't list raises `api.Error`. |
| `permissions` | What it asks to do; see [permissions](#permissions). |
| `commands` | `{"NAME": "help text"}` for each `mv2 NAME` it registers. Needs `cli`. |
| `statusline` | `{"ITEM": {"claude": true, "tmux": false}}`: its status-line items, and whether each shows by default on Claude's line and the tmux bar. Needs `statusline`. |
| `events` | Event types it subscribes to: `"agent.status"`, `"message.*"`, `"*"`. Needs `events`. |
| `harnesses` | Harness extensions that need no code; see [launch additions](#launch-additions). |
| `settings` | `{"version", "schema", "defaults", "legacy"}`; see [settings](#settings). |

`register(mv)` runs once in each process that loads the plugin: a command, the dashboard, a host worker or a helper (`mv.process` is `cli`, `dashboard`, `host` or `helper`). It only declares things. mv2 calls what it registered later, and guards each call.

Keep `mv` in a module variable (`global mv`) for code that mv2 calls without it, such as a command's `run`. Name Textual screens as `"module:Class"`, so the command line never imports Textual.

## Extension points

A method that takes a function also works as a decorator: `@mv.events.on("agent.status")`.

| `extends` | Call | What it adds |
| --- | --- | --- |
| `cli` | `mv.cli.command("hello", configure, run, cli_run=None)` | `mv2 hello`. `configure(parser)` adds options. `run(root, **options)` returns a record, and a record with `"ok": false` exits 1. `cli_run` takes the same arguments and returns text for people |
| `catalog` | No call. Ship `skills/NAME/`, `commands/NAME/` or `workflows/NAME/` laid out as in a universe | Entries for `mv2 catalog`. The universe's own entry of the same name wins, but `mv2 command NAME@my-plugin` always reaches the plugin's |
| `bootstrap` | `mv.bootstrap.line("Run mv2 hello NAME to greet an agent.")` | A line in every agent's bootstrap: at most 2 lines and 300 characters per plugin |
| `doctor` | `mv.doctor.check(fn)`: `fn(universe)` returns a list of sentences | Problems for `mv2 doctor`, prefixed with the plugin's name |
| `dashboard.column` | `mv.dashboard.column("mood", "Mood", 6, value, compact=False, flexible=False, group=None)` | A column a view names as `mood` or `my-plugin:mood`. `value(row)` gets a copy of the agent's row and returns text, or `None` for `—`. `group(row)` fills subverse rows |
| `dashboard.panel` | `mv.dashboard.panel("greetings", render, title=None, auto=False)` | A widget type for a view's `widgets`. `render(context)` returns `api.Markdown(text)`, `api.Table(columns, rows)`, `api.Artifacts(items)` or a list of them. `auto=True` also shows it wherever the plugin is on, unless the view's `plugin_panels` (`"auto"`, `"none"` or a list of types) leaves it out |
| `dashboard.inspector` | `mv.dashboard.inspector(fn)`: `fn(row)` returns a line or `None` | A line in the agent inspector |
| `dashboard.action` | `mv.dashboard.action("Say hello", fn)`: `fn(context)` may return a status line | A palette entry, `my-plugin: Say hello`. `context.row` is a copy of the selected row or `None`, and `context.scope` is the selected universe's path |
| `dashboard.screen` | `mv.dashboard.screen("Board", "screens:Board")` | A Textual screen opened from the palette, built as `Board(root=PATH, backend=...)` |
| `dashboard.settings` | `mv.dashboard.settings(fields=[api.Field("greeting", "Greeting")])` or `screen="screens:Settings"` | What Configure opens on `P`; see [settings](#settings) |
| `dashboard.theme` | `mv.dashboard.theme({"BLUE": "#89b4fa"})` | Colors by their `ui/theme.py` names, used where `plugins.json` has `"theme": "my-plugin"` |
| `statusline` | `mv.statusline.write(agent, "mood", "calm")` | A declared item's text for one agent (an `Agent` or its directory). The text is one line of up to 200 characters, and `""` removes it. People order and hide items in `statusline.json` |
| `launch` | `mv.launch.contribute("claude", fn)`: `fn(info)` returns an `api.Contribution` or `None` | Additions to every launch of that family (`claude`, `codex` or `pi`); see [launch additions](#launch-additions) |
| `capabilities` | `mv.capabilities.add("greeting", fn)`: `fn(session)` returns `api.Capability("supported", "why")` | A capability `mv2 capabilities` reports for each agent: `supported`, `unavailable` or `error`. Core's names are refused |
| `usage` | `mv.usage.adjust(fn)`: `fn(session, usage)` returns the usage record | A change to what `mv2 get-usage` and the dashboard report, after the harness measured it |
| `events` | `mv.events.on("agent.status", fn)`: `fn(event)` | A handler in the plugin host; see [events and the host](#events-and-the-host) |
| `service` | `mv.service(start, stop=None)` | Long-running work in the plugin host |
| `helper` | `mv.helper.action("ping", fn)`: `fn(arguments)` returns an exit code | An action that harness code runs as `"$MV2_PYTHON" -m multiverse.plugins run my-plugin ping ARGS`. `mv.helper.command("ping", *args)` gives that argv |

### Launch additions

```python
@mv.launch.contribute("claude")
def launch(info):
    if not info.bridge:
        return None
    return api.Contribution(environment={"MV2_MY_PLUGIN": "1"}, mode="with my-plugin")
```

- `info` is an `api.LaunchInfo` with `universe`, `agent_id`, `agent_name`, `mode` (`spawn` or `resume`), `family`, `harness`, `version` (the harness's `(major, minor, patch)`, or `None`) and `bridge` (whether the launch has the live connection).
- `api.Contribution` has these fields:
  - `environment`.
  - `claude_settings`, merged into Claude Code's `--settings` (objects merge, lists such as hooks append). Only a connected Claude launch takes it.
  - `problem`, shown as the launch's fallback reason.
  - `mode`, appended to the launch's native mode.
- A plugin may set `MV2_<NAME>` and `MV2_<NAME>_*` (its name upper-cased, hyphens as underscores). It may also set any variable outside `MV2_` except `HOME`, `PATH`, `CODEX_HOME`, `CODEX_SQLITE_HOME`, `CLAUDE_CONFIG_DIR`, `PI_CODING_AGENT_DIR`, `CODEX_THREAD_ID` and `CLAUDECODE`. A plugin that sets a variable the harness or another plugin already set is left out of that launch.
- Additions are read at launch, so a running agent keeps what it started with.

`harnesses` in the manifest adds files with no code. Each family needs its `harness.FAMILY` permission. Typeahead's entry is `"harnesses": {"claude": [{"dir": "claude", "min_version": "2.1.292", "needs_bridge": true}]}`. Each family's entries hold:

- `claude`: a Claude Code plugin directory (`dir`, with `needs_bridge` defaulting to `true`).
- `pi`: an extension file (`extension`, with `needs_bridge` defaulting to `false`).
- `codex`: allowlisted `config` keys, an `instructions` file and `mcp_servers` (see the [design](../design/plugins.md#codex)).

Every entry takes `min_version` and `max_version`, which are inclusive `"MAJOR.MINOR.PATCH"` bounds. An entry that doesn't fit the harness's version or connection is left out. A plugin with no entry that fits is left out of the launch, contributions included.

Harness code reaches mv2 through `"$MV2_CLI"` and helper actions, with `MV2_PYTHON`, `MV2_AGENT_DIR` and `MV2_UNIVERSE` set. Test a Claude directory with `claude plugin test DIR`.

## The `mv` API

| Call | Gives | Needs |
| --- | --- | --- |
| `mv.universe(path=None)` | A `Universe`: `.path`, `.id`, `.name`, `.parent`, `.subverses()`. With no path, the one this process works in | |
| `universe.agents(recursive=False)`, `universe.agent(name_or_id)` | `Agent`s, as the listing shows them | `agents.read` |
| `universe.spawn(name, harness="codex", task=None, model=None, wait=False)` | The new `Agent` | `agents.control`, `agents.read` |
| `universe.catalog()`, `universe.command(name, arguments="")`, `universe.refresh(apply=False)` | Catalog entries; a prompt's text; the template refresh plan | `universe.write` to apply |
| `agent.id`, `.name`, `.harness`, `.live`, `.status`, `.phase`, `.summary`, `.task`, `.universe`, `.directory`, `.record` | The agent as it was read; `agent.reload()` reads it again | `agents.read` to reload |
| `agent.terminal(lines=30)`, `agent.usage()`, `agent.capabilities()`, `universe.doctor()` | The terminal's last lines; usage, capability and doctor results | `agents.read` |
| `agent.resume(wait=False)`, `agent.kill()` | | `agents.control` |
| `agent.send(text, delivery="steer")` | `SendResult(ok, delivery, state, reason, record)`; the agent reads `Message from plugin NAME` | `messages.send` |
| `agent.ask(question)` | The answer to a side question that never enters the conversation | `messages.ask` |
| `agent.inbox()`, `agent.history(last=20)` | Messages as `{at, from, text, delivery}`; `inbox()` leaves them unread | `messages.read` |
| `mv.agent_at(directory)`, `mv.current_agent()` | The agent of a `state/agents/ID` directory; the agent this process runs for, or `None` | `agents.read` |

These need no permission:

- `agent.session()`, the `SessionInfo` that capability and usage functions get.
- `mv.harness_info(universe, name)`, a harness's `family`, `executable`, `unset_env`, `bridge`, `settings` and `home`.
- `mv.settings` ([settings](#settings)).
- `mv.state_dir(universe=None)` and `mv.agent_state_dir(agent)`, which give `state/plugins/NAME/` and `state/agents/ID/NAME/`.
- `mv.log(text, universe=None)`, which writes a line in `state/plugins/NAME/plugin.log`.
- `api.files` (`read_json`, atomic `write_json`, `lock`, `now`) and `api.text` (`clean`, `tokens`).
- `mv.name`, `mv.version`, `mv.directory` and `mv.can(permission)`.

A call that can't be done raises `api.Error` with a readable message.

### Permissions

| Permission | Checked | Where |
| --- | --- | --- |
| `agents.read` | By the API | `agents`, `agent`, `reload`, `terminal`, `usage`, `capabilities`, `doctor`, `agent_at`, `current_agent`; without it a panel's `context.agents` is empty |
| `agents.control` | By the API | `spawn`, `resume`, `kill` |
| `messages.send`, `messages.ask`, `messages.read` | By the API | `send`; `ask`; `inbox` and `history` |
| `universe.write` | By the API | `refresh(apply=True)` |
| `harness.claude`, `harness.codex`, `harness.pi` | By the manifest | A `harnesses` entry for that family; `mv.launch.contribute` doesn't check it |
| `process.spawn` | By the manifest | A Codex entry's local MCP server; otherwise declared only |
| `network.listen`, `models.HARNESS` | Declared only | Listening for connections; spending that harness's model tokens (`models.claude`) |

A checked call that the manifest didn't ask for raises `api.Error`. Declared-only permissions tell the person turning the plugin on what it does, and a reviewer can hold the plugin to them. None of this is a sandbox. A plugin's code runs with the user's permissions.

### Versions

mv2 loads a plugin when the major version of its `api` is mv2's (`api.VERSION`) and its minor version is no higher. Otherwise the plugin is `incompatible`, and the reason says whether to update mv2 or the plugin. `mv2 plugins new` writes this mv2's version.

A minor version only adds things. Changing or removing anything needs a new major version. That rule covers the public names of `multiverse.plugins.api`, `multiverse.plugins.ui` and `multiverse.plugins.testing`, what `multiverse.plugins.dashboard_testing` documents, the manifest, `plugins.json`, settings files, status-item files, the event record and `python -m multiverse.plugins run`. It doesn't cover names starting with `_`, a result's `record`, or anything else in `multiverse`.

## Settings

A manifest's `"settings": {"version": 1, "schema": "settings.schema.json", "defaults": "defaults/settings.json"}` describes its settings file. `defaults` is what `mv2 plugins enable` writes to `plugin-settings/NAME.json` when the universe has none. The defaults file holds `"schema"` equal to `version`.

`schema` is a JSON Schema for the rest of the file, using only `type`, `enum`, `const`, `properties`, `required`, `additionalProperties`, `items`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `minLength`, `maxLength`, `pattern`, `minItems`, `maxItems`, `title`, `description` and `default`. A schema with any other keyword is refused, so no check you rely on is skipped.

```python
values, problem = mv.settings.read(universe)   # {} and the reason when they can't be used
mv.settings.edit({"quiet_seconds": 60, "notify.done": api.DELETE}, universe)

@mv.settings.check
def check(settings):                            # or check(settings, universe_directory)
    if settings.get("quiet_seconds", 0) > 3600:
        raise ValueError("quiet_seconds is at most 3600")
```

- `read` gives the file as written, or the defaults while there is no file. Keys a person removed aren't filled in, so merge your own defaults, as notifier does.
- `edit` changes only the named keys (dotted paths). It writes nothing unless the whole result passes the schema and your check.
- Settings that fail the schema or your check make the plugin act as off in that universe. Its command still runs, after a line saying why.
- Configure on `P` builds fields from the schema for strings, numbers, booleans and enums. `mv.dashboard.settings(fields=[...])` chooses and labels them with `api.Field(path, label, kind="text", options=(), placeholder="", help="")`, where `kind` is `text`, `int`, `float`, `bool` or `choice`.
- `screen="screens:Settings"` names your own screen, built on `multiverse.plugins.ui.FieldScreen`. That module's docstring lists what it promises.

```python
@mv.settings.migrate
def upgrade(data, source):
    if source == "legacy":                      # the file settings.legacy names
        return {"schema": 2, "greeting": data.get("text", "hello")}, data.get("enabled")
    return {**data, "schema": 2}                # source is the older settings version
```

- When the shape changes, raise `settings.version` and register `migrate`. A file with an older `schema` can't be used until `mv2 plugins migrate --apply` converts it.
- `settings.legacy` names a file at a universe's root that the plugin used before it was a plugin, such as compaction's `compaction.json`. It is converted before the next `mv2` command in that universe (`init`, `update` and `plugins` excepted) or helper action, or by `mv2 plugins migrate --apply`. Returning `(settings, on)` also sets the plugin's switch in `plugins.json`.
- A converted legacy file goes to the universe's trash. A conversion that fails leaves every file as it was.

## Events and the host

```python
# plugin.json: "extends": ["events"], "events": ["agent.status"]
@mv.events.on("agent.status")
def waiting(event):
    if event.data.get("to") == "waiting" and event.age < 300:
        mv.log(f"{event.agent['name']} needs you", event.universe)
```

A handler runs in the plugin host, in the plugin's own worker process, for every universe in the tree where the plugin is on. An `api.Event` has `type`, `seq`, `at`, `universe`, `agent` (`{"id", "name"}` or `None`), `launch`, `data` and `age`, the seconds since it happened.

Core records `agent.created`, `agent.launched`, `agent.ready`, `agent.status` (`data`: `from`, `to`), `agent.exited` (`state`, `exit_code`, `reason`), `agent.killed`, `agent.reported` (`phase`, `summary`), `agent.moved`, `agent.removed`, `message.saved`, `message.delivered`, `message.failed`, `message.read`, `universe.created` and `refresh.applied`. The [design](../design/plugins.md#seeing-lifecycle-events-from-any-process) lists each one's `data`. Message text is never recorded.

`mv.events.emit("KIND", data, universe=None, agent=None)` records `my-plugin.KIND` for any plugin that subscribes. A handler whose events were pruned before it read them gets one `events.gap` event, with `missed`, `from` and `to`.

- **Recording.** A universe records a type only while a plugin that is on there subscribes to it. Recording never holds up what it reports. It waits at most 0.2 s for the journal, and an event it can't write is dropped and logged to `state/plugins/events.log`. Events are at most 16 KiB, and larger `data` becomes `{"truncated": true, "bytes": N}`.
- **At least once, in order.** The plugin's cursor, `state/plugins/NAME/events.cursor`, moves past an event once every matching handler finished it, raised (recorded, not retried) or was given up on. After a host restart, an event can arrive again. Make handlers idempotent, and skip old events by `event.age`.
- **Time.** A handler has 60 s per event. A handler that runs out of time holds up only itself, and its siblings go on, however far ahead they get. A call counts one try when it runs out of time, and one more for every further 60 s it keeps running. After 3 tries, the handler is given up on for that event, and its next events get calls of their own. `mv2 plugins`, `mv2 doctor` and `P` show each give-up for a day.
- **Hangs.** A worker left with two calls that never returned restarts itself, and the stuck calls' child processes end with it. After 3 such restarts, however far apart, the plugin is left stopped. Each handler's progress is kept in `state/plugins/NAME/handlers.json`, so a restart never repeats an event to a handler that finished it.
- **Start and retention.** Turning a plugin on starts its cursor at the journal's end. A journal keeps 7 days and 32 MiB by default (`"events": {"retain_days": 7, "max_mb": 32}` in `plugins.json`). `mv2 plugins events` shows its size and cursors, and `--prune` prunes it now.

```python
def start(context):            # api.ServiceContext: universes, stopping, log
    while not context.stopping.wait(5):
        poll(context.universes)

mv.service(start)
```

A service's `start` runs in a thread of the plugin's worker until it returns or `context.stopping` is set. When you give `stop()`, it runs as the host stops the service. Turning the plugin on or off in another universe restarts the service with the new `universes`.

## Testing

```python
from pathlib import Path
import unittest

from multiverse.plugins.testing import PluginTestCase


class MyPluginTest(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    plugin = Path(__file__).resolve().parents[1]

    def test_column(self):
        column = self.loaded().registrations.columns["shout"]
        self.assertEqual(column.value({"name": "worker"}), "WORKER")

    async def test_the_dashboard_shows_it(self):
        self.agent("worker")
        (self.root / "views" / "default.json").write_text('{"columns": ["name", "shout"]}')
        async with self.dashboard() as d:
            headers, rows = d.table()
            self.assertIn("Shout", headers)
            self.assertIn("WORKER", rows[-1])
```

Each test gets:

- a temporary universe from the base template (`self.root`), with the plugin found and on;
- a temporary home, `CLAUDE_CONFIG_DIR` and `bin/` first on `PATH`;
- no inherited agent identity.

Nothing starts an agent, tmux, a harness or the plugin host. A test sees only its own plugins unless the class sets `bundled = True`.

| Helper | Does |
| --- | --- |
| `loaded()`, `module(name)` | The plugin, loaded and registered. `registrations` holds what `register` added (`commands`, `columns`, `data_panels`, `actions`, `launch`, `handlers`, `helpers`, …), so you can call it directly. `module(name)` is one of its modules, as mv2 loaded it |
| `agent(name, harness="claude", native_id=None, runtime=None, **properties)`, `claude_transcript(native_id, records)`, `executable(name, script)` | An agent's records, written without starting anything (returns its directory); a Claude transcript where Claude keeps it; a fake program on `PATH` |
| `write_plugin(name, manifest, files)`, `enable(name, on=True)`, `subverse(name)`, `entry(name)` | Other plugins and scopes |
| `dashboard(size=(120, 40))` | The real dashboard on `self.root`, in an `IsolatedAsyncioTestCase`. Drive it with `press`, `until`, `refresh`, `select`, `enter`, `back`, `open_plugins()`, `open_settings(name)`, `run_action(title)` and `close_screen()`. Read `table()`, `local_view()`, `palette()`, `status` and `header`. `multiverse.plugins.dashboard_testing` lists what is promised |

`multiverse.plugins.testing.plugin_module(directory, name)` imports a plugin's module as mv2 does, for a test module that imports it at module level. Plugins load afresh for each test, so keep a plugin's relative imports at module level.

## Shipping

| Where | How |
| --- | --- |
| Personal | `~/.multiverse/plugins/NAME/` |
| A universe | `<universe>/plugins/NAME/`, such as a repository's `.multiverse/plugins/` or a template's `plugins/`. Each person runs `mv2 plugins trust NAME`, and runs it again after every change |
| Python package | An entry point in the `multiverse.plugins` group, such as `my-plugin = "mv2_my_plugin"` under `[project.entry-points."multiverse.plugins"]` in `pyproject.toml`. It names a top-level package whose directory holds `plugin.json`. mv2 finds it without importing it |
| mv2 checkout | A Git submodule at `plugins/NAME` of the mv2 repository. `mv2 plugins install NAME` initializes it and `mv2 update` moves it |

Don't declare mv2 as a dependency, so installing the plugin never replaces the mv2 someone runs. The `pyproject.toml` files of `plugins/compaction` and `plugins/remote` are full examples.

## Failure rules

| Plugin code | Time limit | When it raises or runs out of time |
| --- | --- | --- |
| Import and `register(mv)` | 10 s | Past 10 s, the plugin is `failed` for now, and its load goes on: the result is used when it ends. Later processes wait for it at most 0.5 s, and a launch leaves it out while it still loads. A load that raises, or a registration the API refuses, leaves it `failed` until its files change or another process tries it |
| A column | 2 s per refresh, for all its rows | The cell shows `—`, or `…` while a slow pass still runs |
| A panel; inspector lines | 2 s; 2 s each and 4 s together | One line saying why; `unavailable` |
| A palette action | 30 s | A status line saying it failed |
| Launch, capability and usage functions | 2 s | The plugin is left out of that launch with the reason; the capability reads `error`; usage stays as measured |
| An event handler | 60 s per event | Recorded and passed. After 3 tries, counted as above, the event is given up on for that handler |
| A command | None | `mv2: plugin NAME: …` and exit 1. `ValueError`, `OSError`, `KeyError` and `RuntimeError` print `mv2: MESSAGE`, as built-in commands do |
| A helper action | None | `mv2 plugin NAME ACTION: …` and exit 1; exit code 2 is left to the action |
| Doctor and settings checks | None | A doctor problem; settings that can't be used |
| A service | None | Its worker ends; the host restarts it at most 3 times in 5 minutes, then leaves it stopped |

- Every failure is recorded against the plugin in two places. `state/plugins/NAME/errors.log` gets it with its traceback (at most one traceback per call site a minute, rotated at 1 MiB), and the dashboard's error count includes it.
- Python can't stop a thread, so a call that runs out of time keeps running. mv2 doesn't start the same call again until it returns.
- Outside its own commands and helper actions, a plugin can't end the process, because `SystemExit` is recorded like any error. Launches, sends, `mv2 list` and the dashboard go on without it.
