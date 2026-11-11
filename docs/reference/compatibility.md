# Compatibility

mv2's requirements and the harness versions it was checked against are listed here. Version numbers appear only on this page and in the README's badges, and other pages link to it.

## Requirements

| Requirement | Version | Notes |
| --- | --- | --- |
| Python | 3.11 or newer | `uv sync` or `pip install .` adds the Python dependencies, `textual>=8.2.3,<9`, `websockets>=15,<18`, and `zstandard` below Python 3.14. Python 3.14 and newer use the built-in `compression.zstd`. |
| tmux | 3.2 or newer | Every agent runs in a tmux pane. mv2 was tested with tmux 3.6a on macOS and 3.4 on Linux. |
| Platform | macOS or Linux | Windows isn't supported, because mv2 needs tmux and POSIX file locks. |
| Harness CLI | See below | Install and sign in to at least one of Codex, Claude Code or Pi. Codex is the default harness. |
| Git, uv | Any | Only `mv2 update` needs them. It needs a Git checkout of mv2, and it syncs its environment with `uv sync --locked` when `uv` is available. |

## Checked harness versions

| Harness | Version | What was checked |
| --- | --- | --- |
| Codex CLI | 0.167.1 | Launch and resume flags, the app-server bridge and its protocol schema, shared conversations, native refusals, conversation restoration after a daemon restart, and the composer and footer the dashboard's live pane leaves out, from its panes and its renderer's snapshots. |
| Claude Code | 2.1.322 | Launch and resume flags, the multiverse mod, and the input box the dashboard's live pane leaves out. Resuming needs 2.1.267 or newer (`--system-prompt-snapshot`). |
| Pi | 0.84.3 | The integration and its optional bridge extension. |

The Codex check compared the official app-server and config schemas with the previous checked release, including mv2's plugin allowlist. Private native fixtures covered plain and connected launches, permissions and read-only retry, shared identity, refusals, steering, queued messages, side conversations and daemon recovery. They used isolated homes with updates disabled and local scripted providers; no account or real model calls were needed. Existing CLI flags, config keys and rollout reading remain compatible. The startup refusal matcher handles the command-center footer and its keymap variants. A native approval requiring a fresh human decision remains terminal-only. Public and private model catalogs were regenerated; their model data is unchanged from the previous checked release. See the [Codex release notes](https://github.com/openai/codex/releases/tag/rust-v0.167.1) and [audit details](../design/harness-adapters.md#codex-01671-compatibility-audit).

The earlier checked releases, 0.163.0, 0.164.0, 0.166.0 and 0.167.0, retain startup-screen checks and native `/btw`, with no older-release notice. Other releases remain unverified. The live-pane fixtures cover the checked release's composer, drafts, status and warning footers, with dialogs kept unchanged.

When checking a new Codex release, also regenerate the adapter's bundled model
catalog from that release's source and review changes to the rollout format.

Codex `ask` and one-shot runs support **0.166.0, 0.167.0 and 0.167.1**. They require a separate release check; leave them unavailable on releases that have not passed it. In a private process, home and workspace, capture empty tool inventories with the trimmed same-model catalog and explicit feature settings, and verify that MCP, hooks, plugins and new instruction discovery stay off. Check copied same-model history and encrypted reasoning, validate the `auth.json` and `auth.lock` link targets without reading or logging credential contents, and prove cleanup after success, timeout, cancellation and startup failure. Regenerate the private catalogue with `scripts/gen_codex_run_models.py SOURCE --version VERSION` and run `test_codex_run_native` alongside the replay, ask and cleanup tests. This checks the recipe's tool declarations without adding a per-request gate.

Following in-terminal Codex `/new`, `/clear` and `/resume` requires **matching CLI
and daemon releases on 0.166.0, 0.167.0 or 0.167.1**. This has its own version gate: expanding startup-screen or
`/btw` support does not enable navigation. Before qualifying another release,
recheck the explicit request shapes and correlated replies, unchanged workspace
and grants, idle and empty-queue refusal, callback identity, lineage and original
receipt targets. Run `test_codex_navigation_native` in a private home with a
private daemon and local provider, alongside the relay and lineage tests. Include
`/clear`'s early view reset, the resume picker with concurrent input, and history
loading failure after RPC success. The [selection boundary](../design/session-lineage.md#codex-conversation-switches)
must remain explicit; a successful RPC does not prove which view the TUI shows.

Codex waiting prompts have a separate **matching CLI and daemon 0.166.0, 0.167.0 or 0.167.1** gate. Before extending it, check the terminal's offered decisions and typed response schemas, review and side-thread routing, request resolution and cancellation, and any observable effects. Run `test_codex_prompts` and `test_relay_connections` with their fake native peer, alongside the core, CLI and plugin prompt tests. Startup-screen support alone does not qualify answering prompts, and a resolution notice still does not identify whose response Codex took.

When checking a new Claude Code or Codex release, also add its screens to
`tests/fixtures/`: real captures, or Codex's renderer snapshots labelled as
such. Then run `tests/test_pane_display.py`. The dashboard's live pane must
still leave out the input box and the rows under it, and keep dialogs
unchanged.

Other releases may behave differently. The Claude Code mod API is early access and can change between releases.

## Bridge feature minimums

| Feature | Needs | With an older version |
| --- | --- | --- |
| Claude bridge (the multiverse mod: message delivery, status, receipts, `ask`, status line, compaction) | Claude Code 2.1.280 | With `inbox_bridge` unset, the agent launches plain and `mv2 inspect` shows why. With `inbox_bridge: true`, the launch fails. A version mv2 can't read counts as new enough. |
| `/multiverse` typeahead in Claude's prompt box | Claude Code 2.1.292 | The typeahead isn't loaded, but the `/multiverse` command works without it. A version mv2 can't read also skips it, unlike the bridge: Claude refuses a plugin that uses an event it doesn't know, and suggestions aren't worth that risk. |
| Codex bridge | No version check | Other CLI releases receive a launch notice that mv2 startup screen checks are disabled. Native side conversations separately require a supported daemon release and a client that supports them. At each launch, mv2 checks that the CLI, the shared app-server and its relay work. It falls back to plain when `inbox_bridge` is unset. The CLI and app-server versions are recorded at launch and may differ. |
| Codex in-terminal conversation navigation | Matching CLI and daemon 0.166.0, 0.167.0 or 0.167.1 | Other releases refuse primary conversation switches through the bridge. Plain and fallback launches are not upgraded into navigation support. |
| Codex waiting prompts | Matching CLI and daemon 0.166.0, 0.167.0 or 0.167.1, with a current verified prompt relay | Other versions and older launches require a checked bridge resume. Plain and fallback launches cannot publish or answer prompts. |
| Pi bridge | No version check | The bridge is off unless `inbox_bridge` is `true`. |

See [harnesses](../use/harnesses.md) for `inbox_bridge` and launch-only agents.
