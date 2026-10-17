# Plugins

Plugins add features to mv2 without changing mv2 itself. Each universe and subverse turns them on for itself.

```sh
mv2 plugins                    # every plugin this universe sees, on or off, and any problem
mv2 plugins info compaction    # what it may do, its command and its settings file
mv2 plugins enable compaction  # turn it on here
```

```text
compaction  off          checkout  1.0.0     Tree compaction: per-turn summary trees in place of a harness's own summary
remote      off          checkout  0.1.0     Remote control: list agents and send to them from outside, over a local, token-checked endpoint
typeahead   on           bundled   1.0.0     Suggests /multiverse subcommands, catalog entries and agent names while you type in Claude Code
```

## What plugins add

| Kind | Example |
| --- | --- |
| `mv2` commands | `mv2 compaction AGENT` sets an agent's compaction; `mv2 remote` shows the remote control's address and token |
| Launch additions for Claude Code, Codex and Pi | typeahead loads a Claude Code plugin into every connected Claude session; compaction adds its hooks |
| Status-line items, on Claude's line and the tmux bar | compaction's `compaction` (`trees 51/52 · building`), cost-item's `cost` (`$0.42`) |
| Dashboard columns, panels, inspector lines, palette actions and settings screens (under `P`) | hello-column's `uptime` column; compaction's settings screen |
| Background work in the [plugin host](#the-plugin-host) | notifier's desktop notifications; the remote control's endpoint |
| Skills, commands and workflows for `mv2 catalog`, bootstrap lines and `mv2 doctor` checks | compaction's and the remote control's `mv2 doctor` checks |

A column or panel shows only where a view names it. To show a column, add its key (`uptime`, or `hello-column:uptime`) to `columns` in `views/default.json`. See [files](../reference/files.md#viewsdefaultjson).

## Where plugins come from

When two sources have a plugin with the same name, the first in this order wins:

| Source (as `mv2 plugins` shows it) | Where | Trusted |
| --- | --- | --- |
| `path` | Each directory in `MV2_PLUGIN_PATH`: a plugin, or a folder of plugins | Yes |
| `universe` | The universe's own `plugins/NAME/`, such as a repository's `.multiverse/plugins/` | Only once you [trust it](#trust-a-universes-own-plugins) |
| `personal` | `~/.multiverse/plugins/NAME/` | Yes |
| `installed` | Python packages that name a plugin in the `multiverse.plugins` entry point group | Yes |
| `checkout` | `plugins/NAME/` in the mv2 checkout you run: optional Git submodules | Yes |
| `bundled` | Inside mv2 itself | Yes |

An untrusted universe copy never hides a plugin of the same name from another source. A trusted copy does, and `mv2 plugins` and the `P` screen say what it overrides. Installing a plugin doesn't turn it on in any universe.

## Install a checkout plugin

```sh
mv2 plugins install compaction
mv2 plugins enable compaction
```

`install` runs `git submodule update --init` for `plugins/NAME` in the mv2 checkout you run. It therefore needs access to the plugin's repository. Afterwards, `mv2 update` moves the plugin with the checkout; see [updating](updating.md).

If you installed mv2 with pip, `pip install` the plugin's repository instead. mv2 finds it through its entry point. Restart a running dashboard to see it.

## Turn plugins on or off

```sh
mv2 plugins enable notifier
mv2 plugins disable notifier
mv2 -u .multiverse/subverses/ops plugins enable remote   # a subverse
```

`enable` lists what the plugin may do, installs its default settings when the universe has none, and says when the plugin needs the plugin host. In the dashboard, press `P`, select the plugin and press `Space`.

The switch lives in the universe's `plugins.json`, which you can also edit by hand:

```json
{"schema": 1, "enabled": {"compaction": true, "remote": false}}
```

- A plugin that `plugins.json` doesn't name stays at its default. Every plugin is off by default, except typeahead.
- Nothing is inherited. A subverse has its own `plugins.json`, so a plugin that is on in the root stays off in a subverse until you turn it on there.
- Running agents keep the plugins they launched with. Restart an agent (`mv2 kill`, then `mv2 resume`) to give it a plugin you just turned on or off.

## Change a plugin's settings

```sh
mv2 plugins settings notifier                          # its file, whether it can be used, and its values
mv2 plugins settings notifier --set quiet_seconds=60   # VALUE is JSON, or else text
mv2 plugins settings notifier --set notify.done=false --unset max_age_seconds
```

Each plugin keeps its settings for a universe in `plugin-settings/NAME.json`. If the plugin's schema refuses an edit, nothing is written. In the dashboard, press `P`, select the plugin and press `Enter` (Configure). This opens the plugin's own settings screen, or fields built from its schema.

Settings that can't be used never stop a launch. Instead, the plugin acts as off in that universe until they're fixed, and `mv2 doctor` says why.

Some plugins used a settings file before they became plugins, such as `compaction.json`. The next `mv2` command that runs in the universe converts that file to `plugin-settings/`. `mv2 plugins migrate` shows what it would convert, and `--apply` converts it now.

## Trust a universe's own plugins

```sh
mv2 plugins check .multiverse/plugins/my-plugin   # reads its manifest and settings; runs none of its code
mv2 plugins trust my-plugin
mv2 plugins untrust my-plugin
```

A repository can carry plugins in its `.multiverse/plugins/`, and cloning the repository must not run their code. Until you trust such a plugin, it is `untrusted`. mv2 never imports it, and you can't turn it on. Review its files first.

- `trust` records the plugin's path and a SHA-256 of all its files in `~/.multiverse/trusted-plugins.json`, outside every universe. It warns when the plugin ships compiled code you can't read.
- `trust NAME` trusts the universe's own copy, even when a bundled, checkout or personal plugin has the same name. It says which copy it trusted and what that copy now overrides. Use this to run your own fork of a plugin such as compaction.
- Python bytecode and test-tool caches are left out of the hash and never run, so running a plugin's own tests keeps it trusted.
- Any change to the files makes the plugin `changed`, and you must trust it again.
- mv2 runs a copy of exactly the files you trusted, kept in `~/.multiverse/plugin-cache/`.
- Only universe plugins need trust. The other sources hold code that you or mv2 installed.

Trust and permissions are not a sandbox. A plugin runs with your permissions. The permissions that `enable` lists are what the plugin asked for. Anything running as you, including an agent, can run `mv2 plugins trust`.

## The plugin host

```sh
mv2 plugins host          # status
mv2 plugins host start
mv2 plugins host stop
```

The plugin host runs plugins' event handlers and services. This is the work that goes on while no command runs and the dashboard is closed, such as notifier's notifications. Each plugin gets a worker process of its own, so a plugin that hangs or crashes affects only itself.

There is one host per top-level universe, in a tmux session on mv2's server. It runs only while a plugin that needs it is on somewhere in the universe tree.

- If a plugin needs the host and it isn't running, the dashboard starts it. It does this when it opens, and again after you change plugins on `P`. The header shows `host off` while the host is needed but not running. On `P`, press `h` to start or stop it.
- Quitting the dashboard doesn't stop the host. The host stops by itself once no plugin needs it.
- A worker that exits is restarted at most 3 times in 5 minutes. After that it stays stopped, and `mv2 plugins host` and `P` show the reason. Turn the plugin off and on again to give it a fresh start.
- A plugin whose event handlers keep hanging is also left stopped, after 3 such restarts within a day, however long each run takes. `mv2 plugins` and `mv2 doctor` say so too.
- The host's log is `state/plugins/host/host.log` in the top-level universe.

## Safe mode

```sh
MV2_PLUGINS=off mv2
```

With `MV2_PLUGINS=off`, no plugin loads at all, bundled ones included. This applies to that command or dashboard and to the agents it launches. `mv2 plugins` shows the plugins that are on as `safe-mode`. Use safe mode to tell whether a problem comes from a plugin.

Agents don't inherit the variable, so their own `mv2` commands load plugins as usual. See [environment](../reference/environment.md#plugin-variables).

## When a plugin breaks

A broken plugin loses only its own part of mv2:

- Its column shows `—`.
- Its panel shows one line saying why.
- Its inspector line shows `unavailable`.
- Its command fails with `mv2: plugin NAME: …`.

A launch goes on without the plugin and records why in the agent's `runtime.json`. For Claude Code, `mv2 inspect` shows the reason on its `Note:` line. No plugin can stop a launch, a send, `mv2 list` or the dashboard.

| Where | What you see |
| --- | --- |
| `mv2 plugins`, `mv2 plugins info NAME` | Each plugin's state and the reason it isn't working |
| `mv2 doctor` | The same, plus the plugins' own checks |
| Dashboard header and `P` | `N plugin errors` for errors in the open dashboard, with the last ones per plugin on `P` |
| `state/plugins/NAME/errors.log` | Every recorded error, with its traceback |

| State | Meaning | What to do |
| --- | --- | --- |
| `loading` | Its code is still loading | Wait. It shows on the next refresh after it loads |
| `failed` | Its code failed to load, or took longer than 10 s | Run `mv2 plugins info NAME`. A slow load is used once it finishes. Otherwise mv2 tries again once its files change |
| `unusable` | Its settings here can't be used, so it acts as off here | `mv2 plugins settings NAME` |
| `untrusted`, `changed` | A universe plugin that isn't trusted, or has changed since you trusted it | Review it, then `mv2 plugins trust NAME` |
| `incompatible` | It needs another plugin API | Update mv2 or the plugin, as the reason says |
| `invalid` | Its `plugin.json` can't be used | Fix or remove it |
| `missing` | It is on in `plugins.json` but not installed | `mv2 plugins install NAME`, or turn it off |
| `safe-mode` | `MV2_PLUGINS=off` is set | Unset it |

## Plugins in this repo

| Plugin | What it does | Where | Details |
| --- | --- | --- | --- |
| `typeahead` | Suggests `/multiverse` subcommands, catalog entries and agent names in Claude Code's prompt. It is on by default | Bundled | The Claude Code it needs is in [compatibility](../reference/compatibility.md#bridge-feature-minimums) |
| `compaction` | Tree compaction for Claude Code agents | Submodule `plugins/compaction`, repository `GoatedGoaterestGoat/GOAT` | [README](../../plugins/compaction/README.md) |
| `remote` | Remote control: list agents and send them messages from outside mv2, over a local, token-checked endpoint | Submodule `plugins/remote`, repository `GoatedGoaterestGoat/mv2-remote-control` | [README](../../plugins/remote/README.md) |
| `hello-column`, `notifier`, `cost-item` | Examples: a column and an inspector line, desktop notifications, a cost status-line item | `examples/plugins/` | Each `plugin.py`; [making plugins](../develop/plugins.md) |

To try the examples from a checkout:

1. Run `export MV2_PLUGIN_PATH="$PWD/examples/plugins"`.
2. Run `mv2 plugins enable hello-column`.
3. Add `uptime` to a view's `columns`.

To write your own, see [making plugins](../develop/plugins.md).
