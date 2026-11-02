# Harness commands

How mv2 runs a harness's own command in a running agent, as if the person
typed it (`/compact`, `/model sonnet`, or any command the harness lists), and
what an adapter provides for it. mv2 decides which commands may run, when,
and what it records; each harness implements it as it can. It is implemented
for Claude. Codex and Pi report it unavailable (see
[Codex and Pi](#codex-and-pi)).

## Surface

```sh
mv2 slash AGENT compact             # run /compact, wait until it has run, print its output
mv2 slash AGENT model sonnet        # /model sonnet
mv2 slash AGENT --list              # what the agent's harness offers, and why any is refused
mv2 slash AGENT --result ID         # the outcome of a run that outlived the wait
```

The command is named without its slash; the rest of the line is its
arguments as the person would type them. A leading slash is dropped, so `mv2
slash AGENT /compact` is the same run. `--timeout SECONDS` (600 by default),
`--no-wait` (prints the run ID at once) and `--json` apply.

Plugins get the same through the API: listing the commands, running one and
reading a run's result ([plugins](../develop/plugins.md)).

## Effects and access

| Operation | Class | Why |
| --- | --- | --- |
| List the commands | observe | Reads the harness's list |
| Run a command | act | Changes the session: it may clear, compact, switch models or start a turn |
| Read a run's result | observe | Reads what the run recorded |

A plugin's run needs act access and `agents.control`; listing needs
`agents.read`. An agent's CLI call is an act, like `mv2 kill`.

## Listing

Each command is listed with its name (without the slash), description,
source (`builtin`, `plugin`, `user`, `mcp`; an open set), the plugin that
added it where the harness says, whether it needs arguments, and why mv2
refuses it, if it does. The list is the live one, read from the running
session, so it includes the person's own plugins, skills and MCP prompts. A
stopped agent, or a launch without mv2's bridge, can't be listed: the
listing is unavailable with the reason.

## Refusals

Refused before anything reaches the harness, each with its reason and the
mv2 command to use instead. Nothing is recorded as run.

1. **mv2 manages it:** `exit` and `quit` (use `mv2 kill`), `resume` (`mv2
   resume`), `login` and `logout`; Claude's `restart`, which relaunches
   Claude without mv2's inbox (use `mv2 kill`, then `mv2 resume`); `plugin`
   with arguments that touch mv2's own Claude plugin, which would turn off
   mv2's messaging; mv2's own `multiverse`, whose features are mv2 commands
   already; and Claude's `rename`, since mv2 names the session at each launch
   and a resume would undo it. `clear` is allowed: session lineage follows
   it.
2. **A screen when bare:** a command whose bare form only opens a picker
   (Claude's `/model`, `/config`, `/agents`, `/mcp`, `/permissions`,
   `/plugin`, `/theme`, ...) runs only with arguments: `model sonnet` runs,
   bare `model` doesn't. A harness can't say this when the command runs (a
   picker would wait in the agent's terminal for a person), so each adapter
   keeps a table of its built-in commands, kept current by its version check.
   A built-in the table doesn't know is refused until the check adds it;
   plugin, user and MCP commands print or prompt, so they run.
3. **Nothing to return, or not for mv2 to run:** a Claude command whose
   output Claude writes only for the model (`/context`), and one Claude runs
   only when the person asks in the session (`/recap`), are refused; the
   Claude table marks them.
4. **Unknown:** a name the live list doesn't have. Only a name exactly as
   the list gives it runs: Claude's `$.command.run` resolves aliases itself
   (`quit` ends the session though only `exit` is listed), so an unlisted
   alias must never reach it.
5. **Not available:** an agent that isn't running, a launch without mv2's
   bridge, a harness that doesn't implement this.
6. **A focused agent:** an agent's command to a focused agent is refused,
   since a command can start a turn and bury the question it is focused on.
   A person's command runs.
7. **Changes to the person's Claude settings:** some Claude built-ins also
   save their choice as the person's default for every new session
   (`/model X` and `/effort Y` write `~/.claude/settings.json`, as when typed;
   so do theme, output style, permission and plugin changes). Such a
   command runs only when a person runs it, and its result says it was also
   saved as their Claude default; from an agent or a plugin it is refused,
   pointing to `mv2 resume AGENT --model X --effort Y`, which changes only
   that agent (launch flags apply to that session alone). The Claude table classifies every built-in; one it doesn't
   classify is refused until the version check does.
8. **Effects mv2 can't follow yet:** a command that would change something
   mv2 records but doesn't track (the working directory, permissions, a
   native session change lineage can't follow) is refused until mv2 handles
   it. For Claude that is none beyond model and effort.

## Waiting and the result

- The command joins the harness's own queue and runs when the agent is idle.
  mv2 never runs one mid-turn and never interrupts a turn for it.
- Commands and follow-ups share that queue in the order they reach it (for
  Claude, `$.command.run` and `$.prompt.submit` use one lane, first in,
  first out). mv2 doesn't reorder them or hold a command behind its own
  undelivered messages; steers are mid-turn and unaffected. A follow-up
  queued after a `clear` lands in the new session, as when a person types
  both.
- `mv2 slash` waits up to its timeout. A queued command can't be withdrawn,
  so when the wait ends first, mv2 says it is still queued and may still run,
  never that it failed, and never sends it again; `--result ID` reads the
  outcome once it is there. A run whose agent stops before it ran is `lost`,
  reported once.
- A result nobody asked for still counts: mv2 settles finished runs when it
  next lists the agent or resumes it, from a small index of unfinished runs
  (`slash/pending.json`, never a scan of every run), before a resume chooses
  the next model and effort; an explicit `--model` or `--effort` on that
  resume still wins. A resume refuses while a `model` or `effort` run of the
  launch it replaces has no final outcome, unless it gives that setting
  itself; the refusal names the run and both ways out. A run settled as lost
  never blocks.
- The result: the run's ID, the command and its arguments, its state (`ran`,
  `queued`, `refused`, `failed`, `lost`; an open set), what the command
  printed (at most 64 KiB, none when it printed nothing), whether it
  expanded into a prompt (a skill, a plugin or MCP prompt: its answer is the
  agent's turn, not output), what mv2 recorded because of it, and why it was
  refused, failed or lost.
- The output is kept in the run's own record in the agent's folder, so
  `--result` can show it later, and never journalled: events and logs record
  the agent, the command's name, the state and who ran it (the person, a
  verified agent, or a plugin by name), never its arguments or output. The
  caller is recorded when the run is submitted, and reading the result later
  doesn't change it. A harness's hidden notes for the model are never returned.

## What mv2 records

The run's result is written only after mv2's records follow the command, so
`mv2 list` right after `mv2 slash` is already right.

- **`clear`** starts a new native session: [session lineage](session-lineage.md)
  records it as for a typed `/clear`, and the result names the new session.
- **`compact`**: nothing new. The transcript gets its boundary, and usage
  stays the last real reading until the next response. With compaction on,
  Claude's `session.compact` hook runs the plugin's install, as for a typed
  `/compact`. The command row and the boundary are stored as for a typed one
  (the boundary's logical parent is the row before the `/compact` row; only
  the row's origin names the plugin), which the
  [history walk](transcript-protocols.md#history-and-context) and compaction
  rely on.
- **`model`, `effort`**: the adapter reports the session's model after the
  run (Claude: `$.session.model()`), and mv2 records it, so `mv2 list`,
  inspect and the next resume use it; without that, the next resume would
  pass the old model and undo the change. mv2 records only what the adapter
  confirms; it never reads an effort from the command's text. After `effort
  LEVEL`, Claude's adapter reads back the effort Claude saved for the
  session's model (`modelSettings.<model>.effortLevel`) and reports it only
  when the model takes that level; after `model X` it reports the model only,
  and the recorded effort stays. `mv2 spawn` and `mv2 resume` take `--effort` beside
  `--model`, recorded with the agent and passed at every launch (Claude
  `--effort`; Codex as its adapter maps it, or a clear refusal), so one
  agent's effort changes without touching the person's defaults. `--effort default`
  clears that recorded override. A model change without `--effort` drops an
  incompatible recorded effort and reports it; an explicitly requested
  incompatible effort still refuses.
- Anything else is the harness's own state.
- In the transcript the command row is labelled `command` with source
  `plugin` and forms a command turn. Message crediting never takes it for a
  delivered message.

## The adapter's part

Optional; without it the capability is unavailable.

```python
class SlashProvider(Protocol):
    def slash_commands(self, session, executor) -> NativeResult: ...   # observe: the live list
    def run_slash(self, session, request, executor) -> NativeResult: ... # act: submits once
    def slash_result(self, session, request, executor) -> NativeResult: ...  # observe: reads the outcome, never sends
```

`run_slash` submits the command once and returns it queued, unknown (sent,
outcome not known yet) or run; `slash_result` only reads the outcome, so
waiting and `--result` never send anything again. A run's value carries the
output, whether it prompted, the session ID it ran in (to check a `clear`
landed) and the model and effort after it. Core owns the refusals, the run
record in the agent's folder, the wait and timeout, recording model and
effort, and the CLI and API.

**Claude**, as `mv2 ask` does: core writes one inbox frame tagged `<<MV2
command id=…>>` with the command and its arguments; mv2's mod consumes it in
`session.receive`, so the model never sees it, calls `$.command.run` after
the hook returns (a hook must not wait on its own run) and writes the outcome
to `claude-commands/<id>.json`. Listing works the same way with
`$.command.list()`, which gives name, description, source and plugin, with
no "opens a screen" flag: hence the adapter's table. The mod checks the name
against the refused list again. Found in the live check on 2.1.316:

- Claude lists MCP prompts under a display name with a space
  (`server:Prompt (MCP)`), which can't be addressed; the adapter leaves such
  names out of the list, and one bad entry never fails the whole list.
- A command that printed nothing answers with an empty text; that is no
  output.
- `/compact` and `/effort` print only on screen, so their run answers with no
  text. The mod returns the line Claude stores just after the command's row;
  a stored error line fails the run. It does so only for a run whose own row
  reached it under the multiverse plugin's name, when nobody else ran the
  same command meanwhile (Claude files the person's typed command as the
  engine's): otherwise the line could be someone else's, and the run reports
  no output. A notice stored as a system row, such as an empty session's
  `Error: No messages to compact`, never reaches the mod.
- A prompt command's turn can start just after its run returns, so after a
  silent run the mod waits up to 2 s for a turn named after the command
  before calling it "ran with no output": a command that prints nothing
  returns about 2 s later.
- A `/compact` run this way is stored as a typed one (the command row's
  origin names the plugin); a follow-up sent after a `clear` lands in the new
  session; no command run is ever credited as a delivered message.

## Codex and Pi

**Pi** reports it unavailable for now.

**Codex** reports it unavailable (checked on 0.166.0's source):

- There is no app-server call that lists or runs slash commands; the menu is
  the TUI's own, and `/compact` sent as a turn is ordinary model input.
- Some commands have an equivalent call on a thread (`thread/compact/start`,
  the experimental `thread/settings/update`, `thread/name/set`, goals,
  background terminals), returning `{}` or structured data, never the
  command's text.
- None waits for idle: compaction and review abort a running turn, and
  settings change during one. Polling for idle and then calling races typed
  input, Codex's own queue and other clients.
- `/clear` starts a new thread, which mv2's Codex bridge refuses.

A later, explicitly mapped Codex set (model and effort first) would need an
event-aware exchange on the agent's thread, a status line mv2 writes, and the
model and effort read back from `thread/read` and recorded. Codex compaction
stays out until Codex offers a way that doesn't interrupt a turn. mv2 never
falls back to typing into the TUI or reading its screen.
