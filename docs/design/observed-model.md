# Observed model and effort

A harness can report the model and effort its running session used for its latest turn. mv2 knows otherwise only
what it launched an agent with, so a launch without `--model`, or a `/model` switch inside the session, would be
invisible. The Model cell (`mv2 list --columns model` and the dashboard) and plugins read these observations.

## The cell

The user's rules (2026-11-12), in order:

1. A running agent: the model its session reports.
2. Otherwise (stopped, or no reading from this launch yet): the agent's recorded model.
3. No recorded model: the harness's default, which is what a resume launches with.
4. None of these: `-`.

Effort, in parentheses, follows the same rules: the session's effort, else the recorded effort, else none. It never
falls back to a default, so core reports no default effort.

## What a reading means

`get_usage` and the `Usage` record carry `model`, `effort` and `observed_at`:

- **The latest turn, as recorded.** They are what the harness recorded for the main conversation's latest turn,
  spelled as it recorded it, never a requested setting or a table default. A harness that records no effort leaves
  it empty. In a turn the harness accepts, model and effort can each be missing on their own; a harness whose turn
  record requires a model treats one without it as damaged (see Codex below). After `/model`, the reading changes
  with the next turn the harness records, so docs say "the model of its latest turn", not "its current setting".
- **The main conversation only.** Subagent turns and side threads never count.
- **This launch only.** `observed_at` is when that turn was recorded. Core keeps the three fields only when
  `observed_at` and the launch's start are both readable, the turn came at or after the launch started, and the
  reading belongs to the agent's current native session; otherwise it clears all three. A turn from before a resume
  with another `--model` doesn't describe the running session, so until a reading from this launch arrives the cell
  shows the recorded model.
- **Measured, not adjusted.** A plugin's usage adjustment can't change them or the reading's native session ID;
  core keeps the measured values, so an adjustment that drops or rewrites that ID can't erase an observation.
- **No event-loop reads.** Usage reads the fields through the existing transcript readers, off the dashboard's event
  loop.

A listing row keeps its recorded `model` and `effort`, and adds `default_model` and display names for the default
and the observed model, from the same tables and `model_names` as recorded models. An unknown spelling has no
display name and shows raw. A damaged or unreadable reading leaves the cell on rules 2 to 4, never an error.

Plugins get the same fields from `agent.usage()` when its state is `ok` (API 1.19). The cell uses them only while
the agent runs; after it stops, `agent.usage()` may still name its last turn, while the cell shows the recorded
model.

## Per harness

- **Claude.** Claude stores both on every reply. The reading takes the latest main-thread assistant row's
  `message.model`, and the effort stored on it: `perTurnEffort` when the row has that key (null meaning none), else
  `effort`, as Claude's own reader does. A model that takes no effort stores none, so the cell shows the recorded
  effort. Claude's `<synthetic>` rows, subagent rows and compaction's kept copies are skipped; `observed_at` is the
  row's own time.
- **Codex.** The primary thread's latest `turn_context` model and effort in its rollout, with that line's time, on
  checked versions, read through the existing rollout reader. Codex's live settings notifications describe settings,
  not a turn that ran, so they don't count. Codex's turn context always records a model, so a latest context without
  a valid one is damaged: all three fields stay empty, and nothing is taken from an earlier turn. The fields are
  added only to a usage result that is already `ok`, so whether Codex usage is available doesn't change; until Codex
  reports this launch's first token count, the cell shows the recorded model.
- **Pi.** Not reported.
