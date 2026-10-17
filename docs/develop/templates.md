# Templates

This page explains how to write a template and the prompts it carries: the bootstrap, skills, workflows and commands. To install and update templates, see [universes](../cli/universes.md).

## Layout

A template is a directory shaped like a universe. The bundled ones are in `src/multiverse/templates/`.

```text
my-template/
  universe.json          # required
  bootstrap.md           # required to launch agents
  skills/NAME/
    entry.json
    SKILL.md
  workflows/NAME/
    entry.json
    prompt.md
  commands/NAME/
    entry.json
    prompt.md
  harnesses/NAME/
    harness.json
    README.md            # notes for agents using this harness
  views/default.json     # dashboard title, accent, columns and widgets
  plugins.json           # which plugins are on here
  plugin-settings/       # each plugin's settings, e.g. compaction.json
  statusline.json        # Claude status line and tmux bar items
  artifacts/             # any other files, such as blank report forms
  subverses/NAME/        # nested universes, installed with the template
```

`universe.json` needs `"schema": 1` and a `name`, and `description` is optional. On install, mv2 sets `id`, `created_at`, `template` and `workspace`. It also gives the top universe the name you chose.

Each skill, workflow and command directory needs an `entry.json` with a one-line description:

```json
{"description": "Write release notes from merged commits."}
```

mv2 reads only `description`. A directory without `entry.json` is not in the catalog and can't be loaded. Harnesses are listed from the `description` in `harness.json`. [Files](../reference/files.md) lists its fields, and [harness adapters](harness-adapters.md) explains what they control.

## Skills, workflows and commands

| Kind | File | Use for |
| --- | --- | --- |
| Skill | `SKILL.md` | Knowledge an agent loads when a task needs it, such as how to coordinate agents |
| Workflow | `prompt.md` | A procedure or role an agent follows step by step |
| Command | `prompt.md` | A short action a person starts with `/multiverse NAME` |

mv2 treats the three alike apart from the file name. The bundled `SKILL.md` files start with `name` and `description` front matter. mv2 doesn't read it, and `mv2 command` prints it with the rest.

## How agents find them

```sh
"$MV2_CLI" catalog                          # skills, commands, workflows, then harnesses
"$MV2_CLI" command release-notes            # print a prompt
"$MV2_CLI" command workflows/release v1.2   # qualified name, with arguments
```

`catalog` lists every `entry.json` and `harness.json` in the agent's own universe, with its description and path. `--json` gives the same list for scripts. It doesn't look in parent universes, because each subverse has its own definitions.

`command NAME` prints the matching `SKILL.md` or `prompt.md` and runs nothing. Arguments after the name are appended as a `User arguments:` section. `NAME` must match exactly one skill, workflow or command, either by directory name or as `KIND/NAME`. When two kinds share a name, the bare name is refused and the qualified one works.

In Claude and Pi sessions with a live connection, `/multiverse NAME` loads the same prompt. See [messaging](../cli/messaging.md#inside-claude-and-pi-sessions).

## The bootstrap

Every launch gets `bootstrap.md` plus identity lines, built by `catalog.bootstrap()`:

```text
<bootstrap.md, trimmed>

Universe: NAME (UNIVERSE_ID)
Agent: NAME (AGENT_ID)
Universe files: /path/to/.multiverse
Your state: /path/to/.multiverse/state/agents/AGENT_ID

<the harness definition's own bootstrap text, if it has one>
```

Codex's definition adds a line telling the agent to run `session-bind`. The result goes to the harness as appended system text (Claude, Pi) or developer instructions (Codex). It is also saved as `launches/LAUNCH_ID/bootstrap.md`.

A launch keeps the bootstrap it started with, so an edited `bootstrap.md` reaches a running agent only when it is resumed. Skills, workflows and commands are read when loaded, so edits to them take effect at once.

## Write for agents

Agent-facing text follows [the docs style](docs-style.md), with one more rule. Every word costs context, in every session that reads it.

- **Keep the bootstrap tiny.** It goes into every launch. The base bootstrap is about 180 words. `tests/test_templates.py` fails when the bundled development or testing bootstrap reaches 300 words with its identity lines.
- **Load the rest on demand.** Put detail in skills and workflows, and name them in the bootstrap so agents know when to load them.
- **Make the description enough to decide.** An agent sees only the `entry.json` description before it loads a prompt. Say what the prompt does and when to use it.
- **Give instructions.** Say what to run, what to check and what to report. Leave out how mv2 works inside.
- **Use `"$MV2_CLI"`**, quoted, rather than `mv2`, because it is the CLI that launched the agent. Point to `"$MV2_CLI" COMMAND --help` rather than repeating options.
- **Write control flow as prose.** No scheduler runs a workflow. The model reads it and decides.

## A minimal skill and workflow

`skills/release-notes/entry.json`:

```json
{"description": "Write release notes from merged commits."}
```

`skills/release-notes/SKILL.md`:

```text
List commits since the last tag with `git log --oneline $(git describe --tags --abbrev=0)..HEAD`. Group them as Added, Changed and Fixed, one line each. Include only changes users notice; skip refactors, tests and docs.
```

`workflows/release/entry.json`:

```json
{"description": "Prepare a release: notes, version bump and a review."}
```

`workflows/release/prompt.md`:

```text
1. Load the release-notes skill with `"$MV2_CLI" command release-notes` and draft the notes.
2. Bump the version in `pyproject.toml`.
3. Ask a reviewer agent to check the diff and the notes, and wait for its answer in your inbox.
4. Report the notes, the review and anything left open to whoever asked.
```

An agent finds both in `"$MV2_CLI" catalog` and loads the workflow with `"$MV2_CLI" command release v1.2`.

## User templates

Put a template in `~/.multiverse/templates/NAME/` and install it by name:

```sh
mkdir -p ~/.multiverse/templates
cp -R src/multiverse/templates/base ~/.multiverse/templates/mine   # from an mv2 checkout
mv2 init ~/my-project --template mine
mv2 -u ~/my-project subverse review --template mine
```

`--template` tries an existing directory path first, then `~/.multiverse/templates/NAME`, then a bundled template. A path to an existing universe also works. It copies that universe's definitions without its agents.

An install copies everything except `state/`, `trash/`, `__pycache__/` and `.git/`. It refuses templates that contain symlinks. Every universe in the template, including nested subverses, gets a fresh ID and empty state. Templates are trusted files, because a harness definition runs programs.

## How refresh treats template files

Install writes `template-files.json` in each new universe. It records the template's name, where this universe sits in it, and a SHA-256 fingerprint of each definition file. Definition files are all files except `universe.json`, `template-files.json`, `state/`, `trash/`, `subverses/`, dotfiles and `__pycache__/`.

`mv2 refresh` compares each file's recorded fingerprint, its current contents and the template's version. For a template author, that means:

| You change in the template | Where the user hasn't touched the file | Where the user changed it |
| --- | --- | --- |
| Edit a file | Updated by `refresh --apply` | Conflict: kept until the user runs `--take PATH` |
| Add a file | Added by `refresh --apply` | A file the user already has at that path is a conflict |
| Remove a file | Reported as removed; left in place | Reported; left in place |
| Rename a file | Old name left in place; new name added | Same |

A file the user deleted stays deleted. Files that exist only in the universe, such as a user's own skills, are never touched. Refresh finds the template by the name it recorded, the same way `--template` does. A user template is therefore refreshed from `~/.multiverse/templates/NAME`.
