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
compaction (for Claude, a `session.compact` hook). It is the bundled
`compaction` plugin (`src/multiverse/plugins/builtin/compaction/`): its engine
(`engine/`) is independent of any harness, each harness family has a source
(`sources/`), and Claude Code is the first harness that supports it. It reaches
mv2 only through the plugin API (`multiverse.plugins.api`, and
`multiverse.plugins.ui` for its screens).

## How it works

- **Turns and leaves.** A turn is one prompt and everything the agent did until
  the next prompt. Its compactable content is a list of leaves, as v1 reads
  them: text, messages from you, and tool exchanges. A tool
  call and its result are always one leaf (v1's `auto_merge_tool_pairs`), so
  no merge can separate them; left apart, Sonnet split about one pair in
  twelve in v1's cached trees, nearly always right between a call and its
  result. The prompt itself is always
  kept verbatim. A turn the harness marks as not conversation (for Claude, a
  display-only command such as `/compact`, `/context` or `/model`) is left out
  entirely: never summarized, never kept. The Claude mod's `/multiverse inbox`
  and `/multiverse send` runs are the exception: they carry the inbox it read
  and the messages it sent, so each is a turn of its own, the command as its
  prompt and its recorded output as its leaf. Bare `/multiverse`, `/multiverse
  list` and `/multiverse <name>` are left out; the prompt `<name>` submits is an
  ordinary turn.
- **No thinking.** Unlike v1, thinking is no part of compaction: it is not a
  leaf, the merge model never sees it, and it is never delivered. The size of
  a conversation before compaction includes its thinking; afterwards there is
  none. See Limits for why.
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

Compaction is the bundled `compaction` plugin. `plugins.json` turns it on or off
for a universe or subverse (`mv2 plugins enable compaction`, or the dashboard's
Plugins screen), and `plugin-settings/compaction.json` configures it. While it
is on it applies to every harness that supports it (today Claude), and
`harnesses.<name>.enabled: false` turns one harness off. The scheduler
`default` is shared by every harness, and `harnesses.<name>` (by its directory
under `harnesses/`) may replace it. Tree building belongs to one harness, since
its transcripts and models are its own: `builder` exists only under
`harnesses.<name>`. The harness supplies the defaults of its model, fallback
model, effort and own options, and checks them.

```json
{
  "schema": 2,
  "default": {"scheduler": "last_k_raw", "k": 2, "budget": 0, "image_mode": "budget_fit"},
  "harnesses": {
    "claude": {
      "builder": {
        "model": "sonnet",
        "fallback_model": "claude-sonnet-4-6",
        "effort": "medium",
        "claude_max_retries": 3,
        "background": true,
        "concurrency": 1,
        "turn_retries": 3,
        "rate_limit": {"retries": 10, "wait_seconds": {"first": 20, "multiplier": 2, "max": 300}}
      }
    }
  }
}
```

| Field | Meaning |
| --- | --- |
| `harnesses.<name>.enabled` | `false` keeps this harness's sessions on their own compaction while the plugin is on. Read at launch, as the plugin switch is: restart an agent after changing either. |
| `default` | The scheduler of every agent that has none of its own, its budget and `image_mode`. |
| `builder.model`, `builder.effort` | The harness's model that writes summaries, and its effort. Claude: default `sonnet`, efforts `low` to `max` (default `medium`). |
| `builder.fallback_model` | Takes over a turn that `model` still refuses after `turn_retries`, from the merges already made; `null` fails the turn instead. Claude: default `claude-sonnet-4-6`. Sonnet 5.5's safeguards refused some short turns when merges still showed thinking, and one turn that cannot be built stops the whole compaction. Sonnet 4.6 refused nothing in testing but builds nearly flat trees (a 22-leaf turn in 2 merges, where Sonnet 5.5 made 6 nested ones), so it serves only as the fallback. |
| `builder.claude_max_retries` | Claude only: Claude Code's own quick retries inside each call (`CLAUDE_CODE_MAX_RETRIES`, 0-15, default 3). |
| `builder.background` | Build each turn's tree as soon as the turn ends. |
| `builder.concurrency` | How many turns are built at once (each turn's merges run one after another). |
| `builder.turn_retries` | Immediate retries of a turn whose build failed (a refusal, an unusable reply, a crash), resuming from the merges that succeeded. |
| `builder.rate_limit.retries`, `wait_seconds` | Retries of one merge call that was rate limited, overloaded or failed in the API, waiting `first` seconds, multiplied by `multiplier` each time up to `max`. |

A missing settings file means the defaults above. A top-level `enabled` is
refused: `plugins.json` is the switch.

**Before the plugin: `compaction.json`.** Universes made before compaction
became a plugin have `compaction.json` at their root (`"schema": 1`), with a
top-level `enabled` (off by default) and `harnesses.<name>.enabled` deciding
which harnesses compact. While that file is there and `plugins.json` says
nothing about compaction, the plugin is on and reads it exactly as before.
`mv2 plugins migrate` shows the conversion and `--apply` makes it: the plugin is
turned on in `plugins.json` where the old file left compaction on for some
harness; `plugin-settings/compaction.json` gets everything but `enabled` (keeping
`harnesses.<name>.enabled: false` for a harness that was off while another was
on); and the old file goes to the universe's trash. A harness the old file did
not name follows the plugin's switch from then on.

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

When the session compacts, the harness runs the plugin's `install` helper
action (`python -m multiverse.plugins run compaction install`). It allocates the budget, builds any tree still missing (or waits for
the background builder working on that turn), expands the trees and streams the
result back. Turns kept whole never wait for a tree. Progress, retries and
errors are reported as they happen.

If anything fails (a turn whose tree cannot be built, a crash, a mismatch with
the session), the compaction is abandoned with the reason and the conversation
stays as it was. It never falls back to the harness's own compaction.

### One-off scheduler settings

The compaction command's arguments set the scheduler for that compaction only;
the agent's saved scheduler (or the universe default) stays as it is. In Claude
Code that is the text after `/compact`. Arguments start from the scheduler in
effect and change only what they name:

| Argument | Meaning |
| --- | --- |
| `last_k_raw`, `proportional`, `uniform`, `recency_boosted` | The scheduler. A different one keeps the budget and `image_mode` and takes its own defaults for the rest. |
| a number: `60000`, `60k`, `1.5m` | The budget, in tokens. |
| `scheduler=NAME` | The scheduler, as above. |
| `budget=N` | The budget, as above (`k` and `m` suffixes allowed). |
| `k=N` | `last_k_raw`: how many recent turns stay whole. |
| `recency_ratio=X`, `recency_half_life=N` | `recency_boosted`: weight of the newest turn, and turns until it halves. |
| `image_mode=MODE` | `budget_fit`, `keep_all` or `drop_all` (Claude always drops prompt images). |

Keys may be written with `-` for `_`, in any order, each at most once. For
example:

```text
/compact                         the agent's scheduler, unchanged
/compact 60k                     the agent's scheduler with a budget of 60000
/compact proportional 60k        proportional, budget 60000
/compact k=4                     last_k_raw keeping the last 4 turns whole
/compact recency_boosted budget=40k recency_ratio=4
```

Anything else (free-text instructions, an unknown setting, a value out of
range, a setting the chosen scheduler does not take, such as `k` with
`proportional`) refuses the compaction with a message listing what is
accepted, before any work; the conversation stays as it was. Tree compaction
cannot follow free-text instructions: its summaries are written ahead of time,
per turn. The result line names the settings used, e.g. `(this time:
proportional, budget 60000)`; image mode is left out of it. To change an agent's scheduler for good, use `mv2
compaction AGENT --scheduler ...` or the dashboard (`P`, then compaction's
Configure: its settings screen, where Enter on an agent opens that agent's).

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
  MCP and skill listings on the next turn. Its arguments are one-off scheduler
  settings (see [One-off scheduler settings](#one-off-scheduler-settings)).
  While it runs, the status line shows
  a progress bar, and every error and retry also appears as a transcript
  notice and a toast.
- **Nothing is kept by reference.** The hook could hand back a row Claude
  offered it, but Claude 2.1.287 stores such a row again with its old links: a
  kept tool result hangs off the call from before the compaction, and a kept
  response keeps its message ID, so loading the session again (Claude's own
  resume) brings back the old rows beside the copies. A built message cannot
  hold an image, so a prompt notes how many images it had, and the budget
  spends nothing on them.
- **Claude's own summary only while compaction is off.** With tree compaction
  enabled, the hook answers every main-session compaction, or skips it with the
  reason; if it ever cannot answer (the mod failed to load, the hook ran out of
  time), the `PreCompact` hook refuses Claude's own compaction with a message.
  A subagent's own compaction is left to Claude. With the plugin off, or the
  harness opted out, there is no guard and Claude compacts as usual.
- **An unusable settings file** (invalid, or builder settings Claude rejects)
  never stops a launch or resume: the session starts as if compaction were off,
  so Claude's own `/compact` works until the file is fixed and the agent
  restarted. The reason shows in the inspector (as the launch's
  fallback), in the `compaction` capability and in `mv2 doctor`.
- **Background building.** The mod's `Stop` hook starts the builder with the time
  the turn ended, so the turn that just finished gets its tree at once. A turn
  end can be lost (a turn that ends in an error runs no `Stop` hook; a restart
  can interrupt one), so the session's start counts as a turn end too: every
  turn so far has ended, and the builder catches up on any left unbuilt. Both
  start a builder only while background building is on. A hot reload of the mod
  (its files changed) starts its state over: the hook awaits its builder call so
  a reload cannot cancel it, and reads the status again after one.
- **Status line.** The mod's status line (`statusline.json`'s `compaction` item)
  shows completed turns that have their tree out of all completed turns,
  `trees 51/52` (a turn of one leaf is its own tree; the turn in progress never
  counts), with `· building` while the background builder works, turns whose
  build failed, and a failed `/compact`. It is read when the session starts,
  when a turn ends, every 15 s while a builder runs and after each `/compact`
  (the plugin's `status` helper action). A running `/compact` shows its
  progress in the same slot.
- **Merges** are v1's privileged CLI merges, unchanged: `claude -p --resume
  <synthetic session>` with no tools, using your Claude login, v1's newest
  system prompt and instruction. The first step's session holds the turn's own
  rows, without thinking; later steps rebuild it from the current frontier (the
  prompt, then one assistant message with each summary, text and tool exchange
  as text).
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
- **Why no thinking.** A signed thinking block can only be delivered by keeping
  Claude's own row, which breaks resume (see above). And Claude records what
  the API did to each request: Opus 5.5 drops a thinking block whose preceding
  conversation changed (`thinking_dropped`, `prefix_binding_mismatch`), so
  thinking kept after a summarized turn would be dropped anyway, and an Opus
  merge never saw it either. Shown to Sonnet 5.5, a turn whose substance was
  mostly thinking was refused (`[reasoning_extraction]`). Tested with Claude
  Code 2.1.287 on 2026-10-01.
- Claude 2.1.287 links each parallel tool result to its own call, and only the
  last result stored continues the conversation, so the transcript reader takes
  every block of a response and every result answering its calls (v1's reader
  missed these).
- **Session-only scheduled tasks do not survive a resume.** Since Claude Code
  2.1.290, Claude's own compaction writes a `session_cron_carry` row listing
  the session's live, non-durable scheduled tasks (`CronCreate`, `/loop` with
  an interval, reminders) so that resuming the session restores them. When a
  hook supplies the compaction, Claude writes no such row, and a hook cannot
  return one (only messages). So after tree compaction those tasks keep firing
  in the running session but are gone once it is resumed (mv2 resumes agents,
  e.g. to wake a dormant one). Durable tasks, kept in
  `.claude/scheduled_tasks.json`, are unaffected. Fixing it needs Claude Code
  to write the row for hook-supplied compactions too.
- **Skills reserved for the user, after a mid-turn compaction.** Claude lets
  the model run a skill marked `disable-model-invocation` only when a user
  message of the current turn contains its `/name`. Claude 2.1.292 skips its
  own summary rows (`isCompactSummary`) in that check; the rows tree
  compaction installs cannot carry that flag and look like typed prompts. So
  if a compaction lands inside a turn and an earlier prompt contained
  `/name`, the model could start that skill unasked. A manual `/compact` runs
  between turns, so its rows are never checked; only Claude's automatic or
  prompt-too-long compaction runs mid-turn (automatic compaction is off by
  default here). Not confirmed live that the check covers the installed rows
  then. Fixing it needs Claude Code to let a hook mark its rows as compaction
  output.
- Claude Code's mod API is early access; this was built and tested on Claude
  Code 2.1.287.
