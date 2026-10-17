# Design records

These pages record decisions, rationale and detailed contracts for maintainers. They aren't user docs, and they may describe history. For how things work today, start at the [docs index](../README.md).

| Record | Covers |
| --- | --- |
| [harness-adapters.md](harness-adapters.md) | The full adapter contract: native services, sessions and attachments, delivery states, bridge resume permissions, Codex recovery, the Claude bridge |
| [session-isolation.md](session-isolation.md) | Launch identity, pane ownership and session binding, and the limits of those checks |
| [plugins.md](plugins.md) | The plugin system's design: discovery, trust, settings, the API and its versions, harness extensions, the event journal and host, and moving compaction into a plugin |

Tree compaction's design lives with the plugin, in `plugins/compaction/docs/compaction.md`.
