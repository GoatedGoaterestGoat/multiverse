# Transcript protocols

How plugins read an agent's conversation and a harness's models, and what a
harness adapter provides for them. This record covers what API 1.13 builds:
reading rows by cursor, labels, history, context and turns, and a harness's
models with a check of a model and effort. The one-shot run and
harness-neutral conversation entries come later (see [Later](#later)).

The protocols are mv2's own. A plugin uses them; it never defines what an
adapter implements ([plugins](plugins.md)). Reading is implemented for Claude.
Codex and Pi report it unavailable ("not implemented for this harness yet")
until they get their own implementation.

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
  reported.
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
its first row's ref.

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
- **Unavailable.** A harness without models (Codex and Pi for now, a custom
  command harness) raises `api.ModelsUnavailable`, so a settings screen can
  offer a free-form field. An adapter's own failure becomes the same error
  with a fixed reason, never with what the adapter read.

An adapter provides models by implementing `ModelProvider` in
`harnesses/types.py`: `models(environment=)` and `check_model(model, effort=,
environment=)`, both observe in `EFFECTS`. Core passes the environment; the
adapter reads only the variables it needs from it.

## Later

- **The one-shot run** (API 1.14): one model reply to a context a plugin
  supplies, with no tools and nothing saved.
- **Conversation entries** (API 1.15): each row's content in one form for
  every harness.
- **Codex and Pi** ([future work](future-work.md)).
