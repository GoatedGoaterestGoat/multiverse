import type { EngineInterface, On, SessionCompactResult, SessionMessage } from 'claude-code'

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
// The rows an install adds are found later from the transcript: they follow
// the compaction's boundary, as many as the install returned.

type Event = { event: string; [key: string]: unknown }
type Message = { keep: string } | { role: 'user' | 'assistant'; text: string; toolUses: unknown[]; toolResults?: unknown[] }

async function config($: EngineInterface) {
  const [enabled, python, directory] = await Promise.all(
    [$.env.get("MV2_COMPACTION"), $.env.get("MV2_PYTHON"), $.env.get("MV2_AGENT_DIR")])
  return { enabled: enabled === "1", python: python ?? "", directory: directory ?? "" }
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
  $.ui.status("compacting…")
  const child = $.process.spawn({
    argv: [python, "-m", "multiverse.compaction", "install", "--agent-dir", directory], input: request })
  const returned: Message[] = []
  let buffer = "", stderr = "", result: Event | undefined, failure: string | undefined
  const handle = (event: Event) => {
    if (event.event === "progress") {
      const line = progressLine(event)
      if (line !== undefined) $.ui.status(line)
    } else if (event.event === "error") {
      const line = errorLine(event)
      $.ui.log(line)
      $.ui.toast(line, { timeoutMs: 8000 })
      if (event.retrying && event.wait) {
        $.ui.status(`compacting · ${String(event.kind)} on turn ${turn(event.turn)}, retrying in ${Math.round(number(event.wait))}s`)
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
    `about ${number(stats.tokens_before)} → ${number(stats.tokens_after)} tokens`)
  return { messages: answer }
}

async function background($: EngineInterface) {
  const { enabled, python, directory } = await config($)
  if (!enabled || !python || !directory) return
  const ended = await $.clock.now()
  const result = await $.process.run(
    [python, "-m", "multiverse.compaction", "background", "--agent-dir", directory, "--turn-ended", String(ended)],
    { timeoutMs: 15000 })
  if (result.exitCode !== 0) $.ui.log(`mv2 compaction: the background builder did not start: ${result.stderr.trim()}`)
}

export function registerCompaction(on: On) {
  on("session.compact", async ($, e, next) => {
    const { enabled } = await config($)
    if (!enabled || e.agentId !== undefined) return next(e)
    if (e.trigger === "precompute") return { skip: "mv2 compaction builds the compacted conversation itself" }
    try {
      return await install($, e.trigger, e.instructions, e.messages)
    } catch (error) {
      return failed($, `unexpected error: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      $.ui.status(undefined)
    }
  }).catch(($, e, next) => {
    if (next.called) return undefined
    return { skip: `mv2 compaction: ${next.error.kind === "timeout" ? "the hook ran out of time" : next.error.message ?? "failed"}` }
  })

  // The main conversation's turn has ended (a subagent's end is SubagentStop).
  on("classic.Stop", async ($, e, next) => {
    const result = await next(e)
    $.clock.after(0, () => {
      background($).catch((error) => $.ui.log(`mv2 compaction: background build failed: ${String(error)}`))
    })
    return result
  })
}
