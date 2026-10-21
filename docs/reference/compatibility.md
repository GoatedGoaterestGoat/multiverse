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
| Codex CLI | 0.163.0 | Launch and resume flags, the app-server bridge and its protocol schema, shared conversations, native refusals and conversation restoration after a daemon restart. |
| Claude Code | 2.1.306 | Launch and resume flags, and the multiverse mod. Resuming needs 2.1.267 or newer (`--system-prompt-snapshot`). |
| Pi | 0.84.3 | The integration and its optional bridge extension. |

Other releases may behave differently. The Claude Code mod API is early access and can change between releases.

## Bridge feature minimums

| Feature | Needs | With an older version |
| --- | --- | --- |
| Claude bridge (the multiverse mod: message delivery, status, receipts, `ask`, status line, compaction) | Claude Code 2.1.280 | With `inbox_bridge` unset, the agent launches plain and `mv2 inspect` shows why. With `inbox_bridge: true`, the launch fails. A version mv2 can't read counts as new enough. |
| `/multiverse` typeahead in Claude's prompt box | Claude Code 2.1.292 | The typeahead isn't loaded, but the `/multiverse` command works without it. A version mv2 can't read also skips it, unlike the bridge: Claude refuses a plugin that uses an event it doesn't know, and suggestions aren't worth that risk. |
| Codex bridge | No version check | CLIs older or newer than the checked release receive a launch notice that mv2 startup screen checks are disabled. Native side conversations separately require the checked daemon release and a client that supports them. At each launch, mv2 checks that the CLI, the shared app-server and its relay work. It falls back to plain when `inbox_bridge` is unset. The CLI and app-server versions are recorded at launch and may differ. |
| Pi bridge | No version check | The bridge is off unless `inbox_bridge` is `true`. |

See [harnesses](../use/harnesses.md) for `inbox_bridge` and launch-only agents.
