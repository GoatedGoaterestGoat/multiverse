# Writing docs

How to write and update mv2's docs. Keep pages short, accurate and easy to scan.

## Where a page goes

| Folder | For | Holds |
| --- | --- | --- |
| `README.md` | Everyone, first visit | What mv2 is, install, a short quickstart, links |
| `docs/getting-started.md`, `docs/concepts.md` | New users | The first hour, and the words the rest of the docs use |
| `docs/use/` | People using mv2 | One task area per page, dashboard first, with clips |
| `docs/cli/` | Agents' commands, and people scripting mv2 | Task guides for the CLI, plus the generated reference |
| `docs/reference/` | Anyone looking something up | Complete lists: dashboard keys and views, files, variables, versions |
| `docs/develop/` | Contributors and integrators | How the code works and how to change it |
| `docs/design/` | Maintainers | Rationale, decisions and detailed contracts. User pages link here only for depth |
| `AGENTS.md` | Coding agents working on this repo | Repo rules in a form an agent can follow |

The prompts installed into universes (`templates/*/bootstrap.md`, `skills/`, `commands/`, `workflows/`) are docs for agents at work. They follow the same rules, but they must stay short: every word costs context.

## Voice

Write in plain, complete sentences, and keep them short. The goal sits between terse notes and full prose:

- **Complete sentences.** No fragments such as "About ten minutes." or "Nothing starts yet."; write "This takes about ten minutes." Avoid punchy closing lines and slogans.
- **No filler.** Cut openers such as "This page walks you through…" and padding such as "exactly as you would" or "you only need to". If a sentence still says the same thing without a phrase, drop the phrase.
- **Lists and tables for parallel facts.** Requirements, keys, options, states and files go in lists or tables, not in prose. Use numbered steps only when order matters and each step is a real action.
- **One idea per sentence.** Most sentences fit in about 12 to 20 words. Split a sentence that joins two ideas with "and", a semicolon or a dash.
- **No dashes for asides.** Use commas, parentheses or a new sentence instead of em dashes.

`docs/getting-started.md`, `docs/use/dashboard.md` and `docs/use/agents.md` are written this way; use them as models.

## Rules

- **Lead with the task.** Start a section with the command or action, then explain what it does. Prefer one good example to a paragraph of description.
- **One topic per page.** Aim for under about 250 lines. Split a page that grows past that.
- **Short paragraphs.** Four sentences at most. Use a list when items are parallel, and a table for options, keys, files or fields.
- **Second person, present tense.** "Run `mv2 list`", not "The user may run". Describe what is true now: no "first slice", "now", "no longer", "new in", and no history. History belongs in commit messages and `docs/design/`.
- **Plain words.** Name things the way the CLI and dashboard do. Define a term once, in `concepts.md`, and link to it.
- **Say it once.** Each fact has one home; other pages link to it. Version numbers and tested releases live only in `reference/compatibility.md`.
- **Accurate examples.** Every command must run as written against the current CLI. Check flags with `mv2 COMMAND --help`. Use `mv2`, not `./bin/mv2`, outside the contributing page.
- **No internal names.** Don't mention agents, sessions or people that built a feature.
- **Relative links.** Link between pages with relative paths (`../use/dashboard.md`), so they work on GitHub and in a checkout.

## Format

- One `#` title per page, then `##` sections. Don't skip levels.
- Code blocks have a language: `sh` for shell, `json`, `text` for trees and output.
- Keep shell examples copy-pasteable: no `$` prompts, and comments only where they help.
- Wrap nothing by hand; write one paragraph per line.

## Clips and screenshots

Pages under `docs/use/` show the dashboard with clips from `docs/media/`. They are recordings of the real dashboard, not mockups. When a change makes one out of date, note it in the pull request.

## Keeping docs true

- `docs/cli/reference.md` is generated: run `python scripts/gen_cli_docs.py` after changing a command or flag. Don't edit it by hand.
- `tests/test_docs.py` fails on broken relative links, on a command missing from the CLI reference, and on a stale generated reference. Run it with the other tests.
- A change that alters behavior updates the page that describes it in the same commit.
