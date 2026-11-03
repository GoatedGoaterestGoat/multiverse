# Run a harness command

`mv2 slash` runs a command in a running Claude agent with a live connection. Name the command without its slash; an optional leading slash is also accepted.

```sh
mv2 slash worker --list
mv2 slash worker compact
mv2 slash worker model sonnet
mv2 slash worker effort high
```

The live list includes the person's plugins, skills and MCP prompts. It shows commands mv2 refuses and those that need arguments. A bare command that opens a picker, such as `model`, is refused. Codex, Pi and agents without a live command connection report this operation unavailable.

Shell words after the command are joined with single spaces. Quote all arguments as one word to preserve their spacing. A display-only command name containing spaces is left out of the list.

## Wait for the result

The command joins the harness's queue and runs when the agent is idle. It does not interrupt a turn. Commands and follow-ups keep the order in which the harness receives them.

```sh
mv2 slash worker compact --timeout 30
mv2 slash worker compact --no-wait
mv2 slash worker --result RUN_ID
```

The default wait is 600 seconds. A timeout or `--no-wait` returns the run ID. The command stays queued and may still run. Read its result with `--result`; submitting the command again would run it twice. There is no cancellation after submission.

Core also settles completed receipts when it next lists or resumes the agent, so a completed model or effort change is retained even when you did not wait. Explicit model and effort options on resume take precedence. At most 128 commands can remain pending; read their results before submitting more.

If a model or effort result is still unreadable or unconfirmed, resume (including a wake) refuses rather than restoring a possibly stale setting. The refusal names the run ID: read it with `--result`, or give that setting explicitly on resume. A model command needs an explicit `--model`; an effort command needs `--effort`. Listing, sending and killing remain available.

A completed command prints its output, limited to 64 KiB. A skill or prompt command reports that it expanded into a prompt; the agent's answer is its next turn. Hidden model notes are not returned. Add `--json` for the state, run ID, output and recorded changes.

If the original launch is proven stopped and no result was written, the run becomes `lost`. An unreadable receipt remains unconfirmed. Reading a result never resends the command.

## Commands mv2 manages

Use `mv2 kill` instead of `exit` or `quit`, and `mv2 resume` instead of `resume`. For Claude's `restart`, use `mv2 kill` followed by `mv2 resume`. Change authentication outside the running agent. Changes to mv2's own Claude plugin and its `multiverse` command are refused; use mv2's commands directly.

An agent or plugin cannot run a command on a [focused agent](messaging.md#focus-an-agent). A person can. Other plugins' commands remain available when the harness lists them.

Commands that save the person's Claude defaults, including `model`, `effort`, theme and plugin changes, run only when a person calls them. The list marks them, and a successful result says the choice was also saved as the person's Claude default. Calls from agents and plugins are refused before submission. To change only one agent's model or effort, stop it and use `mv2 resume AGENT --model X --effort Y`.

`clear` follows the new conversation through mv2's session lineage before reporting success. A successful `model` or `effort` result records the harness's read-back settings, so listing, inspection and the next resume use them.

Command requests and outcomes are kept in the agent's `slash/` folder so `--result` can read them later. The run and its events identify the original caller: a person, agent or plugin. The event journal records the command name and state, never its arguments or output. See the [adapter contract](../design/harness-commands.md) for provider details.
