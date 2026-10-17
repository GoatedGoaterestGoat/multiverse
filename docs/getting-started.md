# Getting started

Install mv2, start your first agent, work with it, and stop it. This takes about ten minutes.

## What you need

- Linux or macOS, Python 3.11 or newer, and tmux.
- At least one agent program, installed and logged in: [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex) or [Pi](https://github.com/earendil-works/pi).

Tested versions are listed in [compatibility](reference/compatibility.md).

## Install

```sh
git clone https://github.com/GoatedGoaterestGoat/multiverse.git
cd multiverse
uv sync
./bin/mv2 --help
```

`./bin/mv2` uses the checkout's environment, so there is nothing to activate. To have `mv2` on your PATH instead, run `pip install .` in the checkout.

To look around before using real agents, run the demo. It shows sample agents and needs no tmux or model account:

```sh
.venv/bin/python examples/tui_demo.py
```

## Open the dashboard

```sh
cd ~/my-project
mv2
```

The first time you run it in a project, mv2 creates `.multiverse/` there and opens the dashboard. No agent starts until you start one.

## Start an agent

Press `n`, type a name, press `Tab` to choose a harness, and press `Enter`. The agent appears in the list as `starting`, then `working`.

![Creating an agent with n: name, harness, Enter](media/create-agent.gif)

## Work with it

Press `Enter` on the agent to open its terminal. This is the normal Claude Code, Codex or Pi screen, so give it a task as you usually would. Press `Ctrl-b d` to return to the dashboard; the agent keeps working.

![Attaching to an agent, asking it something, and returning with Ctrl-b d](media/attach.gif)

Each row in the dashboard shows whether the agent is working, idle or waiting for you, and what it last reported. Press `p` to watch an agent's terminal beside the list without leaving the dashboard.

## Let it bring in help

Agents can start and message other agents with mv2's commands. Ask yours in plain words:

```text
Start a reviewer agent to check your work, then fix what it finds.
```

The new agent appears in the dashboard as soon as it starts.

## Stop and resume

Press `K` to stop an agent; mv2 keeps its conversation. Press `r` to resume it where it left off, even days later. Quitting the dashboard with `q` doesn't stop any agents.

## Next

- [Concepts](concepts.md): universes, subverses, harnesses and agent states.
- [Dashboard](use/dashboard.md): the keys you will use most, with clips.
- [Working with agents](use/agents.md): giving agents work and letting them coordinate.
- [Subverses](use/subverses.md): keeping teams separate, with a complete example.
- [Commands](cli/README.md): what your agents run, and how to script mv2.
