# Universe commands

Create universes and subverses, choose templates and check a universe from a shell. For subverses in the dashboard and a full example, see [subverses](../use/subverses.md).

## Create a universe

```sh
mv2 init ~/my-project
mv2 init ~/my-project --template qa --name my-project
```

`init` installs a template into `DIRECTORY/.multiverse`, where `DIRECTORY` defaults to the current directory. It prints `Created universe NAME in PATH.` (`--json` prints the record) and refuses if a `.multiverse` is already there. Running `mv2` with no command, in a folder without a universe, does the same with the `base` template and then opens the dashboard. Neither command starts any agents.

## Choose which universe a command uses

| Where you run mv2 | Universe used |
| --- | --- |
| Inside an agent session | The agent's own universe |
| Anywhere else | The nearest `.multiverse/` in the current directory or a parent |
| With `-u PATH` | That universe or subverse, which must exist |

Put `-u` before the command: `mv2 -u ~/other-project/.multiverse list`.

## Subverses

```sh
mv2 subverse review
mv2 subverse tests --template qa
mv2 subverse feature-x --workspace ~/worktrees/feature-x
mv2 -u .multiverse/subverses/review spawn --name reviewer --task 'Review the open PR.'
```

A subverse is a universe at `subverses/NAME/` with its own definitions, agents and view. `subverse` prints `Created subverse NAME in PATH.`; `--json` prints the record. It uses its parent's workspace unless `--workspace` points somewhere else, such as an existing Git worktree. mv2 doesn't create the worktree for you. A name can use letters, numbers, `_` and `-`, up to 64 characters.

Subverses don't inherit their parent's prompts after creation. Each one gets a clean copy of its template. To move or remove one, use `mv2 move --subverse` or `mv2 remove --subverse`; see [agents](agents.md#move-and-remove).

## Templates

mv2 bundles one template, `base`: the coordination and mv2-help skills, a help command, a review workflow, and Claude, Codex and Pi harness definitions. `mv2 init` and `mv2 subverse` use it unless you pass `--template`.

`--template` takes a directory path or a name. mv2 looks a name up in `~/.multiverse/templates/NAME` first, then in `src/multiverse/templates/`. A template is a directory with a `universe.json` at its top (name, workspace and description) and the definition files you want: `bootstrap.md`, `skills/`, `harnesses/` and so on. It can't contain symlinks. To make your own, such as the `qa` template above, copy `src/multiverse/templates/base/` to `~/.multiverse/templates/qa` and edit it; [templates](../develop/templates.md) explains the layout. Templates are trusted files, since their harness definitions can run programs.

## Prompts

`mv2 catalog` lists a universe's skills, workflows and commands. `mv2 command NAME` prints one of them. See [prompts](prompts.md).

## Update

The [updating](../use/updating.md) page covers `mv2 update` and `mv2 refresh`.

## Check a universe

```sh
mv2 doctor
```

`doctor` checks the following in each universe and subverse:

- Its workspace exists.
- Its prompts load.
- Its plugins and their settings are usable.
- Every inbox is readable.
- No agent has a failed launch, a lost connection or an unknown state.

It answers in sentences: `No problems.`, or a list of what's wrong. It exits non-zero when something is wrong. It doesn't check `harness.json`; `mv2 capabilities --harness NAME` does that. See [troubleshooting](../use/troubleshooting.md).

## Files

[Files](../reference/files.md) lists everything in `.multiverse/`. It says which files you edit, which ones mv2 owns, and what fields they have.
