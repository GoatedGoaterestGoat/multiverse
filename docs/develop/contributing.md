# Contributing

Set up a checkout, try your change, and send it as a pull request. [Architecture](architecture.md) explains the code.

## Set up a checkout

```sh
git clone https://github.com/GoatedGoaterestGoat/multiverse.git
cd multiverse
uv sync --locked
./bin/mv2 --help
```

`uv sync --locked` creates `.venv` with the locked dependencies and installs mv2 into it in editable mode. `./bin/mv2` runs the checkout's source and switches to `.venv/bin/python` by itself, so you don't need to activate anything.

Plugins kept in their own repositories are optional submodules, and mv2 works without them. To fetch the ones you have access to, run `mv2 plugins install compaction` or `mv2 plugins install remote`.

## Use the checkout's own universe

```sh
. scripts/dev-env.sh
mkdir -p .run
mv2 init .
mv2
```

`scripts/dev-env.sh` points your shell at the checkout by setting these variables:

| Variable | Set to |
| --- | --- |
| `MV2_UNIVERSE` | `.multiverse` in the checkout |
| `MV2_TMUX_SOCKET` | `.run/tmux.sock`: the checkout's own tmux server |
| `MV2_CLI` | `bin/mv2` |
| `MV2_PYTHON` | `.venv/bin/python` |
| `PATH` | `bin` and `.venv/bin` first, so `mv2` is the checkout's launcher |

Git ignores `.multiverse/` and `.run/`. Create `.run` once, because tmux doesn't create a socket's directory. Run `mv2 init .` once too, because mv2 creates a universe only when `MV2_UNIVERSE` is unset. Keep machine-specific notes in `.multiverse/DEVELOPMENT.md`.

The script finds the checkout through Git's common directory. Sourcing it in a worktree therefore still points at the main checkout; see [work in a worktree](#work-in-a-worktree).

## Try the dashboard demo

```sh
.venv/bin/python examples/tui_demo.py
```

The demo runs the real dashboard against `DemoBackend`, an in-memory universe with subverses and sample agents. It needs no tmux, model account or universe. Its actions change only memory.

## Preview the dashboard

For a quick look at a UI change, `scripts/render_tui.py --output DIR` saves SVG screens of the demo. The docs' clips in `docs/media/` are recordings of the real dashboard; if your change makes one out of date, say so in the pull request.

## Run tests

```sh
cd tests
PYTHONPATH="$(git rev-parse --show-toplevel)/src" "$MV2_PYTHON" -m unittest test_refresh
```

Run modules one at a time, with `PYTHONPATH` pinned to your checkout. [Testing](testing.md) explains why, what needs tmux or native CLIs, and which fixtures to use.

## Work in a worktree

Agents that mv2 runs from a checkout call that checkout's `bin/mv2`. Every runner, status line and CLI call loads its source when it starts, so a half-done edit in that checkout breaks those agents mid-task. If you or your agents use mv2 from the main checkout, make changes in a worktree:

```sh
git worktree add ../mv2-my-change -b my-change main
cd ../mv2-my-change
PYTHONPATH="$PWD/src" "$MV2_PYTHON" bin/mv2 --help
```

- **Run the worktree's code** with the shared environment and `PYTHONPATH` pinned, as above. You can also run `uv sync --locked` in the worktree to give it its own `.venv`, which its `bin/mv2` then uses.
- **Don't source `scripts/dev-env.sh` there.** It would point `MV2_UNIVERSE`, `MV2_CLI` and `MV2_TMUX_SOCKET` at the main checkout's live universe.
- **Try a change in a scratch universe** on a separate tmux server, never the live one:

```sh
PYTHONPATH="$PWD/src" "$MV2_PYTHON" bin/mv2 init /tmp/mv2-try
MV2_TMUX_SOCKET=/tmp/mv2-try/tmux.sock PYTHONPATH="$PWD/src" "$MV2_PYTHON" bin/mv2 -u /tmp/mv2-try
```

Inside an mv2 agent, clear the agent's identity first with `env -u MV2_AGENT_ID -u MV2_LAUNCH_ID -u MV2_UNIVERSE ...`. Keep socket paths short, because Unix socket paths are limited to about 100 bytes.

The dashboard and each agent's runner keep the code they loaded. After your change lands in the main checkout, restart the dashboard, and kill and resume each agent so it picks up the change. To bring template changes into a universe, run `mv2 refresh --apply`.

## Commits

Use a conventional prefix with an optional scope, and a summary that says what is true after the change:

```text
feat(harness): set environment variables per harness with settings.env
fix(ui): a view widget can never keep the agent list from loading
refactor: split records from processes into universe and runtime packages
docs(claude): the flags and the mod were checked against Claude Code 2.1.296
test(claude): mod delivery tests pass with Claude Code 2.1.293's test kit
```

| Prefix | For |
| --- | --- |
| `feat` | New behavior |
| `fix` | A bug fix |
| `refactor` | A change in structure with no change in behavior |
| `docs` | Docs and prompts only |
| `test` | Tests only |
| `style` | Formatting and docstrings |

Common scopes are `ui`, `cli`, `claude`, `codex`, `harness`, `compaction`, `catalog`, `tmux` and `statusline`. In the body, say what was wrong, what changed and what you checked, and wrap it at about 72 columns. Keep each commit to one change, with its tests and docs.

## Pull requests

1. Branch from `main` in a worktree.
2. Update the docs in the same change, so a change in behavior updates the page that describes it. Follow [writing docs](docs-style.md). After changing a command or flag, run `python scripts/gen_cli_docs.py`.
3. Run the modules your change touches, plus `test_docs`. Compare any failure in a live harness module with `main` before calling it yours.
4. Push the branch and open a pull request against `main`. Say what changed, why, and how you verified it, including anything you couldn't test.
