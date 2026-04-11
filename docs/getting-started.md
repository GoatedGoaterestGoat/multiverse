# Getting Started

## What is Multiverse?

Multiverse is a multi-agent orchestration layer built on top of Claude Code. It introduces branching conversations, parallel agents on git worktrees, intelligent context management, and inter-agent communication. See the root [README.md](../README.md) for the full vision and architecture.

The first working component is **selective context folding** — agent-initiated, range-based summarization that gives more granular control than `/compact`. This is packaged as a Claude Code plugin in [`multiverse-plugin/`](../multiverse-plugin/) (see its [README](../multiverse-plugin/README.md) for plugin details).

## Repository layout

```
Multiverse/
├── README.md                  # Project vision, architecture, roadmap
├── docs/
│   ├── getting-started.md     # This file
│   └── PROGRESS.md            # What's been built so far
├── multiverse-plugin/         # Claude Code plugin (current focus)
├── context-fold-mcp/          # Original standalone MCP server (superseded by plugin)
├── context-original.md        # Demo: full session before folding
├── context-folded.md          # Demo: same session after folding
├── Playground/                # Experimental prototypes (gitignored)
└── claude-code-src/           # Reference copy of Claude Code source (gitignored)
```

## Reference materials

### Claude Code documentation

The official docs at [code.claude.com/docs](https://code.claude.com/docs) are the authoritative reference. The pages we rely on most:

| Page | URL | What it covers |
|:-----|:----|:---------------|
| **Docs index** | [llms.txt](https://code.claude.com/docs/llms.txt) | Complete list of all doc pages |
| **Plugins** | [plugins](https://code.claude.com/docs/en/plugins) | Tutorial for creating plugins |
| **Plugins reference** | [plugins-reference](https://code.claude.com/docs/en/plugins-reference) | Complete schema, manifests, CLI commands |
| **Plugin marketplaces** | [plugin-marketplaces](https://code.claude.com/docs/en/plugin-marketplaces) | Distribution and marketplace creation |
| **Discover plugins** | [discover-plugins](https://code.claude.com/docs/en/discover-plugins) | Installing plugins, scopes |
| **Hooks** | [hooks](https://code.claude.com/docs/en/hooks) | Event lifecycle, JSON I/O protocol, decision fields |
| **Skills** | [skills](https://code.claude.com/docs/en/skills) | Skill authoring, frontmatter, progressive disclosure |
| **Subagents** | [sub-agents](https://code.claude.com/docs/en/sub-agents) | Specialized agent definitions, isolation |
| **Agent teams** | [agent-teams](https://code.claude.com/docs/en/agent-teams) | Multi-agent coordination across sessions |
| **MCP** | [mcp](https://code.claude.com/docs/en/mcp) | Model Context Protocol server integration |
| **Settings** | [settings](https://code.claude.com/docs/en/settings) | Configuration scopes, settings files |
| **Context window** | [context-window](https://code.claude.com/docs/en/context-window) | How context is used, visualization |

### claude-code-src — local source reference

`claude-code-src/` is a local snapshot of Claude Code's source code (gitignored). It's invaluable for understanding internal mechanisms that aren't fully documented. **Note**: this is a *reference for understanding*, not for building against — Claude Code updates frequently, so any plugin we build must use the public plugin API, not internal source.

**Key files for understanding compaction and session lifecycle:**

| File | What it reveals |
|:-----|:----------------|
| `commands/compact/compact.ts` | The `/compact` command handler entry point |
| `services/compact/compact.ts` | Core compaction logic, `CompactionResult`, `buildPostCompactMessages()` |
| `services/compact/sessionMemoryCompact.ts` | Fast path using persistent session memory (no API call) |
| `services/compact/autoCompact.ts` | Auto-triggered compaction when context fills |
| `query.ts` | The async generator that yields messages — **the hot-swap mechanism** |
| `screens/REPL.tsx` | React state for messages, `setMessages()` hot-swap |
| `screens/ResumeConversation.tsx` | How `--resume` loads sessions from JSONL |
| `utils/sessionStorage.ts` | JSONL persistence, `insertMessageChain()`, parentUuid handling |
| `utils/messages.ts` | Message creation, normalization for API |
| `utils/messagePredicates.ts` | `isHumanTurn()` and other message classifiers |
| `main.tsx` | CLI argument parsing — `--resume`, `--continue`, `--fork-session`, `-p` |
| `cli/print.ts` | Print mode logic, headless resume behavior |
| `state/AppStateStore.ts` | App-level state, initial message handling |
| `types/message.ts` | The discriminated `Message` union type |

**Key insights extracted from this exploration:**

1. **`/compact` works in-process.** It runs inside the `query()` async generator, builds a new message array, yields each message to the REPL, and `setMessages()` hot-swaps `messagesRef.current`. The same generator instance keeps running with the new context. **No restart, no stop.**

2. **`--resume` always waits for input in interactive mode.** The REPL doesn't auto-respond regardless of what the last message in the JSONL is. Auto-continue is only available via `-p "prompt" --resume <id>` (print mode).

3. **Plugins cannot replicate `/compact`'s hot-swap.** Plugins run as external processes (hooks, MCP servers) with no access to `messagesRef`, the generator state, or `setMessages()`. The only way to swap context from a plugin is to stop the session and resume.

4. **Compact boundaries sever the JSONL chain.** A compaction boundary message is written with `parentUuid: null`, orphaning the old conversation chain on disk while in-memory state continues seamlessly.

### MCP (Model Context Protocol)

The plugin's MCP server uses the Python MCP SDK. Reference:

- [MCP specification](https://modelcontextprotocol.io/) — protocol definition
- [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) — `mcp>=1.0.0` package
- [`FastMCP`](https://github.com/modelcontextprotocol/python-sdk) — the high-level decorator API used in `multiverse-plugin/src/server.py`

### Other tools

- [`uv`](https://docs.astral.sh/uv/) — Python package manager used to run the MCP server
- [`jq`](https://jqlang.github.io/jq/) — JSON parser used in the wrapper script and hook scripts

## Working with the reference source

Because `claude-code-src/` updates as Claude Code updates, treat it as a *moving target*. When investigating a feature:

1. **Verify the current version** — run `claude --version` and check the source matches
2. **Search broadly** — file paths and function names change between versions
3. **Don't depend on internals in the plugin** — only use the public hook/MCP/skill APIs
4. **Document findings in PROGRESS.md** — preserve insights even if the source changes

## Next steps

- Read [PROGRESS.md](PROGRESS.md) to see what's been built and what's next
- See [`multiverse-plugin/README.md`](../multiverse-plugin/README.md) for plugin installation and usage
- Browse the roadmap in the root [README.md](../README.md#roadmap)
