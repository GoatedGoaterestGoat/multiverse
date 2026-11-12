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
| With `-u PATH` | That universe or subverse, or the workspace containing its `.multiverse`; it must exist |

Put `-u` before the command: `mv2 -u ~/other-project/.multiverse list`.

An explicit `-u` path never falls back to an enclosing universe. An empty value, a missing path, an ordinary subfolder or an abandoned subverse is refused before the command runs or migrates settings. If the selected universe disappears during the command, later reads fail instead of switching to its parent. Without `-u`, discovery from the current directory or `MV2_UNIVERSE` still searches parent directories.

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

mv2 bundles two templates. `base` has the coordination and mv2-help skills, a help command, a review workflow, and Claude, Codex and Pi harness definitions, with default models for Claude (`claude-opus-5-5`) and Codex (`gpt-6.1-sol`); `mv2 init` and `mv2 subverse` use it unless you pass `--template`. `Silicon23` is a team template: leads own the high-level direction of an area and implementers own the details of their features, as its `team` skill describes. It has Claude and Codex harnesses that skip their permission prompts (`--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`), with the same default models, and that allow only Fable 5.2 and Opus, Sonnet and Haiku 5.5 for Claude and Astra, Sol, Terra and Luna for Codex (their `models`; a model's aliases count too). Its dashboard leaves out the Live column and gives the models short names, such as `Opus` or `Sol` (its harnesses' `model_names`).

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

If a recovered agent's folder name differs from its stored ID, `doctor` checks the inbox in that existing folder. It does not create a second agent folder under the stored ID. If an older doctor already created a recordless folder there, it reports that leftover and still checks the real inbox. When that folder's recovery address is ambiguous or cannot be verified, the advice names its full path for manual inspection instead of suggesting `mv2 remove` with the conflicting ID. Shortening a conflicting ID does not resolve that ambiguity.

It answers in sentences: `No problems.`, or a list of what's wrong. It exits non-zero when something is wrong. It doesn't check `harness.json`; `mv2 capabilities --harness NAME` does that. See [troubleshooting](../use/troubleshooting.md).

## Files

[Files](../reference/files.md) lists everything in `.multiverse/`. It says which files you edit, which ones mv2 owns, and what fields they have.
