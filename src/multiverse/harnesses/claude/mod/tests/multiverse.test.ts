// Offline tests of the multiverse mod: `claude plugin test src/multiverse/harnesses/claude/mod`.
// The test's hooks sit beneath the mod and stand in for Claude's core and the
// host: the files it writes, the status reports it runs, the prompts it submits
// and the forks it asks for. Its session.append stand-in records the row the mod
// hands down to be stored (what the model and the transcript read) and passes it on.
// From Claude Code 2.1.293 the kit stores it (mock.session), so the receipt the mod
// writes once a row is stored is checked too; 2.1.292's kit had no core for it and
// answers "no implementation", which `append` below accepts so the tests run on both.
import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import { agePhrase } from '../hooks/age'

const STEER = '3f2a0c1e-1111-4222-8333-944455556661'
const FOLLOW_UP = '3f2a0c1e-1111-4222-8333-944455556662'
const ASK = '3f2a0c1e-1111-4222-8333-944455556663'
const RETRY = '3f2a0c1e-1111-4222-8333-944455556664'  // core gives each retry an ID of its own
const TRAILER = 'This came from another Claude session — not typed by your user.'

type Submitted = { text: string; asUser: boolean }

// Raise session.append; the stored row, or undefined on a kit (Claude Code before
// 2.1.293) that has no core to store it. Any other failure fails the test.
async function append($: { session: { append: (input: never) => Promise<unknown> } }, row: unknown) {
  try {
    return await $.session.append(row as never) as { uuid?: string }
  } catch (error) {
    if (String(error).includes('no implementation for session.append')) return undefined
    throw error
  }
}

function world(on: On, options: { sessionId?: string; moveExit?: number; file?: () => string | undefined; now?: number; refuse?: string; holdRefusals?: Promise<void>; holdSubmits?: Promise<void>; holdUnsure?: Promise<void>; env?: Record<string, string>; statusThrows?: number } = {}) {
  mock.env(on, { MV2_PYTHON: '/python', MV2_AGENT_DIR: '/agent', ...options.env })
  const clock = mock.clock(on, { now: options.now ?? 0 })
  // Claude's core for the engine events the tests raise: each passes through.
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => ({ text: e.answer }))
  on('session.receive', async ($, e) => ({ text: e.text }))
  // The row the mod hands down to be stored.
  const appended: unknown[] = []
  on('session.append', async ($, e, next) => { appended.push(e.message.content); return next(e) })
  on('classic.PermissionRequest', async () => ({}))
  on('classic.SessionStart', async () => ({}))
  on('prompt.attachment', async ($, e) => ({ text: e.text }))
  // The host: files the mod writes and the status reports it runs.
  const files = new Map<string, string>()
  on('fs.write', async ($, e) => {
    // A refusal write the test holds finishes only when it says so: after a delivery, say.
    if (options.holdRefusals !== undefined && e.path.endsWith('.not-delivered.json')) await options.holdRefusals
    // An unsure refusal's write the test holds: one still running when Claude's own refusal comes.
    if (options.holdUnsure !== undefined && e.path.endsWith('.not-delivered.json') && JSON.parse(e.text).definite === false) {
      await options.holdUnsure
    }
    files.set(e.path, e.text)
    return { value: undefined }
  })
  const statuses: string[] = []
  const moves: string[][] = []
  const reads: (string | undefined)[] = []
  // Each Python callback's runtime_update flag and the PYTHONPATH it was given.
  const pythons: [string, string | undefined][] = []
  const reports: string[][] = []
  let thrown = 0
  on('process.run', async ($, e) => {
    if (e.argv[1] === '-m') pythons.push([e.argv[3] ?? '', e.init?.env?.PYTHONPATH])
    const index = e.argv.indexOf('--status')
    // A status report the test makes throw, as a timeout or a refusal does, before anything is recorded.
    if (index >= 0 && thrown < (options.statusThrows ?? 0)) { thrown += 1; throw new Error('timed out after 10000 ms') }
    if (index >= 0) { statuses.push(e.argv[index + 1] ?? ''); reports.push([...e.argv]) }
    if (e.argv[0] === 'sh') {  // the mod reading Claude's process file
      reads.push(options.file?.())
      const id = reads.at(-1)
      return { value: { exitCode: id ? 0 : 1, stdout: id ? JSON.stringify({ sessionId: id }) : '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    const move = e.argv.includes('--native-session-id')
    if (move) moves.push([...e.argv])
    const exitCode = move ? options.moveExit ?? 0 : 0
    return { value: { exitCode, stdout: '', stderr: exitCode ? 'refused: bound to another agent' : '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.id', async () => ({ value: options.sessionId ?? '11111111-1111-4111-8111-111111111111' }))
  const logs: string[] = []
  on('ui.log', async ($, e) => { logs.push(String(e.text ?? e)); return { value: undefined } })
  const submitted: Submitted[] = []
  on('prompt.submit', async ($, e) => {
    submitted.push({ text: e.text, asUser: e.origin.kind === 'plugin' && e.origin.asUser === true })
    // A submit the test holds is answered only when it says so: after a /compact, say.
    if (options.holdSubmits !== undefined) await options.holdSubmits
    // Claude refusing a plugin's prompt, as after an interruption.
    if (options.refuse !== undefined && e.origin.kind === 'plugin') return { drop: options.refuse }
    return { text: e.text, origin: e.origin }
  })
  return { clock, files, statuses, submitted, appended, moves, logs, reads, pythons, reports }
}

// The text as the mod hands it to Claude: tagged with its ID (and send time), stripped where the row is stored.
const handed = (id: string, text: string, sent?: string) => `<<MV2 id=${id}${sent !== undefined ? ` sent=${sent}` : ''}>>${text}<</MV2>>`

function wrapped(text: string, midTurn = true) {
  const header = midTurn ? 'Another Claude session sent a message while you were working:' : 'Another Claude session sent a message:'
  return `<system-reminder>\n${header}\n${text}\n\n${TRAILER}\n</system-reminder>`
}

const turnComplete = (turnId: string) =>
  ({ turnId, answer: '', durationMs: 1, isAborted: false, reason: 'answer' as const })

describe('session moves', () => {
  const OLD = '11111111-1111-4111-8111-111111111111'
  const NEW = '22222222-2222-4222-8222-222222222222'

  test('a /clear tells mv2 the session this process moved to, once', async ($, on) => {
    let file = NEW
    const w = world(on, { sessionId: OLD, file: () => file })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: NEW } as never)
    await w.clock.settle()
    expect(w.moves).toEqual([['/python', '-m', 'multiverse.tools.runtime_update', '--native-session-id', NEW,
                              '--previous-session-id', OLD, '--session-reason', 'clear']])
    // A second /clear moves on from the new session.
    const NEXT = '33333333-3333-4333-8333-333333333333'
    file = NEXT
    await $.classic.SessionStart({ source: 'clear', session_id: NEXT } as never)
    await w.clock.settle()
    expect(w.moves.at(-1)?.slice(4, 7)).toEqual([NEXT, '--previous-session-id', NEW])
  })

  test('nothing is reported when the session did not change or was never known', async ($, on) => {
    const w = world(on, { sessionId: OLD })
    await $.classic.SessionStart({ source: 'clear', session_id: NEW } as never)  // before the mod knew its session
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: OLD } as never)
    await w.clock.settle()
    expect(w.moves).toEqual([])
  })

  test("the move is reported only once Claude's process file names the new session", async ($, on) => {
    let polls = 0
    const w = world(on, { sessionId: OLD, file: () => (++polls < 4 ? OLD : NEW) })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: NEW } as never)
    for (let i = 0; i < 10 && w.moves.length === 0; i++) await w.clock.advance(100)
    expect(w.reads.slice(0, 4)).toEqual([OLD, OLD, OLD, NEW])  // still the old session three times, then the new one
    expect(w.moves.length).toBe(1)
  })

  test('a process file that never names the new session reports nothing', async ($, on) => {
    const w = world(on, { sessionId: OLD, file: () => OLD })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: NEW } as never)
    for (let i = 0; i < 60; i++) await w.clock.advance(100)
    await w.clock.settle()
    expect(w.moves).toEqual([])
    expect(w.logs).toContain("multiverse: the move to a new session was not recorded: Claude's process file never named it")
  })

  test('a refused move is logged once and never retried', async ($, on) => {
    const w = world(on, { sessionId: OLD, moveExit: 1, file: () => NEW })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: NEW } as never)
    await w.clock.settle()
    await w.clock.advance(60000)
    expect(w.moves.length).toBe(1)
    expect(w.logs.filter((line) => line.includes('not recorded'))).toEqual([
      'multiverse: the move to a new session was not recorded: refused: bound to another agent'])
  })
})

// Every Python callback runs mv2's own checkout: without MV2_SOURCE first on its PYTHONPATH,
// `python -m` imports whatever mv2 the interpreter has installed (a stale install, or none, so
// the bridge never registers), and nothing shows that the wrong code ran.
describe('the mv2 a callback imports', () => {
  const NEW = '22222222-2222-4222-8222-222222222222'
  const rows: [string, Record<string, string>, string | undefined][] = [
    ['MV2_SOURCE comes before a PYTHONPATH already set', { MV2_SOURCE: '/src', PYTHONPATH: '/mine' }, '/src:/mine'],
    ['MV2_SOURCE alone is the PYTHONPATH', { MV2_SOURCE: '/src' }, '/src'],
    ['without MV2_SOURCE, PYTHONPATH is left as it is', { PYTHONPATH: '/mine' }, undefined],
  ]
  for (const [name, env, expected] of rows) {
    test(name, async ($, on) => {
      const w = world(on, { env, file: () => NEW })
      on('session.measure', async () => ({ changed: [] }))
      await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
      await w.clock.settle()
      await $.classic.SessionStart({ source: 'clear', session_id: NEW } as never)
      await w.clock.settle()
      await $.session.measure({ context: { window: 200000, tokens: 1000 }, rateLimits: [] } as never)
      await w.clock.settle()
      expect(w.pythons.map(([flag]) => flag).sort()).toEqual(['--native-session-id', '--status', '--usage-json'])
      for (const [, path] of w.pythons) expect(path).toBe(expected)
    })
  }
})

describe('status', () => {
  // A status report that throws (a timeout, or a refusal before Claude's trust prompt is answered) is logged like
  // one that exits non-zero, and the next status change retries it: unlogged, a first registration that failed
  // left no trace in the pane.
  test('a status report that throws is logged, and the next change retries it', async ($, on) => {
    const w = world(on, { statusThrows: 1 })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await w.clock.settle()
    expect(w.logs.filter((line) => line.includes('status report failed'))).toHaveLength(1)
    expect(w.reports).toEqual([])
    await $.turn.start({ text: 'go', turnId: 't1' })
    await w.clock.settle()
    expect(w.reports.at(-1)).toEqual(['/python', '-m', 'multiverse.tools.runtime_update', '--status', 'working', '--bridge', 'claude-mod-v2'])
  })

  test('idle when the session starts, working in a turn, waiting on a question, idle after', async ($, on) => {
    const w = world(on)
    let answer!: () => void
    const answered = new Promise<void>((resolve) => { answer = resolve })
    on('tool.call', async () => { await answered; return { result: { answers: {} } } as never })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('idle')
    await $.turn.start({ text: 'go', turnId: 't1' })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('working')
    const question = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'q1', questions: [] } as never)
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('waiting')
    answer()
    await question
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('working')
    await $.turn.complete(turnComplete('t1'))
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('idle')
  })

  test("a subagent's or teammate's turns leave the main loop's status alone", async ($, on) => {
    const w = world(on)
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('idle')
    // A background subagent runs while the main loop is idle: still idle, and nothing reported.
    const reported = w.statuses.length
    await $.turn.start({ text: 'look into it', turnId: 's1', agentId: 'sub-1' } as never)
    await w.clock.settle()
    expect(w.statuses.length).toBe(reported)
    expect(w.statuses.at(-1)).toBe('idle')
    await $.turn.complete({ ...turnComplete('s1'), agentId: 'sub-1' } as never)
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('idle')
    // During a main-loop turn, a subagent's turn ending does not end it.
    await $.turn.start({ text: 'go', turnId: 't1' })
    await $.turn.start({ text: 'part', turnId: 's2', agentId: 'sub-2' } as never)
    await $.turn.complete({ ...turnComplete('s2'), agentId: 'sub-2' } as never)
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('working')
    await $.turn.complete(turnComplete('t1'))
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('idle')
  })

  test('a permission prompt waits until the turn ends', async ($, on) => {
    const w = world(on)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: { command: 'rm -rf build' } })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('waiting')
    await $.turn.complete(turnComplete('t1'))
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('idle')
  })
})

describe('delivery', () => {
  test('a mid-turn steer shows without tags and is stored without Claude’s wrapper', async ($, on) => {
    // Claude stores and draws queued text exactly as the mod hands it over, for good, so the steer goes clean: a
    // tag left in it showed in the terminal and reached GOAT's reading of the transcript.
    const w = world(on)
    await $.turn.start({ text: 'work', turnId: 't1' })
    const text = 'Message from lead:\nRebase first.'
    const received = await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>${text}<</MV2>>` })
    expect(received).toEqual({ text })
    const stored = await append($, {
      message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
                 content: [{ type: 'text', text: wrapped(text) }] },
      door: 'delivery', origin: { kind: 'peer' }, uuid: 'row-1' })
    expect(w.appended).toEqual([[{ type: 'text', text: `<system-reminder>\n${text}\n</system-reminder>` }]])
    expect(w.submitted).toEqual([])
    if (stored) {
      // Stored, so the mod writes its receipt for this message.
      await w.clock.settle()
      expect([...w.files.keys()].filter((path) => path.includes('claude-receipts'))).toEqual([`/agent/claude-receipts/${STEER}.json`])
    }
  })

  test('an idle steer and a follow-up are submitted as the person’s words', async ($, on) => {
    const w = world(on)
    await $.turn.complete(turnComplete('t0'))  // an observed idle moment
    const idle = await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Hello.<</MV2>>` })
    expect(idle).toEqual({ consumed: expect.any(String) })
    await $.turn.start({ text: 'Hello.', turnId: 't1' })
    // A follow-up is taken even mid-turn; Claude runs it once the turn ends.
    const later = await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Then test.<</MV2>>` })
    expect(later).toEqual({ consumed: expect.any(String) })
    await w.clock.settle()
    // Submitted clean: Claude shows a submitted prompt's text in its queue line until it runs (CLAUDE-16), so a tag
    // in it would show there. Their rows are stored as bare text, with no wrapper, and credited by that text.
    expect(w.submitted).toEqual([{ text: 'Hello.', asUser: true }, { text: 'Then test.', asUser: true }])
    const stored = await append($, {
      message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Hello.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true }, uuid: 'row-2' })
    expect(w.appended).toEqual([[{ type: 'text', text: 'Hello.' }]])
    if (stored) {
      await w.clock.settle()
      expect([...w.files.keys()].filter((path) => path.includes('claude-receipts'))).toEqual([`/agent/claude-receipts/${STEER}.json`])
    }
  })

  // Identical texts can't be told apart once stored. Every message goes clean, and Claude stores its queue and its
  // submitted prompts in order, so identical ones are credited oldest first; neither is left "accepted, no
  // confirmation" while the agent is live (CLAUDE-14), and a queued steer and a submitted prompt never take each
  // other's row.
  test('identical messages each get their own receipt, oldest first', async ($, on) => {
    const w = world(on)
    const second = (id: string) => id.slice(0, -1) + (id === STEER ? '7' : '8')
    const receipts = () => [...w.files.keys()].filter((path) => path.includes('claude-receipts')).sort()
    const named = (...ids: string[]) => ids.map((id) => `/agent/claude-receipts/${id}.json`).sort()
    await $.turn.start({ text: 'work', turnId: 't1' })
    const steers: string[] = []
    for (const id of [STEER, second(STEER)]) {
      const handed = await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${id}>>Same words.<</MV2>>` })
      steers.push((handed as { text: string }).text)
    }
    for (const id of [FOLLOW_UP, second(FOLLOW_UP)]) {
      await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${id}>>Same words.<</MV2>>` })
    }
    await w.clock.settle()
    expect(steers).toEqual(['Same words.', 'Same words.'])
    expect(w.submitted.map((submitted) => submitted.text)).toEqual(['Same words.', 'Same words.'])
    const steered = () => append($, { message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
      content: [{ type: 'text', text: wrapped('Same words.') }] }, door: 'delivery', origin: { kind: 'peer' } })
    const prompted = () => append($, { message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Same words.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true } })
    // Another plugin's prompt with the same words is not ours.
    const other = await append($, { message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Same words.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'other', asUser: true } })
    if (!other) return  // a kit with no core to store rows writes no receipts
    await w.clock.settle()
    expect(receipts()).toEqual([])
    await prompted()
    await w.clock.settle()
    expect(receipts()).toEqual(named(FOLLOW_UP))  // a prompt row is a submitted message's, never a queued steer's
    await steered()
    await w.clock.settle()
    expect(receipts()).toEqual(named(FOLLOW_UP, STEER))
    await steered()
    await prompted()
    await w.clock.settle()
    expect(receipts()).toEqual(named(STEER, second(STEER), FOLLOW_UP, second(FOLLOW_UP)))
    expect(JSON.stringify(w.appended).includes('<<MV2')).toBe(false)
  })

  test('a stored steer is the waiting one whose whole text it holds, the longest first', async ($, on) => {
    // Queued steers are told by their text alone; a short one inside a longer one must not take its row, or the
    // longer one is left unconfirmed and the shorter one confirmed before Claude stored it.
    const w = world(on)
    const receipts = () => [...w.files.keys()].filter((path) => path.includes('claude-receipts')).sort()
    const longer = STEER.slice(0, -1) + '7'
    await $.turn.start({ text: 'work', turnId: 't1' })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Rebase.<</MV2>>` })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${longer}>>Rebase. Then test.<</MV2>>` })
    const steered = (text: string) => append($, { message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
      content: [{ type: 'text', text: wrapped(text) }] }, door: 'delivery', origin: { kind: 'peer' } })
    if (!(await steered('Rebase. Then test.'))) return  // a kit with no core to store rows writes no receipts
    await w.clock.settle()
    expect(receipts()).toEqual([`/agent/claude-receipts/${longer}.json`])
    await steered('Rebase.')
    await w.clock.settle()
    expect(receipts()).toEqual([`/agent/claude-receipts/${longer}.json`, `/agent/claude-receipts/${STEER}.json`].sort())
  })

  // A late message read as current is acted on as though nothing had changed since it was written; the age is
  // added where each row is stored, on two paths, so a break on either goes unnoticed.
  test('a message reaching the model more than ten minutes after it was sent says how old it is', async ($, on) => {
    const MIN = 60_000
    const now = Date.parse('2026-10-23T12:00:00Z')
    const w = world(on, { now })
    const at = (ms: number) => new Date(now - ms).toISOString()
    // A mid-turn steer, 25 minutes late.
    await $.turn.start({ text: 'work', turnId: 't1' })
    const steer = 'Message from lead:\nRebase first.'
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER} sent=${at(25 * MIN)}>>${steer}<</MV2>>` })
    await append($, {
      message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
                 content: [{ type: 'text', text: wrapped(handed(STEER, steer, at(25 * MIN))) }] },
      door: 'delivery', origin: { kind: 'peer' }, uuid: 'row-1' })
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: '<system-reminder>\nMessage from lead (sent 25 min ago):\nRebase first.\n</system-reminder>' }])
    // Follow-ups typed in as the person's words: the age counts from sending to the moment the row is stored.
    const rows: [string, number, number, string, string][] = [
      ['ten minutes is not late', 10 * MIN, 0, 'Message from lead:', 'Message from lead:'],
      ['an hour, singular', 60 * MIN, 0, 'Message from lead:', 'Message from lead (sent 1 hour ago):'],
      ['from another universe', 125 * MIN, 0, 'Message from lead (in universe review):', 'Message from lead (in universe review) (sent 2 hours ago):'],
      ['days', 3 * 24 * 60 * MIN, 0, 'Message from lead:', 'Message from lead (sent 3 days ago):'],
      ['no sender line', 40 * MIN, 0, 'Rebase first.', 'Rebase first.'],
      ['core aged it already: the age is replaced, not added', 50 * MIN, 0, 'Message from lead (sent 12 min ago):', 'Message from lead (sent 50 min ago):'],
      ['core aged one from another universe', 3 * 60 * MIN, 0, 'Message from lead (in universe review) (sent 1 hour ago):', 'Message from lead (in universe review) (sent 3 hours ago):'],
      // Last, since it moves the clock.
      ['waited behind a turn after arriving', 0, 30 * MIN, 'Message from lead:', 'Message from lead (sent 30 min ago):'],
    ]
    for (const [index, [name, age, wait, line, shown]] of rows.entries()) {
      const id = `3f2a0c1e-1111-4222-8333-9444555567${String(index).padStart(2, '0')}`
      const body = `${line}\nfollow-up ${index}`
      const sent = new Date(now - age).toISOString()
      await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${id} sent=${sent}>>${body}<</MV2>>` })
      await w.clock.settle()
      await w.clock.advance(wait)
      await append($, {
        message: { type: 'user', role: 'user', content: [{ type: 'text', text: handed(id, body, sent) }] },
        door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true }, uuid: `age-${index}` })
      expect([name, w.appended.at(-1)]).toEqual([name, [{ type: 'text', text: `${shown}\nfollow-up ${index}` }]])
    }
    // A prompt Claude stores as several text blocks: only the block with the sender line gains the age, and the
    // others stay exactly as they were, never repeated (reviewer, 6f0af4c).
    const parts = ['Message from lead:\nfirst', 'second']
    const blocksSent = new Date(await w.clock.now() - 60 * MIN).toISOString()
    const blocksId = '3f2a0c1e-1111-4222-8333-944455556799'
    await $.session.receive({ origin: { kind: 'peer' },
      text: `<<MV2 follow-up id=${blocksId} sent=${blocksSent}>>${parts.join('\n')}<</MV2>>` })
    await w.clock.settle()
    // The tag opens in the first block and closes in the last; both come off.
    const tag = handed(blocksId, '', blocksSent).replace('<</MV2>>', '')
    await append($, {
      message: { type: 'user', role: 'user', content: [{ type: 'text', text: tag + parts[0] }, { type: 'text', text: parts[1] + '<</MV2>>' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true }, uuid: 'age-blocks' })
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: 'Message from lead (sent 1 hour ago):\nfirst' }, { type: 'text', text: 'second' }])
    // A send time without its zone gives no age, as in core: JavaScript would read it as local time.
    expect(agePhrase('2026-10-20T10:00:00', now)).toBeUndefined()  // days ago in any zone, so only the rule refuses it
    expect(agePhrase('2026-10-23T10:00:00+00:00', now)).toBe('sent 2 hours ago')
  })

  test('a steer no tool result took is dropped as a peer prompt and submitted instead', async ($, on) => {
    const w = world(on)
    await $.turn.start({ text: 'work', turnId: 't1' })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Late.<</MV2>>` })
    await $.turn.complete(turnComplete('t1'))
    // Claude runs the queued text, which is clean, as a peer prompt of its own.
    const own = await $.prompt.submit({ text: 'Late.', origin: { kind: 'peer' }, wait: false } as never)
    expect(own).toEqual({ drop: expect.any(String) })
    await w.clock.settle()
    expect(w.submitted).toEqual([{ text: 'Late.', asUser: true }])
  })

  test('after an Esc a message runs as Claude’s own peer prompt until the person types', async ($, on) => {
    // Claude refuses a plugin's prompt after an interruption until the person sends input (CLAUDE-15).
    const w = world(on)
    const receipts = () => [...w.files.keys()].filter((path) => path.includes('claude-receipts'))
    await $.turn.start({ text: 'work', turnId: 't1' })
    await $.turn.complete({ ...turnComplete('t1'), isAborted: true, reason: 'aborted' as const })
    const text = 'Message from lead:\nRebase first.'
    // Left in Claude's queue as its clean text, not submitted.
    const queuedText = text
    expect(await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>${text}<</MV2>>` })).toEqual({ text: queuedText })
    // Claude runs it as a turn of its own, and the mod lets it.
    expect(await $.prompt.submit({ text: queuedText, origin: { kind: 'peer' }, wait: false } as never))
      .toEqual({ text: queuedText, origin: { kind: 'peer' } })
    // Its row is stored without Claude's wrapper or the tag, and receipted.
    const stored = await append($, {
      message: { type: 'user', role: 'user', content: [{ type: 'text', text: `Another Claude session sent a message:\n${queuedText}\n\n${TRAILER}` }] },
      door: 'prompt', origin: { kind: 'peer' }, uuid: 'row-5' })
    expect(w.appended).toEqual([[{ type: 'text', text }]])
    await w.clock.settle()
    if (stored) expect(receipts()).toEqual([`/agent/claude-receipts/${STEER}.json`])
    // The turn that prompt started lifts nothing: Claude still refuses the plugin, so a follow-up stays in its queue.
    await $.turn.start({ text, turnId: 't2' })
    await $.turn.complete(turnComplete('t2'))
    expect(await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Then test.<</MV2>>` }))
      .toEqual({ text: 'Then test.' })
    // The person's own input lifts it; the next message is submitted as before.
    await $.prompt.submit({ text: 'go on', origin: { kind: 'composer' }, wait: false } as never)
    expect(await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${ASK}>>Again.<</MV2>>` })).toEqual({ consumed: expect.any(String) })
    await w.clock.settle()
    expect(w.submitted.filter((s) => s.asUser)).toEqual([{ text: 'Again.', asUser: true }])
  })

  test('a submit Claude refuses moves later messages to the peer route', async ($, on) => {
    // A reload forgets the interruption; Claude's refusal tells the mod again.
    const w = world(on, { refuse: 'the session was stopped and nobody has sent input since' })
    await $.turn.complete(turnComplete('t0'))  // an observed idle moment
    expect(await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>First.<</MV2>>` })).toEqual({ consumed: expect.any(String) })
    await w.clock.settle()
    expect(w.logs.some((line) => line.includes('the session was stopped'))).toBe(true)
    expect(await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${FOLLOW_UP}>>Second.<</MV2>>` }))
      .toEqual({ text: 'Second.' })
  })

  test("Claude's own refusal after the idle timeout makes the receipt definite", async ($, on) => {
    // The timeout only says the row hasn't come. Claude answering the held submit with a refusal afterwards is the
    // proof, so --retry may send it again; left unsure, the message would stay unconfirmed and never be retried.
    let release = () => {}
    const reason = 'the session was stopped and nobody has sent input since'
    const w = world(on, { refuse: reason, holdSubmits: new Promise<void>((resolve) => { release = resolve }) })
    const refusal = () => JSON.parse(w.files.get(`/agent/claude-receipts/${STEER}.not-delivered.json`) ?? 'null')
    await $.turn.complete(turnComplete('t0'))  // an observed idle moment
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Hello.<</MV2>>` })
    await w.clock.settle()
    await w.clock.advance(5000)
    expect(refusal()).toEqual(expect.objectContaining({ id: STEER, delivered: false, definite: false }))
    release()
    await w.clock.settle()
    expect(refusal()).toEqual(expect.objectContaining({ id: STEER, delivered: false, definite: true, reason }))
  })

  test("a timeout's write still running when Claude refuses never ends up over Claude's refusal", async ($, on) => {
    // The writes finish in any order. Left that way, a slow unsure write lands last and a message Claude refused
    // stays unconfirmed for good: the drop forgot it, so nothing writes its receipt again.
    let releaseSubmit = () => {}
    let releaseUnsure = () => {}
    const reason = 'the session was stopped and nobody has sent input since'
    const w = world(on, { refuse: reason, holdSubmits: new Promise<void>((resolve) => { releaseSubmit = resolve }),
      holdUnsure: new Promise<void>((resolve) => { releaseUnsure = resolve }) })
    const refusal = () => JSON.parse(w.files.get(`/agent/claude-receipts/${STEER}.not-delivered.json`) ?? 'null')
    await $.turn.complete(turnComplete('t0'))  // an observed idle moment
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Hello.<</MV2>>` })
    await w.clock.settle()
    await w.clock.advance(5000)  // the timeout's unsure write starts, and is held
    releaseSubmit()  // Claude refuses the held submit meanwhile
    await w.clock.settle()
    releaseUnsure()
    await w.clock.settle()
    expect(refusal()).toEqual(expect.objectContaining({ id: STEER, delivered: false, definite: true, reason }))
  })

  test('a refused message gets a not-delivered receipt', async ($, on) => {
    // Without it the message stays accepted-but-unconfirmed forever and --retry never resends it.
    const reason = 'the session was stopped and nobody has sent input since'
    const w = world(on, { refuse: reason })
    await $.turn.complete(turnComplete('t0'))  // an observed idle moment
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Hello.<</MV2>>` })
    await w.clock.settle()
    expect(JSON.parse(w.files.get(`/agent/claude-receipts/${STEER}.not-delivered.json`) ?? 'null'))
      .toEqual({ id: STEER, delivered: false, definite: true, reason, at: expect.any(Number) })
    expect(w.files.has(`/agent/claude-receipts/${STEER}.json`)).toBe(false)
  })

  // A timeout is only "not confirmed": Claude may still hold the message (a /compact or a dialog holds its turn) and
  // store it later, so it is never definite. A second message the person sends anyway, with the same words, can't be
  // told from the first once stored: the rows are credited oldest first, so both end confirmed when both arrive.
  test("a row that hasn't come after a few idle seconds is left unconfirmed, and a second copy is credited oldest first", async ($, on) => {
    const w = world(on)
    await $.turn.complete(turnComplete('t0'))  // an observed idle moment
    const receipt = (id: string) => JSON.parse(w.files.get(`/agent/claude-receipts/${id}.json`) ?? 'null')
    const refusal = (id: string) => JSON.parse(w.files.get(`/agent/claude-receipts/${id}.not-delivered.json`) ?? 'null')
    const prompted = () => append($, { message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Hello.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true } })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Hello.<</MV2>>` })
    await w.clock.settle()
    await w.clock.advance(4000)
    expect(refusal(STEER)).toBe(null)
    await w.clock.advance(1000)
    expect(refusal(STEER)).toEqual(expect.objectContaining({ id: STEER, delivered: false, definite: false }))
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${RETRY}>>Hello.<</MV2>>` })
    await w.clock.settle()
    expect(w.submitted.map((submitted) => submitted.text)).toEqual(['Hello.', 'Hello.'])
    if (!(await prompted())) return
    await w.clock.settle()
    expect([receipt(STEER)?.route, receipt(RETRY)]).toEqual(['prompt', null])
    await prompted()
    await w.clock.settle()
    expect([receipt(STEER)?.route, receipt(RETRY)?.route]).toEqual(['prompt', 'prompt'])
    expect(JSON.stringify(w.appended).includes('<<MV2')).toBe(false)
  })

  test('an old tagged peer row does not confirm a new clean steer after reload', async ($, on) => {
    // An older mod queued STEER inside its tag; a reload forgot it, and this instance queued RETRY, a separate message
    // with the same text, clean. STEER's row names STEER by its outer tag, so it must not confirm RETRY (lifecycle's
    // review of the clean queued route); RETRY stays waiting for its own row.
    const w = world(on)
    const body = 'Message from lead:\nSame text.'
    await $.turn.start({ text: 'work', turnId: 't1' })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${RETRY}>>${body}<</MV2>>` })
    const stored = await append($, { message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
      content: [{ type: 'text', text: wrapped(handed(STEER, body)) }] }, door: 'delivery', origin: { kind: 'peer' } })
    if (stored) {
      await w.clock.settle()
      expect(w.files.has(`/agent/claude-receipts/${STEER}.json`)).toBe(true)
      expect(w.files.has(`/agent/claude-receipts/${RETRY}.json`)).toBe(false)
    }
    // The same on the own-turn route: the old message's peer prompt is the old message, and RETRY still waits.
    const older = STEER.slice(0, -1) + '0'
    await $.turn.complete(turnComplete('t1'))
    expect(await $.prompt.submit({ text: handed(older, body), origin: { kind: 'peer' }, wait: false } as never))
      .toEqual({ drop: expect.any(String) })
    await w.clock.settle()
    expect(w.submitted.at(-1)).toEqual({ text: body, asUser: true })  // submitted clean, as every prompt is
    expect(await $.prompt.submit({ text: body, origin: { kind: 'peer' }, wait: false } as never)).toEqual({ drop: expect.any(String) })
    await w.clock.settle()
    expect(w.submitted.length).toBe(2)
    expect(w.submitted.at(-1)).toEqual({ text: body, asUser: true })
  })

  test('a tag quoted in a clean message is never taken for an older mod\'s envelope, with nothing waiting', async ($, on) => {
    // After a reload nothing is waiting, and an older mod's tag around a whole message is the only way left to name one.
    // A tag the message itself quotes has text before it (its "Message from" line), so it names nothing: no receipt,
    // and the message reaches the model whole, on every path (lifecycle's review of 35b1741).
    const w = world(on)
    const receipts = () => [...w.files.keys()].filter((path) => path.includes('claude-receipts'))
    const body = `Message from lead:\nPlease explain the literal tag <<MV2 id=${ASK}>>hello<</MV2>> in our protocol.`
    const prompt = await append($, { message: { type: 'user', role: 'user', content: [{ type: 'text', text: body }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true } })
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: body }])
    await append($, { message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
      content: [{ type: 'text', text: wrapped(body) }] }, door: 'delivery', origin: { kind: 'peer' } })
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: wrapped(body) }])
    const attached = await $.prompt.attachment({ type: 'queued_command', text: wrapped(body), origin: { kind: 'engine' } })
    expect(String((attached as { text?: string })?.text ?? wrapped(body))).toContain('Please explain the literal tag')
    expect(await $.prompt.submit({ text: body, origin: { kind: 'peer' }, wait: false } as never)).not.toEqual({ drop: expect.any(String) })
    // Ending with the quoted tag, so only the text before it says it isn't an envelope.
    const last = `Message from lead:\nIt looks like <<MV2 id=${ASK}>>hello<</MV2>>`
    await append($, { message: { type: 'user', role: 'user', content: [{ type: 'text', text: last }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true } })
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: last }])
    // Starting with a tag but going on past it (another session's message, say), so only the text after it tells.
    const more = `<<MV2 id=${ASK}>>hello<</MV2>> and then more`
    await append($, { message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
      content: [{ type: 'text', text: wrapped(more) }] }, door: 'delivery', origin: { kind: 'peer' } })
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: wrapped(more) }])
    if (prompt) {
      await w.clock.settle()
      expect(receipts()).toEqual([])
    }
    // An older mod's envelope still names its message there: the attachment's model copy loses it.
    expect(await $.prompt.attachment({ type: 'queued_command', text: wrapped(handed(STEER, 'Old.')), origin: { kind: 'engine' } }))
      .toEqual({ text: 'Old.' })
  })

  test('a message that itself mentions the tags keeps them: only the outer envelope comes off', async ($, on) => {
    // A body discussing this protocol must survive whole: a literal <</MV2>>, and even a whole valid tag with a real
    // ID inside it, is the message's own text. A prompt the mod submits travels tagged; a queued steer goes clean, and
    // its row must still be the steer's, not the inner example's (lifecycle's review of the clean queued route).
    const w = world(on)
    const receipts = () => [...w.files.keys()].filter((path) => path.includes('claude-receipts'))
    const body = `Message from lead:\nThe closing marker is \`<</MV2>>\`, as in \`<<MV2 id=${ASK}>>hi<</MV2>>\`.`
    const prompted = (content: { type: 'text'; text: string }[]) => append($, { message: { type: 'user', role: 'user', content },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true } })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>${body}<</MV2>>` })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${RETRY}>>${body}<</MV2>>` })
    await w.clock.settle()
    expect(w.submitted.map((submitted) => submitted.text)).toEqual([body, body])
    if (!(await prompted([{ type: 'text', text: body }]))) return
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: body }])
    await w.clock.settle()
    expect(receipts()).toContain(`/agent/claude-receipts/${FOLLOW_UP}.json`)
    expect(receipts()).not.toContain(`/agent/claude-receipts/${ASK}.json`)
    // A clean message that ends with a literal closing tag keeps it: only an older mod's envelope comes off.
    const ending = 'Message from lead:\nIt ends with <</MV2>>'
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP.slice(0, -1)}5>>${ending}<</MV2>>` })
    await w.clock.settle()
    await prompted([{ type: 'text', text: ending }])
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: ending }])
    // An older mod's tagged prompt row, split across blocks: the opening at the start of the first and the closing at
    // the end of the last come off, and the quoted tag stays.
    const [head = '', tail = ''] = handed(RETRY, body).split('\nThe')
    await prompted([{ type: 'text', text: head }, { type: 'text', text: `The${tail}` }])
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: 'Message from lead:' }, { type: 'text', text: `The${body.split('\nThe')[1]}` }])
    // A mid-turn steer's peer row keeps it too.
    await $.turn.start({ text: 'work', turnId: 't1' })
    const queuedText = (await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>${body}<</MV2>>` }) as { text: string }).text
    expect(queuedText).toBe(body)
    await append($, { message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
      content: [{ type: 'text', text: wrapped(queuedText) }] }, door: 'delivery', origin: { kind: 'peer' } })
    expect(w.appended.at(-1)).toEqual([{ type: 'text', text: `<system-reminder>\n${body}\n</system-reminder>` }])
    await w.clock.settle()
    expect(receipts()).toContain(`/agent/claude-receipts/${STEER}.json`)
    expect(receipts()).not.toContain(`/agent/claude-receipts/${ASK}.json`)
    // So does a steer no tool result took, which Claude runs as its own peer prompt: it is submitted whole.
    const own = STEER.slice(0, -1) + '9'
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${own}>>${body}<</MV2>>` })
    await $.turn.complete(turnComplete('t1'))
    expect(await $.prompt.submit({ text: body, origin: { kind: 'peer' }, wait: false } as never)).toEqual({ drop: expect.any(String) })
    await w.clock.settle()
    expect(w.submitted.at(-1)).toEqual({ text: body, asUser: true })
  })

  test('a row is credited by its own tag alone, even after a reload forgot what was waiting', async ($, on) => {
    // A reload empties what the mod remembers, which is the normal model here. An older mod's tagged row is credited by
    // its tag; a prompt row is never a queued steer's by its text (RETRY here took the queued route, as nothing has
    // shown the session idle yet).
    const w = world(on)
    const receipt = (id: string) => JSON.parse(w.files.get(`/agent/claude-receipts/${id}.json`) ?? 'null')
    const prompted = (text: string) => append($, { message: { type: 'user', role: 'user', content: [{ type: 'text', text }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true } })
    // This instance never saw STEER: the mod before the reload handed it to Claude. Now its retry arrives.
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${RETRY}>>Hello.<</MV2>>` })
    await w.clock.settle()
    if (!(await prompted(handed(STEER, 'Hello.')))) return
    await w.clock.settle()
    expect([receipt(STEER)?.route, receipt(RETRY)]).toEqual(['prompt', null])
    await prompted('Hello.')
    await w.clock.settle()
    expect(receipt(RETRY)).toBe(null)
    await prompted(handed(RETRY, 'Hello.'))
    await w.clock.settle()
    expect(receipt(RETRY)?.route).toBe('prompt')
  })

  test('a refusal whose write finishes after the row was stored never hides the delivery', async ($, on) => {
    // The two writes for one attempt can finish in either order, and a reload can't serialize them; the delivery
    // receipt is a file of its own, which the adapter lets win (lifecycle's review of CLAUDE-15).
    let release = () => {}
    const w = world(on, { holdRefusals: new Promise<void>((resolve) => { release = resolve }) })
    await $.turn.complete(turnComplete('t0'))  // an observed idle moment
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Hello.<</MV2>>` })
    await w.clock.settle()
    await w.clock.advance(5000)  // the refusal starts; its write is held
    const stored = await append($, { message: { type: 'user', role: 'user', content: [{ type: 'text', text: handed(STEER, 'Hello.') }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true } })
    await w.clock.settle()
    release()
    await w.clock.settle()
    if (!stored) return
    expect(JSON.parse(w.files.get(`/agent/claude-receipts/${STEER}.json`) ?? 'null')).toEqual({ id: STEER, route: 'prompt', at: expect.any(Number) })
    expect(w.files.has(`/agent/claude-receipts/${STEER}.not-delivered.json`)).toBe(true)
  })

  test('after a load the turn state is unknown: no message is timed out before an observed idle moment', async ($, on) => {
    // A reload can come mid-turn: a follow-up taken then waits behind the running turn, and refusing it after five
    // seconds would invite a duplicate retry. A fresh process is the same case (reviewer, 3ccc675).
    const w = world(on)
    const refusal = (id: string) => JSON.parse(w.files.get(`/agent/claude-receipts/${id}.not-delivered.json`) ?? 'null')
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    // A steer takes the queued way, right whether a turn is running or not; a follow-up is submitted, to run after.
    expect(await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Rebase first.<</MV2>>` }))
      .toEqual({ text: 'Rebase first.' })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Then test.<</MV2>>` })
    await w.clock.settle()
    await w.clock.advance(60_000)
    expect([refusal(STEER), refusal(FOLLOW_UP)]).toEqual([null, null])
    // The turn that was running ends: the session is idle now, and the clock starts.
    await $.turn.complete(turnComplete('t0'))
    await w.clock.settle()
    await w.clock.advance(4000)
    expect(refusal(FOLLOW_UP)).toBe(null)
    await w.clock.advance(1000)
    expect(refusal(FOLLOW_UP)).toEqual(expect.objectContaining({ delivered: false, definite: false }))
  })

  test("a turn that ends without the message's row leaves it unconfirmed, never definitely refused", async ($, on) => {
    // Claude ran something else first. Ours may still be queued behind it, and is stored seconds later, so the end
    // of that turn can't prove it was dropped: a definite refusal would invite a duplicate retry.
    const w = world(on)
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Hello.<</MV2>>` })
    await w.clock.settle()
    await $.turn.start({ text: 'a task notification', turnId: 't1' })
    await $.turn.complete(turnComplete('t1'))
    await w.clock.settle()
    expect(JSON.parse(w.files.get(`/agent/claude-receipts/${STEER}.not-delivered.json`) ?? 'null'))
      .toEqual(expect.objectContaining({ delivered: false, definite: false }))
  })

  test('untagged peer messages keep Claude’s handling', async ($, on) => {
    const w = world(on)
    const text = 'A message from another of your sessions.'
    expect(await $.session.receive({ origin: { kind: 'peer' }, text })).toEqual({ text })
    await $.turn.start({ text: 'work', turnId: 't1' })
    await append($, {
      message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
                 content: [{ type: 'text', text: wrapped(text) }] },
      door: 'delivery', origin: { kind: 'peer' }, uuid: 'row-4' })
    expect(w.appended).toEqual([[{ type: 'text', text: wrapped(text) }]])
    await w.clock.settle()
    expect(w.submitted).toEqual([])
    expect([...w.files.keys()].filter((path) => path.includes('receipts'))).toEqual([])
  })

  test('older history keeps working: the first version’s tags are stripped from queued commands', async ($, on) => {
    world(on)
    const answer = await $.prompt.attachment({ type: 'queued_command', text: wrapped('<<MV2>>Old.<</MV2>>'),
                                               origin: { kind: 'engine' } })
    expect(answer).toEqual({ text: 'Old.' })
  })
})

describe('ask', () => {
  test('a question is answered by a fork and never enters the conversation', async ($, on) => {
    const w = world(on)
    const prompts: string[] = []
    on('model.fork', async ($, e) => {
      prompts.push(e.prompt)
      return { value: { isAnswered: true, text: 'About an hour.', usage: { input_tokens: 3, output_tokens: 4,
               cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } } }
    })
    const received = await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 ask id=${ASK}>>How long?<</MV2>>` })
    expect(received).toEqual({ consumed: expect.any(String) })
    await w.clock.settle()
    expect(prompts).toEqual(['How long?'])
    expect(JSON.parse(w.files.get(`/agent/claude-asks/${ASK}.json`) ?? '{}'))
      .toEqual(expect.objectContaining({ id: ASK, answered: true, text: 'About an hour.' }))
    expect(w.submitted).toEqual([])
  })

  test('nothing to fork yet is reported, not answered', async ($, on) => {
    const w = world(on)
    on('model.fork', async () => ({ value: { isAnswered: false, reason: 'nothing-to-fork' } }))
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 ask id=${ASK}>>Anything?<</MV2>>` })
    await w.clock.settle()
    expect(JSON.parse(w.files.get(`/agent/claude-asks/${ASK}.json`) ?? '{}'))
      .toEqual(expect.objectContaining({ answered: false, reason: 'nothing-to-fork' }))
  })
})
