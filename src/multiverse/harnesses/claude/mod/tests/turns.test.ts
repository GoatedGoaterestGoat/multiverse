// Offline tests of the turn events the mod reports (hooks/turns.ts, with index.ts's reports): `claude plugin test`.
// The test's hooks stand in for Claude's core and the host: the status reports, with the turn events each carries,
// and the transcript file, as the row ids the mod finds at its end (the test decides when a row lands there).
import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

const STEER = '3f2a0c1e-1111-4222-8333-944455556661'
const RUN = '7a1b2c3d-1111-4222-8333-944455556671'  // an 'mv2 slash' run: its slash record's id
type Report = { status: string; turns: Record<string, unknown>[]; at: number; session: string | null }

// `slow`: each read of the transcript file takes all the time it is allowed. `heldInfo`: Claude never answers
// $.session.info().
function world(on: On, env: Record<string, string> = {}, slow = false, heldInfo = false) {
  mock.env(on, { MV2_PYTHON: '/python', MV2_AGENT_DIR: '/agent', MV2_LAUNCH_ID: 'launch-1', ...env })
  const clock = mock.clock(on, { now: 0 })
  // Claude's own part of session.start and turn.complete; `heldHooks`, when set, holds it (the mod's hook resumes later).
  const heldHooks: { start?: Promise<void>; complete?: Promise<void> } = {}
  on('session.start', async ($, e) => { if (heldHooks.start !== undefined) await heldHooks.start; return { cwd: e.cwd } })
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => { if (heldHooks.complete !== undefined) await heldHooks.complete; return { text: e.answer } })
  on('session.receive', async ($, e) => ({ text: e.text }))
  on('classic.SessionStart', async () => ({}))
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
  const after = { text: '' }  // anything after them: a record still being written, say
  const reports: Report[] = []
  const reads: number[] = []  // each read's allowed time
  // A slow read returns once the test has moved the clock past the time it was allowed (release()).
  const held: { until: number; wake: () => void }[] = []
  const release = () => { for (const read of held.splice(0)) if (read.until <= clock.now()) read.wake(); else held.push(read) }
  const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  on('process.run', async ($, e) => {
    const at = e.argv.indexOf('--status')
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
      if (slow) await new Promise<void>((wake) => held.push({ until: clock.now() + allowed, wake }))
      return ok(disk.map((entry) => JSON.stringify(typeof entry === 'string' ? { type: 'user', uuid: entry } : entry) + '\n').join('') + after.text)
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
  return { clock, disk, after, reports, reads, turns, release, hold, idHeld, heldHooks, info }
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
    expect([ended?.session, ended?.turns]).toEqual(['s1', [{ type: 'ended', turn: P1, kind: 'prompt', ref: null, state: 'finished' }]])
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
    expect(w.turns().filter((t) => t.type === 'ended').map((t) => [t.turn, t.ref])).toEqual([[C1, C3], [C4, null]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a join whose append never returns gives up its place when its turn ends, and never holds up a new conversation", async ($, on) => {
    // An unfilled join had no deadline: it held its turn's end, and after a /clear every new turn's edges, for good.
    const w = world(on)
    const J1 = 'b1111111-1111-4111-8111-111111111111'
    await prompt($, P1)
    await $.turn.start({ text: 'go', turnId: 't1' })
    const held = w.hold(J1)
    void row($, J1, 'delivery', { kind: 'composer' }, { type: 'attachment', name: 'queued_command', isMeta: true })
    await held.reached
    await done($, D1)
    w.disk.push(P1, J1, D1)
    await $.turn.complete(complete('t1') as never)
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    await prompt($, P2)
    await $.turn.start({ text: 'next', turnId: 't2' })
    await w.clock.advance(100)
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn])))
      .toEqual([['s1', 'started', P1], ['s2', 'started', P2]])  // the new conversation's start isn't held
    for (let i = 0; i < 25; i++) await w.clock.advance(100)
    expect(w.reports.flatMap((r) => r.turns.map((t) => [r.session, t.type, t.turn])))
      .toEqual([['s1', 'started', P1], ['s2', 'started', P2], ['s1', 'ended', P1]])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
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
    // In one conversation too: an older prompt's row noted after a newer one's never takes the newer turn.
    const P3 = 'a7777777-7777-4777-8777-777777777777'
    const P4 = 'a8888888-8888-4888-8888-888888888888'
    const heldOlder = w.hold(P3)
    const older = prompt($, P3)
    await heldOlder.reached
    await prompt($, P4)
    heldOlder.release()
    await older
    await $.turn.start({ text: 'newer', turnId: 't3' })
    await w.clock.settle()
    expect(w.turns().filter((t) => t.type === 'started').map((t) => t.turn)).toEqual([P2, P4])
    await w.clock.advance(6000)  // the /clear's own wait for Claude's process file runs out before the test ends
  })


  test("a /clear leaves nothing of the old conversation to open a turn: a prompt stored but not started, or a row from before its session was known", async ($, on) => {
    // An opener stored but unused before a /clear started a turn in the new conversation with no input of its own; a
    // row that arrived before the session id was known couldn't be told old from new by its session alone.
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
    expect(w.turns().filter((t) => t.type === 'started')).toEqual([])
    await w.clock.advance(6000)  // the /clears' own waits for Claude's process file run out before the test ends
  })

  test("a row older than an opening or command already used never becomes one", async ($, on) => {
    // An older prompt row noted after a newer one was used started a false turn at the next continuation; the same
    // for a local command's row.
    const w = world(on)
    const P2 = 'a5555555-5555-4555-8555-555555555555'
    const held = w.hold(P1)
    const older = prompt($, P1)
    await held.reached
    await prompt($, P2)
    await $.turn.start({ text: 'newer', turnId: 't1' })
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

})
