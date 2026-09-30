import type { EngineInterface, Register } from 'claude-code'

// Multiverse sends each message over this session's peer inbox at `next`
// priority, its text between <<MV2>> and <</MV2>>. The model reads the text
// alone, without the tags or Claude's peer wrapper:
// - Mid-turn it merges at the next tool result as a queued command, and the
//   attachment hook returns the text.
// - Arriving while idle, session.receive takes it before it is queued.
// - Queued mid-turn with no tool result after it, it would start its own turn
//   as a wrapped peer prompt, so prompt.submit drops it.
// Taken messages are typed in: the draft is saved, the prompt box filled,
// Enter sent to this tmux pane, and the draft restored once it is submitted.
// If that fails, the text is submitted as this plugin's prompt instead.
//
// It also reports this session's activity to Multiverse: working while a
// main-loop turn runs, waiting while a permission prompt, a question or an MCP
// elicitation waits on the person, idle otherwise.

const OPEN = "<<MV2>>"
const CLOSE = "<</MV2>>"

// The text between the tags; undefined when the text carries none.
function tagged(text: string): string | undefined {
  const start = text.indexOf(OPEN)
  const end = text.lastIndexOf(CLOSE)
  return start < 0 || end < start ? undefined : text.slice(start + OPEN.length, end)
}

// Main-loop turn state: turn.start fires for the main loop only, and a
// subagent's turn.complete carries its agentId.
let turnRunning = false

// What waits on the person, by key (`question:<id>`, `permission:<id>`,
// `elicitation:<id>`), with the tool it waits for. Any entry makes the session
// `waiting`.
const waiting = new Map<string, string>()

// Tool calls the permission check answered `ask`, by tool_use_id, until a
// prompt opens for one of them (the mode may settle the ask instead).
const asked = new Map<string, string>()

// Tools whose call is itself a question to the person.
const QUESTIONS = new Set(["AskUserQuestion", "ExitPlanMode"])

let prompts = 0
let reported: string | undefined
let registered = false
let reporting = false

function activity(): string {
  return waiting.size > 0 ? "waiting" : turnRunning ? "working" : "idle"
}

// Report the current activity once it changes, one report at a time, so a
// burst of changes ends with the latest state. The first report also tells
// Multiverse this launch's mod is loaded.
async function report($: EngineInterface) {
  if (reporting) return
  reporting = true
  try {
    const cli = await $.env.get("MV2_CLI")
    if (!cli) return
    while (!registered || reported !== activity()) {
      const value = activity()
      const argv = [cli, "runtime-update", "--activity", value]
      if (!registered) argv.push("--bridge", "claude-mod-v1")
      const result = await $.process.run(argv, { timeoutMs: 10000 })
      if (result.exitCode !== 0) {
        $.ui.log(`multiverse: activity report failed: ${result.stderr.trim()}`)
        return
      }
      reported = value
      registered = true
    }
  } finally {
    reporting = false
  }
}

// A call has finished or run: nothing of it waits on the person any more. A
// prompt that matched no call ends with the next finished call of its tool.
function settle($: EngineInterface, id: string | undefined, tool?: string) {
  if (id === undefined) return
  asked.delete(id)
  const removed = [waiting.delete(`question:${id}`), waiting.delete(`permission:${id}`)]
  const unmatched = [...waiting].find(([key, name]) => key.startsWith("permission:unmatched-") && name === tool)
  if (unmatched !== undefined) removed.push(waiting.delete(unmatched[0]))
  if (removed.some(Boolean)) void report($)
}

// Messages waiting to be typed in, oldest first.
const pending: string[] = []

// The prompt typed in and not yet seen submitted, with the draft it replaced.
let inFlight: { text: string; draft: string; attempt: number } | undefined

const ATTEMPTS = 3
const CONFIRM_MS = 3000

async function enter($: EngineInterface): Promise<string | undefined> {
  const pane = await $.env.get("TMUX_PANE")
  const tmux = await $.env.get("TMUX")
  if (!pane || !tmux) return "this session is not in a tmux pane"
  const [socket = tmux] = tmux.split(",")
  const result = await $.process.run(["tmux", "-S", socket, "send-keys", "-t", pane, "Enter"],
    { timeoutMs: 2000 })
  return result.exitCode === 0 ? undefined : `tmux send-keys failed: ${result.stderr.trim()}`
}

// Deliver as this plugin's prompt when typing in cannot.
async function submitInstead($: EngineInterface, text: string, reason: string) {
  $.ui.log(`multiverse: ${reason}; submitting the message as this plugin's prompt`)
  await $.prompt.submit({ text })
}

// Type every waiting message in as one prompt, if the session is idle.
async function flush($: EngineInterface, attempt = 1) {
  if (turnRunning || inFlight !== undefined || pending.length === 0) return
  const text = pending.splice(0, pending.length).join("\n\n")
  const draft = (await $.prompt.read()).text
  const filled = await $.prompt.fill({ text, mode: "replace" })
  const box = (await $.prompt.read()).text
  if (!filled.isFilled || box !== text || turnRunning) {
    // A dialog holds the keys or a turn started: wait for the prompt box.
    if (filled.isFilled && box === text) await $.prompt.fill({ text: draft, mode: "replace" })
    pending.unshift(text)
    $.clock.after(250, () => { void flush($, attempt) })
    return
  }
  inFlight = { text, draft, attempt }
  const failure = await enter($)
  if (failure !== undefined) {
    inFlight = undefined
    await $.prompt.fill({ text: draft, mode: "replace" })
    await submitInstead($, text, failure)
    return
  }
  $.clock.after(CONFIRM_MS, () => { void confirm($, text) })
}

// The prompt was not seen submitted: put the draft back and try again.
async function confirm($: EngineInterface, text: string) {
  if (inFlight === undefined || inFlight.text !== text) return
  const { draft, attempt } = inFlight
  inFlight = undefined
  if ((await $.prompt.read()).text === text) await $.prompt.fill({ text: draft, mode: "replace" })
  if (attempt >= ATTEMPTS) {
    await submitInstead($, text, `the typed-in message was not submitted after ${attempt} attempts`)
    return
  }
  pending.unshift(text)
  $.clock.after(500, () => { void flush($, attempt + 1) })
}

function take($: EngineInterface, text: string) {
  pending.push(text)
  // Leave the calling hook first.
  $.clock.after(0, () => { void flush($) })
}

export const register: Register = (on) => {
  on("session.start", async ($, e, next) => {
    const result = await next(e)
    void report($)
    return result
  })

  on("turn.start", async ($, e, next) => {
    turnRunning = true
    void report($)
    return next(e)
  })

  on("turn.complete", async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      turnRunning = false
      // An ended turn has closed its prompts and questions.
      waiting.clear()
      asked.clear()
      void report($)
      $.clock.after(0, () => { void flush($) })
    }
    return result
  })

  // A question tool waits on the person for as long as its call runs; any
  // call that finishes has left its permission prompt.
  on("tool.call", async ($, e, next) => {
    const id = e.tool_use_id
    if (id !== undefined && QUESTIONS.has(e.tool)) {
      waiting.set(`question:${id}`, e.tool)
      void report($)
    }
    try {
      return await next(e)
    } finally {
      settle($, id, e.tool)
    }
  })

  // `ask` may open a permission prompt for this call, or the mode settles it.
  on("tool.check", async ($, e, next) => {
    const verdict = await next(e)
    if (verdict.decision === "ask" && e.tool_use_id !== undefined) asked.set(e.tool_use_id, e.tool)
    return verdict
  })

  // The prompt is open: it belongs to the oldest asked call of that tool.
  on("classic.PermissionRequest", async ($, e, next) => {
    const id = [...asked].find(([, tool]) => tool === e.tool_name)?.[0]
    if (id !== undefined) asked.delete(id)
    prompts += 1
    waiting.set(`permission:${id ?? `unmatched-${prompts}`}`, e.tool_name)
    void report($)
    return next(e)
  })

  on("classic.PermissionDenied", async ($, e, next) => {
    settle($, e.tool_use_id)
    return next(e)
  })

  // An approved Bash call draws its progress row once it runs.
  on("ui.render", { component: "ToolProgress" }, async ($, e, next) => {
    if (waiting.has(`permission:${e.props.tool_use_id}`)) settle($, e.props.tool_use_id)
    return next(e)
  })

  on("classic.Elicitation", async ($, e, next) => {
    waiting.set(`elicitation:${e.elicitation_id ?? e.mcp_server_name}`, e.mcp_server_name)
    void report($)
    return next(e)
  })

  on("classic.ElicitationResult", async ($, e, next) => {
    if (waiting.delete(`elicitation:${e.elicitation_id ?? e.mcp_server_name}`)) void report($)
    return next(e)
  })

  // Arriving while idle: take the message before Claude queues it.
  on("session.receive", { origin: { kind: "peer" } }, async ($, e, next) => {
    const text = tagged(e.text)
    if (text === undefined || turnRunning) return next(e)
    take($, text)
    return { consumed: "Multiverse message typed in by the multiverse plugin" }
  })

  // Mid-turn: the queued command carries Claude's rendered peer wrapper.
  on("prompt.attachment", { type: "queued_command" }, async ($, e, next) => {
    const text = tagged(e.text)
    return text === undefined ? next(e) : { text }
  })

  // Queued mid-turn with no tool result after it: it would start its own turn
  // as a wrapped peer prompt. Mid-turn deliveries (turnId set) pass.
  on("prompt.submit", { origin: { kind: "peer" } }, async ($, e, next) => {
    const text = tagged(e.text)
    if (text === undefined || e.turnId !== undefined) return next(e)
    take($, text)
    return { drop: "Multiverse message typed in by the multiverse plugin" }
  })

  // The typed-in prompt went through: restore the person's draft.
  on("prompt.submit", { origin: { kind: "composer" } }, async ($, e, next) => {
    if (inFlight === undefined || e.text !== inFlight.text) return next(e)
    const { draft } = inFlight
    inFlight = undefined
    const result = await next(e)
    if (draft !== "") await $.prompt.fill({ text: draft, mode: "replace" })
    return result
  })
}
