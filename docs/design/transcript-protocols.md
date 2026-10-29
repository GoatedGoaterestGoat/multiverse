# Transcript protocols

How plugins read an agent's conversation and a harness's models, and what a
harness adapter provides for them. This record covers what API 1.13 builds:
reading rows by cursor, labels, history, context and turns, and a harness's
models with a check of a model and effort; and what API 1.14 adds: where a
history starts after the harness's own compaction, and the one-shot run.
Harness-neutral conversation entries come later (see [Later](#later)).

The protocols are mv2's own. A plugin uses them; it never defines what an
adapter implements ([plugins](plugins.md)). Reading is implemented for Claude and Codex. Pi reports it unavailable
("not implemented for this harness yet") until it gets its own implementation.

## Rules

- **Reading is observe.** It needs no grant beyond observe, and the plugin
  must be on in the agent's own universe ([effect classes and access
  grants](plugins.md#effect-classes-and-access-grants-113)). Manifests that
  list permissions need `transcripts.read`.
- **Core may bring its own records up to date first.** Before choosing which
  transcript to read, core checks whether the agent's process has moved to
  another session, and records a verified move, as listing and usage do
  ([session lineage](session-lineage.md)): in `sessions.jsonl`, and in the
  agent's current session record (`properties.json`'s `native_session_id` and
  `updated_at` move to the new session). The read itself changes nothing.
- **Nothing is recorded.** Row content never goes into the event journal,
  `errors.log`, status items or any log. Errors name a row by cursor or ref.
- **Bounded.** A page holds at most `limit` rows (default 200, at most 2000)
  and `max_bytes` (default 1 MiB, at most 16 MiB). A row larger than
  `max_bytes` comes alone; one larger than 16 MiB comes without its data.
- **Read-only, from the agent's records.** The native ID and the storage home
  come from the agent's records, never from the caller's environment. Inside
  the agent's own session, an agent launched before mv2 recorded its storage
  uses the session's inherited environment, as its launch did.
- **Not for render paths.** A cold read indexes the whole transcript once, so
  reads belong off the dashboard's event loop and outside a column's budget.

## Rows, cursors and generations

A **row** is one record as the harness keeps it: for Claude, one line of the
session's JSONL transcript. A **ref** is the harness's own ID for a row
(Claude's `uuid`); about a third of a long Claude session's rows have none.

A **cursor** is an opaque position between rows, made by core. It encodes the
generation, the adapter's own position and the nearest ref before it. Rows are
never addressed by number, because every view of a conversation shifts:

- a compaction stores kept rows again under new IDs;
- `/clear` starts a new session;
- a rewind leaves a branch behind;
- Claude removes a row by truncating its file at the row and writing the rest
  back (a tombstone), and can rewrite a whole file in place.

A **generation** is one native session in its recorded storage: the native
ID, the harness and the storage home together. It changes when the agent moves
to another session (`/clear`, a fresh start), never on a compaction, a moved
file or a rewrite. Earlier generations come from the agent's
`sessions.jsonl`; one whose transcript can still be found can be read with its
token.

Malformed generation syntax raises `api.Error`. For a well-formed token that
is unknown or whose transcript is unreadable, `agent.transcript()` reports
`state` (`unavailable`, or `error` for a provider error) and `reason`. Row and
turn reads raise `TranscriptUnavailable` with `retry=False` instead. This lets
a plugin check availability before asking for data, while keeping invalid
arguments distinct from unavailable storage.

When history changed under a cursor, the read raises
`api.TranscriptChanged` with a reason and a place to resume:

| Reason | Means | `resume` |
| --- | --- | --- |
| `replaced` | The cursor's generation is no longer the agent's | The current generation's start |
| `rewritten` | The bytes before the cursor changed | The same place, found again by the nearest ref, or none |
| `diverged` | A history read's row is no longer on the history (a rewind) | After the last row both histories share |

## Copies

A **copy** is one file holding a session's transcript. A Claude session
normally has one. Claude Code 2.1.310 moves a transcript by renaming it, and
sets an existing destination aside under a `.superseded-…` name. Reading uses
the newest copy. Older copies must hold a prefix of the newest copy's rows
(by uuid), or the read says it can't tell. The adapter's evidence readers,
which confirm deliveries, read every copy and check that copies agree.

## Labels

The adapter labels every row. That is all the harness knowledge turns need:
which rows are real prompts.

| Label | Means |
| --- | --- |
| `prompt` | Starts a turn. `source` says who: `human`, `plugin` (mv2's deliveries), `peer`, `task-notification`, `auto-continuation`. A prompt mv2 puts on Claude's command line, such as a spawn's `--task`, is `human`: Claude stores it as typed |
| `mid_turn` | Joins the running turn: a steer, or a prompt typed while the model works |
| `tool_result` | A tool's result |
| `command` | Starts a slash command; core makes it a turn of its own |
| `command_output` | That command's echo, caveat or output |
| `injected` | Context the harness added: reminders, file and environment notes, a background notice merged mid-turn, a skill's instructions |
| `compaction` | Written by a compaction: the boundary, a summary, rows it stored |
| `response` | The model's output |
| `meta` | Anything else: titles, modes, snapshots, API request records |

Claude's rules, checked on rows written by Claude Code 2.1.286 to 2.1.310:

- A `prompt` is a user row with `origin`, which only rows that start a turn
  have. `source` is `origin.kind`. Rows from versions without `origin` fall
  back to a new `promptId` with text or image content.
- A `mid_turn` message is a `queued_command` attachment with `commandMode`
  `prompt`. With `commandMode` `task-notification` it is `injected`.
- A `command` is a typed slash command (a new `promptId`, no `origin`), or
  the `<command-name>` or `system/local_command` row that starts one.
- A skill's instructions are a user row with `isMeta`, `turnCompanion` and
  `sourceToolUseID`: `injected`, with `call_id` naming the tool call.
- Claude's interruption row ("[Request interrupted by user…") is `injected`
  and ends its turn.
- `compaction` covers the `compact_boundary` row, Claude's summary
  (`isCompactSummary`), and the rows after a boundary up to the first
  assistant row with nonzero usage, user row with `origin`, or `tool_result`
  row with `toolUseResult`. That finds the rows a `session.compact` hook
  installed, which carry no field of their own, without knowing the plugin
  that wrote them.

### Codex rollout rules

Codex reads the exact recorded thread's rollout under its recorded `CODEX_HOME`, including `archived_sessions`. These are read-only file operations for plain and bridge agents alike. Draft 7 proposed native paged reads; those could come later if the reader contract gains an executor. Ephemeral threads such as `/btw` have no rollout, so their transcript is unavailable.

- Every complete JSONL line is one row. An unfinished last line waits for its newline. Damaged rows remain as `meta` with no body. Refs are native item IDs from response items or inter-agent communication only; session metadata, turn context and events have no item ref. A missing item ID leaves `ref=None`. `call_id` separately correlates a call and its output. Positions chain the hashes of all preceding line bytes. A ref-less prompt gets core's cursor-based turn ID.
- A user-role `response_item/message` whose content metadata has native `provenance.type=user` is `prompt`, or `mid_turn` after the first input in the same native turn. Native harness/skill/AGENTS.md provenance stays `injected`, as do inherited parent messages. For older rows without provenance, an ordered exact plain-text matching `event_msg/user_message` or `item_completed` with a `UserMessage` item authenticates the input. These event echoes are `meta`. Unpaired or ambiguous legacy inputs stay `injected`; fewer echoes than indistinguishable inputs cannot authenticate one occurrence. Legacy media inputs need native provenance because their UI echoes are not exact model-input copies. Old and new echo streams are counted separately to avoid double counting an input. Unmatched user messages and developer/system messages are `injected`. Message framing never proves provenance, so ordinary input has `source=None`.
- Native `inter_agent_communication` is `prompt` with source `peer` when `trigger_turn` is true, and `mid_turn` otherwise. Assistant messages, reasoning and tool calls are `response`. Tool outputs are `tool_result`, with their native `call_id`. Other events are `meta`; `turn_aborted` is `injected`.
- `task_complete` (also read as `turn_complete`) and `turn_aborted` end the active native turn only when their `turn_id` matches its recorded start. A final assistant message alone proves no end. Core still requires a usable timestamp at or after the prompt.
- A `compacted` row and a response marked `metadata.compaction_output` are `compaction`. Its replacement history remains nested in its body and never creates prompts. Context starts at the latest surviving `compacted` boundary.
- `thread_rolled_back.num_turns` removes the newest non-contextual native input boundaries and their following rows, including steers within one public prompt turn. These boundaries are tracked separately from public turn grouping. The cut moves backward over immediately preceding contextual developer/user model messages, as native `trim_pre_turn_context_updates` does; rollout bookkeeping and response records filtered by native `is_api_message` (system messages, unowned configuration updates, compaction triggers and `Other`) do not block it. Harness-authored configuration updates and other actual model items still act as barriers. Context before the first ordinary input and context separated by a non-context model item stay. An unknown fragment or malformed context shape makes trimming unreadable rather than preserving guessed instructions. Supported native input/context predicates, ordinary-user provenance and unambiguous plain-text legacy echoes establish the boundaries; native inter-agent items also count. Context-looking legacy text cannot gain a rollback boundary from its echo alone. Uncertain input boundaries make a rollback history unreadable rather than deleting an earlier proven prompt. A compaction checkpoint whose tail cannot be proved a non-context barrier is also unreadable when trimming would reach it, because its replacement history is one stored row. Raw reads retain these abandoned rows. A native item ref can select the stored history before that rollback. An unknown tip falls back to the current history with its actual tip and no gap. An in-place rewrite changes positions instead.
- Copies must agree byte for byte through the older copy's complete rows. The newest copy is read only when each older copy is its prefix. Every identity-bearing session header must name the recorded thread under the same strict duplicate-key parsing as the first header; an ambiguous later header remains an invalid identity record. Other ambiguous records remain unreadable metadata and cannot supply rollback or model-membership proof. A completed metadata scan is validated before publication or caching; a changed file needs a stable open-file prefix proof, so mixed scans never release previews. Conflicting copies, unreadable storage, links, unsupported matching filenames, compressed rollouts without a plain sibling, and non-null `session_meta.history_base` report an error rather than guessed history. Body reads permit concurrent appends, re-proving the indexed prefix on the opened file when its signature changes; a rewrite hidden by growth still fails. Native `thread/revert` can create a referenced child rollout under the same thread ID; even a lone child is refused because its ancestor ranges are not implemented. Earlier-generation readability uses these same consistency and history checks, returning unknown for unsupported or inconsistent storage.

## History and context

The **history** is the conversation as it now stands, across every
compaction, without the rows a rewind abandoned. The adapter computes it,
since only it knows the harness's linking rules (Claude: `parentUuid`, and
`logicalParentUuid` across a boundary; parallel tool results branch off their
own call). A row off the history is **abandoned**.

- A history read (`history=True`) returns the history's rows in order and
  takes an optional **tip**, a row ref. Right after a rewind,
  before the next prompt, only the live session knows which branch is
  current. Without a stored tip, the history starts at the newest leaf.
- A **gap** is a parent the history needs that Claude never stored (its
  writes failed for a while). It is reported on the read. When a Claude
  compaction lies between the tip and the gap, the history starts at Claude's
  summary instead, with the boundary's `preservedMessages` rows right after
  it in their order, as Claude itself resumes the session, and no gap is
  reported. The read names that summary instead (`summary`, 1.14): it is then
  the only record of what came before.
- The typed `/compact` row stays on the history, although the boundary's link
  skips it.
- A row without a ref (Claude's API request records, queue and title rows)
  belongs to the history with the nearest row before it that has one, and
  comes right after it, so it is abandoned exactly when that row is.

A row is **in context** when the latest compaction on the history kept it:
from the latest boundary on, or every row when there was none. The model may
still receive less (Claude's server-side context editing, thinking a model
drops).

## Turns

Core groups turns once, from labels, over the history. No adapter implements
turns.

- A `prompt` row starts a prompt turn, which becomes the current one.
- A `command` row starts a command turn, which holds its `command_output`
  rows and the `compaction` rows written while it is the latest turn (so a
  typed `/compact` holds its compaction).
- Every other row joins the current prompt turn.
- Rows before the first turn form the `preamble` turn.

A command run mid-turn is its own turn, and the running turn's later rows
stay with their prompt. Turns are listed by where they start. A turn's ID is
its first row's ref. Since turns interleave in history order, each history
row names its turn (`Row.turn`, 1.14; `None` for an abandoned row), so a
plugin slices one history read by turn instead of reading turn by turn.

**Finished:**

- A prompt turn is finished once a later prompt turn is stored, or a
  turn-end row of its own, timed at or after its prompt. A command turn run
  meanwhile doesn't finish it. A turn-end row without a usable time leaves
  "finished" unknown.
- A command turn is finished once its output is stored.
- Claude's turn-end rows are `system/turn_duration` and its interruption row
  ("[Request interrupted by user…"). An interrupted turn usually writes no
  `turn_duration`.
- Claude writes a turn's last rows after its Stop hooks run, so a turn never
  counts as finished from the Stop hook or the agent's status alone.

## The adapter's part

An adapter that provides transcripts implements `TranscriptProvider` in
`harnesses/types.py`: a view of one generation for one tip (row facts without
bodies, the history order, where context starts, the gap), row bodies by
index, and whether an earlier generation's transcript can still be found.
Its methods are observe in `EFFECTS`. Core owns cursors, bounds, the
generation token, turn grouping and the access checks, and never parses a
harness's rows.

The Claude adapter reads every transcript through
`harnesses/claude/transcripts.py`: finding a session's copies (the folder
rule, regular files only, an unreadable folder that could hold the session as
"can't tell"), an incremental index of the newest copy, rebuilt when the
copy shrinks, changes inode or changes before a cursor, and raw copies for the
evidence readers.

## Models

`mv.universe(root).harness(NAME)` gives a harness's `info` (what
`mv.harness_info` gives), `models()` and `check(model, effort=)`. Both calls
are observe, need the plugin on in that universe, and spend nothing: a
settings screen checks a model before a run would fail on it.

- **From a table, for Claude.** `harnesses/claude/models.json` is generated
  from the two model catalogs built into the Claude Code binary: its
  hand-maintained catalog, and the release copy of its downloadable catalog,
  which is what Claude's /model picker shows. The table is stamped with the
  Claude Code version it came from and regenerated by each Claude Code version
  check. mv2 never reads the binary at run time, nor Claude's
  `cache/model-catalog` or any other account cache (the user's choice).
  `source` is `"table"`; a harness that lists its own models natively will say
  `"native"`.
- **For a provider.** Aliases and IDs depend on where the harness sends
  requests: first-party `sonnet` is not Bedrock's. The adapter decides the
  provider the way Claude does, from the environment a launch from this
  process would get (the harness's settings `env` over this process's,
  without mv2's own variables). A cloud gateway chosen from Claude's login
  state isn't visible to mv2, so aliases there may resolve to older models.
- **Defaults and hidden models.** A model's default effort is the
  hand-maintained catalog's, else the release copy's. The release copy's
  global default model is marked `default`, and a model its picker doesn't
  list for the launch's provider is `hidden` (`--model` may still take it).
  These are Claude Code's built-in catalogs, not the account's, so an account
  may not offer every model or may have another default.
- **The check refuses what Claude Code would change silently.** Claude Code
  never rejects an effort: an effort a model doesn't take is sent as another,
  or as none. So `check()` is not ok for one, with the reason ("claude-sonnet-4-6
  does not take effort xhigh"). `[1m]` is accepted only where the model has a
  long context window. A model the table knows but the provider doesn't offer
  is not ok.
- **Free-form.** Claude takes models the table doesn't list (account-specific
  ones, newer ones). Those pass the check with `verified` false and a reason
  saying the table doesn't know them; Claude decides at the request.
- **`effort`** in a check is what a run would use: the one given, the model's
  default when none was given, or none for a model that takes none.
- **Codex's release table.** `harnesses/codex/models.json` is generated by `scripts/gen_codex_models.py` from the official release's `codex-rs/models-manager/models.json`, with its version and source hash. It never reads an account cache, a running daemon or `model/list`. The table supplies IDs, display names, efforts, default effort, input modalities, context windows and picker visibility. The first picker-visible model by native priority is the release default. Account defaults and custom providers can differ, so `provider=None` and a verified check means the release table knows the ID and effort, not that an account offers it. Unknown IDs remain free-form and unverified. No environment variable alone proves Codex's configured provider or login mode.
- **Unavailable.** A harness without models (Pi for now, a custom
  command harness) raises `api.ModelsUnavailable`, so a settings screen can
  offer a free-form field. An adapter's own failure becomes the same error
  with a fixed reason, never with what the adapter read.

An adapter provides models by implementing `ModelProvider` in
`harnesses/types.py`: `models(environment=)` and `check_model(model, effort=,
environment=)`, both observe in `EFFECTS`. Core passes the environment; the
adapter reads only the variables it needs from it.

## The one-shot run

`harness.run_once(messages, prompt=, system=, model=, effort=, agent=,
transport_retries=, timeout=, cancel=)` (API 1.14, `spend`, needs
`models.FAMILY`) gets one model reply to a context the plugin supplies, with
no tools and no saved conversation. `run_options()` (`observe`, but it may start
the harness once per process to read its version) says whether a
harness runs at all and what it keeps. Claude implements it; Codex, Pi and
custom harnesses report it unsupported.

- **Messages.** A `Row` is the native passthrough: this harness's format, with
  its body, used in the order given as one linear conversation, with the
  fix-ups only the adapter knows. A `Message(role, blocks)` of `TextBlock` and
  `ImageBlock` is built by the adapter. Shape errors the API sees (types, a
  row without a body or of another format) are `api.Error`; the harness's own
  checks (Claude: messages, when there are any, end on the model's, and
  every tool call is answered; with none the prompt runs alone)
  are `RunError("invalid_request")`. Both come before anything is written or
  spent, and so does `check_model` on the run's environment.
- **Environment.** What a launch from this process gets (its environment
  without `MV2_*`, the harness's `unset_env` removed, its settings `env` on
  top), then, for `agent=`, that agent's replayable `HarnessInfo.storage`,
  then a final scrub of every agent identity. Unproven storage is refused
  ("resume it once") except inside the agent's own session. A relative
  storage variable is made absolute where it was set (the agent's recorded
  workspace, or this process's directory without an agent), since the run's
  process starts in a directory of its own. Provider routing
  is this process's, not the agent's: launches don't record it.
- **The run** is core's (`runtime/runs.py`): a private directory under
  `~/.multiverse/runs/`, the adapter's declared session file created
  exclusively and removed afterwards, a small supervisor that owns the child's
  process group, the deadline and `cancel`, and stops it with TERM then KILL
  when the caller ends. A reply is returned even when cleanup leaves
  something behind: the next run sweeps what is proven dead, and `mv2
  doctor` reports what is left. Input is bounded at 32 MiB (over it,
  `too_long`); output is bounded and never logged.
- **Errors** are `api.RunError` with an open `kind` (`rate_limit`,
  `usage_limit`, `refused`, `too_long`, `invalid_request`, `timeout`,
  `cancelled`, `unavailable`, `failed`) and `retry_after`, carrying the
  harness's own error text and never the run's input or reply.
- **Records.** Each run appends its metadata (time, harness, model, agent,
  input, output and cache read and write tokens, cost, duration, outcome) to `state/plugins/NAME/runs.jsonl` in the
  run's universe, never content; `mv2 plugins info` sums the last day. The
  event journal is not that record: it is written only while a plugin
  subscribes, and pruned.

### Claude

The Claude plan (`harnesses/claude/run.py`) resumes a session it declares in
the selected store's `projects/<key of the run directory's real path>/`,
named by the run's ID, and keeps nothing. With no messages it declares no file
and leaves out `--resume`: Claude answers the prompt alone. A run directory
whose key would pass 200 characters gets Claude's own shortened name: its first
200, `-`, and a base-36 hash of the path:

```text
CLAUDE -p --resume RUN_ID --no-session-persistence --tools ''
  --strict-mcp-config --disable-slash-commands --setting-sources user
  --max-turns 1 --output-format json --model M [--effort E]
  --system-prompt S --system-prompt-snapshot off -- PROMPT
```

- **Command.** `CLAUDE` is the harness's `claude` looked up once on the
  run's own `PATH`, as one absolute path: `run_options()` checks that
  binary's version and the plan runs it, so a `PATH` from the harness's
  settings can't make the check and the run use different Claudes. The
  prompt comes last, after `--`, so Claude reads it as text whatever it
  starts with; before `--`, a prompt such as `--resume=ID` would be an option
  and read another conversation. `--max-turns` is hidden in Claude Code's
  help but present.
  `--system-prompt-snapshot off` (2.1.267 and newer; `run_options()` refuses
  an older Claude) keeps a recorded prompt from replacing the run's own. The
  model and effort are the ones `check_model` resolved for the provider; a
  model that takes no effort gets no `--effort`.
- **Environment.** The plan adds only its own variables:
  `CLAUDE_CODE_MAX_RETRIES` from `transport_retries` (0 to 15),
  `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK` and
  `CLAUDE_CODE_DISABLE_REFUSAL_RETRY` (Claude retries a refused request and
  can switch models after a flagged message, so a refusal would otherwise come
  back answered by another model), and `CLAUDE_CODE_DISABLE_CLAUDE_MDS` and
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY`. It removes
  `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS`, so a run loads no mod; core removes the
  identity every launch drops.
- **What still loads.** `--setting-sources user` keeps the user's own
  settings, so their enabled Claude plugins' hooks and language servers still
  start. `--strict-mcp-config` skips MCP servers; `--bare` would skip the rest
  but reads no OAuth login. The model gets no tools. A real 2.1.312 run read no
  CLAUDE.md from its own folder even without the switch, which stays for the
  user's `~/.claude/CLAUDE.md`.
- **Rows** (passthrough) get the run's session ID, folder and a linear parent
  chain; `isSidechain`, `logicalParentUuid` and `agentId` are dropped, and
  recorded usage is cleared, since Claude sizes a resumed session from the last
  usage it finds and would refuse a small run as too long. Each keeps its
  recorded model: copied thinking carries the writing model's signature.
  Rows are user and assistant rows with their bodies, plus a message Claude
  took while busy (a prompt typed or a message delivered mid-turn), which it
  stores as an attachment of type `queued_command`: that is conversation, and
  Claude replays it in a resumed session. Any other attachment is Claude's own
  and is refused.
  Claude runs a response's tool calls as they stream in and stores each
  result as soon as its call ends, so one response with several calls can be
  stored call, call, result, call, result. The session lays such a response
  out as the API saw it: its own rows in their stored order, then the rows
  holding the results of its calls. Only a row that holds nothing but results
  for that response's calls moves; anything else ends the response there.
- **Messages** (built) are stored the way Claude stores them: a user message
  as one row, images only there; an assistant message as one row per block,
  sharing one message ID, `end_turn` on the last only, on the run's model,
  with empty usage. No title row: a real run needed none.
- **Checks** before anything is written: the conversation ends with an
  assistant message, every tool call has exactly one result in the next user
  message, no result lacks its call, and no call ID repeats (Claude fails
  those as a 400 `tool_history_mismatch`). A response split by a typed
  prompt is not refused: Claude 2.1.312 resumes it and sends one assistant
  message with all its calls, then their results and the prompt in one user
  message. The check refuses only what Claude would refuse, since a refused
  turn is never built.
- **Reply.** Claude's JSON result. Its typed `api_error` comes first:
  `usage_limit_reached`, `model_requires_usage_credits` and
  `long_context_credits_required` are `usage_limit`;
  `tool_history_mismatch` is `invalid_request`; `safety_monitor_blocked` is
  `refused`; `no_response` is `rate_limit`; `provider_credentials`,
  `gateway_signin_required`, `gateway_session_expired` and
  `api_key_auth_disabled` are `failed`. Then the text rules: `stop_reason`
  `refusal` is `refused`, "Prompt is too long" `too_long`, a 404 on the model
  `invalid_request`, 408, 429, 529, 5xx, overloaded or network `rate_limit`,
  a usage-limit message `usage_limit`, anything else `failed`. A failed run
  still reports the model, usage and cost the result gives. `retry_after` is
  None: the JSON result has none.

## Later

- **Conversation entries** (API 1.15): each row's content in one form for
  every harness.
- **Pi reading and models; Codex and Pi one-shot runs; a schema for the reply; provider routing recorded at
  launch for per-agent runs** ([future work](future-work.md)).
