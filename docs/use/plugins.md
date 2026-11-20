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
| `personal` | `~/.multiverse/plugins/NAME/` | Yes; one that [`mv2 plugins install`](#install-a-plugin) placed only at the content it installed |
| `installed` | Python packages that name a plugin in the `multiverse.plugins` entry point group | Yes |
| `checkout` | `plugins/NAME/` in the mv2 checkout you run: optional Git submodules | Yes |
| `bundled` | Inside mv2 itself | Yes |

An untrusted universe copy never hides a plugin of the same name from another source. A trusted copy does, and `mv2 plugins` and the `P` screen say what it overrides. Installing a plugin doesn't turn it on in any universe.

## Install a plugin

```sh
mv2 plugins install https://github.com/me/notes.git           # its default branch
mv2 plugins install https://github.com/me/notes.git@v1.2      # a tag, branch or commit
mv2 plugins install git@github.com:me/tools.git@v2#plugins/notes   # a folder inside the repository
mv2 plugins install ~/src/notes                               # a folder on this machine
mv2 plugins enable notes
```

`install` takes a git URL (`https://`, `ssh://`, `git@host:path`, `git://` or `file://`), optionally followed by `@REF` and `#PATH`, or a plugin's folder on this machine. It fetches the plugin into a staging folder and shows what it is before anything is placed: its version, where it comes from and at which commit, the access and permissions it asks for, the Python packages it needs, and the files that run when it loads. It installs only when you say yes. Without a terminal to ask on, it shows the same and installs nothing, unless you add `--yes`.

- **Where it goes.** Your personal plugins, `~/.multiverse/plugins/NAME/`, so any universe can turn it on. With `--here`, the current universe's own `plugins/NAME/` instead. Installing turns it on nowhere: `mv2 plugins enable NAME` does that, and asks to install its Python packages.
- **What runs.** Exactly the files you saw. Nothing from the repository runs while it is fetched, and nothing is checked out. mv2 records the source, the commit and a SHA-256 of the files, adds `.mv2-installed.json` to the plugin, and runs it from a copy of exactly those bytes. If its installed files change later, it's listed as changed and doesn't run, until you run `mv2 plugins trust NAME` after reviewing them, or `mv2 plugins update NAME` to put back what the source has.
- **Name clashes.** A plugin of that name from another source, or a folder already at the target that `install` didn't place, refuses the install, naming where the other one is.
- **Who.** Only you: an agent can't install, update or remove a plugin.
- **When it fails** (no network, a ref or folder that isn't there, a folder that isn't a plugin, a symbolic link or a submodule inside it), nothing is installed and nothing is left behind.

`mv2 plugins info NAME` shows where an installed plugin came from and at which commit.

### Update and remove

```sh
mv2 plugins update notes            # a tag install: the newest tag; a branch install: that branch's tip
mv2 plugins update notes@v2.0       # a given tag, branch or commit
mv2 plugins remove notes
```

- **Update** fetches the new version and says what changed: the commits since the installed one, and anything in what it can do. When only its code changes, it updates without asking and shows the commits. When its access, permissions, extension points, what it adds to harness launches, or Python packages change, it asks first, as `install` does. It also asks when the installed files were changed since the install, since what runs then may not be what was installed. A plugin installed at a commit updates only to a ref you name. One installed from a folder updates from that folder.
- **Remove** removes only what `install` placed: the plugin's folder, its record and its pin. It refuses a plugin you placed by hand. It turns the plugin off nowhere and keeps its settings, grants, secrets and Python packages, then lists what's left with the command that clears each. `mv2 plugins disable NAME` on a plugin a universe no longer has forgets it there: `plugins.json` then neither turns it on nor grants it anything.

In the dashboard, press `P`, then `i` to install: type a git URL or a folder, and the screen shows the same words as `mv2 plugins install` before anything is placed, with Install or Cancel (`PgUp`/`PgDn` scroll a long one). It installs into your personal plugins; use `--here` on the command line for a universe's own. `u` updates the selected plugin and `d` removes it, as `update` and `remove` do: an update asks only when what the plugin can do changes, and otherwise shows what changed afterwards; a removal lists what it left with how to clear each. Both work only on a plugin `install` placed. While a fetch, install or removal is under way, `Esc` waits, so nothing fetched is left behind and what a removal left is still listed. A universe that still has a plugin of that name turned on from before loads it as soon as it's installed, and the screen says so for the universe you're in.

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

A plugin can need Python packages that mv2 doesn't have. Enabling it then lists each package with its exact version and asks before installing them. Installing runs none of their code, but the plugin imports them, so their code runs inside mv2 with the plugin's trust: say yes only if you trust the plugin and its packages. They go in a folder of the plugin's own under `~/.multiverse/plugin-packages/`, never into mv2's own environment, and `mv2 update` leaves them alone. `mv2 plugins packages NAME` shows them and installs them when you say yes; `--install-packages` says yes without the question, for scripts. When the plugin's list changes, it asks again. An agent can't approve an install.

`enable` lists what the plugin may do, installs its default settings when the universe has none, and says when the plugin needs the plugin host. In the dashboard, press `P`, select the plugin and press `Space`. If the plugin can work with less access than it asks for, the dashboard asks which access to grant it in that universe; `Esc` leaves it off. If its Python packages aren't installed, it first shows the same list as `mv2 plugins packages NAME` and asks: Install installs exactly the list it showed and then turns the plugin on; if the plugin's list changed meanwhile, nothing installs and the screen says so. Cancel or `Esc` leaves it off. On a plugin that's on but can't load without them, `Enter` asks the same. While they install, `Esc` waits, so the install isn't left running.

The switch lives in the universe's `plugins.json`, which you can also edit by hand:

```json
{"schema": 1, "enabled": {"compaction": true, "remote": false}}
```

- A plugin that `plugins.json` doesn't name stays at its default. Every plugin is off by default, except typeahead.
- Nothing is inherited. A subverse has its own `plugins.json`, so a plugin that is on in the root stays off in a subverse until you turn it on there.
- Running agents keep the plugins they launched with, so a plugin you turn on reaches an agent at its next launch (`mv2 kill`, then `mv2 resume`). A plugin can react sooner to being turned off: with compaction off, a running agent's next `/compact` is Claude's own.

### Access

Each plugin works under an access level you grant it in each universe:

| Level | The plugin |
| --- | --- |
| `observe` | Reads agents, messages and transcripts, and changes nothing |
| `spend` | Also uses model tokens, money or time (side questions, one-shot runs), still changing nothing |
| `act` | Also changes agents, inboxes and files: spawns, sends, stops |

```sh
mv2 plugins enable compaction                  # on a terminal, asks which level to grant
mv2 plugins enable compaction --grant spend    # grants spend without asking
mv2 plugins grant compaction observe           # changes it later
```

- **What it asks for:** `enable` and `info` say the level a plugin asks for and the least it can work with, with its own reason. You grant that level or a lower one, never below its least. A plugin with less than it asks for offers fewer features.
- **What it can do here:** `info`, `enable` and the Plugins screen list what its level allows, then what that level holds back, each with the level it needs (`send messages ... (needs act)`). What a plugin's own code does, such as listening on a port, no level holds back. While it is off, they list what it asks to do.
- **Without a terminal,** `enable` grants the plugin's least level and says how to grant more.
- **No grant recorded** means the plugin has what it asks for, so plugins that were on before levels existed keep working.
- **Agents can't raise a grant.** From an agent, `enable` grants at most the plugin's least level and `grant` won't raise one; both print the command a person runs. Like permissions, this guards against mistakes, not against an agent that edits `plugins.json`.
- **Read-only:** `"access_ceiling": "observe"` in `plugins.json` caps every plugin in that universe, and `MV2_EFFECTS=observe` caps every plugin in one command or dashboard.

A call above a plugin's level fails with a reason, such as `planner has observe access here; send needs act`, and a launch addition that needs more is left out, with the reason on the agent's `mv2 inspect` `Note:` line. Each says what holds the plugin back and how to change it: its grant (`mv2 plugins grant`), the universe's `access_ceiling`, or `MV2_EFFECTS` for that process. A plugin that is on but held below the least level it can work with is reported by `mv2 plugins` and `mv2 doctor`.

## Set a plugin's secrets

A plugin that needs an API key or a token declares it as a secret, which mv2 keeps out of its settings:

```sh
mv2 plugins secrets notifier        # what it declares, and whether each is set
mv2 plugins secret notifier api_key   # asks for the value; typing is hidden
some-command | mv2 plugins secret notifier api_key   # or from stdin
mv2 plugins secret notifier api_key --clear
```

- **Where they go.** The system keychain: macOS Keychain, or the Secret Service on Linux. Where there is none, a file only you can read, `~/.multiverse/plugin-secrets/secrets.json`, and the command says so. `MV2_SECRET_STORE=file` picks the file anyway, for example in tests or on a machine whose keychain you'd rather not use. Never the universe's folder, a settings file, a log, `mv2 doctor`, a transcript or an agent's environment, and never shown again: only "set" or "not set".
- **Per universe.** Each universe has its own. A subverse without one of its own uses its nearest parent's, so changing the parent's changes theirs. Set one in the subverse to override it there; clearing it there goes back to the parent's.
- **Who.** Only you: an agent can't set or clear one, and the value is never taken as an argument, which your shell would keep in its history. On a terminal that can't hide what you type, the command reads nothing and asks you to pipe the value in. Only the plugin that declared a secret reads it, while it is on and trusted in that universe.
- **Missing.** A plugin missing a required secret is listed with the command that sets it, and acts as off in that universe until it's set.
- **When the keychain refuses.** Setting or clearing one fails with the keychain tool's exit status, never its output. A secret the keychain refused to remove stays set, and `--clear` says so.
- **Disabling** keeps them, so turning the plugin on again needs nothing; a disabled plugin can't read them. `--clear` removes one, and `mv2 doctor` names a secret whose plugin no longer declares it, with the command that clears it.

In the dashboard, press `P`, select the plugin and press `Enter`; for a plugin with settings too, choose Secrets. Each secret says whether it is set here, comes from a parent universe, or is missing, and never shows a value. `Enter` on one asks for its value in the terminal, as `mv2 plugins secret` does: the dashboard steps aside, typing is hidden, and an empty line or `Ctrl+C` keeps it as it is. A terminal that can't hide typing is asked nothing; the screen names the command to run in one that can. The value never passes through the dashboard, so its logs can't hold it. `Ctrl+D` clears the selected one here, after asking. The Plugins screen follows a secret set or cleared in another terminal, so a plugin missing a required one shows it within a second. A secret that comes from a parent universe is cleared there; a value set here overrides it here.

## Change a plugin's settings

```sh
mv2 plugins settings notifier                          # its file, whether it can be used, and its values
mv2 plugins settings notifier --set quiet_seconds=60   # VALUE is JSON, or else text
mv2 plugins settings notifier --set notify.done=false --unset max_age_seconds
```

Each plugin keeps its settings for a universe in `plugin-settings/NAME.json`. If the plugin's schema or its own settings check refuses an edit, nothing is written and the command exits 1 with the reason. A plugin whose code can't run here (untrusted, failed to load, safe mode) has only its schema checked, and `mv2 plugins settings` says so. In the dashboard, press `P`, select the plugin and press `Enter` (Configure). This opens the plugin's own settings screen, or fields built from its schema, whether the plugin is on or off there. If its code can't run (untrusted, failed to load, safe mode), the screen says why.

Settings that can't be used never stop a launch. Instead, the plugin acts as off in that universe until they're fixed: `mv2 plugins` lists it as `unusable`, and `mv2 plugins settings NAME` and `mv2 doctor` say why.

A plugin's own check can also pass the settings with notes, such as a key it ignores. The settings stay usable, and `mv2 plugins info`, `mv2 plugins settings` and the Plugins screen show each note as `Note: ...`. `mv2 doctor` lists each one as a problem, naming the settings file, until it's fixed.

Some plugins used a settings file before they became plugins, such as `compaction.json`. The next `mv2` command that runs in the universe converts that file to `plugin-settings/`. `mv2 plugins migrate` shows what it would convert, and `--apply` converts it now. Once converted, an old file found again (brought back by an old branch, say) is ignored, and `mv2 doctor` says so: remove it.

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
- mv2 runs a copy of exactly the files you trusted, kept in `~/.multiverse/plugin-cache/`. If that copy's files are edited, the plugin is listed as changed and doesn't run; the problem names the copy to remove, and mv2 then makes it again from the trusted plugin.
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
- `mv2 update` restarts the host when it still runs the code from before the update, which its plugins would otherwise fail on. It first lets everything in flight finish, however long that takes (the remote control answers new requests 503 meanwhile), so the update may say the host restarts once that is done. A handler that never returns keeps it waiting; `mv2 plugins host stop` then stops it. A host started by an mv2 from before this restart check is stopped and started instead, which can end a call in flight.
- `mv2 plugins host stop` gives each plugin 10 seconds, so it can end work in flight.
- The host's log is `state/plugins/host/host.log` in the top-level universe.
- If the host's `state/plugins/host/status.json` is damaged, `mv2 plugins host` says its status is unknown and why. The host rewrites the file when it starts.

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
- Its command fails with `mv2: plugin NAME: …`. An error the command gives on purpose (a `ValueError`, `OSError`, `KeyError` or `RuntimeError`) prints `mv2: MESSAGE`, as a built-in command's does.

A launch goes on without the plugin and records why in the agent's `runtime.json`. `mv2 inspect` shows the reason on a `Note:` line, for a plain launch as well as a bridged one. No plugin can stop a launch, a send, `mv2 list` or the dashboard.

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
