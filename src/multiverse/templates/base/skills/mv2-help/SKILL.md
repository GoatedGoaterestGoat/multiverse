---
name: mv2-help
description: Explain mv2 capabilities and configure universes, subverses, templates, skills, workflows, harnesses and views when helping a user set up or customize Multiverse.
---

# mv2-help

Help the user choose and implement a Multiverse setup that fits their work. Use the installed CLI and local definitions as the authority; template defaults and native harness support can change. For an explanation request, inspect and explain. For a setup request, make the requested changes and verify them. Creating a group does not require launching agents or assigning a manager.

## Discover the current setup

Use `"$MV2_CLI"` inside an mv2 agent; use `mv2` outside one. Run `--help` or `<action> --help` for installed syntax. The global `-u PATH` option selects a universe and goes before the action. Use an explicit `-u` when operating outside the agent's own universe so inherited `MV2_UNIVERSE` does not select the wrong scope.

Read the target's `universe.json`, `bootstrap.md` and relevant configuration files. Run `catalog` for local capability metadata and `list` for agents. Load only relevant bodies with `command skills/mv2-help`, `command workflows/review`, or another exact catalog name. `command` prints instructions; it does not execute them. `/multiverse list` and `/multiverse <name> [arguments]` are agent prompt conventions; some native CLIs intercept slash commands, so ordinary text or shell commands may be needed.

A universe is a directory containing definitions and runtime state, usually `PROJECT/.multiverse`. A subverse is an independent universe inside `subverses/NAME`, recursively. Use a subverse when a group needs its own configuration, workflows or workspace; use a skill for reusable instructions. Subverses inherit the parent's workspace at creation, but not its ongoing prompts, skill catalog or configuration. All agents in a scope share its mv2 definitions. Native harness skills and plugins are a separate layer; mv2 has no unified per-agent native skill selector.

## Create universes and subverses

Adapt these examples to the user's paths and names:

```sh
mv2 init /path/to/project --name project --template base
mv2 -u /path/to/project/.multiverse subverse tests
mv2 -u /path/to/project/.multiverse subverse dev --template qa --workspace /path/to/worktree
mv2 -u /path/to/project/.multiverse/subverses/tests catalog
```

`init` installs into `DIRECTORY/.multiverse` and refuses an existing destination. `subverse` installs into the selected universe's `subverses/NAME`; `--workspace` points at an existing project or worktree, it does not create a Git worktree. Names use 1–64 letters, numbers, underscores or hyphens and begin with a letter or number.

The one bundled template is `base`: coordination skill, mv2-help skill, help command, review workflow and Codex/Claude/Pi harness definitions. Other templates, such as `qa` above, are the user's own.

Inspect the chosen template for actual harnesses and defaults. Templates launch no agents and clone no runtime conversations. No manager is mandatory or automatically created. A template name resolves to `~/.multiverse/templates/NAME` before the bundled template; an explicit directory also works. Custom templates require `universe.json` and ordinary files, not symlinks. Fresh installs get fresh IDs, empty state and definition fingerprints.

## Configure the right files

Edit the selected scope's files, preserving unrelated values:

| File or directory | Purpose |
| --- | --- |
| `universe.json` | Identity, name, description, workspace and template provenance. Workspace paths are relative to this universe directory; preserve schema and ID. |
| `bootstrap.md` | Short instructions automatically supplied on launch. Keep detailed skills and workflows on disk. |
| `skills/NAME/entry.json` + `SKILL.md` | Catalog description and reusable agent instructions. |
| `commands/NAME/entry.json` + `prompt.md` | Catalog description and a named prompt. |
| `workflows/NAME/entry.json` + `prompt.md` | Catalog description and a task procedure interpreted by the agent. |
| `harnesses/NAME/harness.json` | Adapter choice and native launch/resume preferences. |
| `views/default.json` | Dashboard title, accent and columns. |
| `plugins.json` + `plugin-settings/NAME.json` | Which plugins are on here, and each one's settings (compaction: `plugin-settings/compaction.json`; an older `compaction.json` until `plugins migrate`). |
| `state/` | Runtime records and conversations; use mv2 operations rather than hand-editing them. |

To add a skill, create `skills/NAME/entry.json` containing `{"description": "When and why to use this skill."}` and put its instructions in `SKILL.md`. Commands and workflows use the same metadata with `prompt.md`. Discovery scans direct child directories with metadata; a lone `SKILL.md` is not cataloged. Resolve ambiguous names by kind, such as `skills/NAME`. Skill bodies are loaded on demand, not automatically injected. Each subverse needs its own installed definitions.

For a built-in harness, the current declarative shape is:

```json
{"schema": 2, "adapter": "codex", "description": "Codex agent", "settings": {}}
```

Use an existing installed definition as the starting point. Settings such as `args`, `spawn_args`, `resume_args` and `inbox_bridge` are adapter-specific; check CLI help and `capabilities --harness NAME --probe` before promising support. Do not silently weaken native sandbox or approval settings. Codex's unset bridge policy is auto, `true` requires the bridge and `false` selects plain mode. Claude and Pi bridges are opt-in. A changed harness definition is evaluated on the next launch/resume. Available native models, credentials, skills and plugins depend on the harness environment, not the mv2 catalog.

For simple view customization, use values such as `{"title": "Testing", "accent": "#cba6f7", "columns": ["name", "live", "phase", "summary"]}`. `accent: "auto"` assigns group colors. The root view controls the shared hierarchy's columns. Group title/accent can differ by subverse. Views display state; they do not schedule work.

For compaction requests, inspect `plugins` (is the compaction plugin on here?), the installed `plugin-settings/compaction.json` (or an unmigrated `compaction.json`), `compaction --help` and the target harness's capabilities. In the dashboard, `P` → compaction → Configure edits scope settings; an agent's scheduler can override the scope default. Do not assume every harness supports mv2 compaction or copy provider settings to another adapter.

## Update mv2 itself

`mv2 update --check` fetches the running package's source checkout and previews changes without merging or syncing. `mv2 update` fast-forwards the current branch from its upstream and runs `uv sync --locked` when `uv` and `uv.lock` are present, reporting when sync is skipped or fails. It refuses tracked, uncommitted changes, no upstream, and branches ahead of or diverged from upstream; untracked files are left alone. It never switches branches, rebases, resets or stashes, and cannot update a package installation without a Git source checkout. It uses the package's repository, regardless of the current directory or `-u`; both modes support `--json`. Running agents and the dashboard keep their old code until relaunched. Use `refresh --apply` separately to update a universe's installed definitions.

## Update templates and existing universes

Installed definitions belong to their universe. Editing a bundled or personal template affects future installs; it does not silently change existing universes. Preview existing-scope updates with:

```sh
mv2 -u /path/to/project/.multiverse refresh --diff
mv2 -u /path/to/project/.multiverse refresh --apply
```

`refresh` is a dry run unless `--apply` is supplied. Unedited files receive template updates; local edits are preserved and conflicting edits need an explicit `--take RELATIVE_PATH` to replace them. Without an install manifest, differing files are unknown and require explicit selection. New template files are added; locally deleted files remain deleted and upstream removals are reported but kept. Replaced files are backed up to `trash/`. `--recursive` handles each subverse against its own template source; `--template NAME_OR_PATH` compares against a different template. Refresh excludes identity, runtime state and subverses from the parent definition update. Skill/command/workflow files are available immediately; running agents keep their old bootstrap until relaunched.

## Verify and explain the result

Run `catalog` in each changed scope and load newly added prompts with `command`. Run `doctor` for basic workspace/configuration diagnostics; it does not prove every prompt or native setting works. Use `capabilities --agent NAME` and `inspect NAME` for an existing agent's actual support and launch state. Report the paths changed, template used, workspace selected and any capability that remains unverified.

For messages spanning several lines, lead with a quoted heredoc:

```sh
mv2 send AGENT <<'EOF'
Please review the universe configuration.
Check the user's $workspace and `bootstrap.md` without changing them.
EOF
```

Messaging commands print short readable results by default; `--json` keeps full records for scripts. `send first,second,third` prints one result per recipient without echoing the message. `inbox` prints sender/content blocks; `inbox --all --last 20` or `--since 2h` shows recent history. `inbox --wait --timeout 30s` waits for an unread inbox-only message (never a follow-up), without consuming anything on timeout. Use `deliver AGENT --check` before resending an uncertain message. Agents send `--delivery inbox-only` by default, `follow-up` for a new task, a quick reply or a task's results, and `steer` only when a message is urgent and immediately relevant; status goes through `report`, not messages. `focus NAME --for 20m` (or `--turn`) makes an agent that waits on the user refuse steer and follow-up messages until `unfocus NAME`, or until the time or turn runs out. Inbox-only messages still arrive, and the dashboard's `f` does the same.

Inside an agent, use `"$MV2_CLI"` in place of `mv2`. Piped or redirected stdin is read automatically when no `--text`, `--file` or `--stdin` option is supplied. Quoting `EOF` preserves quotes, apostrophes, dollar signs and backticks without escaping; choose a delimiter absent from the message's standalone lines. `--stdin` still explicitly reads stdin, `--file PATH` reads a file and `--text` accepts short messages. Empty input is an error; without a message option, terminal stdin reports an error instead of waiting for input.

If the user asks to launch or coordinate agents, read the target's `skills/coordination/SKILL.md`. Spawn/resume use saved workspaces and exact native sessions. Sends may wake stopped agents; queued or accepted delivery is not an answer. Reported phase/summary and observed process/status are separate. Move/remove operations require stopped agents, including those in a moved subverse; removal goes to trash. Detaching a dashboard or terminal does not stop agent work.
