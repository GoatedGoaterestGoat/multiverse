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

function world(on: On, options: { sessionId?: string; moveExit?: number; file?: () => string | undefined; now?: number } = {}) {
  mock.env(on, { MV2_PYTHON: '/python', MV2_AGENT_DIR: '/agent' })
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
  on('fs.write', async ($, e) => { files.set(e.path, e.text); return { value: undefined } })
  const statuses: string[] = []
  const moves: string[][] = []
  const reads: (string | undefined)[] = []
  on('process.run', async ($, e) => {
    const index = e.argv.indexOf('--status')
    if (index >= 0) statuses.push(e.argv[index + 1] ?? '')
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
    return { text: e.text, origin: e.origin }
  })
  return { clock, files, statuses, submitted, appended, moves, logs, reads }
}

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

describe('status', () => {
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
    const idle = await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Hello.<</MV2>>` })
    expect(idle).toEqual({ consumed: expect.any(String) })
    await $.turn.start({ text: 'Hello.', turnId: 't1' })
    // A follow-up is taken even mid-turn; Claude runs it once the turn ends.
    const later = await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW_UP}>>Then test.<</MV2>>` })
    expect(later).toEqual({ consumed: expect.any(String) })
    await w.clock.settle()
    expect(w.submitted).toEqual([{ text: 'Hello.', asUser: true }, { text: 'Then test.', asUser: true }])
    // Their rows are stored as submitted: bare text, no tags, no wrapper.
    const stored = await append($, {
      message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Hello.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true }, uuid: 'row-2' })
    expect(w.appended).toEqual([[{ type: 'text', text: 'Hello.' }]])
    if (stored) {
      await w.clock.settle()
      expect([...w.files.keys()].filter((path) => path.includes('claude-receipts'))).toEqual([`/agent/claude-receipts/${STEER}.json`])
    }
  })

  test('identical messages each get their own receipt, oldest first', async ($, on) => {
    // Two follow-ups or steers with the same text are stored twice; each stored copy confirms the oldest one still
    // waiting, so neither is left "accepted, no confirmation" while the agent is live (CLAUDE-14).
    const w = world(on)
    const second = (id: string) => id.slice(0, -1) + (id === STEER ? '7' : '8')
    const receipts = () => [...w.files.keys()].filter((path) => path.includes('claude-receipts'))
    await $.turn.start({ text: 'work', turnId: 't1' })
    for (const id of [FOLLOW_UP, second(FOLLOW_UP)]) {
      await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${id}>>Please continue.<</MV2>>` })
    }
    for (const id of [STEER, second(STEER)]) {
      await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${id}>>Rebase first.<</MV2>>` })
    }
    await w.clock.settle()
    const steered = async () => append($, { message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
      content: [{ type: 'text', text: wrapped('Rebase first.') }] }, door: 'delivery', origin: { kind: 'peer' } })
    const prompted = async () => append($, { message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Please continue.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true } })
    const stored = await steered()
    if (!stored) return  // a kit with no core to store rows writes no receipts
    await w.clock.settle()
    expect(receipts()).toEqual([`/agent/claude-receipts/${STEER}.json`])
    await steered()
    await prompted()
    await prompted()
    await w.clock.settle()
    expect(receipts()).toEqual([STEER, second(STEER), FOLLOW_UP, second(FOLLOW_UP)].map((id) => `/agent/claude-receipts/${id}.json`))
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
                 content: [{ type: 'text', text: wrapped(steer) }] },
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
      await $.session.receive({ origin: { kind: 'peer' },
        text: `<<MV2 follow-up id=${id} sent=${new Date(now - age).toISOString()}>>${body}<</MV2>>` })
      await w.clock.settle()
      await w.clock.advance(wait)
      await append($, {
        message: { type: 'user', role: 'user', content: [{ type: 'text', text: body }] },
        door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true }, uuid: `age-${index}` })
      expect([name, w.appended.at(-1)]).toEqual([name, [{ type: 'text', text: `${shown}\nfollow-up ${index}` }]])
    }
    // A send time without its zone gives no age, as in core: JavaScript would read it as local time.
    expect(agePhrase('2026-10-20T10:00:00', now)).toBeUndefined()  // days ago in any zone, so only the rule refuses it
    expect(agePhrase('2026-10-23T10:00:00+00:00', now)).toBe('sent 2 hours ago')
  })

  test('a steer no tool result took is dropped as a peer prompt and submitted instead', async ($, on) => {
    const w = world(on)
    await $.turn.start({ text: 'work', turnId: 't1' })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Late.<</MV2>>` })
    await $.turn.complete(turnComplete('t1'))
    const own = await $.prompt.submit({ text: 'Late.', origin: { kind: 'peer' }, wait: false } as never)
    expect(own).toEqual({ drop: expect.any(String) })
    await w.clock.settle()
    expect(w.submitted).toEqual([{ text: 'Late.', asUser: true }])
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
