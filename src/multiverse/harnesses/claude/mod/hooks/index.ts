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
  const result = await $.process.run(["tmux", "-S", tmux.split(",")[0], "send-keys", "-t", pane, "Enter"],
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
  on("turn.start", async ($, e, next) => {
    turnRunning = true
    return next(e)
  })

  on("turn.complete", async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      turnRunning = false
      $.clock.after(0, () => { void flush($) })
    }
    return result
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
