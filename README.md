# Multiverse: Branching Agent Workflows with Time Travel

> *What if agent conversations were a tree instead of a line?*

Multiverse is a **runtime-agnostic** multi-agent orchestration layer that introduces **branching conversations**, **parallel agents on git worktrees**, **intelligent context management**, and **inter-agent communication** -- inspired by [5D Chess with Multiverse Time Travel](https://store.steampowered.com/app/1349230/5D_Chess_With_Multiverse_Time_Travel/).

It runs on top of resumable CLI coding-agent runtimes. [Claude Code](https://docs.anthropic.com/en/docs/claude-code), Codex, and [pi](https://pi.dev) sessions share one dashboard, agent registry, inbox, and spawn broker. Frontier-coherent Multiverse compaction remains Claude-specific; Codex and pi retain their native compaction behavior.

---

## The Problem

Current agent coding assistants (Cursor, Claude Code, etc.) have **linear chat histories**. This creates several pain points:

- **Context pollution**: Tool outputs, long file reads, and debugging traces fill up the context window with low-value information
- **No branching**: You can't explore an idea without committing that exploration to the main conversation history
- **Wasted context**: When you investigate a bug and then want to continue where you left off, you either lose your carefully-built context by starting a new chat, or pollute it by continuing in the same one
- **Single agent**: Only one agent works at a time, with no way to parallelize independent tasks
- **No communication**: Agents working on different parts of a codebase can't share findings or coordinate

## The Vision

### 1. Conversation Trees (Time Travel)

Turn linear chat into a **tree structure**:

- **Branch** from any point in a conversation to explore an idea, debug something, or try a different approach
- **Merge** branches back: bring conclusions from an exploration branch back to the parent without bringing all the noise
- **Restore without reverting**: Jump back to an earlier conversation state while keeping code changes (like git checkpoint restore without reverting the working tree)

This maps directly to git concepts:
| Git | Multiverse |
|-----|-----------|
| Branch | Fork a conversation timeline |
| Merge | Bring conclusions/context from one timeline to another |
| Checkout | Switch active conversation timeline |
| Cherry-pick | Pull specific findings from one timeline into another |

### 2. Intelligent Context Management

A **context manager model** sits between the user/agent and the API:

- Maintains the full conversation history but presents a **curated view** to the working model
- **Summarizes** low-value sections (long tool outputs, file reads) while preserving that they happened
- **Unsummarizes** on demand when the agent needs full detail
- **Recall tool**: The working agent can always call `recall(message_id)` to retrieve the full original content of any summarized message
- Optimizes context per-request: different requests may need different parts of history expanded

Context management applies uniformly to all context sources -- user conversations, agent-to-agent responses, and tool outputs all go through the same curation pipeline before being sent to the API.

### 3. Parallel Agents on Git Worktrees

Multiple agents work simultaneously on **isolated git worktrees**:

- Each agent gets its own worktree (a real, full copy of the repo on a separate branch)
- Agents work in parallel without stepping on each other's files
- The orchestrator manages worktree lifecycle (create, merge, cleanup)
- User's own git branches/workflow are never touched -- all Multiverse branches live in a separate namespace (e.g., `multiverse/agent-1/fix-auth`)

### 4. Inter-Agent Communication

#### Conversation Storage

Each agent's context is composed from multiple **JSONL** (newline-delimited JSON) conversation files, matching Claude Code's native transcript format. Each line is a self-contained event with a `timestamp`, `uuid`, `parentUuid` (for threading), `type`, and `message` payload.

- **`user-agentX.jsonl`** -- the conversation between the user and Agent X
- **`agentA-agentB.jsonl`** -- the conversation between Agent A (asker) and Agent B (responder)

There is one file per agent-user pair and one file per agent-agent pair that has communicated. These files store the full, uncurated history. The context manager curates from these before each API call.

**Why JSONL (matching Claude Code's format):**
- **Append-only**: new messages are appended without rewriting the file, safe for concurrent agents writing simultaneously
- **Streaming-friendly**: the UI or orchestrator can tail/watch files as agents work in real time
- **Natively timestamped**: every event already has an ISO 8601 `timestamp`, making it trivial to interleave messages from multiple files chronologically during context assembly
- **Tree structure built in**: `parentUuid` linking already supports branching conversation topology
- **Claude Code compatibility**: the external UI can read both native Claude Code transcripts and Multiverse conversation files with the same parser

#### Asker/Responder Protocol

All inter-agent communication follows an **asker/responder** model:

- The **asker** is always the agent that initiates the conversation. For example, Agent A is tasked with merging Agent B's changes and needs details -- Agent A is the asker, Agent B is the responder.
- A conversation between two agents is always one-directional in terms of roles: if A asked B, the entire exchange (even if B asks clarifying questions back) is stored as `agentA-agentB.jsonl` with A as asker.
- **The responder cannot initiate a conversation back to the asker.** Anything it needs to say to the asker goes in its response. If B needs clarification from A, B asks within its response, and A answers in its next message -- but the roles don't flip.
- **The responder CAN initiate conversations with other agents.** While responding to A's request, Agent B has full tool access, including the ability to ask Agent C for help. This creates a chain: A asks B, B asks C, C responds to B, B responds to A.

#### Context Assembly Rules

When an agent receives a request (from user or another agent), its **full pre-curation context** is assembled as follows:

**For a user request to Agent X:**
| Source | Included |
|---|---|
| `user-agentX.jsonl` | Full conversation with user |
| `agentX-agentY.jsonl` (for all Y) | Only **responses X received** -- X was the asker, Y was the responder, so these contain Y's replies to X |
| `agentY-agentX.jsonl` (for all Y) | **Not included** -- Y was the asker, X was the responder, so these contain X's responses to others' questions |

**For Agent A asking Agent B:**
| Source | Included |
|---|---|
| `user-agentB.jsonl` | Full conversation with user |
| `agentB-agentY.jsonl` (for all Y) | Only responses B received -- B was the asker, Y was the responder |
| `agentA-agentB.jsonl` | **Full history** -- B can see its own previous responses to A, so it has continuity across multiple requests from the same asker |
| `agentY-agentB.jsonl` (for all Y, Y != A) | Not included -- Y was the asker, B was the responder, so these contain B's responses to others' questions |

The key asymmetry: an agent always sees responses it *received* from others but not responses it *gave* to others -- except when responding to a specific asker, where it sees its own prior responses to that asker for continuity.

**Assembly order**: Messages from all included JSONL files are interleaved by `timestamp` into a single chronological sequence. This means an agent sees events in the order they actually happened -- a user message, then a response from Agent C that arrived mid-conversation, then the next user message, etc. -- rather than seeing each source as a separate block.

#### Why This Design

- **No circular calls**: A can't call B who calls A. This prevents infinite loops and simplifies the execution model.
- **Context efficiency**: Agents don't need to see their own answers to others' questions -- that information is already internalized. They only need what others told *them*.
- **Chained delegation**: B can recruit C while answering A, enabling deep task decomposition without the asker needing to know about it.
- **Continuity**: B seeing its prior responses to A means repeated A-to-B conversations build on each other naturally.
- **Uniform curation**: All of the above is the *pre-curation* context. The context manager then summarizes, trims, and optimizes this assembled context before the actual API call, applying the same process regardless of source.

---

## Architecture

### Building on a runtime

Multiverse needs a handful of things from every CLI it drives: resumable on-disk sessions, a way to inject context, tools, and lifecycle events. Claude Code supplies plugin/MCP/hooks, Codex uses its plugin bundle, and `multiverse-plugin/pi-extension/` provides the equivalent integration for pi.

| Claude Code Feature | Multiverse Use |
|---|---|
| **Agent SDK** (`@anthropic-ai/claude-agent-sdk`) | Core orchestration -- spawn and manage multiple agent sessions programmatically |
| **Git worktrees** (`--worktree`, `isolation: worktree`) | Each parallel branch gets its own isolated worktree at `.claude/worktrees/` |
| **Hooks** (`PreToolUse`, `PostToolUse`, `Stop`, etc.) | Intercept agent lifecycle for context management and coordination |
| **MCP Servers** | Expose custom tools (recall, branch, merge, communicate) to agents |
| **Custom subagent definitions** (`.claude/agents/*.md`) | Define specialized agent roles with constrained tools/models |
| **Session transcripts** (JSONL at `~/.claude/projects/`) | External UI reads conversation history for visualization |
| **Plugins** | Package and distribute the entire Multiverse system |

### High-Level Components

```
+---------------------------+
|     Multiverse UI         |  Web UI: timeline visualization, branch/merge controls
|  (reads git + transcripts)|
+------------+--------------+
             |
+------------+--------------+
|   Orchestrator / CLI      |  Manages agent lifecycle, worktrees, communication
+---+--------+----------+---+
    |        |          |
+---v--+ +---v--+  +---v--+
|Agent | |Agent |  |Agent |   Each agent = Claude Code session via Agent SDK
|  1   | |  2   |  |  3   |   Each in its own git worktree
+---+--+ +---+--+  +---+--+
    |        |          |
+---v--------v----------v---+
|   Context Manager Layer    |  Summarize/unsummarize/recall per agent
+----------------------------+
    |
+---v------------------------+
|   Message Bus / Mailbox    |  Inter-agent communication
+----------------------------+
```

### Key Design Decisions to Explore

**Git isolation**: All Multiverse branches use a namespace prefix (e.g., `multiverse/`) and worktrees live under `.claude/worktrees/`. The user's branches are never modified. `.claude/worktrees/` is gitignored.

**Context manager placement**: The context manager can operate at two levels:
  - *Pre-API*: Rewrite the message history before each API call (more control, more complexity)
  - *Tool-based*: Give agents summarize/recall tools and let them self-manage (simpler, less reliable)
  - Likely a hybrid: automated summarization of tool outputs + agent-accessible recall

**Communication protocol**: How agents message each other:
  - Shared task list (Claude Code's experimental Agent Teams already does this)
  - Direct mailbox (agent A sends a message, agent B sees it on next turn)
  - Synchronous request/response (agent A blocks until agent B answers)

**UI separation**: The UI is a standalone app that reads:
  - Git repo structure (branches, worktrees) for timeline topology
  - Session transcript JSONLs for conversation content
  - A Multiverse metadata file for branch relationships and merge history

---

## Roadmap

### Phase 0: Foundation & Exploration
- [x] Set up repository
- [ ] Prototype with Claude Agent SDK: spawn a single agent session programmatically
- [ ] Prototype git worktree management: create/destroy worktrees, branch naming conventions
- [ ] Experiment with Claude Code hooks to understand lifecycle events
- [ ] Define the Multiverse metadata schema (branch relationships, merge history, context snapshots)

### Phase 1: Branching Conversations (Single Agent)
- [ ] Implement conversation tree data structure (branch, merge, checkout)
- [ ] Build "branch from here" -- fork conversation state at any message
- [ ] Build "merge back" -- inject summary/conclusions from a branch into parent
- [ ] Build "restore without revert" -- jump to earlier conversation state, keep code changes
- [ ] CLI commands or slash skills for branching operations

### Phase 2: Context Management
- [ ] Define JSONL schema for conversation files (`user-agent.jsonl`, `agent-agent.jsonl`)
- [ ] Implement context assembly logic (merge user conversations + received responses per the assembly rules)
- [ ] Implement context manager model that curates assembled context before API calls
- [ ] Auto-summarize large tool outputs (file reads, command outputs) after they've been processed
- [ ] Build `recall` tool for agents to retrieve full original content
- [ ] Track context budget per agent and trigger summarization proactively
- [ ] Benchmark: measure quality impact of context management vs raw history

### Phase 3: Parallel Agents
- [ ] Orchestrator that spawns multiple Agent SDK sessions on separate worktrees
- [ ] Agent lifecycle management (start, monitor, stop, resume)
- [ ] Task decomposition: break a user request into parallelizable sub-tasks
- [ ] Merge agent worktree branches back (handle conflicts)
- [ ] Progress tracking and status reporting

### Phase 4: Inter-Agent Communication
- [ ] Implement per-pair conversation files (`agentA-agentB.jsonl`) with asker/responder roles
- [ ] Build asker/responder protocol: initiation, response, chained delegation
- [ ] Implement context assembly rules (received responses only, asker history for continuity)
- [ ] Enforce no-circular-call constraint (responder cannot call back to asker)
- [ ] Merge conflict negotiation (two agents discuss how to resolve conflicts)
- [ ] End-to-end test: Agent A asks B, B recruits C, chain resolves correctly

### Phase 5: UI
- [ ] Web-based timeline visualization (conversation tree + git branch topology)
- [ ] Standalone app that reads git structure + session transcripts (no Claude Code embedding)
- [ ] Visual branch/merge/checkout controls
- [ ] Cross-branch context injection UI (drag findings from one timeline to another)
- [ ] Agent status dashboard (what each agent is doing, progress, communication log)
- [ ] "5D Chess" style multi-timeline view

### Phase 6: Polish & Distribution
- [x] Mixed runtime orchestration — Claude Code, Codex, and [pi](https://pi.dev) share one dashboard and agent registry
- [x] pi runtime — inter-agent tools, inbox delivery, native session discovery/resume, and native compaction
- [ ] Package as a Claude Code plugin
- [ ] Configuration system (which model for context manager, parallelism limits, etc.)
- [ ] Error recovery (agent crashes, worktree corruption, merge failures)
- [ ] Documentation and examples

---

## Open Questions

1. **Context manager model choice**: Should the context manager use a cheap/fast model (Haiku) for summarization, or the same model as the agent for better fidelity?

2. **Merge semantics**: When merging a branch back, what exactly gets merged? Options:
   - Just the code changes (git merge)
   - Code changes + a summary of findings
   - Code changes + selected messages from the branch conversation

3. **Agent autonomy vs user control**: Should agents autonomously decide when to branch/merge, or should the user always initiate? Probably start with user-initiated, add autonomous later.

4. **Worktree limits**: Git worktrees are real disk copies. For large repos, having many parallel agents could be expensive. Possible mitigations: shallow clones, sparse checkouts, or limiting parallelism.

5. **Communication overhead**: Every inter-agent message costs tokens. How to keep communication concise? Structured protocols? Token budgets per exchange?

6. **Session persistence**: How to handle long-running parallel work across terminal sessions? Agent SDK sessions can be resumed by ID, but orchestrator state needs persistence too.

7. **Conflict resolution**: When two agents modify the same file, do we:
   - Auto-merge and let an agent review?
   - Stop both agents and ask the user?
   - Have agents negotiate?

---

## Design Notes

- [`docs/compaction-algorithm.md`](docs/compaction-algorithm.md) — frontier-coherent tree compaction: the design
- [`docs/PROGRESS.md`](docs/PROGRESS.md) — what is built, what was learned, what is next
- [`multiverse-plugin/README.md`](multiverse-plugin/README.md) — the implementation

## Prior Art & Inspiration

- **5D Chess with Multiverse Time Travel** -- the visual metaphor for branching timelines
- **Claude Code Agent Teams** (experimental) -- native multi-agent with shared task list
- **Git worktrees** -- filesystem-level branch isolation
- **Cursor / Windsurf** -- current state of agent coding assistants (linear chat)
- **Ralph Wiggum Claude Code configs** -- community prompt engineering on top of Claude Code

