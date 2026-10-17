# Prompts

A universe's prompts are plain files that you edit to change what your agents are told. Each subverse has its own set.

| File | Reaches the agent | Use it for |
| --- | --- | --- |
| `bootstrap.md` | At every start and resume, in its system prompt | A few lines every agent here must know. Keep it short |
| `skills/NAME/SKILL.md` | When the agent loads it | Reusable know-how, like `coordination` |
| `workflows/NAME/prompt.md` | When the agent or you load it | A procedure for a kind of task, like `review` |
| `commands/NAME/prompt.md` | When the agent or you load it | A named prompt, like `help` |

Each skill, workflow and command folder also has an `entry.json` with a one-line `description`. The catalog shows that description.

## Load a prompt

```sh
mv2 catalog                    # what's available here
mv2 command review             # print the review workflow
mv2 command workflows/review   # qualify a name used by more than one kind
```

`command` prints the prompt and never runs anything. Agents load a prompt this way only when their task needs it, so long instructions stay out of their context until then.

In a Claude Code or Pi session with a live connection, type `/multiverse` to list the catalog. Type `/multiverse NAME` to use an entry as your prompt. In Codex, ask the agent in words, such as "Run /multiverse review on this branch."

## Change one

Edit the file in the universe. Changes to skills, workflows and commands take effect at once. A bootstrap change reaches each agent the next time it starts or resumes. To add a workflow, create its folder and files:

```sh
mkdir -p .multiverse/workflows/release
echo '{"description": "Cut a release: changelog, tag, and the release notes."}' > .multiverse/workflows/release/entry.json
$EDITOR .multiverse/workflows/release/prompt.md
mv2 catalog
```

`mv2 refresh` keeps your edits when it brings in template updates; see [updating](../use/updating.md). To ship prompts to other universes, put them in a template; see [templates](../develop/templates.md).
