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
  const reads: string[] = []
  on('fs.read', async ($, e) => {
    reads.push(e.path)
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
  return { clock, disk, peeks, lines, logs, reads, setUnread: (messages: unknown[]) => { unread = messages } }
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
    expect(w.peeks).toEqual([['/mv2', 'inbox', '--peek', '--json']])  // the first look, before anything is unread
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
    // A root universe is left out of the agent item: the name alone.
    expect(w.lines).toEqual(['reviewer · working: reading the diff'])
    w.disk(`${UNIVERSE}/statusline.json`, '{"schema": 1, "items": {"phase": false, "summary": false}}')
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBeUndefined()  // nothing left to show: the status is cleared
    w.disk(`${UNIVERSE}/statusline.json`, '{broken')
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBe('working')  // an unusable file means the defaults
    expect(w.logs.some((line) => line.includes('not valid JSON'))).toBe(true)
  })

  test('an agent in a subverse shows the subverse beside its name, read once from universe.json', async ($, on) => {
    const SUB = '/project/.multiverse/subverses/pr-review'
    const w = session(on, { MV2_AGENT_DIR: `${SUB}/state/agents/a1`, MV2_UNIVERSE: SUB, MV2_CLI: '/mv2' })
    w.disk(`${SUB}/state/agents/a1/properties.json`, properties('working'))
    w.disk(`${SUB}/universe.json`, JSON.stringify({ name: 'PR review' }))
    w.disk(`${SUB}/statusline.json`, '{"schema": 1, "items": {"agent": true, "inbox": false}}')
    await start($)
    await w.clock.settle()
    expect(w.lines).toEqual(['reviewer (PR review) · working'])
    w.disk(`${SUB}/state/agents/a1/properties.json`, properties('done'))
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBe('reviewer (PR review) · done')
  })

  // An agent launched without compaction does not declare the item, so even a leftover file shows nothing.
  test('an agent launched without compaction shows no compaction item, even when the config asks for it', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    w.disk(`${AGENT}/runtime.json`, JSON.stringify({ state: 'running' }))
    w.disk(`${AGENT}/status-items/compaction.txt`, 'trees 9/9\n')
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

describe('plugin items on the status line', () => {
  const COST = { name: 'cost', plugin: 'cost-item', claude: true, tmux: true }
  const TESTS = { name: 'test-runs', plugin: 'ci', claude: false, tmux: true }
  const runtime = (items: unknown[], status = 'idle') => JSON.stringify({ state: 'running', status, plugins: { plugins: {}, status_items: items } })
  const item = (name: string) => `${AGENT}/status-items/${name}.txt`

  test('a declared item shows its file after the core items, and follows the file', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    w.disk(`${AGENT}/runtime.json`, runtime([COST]))
    w.disk(item('cost'), '$1.20 today')
    await start($)
    await w.clock.settle()
    expect(w.lines).toEqual(['working · $1.20 today'])
    w.disk(item('cost'), '$1.35 today')
    await w.clock.advance(2000)
    w.disk(item('cost'), undefined)  // a missing file shows nothing
    await w.clock.advance(2000)
    w.disk(item('cost'), '')         // nor does an empty one
    await w.clock.advance(2000)
    expect(w.lines).toEqual(['working · $1.20 today', 'working · $1.35 today', 'working'])
  })

  test('an item that is off is never read; statusline.json turns it on, and off again', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    w.disk(`${AGENT}/runtime.json`, runtime([TESTS]))
    w.disk(item('test-runs'), '3 failing')
    await start($)
    await w.clock.settle()
    await w.clock.advance(2000)
    expect(w.lines).toEqual(['working'])
    expect(w.reads).not.toContain(item('test-runs'))
    w.disk(`${UNIVERSE}/statusline.json`, JSON.stringify({ schema: 1, items: { 'test-runs': true, phase: true } }))
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBe('3 failing · working')  // placed where the file names it
    w.disk(`${UNIVERSE}/statusline.json`, JSON.stringify({ schema: 1, items: { 'test-runs': false } }))
    await w.clock.advance(2000)
    const read = w.reads.filter((path) => path === item('test-runs')).length
    w.disk(item('test-runs'), '4 failing')
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBe('working')
    expect(w.reads.filter((path) => path === item('test-runs')).length).toBe(read)
  })

  test('status reports rewrite runtime.json often; the item files are read only when they change', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    w.disk(`${AGENT}/runtime.json`, runtime([COST]))
    w.disk(item('cost'), '$1.20')
    await start($)
    await w.clock.settle()
    for (const status of ['working', 'idle', 'waiting', 'idle']) {
      w.disk(`${AGENT}/runtime.json`, runtime([COST], status))
      await w.clock.advance(2000)
    }
    expect(w.reads.filter((path) => path === item('cost')).length).toBe(1)
    expect(w.lines).toEqual(['working · $1.20'])
  })

  test('an item declared later is picked up; a malformed record declares none', async ($, on) => {
    const w = session(on)
    w.disk(`${AGENT}/properties.json`, properties('working'))
    w.disk(`${AGENT}/runtime.json`, '{not json')
    w.disk(item('cost'), '$1.20')
    await start($)
    await w.clock.settle()
    expect(w.lines).toEqual(['working'])
    w.disk(`${AGENT}/runtime.json`, JSON.stringify({ state: 'running', plugins: 'nonsense' }))
    await w.clock.advance(2000)
    expect(w.lines).toEqual(['working'])
    w.disk(`${AGENT}/runtime.json`, runtime([COST]))
    await w.clock.advance(2000)
    expect(w.lines).toEqual(['working', 'working · $1.20'])
    w.disk(`${AGENT}/runtime.json`, runtime([]))  // and dropped when a launch no longer declares it
    await w.clock.advance(2000)
    expect(w.lines.at(-1)).toBe('working')
  })

  test('outside a Multiverse agent there are no items, as there is no line', async ($, on) => {
    const w = session(on, {})
    w.disk(`${AGENT}/runtime.json`, runtime([COST]))
    w.disk(item('cost'), '$1.20')
    await start($)
    await w.clock.settle()
    expect(w.lines).toEqual([])
    expect(w.reads).toEqual([])
  })
})

