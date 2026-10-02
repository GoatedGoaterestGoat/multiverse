# Tree compaction

Multiverse can compact an agent's conversation itself, in place of its harness's
own summary. Each turn of the conversation gets a **summary tree**, built in the
background as the turn ends. When the session compacts, each turn's tree is
expanded to the share of a budget its scheduler gives it, and the old turns are
replaced by those partially expanded summaries. It is the same conversation in
the same session: the agent keeps its identity and system prompt, and nothing
tells it that a summary was made.

The algorithm is Multiverse v1's (`multiverse-plugin/vendor/compaction`), with
the same parser, merge prompts, schedulers and expansion. What changed is the
delivery: v1 rewrote the live transcript, while mv2 answers the harness's own
compaction (for Claude, the mod's `session.compact` hook). It is implemented
once, independent of any harness, in `src/multiverse/compaction/`; Claude Code
is the first harness that supports it.

## How it works

- **Turns and leaves.** A turn is one prompt and everything the agent did until
  the next prompt. Its compactable content is a list of leaves, as v1 reads
  them: thinking blocks, text, messages from you, and tool exchanges. A tool
  call and its result are always one leaf (v1's `auto_merge_tool_pairs`), so
  no merge can separate them; left apart, Sonnet split about one pair in
  twelve in v1's cached trees, nearly always right between a call and its
  result. The prompt itself is always
  kept verbatim. A turn the harness marks as not conversation (for Claude, a
  display-only command such as `/compact`, `/context` or `/model`) is left out
  entirely: never summarized, never kept.
- **Trees.** A model builds each turn's tree bottom up: on each step it merges one
  contiguous span of the current frontier into a summary node, until one root
  covers the turn. Node IDs follow merge order.
- **Expansion.** A turn whose share covers all of its leaves stays raw. Any other
  turn starts from its root and undoes the newest merges while the next one still
  fits its share.
- **Budget.** As in v1, `budget` covers the whole compacted conversation.
  Prompt text is always kept and comes off the top. Prompt images follow
  `image_mode`: `budget_fit` (the default) keeps the newest prompts' images
  while they fit half of what is left, `keep_all` keeps every one and
  `drop_all` none; a dropped image leaves a note in its prompt. A harness that
  cannot deliver images (Claude, below) always drops them. A scheduler
  shares the rest among the turns' assistant content:
  - `last_k_raw`: the last `k` turns whole; what is left shared by the earlier
    ones by size. Unlike v1, the last `k` (and their prompt images) stay raw
    even when they exceed the budget, so a budget of 0 summarizes every earlier
    turn to its root.
  - `proportional`: by turn size, tool output included (as in v1 with tool
    pairs joined).
  - `uniform`: equal shares.
  - `recency_boosted`: proportional, with recent turns weighted up to
    `recency_ratio` times, halving over `recency_half_life` turns.

## Configuration

A universe or subverse opts in with `compaction.json` at its root. The top level
is shared by every harness; `harnesses.<name>` overrides it for one harness (by
its directory under `harnesses/`). An override replaces `enabled` and `default`,
and merges into `builder` field by field.

```json
{
  "schema": 1,
  "enabled": false,
  "default": {"scheduler": "last_k_raw", "k": 2, "budget": 0, "image_mode": "budget_fit"},
  "builder": {
    "background": true,
    "model": "claude-sonnet-4-6",
    "effort": "medium",
    "concurrency": 1,
    "turn_retries": 3,
    "rate_limit": {"claude_max_retries": 3, "retries": 10,
                   "wait_seconds": {"first": 20, "multiplier": 2, "max": 300}}
  },
  "harnesses": {"claude": {"enabled": true}}
}
```

| Field | Meaning |
| --- | --- |
| `enabled` | Use tree compaction for this harness's sessions. Read at launch: restart an agent after changing it. |
| `default` | The scheduler of every agent that has none of its own, its budget and `image_mode`. |
| `builder.background` | Build each turn's tree as soon as the turn ends. |
| `builder.model`, `builder.effort` | The model that writes summaries and its effort (`low` to `max`). The default is Sonnet 4.6: Sonnet 5.5's safeguards refuse to summarize some short turns, such as a one-word reply after a thinking block, and one turn that cannot be built stops the whole compaction. |
| `builder.concurrency` | How many turns are built at once (each turn's merges run one after another). |
| `builder.turn_retries` | Immediate retries of a turn whose build failed (a refusal, an unusable reply, a crash), resuming from the merges that succeeded. |
| `builder.rate_limit.retries`, `wait_seconds` | Retries of one merge call that was rate limited, overloaded or failed in the API, waiting `first` seconds, multiplied by `multiplier` each time up to `max`. |
| `builder.rate_limit.claude_max_retries` | Claude Code's own quick retries inside each call (`CLAUDE_CODE_MAX_RETRIES`, 0-15). |

The base template ships the file with compaction off; the development template
enables it for Claude. A missing file means compaction is off.

Each agent can have its own scheduler; without one it follows the current
default:

```sh
mv2 compaction worker                                  # settings, trees, recent errors
mv2 compaction worker --scheduler proportional --budget 60000
mv2 compaction worker --scheduler last_k_raw --k 4 --budget 20000
mv2 compaction worker --image-mode drop_all             # never keep prompt images
mv2 compaction worker --use-default                     # back to the universe default
mv2 compaction worker --plan                            # each turn's share, raw or summarized
mv2 compaction worker --build                           # build the trees a compaction needs now
```

## Files

Everything belongs to the agent, under `state/agents/<id>/compaction/`:

| Path | Contents |
| --- | --- |
| `config.json` | The agent's own scheduler. |
| `trees/<turn>.json` | One tree per turn: summaries and structure, with leaves referenced by the harness's row IDs, not copied. A tree whose turn now reads differently is rebuilt. |
| `trees/<turn>.lock` | Held by the process building that turn. |
| `build.log` | Every build event, including errors that a retry later recovered from. |
| `status.json` | The current activity, the last 20 errors and turns whose build failed. |
| `turn-end.json` | Where the newest finished turn ended (the harness's marker). |
| `installs/<id>.json`, `installed.jsonl` | What each compaction installed, so later reads can leave those rows out. |

The native transcript is the source of every leaf, so it must be kept: Claude
Code's `cleanupPeriodDays` should be large.

## Compacting a session

When the session compacts, the harness runs `python -m multiverse.compaction
install`. It allocates the budget, builds any tree still missing (or waits for
the background builder working on that turn), expands the trees and streams the
result back. Turns kept whole never wait for a tree. Progress, retries and
errors are reported as they happen.

If anything fails (a turn whose tree cannot be built, a crash, a mismatch with
the session), the compaction is abandoned with the reason and the conversation
stays as it was. It never falls back to the harness's own compaction.

## Claude Code

Compaction needs the `multiverse` mod (`"inbox_bridge": true`). An enabled launch
sets `MV2_COMPACTION=1` for the mod and adds a `PreCompact` hook to its settings.

- **`/compact`** runs the mod's `session.compact` hook, which returns the
  compacted conversation in place of the old turns, every message built from
  the transcript's content: prompts and your messages as user text, summaries
  as plain first-person text (without v1's "Compacted summary" label), and
  each tool exchange as its call followed at once by its result. A call or
  result left unpaired (a session cut off mid-call) gets v1's placeholder:
  "(result summarized)" for a missing result, a `summarized_tool` call for a
  missing call. Claude writes a boundary, those
  rows and the `/compact` command's own rows, and re-announces its tool, agent,
  MCP and skill listings on the next turn. While it runs, the status line shows
  a progress bar, and every error and retry also appears as a transcript
  notice and a toast.
- **Nothing is kept by reference.** The hook could hand back a row Claude
  offered it, but Claude 2.1.287 stores such a row again with its old links: a
  kept tool result hangs off the call from before the compaction, and a kept
  response keeps its message ID, so loading the session again (Claude's own
  resume) brings back the old rows beside the copies. A built message cannot
  hold a thinking block or an image, so those are left out: a prompt notes how
  many images it had, and the budget spends nothing on them.
- **Never Claude's own summary.** The hook answers every main-session
  compaction, or skips it with the reason. If it ever cannot answer (the mod
  failed to load, the hook ran out of time), the `PreCompact` hook refuses Claude's
  own compaction with a message. A subagent's own compaction is left to Claude.
- **Background building.** The mod's `Stop` hook starts the builder with the time
  the turn ended, so the turn that just finished gets its tree at once.
- **Merges** are v1's privileged CLI merges, unchanged: `claude -p --resume
  <synthetic session>` with no tools, using your Claude login, v1's newest
  system prompt and instruction. The first step's session holds the turn's own
  rows; later steps rebuild it from the current frontier (the prompt, then one
  assistant message with each summary as text, each tool call and result as
  text, and each thinking block not yet merged verbatim with its signature).
  The files live in a Claude project of the agent's compaction directory and
  are deleted after each call. A refused merge is retried like any other
  failure (`turn_retries`), unchanged.
- **History across compactions.** Claude keeps every row in the transcript and
  links each boundary to the conversation before it, so later compactions read
  the whole conversation as it was. The rows an install added are told apart by
  counting them after its boundary, against the install's payload.

### Limits

- Token counts are v1's estimates: about four characters a token, images by
  their pixel size.
- **Thinking and the API.** Claude records what the API did to each request.
  Opus 5.5 drops a thinking block whose preceding conversation changed
  (`thinking_dropped`, `prefix_binding_mismatch`): so an Opus merge never sees
  the turn's thinking, and thinking kept in a compacted conversation is
  dropped once anything before it was summarized, though the budget still
  counts it. Sonnet 5.5 and Sonnet 4.6 keep the blocks in merge sessions, but
  Sonnet 5.5's safeguards refuse a turn whose substance is only in its
  thinking (`[reasoning_extraction]`), and Sonnet 4.6 did not reliably name a
  decision made only in thinking. Tested with Claude Code 2.1.287 on
  2026-10-01.
- Thinking is never kept in the compacted conversation (see above); it still
  reaches the merge model, and its estimated size still counts toward a raw
  turn's share.
- Claude 2.1.287 links each parallel tool result to its own call, and only the
  last result stored continues the conversation, so the transcript reader takes
  every block of a response and every result answering its calls (v1's reader
  missed these).
- Claude Code's mod API is early access; this was built and tested on Claude
  Code 2.1.287.
