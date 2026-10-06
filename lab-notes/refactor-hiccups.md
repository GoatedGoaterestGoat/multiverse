# Hiccups found during the package refactor

Found while restructuring the source (branch `refactor/packages`) and then using
the result end to end: a fresh project, a fake interactive harness, spawn,
send, kill, subverse, move, resume in the subverse, remove, trash, refresh,
doctor and the dashboard. None of these were changed by the refactor; each is
present on main.

## Using mv2

### RH-1 (medium): messages to a plain session get the wrong advice
Repro: spawn an agent with a harness that has no native delivery (any schema-1
definition), then `mv2 send NAME --text hello`.
Actual: `Not delivered to NAME: the session is still starting. Saved; resume it, then mv2 deliver NAME --retry.`
The session is running, not starting, and resuming it cannot help: a plain
session can never take a steer or follow-up. The message is saved as failed and
never shows in the agent's inbox. `--delivery inbox-only` works.
The summary maps the adapter's "No exact native session ID bound" to "still
starting". Suggest saying the session cannot receive messages directly and
saving it to the inbox instead, or suggesting `--delivery inbox-only`.

### RH-2 (low): a failed first launch keeps the name and stays "starting"
Repro: spawn with a tmux socket path over the Unix limit (about 108 bytes),
for example `MV2_TMUX_SOCKET=/a/very/long/path/tmux.sock`.
Actual: the raw error `error connecting to ... (File name too long)`, and a record
that `list` shows as dormant with phase `starting`. Spawning the same name is
refused ("resume it"); `resume` works.
Suggest checking the socket path length before launching, with a clear message.
Separately, harnesses that never report a phase show `starting` forever while
running and idle.

### RH-3 (low): human commands print JSON
`init`, `subverse`, `kill`, `move`, `remove` and `trash` print their full records
as JSON, while `spawn`, `resume`, `list`, `inspect`, `send` and `inbox` print
sentences and keep JSON behind `--json`. Suggest one line each for people, with
`--json` for scripts.

### RH-4 (low): running agents keep stale instructions silently
After an update, `mv2 refresh --apply` fixes a universe's bootstrap, but running
agents keep the bootstrap they launched with until resumed. This session's
bootstrap still told it to run `mv2 ack`, which no longer exists, and
`mv2 status`, now `report`. Nothing tells an agent its instructions changed.
Suggest a hint from removed commands (`ack`: "messages are marked read
automatically") and a note from `refresh` listing agents that need a resume.

## Test suite

### RT-1: eight modules fail on main on this machine
The Codex daemon, daemon working directory, mouse, permissions, recovery and
timing tests fail because the installed Codex writes an app-server daemon
settings file the fixtures cannot parse, and refuses helper binaries under
/tmp. The Pi timing and wake-prompt tests fail on delivery timing. These cover
the real launch and delivery paths, so on this machine those paths are less
protected than the suite suggests. Suggest pinning or detecting the harness
versions the fixtures expect and skipping with a reason when they differ.

### RT-2: a statusline test asserts on an undefined name
`tests/test_tmux_statusline.py` uses `agent_id` in a branch where it is never
defined (pyflakes flags it). The branch never runs, so its assertion checks
nothing.

### RT-3: tests are coupled to where functions live
Fixtures matched the literal path `src/multiverse/runner.py`, and tests patch
module attributes such as `agents.observe` (13 places) and `native.observe` to
fake liveness. Moving a function silently changes which patch applies: moving
wake-or-deliver out of `native` made it read the launch module's patched
liveness instead, and several tests failed without any import error. Deriving
paths from modules, and giving liveness one seam to patch, would make tests
survive refactors.

### RT-4: the dashboard has no screen snapshot tests
The refactor was checked by rendering 29 dashboard screens (demo backend,
compaction screens, a portal with panels and an artifact preview) plus four
screens of a real universe with live agents, before and after, and comparing
the SVGs. That check lives outside the repo. Committing it as snapshot tests
would protect the UI through the configurable-view work. The artifact preview
needs a fixed path: a temporary path's length moves the text around.

## Refactor decisions to revisit

- Waking a stopped agent launches it, so `runtime.native` imports
  `runtime.lifecycle` inside `send_or_wake`. It is the one import cycle left.
- `runner.py`, `statusline.py` and `recovery.py` at the package top are stubs for
  agents launched before the layout changed. Remove them once those agents have
  been resumed or removed.
- Harness discovery stays in `ui/backend.py`: it validates names with universe
  rules, and `harnesses` must not import `universe`. The compaction tool still
  hosts the functions behind the dashboard's compaction screens, matching the
  rule that the dashboard calls tools.
