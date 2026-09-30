import type { EngineInterface, On, SessionUsage } from 'claude-code'

// Reports the session's context fill, cost and rate-limit windows to
// Multiverse, which stores them in this launch's runtime.json. session.measure
// fires after each main-thread turn and when a rate-limit window moves;
// PostToolUse refreshes a long turn, at most every 30 seconds. Reported only
// when a figure changed, one report at a time.

const TOOL_INTERVAL_MS = 30000
let lastRead = -Infinity

let reported: string | undefined
let reporting = false
let latest: string | undefined

function snapshot({ context, cost, rateLimits }: Pick<SessionUsage, "context" | "cost" | "rateLimits">): string {
  return JSON.stringify({
    context: { tokens: context.tokens, window: context.window, percent: context.percent },
    cost_usd: cost?.usd,
    rate_limits: rateLimits.map((limit) => ({
      window: limit.kind, percent_used: limit.percentUsed, resets_at: limit.resetsAt,
    })),
  })
}

// Send the latest figures unless they were already sent; a burst of changes
// ends with the newest.
async function report($: EngineInterface, value: string) {
  latest = value
  if (reporting) return
  reporting = true
  try {
    const python = await $.env.get("MV2_PYTHON")
    if (!python) return
    while (latest !== undefined && latest !== reported) {
      const sending = latest
      const result = await $.process.run(
        [python, "-m", "multiverse.tools.runtime_update", "--usage-json", sending], { timeoutMs: 10000 })
      if (result.exitCode !== 0) {
        $.ui.log(`multiverse: usage report failed: ${result.stderr.trim()}`)
        return
      }
      reported = sending
    }
  } catch (error) {
    $.ui.log(`multiverse: usage unavailable: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    reporting = false
  }
}

export function registerUsage(on: On) {
  on("session.measure", async ($, e, next) => {
    lastRead = await $.clock.now()
    void report($, snapshot(e))
    return next(e)
  })

  // A subagent's tool calls carry its agent_id; only the main loop reports.
  on("classic.PostToolUse", async ($, e, next) => {
    if (e.agent_id === undefined) {
      const now = await $.clock.now()
      if (now - lastRead >= TOOL_INTERVAL_MS) {
        lastRead = now
        try {
          void report($, snapshot(await $.session.usage()))
        } catch (error) {
          $.ui.log(`multiverse: usage unavailable: ${error instanceof Error ? error.message : String(error)}`)
        }
      }
    }
    return next(e)
  })
}
