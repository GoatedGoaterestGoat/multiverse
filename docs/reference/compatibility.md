# Compatibility

mv2's requirements and the harness versions it was checked against are listed here. Version numbers appear only on this page and in the README's badges, and other pages link to it.

## Requirements

| Requirement | Version | Notes |
| --- | --- | --- |
| Python | 3.11 or newer | `uv sync` or `pip install .` adds the Python dependencies, `textual>=8.2.3,<9` and `websockets>=15,<18`. |
| tmux | Any recent release | Every agent runs in a tmux pane. mv2 was tested with tmux 3.6a on macOS and 3.4 on Linux. |
| Platform | macOS or Linux | Windows isn't supported, because mv2 needs tmux and POSIX file locks. |
| Harness CLI | See below | Install and sign in to at least one of Codex, Claude Code or Pi. Codex is the default harness. |
| Git, uv | Any | Only `mv2 update` needs them. It needs a Git checkout of mv2, and it syncs its environment with `uv sync --locked` when `uv` is available. |

## Checked harness versions

| Harness | Version | What was checked |
| --- | --- | --- |
| Codex CLI | 0.164.0 | Launch and resume flags, the app-server bridge and its protocol schema, shared conversations, native refusals and conversation restoration after a daemon restart. |
| Claude Code | 2.1.310 | Launch and resume flags, and the multiverse mod. Resuming needs 2.1.267 or newer (`--system-prompt-snapshot`). |
| Pi | 0.84.3 | The integration and its optional bridge extension. |

The Codex check compared generated experimental app-server schemas and the
versioned config schema with the previous checked release, including mv2's
plugin allowlist. Private native fixtures covered plain and connected launches,
permissions and read-only retry, shared identity, refusals, status, usage,
steering, queued messages, side conversations and daemon recovery. They used
isolated homes with updates disabled and local scripted providers; no account or
real model calls were needed. Existing CLI flags and config keys remain valid.
The new image-generation item fields do not change mv2's delivery or status
protocol. See the [Codex release notes](https://github.com/openai/codex/releases/tag/rust-v0.164.0).
0.164.0 kept 0.163.0's native screens and side-thread protocol, so on 0.163.0,
the previous checked release, the startup screen checks and native `/btw` stay
on, with no older-release notice.

When checking a new Codex release, also regenerate the adapter's bundled model
catalog from that release's source and review changes to the rollout format.

Other releases may behave differently. The Claude Code mod API is early access and can change between releases.

## Bridge feature minimums

| Feature | Needs | With an older version |
| --- | --- | --- |
| Claude bridge (the multiverse mod: message delivery, status, receipts, `ask`, status line, compaction) | Claude Code 2.1.280 | With `inbox_bridge` unset, the agent launches plain and `mv2 inspect` shows why. With `inbox_bridge: true`, the launch fails. A version mv2 can't read counts as new enough. |
| `/multiverse` typeahead in Claude's prompt box | Claude Code 2.1.292 | The typeahead isn't loaded, but the `/multiverse` command works without it. A version mv2 can't read also skips it, unlike the bridge: Claude refuses a plugin that uses an event it doesn't know, and suggestions aren't worth that risk. |
| Codex bridge | No version check | Other CLI releases receive a launch notice that mv2 startup screen checks are disabled. Native side conversations separately require a supported daemon release and a client that supports them. At each launch, mv2 checks that the CLI, the shared app-server and its relay work. It falls back to plain when `inbox_bridge` is unset. The CLI and app-server versions are recorded at launch and may differ. |
| Pi bridge | No version check | The bridge is off unless `inbox_bridge` is `true`. |

See [harnesses](../use/harnesses.md) for `inbox_bridge` and launch-only agents.
