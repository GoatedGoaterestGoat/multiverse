// Offline tests of the multiverse mod: `claude plugin test src/multiverse/harnesses/claude/mod`.
// The test's hooks sit beneath the mod and stand in for Claude's core and the
// host: the files it writes, the status reports it runs, the prompts it submits
// and the forks it asks for. The kit has no core that stores a session.append
// row, so tests check the row the mod hands down to be stored (what the model
// and the transcript read); the receipt written once a row is stored is covered
// by the Python tests and live runs.
import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

const STEER = '3f2a0c1e-1111-4222-8333-944455556661'
const FOLLOW_UP = '3f2a0c1e-1111-4222-8333-944455556662'
const ASK = '3f2a0c1e-1111-4222-8333-944455556663'
const TRAILER = 'This came from another Claude session — not typed by your user.'

type Submitted = { text: string; asUser: boolean }

function world(on: On) {
  mock.env(on, { MV2_PYTHON: '/python', MV2_AGENT_DIR: '/agent' })
  const clock = mock.clock(on)
  // Claude's core for the engine events the tests raise: each passes through.
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => ({ text: e.answer }))
  on('session.receive', async ($, e) => ({ text: e.text }))
  // The row the mod hands down to be stored.
  const appended: unknown[] = []
  on('session.append', async ($, e, next) => { appended.push(e.message.content); return next(e) })
  on('classic.PermissionRequest', async () => ({}))
  on('prompt.attachment', async ($, e) => ({ text: e.text }))
  on('ui.log', async () => ({ value: undefined }))
  // The host: files the mod writes and the status reports it runs.
  const files = new Map<string, string>()
  on('fs.write', async ($, e) => { files.set(e.path, e.text); return { value: undefined } })
  const statuses: string[] = []
  on('process.run', async ($, e) => {
    const index = e.argv.indexOf('--status')
    if (index >= 0) statuses.push(e.argv[index + 1] ?? '')
    return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  const submitted: Submitted[] = []
  on('prompt.submit', async ($, e) => {
    submitted.push({ text: e.text, asUser: e.origin.kind === 'plugin' && e.origin.asUser === true })
    return { text: e.text, origin: e.origin }
  })
  return { clock, files, statuses, submitted, appended }
}

function wrapped(text: string, midTurn = true) {
  const header = midTurn ? 'Another Claude session sent a message while you were working:' : 'Another Claude session sent a message:'
  return `<system-reminder>\n${header}\n${text}\n\n${TRAILER}\n</system-reminder>`
}

const turnComplete = (turnId: string) =>
  ({ turnId, answer: '', durationMs: 1, isAborted: false, reason: 'answer' as const })

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
    await expect($.session.append({
      message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
                 content: [{ type: 'text', text: wrapped(text) }] },
      door: 'delivery', origin: { kind: 'peer' }, uuid: 'row-1' } as never)).rejects.toThrow('no implementation')
    expect(w.appended).toEqual([[{ type: 'text', text: `<system-reminder>\n${text}\n</system-reminder>` }]])
    expect(w.submitted).toEqual([])
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
    await expect($.session.append({
      message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'Hello.' }] },
      door: 'prompt', origin: { kind: 'plugin', name: 'multiverse', asUser: true }, uuid: 'row-2' } as never))
      .rejects.toThrow('no implementation')
    expect(w.appended).toEqual([[{ type: 'text', text: 'Hello.' }]])
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
    await expect($.session.append({
      message: { type: 'attachment', name: 'queued_command', role: 'user', isMeta: true,
                 content: [{ type: 'text', text: wrapped(text) }] },
      door: 'delivery', origin: { kind: 'peer' }, uuid: 'row-4' } as never)).rejects.toThrow('no implementation')
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
