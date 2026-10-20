# Concepts

These are the words the rest of the docs use. The guides assume you know them, so read this page once.

## Universe

A universe is a `.multiverse/` directory in a project. It holds two kinds of files:

- **Definitions** you can read and edit: the agent prompts, harness settings and dashboard view.
- **State** that mv2 writes: one directory per agent, with its record, inbox and launch history.

A universe belongs to its project folder, called its **workspace**. Agents start in the workspace. mv2 looks for the universe in the current directory and then in each parent, so `mv2` inside a project uses that project's universe. Use `-u PATH` to pick another one.

## Subverse

A subverse is a universe nested inside another, at `subverses/NAME/`. It has its own definitions, agents and dashboard view. It can also point at a different workspace, such as a Git worktree. Use one when a group of agents needs its own setup, such as a review team, a test department or an experiment.

The dashboard shows subverses as folders. Subverses nest to any depth.

## Agent

An agent is a coding-agent session that mv2 started and tracks. It is a Claude Code, Codex or Pi process running in its own tmux pane. Each agent has an ID and a name that is unique in its universe.

Stopping an agent keeps its record and conversation, so `mv2 resume NAME` continues where it left off. Agents keep running when you close the dashboard.

## Harness

A harness is the agent program and mv2's adapter for it: `claude`, `codex`, `pi`, or a custom command. Each universe configures its harnesses in `harnesses/NAME/harness.json`. Codex is the default.

## Live connection and launch-only

mv2 never types into an agent's terminal. It works with a running program in one of two modes:

- **Live connection.** mv2 talks to the program through the program's own API: a Claude Code plugin, the Codex app-server, or a Pi extension. Messages arrive in the running session, and the dashboard shows whether the agent is working, waiting for you or idle.
- **Launch-only.** mv2 only starts, stops and resumes the program in its tmux pane. Messages wait in the inbox until the agent checks it or is resumed.

Claude Code and Codex connect whenever the installed version allows it. Otherwise they fall back to launch-only and show the reason. Pi connects only when you turn the connection on. See [harnesses](use/harnesses.md).

## Messages and the inbox

Every message is saved to the recipient's inbox first, and then delivered. You choose how it is delivered:

| Delivery | Arrives |
| --- | --- |
| `steer` (default) | During the current turn at the next safe point, or as a new turn if the agent is idle |
| `follow-up` | When the current turn ends |
| `inbox-only` | Not delivered. The agent reads it with `mv2 inbox` at its next check |

A `steer` or `follow-up` message to a stopped agent resumes it (with a harness that supports waking: Claude Code, Codex and Pi; a custom command harness doesn't), with the message as its first prompt. A saved or delivered message has not necessarily been read or answered.

An agent that waits on your answer can **focus**. While it is focused, it refuses `steer` and `follow-up` messages, so nothing buries its question, and `inbox-only` messages still arrive. See [focus an agent](cli/messaging.md#focus-an-agent). Agents send messages with mv2's commands, described in [messaging](cli/messaging.md).

## Agent state

The dashboard and `mv2 list` show three separate things:

| Field | Comes from | Values |
| --- | --- | --- |
| **Live** | tmux and the process | `alive`, `ending`, `stopped` (exited, pane kept), `dormant` (no pane), `unknown` |
| **Status** | The harness, over a live connection | `working`, `waiting` (needs you: a permission prompt or question), `idle`, `disconnected`; `unknown` when launch-only |
| **Phase** and **summary** | The agent itself, with `mv2 report` | `starting`, `working`, `blocked`, `done`, plus a one-line summary |

mv2 never guesses state from terminal text. When it has no reliable source for a field, it shows `unknown`.

## Templates

A template is a starting set of definitions. `mv2 init` and `mv2 subverse` install one: the bundled `base` (the default) or your own in `~/.multiverse/templates/NAME`. After that, the universe owns its copy. `mv2 refresh` brings in template updates for files you haven't edited. See [subverses](use/subverses.md) and [prompts](cli/prompts.md).

## Prompts: bootstrap, skills, workflows and commands

mv2 adds as little as possible to an agent's context:

- **Bootstrap** (`bootstrap.md`): a few lines every agent gets at launch, telling it how to use mv2.
- **Skills, workflows and commands**: longer prompts that stay on disk. `mv2 catalog` lists them. An agent or a person loads one by name with `mv2 command NAME`, or with `/multiverse NAME` inside an agent session.

Agents use mv2 through their ordinary shell tool, as `"$MV2_CLI"`. mv2 adds no model tools and runs no server of its own.

## Plugins

Plugins add features to mv2: dashboard columns and panels, commands, status-line items, and options for how harnesses launch. Tree compaction and the remote control are plugins. Each universe turns plugins on or off for itself. See [plugins](use/plugins.md).

## tmux

Every agent runs in a pane on mv2's own tmux server, separate from your other tmux sessions. Each pane carries hidden tags with the universe, agent and launch IDs, so renaming a window never confuses mv2. You can attach to any agent from the dashboard and detach with `Ctrl-b d`.
