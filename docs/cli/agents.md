# Agent commands

Start, find, watch, stop, resume, move and remove agents from a shell. Agents run these commands to manage their helpers. You can run them in scripts too. The [dashboard](../use/dashboard.md) has a key for each of the same actions.

## Start an agent

```sh
mv2 spawn --name reviewer --harness claude --task 'Review the current diff.'
mv2 spawn --name tester --harness claude --model sonnet --task-file brief.md --wait
```

| Option | Meaning |
| --- | --- |
| `--name` | Must be unique in the universe, counting stopped and failed agents, and cannot look like a UUID |
| `--harness` | A harness configured in this universe. The default is `codex` |
| `--task` / `--prompt`, `--task-file` | The first prompt. Leave it out to start the agent idle |
| `--model` | Override the harness's default model for this agent |
| `--wait`, `--wait-timeout SECONDS` | Return only once the agent can receive messages or has failed. The timeout defaults to 60 s |

Without `--wait`, `spawn` returns as soon as the launch is registered. Use `--wait` in scripts that send a message right after starting an agent. A timeout leaves the launch running, so check it with `mv2 inspect` before you retry.

To start an agent in a subverse, select the subverse with `-u`:

```sh
mv2 -u .multiverse/subverses/review spawn --name reviewer --task '...'
```

## Find and refer to agents

```sh
mv2 list               # this universe
mv2 list --recursive   # and every subverse
mv2 list --wide        # full summaries and errors
```

Each line shows the agent's name, state, reported phase and summary. Agents waiting for you come first.

Wherever a command takes an agent, you can give its name, its full ID or an unambiguous ID prefix. An exact full ID wins over an older agent's colliding name; use that older agent's own full ID to address it. An exact name wins over another agent's ID prefix. Leftover folders and unreadable identity records require their full folder ID and do not make a healthy agent's prefix ambiguous. Duplicate names and ambiguous prefixes are refused with the matching agents listed. Names may contain underscores; only the hexadecimal UUID forms, with or without hyphens, are reserved as IDs.

## Watch an agent

```sh
mv2 inspect worker            # key facts and the last 30 terminal lines
mv2 inspect worker --lines 100
mv2 attach worker             # open its terminal
```

Press `Ctrl-b d` to detach from an attached terminal; the agent keeps running. [Concepts](../concepts.md#agent-state) explains the live, status and phase fields.

`mv2 capabilities --agent worker` lists what that agent can do now, one line each, such as `steer: supported` or `ask: unavailable: REASON`. `--harness NAME` answers for a harness's launches instead; with `--probe` it also checks that the executable exists and, for Claude, its version. `--json` prints every record.

## Stop and resume

```sh
mv2 kill worker
mv2 resume worker
mv2 resume worker --model opus --wait
```

`kill` (alias `stop`) cancels the agent's current work, ends its process and closes its pane, then prints `Stopped NAME.` If mv2 couldn't confirm the work was cancelled, the line says so and why; `--json` prints the record. Its record, inbox and native conversation are kept, so `resume` continues the same conversation. You can resume an agent that is `stopped` or `dormant`. If a caller storage override is ignored to preserve the saved conversation, the resume summary explains it on a separate line (also with `--wait`); `--json` includes it in `notices`. A message that wakes the agent returns the same notice.

`kill --attachment-only` closes mv2's terminal and connection without requesting native cancellation. Work owned by a separate native service, such as a Codex bridge conversation, keeps running. Work owned by the terminal process ends when that process closes; the flag does not preserve it. You can also close a refused Codex terminal this way before a new `mv2 resume`, leaving the other client's conversation alone.

Before stop completes or a replacement terminal starts, mv2 waits for its old connections to close. If process identity cannot be verified, the error names the PID, command, start time and record path. Inspect those before retrying; do not kill a process just because it reused an old PID. Unclaimed reconnect workers expire after 60 seconds.

Linux launches record the process's start ticks and boot ID, so a wall-clock adjustment cannot change its identity. Older records can also prove PID reuse from their stopped or dormant time, or prove that the launch predates this boot. If inspection is unavailable, restore it first. If you independently verify that the old process has exited but its stale evidence still blocks recovery, back up the named record and remove its obsolete PID and start-token fields (or its obsolete cleanup or role record), then retry. Never discard evidence for a live or unverified process.

A `steer` or `follow-up` message to a stopped agent also resumes it (with a harness that supports waking: Claude Code, Codex and Pi; a custom command harness doesn't), with the message as its first prompt. An `inbox-only` message never wakes an agent.

For a plain Codex version whose startup screen mv2 recognizes, `--wait` also waits for that screen to settle. A native refusal fails with Codex's reason; a started session proceeds. If the bounded startup window or your explicit `--wait-timeout` ends without either, a healthy process that passed its startup check proceeds with a notice that screen startup could not be confirmed. A process that has not passed that check still times out. Other versions keep process readiness. Screen observations never bind a conversation or grant permissions.

## Move and remove

```sh
mv2 move worker review                     # into the subverse "review"
mv2 move worker review/verification        # into a nested subverse
mv2 move --subverse review ../other/.multiverse
mv2 remove worker                          # into this universe's trash/
mv2 remove --subverse review
mv2 trash                                  # list what's in the trash
mv2 trash --empty                          # delete it permanently
```

Stop agents before you move or remove them. A subverse can move only when all its agents are stopped. A moved agent keeps its inbox and conversation. `move` prints `Moved NAME to UNIVERSE (PATH).` or `Moved subverse NAME to PATH.`, and `remove` prints `Moved NAME to the trash.`, and `trash` lists one entry per line, such as `agent worker, removed 2026-10-18 00:30 UTC`. All three take `--json` for the records.

Move and remove use the same ownership checks when `runtime.json` is missing or
its launch identity is damaged. A tagged terminal, a live or unverified recorded
process, or incomplete terminal discovery refuses the operation and names the
file to repair. Restore the record or use `mv2 kill NAME` to close its verified
terminal first. A subverse operation checks every contained agent. Proven absence
allows the move or removal without rewriting the damaged runtime record.

A removed agent or subverse goes to `trash/` with a record of where it came from. Moving it back restores it. Top-level universes can't be moved or removed this way.

## What an agent gets

At launch, each agent gets:

- **Bootstrap:** a short note in its system prompt on how to use mv2, taken from the universe's `bootstrap.md`.
- **Its first prompt:** the task, if one was given.
- **Environment variables:** `MV2_CLI` (the mv2 command to run), `MV2_UNIVERSE`, `MV2_AGENT_ID` and a few more; see [environment](../reference/environment.md).

Agents run mv2 through their shell as `"$MV2_CLI"`. Inside an agent, commands default to the agent's own universe and identity. An agent can therefore run `send`, `inbox`, `report`, `list` and `spawn` without extra options.

## Damaged records

An old agent folder containing only plugin leftovers, with no `properties.json`,
`runtime.json` or `sessions.jsonl`, cannot reserve a name or native
conversation. It does not block spawns, session claims or resumes elsewhere.
This includes abandoned launch folders containing only bootstrap or task files,
with no request, ready marker, registration, cleanup or connection records.
List and doctor identify the leftover folder and offer `mv2 remove UUID` to move it to trash. Removal
checks that no terminal is tagged for that agent on the current servers before
moving the folder and its plugin leftovers to trash; unreadable servers or new
core records refuse the move. A refusal names any tagged pane that must be verified and closed first.
Any surviving core record or native startup evidence, or an error inspecting it,
keeps the usual recovery checks. A missing properties file does not block an
unrelated session claim when the surviving readable records cannot hold its ID.

While a launch is being prepared, both the text listing and `mv2 list --json`
show its live state as `starting`, including the short interval while its
terminal is being tagged and published. An unreadable terminal server is still
reported as unknown.

If an agent's `properties.json` or `runtime.json` cannot be read, list and inspect
show that agent with its file and reason. Other agents remain readable. Doctor
reports the damage as a problem and exits nonzero. Use the damaged row's UUID
when its name cannot be recovered. A folder with surviving core records still reserves its
UUID when properties.json is missing. Commands that would act on damaged
identity refuse until it is repaired; listing does not rewrite or reset it.

A missing runtime.json, or damaged launch authority inside it (such as an invalid
launch UUID or terminal socket, pane or session identity), is reported even though
the agent's name and harness remain readable. Before resume or wake, mv2 checks the launch's recorded and
current tmux servers for its exact universe, agent and launch tags, and checks
recorded process identities. A surviving terminal or process prevents a second
launch. An unreachable server names the discovery problem instead of guessing
that the agent stopped. If neither a terminal nor a process remains, resume
starts a new launch with a note naming the missing or damaged runtime record.
Unknown process identity is not proof of exit, and refusal leaves the damaged
record unchanged.

A valid-looking pane or socket can also be wrong. If it has no owned pane while
the recorded runner or child is still alive with a matching process start time,
list and doctor name `runtime.json` as inconsistent, and resume refuses. Kill
can find and close the uniquely tagged terminal for that exact saved launch;
it never adopts a different generation. A missing process-start token, a reused
PID, or failed process inspection does not establish that identity.

If damaged process fields prevent recovery, restore `runtime.json` from a
verified backup. Alternatively, after independently verifying that this agent's
terminal, runner and child are all gone, remove the damaged `runtime.json` and
resume. Deleting a record does not stop a process.

Kill can close a uniquely tagged terminal even when runtime.json is missing or
its launch authority is damaged.
Discovery skips an earlier launch whose request was never written; a malformed
or unreadable request still refuses recovery with its path and repair advice.
It never guesses a child PID or contacts a shared server from the conversation
UUID alone. Native cancellation uses only a validated generation-private relay
when available; otherwise the result says cancellation is unconfirmed. Other
clients and their server are not closed. A failed preparation keeps a named,
failed record that doctor reports and mv2 remove can clear. New agent folders
are published with complete minimal properties and a starting runtime record,
including during concurrent spawns. While the publishing process holds its launch
lock, list shows `starting`; it is not a damaged record. Doctor also accepts the
full starting record while that same launch is still preparing its terminal
under the lock. Once the lock is released, unexplained unknown state is reported. If preparation is
interrupted before any native startup, doctor reports the failed preparation and
the record can be resumed or removed. Unknown child evidence still requires
repair of the runtime record rather than assuming the process stopped.
Name and conversation claims skip a readable damaged peer only when its bytes
(and, for conversations, its lineage journal) do not mention the requested name
or UUID. A possible collision or unreadable evidence names the peer and refuses
until that evidence is repaired. The check is a conservative substring search,
so a damaged file may reserve a name mentioned elsewhere in that file too.
Unreadable peer evidence can block new names or conversation claims; it does not
prevent inspecting, stopping or messaging a healthy agent. Doctor also names an
agent whose recorded workspace folder has disappeared.
Doctor names each damaged harness definition, including unsupported schema
versions, and continues checking the other definitions and agents. Catalog and
command lookup skip damaged definitions with a named warning on stderr. Restore or remove a missing peer properties file before
claiming a name that mv2 cannot safely rule out.

## When a launch fails

Starting without `--wait` returns after setup; that command's normal exit does not fail the agent. A launch that is still preparing isn't failed for being slow. mv2 waits while preparation makes progress, up to 60 seconds without progress and 180 seconds in total.

The agent's record stays, marked `failed`, with the error. The error includes the last lines of the agent's terminal. `mv2 list`, `mv2 inspect` and `mv2 doctor` all show it. Fix the cause, then run `mv2 resume NAME`.

If the launch never got as far as a native session, `resume` says so. In that case, remove the failed record with `mv2 remove NAME` to free its name, then spawn again. See [troubleshooting](../use/troubleshooting.md).

Resume never replays the original task. In a custom harness recipe, `{task}`
and the contents of `{task_file}` contain only a wake message on resume, or are
empty when there is no wake message. The saved original task remains in the
agent's properties and original launch history.

A preflight failure prints `mv2: NAME did not start: REASON` (or `did not resume`
for resume). A failed native launch with a retained terminal shows LIVE stopped
and its error; a failure before terminal creation has no wrapper and is dormant
with the error. Inspect and doctor retain the reason in either case.
With `--json`, preflight refusals also return a record on stdout with `ok: false`,
the agent's name and the reason, and exit 1. Any saved failed record is retained.
