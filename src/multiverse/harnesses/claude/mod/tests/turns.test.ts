// Offline tests of the turn events the mod reports (hooks/turns.ts, with index.ts's reports): `claude plugin test`.
// The test's hooks stand in for Claude's core and the host: the status reports, with the turn events each carries,
// and the transcript file, as the row ids the mod finds at its end (the test decides when a row lands there).
import type { On } from 'claude-code'
import { describe, expect, mock, test } from './kit'

const STEER = '3f2a0c1e-1111-4222-8333-944455556661'
const RUN = '7a1b2c3d-1111-4222-8333-944455556671'  // an 'mv2 slash' run: its slash record's id
type Report = { status: string; turns: Record<string, unknown>[]; at: number; session: string | null }

// `slow`: each read of the transcript file takes all the time it is allowed. `heldInfo`: Claude never answers
// $.session.info().
// Each test's plugin is a fresh copy, but the one before it can still be finishing a report as the next test starts,
// and that call reaches the next test's hooks. Each world names its own Python, so it counts only its own copy's.
let worlds = 0

function world(on: On, env: Record<string, string> = {}, slow = false, heldInfo = false) {
  const python = `/python-${++worlds}`
  mock.env(on, { MV2_PYTHON: python, MV2_AGENT_DIR: '/agent', MV2_LAUNCH_ID: 'launch-1', ...env })
  const clock = mock.clock(on, { now: 0 })
  // Claude's own part of session.start and turn.complete; `heldHooks`, when set, holds it (the mod's hook resumes later).
  const heldHooks: { start?: Promise<void>; complete?: Promise<void> } = {}
  on('session.start', async ($, e) => { if (heldHooks.start !== undefined) await heldHooks.start; return { cwd: e.cwd } })
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => { if (heldHooks.complete !== undefined) await heldHooks.complete; return { text: e.answer } })
  on('session.receive', async ($, e) => ({ text: e.text }))
  // Claude's process file, naming its session: after a /clear, a /resume or a fork it names the new one, as Claude's
  // does, unless the test sets `processFile.id` itself (`follows: false`).
  const processFile: { id?: string; follows: boolean } = { follows: true }
  on('classic.SessionEnd', async () => ({}))
  on('classic.SessionStart', async ($, e) => {
    const start = e as unknown as { source?: string; session_id?: string }
    if (['clear', 'resume', 'fork'].includes(start.source ?? '') && processFile.follows) processFile.id = start.session_id
    return {}
  })
  on('session.compact', async () => ({ skip: 'test' }) as never)
  // A row's append the test holds after the row is stored (on disk, its callback not yet returned): hold(uuid).
  const holds = new Map<string, { entered: () => void; until: Promise<void> }>()
  on('session.append', async ($, e, next) => {
    const result = await next(e)
    const held = holds.get(e.uuid)
    if (held !== undefined) { held.entered(); await held.until }
    return result
  })
  on('prompt.submit', async ($, e) => ({ text: e.text, origin: e.origin }))
  on('fs.write', async () => ({ value: undefined }))
  on('fs.read', async () => { throw new Error('no such file') })
  on('fs.list', async () => ({ value: [] }) as never)
  on('fs.stat', async () => { throw new Error('no such file') })
  on('ui.log', async () => ({ value: undefined }))
  // Claude's answer to a session id lookup; `idHeld.until`, when set, holds it.
  const idHeld: { until?: Promise<void> } = {}
  on('session.id', async () => { if (idHeld.until !== undefined) await idHeld.until; return { value: 's1' } })
  // `info.transcriptPath`: what Claude answers for the transcript's path (undefined: it gives none).
  const info: { transcriptPath?: string } = { transcriptPath: '/home/.claude/projects/p/s1.jsonl' }
  on('session.info', async () => {
    if (heldInfo) await new Promise<void>(() => {})
    return { value: { ...info } } as never
  })
  // The commands Claude lists and runs, for a command mv2 asks the mod to run.
  on('command.list', async () => ({ value: [{ name: 'context', description: 'Context usage', source: 'builtin' }] }) as never)
  on('command.run', async () => ({ text: '' }) as never)
  on('session.messages', async () => ({ value: [] }) as never)
  on('session.model', async () => ({ value: 'claude-haiku-5-5' }) as never)
  on('settings.read', async () => ({ value: {} }) as never)
  const disk: (string | Record<string, unknown>)[] = []  // the whole records in the transcript file, in order (an id stands for a plain row)
  // Another conversation's file, by path, where a test sets one (any other path reads `disk`); `paths`: each path read.
  const files = new Map<string, (string | Record<string, unknown>)[]>()
  const paths: string[] = []
  const after = { text: '' }  // anything after them: a record still being written, say
  const reports: Report[] = []
  const reads: number[] = []  // each read's allowed time
  // A slow read returns once the test has moved the clock past the time it was allowed (release()).
  const held: { until: number; wake: () => void }[] = []
  const release = () => { for (const read of held.splice(0)) if (read.until <= clock.now()) read.wake(); else held.push(read) }
  const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  // The move mv2 records after a /clear; `hanging.until`, when set: core answers no status report until it settles.
  const moves: { session: string; previous: string; reason: string; after: number; at: number }[] = []  // `after`: how many reports went before it
  const hanging: { until?: Promise<void> } = {}
  on('process.run', async ($, e) => {
    if (e.argv[0] === 'sh' && String(e.argv[2] ?? '').includes('/sessions/')) return ok(processFile.id === undefined ? '' : JSON.stringify({ sessionId: processFile.id }))
    if (e.argv[0] === python && e.argv.includes('--native-session-id')) {
      moves.push({ session: e.argv[e.argv.indexOf('--native-session-id') + 1] ?? '', after: reports.length, at: clock.now(),
        previous: e.argv[e.argv.indexOf('--previous-session-id') + 1] ?? '', reason: e.argv[e.argv.indexOf('--session-reason') + 1] ?? '' })
      return ok()
    }
    const at = e.argv[0] === python ? e.argv.indexOf('--status') : -1
    if (at >= 0 && hanging.until !== undefined) await hanging.until
    if (at >= 0) {
      const flag = e.argv.indexOf('--turn-events-json')
      const session = e.argv.indexOf('--turn-session-id')
      reports.push({ status: e.argv[at + 1] ?? '', turns: flag < 0 ? [] : JSON.parse(e.argv[flag + 1] ?? '[]'), at: clock.now(),
        session: session < 0 ? null : e.argv[session + 1] ?? null })
      return ok()
    }
    if (e.argv[0] === 'sh' && e.argv[3] === 'rows') {
      const allowed = (e as { init?: { timeoutMs?: number } }).init?.timeoutMs ?? 0
      reads.push(allowed)
      paths.push(e.argv[4] ?? '')
      if (slow) await new Promise<void>((wake) => held.push({ until: clock.now() + allowed, wake }))
      const entries = files.get(e.argv[4] ?? '') ?? disk
      return ok(entries.map((entry) => JSON.stringify(typeof entry === 'string' ? { type: 'user', uuid: entry } : entry) + '\n').join('') + after.text)
    }
    return ok()
  })
  const turns = () => reports.flatMap((r) => r.turns)
  const hold = (uuid: string) => {
    let entered = () => {}
    let release = () => {}
    const reached = new Promise<void>((resolve) => { entered = resolve })
    holds.set(uuid, { entered: () => entered(), until: new Promise<void>((resolve) => { release = resolve }) })
    return { reached, release: () => release() }
  }
  return { clock, disk, files, paths, after, reports, reads, turns, release, hold, idHeld, heldHooks, info, processFile, moves, hanging }
}

type Raise = { session: { append: (input: never) => Promise<unknown> } }
const row = ($: Raise, uuid: string, door: string, origin: Record<string, unknown>, message: Record<string, unknown>) =>
  $.session.append({ uuid, door, origin, message: { role: 'user', content: [{ type: 'text', text: 'x' }], ...message } } as never)
const prompt = ($: Raise, uuid: string, origin: Record<string, unknown> = { kind: 'composer' }) => row($, uuid, 'prompt', origin, { type: 'user' })
const answer = ($: Raise, uuid: string) => row($, uuid, 'response', { kind: 'model' }, { type: 'assistant', role: 'assistant' })
const done = ($: Raise, uuid: string) => row($, uuid, 'notice', { kind: 'engine' }, { type: 'system', name: 'turn_duration' })
const complete = (turnId: string, reason: 'answer' | 'aborted' = 'answer') =>
  ({ turnId, answer: '', durationMs: 1, isAborted: reason === 'aborted', reason })
const P1 = 'a1111111-1111-4111-8111-111111111111'
const R1 = 'a2222222-2222-4222-8222-222222222222'
const D1 = 'a3333333-3333-4333-8333-333333333333'

describe('turn events', () => {
  test("a turn's start goes with the working report, and its end only once its last row is on disk, with the idle report", async ($, on) => {
    // Claude ends a turn about 0.1-0.2 s before its rows reach the transcript file: an end sent at once would have a
    // plugin read a turn without its answer, and an idle sent before the end breaks 'the end comes first'.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    await done($, D1)
    await $.turn.complete(complete('t1') as never)
    for (let i = 0; i < 4; i++) await w.clock.advance(50)
    expect(w.reports.some((r) => r.status === 'idle')).toBe(false)
    w.disk.push(P1, R1, D1)
    for (let i = 0; i < 4 && !w.reports.some((r) => r.status === 'idle'); i++) await w.clock.advance(50)
    const started = w.reports.findIndex((r) => r.turns.some((t) => t.type === 'started'))
    expect(w.reports[started]?.turns[0]).toEqual({ type: 'started', turn: P1, kind: 'prompt', ref: P1, source: 'person', first_task: false })
    expect(w.reports.slice(0, started).some((r) => r.status === 'idle')).toBe(false)
    const idle = w.reports.findIndex((r) => r.status === 'idle')
    const ended = w.reports.findIndex((r) => r.turns.some((t) => t.type === 'ended'))
    expect([ended >= 0, ended <= idle]).toEqual([true, true])
    expect(w.reports[ended]?.turns).toEqual([{ type: 'ended', turn: P1, kind: 'prompt', ref: D1, state: 'finished' }])
  })

  test('a prompt that joins a turn names it, with the mv2 delivery it was matched to', async ($, on) => {
    // The join's attempt is how core tells a plugin which mv2 message it was, without anything in the row the agent
    // reads; a typed one carries none.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await row($, 'b1111111-1111-4111-8111-111111111111', 'delivery', { kind: 'composer' }, { type: 'attachment', name: 'queued_command', isMeta: true })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Rebase first.<</MV2>>` })
    await row($, 'b2222222-2222-4222-8222-222222222222', 'delivery', { kind: 'peer' }, { type: 'attachment', name: 'queued_command', isMeta: true,
      content: [{ type: 'text', text: '<system-reminder>\nAnother Claude session sent a message while you were working:\nRebase first.\n</system-reminder>' }] })
    await w.clock.settle()
    expect(w.turns().filter((t) => t.type === 'joined')).toEqual([
      { type: 'joined', turn: P1, ref: 'b1111111-1111-4111-8111-111111111111', source: 'person' },
      { type: 'joined', turn: P1, ref: 'b2222222-2222-4222-8222-222222222222', source: 'harness', attempt: STEER }])
  })

  test("an Esc ends a turn interrupted; a turn Claude continues ends again, and a lost row's end names what the file holds", async ($, on) => {
    // A plugin must not read Claude's '[Request interrupted by user' text; a continuation has no new prompt row, so a
    // second start would name a turn the transcript doesn't have; and past the wait the end must still go.
    const w = world(on)
    w.disk.push(P1, R1, D1)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    await $.turn.complete(complete('t1', 'aborted') as never)
    await w.clock.advance(100)
    await $.turn.start({ text: '', turnId: 't2' })
    await done($, D1)
    await $.turn.complete(complete('t2') as never)
    await w.clock.advance(100)
    const lost = 'a4444444-4444-4444-8444-444444444444'
    await $.turn.start({ text: '', turnId: 't3' })
    await done($, lost)
    await $.turn.complete(complete('t3') as never)
    await w.clock.advance(2100)
    await w.clock.settle()
    expect(w.turns().map((t) => [t.type, t.turn, t.ref, t.state])).toEqual([
      ['started', P1, P1, undefined], ['ended', P1, R1, 'interrupted'], ['ended', P1, D1, 'finished'], ['ended', P1, D1, 'finished']])
    expect(w.reports.at(-1)?.status).toBe('idle')
  })

  test("a local command is a turn of its own, from its row to its output, and one mv2 ran names its run", async ($, on) => {
    // Claude raises no turn.start or turn.complete for a local command: without these, the transcript's command turn
    // would have no events at all. Its end, like a turn's, goes only once its output row is in the file. Claude stores
    // mv2's run under the plugin's name, which isn't who asked: the run's id is how core finds the slash record.
    const w = world(on)
    const command = (uuid: string, text: string, origin: Record<string, unknown>) =>
      row($, uuid, 'command', origin, { type: 'system', name: 'local_command', content: [{ type: 'text', text }] })
    await command('c1111111-1111-4111-8111-111111111111', '<command-name>/context</command-name>', { kind: 'engine' })
    await command('c2222222-2222-4222-8222-222222222222', '<local-command-stdout>Context usage</local-command-stdout>', { kind: 'engine' })
    w.disk.push('c1111111-1111-4111-8111-111111111111')
    await w.clock.advance(200)
    expect(w.turns().some((t) => t.type === 'ended')).toBe(false)
    w.disk.push('c2222222-2222-4222-8222-222222222222')
    await w.clock.advance(200)
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 command id=${RUN}>>${JSON.stringify({ command: 'context', args: '' })}<</MV2>>` })
    await w.clock.settle()
    await command('c3333333-3333-4333-8333-333333333333', '<command-name>/context</command-name>', { kind: 'plugin', name: 'multiverse' })
    await w.clock.settle()
    expect(w.turns()).toEqual([
      { type: 'started', turn: 'c1111111-1111-4111-8111-111111111111', kind: 'command', ref: 'c1111111-1111-4111-8111-111111111111', source: 'person', first_task: false },
      { type: 'ended', turn: 'c1111111-1111-4111-8111-111111111111', kind: 'command', ref: 'c2222222-2222-4222-8222-222222222222', state: 'finished' },
      { type: 'started', turn: 'c3333333-3333-4333-8333-333333333333', kind: 'command', ref: 'c3333333-3333-4333-8333-333333333333', source: 'person', attempt: RUN, first_task: false }])
  })

  test("a task launch marks the first prompt the person's side stores as the task, and only that one", async ($, on) => {
    // The task is stored exactly like a typed prompt: only core's word that this launch was given one tells them
    // apart, and a later prompt marked as the task would make a plugin act on the wrong turn as the agent's task.
    const w = world(on, { MV2_FIRST_TASK: '1' })
    await $.session.start({ cwd: '/w' } as never)
    await prompt($, P1)
    await $.turn.start({ text: 'task', turnId: 't1' })
    await prompt($, 'a5555555-5555-4555-8555-555555555555')
    await $.turn.start({ text: 'next', turnId: 't2' })
    await w.clock.settle()
    expect(w.turns().filter((t) => t.type === 'started').map((t) => t.first_task)).toEqual([true, false])
  })

  test("a turn's end waits at most its bound by the clock, even when each read of the file is slow", async ($, on) => {
    // Each read is a process that can take long: a bound that counted only the pauses kept an agent 'working' for
    // over a minute after its turn. And a row an earlier read proved stays the end's row when a later one can't.
    const w = world(on, {}, true)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    await done($, D1)
    w.disk.push(P1, R1)
    const begun = w.clock.now()
    await $.turn.complete(complete('t1') as never)
    for (let i = 0; i < 40 && !w.reports.some((r) => r.status === 'idle'); i++) {
      await w.clock.advance(100)
      w.release()
      await w.clock.settle()
    }
    const idle = w.reports.find((r) => r.status === 'idle')
    expect(idle?.turns).toEqual([{ type: 'ended', turn: P1, kind: 'prompt', ref: R1, state: 'finished' }])
    expect((idle?.at ?? Infinity) - begun).toBeLessThanOrEqual(2100)
    expect(w.reads.reduce((sum, allowed) => sum + allowed, 0)).toBeLessThanOrEqual(2000)
  })


  test("a turn's end goes on time even when Claude never says where its transcript is", async ($, on) => {
    // A lookup that never answered kept the agent 'working' for good: the bound must start before anything is asked.
    const w = world(on, {}, false, true)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await done($, D1)
    const begun = w.clock.now()
    await $.turn.complete(complete('t1') as never)
    for (let i = 0; i < 30 && !w.reports.some((r) => r.status === 'idle'); i++) await w.clock.advance(100)
    const idle = w.reports.find((r) => r.status === 'idle')
    expect(idle?.turns).toEqual([{ type: 'ended', turn: P1, kind: 'prompt', ref: null, state: 'finished' }])
    expect((idle?.at ?? Infinity) - begun).toBeLessThanOrEqual(2100)
    expect(w.reports.filter((r) => r.turns.some((t) => t.type === 'ended')).length).toBe(1)
  })

  test("only a whole record's own id proves a row is in the file: never a torn last line or an id inside another row", async ($, on) => {
    // A plugin reads the turn on its end: an end sent while the last row was half written, or on the strength of a
    // tool result quoting its id, would have it read a turn without its answer.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    await done($, D1)
    w.disk.push(P1, R1)
    w.after.text = JSON.stringify({ type: 'user', message: { content: `see ${D1}` }, nested: { uuid: D1 } }) + '\n' + `{"type":"system","uuid":"${D1}"}`  // whole, but its newline not yet written
    const begun = w.clock.now()
    await $.turn.complete(complete('t1') as never)
    for (let i = 0; i < 30 && !w.reports.some((r) => r.status === 'idle'); i++) await w.clock.advance(100)
    const idle = w.reports.find((r) => r.status === 'idle')
    expect(idle?.turns).toEqual([{ type: 'ended', turn: P1, kind: 'prompt', ref: R1, state: 'finished' }])
    expect((idle?.at ?? 0) - begun).toBeGreaterThanOrEqual(2000)
  })


  test("a turn's end goes under the conversation it ran in, even after a /clear while it waited", async ($, on) => {
    // An end labelled with the new conversation would close a turn core never opened there, and leave the old one
    // open until the switch closes it as unknown.
    const w = world(on, {}, false, true)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await done($, D1)
    await $.turn.complete(complete('t1') as never)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    for (let i = 0; i < 30 && !w.reports.some((r) => r.turns.some((t) => t.type === 'ended')); i++) await w.clock.advance(100)
    const ended = w.reports.find((r) => r.turns.some((t) => t.type === 'ended'))
    // No file proves a row here: with its conversation moved on, the end names its last noted row, never none.
    expect([ended?.session, ended?.turns]).toEqual(['s1', [{ type: 'ended', turn: P1, kind: 'prompt', ref: D1, state: 'finished' }]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("an end still being proved keeps its place: what comes after it waits behind it", async ($, on) => {
    // Core closes a turn as unknown when the next one starts first, then gets its real end too: a local command
    // proving its output row, or a turn whose queued follow-up starts the next turn at once, must not let that happen.
    const w = world(on)
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    const C2 = 'c2222222-2222-4222-8222-222222222222'
    const command = (uuid: string, text: string) =>
      row($, uuid, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command', content: [{ type: 'text', text }] })
    await command(C1, '<command-name>/context</command-name>')
    await command(C2, '<local-command-stdout>Context usage</local-command-stdout>')
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    await w.clock.advance(100)
    expect(w.turns().map((t) => [t.type, t.turn])).toEqual([['started', C1]])
    expect(w.reports.at(-1)?.status).toBe('working')  // the status goes on
    w.disk.push(C1, C2, P1, R1)
    await w.clock.advance(100)
    await done($, D1)
    await $.turn.complete(complete('t1') as never)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)  // a queued follow-up: Claude starts it as soon as the turn ends
    await $.turn.start({ text: 'next', turnId: 't2' })
    await w.clock.advance(100)
    w.disk.push(D1)
    for (let i = 0; i < 10; i++) await w.clock.advance(100)
    expect(w.turns().map((t) => [t.type, t.turn])).toEqual([['started', C1], ['ended', C1], ['started', P1], ['ended', P1], ['started', P2]])
  })


  test("a proved turn end queued behind a command's end still holds 'working' until it can go", async ($, on) => {
    // An idle sent while the turn's end still waits in the queue tells a plugin the turn is over before it has the end.
    const w = world(on)
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    const C2 = 'c2222222-2222-4222-8222-222222222222'
    const command = (uuid: string, text: string) =>
      row($, uuid, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command', content: [{ type: 'text', text }] })
    await command(C1, '<command-name>/context</command-name>')
    await command(C2, '<local-command-stdout>Context usage</local-command-stdout>')
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await done($, D1)
    w.disk.push(C1, P1, D1)  // the turn's rows are there; the command's output row isn't yet
    await $.turn.complete(complete('t1') as never)
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    expect(w.reports.some((r) => r.status === 'idle' && w.reports.indexOf(r) > 0)).toBe(false)
    w.disk.push(C2)
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    const idle = w.reports.findLast((r) => r.status === 'idle')
    expect(idle?.turns.map((t) => [t.type, t.turn])).toEqual([['ended', C1], ['started', P1], ['ended', P1]])
  })


  test("a finished turn's end names its turn_duration row, even when that row is noted after turn.complete", async ($, on) => {
    // Claude stores turn_duration just before turn.complete; an end naming the row before it has a plugin reading up
    // to the end miss the row the transcript API puts last in the turn.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    w.disk.push(P1, R1, D1)
    const storing = done($, D1)  // still being stored when the turn completes
    await $.turn.complete(complete('t1') as never)
    await storing
    for (let i = 0; i < 10 && !w.reports.some((r) => r.status === 'idle'); i++) await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type === 'ended')).toEqual([{ type: 'ended', turn: P1, kind: 'prompt', ref: D1, state: 'finished' }])
  })


  test("a turn's end waits for a row that arrived while it ran and isn't noted yet, and that row stays its own", async ($, on) => {
    // The turn_duration row can be on disk while its append hasn't returned when the end is proved: proved early, the
    // end names the answer. And returning after the next turn has started, it must not join that turn.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    w.disk.push(P1, R1, D1)
    const held = w.hold(D1)
    const storing = done($, D1)
    await held.reached
    await $.turn.complete(complete('t1') as never)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const R2 = 'a6666666-6666-4666-8666-666666666666'
    await prompt($, P2)
    await $.turn.start({ text: 'next', turnId: 't2' })
    await answer($, R2)
    w.disk.push(P2, R2)
    await w.clock.advance(300)
    expect(w.turns().some((t) => t.type === 'ended')).toBe(false)  // its row still arriving
    held.release()
    await storing
    await w.clock.settle()  // no pause needed: the row's arrival wakes the end's proof
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[P1, D1]])
    await $.turn.complete(complete('t2') as never)
    for (let i = 0; i < 24; i++) await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[P1, D1], [P2, R2]])
  })


  test("the end names the turn's row latest in the file, whatever order Claude's appends finish in", async ($, on) => {
    // An end ranked by when rows were noted named the answer when its append finished after turn_duration's.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    const held = w.hold(R1)
    const storing = answer($, R1)
    await held.reached
    await done($, D1)
    w.disk.push(P1, R1, D1)
    await $.turn.complete(complete('t1') as never)
    await w.clock.advance(100)
    held.release()
    await storing
    for (let i = 0; i < 10 && !w.reports.some((r) => r.turns.some((t) => t.type === 'ended')); i++) await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => t.ref)).toEqual([D1])
  })

  test("a join still being stored when its turn ends goes before that end, under the turn's own conversation", async ($, on) => {
    // A join noted after its turn's end went after it (started, ended, joined), and after a /clear under the new
    // conversation with the old turn's id.
    const w = world(on)
    const J1 = 'b1111111-1111-4111-8111-111111111111'
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    const held = w.hold(J1)
    const joining = row($, J1, 'delivery', { kind: 'composer' }, { type: 'attachment', name: 'queued_command', isMeta: true })
    await held.reached
    await done($, D1)
    w.disk.push(P1, R1, J1, D1)
    await $.turn.complete(complete('t1') as never)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    held.release()
    await joining
    for (let i = 0; i < 10; i++) await w.clock.advance(100)
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn])))
      .toEqual([['s1', 'started', P1], ['s1', 'joined', P1], ['s1', 'ended', P1]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a local command's end names the report it leaves for the model, and never one from another conversation", async ($, on) => {
    // /context stores its report as a note right under its output row; an end at the output row has a plugin read the
    // command's turn without it. A /clear in between makes the next note the new conversation's, never the command's.
    const w = world(on)
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    const C2 = 'c2222222-2222-4222-8222-222222222222'
    const C3 = 'c3333333-3333-4333-8333-333333333333'
    const command = (uuid: string, text: string) =>
      row($, uuid, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command', content: [{ type: 'text', text }] })
    const note = (uuid: string) => row($, uuid, 'note', { kind: 'engine' }, { type: 'user', isMeta: true, content: [{ type: 'text', text: '## Context Usage' }] })
    await command(C1, '<command-name>/context</command-name>')
    await command(C2, '<local-command-stdout>Context usage</local-command-stdout>')
    w.disk.push(C1, C2)
    await w.clock.advance(200)
    expect(w.turns().some((t) => t.type === 'ended')).toBe(false)  // its output is proved, but its report may follow
    await note(C3)
    w.disk.push({ type: 'user', uuid: C3, parentUuid: C2, isMeta: true })  // the reader's link: a meta row under the output
    await w.clock.advance(200)
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[C1, C3]])
    const C4 = 'c4444444-4444-4444-8444-444444444444'
    const C5 = 'c5555555-5555-4555-8555-555555555555'
    await command(C4, '<command-name>/context</command-name>')
    await command(C5, '<local-command-stdout>Context usage</local-command-stdout>')
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await note('c6666666-6666-4666-8666-666666666666')
    w.disk.push(C4, C5)
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    // Its own conversation's file still proves its output after the /clear; the new conversation's note is never its.
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[C1, C3], [C4, C5]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a join whose append never returns gives up its place when its turn ends, and never holds up a new conversation", async ($, on) => {
    // An unfilled join had no deadline: it held its turn's end, and after a /clear every new turn's edges, for good.
    // The new conversation's edges wait only until the old one's have gone (core takes them only once the move is
    // recorded, and the move only after them), so at most its end's bound.
    const w = world(on)
    const J1 = 'b1111111-1111-4111-8111-111111111111'
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    const held = w.hold(J1)
    const stuck = row($, J1, 'delivery', { kind: 'composer' }, { type: 'attachment', name: 'queued_command', isMeta: true })
    await held.reached
    await done($, D1)
    w.disk.push(P1, J1, D1)
    await $.turn.complete(complete('t1') as never)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    await $.turn.start({ text: 'next', turnId: 't2' })
    await w.clock.advance(100)
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn]))).toEqual([['s1', 'started', P1]])
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn])))
      .toEqual([['s1', 'started', P1], ['s1', 'ended', P1], ['s2', 'started', P2]])
    expect(w.moves.map((m) => m.session)).toEqual(['s2'])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
    held.release()  // left held, the kit would abort it as the test ends, a rejection no one hears
    await stuck
  })

  test("rows rank by the order Claude stored them: a later row still half written beyond a clipped tail holds the end", async ($, on) => {
    // turn_duration noted before an older answer, its record still half written, while the tail no longer reaches the
    // opener: taken as older than the answer, the end went early with the answer.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    const held = w.hold(R1)
    const storing = answer($, R1)
    await held.reached
    await done($, D1)
    held.release()
    await storing
    w.disk.push({ type: 'user', uuid: 'f0000000-0000-4000-8000-000000000000', pad: 'x'.repeat(270000) }, R1)  // a long tail: the opener is before it
    w.after.text = `{"type":"system","uuid":"${D1}","subtype":"turn_d`  // half written
    await $.turn.complete(complete('t1') as never)
    await w.clock.advance(300)
    expect(w.turns().some((t) => t.type === 'ended')).toBe(false)
    w.after.text = ''
    w.disk.push(D1)
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => t.ref)).toEqual([D1])
  })

  test("a local command's report counts even when its append finishes before the output's", async ($, on) => {
    // The report's callback can complete first; with no end for it to join yet, the end went at the output row.
    const w = world(on)
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    const C2 = 'c2222222-2222-4222-8222-222222222222'
    const C3 = 'c3333333-3333-4333-8333-333333333333'
    const command = (uuid: string, text: string) =>
      row($, uuid, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command', content: [{ type: 'text', text }] })
    await command(C1, '<command-name>/context</command-name>')
    const held = w.hold(C2)
    const output = command(C2, '<local-command-stdout>Context usage</local-command-stdout>')
    await held.reached
    await row($, C3, 'note', { kind: 'engine' }, { type: 'user', isMeta: true, content: [{ type: 'text', text: '## Context Usage' }] })
    w.disk.push(C1, C2, C3)  // plain records: found by its own id, not only by its parent link
    held.release()
    await output
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[C1, C3]])
  })


  test("a row noted after a /clear, or after a newer row, never opens a turn or a command for the conversation it isn't in", async ($, on) => {
    // An old command's append returning after a /clear started it under the new conversation; an old prompt returning
    // after the new one was stored took the new turn's opening.
    const w = world(on)
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    await answer($, R1)  // the session is known before the rows below arrive
    const heldCommand = w.hold(C1)
    const command = row($, C1, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command',
      content: [{ type: 'text', text: '<command-name>/context</command-name>' }] })
    await heldCommand.reached
    const heldPrompt = w.hold(P1)
    const old = prompt($, P1)
    await heldPrompt.reached
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    heldCommand.release()
    heldPrompt.release()
    await command
    await old
    await $.turn.start({ text: 'new', turnId: 't2' })
    await w.clock.settle()
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn]))).toEqual([['s2', 'started', P2]])
    // In one conversation too: an older prompt's row noted after a newer one's never takes the newer turn; it is a
    // turn of its own, before it, as Claude stored them.
    const P3 = 'a7777777-7777-4777-8777-777777777777'
    const P4 = 'a8888888-8888-4888-8888-888888888888'
    const heldOlder = w.hold(P3)
    const older = prompt($, P3)
    await heldOlder.reached
    await prompt($, P4)
    heldOlder.release()
    await older
    await $.turn.start({ text: 'newer', turnId: 't3' })
    w.disk.push(P3)
    await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type !== 'joined').map((t) => [t.type, t.turn])).toEqual([['started', P2], ['started', P3], ['ended', P3], ['started', P4]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a /clear leaves nothing of the old conversation to open a turn: a prompt stored but not started, or a row from before its session was known", async ($, on) => {
    // An opener stored but unused before a /clear started a turn in the new conversation with no input of its own; a
    // row that arrived before the session id was known couldn't be told old from new by its session alone. The one
    // stored but not started is the old conversation's turn: it starts there, at the /clear.
    const w = world(on)
    const held = w.hold(P1)
    const early = prompt($, P1)  // arrives before anything has looked the session up
    await held.reached
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    held.release()
    await early
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    await $.classic.SessionStart({ source: 'clear', session_id: 's3' } as never)  // P2 was stored but never started
    await $.turn.start({ text: '', turnId: 't1' })  // a turn with no input of its own
    await w.clock.settle()
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn]))).toEqual([['s2', 'started', P2]])
    await w.clock.advance(6000)  // the /clears' own waits for Claude's process file run out before the test ends
  })

  test("a row older than an opening or command already used never becomes one", async ($, on) => {
    // An older prompt row noted after a newer one was used started a false turn at the next continuation; the same
    // for a local command's row. A turn.start waits for an opener row still being stored, up to its bound: one
    // noted after that is never a turn, since its start could no longer go in its place.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const held = w.hold(P1)
    const older = prompt($, P1)
    await held.reached
    await prompt($, P2)
    const starting = $.turn.start({ text: 'newer', turnId: 't1' })
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    await starting
    held.release()
    await older
    await $.turn.complete(complete('t1') as never)
    await $.turn.start({ text: '', turnId: 't2' })  // Claude continues on its own
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    const C2 = 'c2222222-2222-4222-8222-222222222222'
    const command = (uuid: string) => row($, uuid, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command',
      content: [{ type: 'text', text: '<command-name>/context</command-name>' }] })
    const heldCommand = w.hold(C1)
    const olderCommand = command(C1)
    await heldCommand.reached
    await command(C2)
    heldCommand.release()
    await olderCommand
    for (let i = 0; i < 25; i++) await w.clock.advance(100)  // the first turn's end goes at its bound; the starts queue behind it
    expect(w.turns().filter((t) => t.type === 'started').map((t) => t.turn)).toEqual([P2, C2])
  })


  test("a session id lookup still under way at a /clear never puts the old conversation's id back", async ($, on) => {
    // The lookup started before the /clear answered after it, and the new conversation's turns went under the old id.
    const w = world(on)
    let answer = () => {}
    w.idHeld.until = new Promise<void>((resolve) => { answer = resolve })
    const early = prompt($, P1)  // its noting looks the session up
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    w.idHeld.until = undefined
    answer()
    await early
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    await $.turn.start({ text: 'new', turnId: 't1' })
    await w.clock.settle()
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn]))).toEqual([['s2', 'started', P2]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("an old turn.start whose session lookup finishes after a /clear never takes the new conversation's opening", async ($, on) => {
    // The old start awaited the session id; a /clear and a new prompt came meanwhile; it then took that prompt's row.
    const w = world(on)
    let answer = () => {}
    w.idHeld.until = new Promise<void>((resolve) => { answer = resolve })
    const old = prompt($, P1)
    const oldStart = $.turn.start({ text: 'old', turnId: 't0' })
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    w.idHeld.until = undefined
    answer()
    await old
    await oldStart
    await w.clock.settle()
    expect(w.turns().filter((t) => t.type === 'started')).toEqual([])  // the old start takes nothing
    await $.turn.start({ text: 'new', turnId: 't1' })
    await w.clock.settle()
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn]))).toEqual([['s2', 'started', P2]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a turn.complete or session.start entered before a /clear never ends or resets the new conversation's turn", async ($, on) => {
    // Resuming after the /clear, the old turn.complete ended the newly started turn, and the old session.start erased
    // the new conversation's opening.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'old', turnId: 't1' })
    let finishComplete = () => {}
    w.heldHooks.complete = new Promise<void>((resolve) => { finishComplete = resolve })
    const oldComplete = $.turn.complete(complete('t1') as never)
    await w.clock.settle()
    let finishStart = () => {}
    w.heldHooks.start = new Promise<void>((resolve) => { finishStart = resolve })
    const oldStart = $.session.start({ cwd: '/w' } as never)
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    w.heldHooks.start = undefined
    finishStart()
    await oldStart
    await $.turn.start({ text: 'new', turnId: 't2' })
    w.heldHooks.complete = undefined
    finishComplete()
    await oldComplete
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    // Order is kept per conversation (core keeps no order across conversations): each one's edges in its own order.
    const edges = w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn]))
    expect(edges.filter((e) => e[0] === 's1')).toEqual([['s1', 'started', P1], ['s1', 'ended', P1]])
    expect(edges.filter((e) => e[0] === 's2')).toEqual([['s2', 'started', P2]])
    expect(w.reports.at(-1)?.status).toBe('working')  // the new turn still runs
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a turn.complete that found no turn on entry (a reload mid-turn) ends nothing, not a turn started since", async ($, on) => {
    // Passing its captured 'no turn' on let a default take the turn current when it resumed: the new one.
    const w = world(on)
    let finishComplete = () => {}
    w.heldHooks.complete = new Promise<void>((resolve) => { finishComplete = resolve })
    const oldComplete = $.turn.complete(complete('t0') as never)  // this load never saw the turn it ends
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    await $.turn.start({ text: 'new', turnId: 't1' })
    w.heldHooks.complete = undefined
    finishComplete()
    await oldComplete
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    expect(w.turns().map((t) => [t.type, t.turn])).toEqual([['started', P2]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a turn started while an earlier turn's completion is still running goes after that turn's end", async ($, on) => {
    // The end took its place only once Claude's own part of turn.complete returned, so the new start went first:
    // core closed the old turn as unknown, then got a second end for it.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'old', turnId: 't1' })
    let finishComplete = () => {}
    w.heldHooks.complete = new Promise<void>((resolve) => { finishComplete = resolve })
    const oldComplete = $.turn.complete(complete('t1') as never)
    await w.clock.settle()
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    await $.turn.start({ text: 'new', turnId: 't2' })
    w.heldHooks.complete = undefined
    finishComplete()
    await oldComplete
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    expect(w.turns().map((t) => [t.type, t.turn])).toEqual([['started', P1], ['ended', P1], ['started', P2]])
    expect(w.reports.at(-1)?.status).toBe('working')  // the new turn still runs
  })

  test("after a /clear, a turn that completes with no opener seen ends nothing of the old conversation", async ($, on) => {
    // The old turn stayed the current one across the /clear, so the new conversation's first completion ended it.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'old', turnId: 't1' })
    await $.turn.complete(complete('t1') as never)
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await $.turn.start({ text: '', turnId: 't2' })  // no opener stored in the new conversation
    await $.turn.complete(complete('t2') as never)
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn])))
      .toEqual([['s1', 'started', P1], ['s1', 'ended', P1]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a turn_duration row stored while Claude's part of turn.complete runs is still the end's row", async ($, on) => {
    // The proof started on the hook's entry could finish, and the end go, before the row Claude stores meanwhile.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    w.disk.push(P1, R1)
    let finishComplete = () => {}
    w.heldHooks.complete = new Promise<void>((resolve) => { finishComplete = resolve })
    const completing = $.turn.complete(complete('t1') as never)
    await w.clock.advance(100)
    await done($, D1)
    w.disk.push(D1)
    w.heldHooks.complete = undefined
    finishComplete()
    await completing
    for (let i = 0; i < 10 && !w.reports.some((r) => r.status === 'idle'); i++) await w.clock.advance(50)
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[P1, D1]])
  })

  test("a turn.complete whose turn Claude continued while its own part ran leaves the continuation working", async ($, on) => {
    // A continuation keeps the same turn, so the old hook's check missed it and reported idle over the running turn.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    w.disk.push(P1, R1)
    let finishComplete = () => {}
    w.heldHooks.complete = new Promise<void>((resolve) => { finishComplete = resolve })
    const completing = $.turn.complete(complete('t1') as never)
    await w.clock.settle()
    await $.turn.start({ text: '', turnId: 't2' })  // a continuation: no new opener
    w.heldHooks.complete = undefined
    finishComplete()
    await completing
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    expect(w.reports.some((r) => r.status === 'idle')).toBe(false)
  })


  test("an end still waiting on Claude's part of turn.complete never takes a row of the continuation Claude started meanwhile", async ($, on) => {
    // A continuation keeps the turn, so the earlier end's proof read the continuation's first row as its own last row.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    w.disk.push(P1, R1)
    let finishComplete = () => {}
    w.heldHooks.complete = new Promise<void>((resolve) => { finishComplete = resolve })
    const completing = $.turn.complete(complete('t1') as never)
    await w.clock.settle()
    await $.turn.start({ text: '', turnId: 't2' })  // a continuation: no new opener
    const R2 = 'a6666666-6666-4666-8666-666666666666'
    await answer($, R2)
    w.disk.push(R2)
    w.heldHooks.complete = undefined
    finishComplete()
    await completing
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[P1, R1]])
  })

  test("with no transcript path, an end still waits for Claude's part of turn.complete, up to the bound", async ($, on) => {
    // The proof's loop never ran without a path, so the end went at once with no row while Claude's part still ran.
    const w = world(on)
    w.info.transcriptPath = undefined
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    let finishComplete = () => {}
    w.heldHooks.complete = new Promise<void>((resolve) => { finishComplete = resolve })
    const completing = $.turn.complete(complete('t1') as never)
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type === 'ended')).toEqual([])
    w.heldHooks.complete = undefined
    finishComplete()
    await completing
    await w.clock.settle()
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[P1, null]])
  })


  test("an earlier end going gives up only its own step's join places, never a continuation's", async ($, on) => {
    // The earlier end dropped every pending join place of the turn, so a continuation's join never went out.
    const w = world(on)
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    w.disk.push(P1, R1)
    let finishComplete = () => {}
    w.heldHooks.complete = new Promise<void>((resolve) => { finishComplete = resolve })
    const completing = $.turn.complete(complete('t1') as never)
    await w.clock.settle()
    await $.turn.start({ text: '', turnId: 't2' })  // a continuation: no new opener
    const J2 = 'b7777777-7777-4777-8777-777777777777'
    const held = w.hold(J2)
    const joining = row($, J2, 'delivery', { kind: 'composer' }, { type: 'attachment', name: 'queued_command', isMeta: true })
    await held.reached
    w.disk.push(J2)
    w.heldHooks.complete = undefined
    finishComplete()
    await completing
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    expect(w.turns().map((t) => [t.type, t.turn])).toEqual([['started', P1], ['ended', P1]])
    held.release()
    await joining
    await w.clock.settle()
    expect(w.turns().map((t) => [t.type, t.turn, t.ref])).toEqual([['started', P1, P1], ['ended', P1, R1], ['joined', P1, J2]])
  })


  test("inputs Claude takes in together after a turn are each a turn of their own: each starts, and all but the last end, in order", async ($, on) => {
    // Issue #47: Claude stores follow-ups queued behind a turn as one prompt row each, then starts one turn for them
    // all. The transcript reader makes each row a turn, but only the last got a start, so the others' messages were
    // in no event. Each start must name its own row and delivery, and a person's prompt queued with them its own source.
    const w = world(on)
    const delivery = (n: number) => `7a1b2c3d-1111-4222-8333-${944455556000 + n}`
    const uuid = (n: number) => `e${String(n).padStart(7, '0')}-1111-4111-8111-111111111111`
    const working = async (turn: string, ending: string) => {
      w.disk.push(ending)
      for (let i = 0; i < 25 && !w.turns().some((t) => t.type === 'ended' && t.turn === turn); i++) await w.clock.advance(100)
    }
    let n = 0
    for (const queued of [['mv2', 'mv2'], ['mv2', 'mv2', 'mv2'], ['mv2', 'person', 'mv2']]) {
      const before = w.turns().length
      const run = uuid(++n)
      const reply = uuid(++n)
      const duration = uuid(++n)
      await prompt($, run)
      await $.turn.start({ text: 'run', turnId: `t${n}` })
      await answer($, reply)
      const rows = queued.map((kind) => ({ kind, id: delivery(++n), row: uuid(n), text: `queued ${n}` }))
      for (const input of rows) {
        if (input.kind === 'mv2') await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${input.id}>>${input.text}<</MV2>>` })
      }
      await done($, duration)
      await $.turn.complete(complete(`t${n}`) as never)
      w.disk.push(run, reply)
      await working(run, duration)
      for (const input of rows) {
        if (input.kind === 'mv2') await row($, input.row, 'prompt', { kind: 'plugin', name: 'multiverse', asUser: true }, { type: 'user', content: [{ type: 'text', text: input.text }] })
        else await prompt($, input.row)  // typed into Claude's queue while the turn ran
        w.disk.push(input.row)
      }
      await $.turn.start({ text: rows[rows.length - 1]?.text ?? '', turnId: `t${n}b` })
      const last = rows[rows.length - 1]?.row ?? ''
      const answered = uuid(++n)
      const ended = uuid(++n)
      await answer($, answered)
      await done($, ended)
      await $.turn.complete(complete(`t${n}b`) as never)
      w.disk.push(answered)
      await working(last, ended)
      const started = (input: { kind: string; id: string; row: string }) => ({ type: 'started', turn: input.row, kind: 'prompt', ref: input.row,
        source: input.kind === 'mv2' ? 'harness' : 'person', ...(input.kind === 'mv2' ? { attempt: input.id } : {}), first_task: false })
      expect(w.turns().slice(before)).toEqual([
        { type: 'started', turn: run, kind: 'prompt', ref: run, source: 'person', first_task: false },
        { type: 'ended', turn: run, kind: 'prompt', ref: duration, state: 'finished' },
        ...rows.flatMap((input, index) => index === rows.length - 1 ? [started(input)]
          : [started(input), { type: 'ended', turn: input.row, kind: 'prompt', ref: input.row, state: 'finished' }]),
        { type: 'ended', turn: last, kind: 'prompt', ref: ended, state: 'finished' }])
      // The agent stays working from the first of them to the last turn's end, and idle comes after that end.
      const at = (match: (t: Record<string, unknown>) => boolean) => w.reports.findIndex((r) => r.turns.some(match))
      const first = at((t) => t.type === 'started' && t.turn === rows[0]?.row)
      const lastEnd = at((t) => t.type === 'ended' && t.turn === last)
      expect(w.reports.slice(first, lastEnd).some((r) => r.status === 'idle')).toBe(false)
      expect(w.reports.slice(lastEnd).some((r) => r.status === 'idle')).toBe(true)
    }
  })

  test("a steer joins the running turn and a follow-up starts its own after it, each with its own delivery", async ($, on) => {
    // The control for issue #47: a steer is taken in mid-turn, a follow-up only after the turn; neither may take the
    // other's place or delivery.
    const w = world(on)
    const FOLLOW = '7a1b2c3d-1111-4222-8333-944455556672'
    const J1 = 'b2222222-2222-4222-8222-222222222222'
    const F1 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P1)
    await $.turn.start({ text: 'run', turnId: 't1' })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 steer id=${STEER}>>Rebase first.<</MV2>>` })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${FOLLOW}>>Then test.<</MV2>>` })
    await row($, J1, 'delivery', { kind: 'peer' }, { type: 'attachment', name: 'queued_command', isMeta: true,
      content: [{ type: 'text', text: '<system-reminder>\nAnother Claude session sent a message while you were working:\nRebase first.\n</system-reminder>' }] })
    await answer($, R1)
    await done($, D1)
    await $.turn.complete(complete('t1') as never)
    w.disk.push(P1, J1, R1, D1)
    for (let i = 0; i < 25 && !w.turns().some((t) => t.type === 'ended'); i++) await w.clock.advance(100)
    await row($, F1, 'prompt', { kind: 'plugin', name: 'multiverse', asUser: true }, { type: 'user', content: [{ type: 'text', text: 'Then test.' }] })
    await $.turn.start({ text: 'Then test.', turnId: 't2' })
    await w.clock.settle()
    expect(w.turns()).toEqual([
      { type: 'started', turn: P1, kind: 'prompt', ref: P1, source: 'person', first_task: false },
      { type: 'joined', turn: P1, ref: J1, source: 'harness', attempt: STEER },
      { type: 'ended', turn: P1, kind: 'prompt', ref: D1, state: 'finished' },
      { type: 'started', turn: F1, kind: 'prompt', ref: F1, source: 'harness', attempt: FOLLOW, first_task: false }])
  })

  test("an opener row still being stored when its turn starts is waited for, and starts its own turn in its place", async ($, on) => {
    // Its append can still be running when Claude fires turn.start: taken without it, its turn would never start,
    // and a start sent once it is noted would come after the next input's.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const held = w.hold(P1)
    const first = prompt($, P1)
    await held.reached
    await prompt($, P2)
    const starting = $.turn.start({ text: 'both', turnId: 't1' })
    await w.clock.advance(100)
    expect(w.turns()).toEqual([])
    held.release()
    await first
    await starting
    w.disk.push(P1)
    await w.clock.advance(100)
    expect(w.turns()).toEqual([
      { type: 'started', turn: P1, kind: 'prompt', ref: P1, source: 'person', first_task: false },
      { type: 'ended', turn: P1, kind: 'prompt', ref: P1, state: 'finished' },
      { type: 'started', turn: P2, kind: 'prompt', ref: P2, source: 'person', first_task: false }])
    expect(w.reports.some((r) => r.status === 'working')).toBe(true)
  })

  test("a typed local command Claude stores as user rows is a command turn from its caveat, never a prompt turn", async ($, on) => {
    // /reload-plugins (Claude Code 2.1.332) stores a caveat row and a <command-name> row as user rows with no origin
    // stamped, then its output: the reader makes the caveat the command and the rest its output, so they must not
    // become prompt turns, nor a command turn with no events.
    const w = world(on)
    const user = (uuid: string, text: string, meta: boolean) =>
      row($, uuid, 'command', { kind: 'unclassified' }, { type: 'user', content: [{ type: 'text', text }], ...(meta ? { isMeta: true } : {}) })
    await user('c1111111-1111-4111-8111-111111111111', '<local-command-caveat>The command below was run.</local-command-caveat>', true)
    await user('c2222222-2222-4222-8222-222222222222', '<command-name>/reload-plugins</command-name>', false)
    await row($, 'c3333333-3333-4333-8333-333333333333', 'command', { kind: 'engine' }, { type: 'system', name: 'local_command',
      content: [{ type: 'text', text: '<local-command-stdout>Reloaded</local-command-stdout>' }] })
    await prompt($, P1, { kind: 'plugin', name: 'multiverse', asUser: true })
    await $.turn.start({ text: 'go', turnId: 't1' })
    w.disk.push('c1111111-1111-4111-8111-111111111111', 'c2222222-2222-4222-8222-222222222222', 'c3333333-3333-4333-8333-333333333333')
    await w.clock.advance(300)
    // They are the command turn (the caveat its first row), and the output ends it; the prompt is the only prompt turn.
    expect(w.turns().filter((t) => t.type === 'started' || t.type === 'ended').map((t) => [t.type, t.kind, t.turn, t.ref])).toEqual([
      ['started', 'command', 'c1111111-1111-4111-8111-111111111111', 'c1111111-1111-4111-8111-111111111111'],
      ['ended', 'command', 'c1111111-1111-4111-8111-111111111111', 'c3333333-3333-4333-8333-333333333333'],
      ['started', 'prompt', P1, P1]])
    expect(w.turns()[0]?.source).toBe('person')
  })

  test("a row Claude stores with a queued prompt before the next one is that turn's last row, even one still being stored", async ($, on) => {
    // The reader groups such a row (hook context, say) with the prompt before it, so that turn's end names it; an end
    // naming only the prompt row would cut a plugin's read of the turn short.
    const w = world(on)
    const H1 = 'b1111111-1111-4111-8111-111111111111'
    const H2 = 'b3333333-3333-4333-8333-333333333333'
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const P3 = 'a7777777-7777-4777-8777-777777777777'
    const context = (uuid: string) => row($, uuid, 'hook-context', { kind: 'engine' }, { type: 'attachment', name: 'hook_additional_context', isMeta: true })
    await prompt($, P1)
    await context(H1)
    await prompt($, P2)
    const held = w.hold(H2)
    const storing = context(H2)
    await held.reached
    await prompt($, P3)
    await $.turn.start({ text: 'all', turnId: 't1' })
    w.disk.push(P1, H1, P2, H2, P3)
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    // P2's end waits for H2, its row still being stored.
    expect(w.turns().map((t) => [t.type, t.turn, t.ref])).toEqual([['started', P1, P1], ['ended', P1, H1], ['started', P2, P2]])
    held.release()
    await storing
    await w.clock.advance(100)
    expect(w.turns().map((t) => [t.type, t.turn, t.ref])).toEqual([['started', P1, P1], ['ended', P1, H1], ['started', P2, P2],
      ['ended', P2, H2], ['started', P3, P3]])
  })


  test("prompts a conversation stored and never took in before a /clear still get their turns, under that conversation", async ($, on) => {
    // The reader makes each stored prompt row a turn: dropped at the /clear, they would be inputs with no event.
    // Each starts under the old conversation, each but the last ends finished, and the last is left for core to end
    // at the switch; the new conversation's turns are its own.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const P3 = 'a7777777-7777-4777-8777-777777777777'
    await answer($, R1)  // the session is known
    await prompt($, P1)
    await prompt($, P2)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await w.clock.settle()
    await prompt($, P3)
    await $.turn.start({ text: 'new', turnId: 't1' })
    for (let i = 0; i < 20 && w.moves.length === 0; i++) await w.clock.advance(100)
    await w.clock.advance(100)
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn, t.ref ?? null]))).toEqual([
      ['s1', 'started', P1, P1], ['s1', 'ended', P1, P1], ['s1', 'started', P2, P2], ['s2', 'started', P3, P3]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a /clear's move reaches core after the old conversation's edges, and the new conversation's edges after the move", async ($, on) => {
    // Core drops a batch for a conversation other than the one it has recorded: the old conversation's starts and
    // ends sent after the move, or the new one's sent before it, would be lost.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const P3 = 'a7777777-7777-4777-8777-777777777777'
    await answer($, R1)
    await prompt($, P1)
    await prompt($, P2)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await prompt($, P3)
    await $.turn.start({ text: 'new', turnId: 't1' })
    for (let i = 0; i < 80 && w.moves.length === 0; i++) await w.clock.advance(100)
    await w.clock.advance(100)
    expect(w.moves.map((m) => m.session)).toEqual(['s2'])
    const move = w.moves[0]?.after ?? -1
    const at = (session: string, type: string, turn: string) =>
      w.reports.findIndex((r) => r.session === session && r.turns.some((t) => t.type === type && t.turn === turn))
    const old = [at('s1', 'started', P1), at('s1', 'ended', P1), at('s1', 'started', P2)]
    expect(old.every((i) => i >= 0 && i < move)).toBe(true)
    expect(at('s2', 'started', P3)).toBeGreaterThanOrEqual(move)
  })

  test("a compaction's own rows open no turn until its first real row; an unstamped plain row outside one does", async ($, on) => {
    // The reader counts rows after a compaction boundary as the compaction's, until a stamped prompt, a reply or a
    // tool result; Claude doesn't show an append the boundary, so session.compact marks the span. Outside a span, an
    // unstamped row that is neither meta nor a command's is a prompt, as the reader counts it.
    const w = world(on)
    const unstamped = (uuid: string) => row($, uuid, 'prompt', { kind: 'unclassified' }, { type: 'user' })
    const U1 = 'f1111111-1111-4111-8111-111111111111'
    const U2 = 'f2222222-2222-4222-8222-222222222222'
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await unstamped(U1)
    await $.turn.start({ text: 'one', turnId: 't1' })
    await answer($, R1)
    await done($, D1)
    await $.turn.complete(complete('t1') as never)
    w.disk.push(U1, R1, D1)
    for (let i = 0; i < 25 && !w.turns().some((t) => t.type === 'ended'); i++) await w.clock.advance(100)
    await $.session.compact({ trigger: 'manual', messages: [] } as never)
    await unstamped(U2)
    await prompt($, P2)  // a real row: the span ends here
    await $.turn.start({ text: 'two', turnId: 't2' })
    const R2 = 'a6666666-6666-4666-8666-666666666666'
    const D2 = 'a8888888-8888-4888-8888-888888888888'
    const U3 = 'f3333333-3333-4333-8333-333333333333'
    await answer($, R2)
    await done($, D2)
    await $.turn.complete(complete('t2') as never)
    w.disk.push(U2, P2, R2, D2)
    for (let i = 0; i < 25 && w.turns().filter((t) => t.type === 'ended').length < 2; i++) await w.clock.advance(100)
    await unstamped(U3)
    await $.turn.start({ text: 'three', turnId: 't3' })
    await w.clock.settle()
    expect(w.turns().filter((t) => t.type === 'started').map((t) => t.turn)).toEqual([U1, P2, U3])
  })


  test("a /clear's move still goes, at its bound, when core never answers the report carrying the old conversation's edges", async ($, on) => {
    // The move waits for the old edges, but a core that never takes them must not hold it, nor the new
    // conversation's edges behind it, for good.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await answer($, R1)
    await prompt($, P1)
    await prompt($, P2)
    let answer_ = () => {}
    w.hanging.until = new Promise<void>((resolve) => { answer_ = resolve })
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    const cleared = w.clock.now()
    for (let i = 0; i < 400 && w.moves.length === 0; i++) await w.clock.advance(100)
    expect(w.moves.map((m) => m.session)).toEqual(['s2'])
    expect((w.moves[0]?.at ?? Infinity) - cleared).toBeLessThanOrEqual(2050 + 3 * 10000 + 300)
    answer_()  // left hanging, the report would outlive the test
    await w.clock.settle()
  })


  test("two /clears: each conversation's edges wait for the move to it, and the moves go in order from the one core has", async ($, on) => {
    // With only the newest conversation held, the middle one's edges went before its move was recorded, so core dropped
    // them. When the second /clear comes before Claude has named the first one's conversation, that move can't be
    // recorded at all, so the next goes from the conversation core still has, never from one it never recorded.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const P3 = 'a7777777-7777-4777-8777-777777777777'
    const P4 = 'a8888888-8888-4888-8888-888888888888'
    const reported = (session: string, turn: string) =>
      w.reports.findIndex((r) => r.session === session && r.turns.some((t) => t.type === 'started' && t.turn === turn))
    await answer($, R1)
    // In a row, first: Claude names s3 before the move to s2 could be recorded, so the move goes from s1 straight to s3.
    await prompt($, P1)  // stored, never taken in: s1's
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await $.classic.SessionStart({ source: 'clear', session_id: 's3' } as never)
    for (let i = 0; i < 80 && w.moves.length < 1; i++) await w.clock.advance(100)
    expect(w.moves.map((m) => [m.previous, m.session])).toEqual([['s1', 's3']])
    expect(reported('s1', P1)).toBeLessThan(w.moves[0]?.after ?? -1)
    // Apart: each move is recorded, and each conversation's edges go between the move to it and the move away.
    await prompt($, P2)  // s3's
    await $.classic.SessionStart({ source: 'clear', session_id: 's4' } as never)
    for (let i = 0; i < 20 && w.moves.length < 2; i++) await w.clock.advance(100)
    await prompt($, P3)  // s4's
    await $.classic.SessionStart({ source: 'clear', session_id: 's5' } as never)
    await prompt($, P4)
    await $.turn.start({ text: 'new', turnId: 't1' })
    for (let i = 0; i < 20 && w.moves.length < 3; i++) await w.clock.advance(100)
    await w.clock.advance(100)
    expect(w.moves.map((m) => [m.previous, m.session])).toEqual([['s1', 's3'], ['s3', 's4'], ['s4', 's5']])
    const [, second, third] = w.moves.map((m) => m.after)
    expect(reported('s3', P2)).toBeLessThan(second ?? -1)
    expect(reported('s4', P3)).toBeGreaterThanOrEqual(second ?? Infinity)
    expect(reported('s4', P3)).toBeLessThan(third ?? -1)
    expect(reported('s5', P4)).toBeGreaterThanOrEqual(third ?? Infinity)
  })


  // Every wake and mv2 resume starts Claude with --resume, and Claude raises classic.SessionStart with source resume
  // as it starts, after session.start or inside its own part. Taken as a switch, it moved nothing but reset the turn
  // state, so the wake's first prompt lost first_task, which core uses to credit a Claude wake to mv2 and its message.
  async function startupResume($: Raise & { session: { start: (input: never) => Promise<unknown> };
    classic: { SessionStart: (input: never) => Promise<unknown> }; turn: { start: (input: never) => Promise<unknown> } },
    w: ReturnType<typeof world>, inside: boolean) {
    let finish = () => {}
    if (inside) w.heldHooks.start = new Promise<void>((resolve) => { finish = resolve })
    const start = $.session.start({ cwd: '/w' } as never)
    if (inside) await w.clock.settle()
    else await start
    await $.classic.SessionStart({ source: 'resume', session_id: 's1' } as never)
    if (inside) {
      w.heldHooks.start = undefined
      finish()
      await start
    }
    await prompt($, P1)
    await $.turn.start({ text: 'wake', turnId: 't1' } as never)
    for (let i = 0; i < 30; i++) await w.clock.advance(100)
    expect([w.moves, w.turns().filter((t) => t.type === 'started').map((t) => t.first_task)]).toEqual([[], [true]])
  }

  test("a resumed launch's startup resume after session.start is no switch: the wake's first prompt keeps its marker", async ($, on) => {
    await startupResume($ as never, world(on, { MV2_FIRST_TASK: '1' }), false)
  })

  test("a resumed launch's startup resume inside session.start's own part is no switch either", async ($, on) => {
    await startupResume($ as never, world(on, { MV2_FIRST_TASK: '1' }), true)
  })


  test("a /resume moves the agent as a /clear does, and the next /clear goes from the resumed conversation", async ($, on) => {
    // Claude raises classic.SessionStart (source resume) for a typed /resume, never session.start (2.1.332): the mod
    // kept the cleared conversation as its own, so the resumed one's turns went under an id core no longer has once it
    // reads Claude's process file, and the next /clear's move named that id as the one it left, which core refuses.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await answer($, R1)  // the session is known: s1
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    for (let i = 0; i < 20 && w.moves.length < 1; i++) await w.clock.advance(100)
    await $.classic.SessionStart({ source: 'resume', session_id: 's1' } as never)
    for (let i = 0; i < 20 && w.moves.length < 2; i++) await w.clock.advance(100)
    await prompt($, P2)
    await $.turn.start({ text: 'again', turnId: 't2' })
    await w.clock.settle()
    await $.classic.SessionStart({ source: 'clear', session_id: 's3' } as never)
    for (let i = 0; i < 20 && w.moves.length < 3; i++) await w.clock.advance(100)
    expect(w.moves.map((m) => [m.previous, m.session, m.reason])).toEqual(
      [['s1', 's2', 'clear'], ['s2', 's1', 'resume'], ['s1', 's3', 'clear']])
    expect(w.reports.find((r) => r.turns.some((t) => t.type === 'started' && t.turn === P2))?.session).toBe('s1')
    await w.clock.advance(6000)  // the last /clear's own wait for Claude's process file runs out before the test ends
  })


  // Claude switches, and names the new conversation in its process file, only once its SessionEnd hooks return; any
  // mv2 read after that rebinds the agent itself and drops the old conversation's later edges (qa-claude's Q-5). An
  // end still being proved at a /clear or a /resume went after the switch, and core closed its turn as unknown.
  async function heldSwitch($: Raise & { turn: { start: (input: never) => Promise<unknown>; complete: (input: never) => Promise<unknown> };
    classic: { SessionEnd: (input: never) => Promise<unknown>; SessionStart: (input: never) => Promise<unknown> } },
    w: ReturnType<typeof world>, reason: string) {
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' } as never)
    await answer($, R1)
    await done($, D1)
    await $.turn.complete(complete('t1') as never)  // its rows aren't in the file yet: its end is still being proved
    let atSwitch = -1
    const ending = $.classic.SessionEnd({ reason } as never).then(() => { atSwitch = w.reports.length })
    await w.clock.advance(300)
    expect(atSwitch).toBe(-1)  // Claude waits
    w.disk.push(P1, R1, D1)
    for (let i = 0; i < 30 && atSwitch < 0; i++) await w.clock.advance(50)
    await ending
    const ended = w.reports.findIndex((r) => r.session === 's1' && r.turns.some((t) => t.type === 'ended' && t.turn === P1))
    expect([ended >= 0, ended < atSwitch]).toEqual([true, true])
    await $.classic.SessionStart({ source: reason, session_id: 's2' } as never)
    await w.clock.advance(6000)  // the switch's own wait for Claude's process file runs out before the test ends
  }

  test("a /clear's SessionEnd holds the switch until the running turn's end has reached core", async ($, on) => {
    await heldSwitch($ as never, world(on), 'clear')
  })

  test("a typed /resume's SessionEnd holds it the same way", async ($, on) => {
    await heldSwitch($ as never, world(on), 'resume')
  })


  test("at a switch's SessionEnd, a local command's end waiting for its next row goes at once, before the switch", async ($, on) => {
    // Its end waits up to its bound for the next row to say whether a report follows the output. Leaving the
    // conversation, none comes: the wait outlasted Claude's hold on the switch, and core closed the turn as unknown.
    const w = world(on)
    const [K1, O1] = ['c1111111-1111-4111-8111-111111111111', 'c3333333-3333-4333-8333-333333333333']
    await answer($, R1)
    await row($, K1, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command',
      content: [{ type: 'text', text: '<command-name>/context</command-name>' }] })
    await row($, O1, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command',
      content: [{ type: 'text', text: '<local-command-stdout>Context</local-command-stdout>' }] })
    w.disk.push(R1, K1, O1)
    await w.clock.settle()
    let atSwitch = -1
    const started = w.clock.now()
    const ending = $.classic.SessionEnd({ reason: 'clear' } as never).then(() => { atSwitch = w.reports.length })
    for (let i = 0; i < 30 && atSwitch < 0; i++) await w.clock.advance(50)
    await ending
    const ended = w.reports.findIndex((r) => r.session === 's1' && r.turns.some((t) => t.type === 'ended' && t.turn === K1 && t.ref === O1))
    expect([ended >= 0, ended < atSwitch, w.clock.now() - started < 1000]).toEqual([true, true, true])
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a switch's SessionEnd waits for a local command still open in the conversation it leaves", async ($, on) => {
    // A command whose output row isn't noted yet has only its start queued: the hold returned, and the output row,
    // noted after the switch, ended nothing (reviewer-agy's case E). Its end, once noted, goes with no report.
    const w = world(on)
    const [K1, O1] = ['c1111111-1111-4111-8111-111111111111', 'c3333333-3333-4333-8333-333333333333']
    await answer($, R1)
    await row($, K1, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command',
      content: [{ type: 'text', text: '<command-name>/context</command-name>' }] })
    w.disk.push(R1, K1)
    await w.clock.settle()
    let atSwitch = -1
    const ending = $.classic.SessionEnd({ reason: 'clear' } as never).then(() => { atSwitch = w.reports.length })
    await w.clock.advance(150)
    await row($, O1, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command',
      content: [{ type: 'text', text: '<local-command-stdout>Context</local-command-stdout>' }] })
    w.disk.push(O1)
    for (let i = 0; i < 30 && atSwitch < 0; i++) await w.clock.advance(50)
    await ending
    const ended = w.reports.findIndex((r) => r.session === 's1' && r.turns.some((t) => t.type === 'ended' && t.turn === K1 && t.ref === O1))
    expect([ended >= 0, ended < atSwitch]).toEqual([true, true])
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a local command that never got its output doesn't hold a later switch", async ($, on) => {
    // A /model row with no output row stays open; counted as open at any age, every later /clear or /resume from
    // that conversation waited out the whole hold (reviewer-agy's case F). Its output can only still come soon after it.
    const w = world(on)
    const K1 = 'c1111111-1111-4111-8111-111111111111'
    await answer($, R1)
    await row($, K1, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command',
      content: [{ type: 'text', text: '<command-name>/model</command-name>' }] })
    w.disk.push(R1, K1)
    await w.clock.advance(2500)
    const before = w.clock.now()
    let held = -1
    const ending = $.classic.SessionEnd({ reason: 'clear' } as never).then(() => { held = w.clock.now() - before })
    for (let i = 0; i < 40 && held < 0; i++) await w.clock.advance(50)
    await ending
    expect(held).toBeLessThan(300)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("two /clears in a row: the second's SessionEnd lets the move to the first one's conversation land, with its edges, before Claude moves on", async ($, on) => {
    // Claude names a /clear's new conversation in its process file only after the hook, and runs two queued /clears
    // about 70 ms apart, so the move to the middle one was overtaken: never recorded, its edges dropped (Shao's 512).
    // Claude waits for the second /clear's SessionEnd, with its file naming the middle conversation, so that move goes.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const D2 = 'a6666666-6666-4666-8666-666666666666'
    w.processFile.follows = false
    w.processFile.id = 's1'
    await answer($, R1)  // the session is known: s1
    await $.classic.SessionEnd({ reason: 'clear' } as never)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    await prompt($, P2)  // a turn in s2, quick
    await $.turn.start({ text: 'two', turnId: 't2' })
    await done($, D2)
    w.disk.push(R1, P2, D2)
    await $.turn.complete(complete('t2') as never)
    let switched = false
    const ending = $.classic.SessionEnd({ reason: 'clear' } as never).then(() => { switched = true })
    w.processFile.id = 's2'  // Claude's write for s2 lands while it waits in the second /clear's SessionEnd
    for (let i = 0; i < 30 && !switched; i++) await w.clock.advance(50)
    await ending
    w.processFile.id = 's3'
    await $.classic.SessionStart({ source: 'clear', session_id: 's3' } as never)
    for (let i = 0; i < 80 && w.moves.length < 2; i++) await w.clock.advance(100)
    expect(w.moves.map((m) => [m.previous, m.session])).toEqual([['s1', 's2'], ['s2', 's3']])
    const at = (type: string) => w.reports.findIndex((r) => r.session === 's2' && r.turns.some((t) => t.type === type && t.turn === P2))
    const [first, second] = w.moves.map((m) => m.after)
    expect([at('started') >= (first ?? Infinity), at('ended') >= 0, at('ended') < (second ?? -1)]).toEqual([true, true, true])
    await w.clock.advance(6000)
  })


  test("a /clear while an earlier move still waits for its old edges: that move goes first, then the middle conversation's edges, then the next move", async ($, on) => {
    // Held, the middle conversation's edges go only once the move to it is recorded, and the next move only after them,
    // from the conversation the first move recorded. (Core still refuses the first move if Claude's process file
    // already names the next conversation by then; the next move then goes from the one core has, as above.)
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const reported = (session: string, turn: string) =>
      w.reports.findIndex((r) => r.session === session && r.turns.some((t) => t.type === 'started' && t.turn === turn))
    await answer($, R1)
    await prompt($, P1)  // s1's, never taken in
    let answer_ = () => {}
    w.hanging.until = new Promise<void>((resolve) => { answer_ = resolve })  // core is slow to take s1's report
    w.processFile.follows = false
    w.processFile.id = 's2'
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    await prompt($, P2)  // s2's, never taken in
    w.processFile.id = 's3'
    await $.classic.SessionStart({ source: 'clear', session_id: 's3' } as never)
    for (let i = 0; i < 5; i++) await w.clock.advance(100)
    expect(w.moves).toEqual([])
    w.hanging.until = undefined
    answer_()
    for (let i = 0; i < 40 && w.moves.length < 2; i++) await w.clock.advance(100)
    await w.clock.advance(100)
    expect(w.moves.map((m) => [m.previous, m.session])).toEqual([['s1', 's2'], ['s2', 's3']])
    const [first, second] = w.moves.map((m) => m.after)
    expect(reported('s1', P1)).toBeLessThan(first ?? -1)
    expect(reported('s2', P2)).toBeGreaterThanOrEqual(first ?? Infinity)
    expect(reported('s2', P2)).toBeLessThan(second ?? -1)
  })

  test("a local command mv2 ran, stored as stamped user rows, is one command turn named by its run", async ($, on) => {
    // mv2 slash's run of such a command stores the same rows stamped with the multiverse plugin's origin. Its caveat
    // opens the command turn, but only the '<command-name>' row after it names the run, so the start waits for that
    // row, even one still being stored, and core can join the turn to mv2's slash record.
    const w = world(on)
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    const C2 = 'c2222222-2222-4222-8222-222222222222'
    const C3 = 'c3333333-3333-4333-8333-333333333333'
    const ours = { kind: 'plugin', name: 'multiverse' }
    await answer($, R1)
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 command id=${RUN}>>${JSON.stringify({ command: 'context', args: '' })}<</MV2>>` })
    await w.clock.settle()
    // Both rows still being stored, the caveat noted first: its start waits for the row that names the run.
    const heldCaveat = w.hold(C1)
    const opening = row($, C1, 'command', ours, { type: 'user', isMeta: true, content: [{ type: 'text', text: '<local-command-caveat>Caveat</local-command-caveat>' }] })
    await heldCaveat.reached
    const held = w.hold(C2)
    const naming = row($, C2, 'command', ours, { type: 'user', content: [{ type: 'text', text: '<command-name>/context</command-name>' }] })
    await held.reached
    heldCaveat.release()
    await opening
    await w.clock.settle()
    expect(w.turns()).toEqual([])
    held.release()
    await naming
    await row($, C3, 'command', { kind: 'engine' }, { type: 'system', name: 'local_command', content: [{ type: 'text', text: '<local-command-stdout>Context</local-command-stdout>' }] })
    w.disk.push(C1, C2, C3)
    for (let i = 0; i < 25; i++) await w.clock.advance(100)  // its end waits, up to its bound, for the row after the output
    expect(w.turns()).toEqual([
      { type: 'started', turn: C1, kind: 'command', ref: C1, source: 'person', attempt: RUN, first_task: false },
      { type: 'ended', turn: C1, kind: 'command', ref: C3, state: 'finished' }])
  })

  test("a caveat with no '<command-name>' row after it starts its command turn with its own source", async ($, on) => {
    // Its start takes its place at once: a caveat the next row doesn't name must not hold everything behind it.
    const w = world(on)
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    await row($, C1, 'command', { kind: 'unclassified' }, { type: 'user', isMeta: true, content: [{ type: 'text', text: '<local-command-caveat>Caveat</local-command-caveat>' }] })
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await w.clock.settle()
    expect(w.turns().map((t) => [t.type, t.kind, t.turn, t.source])).toEqual([['started', 'command', C1, 'person'], ['started', 'prompt', P1, 'person']])
  })


  test("a caveat a compaction stored inside its span starts no command turn", async ($, on) => {
    // The reader keeps such a caveat as the compaction's command_output, so the /compact's '<command-name>' row stays
    // its command turn's first row; a command turn started at the caveat would name a turn the reader doesn't have.
    const w = world(on)
    const C1 = 'c1111111-1111-4111-8111-111111111111'
    const C2 = 'c2222222-2222-4222-8222-222222222222'
    const caveat = (uuid: string) => row($, uuid, 'command', { kind: 'unclassified' }, { type: 'user', isMeta: true,
      content: [{ type: 'text', text: '<local-command-caveat>Caveat</local-command-caveat>' }] })
    await answer($, R1)
    await $.session.compact({ trigger: 'manual', messages: [] } as never)
    await caveat(C1)
    await prompt($, P1)  // a real row: the span ends
    await $.turn.start({ text: 'go', turnId: 't1' })
    await caveat(C2)  // outside a span: a local command
    await row($, 'c3333333-3333-4333-8333-333333333333', 'command', { kind: 'unclassified' }, { type: 'user',
      content: [{ type: 'text', text: '<command-name>/reload-plugins</command-name>' }] })
    await w.clock.settle()
    expect(w.turns().filter((t) => t.type === 'started').map((t) => [t.kind, t.turn])).toEqual([['prompt', P1], ['command', C2]])
  })

  test("an end a /clear overtakes is still proved in its own conversation's file", async ($, on) => {
    // Its proof read the file Claude named as current, and stopped at the /clear: the end went with no row, and core
    // resolved neither its turn nor its row (qa-claude's Q-2). Claude keeps the old conversation's file beside the new.
    const w = world(on)
    const old = '/home/.claude/projects/p/s1.jsonl'
    const fresh = '/home/.claude/projects/p/s2.jsonl'
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)
    await done($, D1)
    w.files.set(old, [P1, R1, D1])
    w.files.set(fresh, [])
    w.info.transcriptPath = fresh  // Claude already names the new conversation's file
    await $.turn.complete(complete('t1') as never)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    for (let i = 0; i < 30 && !w.turns().some((t) => t.type === 'ended'); i++) await w.clock.advance(100)
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[P1, D1]])
    expect([...new Set(w.paths)]).toEqual([old])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })

  test("a turn an automatic compaction cuts mid-turn ends at its last row after the boundary, and a join after it names the turn", async ($, on) => {
    // qa-claude's shape on Claude Code 2.1.332: the compaction runs before the turn's next model call, and the history
    // then starts at its summary, without the turn's prompt row. Core keeps the start's turn for the end and the join
    // and resolves their rows on the new history, so the mod must name the turn and rows stored after the boundary
    // (its reply, its turn_duration), never only rows the history lost.
    const w = world(on)
    const [T1, B1, S1, J1, R3, D2] = ['a4444444-4444-4444-8444-444444444444', 'b1111111-1111-4111-8111-111111111111',
      'b2222222-2222-4222-8222-222222222222', 'b3333333-3333-4333-8333-333333333333', 'a6666666-6666-4666-8666-666666666666',
      'a8888888-8888-4888-8888-888888888888']
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    await answer($, R1)  // a tool call
    await row($, T1, 'tool-result', { kind: 'tool' }, { type: 'user' })
    await $.session.compact({ trigger: 'auto', messages: [{ role: 'user', text: 'Earlier', toolUses: [] }] } as never)
    await row($, J1, 'delivery', { kind: 'composer' }, { type: 'attachment', name: 'queued_command', isMeta: true })
    await answer($, R3)
    await done($, D2)
    w.disk.push(P1, R1, T1, { type: 'system', uuid: B1 }, { type: 'user', uuid: S1, parentUuid: B1 }, J1, R3, D2)
    await $.turn.complete(complete('t1') as never)
    for (let i = 0; i < 30 && !w.turns().some((t) => t.type === 'ended'); i++) await w.clock.advance(100)
    expect(w.turns().map((t) => [t.type, t.turn, t.ref])).toEqual([['started', P1, P1], ['joined', P1, J1], ['ended', P1, D2]])
  })

})
