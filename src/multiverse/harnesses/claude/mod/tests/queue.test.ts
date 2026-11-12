// Offline tests of the queue file the mod keeps for mv2 (hooks/queue.ts): `claude plugin test`. The test's hooks stand
// in for Claude's core and the host: a file system the mod's files land in, and Claude taking, storing or refusing.
import type { EngineInterface, On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import { reloaded } from '../hooks/queue'

const FILE = '/agent/claude-queue.json'
const FOLLOW_UP = '7b2c0c1e-1111-4222-8333-944455556601'
const STEER = '7b2c0c1e-1111-4222-8333-944455556602'
const TRAILER = 'This came from another Claude session — not typed by your user.'

function world(on: On, options: { files?: Record<string, string>; refuse?: string } = {}) {
  mock.env(on, { MV2_PYTHON: '/python', MV2_AGENT_DIR: '/agent', MV2_LAUNCH_ID: 'launch-1' })
  const clock = mock.clock(on, { now: Date.parse('2026-11-10T12:00:00Z') })
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => ({ text: e.answer }))
  on('session.receive', async ($, e) => ({ text: e.text }))
  on('session.append', async ($, e, next) => next(e))
  on('classic.PermissionRequest', async () => ({}))
  on('classic.SessionStart', async () => ({}))
  on('session.id', async () => ({ value: 's1' }))
  on('ui.log', async () => ({ value: undefined }))
  on('ui.status', async () => ({ value: undefined }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }) as never)
  const files = new Map<string, string>(Object.entries(options.files ?? {}))
  const moves: string[][] = []
  on('fs.write', async ($, e) => { files.set(e.path, e.text); return { value: undefined } })
  on('fs.read', async ($, e) => {
    const text = files.get(e.path)
    if (text === undefined) throw new Error('no such file')
    return { value: text } as never
  })
  on('fs.stat', async () => ({ deny: 'ENOENT' }) as never)
  on('fs.list', async () => ({ value: [] }) as never)
  const ok = { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  on('process.run', async ($, e) => {
    if (e.argv[0] === 'mv') {
      moves.push([...e.argv])
      const text = files.get(e.argv[1] ?? '')
      if (text !== undefined) { files.delete(e.argv[1] ?? ''); files.set(e.argv[2] ?? '', text) }
    }
    return ok
  })
  on('prompt.submit', async ($, e) => options.refuse !== undefined && e.origin.kind === 'plugin'
    ? { drop: options.refuse } : { text: e.text, origin: e.origin })
  const queue = () => JSON.parse(files.get(FILE) ?? 'null') as
    { launch_id: string; session_id: string | null; coverage: string; reason: string | null; entries: { message_id: string; mode: string; since: string }[] } | null
  return { clock, files, moves, queue, ids: () => (queue()?.entries ?? []).map((entry) => [entry.message_id, entry.mode]) }
}

const start = ($: { session: { start: (input: never) => Promise<unknown> } }) =>
  $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true } as never)
const complete = (turnId: string) => ({ turnId, answer: '', durationMs: 1, isAborted: false, reason: 'answer' as const })
const wrapped = (text: string) =>
  `<system-reminder>\nAnother Claude session sent a message while you were working:\n${text}\n\n${TRAILER}\n</system-reminder>`

describe('queue file', () => {
  // mv2 refuses an agent's message to B while B's follow-ups wait in its queue; an entry left after its row is
  // stored would refuse sends for good, and one never written would let the two agents' messages cross.
  test('it lists the mv2 messages Claude holds, each until its row is stored', async ($, on) => {
    const w = world(on)
    await start($)
    await w.clock.settle()
    expect(w.queue()).toMatchObject({ launch_id: 'launch-1', session_id: 's1', coverage: 'complete', reason: null, entries: [] })
    expect(w.moves.at(-1)).toEqual(['mv', `${FILE}.tmp`, FILE])  // never read half-written
    await $.turn.start({ text: 'work', turnId: 't1' })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Message from b:\nThen test.<</MV2>>` })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Message from b:\nRebase.<</MV2>>` })
    await w.clock.settle()
    expect(w.ids()).toEqual([[FOLLOW_UP, 'follow-up'], [STEER, 'steer']])
    expect(w.queue()?.entries[0]?.since).toBe('2026-11-10T12:00:00.000Z')
    // The steer merges at a tool result; the follow-up runs once the turn ends.
    await $.session.append({ message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
      content: [{ type: 'text', text: wrapped('Message from b:\nRebase.') }] }, door: 'delivery', origin: { kind: 'peer' }, uuid: 'r1' } as never)
    await w.clock.settle()
    expect(w.ids()).toEqual([[FOLLOW_UP, 'follow-up']])
    await $.turn.complete(complete('t1'))
    await $.session.append({ message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Message from b:\nThen test.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true }, uuid: 'r2' } as never)
    await w.clock.settle()
    expect(w.ids()).toEqual([])
  })

  test('a message Claude refuses leaves it', async ($, on) => {
    // After an Esc Claude drops a queued plugin prompt; listing it would refuse the other agent's sends with nothing
    // waiting.
    const w = world(on, { refuse: 'the prompt was removed from the queue before it ran' })
    await start($)
    await $.turn.start({ text: 'work', turnId: 't1' })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Then test.<</MV2>>` })
    await w.clock.settle()
    expect(w.ids()).toEqual([])
    expect(w.files.has(`/agent/claude-receipts/${FOLLOW_UP}.not-delivered.json`)).toBe(true)
    expect(w.queue()?.coverage).toBe('complete')  // Claude's own refusal is final: nothing is held
  })

  test('a message Claude never stores leaves it after the grace, and coverage is unknown until a later turn', async ($, on) => {
    // Not stored within the grace, it counts as not confirmed, but that's no proof Claude dropped it: /compact or a
    // dialog holds a taken message and stores it afterwards. Listing it would refuse the other agent's sends for
    // good; calling the queue complete would hide that it may still come.
    const w = world(on)
    await start($)
    await $.turn.complete(complete('t0'))  // an observed idle moment
    await w.clock.advance(2000)
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Then test.<</MV2>>` })
    await w.clock.settle()
    expect(w.ids()).toEqual([[FOLLOW_UP, 'follow-up']])
    await w.clock.advance(6000)
    expect([w.ids(), w.queue()?.coverage]).toEqual([[], 'unknown'])
    expect(w.queue()?.reason).toContain("didn't store in time")
    // A later turn ends and the session settles idle: Claude holds nothing queued then.
    await $.turn.start({ text: 'other work', turnId: 't1' })
    await $.turn.complete(complete('t1'))
    await w.clock.advance(2000)
    expect([w.ids(), w.queue()?.coverage, w.queue()?.reason]).toEqual([[], 'complete', null])
  })

  test('an unsure message whose row comes later is credited, and the queue is complete again', async ($, on) => {
    // The point of not calling it refused for good: Claude stores a held message once /compact or a dialog lets go.
    // Its receipt must still be written, and the uncertainty it caused must end with it.
    const w = world(on)
    await start($)
    await $.turn.complete(complete('t0'))
    await w.clock.advance(2000)
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Then test.<</MV2>>` })
    await w.clock.advance(6000)
    expect([w.ids(), w.queue()?.coverage]).toEqual([[], 'unknown'])
    await $.session.append({ message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Then test.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true }, uuid: 'r1' } as never)
    await w.clock.settle()
    expect(w.files.has(`/agent/claude-receipts/${FOLLOW_UP}.json`)).toBe(true)
    expect([w.ids(), w.queue()?.coverage, w.queue()?.reason]).toEqual([[], 'complete', null])
  })

  // A reload empties the mod's memory while Claude may still hold what the code before it handed over, so it can't
  // claim to list everything; a new launch's first load can, since Claude's queue starts empty.
  const thisLaunch = JSON.stringify({ launch_id: 'launch-1', native_bridge: 'claude-mod-v4' })
  const before = JSON.stringify({ schema: 1, launch_id: 'launch-1', coverage: 'complete', entries: [] })
  // Brief replacement gaps in both records must not make a reload claim complete queue coverage.
  const gaps: [string, Record<string, string>, number, string, boolean, number[]][] = [
    ['queue briefly missing', { [FILE]: before }, 2, 'ENOENT', true, [3, 0]],
    ['runtime briefly missing', { [FILE]: '{}', '/agent/runtime.json': thisLaunch }, 2, 'ENOENT', true, [3, 3]],
    ['both permanently missing', {}, Infinity, 'ENOENT', false, [5, 5]],
    ['permission refused', {}, Infinity, 'EACCES', false, [1, 1]],
    ['malformed records', { [FILE]: '{', '/agent/runtime.json': '{' }, 0, 'ENOENT', false, [1, 1]],
  ]
  for (const [name, files, failures, code, expected, attempts] of gaps) {
    test(`reload detection with ${name} preserves evidence and bounds absence`, async () => {
      let now = 0
      const reads = new Map<string, number>()
      const delays: number[] = []
      const engine = {
        env: { get: async (key: string) => key === 'MV2_AGENT_DIR' ? '/agent' : 'launch-1' },
        clock: { now: async () => now, sleep: async (delay: number) => { delays.push(delay); now += delay } },
        fs: { read: async (path: string) => {
          const count = (reads.get(path) ?? 0) + 1
          reads.set(path, count)
          if (count <= failures || files[path] === undefined) throw Object.assign(new Error(code), { code })
          return files[path]
        } },
      } as unknown as EngineInterface
      expect(await reloaded(engine)).toBe(expected)
      expect([reads.get(FILE) ?? 0, reads.get('/agent/runtime.json') ?? 0]).toEqual(attempts)
      expect(delays.every((delay) => delay === 5)).toBe(true)
      expect(now <= 50).toBe(true)
    })
  }
  const loads: [string, Record<string, string>, string][] = [
    ['the queue file names this launch', { [FILE]: before }, 'unknown'],
    ['the bridge is registered for this launch', { '/agent/runtime.json': thisLaunch }, 'unknown'],
    ['a new launch, whose files are the last launch’s', { [FILE]: before.replace('launch-1', 'launch-0'),
      '/agent/runtime.json': thisLaunch.replace('launch-1', 'launch-0') }, 'complete'],
  ]
  for (const [name, files, coverage] of loads) {
    test(`a load where ${name} starts ${coverage}`, async ($, on) => {
      const w = world(on, { files })
      await start($)
      await w.clock.settle()
      expect(w.queue()?.coverage).toBe(coverage)
      if (coverage === 'complete') return
      expect(w.queue()?.reason).toContain('reloaded')
      await $.turn.start({ text: 'work', turnId: 't1' })
      await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Then test.<</MV2>>` })
      await w.clock.settle()
      expect([w.queue()?.coverage, w.ids().length]).toEqual(['unknown', 1])
      // What Claude queued starts as soon as a turn ends: idle counts only once nothing has started.
      await $.turn.complete(complete('t1'))
      await $.turn.start({ text: 'Then test.', turnId: 't2' })
      await w.clock.advance(2000)
      expect(w.queue()?.coverage).toBe('unknown')
      await $.turn.complete(complete('t2'))
      await w.clock.advance(2000)
      expect([w.queue()?.coverage, w.queue()?.reason]).toEqual(['complete', null])
    })
  }
})
