# Testing

This page explains how to run mv2's tests, what each kind needs, and which fixtures to use when you add one.

## Run a module

```sh
cd tests
PYTHONPATH="$(git rev-parse --show-toplevel)/src" "$MV2_PYTHON" -m unittest test_refresh
PYTHONPATH="$(git rev-parse --show-toplevel)/src" "$MV2_PYTHON" -m unittest test_tui.DashboardTests
```

- **Run from `tests/`.** Modules import shared fixtures (`test_multiverse`, `native_safety`) by bare name.
- **Pin `PYTHONPATH`.** `uv sync` installs mv2 into `.venv` in editable mode, pointing at the checkout that created it. Without the pin, a worktree's tests, and the runners and helpers they start, import that checkout's code instead of yours.
- **Use `$MV2_PYTHON`**, the checkout's `.venv/bin/python` (`scripts/dev-env.sh` sets it). System Python lacks Textual, and the environment has no pytest.
- **Inside an mv2 agent, clear its identity**, or tools treat the tests as that agent. Run `env -u MV2_AGENT_ID -u MV2_LAUNCH_ID -u MV2_UNIVERSE PYTHONPATH=... "$MV2_PYTHON" -m unittest ...`.

## Run everything

Run modules one at a time, so a slow or failing live module doesn't hide the rest:

```sh
cd tests
src="$(git rev-parse --show-toplevel)/src"
mkdir -p /tmp/mv2-tests
for file in test_*.py; do
  module=${file%.py}
  if PYTHONPATH="$src" "$MV2_PYTHON" -m unittest "$module" > "/tmp/mv2-tests/$module.log" 2>&1
  then echo "ok   $module"; else echo "FAIL $module"; fi
done
```

The suite takes a few minutes. The live Codex and Pi modules depend on the installed versions and on timing. They can fail on a machine for reasons unrelated to your change. Before blaming a change, run the same module on `main` in another worktree and compare.

## Layout

| Files | Cover |
| --- | --- |
| `test_multiverse.py` | Shared fixtures, file protocol, catalog, terminal integration |
| `test_adapters.py`, `test_operations.py`, `test_native_*.py`, `test_harnesses.py` | The adapter contract, core native execution, real launches with fake CLIs |
| `test_claude_*.py`, `test_codex_*.py`, `test_pi_*.py` | One harness each |
| `test_tui*.py`, `test_portal.py`, `test_ui_independent.py` | The dashboard |
| `test_compaction.py` | Tree compaction, independent of any harness |
| `test_docs.py` | Relative links in the docs, and the generated CLI reference |
| `native_safety.py` | The Codex daemon guard (not a test module) |
| `mock-provider.ts`, `steering-provider.ts`, `pi_bridge_test.mjs` | Offline Pi providers and the Pi extension's tests |

The Claude mod and typeahead keep their TypeScript tests next to them, in `src/multiverse/harnesses/claude/mod/tests/` and `src/multiverse/plugins/builtin/typeahead/claude/tests/`.

## What needs what

Most modules need only Python, with no tmux, sockets or model. The rest skip when a tool is missing.

| Needs | Modules | Without it |
| --- | --- | --- |
| tmux | Tests on `TerminalBase`: terminal tests in `test_multiverse`, `test_harnesses`, `test_kill`, `test_auto_exit`, `test_attach_return`, `test_launch_diagnostics`, `test_tmux_statusline` | Skipped |
| `codex`, and tmux for most | `test_codex_daemon`, `test_codex_daemon_cwd`, `test_codex_mouse`, `test_codex_permissions`, `test_codex_timing`, and the live part of `test_codex_recovery` | Skipped |
| `pi` and tmux | `test_pi_timing`, Pi tests in `test_multiverse` and `test_wake_prompt` | Skipped |
| `claude` | The mod and typeahead plugin tests in `test_claude_bridge`; with tmux, the Claude test in `test_wake_prompt` | Skipped |
| Node 22+ | `test_pi_bridge` | Skipped |
| Unix sockets | Terminal and native tests | A sandbox that blocks Unix sockets must allow them |

Live native tests use local scripted providers and isolated homes. They need no account and spend nothing. No test runs an authenticated model turn.

To run the bridges' own tests directly, run these from the checkout root:

```sh
claude plugin test src/multiverse/harnesses/claude/mod
claude plugin test src/multiverse/plugins/builtin/typeahead/claude
node --experimental-strip-types tests/pi_bridge_test.mjs
```

## Fixtures and fakes

| Fixture | Where | Gives you |
| --- | --- | --- |
| `BaseTest` | `test_multiverse.py` | A temporary directory with a fresh `base` universe in `self.root` |
| `TerminalBase` | `test_multiverse.py` | `BaseTest` plus a private tmux server at `self.socket` and a `fake` harness that records its argv in `seen.json`; cleanup kills the server and waits for its runners to exit |
| `until(predicate)` | `test_multiverse.py` | Polls for up to 15 seconds |
| Fake CLIs | `fake_cli` in `test_harnesses.py` | A script set as `settings.executable`, with the bridge off |
| `FourthAdapter` | `test_operations.py` | An adapter that implements only the contract |
| `DemoBackend`, `RecordingBackend` | `examples/tui_demo.py`, `test_tui.py` | An in-memory dashboard backend, and one that records calls |
| Local providers | `test_codex_timing.py`, `test_wake_prompt.py`, the Pi `.ts` files | Native CLIs answering from a local script |
| Local remotes | `test_update.py` | `mv2 update` against a bare repository, never the running checkout |

Pass `socket_path=self.socket` to `lifecycle.start` so launches stay on the test's tmux server. Wait with `until`, and never with a fixed sleep.

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
