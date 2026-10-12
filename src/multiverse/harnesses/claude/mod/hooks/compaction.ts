import type { EngineInterface, On, SessionCompactResult, SessionMessage } from 'claude-code'
import { currentLine, setProgress } from './statusline'

// Transition copy: the compaction plugin's own Claude directory now holds these
// hooks (plugins/builtin/compaction/claude). A launch that loads it sets
// MV2_COMPACTION_PLUGIN=1 and these stand aside; they answer only for launches
// from before it existed. Remove once mv2 doctor shows no such launch is left.
//
// Multiverse tree compaction for this session, when its universe enables it
// for the Claude harness (the launch sets MV2_COMPACTION=1).
//
// - session.compact: the session's /compact runs
//   `python -m multiverse.compaction install`, which builds any summary trees
//   still missing, expands them by the agent's scheduler and streams back the
//   compacted conversation. Progress, retries and errors show in the status
//   line, as transcript notices and as toasts while it runs. The hook answers
//   with those messages, or skips with the reason: it never hands the
//   compaction to Claude's own summarizer (the launch's PreCompact hook refuses
//   that too). A subagent's own compaction is left to Claude.
// - classic.Stop: the main conversation's turn has ended. The background tree
//   builder is told when (its turn-end marker), so it builds that turn's tree
//   at once. It returns at once when a builder is running (which takes the new
//   marker up) or background building is off.
//
// - classic.SessionStart: every turn so far has ended, so the background builder
//   is told the session's start as a turn end: it builds what a lost turn end
//   left (a reload, a turn that ended in an error, a restart), when background
//   building is on.
//
// The status line's compaction item is a file: `python -m multiverse.compaction`
// writes status-items/compaction.txt whenever it learns where compaction
// stands (`status`, which `background` also runs, and each install), and the
// status line reads it like any plugin's item. These hooks have it learn that
// when the session starts, when a turn ends, every 15 s while a builder runs
// and after each /compact. The progress of a running /compact shares the
// plugin's one status slot (statusline.ts).
//
// The rows an install adds are found later from the transcript: they follow
// the compaction's boundary, as many as the install returned.

type Event = { event: string; [key: string]: unknown }
// What `python -m multiverse.compaction status` reports.
export type Status = { enabled?: boolean; trees?: number; turns?: number; missing?: number; building?: boolean; failed?: number }

// What the hooks last learned: whether a builder runs, so they keep looking while it does.
const known: { status?: Status; following: boolean } = { following: false }
const FOLLOW_MS = 15000, FOLLOW_ROUNDS = 40

// The last status a process printed, if it printed one.
export function parseStatus(stdout: string): Status | undefined {
  for (const line of stdout.split("\n").reverse()) {
    try {
      const event = JSON.parse(line) as Event
      if (event.event === "status") return event as Status
    } catch {
      // not a status line
    }
  }
  return undefined
}

function readStatus(stdout: string) {
  known.status = parseStatus(stdout) ?? known.status
}

// Every status line write: a running /compact's progress wins the plugin's one slot.
function showProgress($: EngineInterface, line: string | undefined) {
  setProgress(line)
  $.ui.status(currentLine())
}
type Message = { keep: string } | { role: 'user' | 'assistant'; text: string; toolUses: unknown[]; toolResults?: unknown[] }

async function config($: EngineInterface) {
  const [enabled, plugin, python, directory] = await Promise.all(
    [$.env.get("MV2_COMPACTION"), $.env.get("MV2_COMPACTION_PLUGIN"), $.env.get("MV2_PYTHON"), $.env.get("MV2_AGENT_DIR")])
  // A launch that loads the compaction plugin's own Claude directory has it answer instead.
  return { enabled: enabled === "1" && plugin !== "1", python: python ?? "", directory: directory ?? "" }
}

async function refresh($: EngineInterface) {
  const { enabled, python, directory } = await config($)
  if (!enabled || !python || !directory) return
  const result = await $.process.run([python, "-m", "multiverse.compaction", "status", "--agent-dir", directory],
    { timeoutMs: 15000 })
  if (result.exitCode === 0) readStatus(result.stdout)
}

// While a builder runs, look again every 15 s until it is done (at most 10 minutes per start).
function follow($: EngineInterface) {
  if (known.following || !known.status?.building) return
  known.following = true
  let rounds = 0
  const look = () => {
    refresh($).catch(() => undefined).finally(() => {
      rounds += 1
      if (known.status?.building && rounds < FOLLOW_ROUNDS) $.clock.after(FOLLOW_MS, look)
      else known.following = false
    })
  }
  $.clock.after(FOLLOW_MS, look)
}

function bar(done: number, total: number): string {
  const width = 8
  const filled = total > 0 ? Math.min(width, Math.round((done / total) * width)) : 0
  return "▰".repeat(filled) + "▱".repeat(width - filled)
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0
}

function turn(value: unknown): string {
  return typeof value === "string" ? value.slice(0, 8) : "?"
}

// The status line for one progress event.
export function progressLine(event: Event): string | undefined {
  const done = number(event.turns_done), total = number(event.turns)
  if (event.phase === "building") {
    return `compacting ${bar(done, total)} ${done}/${total} turns · turn ${turn(event.turn)}: ` +
      `${number(event.merges)} merges, ${number(event.merges_left)} to go`
  }
  if (event.phase === "waiting") return `compacting ${bar(done, total)} · waiting for the builder of turn ${turn(event.turn)}`
  if (event.phase === "built") return `compacting ${bar(done, total)} ${done}/${total} turns`
  return undefined
}

// The notice for one error event: every error is shown, retried or not.
export function errorLine(event: Event): string {
  const where = event.turn ? `turn ${turn(event.turn)}` : "compaction"
  const retry = event.retrying
    ? ` (retry ${number(event.attempt)}/${number(event.attempts)}${event.wait ? ` in ${Math.round(number(event.wait))}s` : ""})`
    : ""
  return `mv2 compaction: ${where}: ${String(event.kind)}: ${String(event.message)}${retry}`
}

// " (this time: proportional, budget 60000)" when the command's arguments set the scheduler.
// Image mode is left out: Claude always drops prompt images.
export function oneOff(stats: Record<string, unknown>): string {
  if (stats.scheduler_from !== "arguments" || typeof stats.scheduler !== "object" || stats.scheduler === null) return ""
  const settings = Object.entries(stats.scheduler as Record<string, unknown>)
    .filter(([key]) => key !== "image_mode")
    .map(([key, value]) => key === "scheduler" ? String(value) : `${key} ${String(value)}`)
  return ` (this time: ${settings.join(", ")})`
}

function failed($: EngineInterface, reason: string): SessionCompactResult {
  const text = reason.startsWith("mv2 compaction") ? reason : `mv2 compaction: ${reason}`
  $.ui.log(text)
  $.ui.toast(text, { timeoutMs: 10000 })
  return { skip: text }
}

async function install($: EngineInterface, trigger: string, instructions: string | undefined,
                       messages: readonly SessionMessage[]): Promise<SessionCompactResult> {
  const { python, directory } = await config($)
  if (!python || !directory) return failed($, "MV2_PYTHON or MV2_AGENT_DIR is missing from this session")
  const offered = new Map<string, SessionMessage>()
  for (const message of messages) if (message.handle !== undefined) offered.set(message.handle, message)
  const request = JSON.stringify({
    trigger, instructions: instructions ?? null,
    offered: [...offered.values()].map((message) => ({ handle: message.handle, role: message.role })),
  })
  showProgress($, "compacting…")
  const child = $.process.spawn({
    argv: [python, "-m", "multiverse.compaction", "install", "--agent-dir", directory], input: request })
  const returned: Message[] = []
  let buffer = "", stderr = "", result: Event | undefined, failure: string | undefined
  const handle = (event: Event) => {
    if (event.event === "progress") {
      const line = progressLine(event)
      if (line !== undefined) showProgress($, line)
    } else if (event.event === "error") {
      const line = errorLine(event)
      $.ui.log(line)
      $.ui.toast(line, { timeoutMs: 8000 })
      if (event.retrying && event.wait) {
        showProgress($, `compacting · ${String(event.kind)} on turn ${turn(event.turn)}, retrying in ${Math.round(number(event.wait))}s`)
      }
    } else if (event.event === "log") {
      $.ui.log(`mv2 compaction: ${String(event.message)}`)
    } else if (event.event === "message") {
      returned.push(event.message as Message)
    } else if (event.event === "result") {
      result = event
    } else if (event.event === "failed") {
      failure = String(event.message)
    }
  }
  for await (const { stream, text } of child) {
    if (stream === "stderr") {
      stderr = (stderr + text).slice(-2000)
      continue
    }
    buffer += text
    let newline = buffer.indexOf("\n")
    while (newline >= 0) {
      const line = buffer.slice(0, newline).trim()
      buffer = buffer.slice(newline + 1)
      newline = buffer.indexOf("\n")
      if (!line) continue
      let event: Event | undefined
      try {
        event = JSON.parse(line) as Event
      } catch {
        stderr = (stderr + line + "\n").slice(-2000)
      }
      if (event !== undefined) handle(event)
    }
  }
  const exit = await child.result
  if (failure !== undefined) return failed($, failure)
  if (result === undefined || exit.code !== 0) {
    const how = exit.signal ?? `exit ${exit.code}`
    const said = stderr.trim().split("\n").slice(-3).join(" ")
    return failed($, `the compaction process stopped (${how})${said ? `: ${said}` : ""}`)
  }
  const count = number(result.count)
  if (returned.length !== count || count === 0) {
    return failed($, `expected ${count} messages from the compaction, received ${returned.length}`)
  }
  const answer: SessionMessage[] = []
  for (const message of returned) {
    if ("keep" in message) {
      const kept = offered.get(message.keep)
      if (kept === undefined) return failed($, `the compaction kept a row Claude did not offer (${message.keep})`)
      answer.push(kept)
    } else {
      answer.push(message as unknown as SessionMessage)
    }
  }
  const stats = (result.stats ?? {}) as Record<string, unknown>
  $.ui.log(`mv2 compaction: ${number(stats.turns)} turns, ${number(stats.summarized)} summarized, ` +
    `about ${number(stats.tokens_before)} → ${number(stats.tokens_after)} tokens${oneOff(stats)}`)
  return { messages: answer }
}

async function background($: EngineInterface) {
  const { enabled, python, directory } = await config($)
  if (!enabled || !python || !directory) return
  const ended = await $.clock.now()
  const result = await $.process.run(
    [python, "-m", "multiverse.compaction", "background", "--agent-dir", directory, "--turn-ended", String(ended)],
    { timeoutMs: 15000 })
  if (result.exitCode !== 0) {
    $.ui.log(`mv2 compaction: the background builder did not start: ${result.stderr.trim()}`)
    return
  }
  readStatus(result.stdout)
  follow($)
}

export function registerCompaction(on: On) {
  on("session.compact", async ($, e, next) => {
    const { enabled } = await config($)
    if (!enabled || e.agentId !== undefined) return next(e)
    if (e.trigger === "precompute") return { skip: "mv2 compaction builds the compacted conversation itself" }
    let answer: SessionCompactResult
    try {
      answer = await install($, e.trigger, e.instructions, e.messages)
    } catch (error) {
      answer = failed($, `unexpected error: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      showProgress($, undefined)
    }
    $.clock.after(0, () => { refresh($).catch(() => undefined) })
    return answer
  }).catch(($, e, next) => {
    if (next.called) return undefined
    return { skip: `mv2 compaction: ${next.error.kind === "timeout" ? "the hook ran out of time" : next.error.message ?? "failed"}` }
  })

  // A new or resumed session: every turn so far has ended, so catch up on any
  // whose end was lost, as at a turn end.
  on("classic.SessionStart", async ($, e, next) => {
    const result = await next(e)
    known.status = undefined
    $.clock.after(0, () => {
      background($).catch((error) => $.ui.log(`mv2 compaction: background build failed: ${String(error)}`))
    })
    return result
  })

  // A hot reload (the mod's files changed) starts this module's variables over
  // and cancels its timers: learn the status again. It also fires once at the
  // session's start, beside classic.SessionStart. (index.ts holds the unmatched
  // session.start hook; this one is matched.)
  on("session.start", { isInteractive: true }, async ($, e, next) => {
    const result = await next(e)
    $.clock.after(0, () => {
      refresh($).then(() => follow($)).catch(() => undefined)
    })
    return result
  })

  // The main conversation's turn has ended (a subagent's end is SubagentStop).
  // Awaited, not left on a timer: a reload at the turn's end would cancel it.
  on("classic.Stop", async ($, e, next) => {
    const result = await next(e)
    try {
      await background($)
    } catch (error) {
      $.ui.log(`mv2 compaction: background build failed: ${String(error)}`)
    }
    return result
  })
}
