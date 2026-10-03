// The /multiverse command: parsing, each subcommand, and who may run it.
// The test's hooks stand in for the host: `mv2` runs, prompts submitted and the
// commands registered.
import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import { DESCRIPTION, HINT, NAME, inbox, parse } from '../hooks/multiverse'

type Run = { argv: readonly string[]; env?: Record<string, string> }
type Reply = { exitCode?: number; stdout?: string; stderr?: string }

function world(on: On, reply: (argv: readonly string[]) => Reply = () => ({})) {
  mock.env(on, { MV2_CLI: '/mv2' })
  const clock = mock.clock(on)
  const runs: Run[] = []
  on('process.run', async ($, e) => {
    runs.push({ argv: e.argv, env: e.init?.env })
    const answer = reply(e.argv)
    return { value: { exitCode: answer.exitCode ?? 0, stdout: answer.stdout ?? '', stderr: answer.stderr ?? '',
                      isStdoutTruncated: false, isStderrTruncated: false } }
  })
  const submitted: { text: string; asUser: boolean }[] = []
  on('prompt.submit', async ($, e) => {
    submitted.push({ text: e.text, asUser: e.origin.kind === 'plugin' && e.origin.asUser === true })
    return { text: e.text, origin: e.origin }
  })
  return { clock, runs, submitted }
}

const run = ($: { command: { run: (input: never) => Promise<{ text?: string }> } }, args: string, origin = 'composer') =>
  $.command.run({ command: NAME, args, origin: { kind: origin }, presentation: { isFullscreen: false, columns: 100 } } as never)

describe('parse', () => {
  test('nothing or list means the catalog; the subcommands and a name are told apart', async () => {
    expect(parse('')).toEqual({ kind: 'list' })
    expect(parse('  list  ')).toEqual({ kind: 'list' })
    expect(parse('list extra words')).toEqual({ kind: 'list' })
    expect(parse('inbox')).toEqual({ kind: 'inbox' })
    expect(parse('send reviewer Please rebase first.')).toEqual({ kind: 'send', agent: 'reviewer', text: 'Please rebase first.' })
    expect(parse('review-pr 12 --fast')).toEqual({ kind: 'prompt', name: 'review-pr', args: '12 --fast' })
    expect(parse('review-pr')).toEqual({ kind: 'prompt', name: 'review-pr', args: '' })
  })

  test('a message keeps its inner spacing and line breaks; a send without a target or text is an error', async () => {
    expect(parse('send lead first line\nsecond   line')).toEqual({ kind: 'send', agent: 'lead', text: 'first line\nsecond   line' })
    for (const args of ['send', 'send lead', 'send   lead  ']) {
      expect(parse(args)).toEqual({ kind: 'error', text: 'Usage: /multiverse send <agent> <text>' })
    }
  })
})

describe('inbox formatting', () => {
  test('unread messages are listed with sender and time and a note that they are now marked read', async () => {
    const text = inbox(JSON.stringify({ messages: [
      { at: '2026-10-03T09:15:42.123456+00:00', from: { agent_id: '7e2c007f-45a2-4185-870e-dd409c4cdbb3', name: 'agent' }, text: 'Rebase done.' },
      { at: '2026-10-03T09:20:00+00:00', from: { agent_id: '', name: 'user' }, text: 'Thanks\u001b[31m!' },
    ] }))
    expect(text.split('\n')).toEqual([
      '2 unread inbox-only messages (now marked read):',
      '• agent 7e2c007f (2026-10-03 09:15:42 UTC): Rebase done.',
      '• user (2026-10-03 09:20:00 UTC): Thanks [31m!',
    ])
  })

  test('none, one, a long one and bad output', async () => {
    expect(inbox(JSON.stringify({ messages: [] }))).toBe('No unread inbox-only messages.')
    expect(inbox(JSON.stringify({ messages: [{ from: { name: 'user' }, text: 'x'.repeat(900) }] })).split('\n')[0])
      .toBe('1 unread inbox-only message (now marked read):')
    const long = inbox(JSON.stringify({ messages: [{ from: { name: 'user' }, text: 'x'.repeat(900) }] })).split('\n')[1] ?? ''
    expect(long.length).toBeLessThan(520)
    expect(long.endsWith('…')).toBe(true)
    expect(inbox('not json')).toContain('Could not read the inbox')
  })
})

describe('/multiverse', () => {
  test('list prints the catalog as output and starts no turn', async ($, on) => {
    const w = world(on, () => ({ stdout: 'commands/help: Explain mv2.\n  /u/commands/help\n' }))
    const result = await run($, '')
    expect(result.text).toBe('commands/help: Explain mv2.\n  /u/commands/help')
    expect(w.runs.map((r) => r.argv)).toEqual([['/mv2', 'catalog']])
    await w.clock.settle()
    expect(w.submitted).toEqual([])
  })

  test('inbox is a normal, consuming read: the messages it shows are marked read', async ($, on) => {
    const w = world(on, () => ({ stdout: JSON.stringify({ messages: [{ from: { name: 'user' }, text: 'ping' }], cursor: 10 }) }))
    const result = await run($, 'inbox')
    expect(result.text).toContain('1 unread inbox-only message')
    expect(result.text).toContain('• user: ping')
    expect(w.runs.map((r) => r.argv)).toEqual([['/mv2', 'inbox', '--json']])  // not --peek: that is the status line's count
  })

  test('send goes out as this agent, from the person’s own prompt', async ($, on) => {
    const w = world(on, () => ({ stdout: JSON.stringify({ state: 'native_queued', ok: true }) }))
    const result = await run($, 'send reviewer --text looks odd, "quoted" $(not run)')
    expect(result.text).toBe('Sent to reviewer (native_queued).')
    // The text is one argument, so it can never read as an option, and the session's own
    // MV2_AGENT_ID is left alone, so the recipient reads "Message from <this agent>".
    expect(w.runs).toEqual([{ argv: ['/mv2', 'send', 'reviewer', '--text=--text looks odd, "quoted" $(not run)'], env: undefined }])
    expect((await run($, 'send lead hi', 'bridge')).text).toBe('Sent to lead (native_queued).')
  })

  test('a send that fails says so', async ($, on) => {
    world(on, () => ({ exitCode: 1, stderr: 'mv2: No agent matches nobody\n' }))
    expect((await run($, 'send nobody hi')).text).toBe('Not sent: mv2: No agent matches nobody')
  })

  test('only the person can send, read the inbox or submit: other origins may only list', async ($, on) => {
    const w = world(on, () => ({ stdout: 'prompt text' }))
    for (const origin of ['plugin', 'sdk', 'peer', 'channel']) {
      for (const args of ['send lead hi', 'inbox', 'review-pr 12']) {
        expect((await run($, args, origin)).text).toContain('only a prompt the person typed can run it')
      }
    }
    expect(w.runs).toEqual([])  // nothing ran, so nothing was sent, marked read or loaded
    await w.clock.settle()
    expect(w.submitted).toEqual([])
    expect((await run($, 'list', 'plugin')).text).toBe('prompt text')
    expect((await run($, 'inbox', 'bridge')).text).toContain('Could not read the inbox')  // Remote Control is the person
  })

  test('a catalog entry is loaded and submitted as the person’s own prompt, after the command returns', async ($, on) => {
    const w = world(on, () => ({ stdout: 'Review the diff carefully.\n\nUser arguments:\n12 --fast\n' }))
    const result = await run($, 'review-pr 12 --fast')
    expect(result.text).toBe('Running review-pr.')
    expect(w.runs).toEqual([{ argv: ['/mv2', 'command', 'review-pr', '--', '12 --fast'], env: undefined }])
    expect(w.submitted).toEqual([])  // not from inside the hook: that would wait on its own turn
    await w.clock.settle()
    expect(w.submitted).toEqual([{ text: 'Review the diff carefully.\n\nUser arguments:\n12 --fast\n', asUser: true }])
  })

  test('without arguments there is no separator; an unknown name points at the catalog and submits nothing', async ($, on) => {
    const w = world(on, (argv) => argv[2] === 'nosuchthing'
      ? { exitCode: 1, stderr: "mv2: Expected one exact match for 'nosuchthing'; found 0. Use mv2 catalog.\n" } : { stdout: 'The help prompt.' })
    await run($, 'help')
    expect(w.runs.at(-1)?.argv).toEqual(['/mv2', 'command', 'help'])
    const missing = await run($, 'nosuchthing')
    expect(missing.text).toContain("found 0")
    expect(missing.text).toContain('Run /multiverse list to see what is available.')
    await w.clock.settle()
    expect(w.submitted).toEqual([{ text: 'The help prompt.', asUser: true }])
  })

  test('outside a Multiverse session it says so', async ($, on) => {
    mock.env(on, {})
    expect((await run($, 'list')).text).toContain('MV2_CLI is not set')
  })
})

describe('/multiverse registration', () => {
  test('session start registers it with a description, an argument hint and mid-turn running', async ($, on) => {
    mock.env(on, { MV2_CLI: '/mv2' })
    mock.clock(on)
    on('session.start', async ($, e) => ({ cwd: e.cwd }))
    on('process.run', async () => ({ value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
    on('ui.log', async () => ({ value: undefined }))
    const registered: unknown[] = []
    on('command.register', async ($, e) => { registered.push(e); return { value: { command: e.name } } })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    expect(registered).toEqual([{ name: NAME, description: DESCRIPTION, argumentHint: HINT, immediate: true }])
    expect(NAME).toBe('multiverse')
    expect(HINT).toContain('send <agent> <text>')
  })
})
