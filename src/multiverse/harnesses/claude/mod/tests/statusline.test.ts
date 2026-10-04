// The status line's pure logic: config parsing, rendering and the shared slot.
import { describe, expect, test } from 'claude-code/testing'
import { DEFAULTS, clean, currentLine, defaultConfig, isSubverse, parseConfig, render, setBase, setProgress } from '../hooks/statusline'

const FACTS = { agent: 'reviewer', universe: 'pr-review', phase: 'working', summary: 'reading the diff', inbox: 2, compaction: 'trees 3 · prepared' }

describe('statusline config', () => {
  test('no file means the defaults, with nothing to report', async () => {
    expect(parseConfig(undefined)).toEqual({ config: defaultConfig() })
    expect(defaultConfig().on).toEqual(DEFAULTS)
    expect(DEFAULTS).toEqual({ agent: false, phase: true, summary: false, inbox: true, compaction: true })
  })

  test('the order of the keys is the display order and a missing key keeps its default', async () => {
    const { config, problem } = parseConfig('{"schema": 1, "items": {"inbox": true, "agent": true, "phase": false}}')
    expect(problem).toBeUndefined()
    expect(config.order).toEqual(['inbox', 'agent', 'phase', 'summary', 'compaction'])
    expect(config.on).toEqual({ agent: true, phase: false, summary: false, inbox: true, compaction: true })
  })

  test('unknown keys are ignored', async () => {
    const { config, problem } = parseConfig('{"schema": 1, "items": {"colour": true, "phase": true}, "extra": 1}')
    expect(problem).toBeUndefined()
    expect(config.on).toEqual(DEFAULTS)
    expect(config.order).toEqual(['phase', 'agent', 'summary', 'inbox', 'compaction'])  // only known keys, listed first
  })

  test('a tmux block for the tmux status bar is ignored, whatever it holds', async () => {
    const tmux = '{"refresh_seconds": 2, "items": {"agent": true, "harness": true, "status": false, "phase": true, "summary": false, ' +
      '"inbox": true, "context": false}, "harnesses": {"codex": {"items": {"status": true, "context": true}}, ' +
      '"claude": {"items": {"phase": false, "inbox": false}}}}'
    const claude = '"items": {"agent": false, "phase": true, "summary": false, "inbox": true, "compaction": true}'
    const parsed = parseConfig(`{"schema": 1, ${claude}, "tmux": ${tmux}}`)
    expect(parsed.problem).toBeUndefined()
    expect(parsed.config).toEqual(defaultConfig())  // the tmux items never reach the Claude line
    // Nor can a broken or odd block of another consumer change or spoil it.
    for (const odd of ['"x"', 'null', '[]', '{"items": 5, "harnesses": []}', '{"refresh_seconds": "soon"}']) {
      expect(parseConfig(`{"schema": 1, ${claude}, "tmux": ${odd}}`)).toEqual({ config: defaultConfig() })
    }
    // Only Claude's own items decide Claude's line, even when tmux uses the same names differently.
    const own = parseConfig('{"items": {"agent": true}, "tmux": {"items": {"agent": false, "phase": false}}}')
    expect(own.config.on).toEqual({ ...DEFAULTS, agent: true })
  })

  test('anything unusable falls back to the defaults and says why', async () => {
    for (const [text, problem] of [
      ['{unfinished', 'not valid JSON'], ['[]', 'must be an object'], ['{"schema": 2}', 'unsupported schema'],
      ['{"items": []}', 'items must be an object'],
    ] as const) {
      const parsed = parseConfig(text)
      expect(parsed.config).toEqual(defaultConfig())
      expect(parsed.problem).toContain(problem)
    }
  })

  test('a value that is not true or false is skipped and reported, the rest still applies', async () => {
    const { config, problem } = parseConfig('{"items": {"agent": "yes", "summary": true}}')
    expect(config.on).toEqual({ ...DEFAULTS, summary: true })
    expect(problem).toContain('agent')
  })
})

describe('statusline render', () => {
  test('the defaults show the phase, the unread count and the compaction state', async () => {
    expect(render(defaultConfig(), FACTS)).toBe('working · inbox 2 · trees 3 · prepared')
  })

  test('the agent item names the agent and its universe', async () => {
    const on = { ...DEFAULTS, agent: true }
    expect(render({ order: ['agent', 'phase', 'summary', 'inbox', 'compaction'], on }, FACTS))
      .toBe('reviewer (pr-review) · working · inbox 2 · trees 3 · prepared')
    expect(render({ order: ['agent'], on }, { ...FACTS, universe: undefined })).toBe('reviewer')
  })

  test('phase and summary join into one segment, or stand alone', async () => {
    const order = ['phase', 'summary', 'inbox', 'compaction', 'agent'] as const
    expect(render({ order, on: { ...DEFAULTS, summary: true } }, FACTS)).toBe('working: reading the diff · inbox 2 · trees 3 · prepared')
    expect(render({ order, on: { ...DEFAULTS, phase: false, summary: true } }, FACTS)).toBe('reading the diff · inbox 2 · trees 3 · prepared')
    expect(render({ order, on: { ...DEFAULTS, summary: true } }, { ...FACTS, summary: '' })).toBe('working · inbox 2 · trees 3 · prepared')
  })

  test('the inbox shows only when something is unread, and caps at 50', async () => {
    expect(render(defaultConfig(), { ...FACTS, inbox: 0 })).toBe('working · trees 3 · prepared')
    expect(render(defaultConfig(), { ...FACTS, inbox: 50 })).toContain('inbox 50+')
    expect(render(defaultConfig(), { ...FACTS, inbox: 49 })).toContain('inbox 49')
  })

  test('an empty line is undefined, so the status is cleared', async () => {
    expect(render(defaultConfig(), { inbox: 0 })).toBeUndefined()
    expect(render({ order: ['phase'], on: { ...DEFAULTS, phase: false } }, FACTS)).toBeUndefined()
  })

  test('text from another agent carries no control characters and a summary is shortened', async () => {
    expect(clean('a\u001b[31mred\u0007\nline\u2028two')).toBe('a [31mred line two')
    const line = render({ order: ['summary'], on: { ...DEFAULTS, summary: true, phase: false } },
      { ...FACTS, summary: 'x'.repeat(200) }) ?? ''
    expect(line.length).toBe(60)
    expect(line.endsWith('…')).toBe(true)
    expect(render(defaultConfig(), { ...FACTS, phase: 'work\u001b]0;pwned\u0007ing' })).not.toContain('\u001b')
  })
})

describe('which universes name themselves in the agent item', () => {
  test('a subverse does, a root universe does not', async () => {
    for (const path of ['/p/.multiverse/subverses/pr-review', '/p/.multiverse/subverses/pr-review/', '/subverses/x',
                        '/p/.multiverse/subverses/a/subverses/b']) {
      expect(isSubverse(path)).toBe(true)
    }
    for (const path of ['/p/.multiverse', '/p/.multiverse/', '/subverses', '/p/subverses/a/b', '/p/.multiverse/state', '', '/']) {
      expect(isSubverse(path)).toBe(false)
    }
  })
})

describe('statusline slot', () => {
  test('progress shows while it lasts and the normal line returns after', async () => {
    setBase(undefined)
    setProgress(undefined)
    expect(currentLine()).toBeUndefined()
    setBase('working · inbox 1')
    expect(currentLine()).toBe('working · inbox 1')
    setProgress('compacting…')
    expect(currentLine()).toBe('compacting…')
    setBase('idle')
    expect(currentLine()).toBe('compacting…')
    setProgress(undefined)
    expect(currentLine()).toBe('idle')
    setBase(undefined)
    expect(currentLine()).toBeUndefined()
  })
})
