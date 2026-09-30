import type { EngineInterface } from 'claude-code'

// Reports the session's context fill, cost and rate-limit windows
// ($.session.usage(), free without arguments) to Multiverse, which stores them
// in this launch's runtime.json. Read when a session starts, when a main-loop
// turn completes, and after main-loop tool calls at most every 30 seconds, so a
// long turn stays current; a subagent's events carry its agentId.
// Reported only when a figure changed, one report at a time.

const TOOL_INTERVAL_MS = 30000
let lastRead = -Infinity

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
  lastRead = await $.clock.now()
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

// Called by index.ts when a session starts and when a main-loop turn completes:
// a plugin registers each event once, so this module adds no hooks of its own.
export function reportUsage($: EngineInterface) {
  void report($)
}

// Called by index.ts after each main-loop tool call; reports at most every
// 30 seconds, so a long turn stays current.
export async function reportUsageAfterTool($: EngineInterface) {
  if ((await $.clock.now()) - lastRead >= TOOL_INTERVAL_MS) void report($)
}
