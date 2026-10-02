// Offline tests of the mod's compaction module: `claude plugin test src/multiverse/harnesses/claude/mod`.
// The test's hooks stand in for Claude's core (its own compaction beneath the
// mod) and the host: the install process the mod spawns, the background
// builder it runs, and the status line, notices and toasts it shows.
import type { On, SessionMessage } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

type Spawned = { argv: readonly string[]; input?: string }

function world(on: On, events: object[], options: { enabled?: boolean; exit?: number | null } = {}) {
  mock.env(on, { MV2_PYTHON: '/python', MV2_AGENT_DIR: '/agent', ...(options.enabled === false ? {} : { MV2_COMPACTION: '1' }) })
  const clock = mock.clock(on)
  const spawned: Spawned[] = []
  on('process.spawn', async function* ($, e) {
    spawned.push({ argv: e.argv, input: e.input })
    for (const event of events) yield { stream: 'stdout' as const, text: JSON.stringify(event) + '\n' }
    return { value: { code: options.exit === undefined ? 0 : options.exit, signal: null } }
  })
  const runs: (readonly string[])[] = []
  on('process.run', async ($, e) => {
    runs.push(e.argv)
    return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // Claude's own compaction, beneath the mod.
  const core: string[] = []
  on('session.compact', async ($, e) => {
    core.push(e.trigger)
    return { messages: [{ role: 'user', text: 'Claude summary', toolUses: [] }] }
  })
  on('classic.Stop', async () => ({}))
  const statuses: (string | undefined)[] = [], logs: string[] = [], toasts: string[] = []
  on('ui.status', async ($, e) => { statuses.push(e.text); return { value: undefined } })
  on('ui.log', async ($, e) => { logs.push(e.text); return { value: undefined } })
  on('ui.toast', async ($, e) => { toasts.push(e.text); return { value: undefined } })
  return { clock, spawned, runs, core, statuses, logs, toasts }
}

const offered: SessionMessage[] = [
  { role: 'user', text: 'Fix the bug', toolUses: [], handle: 'row-1' },
  { role: 'assistant', text: 'Fixed.', toolUses: [], handle: 'row-2' },
]

const built = { role: 'assistant', text: 'I traced the bug to parse().', toolUses: [] }

const success = [
  { event: 'progress', phase: 'building', turn: 'turn-aaaaaaaa', turns: 2, turns_done: 0, merges: 1, merges_left: 3 },
  { event: 'error', turn: 'turn-aaaaaaaa', kind: 'rate_limit', message: 'HTTP 429', retrying: true, attempt: 1, attempts: 10, wait: 20 },
  { event: 'progress', phase: 'built', turns: 2, turns_done: 2 },
  { event: 'message', message: { keep: 'row-1' } },
  { event: 'message', message: built },
  { event: 'result', count: 2, install_id: 'install-1', path: '/agent/compaction/installs/install-1.json',
    stats: { turns: 3, summarized: 1, tokens_before: 9000, tokens_after: 1200 } },
]

describe('session.compact', () => {
  test('answers /compact with the conversation the install streamed, showing its progress and errors', async ($, on) => {
    const w = world(on, success)
    const result = await $.session.compact({ trigger: 'manual', messages: offered })
    expect(result).toEqual({ messages: [offered[0], built] })
    expect(w.core).toEqual([])
    expect(w.spawned[0]!.argv).toEqual(['/python', '-m', 'multiverse.compaction', 'install', '--agent-dir', '/agent'])
    const request = JSON.parse(w.spawned[0]!.input ?? '{}')
    expect(request.trigger).toBe('manual')
    expect(request.offered).toEqual([{ handle: 'row-1', role: 'user' }, { handle: 'row-2', role: 'assistant' }])
    expect(w.statuses).toContain('compacting ▱▱▱▱▱▱▱▱ 0/2 turns · turn turn-aaa: 1 merges, 3 to go')
    expect(w.statuses).toContain('compacting · rate_limit on turn turn-aaa, retrying in 20s')
    expect(w.statuses.at(-1)).toBeUndefined()
    expect(w.logs).toContain('mv2 compaction: turn turn-aaa: rate_limit: HTTP 429 (retry 1/10 in 20s)')
    expect(w.toasts.length).toBe(1)
    expect(w.logs.at(-1)).toBe('mv2 compaction: 3 turns, 1 summarized, about 9000 → 1200 tokens')
  })

  test('a failed install skips with its reason and never falls back to Claude', async ($, on) => {
    const w = world(on, [{ event: 'failed', message: 'Could not build 1 summary tree(s): turn abc: refused' }], { exit: 1 })
    const result = await $.session.compact({ trigger: 'manual', messages: offered })
    expect(result).toEqual({ skip: 'mv2 compaction: Could not build 1 summary tree(s): turn abc: refused' })
    expect(w.core).toEqual([])
    expect(w.toasts.at(-1)).toBe('mv2 compaction: Could not build 1 summary tree(s): turn abc: refused')
  })

  const broken: [string, object[], number | null, string][] = [
    ['a stopped process', [], null, 'the compaction process stopped'],
    ['a short stream', [{ event: 'message', message: built }, { event: 'result', count: 2, install_id: 'i' }], 0,
      'expected 2 messages'],
    ['an unknown row', [{ event: 'message', message: { keep: 'row-9' } }, { event: 'result', count: 1, install_id: 'i' }],
      0, 'did not offer (row-9)'],
  ]
  for (const [name, events, exit, reason] of broken) {
    test(`${name} skips with its reason`, async ($, on) => {
      const w = world(on, events, { exit })
      const result = await $.session.compact({ trigger: 'manual', messages: offered })
      expect('skip' in result && result.skip.includes(reason)).toBe(true)
      expect(w.core).toEqual([])
    })
  }

  test("leaves Claude's compaction alone when compaction is off", async ($, on) => {
    const w = world(on, success, { enabled: false })
    await $.session.compact({ trigger: 'manual', messages: offered })
    expect(w.core).toEqual(['manual'])
    expect(w.spawned).toEqual([])
  })

  test("leaves a subagent's compaction to Claude and refuses precompute", async ($, on) => {
    const w = world(on, success)
    await $.session.compact({ trigger: 'auto', messages: offered, agentId: 'sub-1' })
    expect(w.core).toEqual(['auto'])
    const precompute = await $.session.compact({ trigger: 'precompute', messages: offered })
    expect('skip' in precompute).toBe(true)
    expect(w.spawned).toEqual([])
  })
})

describe('turn end', () => {
  test('starts the background builder with when the turn ended', async ($, on) => {
    const w = world(on, [])
    await $.classic.Stop({ stop_hook_active: false })
    await w.clock.settle()
    const argv = w.runs.find((run) => run.includes('background'))
    expect(argv?.slice(0, 6)).toEqual(['/python', '-m', 'multiverse.compaction', 'background', '--agent-dir', '/agent'])
    expect(argv?.[6]).toBe('--turn-ended')
    expect(Number(argv?.[7])).toBe(w.clock.now())
  })

  test('does nothing when compaction is off', async ($, on) => {
    const w = world(on, [], { enabled: false })
    await $.classic.Stop({ stop_hook_active: false })
    await w.clock.settle()
    expect(w.runs.filter((run) => run.includes('background'))).toEqual([])
  })
})
