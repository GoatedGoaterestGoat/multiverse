// Offline tests of the mod's part of `mv2 slash`: Claude's command list and one command run for mv2.
// The test's hooks stand in for Claude's core: its command list, its command runs (held where a test needs a run
// to wait), the session's model and ID, and the files the mod writes.
import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

const RUN = '7a1b2c3d-1111-4222-8333-944455556671'
const LIST = '7a1b2c3d-1111-4222-8333-944455556672'

type Ran = { command: string; args: string }

function world(on: On, options: { run?: (ran: Ran) => Promise<{ text?: string }>; model?: string; list?: Promise<void>;
  settings?: Record<string, unknown>; rows?: { role: 'user' | 'assistant'; text: string }[];
  rowsHold?: { next?: Promise<void> }; peerHold?: { until?: Promise<void> } } = {}) {
  mock.env(on, { MV2_PYTHON: '/python', MV2_AGENT_DIR: '/agent' })
  const clock = mock.clock(on, { now: 0 })
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => ({ text: e.answer }))
  // Claude's queue, as mv2's entries reach it: each command run, each submitted prompt and each peer prompt, in order.
  const queue: string[] = []
  // A peer prompt reaches the queue as its hooks return; `peerHold.until` keeps that from happening at once.
  on('session.receive', async ($, e) => { await options.peerHold?.until; queue.push(`peer:${e.text}`); return { text: e.text } })
  const files = new Map<string, string>()
  on('fs.write', async ($, e) => { files.set(e.path, e.text); return { value: undefined } })
  on('process.run', async () => ({ value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  on('session.id', async () => ({ value: '11111111-1111-4111-8111-111111111111' }))
  on('session.model', async () => ({ value: options.model ?? 'claude-haiku-5-5' }))
  // A read of the conversation, held when the test asks for it to answer late (it answers with the rows by then).
  on('session.messages', async () => {
    const hold = options.rowsHold?.next
    if (options.rowsHold) options.rowsHold.next = undefined
    if (hold) await hold
    return { value: (options.rows ?? []).map((row) => ({ ...row, toolUses: [] })) }
  })
  on('ui.log', async () => ({ value: undefined }))
  on('prompt.submit', async ($, e) => { queue.push(`submit:${e.text}`); return { text: e.text, origin: e.origin } })
  on('settings.read', async () => ({ value: options.settings ?? {} }))
  on('command.list', async () => { if (options.list !== undefined) await options.list; return { value: [
    { name: 'compact', description: 'Free up context', source: 'builtin' },
    { name: 'exit', description: 'Exit the CLI', source: 'builtin' },
    { name: 'review', description: 'Review the change', source: 'user' },
    { name: 'other', description: 'Another prompt', source: 'user' },
    { name: 'effort', description: 'Set effort level', source: 'builtin' },
  ] } })
  const ran: Ran[] = []
  on('command.run', async ($, e) => {
    ran.push({ command: e.command, args: e.args })
    queue.push(`run:${e.command}`)
    return options.run !== undefined ? options.run({ command: e.command, args: e.args }) : { text: `ran /${e.command}` }
  })
  return { clock, files, ran, queue }
}

const frame = (mode: 'command' | 'commands', id: string, body: unknown) =>
  `<<MV2 ${mode} id=${id}>>${JSON.stringify(body)}<</MV2>>`

// A row reaching session.append from `origin`: our own command run's (the multiverse plugin's) or anyone else's.
async function append($: { session: { append: (input: never) => Promise<unknown> } }, text: string, origin: Record<string, unknown>) {
  try {
    await $.session.append({ message: { type: 'user', role: 'user', content: [{ type: 'text', text }] },
      door: 'command', origin, uuid: `row-${Math.random()}` } as never)
  } catch (error) {
    if (!String(error).includes('no implementation for session.append')) throw error
  }
}
const OURS = { kind: 'plugin', name: 'multiverse' }
const ENGINE = { kind: 'engine' }  // how Claude files the person's own typed command

const record = (files: Map<string, string>, id: string) => JSON.parse(files.get(`/agent/claude-commands/${id}.json`) ?? 'null')

describe('commands for mv2', () => {
  // The frame is mv2's request to the mod: shown to the model, it would read as a message from mv2 asking it to run
  // a command, and a run inside the receive hook would wait on itself. Both break unnoticed.
  test('a command frame is consumed unseen, and the command runs after the hook with its result recorded', async ($, on) => {
    const w = world(on, { model: 'claude-haiku-4-5' })
    await $.session.start({ cwd: '/work' } as never)
    const received = await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', RUN, { command: 'compact', args: 'keep the plan' }) })
    expect(received).toEqual({ consumed: expect.any(String) })
    expect(w.ran).toEqual([])  // not inside the hook
    await w.clock.settle()
    expect(w.ran).toEqual([{ command: 'compact', args: 'keep the plan' }])
    expect(record(w.files, RUN)).toEqual({ id: RUN, state: 'ran', text: 'ran /compact', prompted: false,
      model: 'claude-haiku-4-5', session_id: '11111111-1111-4111-8111-111111111111', at: expect.any(Number) })
  })

  // Claude's run resolves aliases itself (`quit` runs /exit and ends the session), and a plugin's own command.run hook
  // doesn't see its own runs, so the mod's exact-name check is the only thing between an alias and the session.
  test('only a name exactly as Claude lists it runs, and mv2-managed names never do', async ($, on) => {
    const w = world(on)
    await $.session.start({ cwd: '/work' } as never)
    const cases: [string, string][] = [['7a1b2c3d-1111-4222-8333-944455556681', 'quit'], ['7a1b2c3d-1111-4222-8333-944455556682', 'exit'],
      ['7a1b2c3d-1111-4222-8333-944455556683', 'compact x'], ['7a1b2c3d-1111-4222-8333-944455556684', '']]
    for (const [id, command] of cases) {
      await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', id, { command, args: '' }) })
      await w.clock.settle()
      expect(record(w.files, id)).toEqual({ id, state: 'refused', reason: expect.any(String), at: expect.any(Number) })
    }
    expect(w.ran).toEqual([])
  })

  // A prompt-type command's run resolves when its turn starts, with no text: without the turn it would read as a
  // command that printed nothing, and mv2 would show an empty result instead of saying the agent is answering.
  test('a command that expands into a prompt is recorded as prompted; one that only prints nothing is not', async ($, on) => {
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => { release = resolve })
    // Claude 2.1.316 answers a run that printed nothing with text "", not with no text.
    const w = world(on, { run: async (ran) => { if (ran.command === 'review') await held; return { text: '' } } })
    await $.session.start({ cwd: '/work' } as never)
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', RUN, { command: 'review', args: '' }) })
    await w.clock.settle()
    await $.turn.start({ text: '<command-message>review</command-message>\n<command-name>/review</command-name>', turnId: 't1' } as never)
    release()
    await w.clock.settle()
    expect(record(w.files, RUN)).toMatchObject({ state: 'ran', text: null, prompted: true })
    // Live on 2.1.316 the prompt's turn usually starts just AFTER the run resolves: that counts too.
    const after = '7a1b2c3d-1111-4222-8333-944455556674'
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', after, { command: 'review', args: '' }) })
    release()
    await w.clock.advance(100)
    await $.turn.start({ text: '<command-message>review</command-message>\n<command-name>/review</command-name>', turnId: 't2' } as never)
    await w.clock.settle()
    expect(record(w.files, after)).toMatchObject({ state: 'ran', text: null, prompted: true })
    const quiet = '7a1b2c3d-1111-4222-8333-944455556673'
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', quiet, { command: 'compact', args: '' }) })
    await w.clock.settle()
    expect(record(w.files, quiet)).toBeNull()  // no output and no turn yet: it waits a moment for a prompt's turn
    await w.clock.advance(2000)
    await w.clock.settle()
    expect(record(w.files, quiet)).toMatchObject({ state: 'ran', text: null, prompted: false })
  })

  // A rejected run (Claude refuses it past a Stop, say) must reach mv2 as failed, or mv2 waits out its timeout and
  // reports a command still queued that never will run.
  test('a run Claude rejects is recorded as failed with its reason', async ($, on) => {
    const w = world(on, { run: async () => { throw new Error('$.command.run: past a Stop') } })
    await $.session.start({ cwd: '/work' } as never)
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', RUN, { command: 'compact', args: '' }) })
    await w.clock.settle()
    // The kit can't make Claude reject a run (a stand-in hook that throws is skipped), so the rejection reaches the mod
    // as the kit's own error: either way the run is recorded as failed, with a reason.
    expect(record(w.files, RUN)).toEqual({ id: RUN, state: 'failed', reason: expect.any(String), at: expect.any(Number) })
  })

  // The list is what mv2 refuses against and shows: a frame answered as a turn, or a list missing the source, would
  // break both.
  test("the list frame records Claude's own list with each command's source", async ($, on) => {
    const w = world(on)
    await $.session.start({ cwd: '/work' } as never)
    const received = await $.session.receive({ origin: { kind: 'peer' }, text: frame('commands', LIST, {}) })
    expect(received).toEqual({ consumed: expect.any(String) })
    await w.clock.settle()
    expect(record(w.files, LIST).commands.slice(0, 3)).toEqual([
      { name: 'compact', description: 'Free up context', source: 'builtin', plugin: null },
      { name: 'exit', description: 'Exit the CLI', source: 'builtin', plugin: null },
      { name: 'review', description: 'Review the change', source: 'user', plugin: null }])
    expect(w.ran).toEqual([])
  })

  // Commands and follow-ups share Claude's first-in, first-out queue (the design's order rule). The mod checks a
  // command against Claude's list before running it, so without its lane a follow-up that arrived after the command
  // would reach Claude's queue first, and run before it, unnoticed. After an Esc the follow-up is a peer prompt that
  // only its own receive hook can queue, so that hook waits for the earlier check and later entries wait for it.
  test('a command enters Claude\'s queue before a follow-up that reached the mod after it', async ($, on) => {
    let release: () => void = () => {}
    const slowList = new Promise<void>((resolve) => { release = resolve })
    const w = world(on, { list: slowList })
    await $.session.start({ cwd: '/work' } as never)
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', RUN, { command: 'compact', args: '' }) })
    await $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=7a1b2c3d-1111-4222-8333-944455556690>>Then this.<</MV2>>` })
    await w.clock.advance(10)
    expect(w.queue).toEqual([])  // the follow-up waits for the command's check
    release()
    await w.clock.settle()
    expect(w.queue).toEqual(['run:compact', 'submit:Then this.'])
  })

  test('after an Esc a follow-up, now a peer prompt, still keeps its place between commands', async ($, on) => {
    let release: () => void = () => {}
    let queued: () => void = () => {}
    const peerHold: { until?: Promise<void> } = {}
    const w = world(on, { list: new Promise<void>((resolve) => { release = resolve }), peerHold })
    await $.session.start({ cwd: '/work' } as never)
    const follow = (text: string) => `<<MV2 follow-up id=7a1b2c3d-1111-4222-8333-944455556705>>${text}<</MV2>>`
    await $.turn.complete({ turnId: 't1', answer: '', durationMs: 1, isAborted: true, reason: 'aborted' } as never)
    // An earlier command still being checked, the follow-up, then a later command behind it.
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', '7a1b2c3d-1111-4222-8333-944455556703', { command: 'compact', args: 'one' }) })
    await w.clock.settle()
    const pending = $.session.receive({ origin: { kind: 'peer' }, text: follow('After the Esc.') })
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', '7a1b2c3d-1111-4222-8333-944455556704', { command: 'compact', args: 'two' }) })
    await w.clock.advance(10)
    expect(w.queue).toEqual([])  // the peer prompt waits in its hook for the earlier check
    // From here the peer prompt reaches the queue only when let go, as Claude queues it once its hooks return.
    peerHold.until = new Promise<void>((resolve) => { queued = resolve })
    release()
    await w.clock.advance(10)
    expect(w.queue).toEqual(['run:compact'])  // the later command waits until the peer prompt is in
    queued()
    await pending
    await w.clock.settle()
    expect(w.queue).toEqual(['run:compact', 'peer:After the Esc.', 'run:compact'])
  })

  // The lane holds a command only until its run is handed to Claude, never until the run ends: a command waiting
  // for the session, or for a turn of its own, would otherwise keep the peer prompt in its hook until the bound.
  test("after an Esc a follow-up waits for an earlier command's check, not for its run", async ($, on) => {
    const w = world(on, { run: () => new Promise(() => {}) })  // the command's run never ends
    await $.session.start({ cwd: '/work' } as never)
    await $.turn.complete({ turnId: 't1', answer: '', durationMs: 1, isAborted: true, reason: 'aborted' } as never)
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', '7a1b2c3d-1111-4222-8333-944455556708', { command: 'compact', args: '' }) })
    await w.clock.settle()
    const pending = $.session.receive({ origin: { kind: 'peer' }, text: '<<MV2 follow-up id=7a1b2c3d-1111-4222-8333-944455556709>>Not held.<</MV2>>' })
    await w.clock.advance(10)
    await pending
    expect(w.queue).toEqual(['run:compact', 'peer:Not held.'])
  })

  // The peer prompt waits inside its receive hook, so a check that doesn't answer must not keep it there for good;
  // nor may the wait's end let it overtake the command: it is kept out and reported not delivered, for --retry.
  test('after an Esc a follow-up waits for an earlier check only so long, and never overtakes it', async ($, on) => {
    let release: () => void = () => {}
    const w = world(on, { list: new Promise<void>((resolve) => { release = resolve }) })
    await $.session.start({ cwd: '/work' } as never)
    await $.turn.complete({ turnId: 't1', answer: '', durationMs: 1, isAborted: true, reason: 'aborted' } as never)
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', '7a1b2c3d-1111-4222-8333-944455556706', { command: 'compact', args: '' }) })
    await w.clock.settle()
    const late = '7a1b2c3d-1111-4222-8333-944455556707'
    const pending = $.session.receive({ origin: { kind: 'peer' }, text: `<<MV2 follow-up id=${late}>>Too late.<</MV2>>` })
    await w.clock.advance(1990)
    expect(w.queue).toEqual([])
    await w.clock.advance(20)
    expect(await pending).toEqual({ consumed: expect.any(String) })
    await w.clock.settle()
    expect(JSON.parse(w.files.get(`/agent/claude-receipts/${late}.not-delivered.json`) ?? 'null'))
      .toMatchObject({ id: late, delivered: false, definite: true, reason: expect.stringContaining('would have run before it') })
    release()
    await w.clock.settle()
    expect(w.queue).toEqual(['run:compact'])  // the held-back message never enters Claude's queue, even now
    // The idle wait for a stored row, which writes an unsure refusal, never replaces this definite one.
    await w.clock.advance(10000)
    await w.clock.settle()
    expect(JSON.parse(w.files.get(`/agent/claude-receipts/${late}.not-delivered.json`) ?? 'null'))
      .toMatchObject({ definite: true, reason: expect.stringContaining('would have run before it') })
    expect(w.queue).toEqual(['run:compact'])
  })

  // Two commands can wait behind one busy turn: each prompt's turn must be credited to its own command, or the first
  // would read as printing nothing and the second take its turn.
  test('two commands waiting at once: the prompt one is credited with its turn, the other is not', async ($, on) => {
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => { release = resolve })
    // /review becomes a prompt; /other prints and starts no turn.
    const w = world(on, { run: async (ran) => { await held; return { text: ran.command === 'other' ? 'printed' : '' } } })
    await $.session.start({ cwd: '/work' } as never)
    const second = '7a1b2c3d-1111-4222-8333-944455556675'
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', RUN, { command: 'review', args: '' }) })
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', second, { command: 'other', args: '' }) })
    await w.clock.settle()
    await $.turn.start({ text: '<command-message>review</command-message>\n<command-name>/review</command-name>', turnId: 't1' } as never)
    release()
    await w.clock.advance(2000)
    await w.clock.settle()
    expect(record(w.files, RUN)).toMatchObject({ text: null, prompted: true })
    expect(record(w.files, second)).toMatchObject({ text: 'printed', prompted: false })
  })

  // Claude has no call that reads the effort: /effort's own saved setting is the only confirmation, and no other
  // command may report one (mv2 would record an effort nothing confirmed).
  test('only /effort reports an effort, read back from what Claude saved for the model', async ($, on) => {
    const w = world(on, { settings: { modelSettings: { 'claude-haiku-5-5': { effortLevel: 'low' } } } })
    await $.session.start({ cwd: '/work' } as never)
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', RUN, { command: 'effort', args: 'low' }) })
    const plain = '7a1b2c3d-1111-4222-8333-944455556676'
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', plain, { command: 'compact', args: '' }) })
    await w.clock.settle()
    expect(record(w.files, RUN)).toMatchObject({ state: 'ran', effort: 'low', model: 'claude-haiku-5-5' })
    expect('effort' in record(w.files, plain)).toBe(false)
  })

  // /compact and /effort show their line only on screen: Claude 2.1.316 answers their run with no text and stores the
  // line as the row after the command's own, /compact's past session.append. Without it mv2 says "ran with no
  // output", and an error line reads as a command that ran; crediting a line that isn't this run's would be worse.
  test("the line Claude stores for this run is its output, an error line fails it, and no other run's line counts", async ($, on) => {
    const named = (name: string, args = '') =>
      `<command-name>/${name}</command-name>\n            <command-message>${name}</command-message>\n            <command-args>${args}</command-args>`
    const user = (text: string) => ({ role: 'user' as const, text })
    const COMPACTED = '<local-command-stdout>\x1b[2mCompacted (ctrl+o to see full summary)\x1b[22m</local-command-stdout>'
    const rows: { role: 'user' | 'assistant'; text: string }[] = [{ role: 'assistant', text: 'APPLE' }]
    // Claude's core in this test: each run's own row as it starts (the multiverse plugin's), a run held until
    // released, and what it stores as it resolves.
    const starts = new Map<string, () => Promise<void>>()
    const stores = new Map<string, () => void>()
    const holds = new Map<string, Promise<void>>()
    const w = world(on, { rows, run: async (ran) => {
      const key = `${ran.command} ${ran.args}`
      await (starts.get(key) ?? (() => append($, ran.args ? `/${ran.command} ${ran.args}` : `/${ran.command}`, OURS)))()
      await holds.get(key)
      stores.get(key)?.()
      return ran.command === 'review' ? { text: 'reviewed' } : {}
    } })
    await $.session.start({ cwd: '/work' } as never)
    const run = async (id: string, command: string, args = '') => {
      await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', id, { command, args }) })
      await w.clock.settle()
    }
    const done = async () => { await w.clock.advance(2000); await w.clock.settle() }
    // Stored as the run resolves, ANSI codes and all.
    stores.set('compact ', () => rows.push(user(named('compact')), user(COMPACTED)))
    await run('7a1b2c3d-1111-4222-8333-944455556691', 'compact')
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556691')).toMatchObject({ state: 'ran', text: 'Compacted (ctrl+o to see full summary)', prompted: false })
    // The same command again, resolving without storing anything: the line at the end is the earlier run's.
    stores.delete('compact ')
    await run('7a1b2c3d-1111-4222-8333-944455556697', 'compact')
    await done()
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556697')).toMatchObject({ state: 'ran', text: null })
    // Stored just after the run resolves: read again once the wait for a prompt's turn is over. /effort's own row is
    // its <command-name> row, under the multiverse plugin's name.
    starts.set('effort low', () => append($, named('effort', 'low'), OURS))
    stores.set('effort low', () => rows.push(user(named('effort', 'low'))))
    await run('7a1b2c3d-1111-4222-8333-944455556693', 'effort', 'low')
    rows.push(user('<local-command-stdout>Set effort level to low (saved as your default for new sessions): Quick</local-command-stdout>'))
    await done()
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556693')).toMatchObject({ state: 'ran', text: 'Set effort level to low (saved as your default for new sessions): Quick' })
    // An error line fails the command.
    stores.set('compact ', () => rows.push(user(named('compact')), user('<local-command-stderr>Error: compaction failed</local-command-stderr>')))
    await run('7a1b2c3d-1111-4222-8333-944455556692', 'compact')
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556692')).toMatchObject({ state: 'failed', reason: 'Error: compaction failed' })
    // Not credited: another command's line (the person's own /context), and a line that is no longer last.
    stores.set('compact ', () => rows.push(user(named('context')), user('<local-command-stdout>Context usage: 4%</local-command-stdout>')))
    await run('7a1b2c3d-1111-4222-8333-944455556694', 'compact')
    await done()
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556694')).toMatchObject({ state: 'ran', text: null })
    stores.set('compact ', () => rows.push(user(named('compact')), user(COMPACTED), user('Message from user:\nnext')))
    await run('7a1b2c3d-1111-4222-8333-944455556696', 'compact')
    await done()
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556696')).toMatchObject({ state: 'ran', text: null })
    // Two runs of one command waiting: the first never reads the end of the conversation, which may be the second's.
    let release: () => void = () => {}
    holds.set('compact first', new Promise<void>((resolve) => { release = resolve }))
    stores.set('compact second', () => rows.push(user(named('compact', 'second')), user('<local-command-stdout>second</local-command-stdout>')))
    await run('7a1b2c3d-1111-4222-8333-944455556698', 'compact', 'first')
    await run('7a1b2c3d-1111-4222-8333-944455556699', 'compact', 'second')
    release()
    await done()
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556698')).toMatchObject({ state: 'ran', text: null })
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556699')).toMatchObject({ state: 'ran', text: 'second' })
    // Not credited either: a line at the end while this run's own row never came, and the person's own run of the
    // same command during the wait (Claude files the person's row as the engine's), its output or its error.
    stores.set('compact ', () => rows.push(user(named('compact')), user(COMPACTED)))
    starts.set('compact ', async () => {})
    await run('7a1b2c3d-1111-4222-8333-944455556710', 'compact')
    await done()
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556710')).toMatchObject({ state: 'ran', text: null })
    starts.delete('compact ')
    stores.delete('compact ')
    for (const [id, line] of [['7a1b2c3d-1111-4222-8333-944455556711', '<local-command-stdout>PERSON_RESULT</local-command-stdout>'],
      ['7a1b2c3d-1111-4222-8333-944455556712', '<local-command-stderr>Error: the person\'s compact failed</local-command-stderr>']] as const) {
      await run(id, 'compact')  // resolves storing nothing; the person's /compact then runs during the wait
      await append($, '/compact', ENGINE)
      rows.push(user(named('compact')), user(line))
      await done()
      expect(record(w.files, id)).toMatchObject({ state: 'ran', text: null })
    }
    // Claude's own text wins over any stored line.
    await run('7a1b2c3d-1111-4222-8333-944455556695', 'review')
    expect(record(w.files, '7a1b2c3d-1111-4222-8333-944455556695')).toMatchObject({ state: 'ran', text: 'reviewed' })
  })

  // The read of the conversation is a call that can answer late: a later run of the same command handed over and
  // finished meanwhile has put its own line at the end, which the earlier run must not take.
  test("a later run of the same command handed over during the earlier run's read takes its line from it", async ($, on) => {
    const user = (text: string) => ({ role: 'user' as const, text })
    const rows: { role: 'user' | 'assistant'; text: string }[] = [{ role: 'assistant', text: 'APPLE' }]
    const rowsHold: { next?: Promise<void> } = {}
    let release: () => void = () => {}
    const w = world(on, { rows, rowsHold, run: async (ran) => {
      await append($, `/compact ${ran.args}`, OURS)
      // The first run resolves at once, storing nothing; the mod's read after it answers only when released.
      if (ran.args === 'first') rowsHold.next = new Promise<void>((resolve) => { release = resolve })
      else rows.push(user('<command-name>/compact</command-name>'), user('<local-command-stdout>second</local-command-stdout>'))
      return {}
    } })
    await $.session.start({ cwd: '/work' } as never)
    const first = '7a1b2c3d-1111-4222-8333-944455556701', second = '7a1b2c3d-1111-4222-8333-944455556702'
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', first, { command: 'compact', args: 'first' }) })
    await w.clock.settle()
    await $.session.receive({ origin: { kind: 'peer' }, text: frame('command', second, { command: 'compact', args: 'second' }) })
    await w.clock.settle()
    release()
    await w.clock.advance(2000)
    await w.clock.settle()
    expect(record(w.files, first)).toMatchObject({ state: 'ran', text: null })
    expect(record(w.files, second)).toMatchObject({ state: 'ran', text: 'second' })
  })
})
