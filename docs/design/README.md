# Design records

These pages record decisions, rationale and detailed contracts for maintainers. They aren't user docs, and they may describe history. For how things work today, start at the [docs index](../README.md).

| Record | Covers |
| --- | --- |
| [harness-adapters.md](harness-adapters.md) | The full adapter contract: native services, sessions and attachments, delivery states, bridge resume permissions, Codex recovery, the Claude bridge |
| [session-isolation.md](session-isolation.md) | Launch identity, pane ownership and session binding, and the limits of those checks |
| [session-lineage.md](session-lineage.md) | How an agent follows native session changes such as Claude's `/clear`, and the lineage it records |
| [shared-conversations.md](shared-conversations.md) | Agent identity in shared Codex conversations, the command boundary, attachment cleanup and native refusals |
| [future-work.md](future-work.md) | Agreed ideas that are not scheduled yet |
| [plugins.md](plugins.md) | The plugin system's design: discovery, trust, settings, the API and its versions, harness extensions, the event journal and host, and moving compaction into a plugin |
| [codex-plugins.md](codex-plugins.md) | The Codex investigation behind the plugin design's Codex section: what 0.159.3 and 0.162.1 support, the launch contribution, and the full inventory of config keys |

Tree compaction's design lives with the plugin, in `plugins/compaction/docs/compaction.md`.
