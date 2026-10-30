# Troubleshooting

You can still use `inbox --agent NAME` and `report --agent NAME` as the operator
when an agent's runtime record is damaged. These commands do not repair that
record; callbacks from an agent still require their verified identity.

A leftover folder with damaged records may conservatively reserve a name when
that text appears in a surviving record. Repair the named damaged file. If `properties.json` is missing, list, inspect and doctor offer `mv2 remove UUID` even when other records survive. Removal checks those records and the agent's processes before moving the leftover folder to the trash; a live or unprovable process still blocks it. A present but damaged properties file must be repaired instead: list, inspect, doctor and a refused remove show its full path and advise repairing it from a backup or restoring an intact copy. They never offer removal for that file.

Each problem below gives the symptom, the cause and the fix. If you don't know the cause yet, start with the two commands in the next section.

## Start here

```sh
mv2 doctor
mv2 inspect NAME
```

`doctor` checks the universe and every subverse below it. It covers workspaces, catalog entries, plugins and their settings, and every agent's last launch. It prints `No problems.` or one sentence per problem, such as `1 problem: worker's last launch failed: ...`. It exits non-zero when it finds a problem.

`inspect` shows one agent's key facts, then the last 30 lines of its terminal (`--lines N` for more):

```text
worker · Codex, bridge · waiting
Reported: working — Checking the launch permissions

Terminal (last 30 lines):
...
```

The first line gives the name, the harness and its mode, and the state. The mode is `bridge` for a live connection and `plain` for launch-only. When present, `Last error:` gives the launch error, and `Note:` gives the reason for a plain fallback. `mv2 list --wide` shows every agent with its full errors. `mv2 capabilities --agent NAME` says why each operation is or isn't available.

## Launches

### A spawn or resume failed

`spawn` or `resume` prints `mv2: NAME did not start: REASON` (or `did not resume`). `mv2 list` shows `! REASON` under the agent. Without `--wait`, `spawn` returns once the launch is registered. A program that exits during startup therefore shows up in `mv2 list` a few seconds later.

| Reason starts with | Cause | Fix |
| --- | --- | --- |
| `Native process exited with code N` | The program quit; the last lines of its terminal follow. SIGTERM, recorded as -15 or the shell exit status 143, is an ordinary stop, not a launch failure in list or doctor. Other signals and exit codes keep their existing meaning | Read them in `mv2 inspect NAME`. The cause is often a login, trust or flag problem, so run the program once by hand |
| `Timed out after 60s waiting for launch readiness` | `--wait` gave up; the launch is still running | Run `mv2 inspect NAME`, and attach to answer a login or trust prompt |
| `Harness executable not found` | The program isn't on `PATH` | Install it, or set `executable` in `harness.json` |
| `Workspace does not exist` | The universe's workspace or the agent's saved one is gone | Restore the directory |
| `tmux is required to spawn agents`, `error connecting to` | tmux is missing or its socket is unusable | See [tmux problems](#tmux-socket-or-permission-problems) |

If `mv2 list` shows the failed agent, mv2 has kept its record and its name. After fixing the cause, retry it:

| Harness | Retry |
| --- | --- |
| Claude | `mv2 resume NAME`. An agent that never got its first message starts fresh under the same session ID. |
| Codex | `mv2 resume NAME` if it recorded a session. If resume says `No native session ID recorded`, see below. |
| Pi | `mv2 resume NAME` if its session file exists; otherwise remove it and spawn again. |

To start over under the same name, run `mv2 remove NAME` and then spawn again. `remove` moves the failed record to the trash. If you use a custom tmux socket, set the same `MV2_TMUX_SOCKET` for every retry.

### Resume says "No native session ID recorded"

A launch-only Codex agent records its session by running `mv2 session-bind` at startup, and this one never did. mv2 never guesses a session from Codex's history. If Codex did start a session, find its ID with `/status` in that Codex session or in its exit message. Then bind it and resume:

```sh
mv2 session-bind --agent NAME --session-id UUID
mv2 resume NAME
```

If no session ever started, remove the agent and spawn again.

### The bootstrap is stale after a refresh

`mv2 refresh --apply` prints `running agents keep their old bootstrap until relaunched`. This is because each launch copies `bootstrap.md` when it starts. The same holds for `harness.json`, plugins turned on or off, and new mv2 code after `mv2 update`. Relaunch the agent to pick up the changes, which keeps its conversation:

```sh
mv2 kill NAME
mv2 resume NAME
```

## Agent state

### An agent shows unknown or dormant

| Shows | Cause | Fix |
| --- | --- | --- |
| LIVE `dormant` | No tmux pane: the program exited, it was killed, or the tmux server stopped, for example after a reboot | `mv2 resume NAME`, or send it a message, which resumes it |
| LIVE `stopped` | The program exited and its pane was kept, usually with a startup error | `mv2 inspect NAME`, then fix and resume |
| LIVE `unknown` | mv2 couldn't ask tmux; `mv2 doctor` says `state is unknown: REASON` | Fix tmux access (below). Until then mv2 refuses to kill or relaunch it |
| STATUS `unknown` on a live agent | Launch-only (`plain`), or the live connection hasn't reported yet | This is expected when launch-only. `mv2 inspect NAME` shows the mode |

### "Outdated launch"

For launch-scoped agents, `report`, `inbox` and `session-bind` print `mv2: Outdated launch` when their `MV2_LAUNCH_ID` names an earlier launch. The command came from one of these:

- A shell or background job left over from before the agent was resumed.
- A terminal that copied an agent's environment.

mv2 refuses so that an old process can't act as the current one.

Run the command from the agent's current session instead. To run it yourself, clear those variables and name the agent:

```sh
env -u MV2_AGENT_ID -u MV2_AGENT_DIR -u MV2_LAUNCH_ID -u MV2_IDENTITY_MODE mv2 inbox --agent NAME --peek
```

`Outdated launch; refusing to kill a replacement` means the agent was relaunched while `kill` ran. Check `mv2 list` and kill again if needed.

Current launches omit the launch ID from connected Codex tools; older loaded conversations can retain it. Their data callbacks use the agent's identity, native thread and saved storage paths. An incomplete or mismatched identity is refused; it does not become an operator call. Runner updates and control calls still require the current launch. See [environment](../reference/environment.md).

Use the current mv2 installation for an older connected Codex conversation. Its own retained bridge request and current conversation proof can authorize data callbacks without replacing cached tool settings. Codex can keep an idle conversation loaded for up to 30 minutes after its last client leaves; restarting the frontend does not guarantee fresh settings. A separate old checkout still runs old code. If the current installation refuses the identity, inspect the saved records; do not copy identity values into the conversation. See [the upgrade rules](../design/shared-conversations.md#upgrade-running-codex-bridge-agents).

### A name is taken or ambiguous

| Message | Cause | Fix |
| --- | --- | --- |
| `This universe already has an agent named NAME` | Names are unique in a universe, including stopped, dormant and failed agents | `mv2 resume NAME`, choose another name, or `mv2 remove NAME` first |
| `Expected one agent matching 'REF'; found 2. Use a full UUID from these matches: ...` | A reference matches a name, a full ID or the start of an ID, so a short hex name can match another agent's ID | Use one of the full UUIDs listed |
| `No agent named NAME here` | The agent is in another universe or subverse | Run `mv2 list --recursive`, then use `-u PATH` for that universe |

## Messages

### A message shows uncertain or failed

Every message is saved before delivery, so nothing is lost. What to do depends on the result line, and [messaging](../cli/messaging.md#read-the-result) lists all of them:

| Result | Fix |
| --- | --- |
| `NAME: cannot yet tell whether it received the message.` | `mv2 deliver NAME --check`. It asks the session and records what arrived, without sending anything. Don't resend by hand, because the agent may already have it. |
| `NAME: message accepted; no confirmation yet. Do not resend.` | Nothing is needed. Check again later with `--check` if you need the confirmation. |
| `Not delivered to NAME: REASON. Saved; resume it, then mv2 deliver NAME --retry.` | Fix the reason, then `mv2 deliver NAME --retry`. It resends only definite failures. If the agent is stopped, `--retry` resumes it with the message as its first prompt. |

`deliver` exits non-zero while something is still failed or uncertain.

### A launch-only agent can't receive messages

`send` prints `Not delivered to NAME: ...` with a reason such as `messaging is not connected for this session`. Codex says `the session is still starting` instead. `mv2 inspect NAME` shows `plain`. A plain launch has no channel into the running program (see [harnesses](harnesses.md#live-connection-or-launch-only)). Choose one of these:

- Send with `--delivery inbox-only`. The agent reads the message with `mv2 inbox` at its next check.
- Attach with `mv2 attach NAME` and type to it.
- Relaunch it with a live connection. Fix the cause in `mv2 inspect`'s `Note:` line, or set `inbox_bridge`. Then run `mv2 kill NAME` and `mv2 deliver NAME --retry`. The retry resumes the agent with the saved message.

## Harnesses

### Claude starts launch-only (`plain`)

`spawn` prints `Started NAME (Claude, plain).` Find the reason on `mv2 inspect NAME`'s `Note:` line, or in `mv2 capabilities --agent NAME` as `This agent is launch-only: REASON`. A send to it says it is launch-only, too. It is recorded in the agent's `runtime.json` as `native_mode.fallback_reason`.

| Reason | Fix |
| --- | --- |
| `Claude Code X is older than Y, which the multiverse mod needs` | Update Claude Code, then kill and resume the agent. With `"inbox_bridge": true` the launch fails instead. |
| `its harness sets inbox_bridge to false`, and the dashboard inspector shows `Mode: plain (plain)` | Remove `"inbox_bridge": false` from `harness.json`, then kill and resume. |
| `Launched without compaction: compaction.json has not been converted: ...` | This is not launch-only. The connection works, but an old `compaction.json` hasn't been converted yet. Run `mv2 plugins migrate --apply`, then kill and resume the agent. |
| `plugin compaction was left out: its settings cannot be used: ...` | This is not launch-only. The connection works, but the compaction plugin's settings are unusable, so Claude's own `/compact` applies. `mv2 doctor` names the problem, so fix it and relaunch. |
| `Claude has no saved conversation for this session: the agent was stopped before its first message, ...` | This is not launch-only. There was nothing to resume, so the agent started fresh under the same session ID. `mv2 resume` prints the same line. |
| `Cannot prove unique conversation storage` | Resume from the shell whose storage variable points at the real conversation. If every known candidate is readable and the conversation is absent everywhere, use `mv2 resume NAME --start-fresh` to keep the agent and inbox with a new conversation. This never discards readable history. |
| `Claude's saved conversation for this session is missing (Claude deletes conversations older than its cleanupPeriodDays setting): ...` | This is not launch-only. The agent had a conversation, but Claude's file for it is gone, so it started fresh without its earlier context. `mv2 resume` prints the same line. mv2 tells the two cases apart from its delivery receipts, the context the agent last reported, and Claude's prompt history. To keep conversations, set a large `cleanupPeriodDays` in Claude's settings. |

Sometimes a connected launch never becomes ready: `--wait` times out, and sends report `messaging is not connected for this session`. The launch may be waiting at a Claude login or workspace trust prompt. Attach and answer it.

### Codex shows `STATUS disconnected`

The agent's Codex terminal lost its connection to the shared app-server while the process kept running. New messages are refused, but saved.

If the session was idle, mv2 reconnects it in the same pane, at most twice in five minutes. Idle means it had no turn, prompt, queued message or background terminal. `mv2 list` then shows `Note: Reconnected automatically.`, and `doctor` prints `Note: NAME reconnected automatically.`

Otherwise, it stays disconnected, and `mv2 inspect NAME` shows the reason. One example is `Native relay lost. Kill then resume the agent; native work may still be running.` To recover, run:

```sh
mv2 kill NAME
mv2 resume NAME
mv2 deliver NAME --retry
```

If the reason says `Kill this unbound launch and spawn again.`, the launch never recorded a session. Kill it, remove it and spawn again. mv2 never restarts the shared daemon. It also never replays a message that may have arrived.

### Codex callbacks retain an old launch identity

Run the callback through the current mv2 installation. It accepts an older bridge conversation when the retained launch request and current thread, storage and agent records all match. If it refuses, inspect the named identity or request record; a different thread, changed storage or missing proof remains refused.

Codex can retain an idle conversation's tool settings for up to 30 minutes after its last client leaves by default. Stopping and resuming the frontend does not guarantee that those settings reload. If `MV2_CLI` points at a separate older checkout, that checkout still runs its old checks; use the current installation. See [upgrade identity proof](../design/shared-conversations.md#upgrade-running-codex-bridge-agents).

### Codex: "This conversation is open in another app"

A connected launch reports the same refusal as "thread … already has an active writer". A different Codex server, or a `codex resume --no-daemon` session, holds the conversation. mv2 shows Codex's refusal as the launch's error, and pressing R in Codex doesn't clear it. Close the other session, then close mv2's terminal without cancelling the conversation's work, and resume:

```sh
mv2 kill NAME --attachment-only
mv2 resume NAME
```

### Codex: "failed to resolve feature override precedence"

A launch error or `Note:` reads `Cannot isolate shared native session environment: ... failed to resolve feature override precedence: No such file or directory`. The shared Codex daemon was started from a directory that has since been deleted, such as a removed worktree. With `inbox_bridge` unset, the agent runs plain. With `true`, the launch fails.

The message ends with a restart command for the agent's selected Codex home, like `cd /home/me && CODEX_HOME=/home/me/.codex codex app-server daemon restart`. It uses the launch's `HOME` when `CODEX_HOME` is unset or empty, including a home retained during legacy adoption. Restarting interrupts every Codex session on that daemon. Run the command exactly as printed, and only when no other Codex work is running. Then resume the agent, killing it first if it is running plain. mv2 never restarts the daemon itself.

## Environment

### tmux socket or permission problems

New launches require tmux 3.2 or newer. Check `tmux -V` and upgrade if mv2 names an older version. Version 3.2 added the per-session environment option (`new-session -e`) that mv2 uses; see the [upstream 3.2 command](https://github.com/tmux/tmux/blob/3.2/cmd-new-session.c). This check applies to spawn and resume, not observing or stopping existing agents. Unrecognized development and OpenBSD version strings are left to tmux itself.

mv2 runs new agents on `~/.multiverse/run/tmux.sock`, or on the socket in `MV2_TMUX_SOCKET`. `MV2_RUN_DIR` overrides the private run directory and also moves new native bridge sockets. `spawn` and `resume` use the current selection; existing agents stay at their saved addresses. Children inherit their verified parent's current server and run directory. Discovery also checks the old `tmux -L multiverse-v2` server, so an upgrade sees agents still running there.

If a cleaner removes the socket or its folder while tmux is still running, mv2 reconnects to that server. It verifies the saved PID, process start time and executable before sending `SIGUSR1`, recreates missing folders privately, and checks that the server answers. Starting or recovering a server shares a lock, with a bounded wait of 30 seconds. A healthy server whose PID and start time match the registry can be observed or joined without waiting on that lock; joining never starts a replacement server. If an unrecognized build rejects the join-only flag (`-N`), mv2 also refuses the launch with the tmux 3.2 requirement; it never falls back to a command that could start a second server. The server PID and start time returned when a session is created stay attached to its pane operations. If another server takes the socket, tagging, status setup and cleanup refuse even when that server has reused the same pane or session number. A proven conflict after a session was created removes only that new session, guarded by the replying server’s PID and start time; it never removes another server’s sessions. A different server answering at the same path is a conflict: mv2 names both PIDs and sends no signal.

If a file blocks the socket folder, mv2 names the file and leaves it untouched. Move it aside, then repeat the command. For a recorded live server, that command retries recovery of the same server; it does not replace it.

If recovery fails, the agent stays `unknown`, and the error names the socket and the process to check. Follow its manual recovery command only after verifying that PID still names the original tmux server. Do not remove `runtime.json` or start another server at the same path. Saved failed messages still need an explicit `mv2 deliver NAME --retry` or `--dismiss`; recovery never resends them.

A server that answers remains usable when its process details are temporarily unreadable. mv2 saves no new signal authority until it can verify those details; a missing socket still requires the full process proof before recovery.

mv2 keeps server identities under `~/.multiverse/tmux-servers/`. Older launches identify a healthy server by their exact saved pane, session and launch tag. If that pane is absent, an active launch must prove its server through its runner; a registry entry learned from another server cannot replace that proof. If that runner may be alive but its process identity cannot be read, mv2 refuses to start a replacement server until the inspection succeeds. If both the registry and socket are deleted, a new universe with no launch records cannot discover that old server; restore its address before spawning there.

A runner observed as a zombie has exited, even if its older record has no process start token. A live or unreadable runner without that token remains unverified and cannot authorize a replacement or a signal.

When a locked command finishes, it removes that socket's registry entry only if the recorded server is proven gone, and removes the matching lock too. A lock without a record is also removed. It keeps live, uncertain or damaged records and does not sweep other sockets' entries.

A missing native bridge socket has a different remedy: once that launch has been ready, if its recorded process is still alive, stop and resume that agent to recreate the socket. A bridge still starting may not have created its socket yet; mv2 does not call that a lost connection. A working alternate delivery path remains usable. Codex's shared daemon socket is separate from this launch-local socket.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `error connecting to PATH (Permission denied)`; LIVE `unknown` | The socket belongs to another user, or a sandbox blocks Unix sockets | Run mv2 as the user who owns the server, or allow the socket in the sandbox |
| `error connecting to PATH (File name too long)` | Unix socket paths are limited to about 100 characters | Use a shorter `MV2_TMUX_SOCKET`; for a native socket, set a shorter absolute `MV2_RUN_DIR` |
| `tmux ls` doesn't list the agents | They are on mv2's server, not your default one | `mv2 attach NAME`, or `tmux -S "${MV2_RUN_DIR:-$HOME/.multiverse/run}/tmux.sock" ls`; older launches use `tmux -L multiverse-v2 ls` |
| `server exited unexpectedly` on spawn, and the server's other sessions are gone | `MV2_TMUX_SOCKET` names a server with a global `window-size manual`; tmux 3.6a crashes on any new session there, with or without mv2 | Use mv2's own server, or unset `window-size` on yours (`tmux set -gu window-size`) |

### The dashboard doesn't show your agents

- **It opened another universe.** `mv2` uses `-u PATH`, then `MV2_UNIVERSE`, then the nearest `.multiverse` at or above the current directory. Inside an agent's terminal, `MV2_UNIVERSE` points at that agent's universe. Run `mv2 -u PATH`.
- **It made a new, empty universe.** Running `mv2` with no command, in a directory with no universe above it, creates one there. Open the right one with `mv2 -u PATH`. Delete the stray `.multiverse` if you don't want it.
- **It opened a subverse.** Opened at a subverse, the dashboard shows that subverse and the ones below it, but never its parent. Open the root universe instead.
- **It shows an error row.** An agent whose record can't be read shows `unknown`, and `mv2 doctor` names the problem.

The dashboard refreshes every 1.5 seconds, and F5 refreshes it at once. `mv2 list --recursive` shows the same agents.

### The dashboard says "mv2 changed"

The header shows `mv2 changed: reopen`, or an action fails with `mv2 changed since this dashboard started; quit and reopen it`, followed by the import or attribute error it hit.

mv2's source changed on disk after the dashboard started, for example from `mv2 update` or a `git pull`. The dashboard keeps running the code it loaded and loads the rest from disk when it first needs it, so an action can pair new code with old and fail with an error the new code would not give. Press `q` and run `mv2` again. Agents keep running.

### `mv2 update` refuses

`mv2 update` prints `mv2 was not fully updated: REASON`. `mv2 update --check` shows the same refusal without changing anything.

| Reason | Fix |
| --- | --- |
| `The mv2 checkout has uncommitted changes to tracked files; commit them before updating.` | Commit or stash your changes in the mv2 checkout, then update. Untracked files don't count. |
| `... has diverged from ...`, `... is ahead of ...` | Your branch has its own commits. Push or rebase them, then update. |
| `... has no upstream` or `no current branch (detached HEAD)` | Check out a branch that tracks a remote. |

After an update, running agents and the dashboard keep the old code until you relaunch them.
