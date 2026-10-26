# Testing

This page explains how to run mv2's tests, what each kind needs, and which fixtures to use when you add one.

## Run the tests

```sh
"$MV2_PYTHON" tests/run.py
"$MV2_PYTHON" tests/run.py test_refresh test_tui.DashboardTests
```

`tests/run.py` runs the same tests as `unittest discover -s tests`. It runs each module, or a shard of a slow one, in its own process, as many at once as the machine has CPUs. The whole suite takes about two and a half minutes on 8 cores. It finds the modules as discovery does, through package directories such as `tests/qa/`, and `--list` prints them. Name modules, packages (`qa`), classes or tests for a focused run, and add `-v` to see each test.

- **Concurrent runs share the machine.** Runs started at the same time take turns for its CPU slots instead of overloading it. A waiting run says which runs hold the slots, and again every 30 seconds while it waits: each run's agent, or `not an agent`, with its PID and directory, as in `Waiting for a slot: held by plugins (4 slots, pid 85884, /tmp/…)`. Ask that agent rather than stopping its run.
- **Stopping is clean.** A unit past `--timeout` (20 minutes), or a run stopped with Ctrl-C or SIGTERM, fails the running test and still runs its cleanups: the stop interrupts setUp or the test itself, never a tearDown or cleanup already running. A unit still running 30 seconds after the stop is killed. If the runner itself dies, each unit's watchdog process stops the unit the same way, with the same 30 seconds. Either way, what the unit's tests named with `track` (below) is ended afterwards. Never stop a run by pattern-killing processes.
- **Real mv2, Claude and Codex state is guarded.** A test that writes the real `~/.multiverse`, `~/.claude`, `~/.claude.json` (or a copy beside it, such as `~/.claude.json.backup`) or `~/.codex`, or the `.multiverse` of the checkout the tests run from (in the live checkout, the universe agents run in), fails the run.
- **No credentials reach the tests.** Before it imports or starts anything, the runner drops every variable that looks like a credential or leads to one: tokens, secrets, passwords, keys (`*_KEY*`, `*APIKEY*`), `*_PWD`, `*_AUTH*` and `AUTHORIZATION` (such as `SSH_AUTH_SOCK` and `HTTP_AUTHORIZATION`, but not git's `GIT_AUTHOR_*`), askpass helpers and VS Code's helpers (`GIT_ASKPASS`, `VSCODE_GIT_*`, `VSCODE_IPC_*`), what points at credentials (`DATABASE_URL`, `PGPASSFILE`, `NETRC`, `DOCKER_CONFIG`, `KUBECONFIG`), and `GH_*`, `GITHUB_*`, `AWS_*`, `ANTHROPIC_*`, `OPENAI_*`. No module, test or process a test starts sees one, so no failing assertion can print one. It also points `MV2_CLI` and `MV2_PYTHON` at this checkout's `bin/mv2` and the running interpreter, so a worktree's tests never run the live checkout's mv2.
- **Use `$MV2_PYTHON`**, the checkout's `.venv/bin/python` (`scripts/dev-env.sh` sets it). System Python lacks Textual, and the environment has no pytest.
- **Pin `PYTHONPATH` in a worktree.** `uv sync` installs mv2 into `.venv` in editable mode, pointing at the checkout that created it. Run `PYTHONPATH="$(git rev-parse --show-toplevel)/src" "$MV2_PYTHON" tests/run.py` so the tests, and the runners and helpers they start, import your worktree's code.

The live Codex and Pi modules depend on the installed versions and on timing. They can fail on a machine for reasons unrelated to your change. Before blaming a change, run the same module on `main` in another worktree and compare.

## Run them on Linux

```sh
scripts/linux-tests.sh                                  # the whole suite, on this checkout's working tree
scripts/linux-tests.sh test_run test_native_processes   # some modules
scripts/linux-tests.sh --commit HEAD --python 3.11      # a commit, as committed, on Python 3.11
```

`scripts/linux-tests.sh` runs `tests/run.py` in Docker:
- **The image:** Ubuntu 24.04 with tmux, git and a system `python3`, and Python 3.11 or 3.13 (the default) from uv. The tests run as a user who isn't root. The first run builds the image, in about a minute, and later runs reuse it.
- **The copy:** a clean copy of the checkout's working tree, uncommitted changes included, or of a commit with `--commit`. Submodules come too.
- **What stays out:** nothing git ignores goes in, so no `.venv` and never the checkout's `.multiverse`. No `MV2_*` variable goes in either.
- **The result:** its exit code is the suite's.

Run it:
- before landing a change to anything that differs on Linux: reading `/proc`, boot IDs, `sys.platform` branches, process identity, or the runner's process tracking;
- once for the whole suite before a branch is pushed, next to the full `tests/run.py` run on macOS.

It can't run native Claude, Codex or Pi: none is installed there, so their live tests skip, as on CI. `test_pi_bridge` skips too, without Node. On a Mac, Docker runs aarch64 Linux, while CI runs x86-64.

## What deserves a test

Fewer tests are better. A test earns its place only if it guards behaviour that is easy to break, or whose break would be hard to notice. Before adding one, write in one line which feature it tests and why. That line is its docstring:

```python
def test_runs_at_once_share_the_machines_slots(self):
    """Concurrent runs, whatever each one's -j, never run more units at once than the machine's slots; a broken
    lock shows only as load and timing flakes in other agents' runs."""
```

- If you can't write that line, don't add the test.
- Don't test wording, formatting, layout, or simple argument parsing and CLI plumbing.
- Give each behaviour one home. To cover another case of something already tested, add a row to its table-driven test instead of a near-identical test.
- A bug fix keeps one test that would have caught the bug, if the bug could plausibly come back.

## Layout

| Files | Cover |
| --- | --- |
| `test_multiverse.py` | Shared fixtures, file protocol, catalog, terminal integration |
| `test_adapters.py`, `test_operations.py`, `test_native_*.py`, `test_harnesses.py` | The adapter contract, core native execution, real launches with fake CLIs |
| `test_claude_*.py`, `test_codex_*.py`, `test_pi_*.py` | One harness each |
| `test_tui*.py`, `test_portal.py` | The dashboard |
| `test_compaction.py` | Tree compaction, independent of any harness |
| `test_docs.py` | Relative links in the docs, and the generated CLI reference |
| `run.py`, `tracking.py`, `test_run.py` | The parallel runner, and what it records to end after a unit (not test modules); the runner's tests |
| `isolation.py`, `home_safety.py`, `credentials.py` | Each test's own HOME and environment, the guard on the real `~/.multiverse`, `~/.claude`, `~/.claude.json` and `~/.codex`, and which variables count as credentials (not test modules) |
| `native_safety.py` | The Codex daemon guard (not a test module) |
| `native_processes.py` | Ends the processes of a private Codex server before its test removes the home (not a test module) |
| `native_cli.py` | An `mv2` launcher pinned to this checkout's source and interpreter, and a `python3` that fails if called, for native callback fixtures (not a test module) |
| `fixtures/` | Data recorded from the real thing: Codex 0.163.0 screens, Claude session files and `/proc` lines, a delivery ledger written by main@52b9df4, and runtime records from older writers |
| `mock-provider.ts`, `steering-provider.ts`, `pi_bridge_test.mjs` | Offline Pi providers and the Pi extension's tests |

The Claude mod and typeahead keep their TypeScript tests next to them, in `src/multiverse/harnesses/claude/mod/tests/` and `src/multiverse/plugins/builtin/typeahead/claude/tests/`.

## What needs what

Most modules need only Python, with no tmux, sockets or model. The rest skip when a tool is missing.

| Needs | Modules | Without it |
| --- | --- | --- |
| tmux | Tests on `TerminalBase`: terminal tests in `test_multiverse`, `test_harnesses`, `test_kill`, `test_auto_exit`, `test_attach_return`, `test_attachment_teardown`, `test_launch_diagnostics`, `test_launch_display_followups`, `test_launch_notices`, `test_launch_registration`, `test_native_service`, `test_resume_placeholders`, `test_spawn_socket`, `test_tmux_statusline`, `test_claude_boundary`, `test_claude_resume`, `test_codex_auto`, `qa.test_lifecycle_spec`; the shared live group in `test_runtime_authority`; also `test_helper_stdin` and one test in `test_run` | Skipped |
| `codex`, and tmux for most | `test_codex_daemon`, `test_codex_permissions`, `test_codex_timing`, `test_codex_refusal_native`, `test_codex_shared_identity`, `test_codex_side_native`, `test_codex_queue_continuation_native`, the native tests in `test_codex_plugins`, and the live part of `test_codex_recovery` | Skipped |
| `pi` and tmux | `test_pi_timing`, Pi tests in `test_multiverse` and `test_wake_prompt` | Skipped |
| `claude` | The mod and typeahead plugin tests in `test_claude_bridge`; with tmux, the Claude test in `test_wake_prompt` | Skipped |
| Node 22+ | `test_pi_bridge` | Skipped |
| Unix sockets | Terminal and native tests | A sandbox that blocks Unix sockets must allow them |

Live native tests use local scripted providers and isolated homes. They need no account and spend nothing. No test runs an authenticated model turn.

## Continuous integration

`.github/workflows/tests.yml` runs on Ubuntu 24.04 with tmux 3.4 and Node 22. Codex, Claude and Pi aren't installed there, so their live tests skip.

- **suite** runs `tests/run.py` on Python 3.11, the oldest version mv2 supports, and on 3.13, for pull requests and for pushes to `main` and `ci/*` branches. Each takes about three and a half minutes.
- **each module alone** runs nightly, and on demand from the Actions tab: every module `tests/run.py --list` prints, by itself with plain `unittest`, one at a time. A module can pass beside others and fail alone, when nothing else loads the machine.

The plugin submodules are private and aren't fetched, so CI runs mv2's own tests only: not the GOAT and remote-control plugins' own tests, and not a submodule pin bump. Run `tests/run.py test_plugin_checkouts` locally, with the submodules checked out, before bumping a pin.

To run the bridges' own tests directly, run these from the checkout root:

```sh
claude plugin test src/multiverse/harnesses/claude/mod
claude plugin test src/multiverse/plugins/builtin/typeahead/claude
node --experimental-strip-types tests/pi_bridge_test.mjs
```

## Fixtures and fakes

| Fixture | Where | Gives you |
| --- | --- | --- |
| `isolate(self)` | `isolation.py` | Call it first in `setUp`: the test gets its own HOME, which the processes it starts inherit, and loses the agent's environment (`MV2_*`, `CODEX_*`, `CLAUDE_CODE_*`, `CLAUDE_CONFIG_DIR`, `TMUX`) and every variable that looks like a credential, which matters when a module is run directly with `unittest`, since `tests/run.py` has dropped them already. A value the test set itself before calling it stays |
| `track(tmux_socket=…, pid=…)` | `isolation.py` | Names a tmux server or process the test started outside its own process group, so `tests/run.py` ends it even if the test is stopped before its cleanups run. A PID is ended only while its start time matches. For a process that leads its own group, such as a daemon, the whole group is ended, helpers included, while any process seen in it is still there, even after the leader has exited |
| `home_safety` | `home_safety.py` | The real-state guard: a refused write under `~/.multiverse`, `~/.claude`, `~/.codex` or the checkout's own `.multiverse`, or to `~/.claude.json` and its copies, fails the test that made it. `isolate` and `PluginTestCase` turn it on, as does every `tests/run.py` worker |
| `BaseTest` | `test_multiverse.py` | `isolate(self)`, then a temporary directory with a fresh `base` universe in `self.root` |
| `TerminalBase` | `test_multiverse.py` | `BaseTest` plus a private tmux server at `self.socket`, tracked, and a `fake` harness that records its argv in `seen.json`; cleanup kills the server and waits for its runners to exit |
| `until(predicate)` | `test_multiverse.py` | Polls for up to 15 seconds |
| Fake CLIs | `fake_cli` in `test_harnesses.py` | A script set as `settings.executable`, with the bridge off |
| `FourthAdapter` | `test_operations.py` | An adapter that implements only the contract |
| `DemoBackend`, `RecordingBackend` | `examples/tui_demo.py`, `test_tui.py` | An in-memory dashboard backend, and one that records calls |
| Local providers | `test_codex_timing.py`, `test_wake_prompt.py`, the Pi `.ts` files | Native CLIs answering from a local script |
| Local remotes | `test_update.py` | `mv2 update` against a bare repository, never the running checkout |

Pass `socket_path=self.socket` to `lifecycle.start` so launches stay on the test's tmux server. Wait with `until`, and never with a fixed sleep. A test class that isn't a `BaseTest` calls `isolate(self)` first in its own `setUp`, and a test that starts a tmux server or daemon of its own names it with `track`.

`track` has one limit with a daemon's group. The runner sees who is in the group when you call `track`, then every half second, and once more as the test ends. It ends the group afterwards only while a process it saw there is still in it, since a group's number can later belong to someone else. So a helper that starts after one look, whose leader and other seen processes have all exited before the next, can't be shown to be the daemon's, and it's left running. If a daemon starts helpers and may exit quickly, do one of these:

- call `track(pid=helper)` for a helper you start yourself;
- call `track(pid=leader)` again once the leader has started its helpers and is still running, so the runner looks again.

## Dashboard tests

Textual tests drive the real app with `App.run_test()` and Pilot. They use real widgets, keys, clicks and resizes, with no terminal.

```python
class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)

    async def test_spawn_draft_opens(self):
        async with self.app.run_test(size=(120, 32)) as pilot:
            await pilot.press("n")
            await pilot.pause()
            await self.app.workers.wait_for_complete()
```

`refresh_interval=0` turns off the refresh timer, so wait for workers instead. Use `pilot.resize_terminal(60, 20)` to check narrow layouts. For a visual check, render the same screens with `scripts/render_tui.py` ([contributing](contributing.md#preview-the-dashboard)).

## Never touch a person's Codex daemon

Tests must never start, stop or connect to the developer's own Codex app-server.

- **Install the guard.** A module that may run `codex` calls `native_safety.install()` at import. Its audit hook fails the test when a subprocess runs `codex` against `~/.codex` or the caller's `CODEX_HOME`. It also fails the test when a Unix socket connects to that home's daemon.
- **Use a disposable home.** Create it with `native_safety.isolated_home(path)`, which turns off daemon auto-updates and startup update checks. The guard also refuses a home that leaves either on.

Claude tests likewise run with their own `CLAUDE_CONFIG_DIR` under the test directory.
