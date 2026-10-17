// Typeahead for /multiverse: which rows each position offers, and how the catalog and the
// agent list are read (in the background, kept briefly, never waited on, no rows on failure).
// The test's hooks stand in for the host: `mv2` runs and the engine's own rows.
import type { On, PromptAutocompleteInput, PromptAutocompleteResult } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import { agentRows, catalogEntries, catalogRows, suggest } from '../hooks/index'

// What `mv2 catalog --json` prints.
const CATALOG = JSON.stringify([
  { kind: 'skills', name: 'coordination', description: 'Spawn, inspect and communicate with agents.', path: '/u/skills/coordination' },
  { kind: 'skills', name: 'mv2-help', description: 'Explain mv2 capabilities.', path: '/u/skills/mv2-help' },
  { kind: 'commands', name: 'help', description: 'Explain the current universe.', path: '/u/commands/help' },
  { kind: 'workflows', name: 'review', description: 'Review a change.', path: '/u/workflows/review' },
  { kind: 'harnesses', name: 'claude', description: 'Claude Code.', path: '/u/harnesses/claude' },
])
const entries = (...list: [string, string, string?][]) => list.map(([kind, name, description]) => ({ kind, name, description }))
const UNIVERSE = '/u'
const AGENTS = [
  { id: 'self-id', name: 'claude-support', universe: UNIVERSE, live: 'alive', status: 'working', phase: 'working' },
  { id: 'a1', name: 'mv2-lead', universe: UNIVERSE, live: 'alive', status: 'idle', phase: 'working' },
  { id: 'a2', name: 'codex-status', universe: UNIVERSE, live: 'dormant', status: '-', phase: 'done' },
  { id: 'a3', name: 'compaction', universe: UNIVERSE, live: 'alive', status: 'waiting', phase: '' },
  { id: 'a4', name: 'reviewer', universe: '/u/subverses/pr', live: 'alive', status: 'idle', phase: 'working' },
]
const KNOWN = { catalog: catalogRows(catalogEntries(JSON.parse(CATALOG))), agents: agentRows(AGENTS, 'self-id', UNIVERSE) }
const texts = (rows: readonly { text: string }[]) => rows.map((row) => row.text)

describe('reading the catalog and the agents', () => {
  test('only what mv2 command runs is offered, by its bare name, described by its kind', async () => {
    expect(catalogEntries(JSON.parse(CATALOG)).map((e) => `${e.kind}/${e.name}`))
      .toEqual(['skills/coordination', 'skills/mv2-help', 'commands/help', 'workflows/review'])
    // Anything else in the list is left out: other kinds, names that are not one word, missing fields.
    expect(catalogEntries([{ kind: 'commands', name: 'a b' }, { kind: 'commands' }, { name: 'x' }, null, 'commands/x',
                           { kind: 'commands', name: 'ok' }])).toEqual([{ kind: 'commands', name: 'ok', description: '' }])
    expect(catalogEntries({ entries: [] })).toEqual([])
    expect(KNOWN.catalog).toEqual([
      { text: 'coordination', description: 'skill · Spawn, inspect and communicate with agents.' },
      { text: 'mv2-help', description: 'skill · Explain mv2 capabilities.' },
      { text: 'help', description: 'command · Explain the current universe.' },
      { text: 'review', description: 'workflow · Review a change.' },
    ])
  })

  test('a name two kinds share, or one a subcommand would shadow, is written kind/name', async () => {
    const rows = catalogRows(catalogEntries(entries(['commands', 'review', 'A.'], ['workflows', 'review', 'B.'], ['commands', 'send', 'C.'], ['skills', 'inbox'])))
    expect(texts(rows)).toEqual(['commands/review', 'workflows/review', 'commands/send', 'skills/inbox'])
    expect(rows[3]?.description).toBe('skill')
  })

  test('agents mv2 send can reach: this universe, not this agent, one word each', async () => {
    expect(KNOWN.agents).toEqual([
      { text: 'mv2-lead', description: 'alive · idle · working' },
      { text: 'codex-status', description: 'dormant · done' },
      { text: 'compaction', description: 'alive · waiting' },
    ])
    expect(agentRows([{ id: 'x', name: 'two words', universe: UNIVERSE }, { id: 'y', name: 'a,b', universe: UNIVERSE },
                      { id: 'z', name: 7 }, null], undefined, UNIVERSE)).toEqual([])
    expect(agentRows({ agents: [] })).toEqual([])
  })
})

describe('which rows each position offers', () => {
  test('the first word: subcommands and catalog entries, by prefix, any case', async () => {
    expect(texts(suggest('/multiverse ', 's', KNOWN))).toEqual(['send '])
    expect(suggest('/multiverse ', 'se', KNOWN)[0]).toEqual({ text: 'send ', label: 'send', description: 'message an agent, as this agent' })
    expect(texts(suggest('/multiverse ', 'H', KNOWN))).toEqual(['help'])
    expect(texts(suggest('/multiverse  \t', 'i', KNOWN))).toEqual(['inbox'])
    expect(texts(suggest('/multiverse ', 'mv', KNOWN))).toEqual(['mv2-help'])
    // A kind/name row also matches by its name.
    const shared = { catalog: catalogRows(catalogEntries(entries(['commands', 'review', 'A.'], ['workflows', 'review', 'B.']))), agents: [] }
    expect(texts(suggest('/multiverse ', 're', shared))).toEqual(['commands/review', 'workflows/review'])
  })

  test('after send: recipients, with a space after; a comma list completes its last name', async () => {
    expect(suggest('/multiverse send ', 'c', KNOWN)).toEqual([
      { text: 'codex-status ', label: 'codex-status', description: 'dormant · done' },
      { text: 'compaction ', label: 'compaction', description: 'alive · waiting' },
    ])
    expect(texts(suggest('/multiverse send ', 'mv2-lead,co', KNOWN))).toEqual(['mv2-lead,codex-status ', 'mv2-lead,compaction '])
    expect(texts(suggest('/multiverse send ', 'compaction,', KNOWN))).toEqual(['compaction,mv2-lead ', 'compaction,codex-status '])
    expect(suggest('/multiverse send ', 'claude', KNOWN)).toEqual([])  // this agent itself
  })

  test('nothing for free arguments, a message, another command or text before the command', async () => {
    expect(suggest('/multiverse review ', 'x', KNOWN)).toEqual([])
    expect(suggest('/multiverse send mv2-lead ', 'he', KNOWN)).toEqual([])
    expect(suggest('/multiverse list ', 'i', KNOWN)).toEqual([])
    expect(suggest('/model ', 's', KNOWN)).toEqual([])
    expect(suggest('please run /multiverse ', 's', KNOWN)).toEqual([])
    expect(suggest('', '/multiverse', KNOWN)).toEqual([])
  })
})

type Reply = { exitCode?: number; stdout?: string; throws?: boolean; waitMs?: number }

function world(on: On, reply: (argv: readonly string[]) => Reply, env: Record<string, string> = {}) {
  mock.env(on, { MV2_CLI: '/mv2', MV2_AGENT_ID: 'self-id', MV2_UNIVERSE: UNIVERSE, ...env })
  const clock = mock.clock(on)
  const runs: string[] = []
  on('process.run', async ($, e) => {
    runs.push(e.argv.slice(1).join(' '))
    const answer = reply(e.argv)
    if (answer.waitMs) await clock.sleep(answer.waitMs)
    if (answer.throws) throw new Error('timed out')
    return { value: { exitCode: answer.exitCode ?? 0, stdout: answer.stdout ?? '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // The engine's own rows, which the plugin's join after.
  on('prompt.autocomplete', async () => ({ suggestions: [{ text: 'engine-row' }] }))
  return { clock, runs }
}

const healthy = (argv: readonly string[]): Reply =>
  argv[1] === 'catalog' ? { stdout: CATALOG } : { stdout: JSON.stringify(AGENTS) }
// mv2 is asked for JSON both times.
const ASKED = ['catalog --json', 'list --json']

type Autocomplete = (input: PromptAutocompleteInput) => Promise<PromptAutocompleteResult>

// What the prompt box asks when the person has typed `text`, the cursor at its end. The test kit
// raises the event as `$.prompt.autocomplete`, which Claude Code 2.1.292's typings leave out of `$`.
function ask($: unknown, text: string) {
  const token = /\S+$/.exec(text)?.[0] ?? ''
  const autocomplete = ($ as { prompt: { autocomplete: Autocomplete } }).prompt.autocomplete
  return autocomplete({ text, cursor: text.length, token, start: text.length - token.length })
    .then((result) => texts(result.suggestions))
}

describe('reading in the background', () => {
  test('typing the command reads ahead; the words after it are then offered beneath the engine rows', async ($, on) => {
    const w = world(on, healthy)
    expect(await ask($, '/mu')).toEqual(['engine-row'])
    await w.clock.settle()
    expect(w.runs.sort()).toEqual(ASKED)
    expect(await ask($, '/multiverse h')).toEqual(['engine-row', 'help'])
    expect(await ask($, '/multiverse send m')).toEqual(['engine-row', 'mv2-lead '])
  })

  test('kept for a few seconds, then read again in the background while the kept rows answer', async ($, on) => {
    const w = world(on, healthy)
    await ask($, '/mul')
    await w.clock.settle()
    await ask($, '/multiverse h')
    await w.clock.advance(4000)
    await ask($, '/multiverse he')
    expect(w.runs.length).toBe(2)  // still fresh: no new read
    await w.clock.advance(2000)
    expect(await ask($, '/multiverse he')).toEqual(['engine-row', 'help'])  // answered from what is kept
    await w.clock.settle()
    expect(w.runs.length).toBe(4)  // and read again, once each
  })

  test('typing never waits on a slow mv2, and a read under way is not started twice', async ($, on) => {
    const w = world(on, (argv) => ({ ...healthy(argv), waitMs: 3000 }))
    expect(await ask($, '/multi')).toEqual(['engine-row'])
    await w.clock.settle()
    expect(await ask($, '/multiverse h')).toEqual(['engine-row'])  // nothing kept yet, and no wait for it
    expect(await ask($, '/multiverse he')).toEqual(['engine-row'])
    await w.clock.settle()
    expect(w.runs.length).toBe(2)
    await w.clock.advance(3000)
    expect(await ask($, '/multiverse he')).toEqual(['engine-row', 'help'])
  })

  test('a failed or broken read offers no rows, never an error', async ($, on) => {
    let mode = 'exit'
    const w = world(on, (argv) => mode === 'exit' ? { exitCode: 1 } : mode === 'throw' ? { throws: true }
      : argv[1] === 'catalog' ? { stdout: 'not a catalog' } : { stdout: '{not json' })
    for (mode of ['exit', 'throw', 'garbage']) {
      await w.clock.advance(6000)
      await ask($, '/multiverse h')
      await w.clock.settle()
      expect(await ask($, '/multiverse h')).toEqual(['engine-row'])
      expect(await ask($, '/multiverse send m')).toEqual(['engine-row'])
    }
    expect(w.runs.length).toBe(6)
  })

  test('nothing is read for other prompts, or outside a Multiverse session', async ($, on) => {
    const w = world(on, healthy)
    for (const text of ['hello', '/model s', '/m', 'look at /multiverse h']) expect(await ask($, text)).toEqual(['engine-row'])
    await w.clock.settle()
    expect(w.runs).toEqual([])
  })

  test('without MV2_CLI nothing is read and nothing is added', async ($, on) => {
    const w = world(on, healthy, { MV2_CLI: '' })
    expect(await ask($, '/multiverse h')).toEqual(['engine-row'])
    await w.clock.settle()
    expect(w.runs).toEqual([])
  })
})
