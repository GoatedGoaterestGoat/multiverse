# AGENTS.md

Follow these rules when you work on this repository as a coding agent.

## What this is

mv2 (multiverse-v2) is a Python CLI and Textual dashboard that runs coding agents (Claude Code, Codex, Pi) in tmux panes and lets them message each other. State is plain files in a universe's `.multiverse/` directory. mv2 runs no daemon of its own.

## Where things are

| Path | Holds |
| --- | --- |
| `src/multiverse/cli.py`, `tools/` | The `mv2` command; one module per command |
| `src/multiverse/universe/` | Everything on disk: agents, inboxes, templates, catalog |
| `src/multiverse/runtime/` | Launching, delivery, the pane runner |
| `src/multiverse/terminal/` | tmux and the status line |
| `src/multiverse/harnesses/` | Adapters; the contract is `types.py` |
| `src/multiverse/plugins/` | The plugin system and its API; bundled plugins in `builtin/` |
| `src/multiverse/ui/`, `templates/` | The dashboard, bundled templates |
| `plugins/` | Optional plugins in their own repositories, as git submodules (`compaction`, `remote`) |
| `examples/` | The demo backend the UI tests use, sample plugins |
| `tests/` | unittest modules |
| `docs/` | User docs; `docs/develop/` for contributors; `docs/design/` for rationale |

Read [docs/develop/architecture.md](docs/develop/architecture.md) before changing launch, delivery or identity code.

## Work in a worktree

Live agents run mv2 from the main checkout. Every new runner or CLI call loads its source, so never edit the main checkout.

```sh
git worktree add ../mv2-TOPIC -b TOPIC main
```

- Run the worktree's code with `PYTHONPATH="$PWD/src" "$MV2_PYTHON" bin/mv2 ...`.
- Don't source `scripts/dev-env.sh` in a worktree, because it points at the main checkout's live universe.

## Leave the live universe alone

- Never run tests or `mv2` against the main checkout's `.multiverse` or its tmux server. Try changes in a scratch universe (`mv2 init /tmp/mv2-try`) with its own `MV2_TMUX_SOCKET`.
- Don't message, kill or resume agents you didn't start for your task.
- Never start, stop or restart a real Codex app-server daemon, or launch real Claude, Codex or Pi sessions to test.

## Run tests

```sh
env -u MV2_AGENT_ID -u MV2_LAUNCH_ID -u MV2_UNIVERSE \
  PYTHONPATH="$(git rev-parse --show-toplevel)/src" "$MV2_PYTHON" tests/run.py test_refresh
```

- Use `tests/run.py`: name modules for a focused run, or give no names for the whole suite. Concurrent runs share the machine's CPU slots. Never run plain `unittest discover`, and never stop a run by pattern-killing processes.
- Pin `PYTHONPATH` to your checkout. The shared `.venv` has an editable install of the main checkout, so without the pin you test main's code.
- Unset `MV2_AGENT_ID`, `MV2_LAUNCH_ID` and `MV2_UNIVERSE`. You are an mv2 agent, and the tests must not act as you.
- The live Codex and Pi modules (`test_codex_daemon`, `test_codex_daemon_cwd`, `test_codex_mouse`, `test_codex_permissions`, `test_codex_recovery`, `test_codex_timing`, `test_pi_timing`, `test_wake_prompt`) can fail for environmental reasons, such as the installed versions or timing. Before calling a failure a regression, run the same module on a clean `main` worktree and compare.
- Add a test only for behaviour that is easy to break, or whose break would go unnoticed, and make that one-line reason its docstring ([what deserves a test](docs/develop/testing.md#what-deserves-a-test)). Write it with the fixtures in [docs/develop/testing.md](docs/develop/testing.md). Never let a test touch the user's Codex home or daemon.

## Change docs with behavior

- When you change behavior, update the page that describes it in the same commit. Follow [docs/develop/docs-style.md](docs/develop/docs-style.md).
- After changing a command or flag, run `python scripts/gen_cli_docs.py`, then `test_docs`.
- Keep prompts under `src/multiverse/templates/` short, because they go into agents' context.

## Commit

- Commit, push or open a pull request only when asked.
- Write the subject as `type(scope): summary`, with type `feat`, `fix`, `refactor`, `docs`, `test` or `style`. The summary says what is true after the change, as in `fix(ui): a view widget can never keep the agent list from loading`.
- In the body, say what was wrong, what changed and what you checked.
- Don't add `Co-Authored-By` trailers or generated-by lines unless the user asks for them.
