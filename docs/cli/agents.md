# Agent commands

Start, find, watch, stop, resume, move and remove agents from a shell. Agents run these commands to manage their helpers. You can run them in scripts too. The [dashboard](../use/dashboard.md) has a key for each of the same actions.

## Start an agent

```sh
mv2 spawn --name reviewer --harness claude --task 'Review the current diff.'
mv2 spawn --name tester --harness claude --model sonnet --task-file brief.md --wait
```

| Option | Meaning |
| --- | --- |
| `--name` | Must be unique in the universe, counting stopped and failed agents |
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

Wherever a command takes an agent, you can give its name, its full ID or an unambiguous ID prefix. If a name matches more than one agent, the command fails and lists their IDs.

## Watch an agent

```sh
mv2 inspect worker            # key facts and the last 30 terminal lines
mv2 inspect worker --lines 100
mv2 attach worker             # open its terminal
```

Press `Ctrl-b d` to detach from an attached terminal; the agent keeps running. [Concepts](../concepts.md#agent-state) explains the live, status and phase fields.

## Stop and resume

```sh
mv2 kill worker
mv2 resume worker
mv2 resume worker --model opus --wait
```

`kill` (alias `stop`) cancels the agent's current work, ends its process and closes its pane. Its record, inbox and native conversation are kept, so `resume` continues the same conversation. You can resume an agent that is `stopped` or `dormant`.

`kill --attachment-only` closes only mv2's terminal and connection. The conversation's work keeps running, which matters when another client is working in a shared Codex conversation.

A `steer` or `follow-up` message to a stopped agent also resumes it, with the message as its first prompt. An `inbox-only` message never wakes an agent.

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

Stop agents before you move or remove them. A subverse can move only when all its agents are stopped. A moved agent keeps its inbox and conversation.

A removed agent or subverse goes to `trash/` with a record of where it came from. Moving it back restores it. Top-level universes can't be moved or removed this way.

## What an agent gets

At launch, each agent gets:

- **Bootstrap:** a short note in its system prompt on how to use mv2, taken from the universe's `bootstrap.md`.
- **Its first prompt:** the task, if one was given.
- **Environment variables:** `MV2_CLI` (the mv2 command to run), `MV2_UNIVERSE`, `MV2_AGENT_ID` and a few more; see [environment](../reference/environment.md).

Agents run mv2 through their shell as `"$MV2_CLI"`. Inside an agent, commands default to the agent's own universe and identity. An agent can therefore run `send`, `inbox`, `report`, `list` and `spawn` without extra options.

## When a launch fails

A launch that is still preparing isn't failed for being slow. mv2 waits while preparation makes progress, up to 60 seconds without progress and 180 seconds in total.

The agent's record stays, marked `failed`, with the error. The error includes the last lines of the agent's terminal. `mv2 list`, `mv2 inspect` and `mv2 doctor` all show it. Fix the cause, then run `mv2 resume NAME`.

If the launch never got as far as a native session, `resume` says so. In that case, remove the failed record with `mv2 remove NAME` to free its name, then spawn again. See [troubleshooting](../use/troubleshooting.md).
