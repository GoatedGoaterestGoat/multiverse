import type { EngineInterface, On } from 'claude-code'

// Reports the session's context fill, cost and rate-limit windows
// ($.session.usage(), free without arguments) to Multiverse, which stores them
// in this launch's runtime.json. Read when a session starts and when a
// main-loop turn completes; a subagent's turn.complete carries its agentId.
// Reported only when a figure changed, one report at a time.

let reported: string | undefined
let reporting = false
let again = false

async function snapshot($: EngineInterface): Promise<string> {
  const { context, cost, rateLimits } = await $.session.usage()
  return JSON.stringify({
    context: { tokens: context.tokens, window: context.window, percent: context.percent },
    cost_usd: cost?.usd,
    rate_limits: rateLimits.map((limit) => ({
      window: limit.kind, percent_used: limit.percentUsed, resets_at: limit.resetsAt,
    })),
  })
}

async function report($: EngineInterface) {
  if (reporting) {
    again = true
    return
  }
  reporting = true
  try {
    const python = await $.env.get("MV2_PYTHON")
    if (!python) return
    do {
      again = false
      const value = await snapshot($)
      if (value === reported) continue
      const result = await $.process.run(
        [python, "-m", "multiverse.tools.runtime_update", "--usage-json", value], { timeoutMs: 10000 })
      if (result.exitCode !== 0) {
        $.ui.log(`multiverse: usage report failed: ${result.stderr.trim()}`)
        return
      }
      reported = value
    } while (again)
  } catch (error) {
    $.ui.log(`multiverse: usage unavailable: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    reporting = false
  }
}

export function registerUsage(on: On) {
  on("session.start", async ($, e, next) => {
    const result = await next(e)
    void report($)
    return result
  })

  on("turn.complete", async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) void report($)
    return result
  })
}
