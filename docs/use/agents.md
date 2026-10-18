# Working with agents

Start agents, give them work, let them bring in help, and keep track of them. All of this happens in the [dashboard](dashboard.md) and in each agent's own terminal.

## Start an agent and give it work

1. Select the folder it should live in and press `n`.
2. Type a name that says what it's for, such as `reviewer` or `kernel-bench`, choose a harness, and press `Enter`.
3. Press `Enter` on the new agent to open its terminal, and give it a task as you would in Claude Code, Codex or Pi on their own.
4. Press `Ctrl-b d` to return to the dashboard. The agent keeps working.

When an agent starts, mv2 gives it a short note saying that it is part of a team and how to reach the other agents.

## Let agents bring in help

Agents can start other agents, message them, and wait for their results, using mv2's commands from their shell. Ask in plain words:

```text
Split the test failures into three groups and start one agent per group.
Tell me when they're all done, with a one-line result each.
```

```text
Start a reviewer agent on Codex to check your change before you push.
Send it the diff and fix what it finds.
```

```text
Ask kernel-bench for its latest numbers and use them in the report.
```

New agents appear in the dashboard as soon as they start. Open any of them to see what it is doing or to change its instructions.

## Keep track of them

| You see | It means |
| --- | --- |
| `alive` · `working` | Running, in the middle of a turn |
| `alive` · `idle` | Running and waiting for its next instruction |
| `alive` · `waiting`, with an orange `●` | It needs you, usually for a permission prompt or a question. Open its terminal to answer |
| `stopped` or `dormant` | Not running. Its conversation is saved, and `r` resumes it |
| Phase and summary | What the agent last reported: `working`, `blocked` or `done`, and one line about it |

The header counts live agents and agents waiting for you, so you can leave the dashboard open in a corner and glance at it. For a closer look without attaching, press `p` for the inspector.

`working` and `waiting` need a live connection to the harness; see [harnesses](harnesses.md). Without one, the status is `unknown`.

## Keep a question on top

An agent that asks you something focuses itself before it ends its turn. A focused agent shows a `◉` after its name. Other agents can't interrupt it until you answer, so their messages wait in its inbox.

Press `f` on an agent to focus it yourself, for a time or until its next turn ends. Press `f` again to unfocus it. [Focus an agent](../cli/messaging.md#focus-an-agent) explains what a focus refuses.

## Stop, resume and clean up

- **`K`** stops an agent after you confirm. Its conversation stays on disk.
- **`r`** resumes it in the same conversation, even days later.
- **`x`** moves a stopped agent: press `x`, select the destination, and press `Enter`.
- **`d`** moves a stopped agent to the universe's trash after you confirm.

A message from another agent also wakes a stopped agent, so a team can pick up where it left off without you resuming each one.

## Choose a harness

Each agent runs one harness: Claude Code, Codex, Pi, or a command you configure. You pick it when you create the agent. [Harnesses](harnesses.md) compares them and explains their settings.

## When something goes wrong

If an agent shows `failed` or `unknown`, select it and press `p`. The inspector shows the error and the last lines of its terminal. [Troubleshooting](troubleshooting.md) lists the common causes.
