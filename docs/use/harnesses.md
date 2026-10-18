# Harnesses

A harness is the program an agent runs: Claude Code, Codex, Pi, or a command you configure. You pick one when you create an agent. Each universe lists its harnesses in `harnesses/NAME/harness.json`. Codex is the default.

## Live connection or launch-only

mv2 never types into an agent's terminal, so it uses no tmux `send-keys` and no pasting. What it can do with a running agent depends on whether the harness has an API that mv2 can talk to:

| | Live connection | Launch-only |
| --- | --- | --- |
| How mv2 talks to it | Through the harness's own API: a Claude Code plugin, the Codex app-server, a Pi extension | It doesn't. mv2 only starts, stops and resumes the program in its tmux pane |
| Messages from other agents | Arrive in the running session, mid-turn or after the turn | Wait in the agent's inbox until it checks with `mv2 inbox`, or until the agent is resumed |
| Status in the dashboard | `working`, `waiting`, `idle` | `unknown` |
| Context use, cost | Shown | Not shown |

Some things work the same in both modes:

- You can attach and type to the agent yourself.
- A message to a stopped agent resumes it with that message.
- Resume continues the exact conversation.

## Compare harnesses

| | Claude Code | Codex | Pi | Custom command |
| --- | --- | --- | --- | --- |
| How mv2 connects | A Claude Code plugin (mv2's `multiverse` mod) | The Codex app-server's API | A Pi extension | Launch-only |
| Live connection by default | Yes, when Claude Code is new enough | Yes, when the app-server is available | No; set `inbox_bridge` to `true` | Never |
| Messages reach a running agent | With the connection | With the connection | With the connection | No |
| A message wakes a stopped agent | Yes | Yes | Yes | No |
| Live status | With the connection | With the connection, plus `disconnected` | With the connection: `working`, `idle` | No |
| Context use | Yes; the connection adds the window, cost and rate limits | With the connection | No | No |
| `mv2 ask` side questions | With the connection | No | No | No |
| Tree compaction ([plugin](plugins.md)) | With the connection | No | No | No |
| `/multiverse` in the session | With the connection | No | With the connection | No |
| Reconnects a lost terminal | No | With the connection, when idle | No | No |

`mv2 capabilities --harness NAME` shows what a harness supports on your machine. For Claude it says what its launches get with the harness's `inbox_bridge` setting; add `--probe` to check the installed Claude Code too. `mv2 capabilities --agent NAME` shows what one running agent has, with the reason for anything missing. When an agent has no live connection, the dashboard's inspector says why, for example because Claude Code is too old.

## Turn the connection on or off

The `inbox_bridge` setting in `harness.json` controls the connection:

| `inbox_bridge` | Claude Code and Codex | Pi |
| --- | --- | --- |
| unset | Connect if possible; otherwise launch-only, with the reason shown | Launch-only |
| `true` | Connect; the launch fails if it can't | Connect |
| `false` | Launch-only | Launch-only |

mv2 applies the setting at every start and resume. A running agent keeps the mode it started with. After changing the setting, stop the agent and resume it. Its conversation is kept.

## Claude Code

```json
{"schema": 2, "adapter": "claude", "settings": {}}
```

With a live connection, mv2 loads its `multiverse` mod into Claude Code and gives the session a private inbox. `steer` merges a message into the current turn at the next tool result, or starts a turn when Claude is idle. `follow-up` waits for the turn to end. A message that starts its own turn adds a `Prompt dropped by a hook` line to Claude's transcript. Claude Code writes it when the mod replaces its own framing of the message; it is not an error, and mv2 can't remove it. The mod also reports status, delivery receipts, context use, cost and rate limits, and it answers `mv2 ask`. The dashboard header shows the account's rate-limit windows, such as `5h 34% · 7d 12%`.

If the installed Claude Code is too old for the mod and `inbox_bridge` is unset, the agent starts launch-only. `mv2 inspect NAME` then shows the reason on its `Note:` line. With `inbox_bridge` set to `false`, mv2 loads no mod, plugin or hook. Minimum versions are in [compatibility](../reference/compatibility.md#bridge-feature-minimums).

**Status line.** Connected sessions show a line under Claude's prompt, next to your own status line. It shows these items:

- The reported phase.
- The count of unread inbox-only messages, when above zero.
- The compaction trees (`trees 116/118 · building`), when compaction is on.

Choose the items and their order in `statusline.json` at the universe root:

```json
{"schema": 1, "items": {"agent": true, "phase": true, "summary": false, "inbox": true, "compaction": true}}
```

Claude Code draws every plugin's status line in yellow, with `⚠` and the plugin's name (`⚠ multiverse: working`). That is its style for plugin lines, not an error, and mv2 can't change it. A running `/compact` shows its progress on the same line. `mv2 refresh --apply` adds `statusline.json` to a universe created before it existed. If you deleted it, refresh keeps it deleted; copy it from a new universe or from `src/multiverse/templates/base/` to get it back.

**`/multiverse`.** With a live connection, `/multiverse` is a real Claude command. Its subcommands are `list`, `NAME [args]`, `send AGENT TEXT` and `inbox` (see [messaging](../cli/messaging.md#inside-claude-and-pi-sessions)). Only a person typing at the prompt can use it: the model can't run `/multiverse` (Claude's Skill tool refuses plugin commands), so agents use `mv2` commands instead. Recent Claude Code versions suggest the subcommands and agent names as you type.

**Resume and the bootstrap.** A resumed Claude agent gets the universe's current bootstrap, because mv2 resumes it with `--system-prompt-snapshot off`. To keep the prompt Claude recorded at the conversation's start instead, set `"system_prompt_snapshot": "on"`. A bootstrap change then reaches only new agents. A `--system-prompt-snapshot` flag you pass yourself in `args` or `resume_args` wins over the setting.

**`/clear` and `/restart`.** `/clear` moves Claude to a new conversation. mv2 follows it, so messages, context use and resume go to the new conversation, and the earlier one stays recorded for the agent. Claude's own `/restart` relaunches without mv2's inbox. mv2 still delivers when it can confirm the relaunched process is the same agent; otherwise `mv2 inspect NAME` says to kill and resume it.

**Permissions.** Claude's permission prompts, login and workspace trust stay native. The agent shows `waiting`, and you attach to answer. To run without prompts in a sandbox, pass Claude's own flags in `args`, for example `"args": ["--dangerously-skip-permissions"]`.

## Codex

```json
{"schema": 2, "adapter": "codex", "settings": {}}
```

The live connection runs through Codex's shared app-server daemon. There is one daemon per OS user and Codex home, shared by every agent, project and dashboard. Spawn and resume start or reuse it with `codex app-server daemon start`, then connect the agent's terminal through a private relay. mv2 never stops, restarts or updates the daemon. Closing the dashboard leaves it running.

**Codex home.** Set `CODEX_HOME` (and `CODEX_SQLITE_HOME`, if you use it) in the environment you run mv2 from, not in harness `env`. A connected agent remembers these storage paths and keeps using them on resume. If you pass a different value for a saved path, the resume result names the kept path and the ignored override. Spawn another agent to use different storage. Adding `CODEX_SQLITE_HOME` to an agent that never used it changes its storage scope and is refused.

**Fallback.** With `inbox_bridge` unset, the agent starts launch-only in these cases:

- The daemon, the CLI's `--remote` support or the relay is unavailable.
- `args` contain `--remote` or `--no-daemon`.
- On resume, `args` contain `--profile`, `--add-dir` or `--approve-for-me`.

`mv2 inspect NAME` shows the reason.

**Permissions.** Put sandbox and approval flags that must hold on resume in `args`, not `spawn_args`. On a connected resume, mv2 applies `--sandbox`, `--ask-for-approval` and `--dangerously-bypass-approvals-and-sandbox` to the saved conversation. It then checks that Codex accepted them.

```json
{"schema": 2, "adapter": "codex", "settings": {"args": ["--sandbox", "workspace-write"]}}
```

**Session binding.** With a live connection, mv2 records Codex's session ID automatically. A launch-only agent is asked to run `mv2 session-bind` at startup. If it never did, take the ID from Codex's `/status` and bind it with `mv2 session-bind --agent NAME --session-id UUID` before resuming.

**Lost terminals.** If an agent's terminal loses its connection to the daemon, `STATUS` shows `disconnected`. New messages are then refused, but saved. If the session was idle, mv2 reconnects it in the same pane, at most twice in five minutes, and `mv2 list` notes `Reconnected automatically.` Unsent text in Codex's composer is lost. When mv2 doesn't reconnect it, kill and resume the agent, as [troubleshooting](troubleshooting.md#codex-shows-status-disconnected) describes.

**Shared conversations.** Another Codex client on the same daemon can open an agent's conversation at the same time, for example `codex resume` in another window. It is still the same agent, so its `mv2 report` and `mv2 inbox` work from either client. While another client has it open, Codex keeps the conversation's current settings. A resume can't change its permissions then, and `mv2 inspect NAME` names the settings that differ. See [shared conversations](../design/shared-conversations.md).

**Opened elsewhere.** A different Codex server, or a `codex resume --no-daemon` session, can hold the conversation. Codex then refuses to open it: a plain launch shows "This conversation is open in another app", and a connected one gets "thread … already has an active writer". mv2 shows that refusal as the launch's error in `mv2 list`, `mv2 inspect`, `--wait` and `mv2 send`. Pressing R in Codex doesn't clear it. Close the other session, run `mv2 kill NAME --attachment-only`, then run `mv2 resume NAME`.

On supported Codex layouts, startup refusal observation ends when the ordinary composer is ready. Later navigation does not turn that successful launch into a startup refusal. Loading, resuming and forking screens stay under observation. An incomplete or unrecognized screen stays under observation for a bounded startup period.

**Update prompts.** mv2 starts Codex with `-c check_for_update_on_startup=false`, so unattended terminals don't stop at an update dialog. Add `["-c", "check_for_update_on_startup=true"]` to `args` to get the prompts back. A launch can then wait at the dialog until you answer it, so `--wait` can time out.

**Daemon restarts.** If a launch reports `failed to resolve feature override precedence`, the daemon was started from a directory that has since been deleted. Run the restart command that the error prints, but only when no other Codex work is running. Then resume the agent. See [troubleshooting](troubleshooting.md#codex-failed-to-resolve-feature-override-precedence).

The mouse wheel scrolls Codex's transcript, because mv2 turns on tmux mouse mode for Codex agent sessions only. Codex has no native `/multiverse` command. Instead, ask the agent in words ("Please run /multiverse list"), or run `! "$MV2_CLI" catalog` in Codex.

## Pi

```json
{"schema": 2, "adapter": "pi", "settings": {"inbox_bridge": true}}
```

Pi is launch-only unless `inbox_bridge` is `true`. With that setting, mv2 loads its extension into Pi. The extension does four things:

- It receives `steer` and `follow-up` over a private socket.
- It reports `working` and `idle`.
- It confirms deliveries from Pi's transcript.
- It adds `/multiverse` with `list` and `NAME [args]`.

Each agent has its own Pi session file, which resume needs.

## Custom commands

A schema-1 `harness.json` runs any command-line agent from `spawn` and `resume` argument lists, with no adapter code. It is launch-only, so it has no message delivery, status or wake. See [files](../reference/files.md#schema-1) for the fields. To write a built-in adapter, see [harness adapters](../develop/harness-adapters.md).

## Settings

Every setting is optional. A change applies at the next spawn or resume:

| Setting | Use |
| --- | --- |
| `model` | Default model for new agents |
| `args` | Extra flags on spawn and resume; `spawn_args` and `resume_args` for one of them |
| `env` | Variables set for the agent, as strings |
| `inbox_bridge` | The live connection, described above |
| `system_prompt_snapshot` | Claude only: `"off"` (the default) or `"on"`, described under [Claude Code](#claude-code) |
| `executable`, `unset_env` | Another binary to run; variables to remove |

```json
{
  "schema": 2,
  "adapter": "claude",
  "settings": {
    "model": "opus",
    "args": ["--permission-mode", "acceptEdits"],
    "env": {"DISABLE_AUTOUPDATER": "1"}
  }
}
```

`env` can't set `MV2_*`, `PATH`, `HOME` or the storage variables mv2 reads (`CODEX_HOME`, `CODEX_SQLITE_HOME`, `CLAUDE_CONFIG_DIR`). Its values are stored in plain text in `harness.json` and in each launch's records, so keep secrets out of it. `mv2 capabilities --harness NAME` reports an invalid file. The full schema is in [files](../reference/files.md#harnessesnameharnessjson).

## Choose a model

```sh
mv2 spawn --name planner --harness claude --model opus
```

The name goes to the program's own `--model` flag, so use the names that program accepts. Without `--model`, a new agent gets the harness's `model` setting, or the program's own default. Resume keeps the agent's saved model. To switch a stopped agent to another model, run `mv2 resume planner --model sonnet`.

For a model you use often, add a harness profile. A profile is a second directory with the same adapter, such as `harnesses/claude-opus/harness.json`:

```json
{"schema": 2, "adapter": "claude", "settings": {"model": "opus"}}
```

Then run `mv2 spawn --name planner --harness claude-opus`. The dashboard's harness selector lists every profile.

## The tmux status bar

Each agent's tmux bar shows a short live line instead of tmux's session list and clock:

| Harness | Example |
| --- | --- |
| Codex | `reader · codex · status waiting · phase working · inbox 2` |
| Pi, custom | `reader · pi · phase working · inbox 2` |
| Claude | `reader · claude` (its status line carries the rest) |

The `tmux` block of `statusline.json` chooses the items: `agent`, `harness`, `status`, `phase`, `summary`, `inbox` and `context`. List them in `tmux.items` in the order you want. Items you leave out follow in their default order.

`tmux.harnesses.ADAPTER.items` turns items on or off for one adapter (`codex`, `claude` or `pi`), whatever the profile is called. Use it to change Codex's `status` or Claude's `phase` and `inbox`, which have built-in adapter defaults:

```json
{"schema": 1, "tmux": {"harnesses": {"codex": {"items": {"context": true}}, "pi": {"items": {"summary": true}}}}}
```

While you are attached, the bar refreshes every 2 seconds. Set `refresh_seconds` (1 to 60) to change the interval. Item changes show at the next refresh, and a new interval applies at the next spawn or resume. mv2 changes only its own agent sessions, never your global tmux options. See [files](../reference/files.md#statuslinejson) for every key.

## Known limits

| Harness | Limits |
| --- | --- |
| Claude Code | The connection lives inside the Claude process, so nothing reconnects a lost terminal. Resume restarts the conversation. `mv2 ask` has nothing to answer from before the first response or right after `/clear`. An agent stopped before its first message has no saved conversation, so it restarts fresh under the same ID. So does one whose conversation Claude deleted after `cleanupPeriodDays`, without its earlier context; `mv2 resume` says which case it was. Keep Claude's `cleanupPeriodDays` large. |
| Codex | When launch-only, it has no message delivery, status or session name, and no new context readings. A daemon crash interrupts every agent on it. Native work can keep running after its terminal disconnects, and `mv2 kill` cancels it. Codex permission and trust prompts stay native, so attach to answer them. |
| Pi | It has no `mv2 ask`, context use or compaction, and no `waiting` status. Resume refuses when the agent's session file is missing. |
| Custom | It is launch-only, with no delivery, status, wake or naming. |

Tested versions are listed in [compatibility](../reference/compatibility.md).
