// The status line running in a session: what it reads, when, and what it shows.
// A virtual disk and the mock clock stand in for the host; the 2 s timer is
// moved by hand.
import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

const AGENT = '/universe/state/agents/a1'
const UNIVERSE = '/universe'

function session(on: On, env: Record<string, string> = { MV2_AGENT_DIR: AGENT, MV2_UNIVERSE: UNIVERSE, MV2_CLI: '/mv2' }) {
  mock.env(on, env)
  const clock = mock.clock(on)
  // Claude's core for the events these tests raise.
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => ({ text: e.answer }))
  on('ui.log', async ($, e) => { logs.push(e.text); return { value: undefined } })
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  // The disk: a file changes when it is set again, and its time moves on.
  const files = new Map<string, { text: string; mtimeMs: number }>()
  let version = 1
  const disk = (path: string, text: string | undefined) => {
    if (text === undefined) files.delete(path)
    else files.set(path, { text, mtimeMs: version++ })
  }
  on('fs.stat', async ($, e) => {
    const file = files.get(e.path)
    if (!file) return { deny: 'ENOENT' }
    return { value: { kind: 'file', size: file.text.length, mtimeMs: file.mtimeMs, isLink: false } }
  })
  on('fs.read', async ($, e) => {
    const file = files.get(e.path)
    return file ? { value: file.text } : { deny: 'ENOENT' }
  })
  const peeks: string[][] = []
  let unread: unknown[] = []
  on('process.run', async ($, e) => {
    let stdout = ''
    if (e.argv[1] === 'inbox') {
      peeks.push([...e.argv])
      stdout = JSON.stringify({ messages: unread, cursor: 0 })
    }
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  const lines: (string | undefined)[] = []
  on('ui.status', async ($, e) => { lines.push(e.text); return { value: undefined } })
  on('ui.toast', async () => ({ value: undefined }))
  const logs: string[] = []
  return { clock, disk, peeks, lines, logs, setUnread: (messages: unknown[]) => { unread = messages } }
}

const properties = (phase: string, summary = '') => JSON.stringify({ id: 'a1', name: 'reviewer', phase, summary })
const start = ($: { session: { start: (input: never) => Promise<unknown> } }) =>
  $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true } as never)

describe('status line', () => {
  test('it shows the reported phase at session start', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    w.disk(`${UNIVERSE}/universe.json`, JSON.stringify({ name: 'pr-review' }))
    await start($)
    await w.clock.settle()
    expect(w.lines).toEqual(['working'])
  })

  test('the timer notices a changed phase and does nothing when nothing changed', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    await start($)
    await w.clock.settle()
    await w.clock.advance(2000)
    await w.clock.advance(2000)
    expect(w.lines).toEqual(['working'])  // unchanged ticks never touch the screen
    w.disk(`${AGENT}/properties.json`, properties('done', 'merged the PR'))
    await w.clock.advance(2000)
    expect(w.lines).toEqual(['working', 'done'])
    expect(w.peeks.length).toBe(1)  // only the first look counted the inbox; a phase change never does
  })

  test('an inbox change is counted with one peek, and only when the files changed', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    await start($)
    await w.clock.settle()
    expect(w.peeks).toEqual([['/mv2', 'inbox', '--peek']])  // the first look, before anything is unread
    expect(w.lines.at(-1)).toBe('working')
    w.setUnread([{ text: 'a' }, { text: 'b' }])
    w.disk(`${AGENT}/inbox.jsonl`, '{"id":"1"}\n{"id":"2"}\n')
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBe('working · inbox 2')
    expect(w.peeks.length).toBe(2)
    await w.clock.advance(2000)
    await w.clock.advance(2000)
    expect(w.peeks.length).toBe(2)  // unchanged files, no more subprocess
    // The agent reads its inbox: the cursor file changes and the count drops.
    w.setUnread([])
    w.disk(`${AGENT}/inbox.cursor`, '40')
    await w.clock.advance(2000)
    expect(w.peeks.length).toBe(3)
    expect(w.lines.at(-1)).toBe('working')
  })

  test('statusline.json sets the items and their order, and a change applies on the next tick', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working', 'reading the diff'))
    w.disk(`${UNIVERSE}/universe.json`, JSON.stringify({ name: 'pr-review' }))
    w.disk(`${UNIVERSE}/statusline.json`, '{"schema": 1, "items": {"agent": true, "summary": true, "inbox": false}}')
    await start($)
    await w.clock.settle()
    expect(w.lines).toEqual(['reviewer (pr-review) · working: reading the diff'])
    w.disk(`${UNIVERSE}/statusline.json`, '{"schema": 1, "items": {"phase": false, "summary": false}}')
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBeUndefined()  // nothing left to show: the status is cleared
    w.disk(`${UNIVERSE}/statusline.json`, '{broken')
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBe('working')  // an unusable file means the defaults
    expect(w.logs.some((line) => line.includes('not valid JSON'))).toBe(true)
  })

  test('a running /compact’s progress keeps the slot while the timer ticks, and the normal line returns after', async ($, on) => {
    const w = session(on, { MV2_AGENT_DIR: AGENT, MV2_UNIVERSE: UNIVERSE, MV2_CLI: '/mv2', MV2_PYTHON: '/python', MV2_COMPACTION: '1' })
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const event = (value: object) => ({ stream: 'stdout' as const, text: JSON.stringify(value) + '\n' })
    on('process.spawn', async function* () {
      yield event({ event: 'progress', phase: 'building', turn: 'turn-aaaaaaaa', turns: 2, turns_done: 0, merges: 1, merges_left: 3 })
      await gate
      yield event({ event: 'message', message: { keep: 'row-1' } })
      yield event({ event: 'result', count: 1, install_id: 'install-1', path: '/agent/compaction/installs/install-1.json',
                    stats: { turns: 1, summarized: 0, tokens_before: 10, tokens_after: 5 } })
      return { value: { code: 0, signal: null } }
    })
    on('session.compact', async () => ({ messages: [] }))
    w.disk(`${AGENT}/properties.json`, properties('working'))
    await start($)
    await w.clock.settle()
    expect(w.lines.at(-1)).toBe('working')
    const compacting = $.session.compact({ trigger: 'manual', messages: [{ role: 'user', text: 'Fix it', toolUses: [], handle: 'row-1' }] })
    await w.clock.settle()
    expect(w.lines.at(-1)).toContain('compacting')
    // The phase changes and the timer ticks while it runs: the progress keeps the slot.
    w.disk(`${AGENT}/properties.json`, properties('blocked'))
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toContain('compacting')
    release()
    await compacting
    await w.clock.settle()
    // The normal line is back, as of the tick during compaction, now with compaction's own item.
    expect(w.lines.at(-1)).toBe('blocked · compaction on')
  })

  // compaction.ts takes "enabled" from the launch's MV2_COMPACTION, which cannot change in a running
  // process, so an agent launched without it has no compaction state to show.
  test('an agent launched without compaction shows no compaction item, even when the config asks for it', async ($, on) => {
    const w = session(on)  // no MV2_COMPACTION in this launch
    w.disk(`${AGENT}/properties.json`, properties('working'))
    w.disk(`${UNIVERSE}/statusline.json`, '{"schema": 1, "items": {"compaction": true}}')
    await start($)
    await w.clock.settle()
    await w.clock.advance(2000)
    expect(w.lines).toEqual(['working'])
  })

  test('outside a Multiverse agent there is no line and no timer work', async ($, on) => {
    const w = session(on, {})
    await start($)
    await w.clock.settle()
    await w.clock.advance(4000)
    expect(w.lines).toEqual([])
    expect(w.peeks).toEqual([])
  })

  test('a reload starts again: it shows the line even if it is the same as before', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    await start($)
    await w.clock.settle()
    await start($)
    await w.clock.settle()
    expect(w.lines).toEqual(['working', 'working'])
  })
})
