// Offline tests of the prompts the mod publishes for mv2 (hooks/prompts.ts): `claude plugin test`. The test's
// hooks stand in for Claude's core and the host beneath the mod: a file system the mod's files land in, and the
// wait process, which ends when its answer file appears or its prompt file goes, or throws when the test presses Esc.
import type { On } from 'claude-code'
import { describe, expect, mock, onTestEnd, test } from './kit'
import { closedOutcome, suggestionMeaning } from '../hooks/prompts'

const FOLDER = '/agent/claude-prompts'
const ID = /^\/agent\/claude-prompts\/([0-9a-f-]{36})\.json$/

// `lose`: how many of core's answer files core removes first (it proved the launch ended) before the mod's take.
// `failReports`: how many status reports carrying prompt events core fails first.
type Options = { below?: Record<string, unknown>; files?: Record<string, string>; lose?: number; now?: number;
  mtimes?: Record<string, number>; failReports?: number }
// A status report as core takes it: its status, and the prompts' opens and ends it carries, with their conversation.
type Report = { status: string; events: Record<string, unknown>[]; session: string | null }

// A test's own asks are awaited before it ends: each returns once its prompt does.
async function closed(w: ReturnType<typeof world>, asks: Promise<unknown>[]) {
  w.escape()
  await Promise.all(asks)
  await w.clock.settle()
}

// Each test's plugin is a fresh copy, but the one before it can still be finishing a report as the next test starts,
// and that call reaches the next test's hooks. Each world names its own Python, so it counts only its own copy's.
let worlds = 0

function world(on: On, options: Options = {}) {
  const python = `/python-${++worlds}`
  mock.env(on, { MV2_PYTHON: python, MV2_AGENT_DIR: '/agent', MV2_LAUNCH_ID: 'launch-1' })
  const clock = mock.clock(on, { now: options.now ?? 0 })
  const mtimes = new Map<string, number>(Object.entries(options.mtimes ?? {}))
  const files = new Map<string, string>(Object.entries(options.files ?? {}))
  const waiters: (() => void)[] = []
  const touch = () => { for (const wake of waiters.splice(0)) wake() }
  let escaped = false
  // What the permission check answers; the kit's own tool.call runs it too.
  const check = { decision: 'ask' }
  const lost = { left: options.lose ?? 0, taken: 0 }
  const ok = (exitCode: number) => ({ value: { exitCode, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  const reports: Report[] = []
  let waits = 0  // the plugin's prompt waits in flight
  const waited = new Set<string>()  // the prompt files they were for
  let failReports = options.failReports ?? 0
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('session.id', async () => ({ value: 's1' }))
  // Claude's process file, naming its session: after a /clear it names the new one once `processFile.id` says so.
  const processFile: { id?: string } = {}
  on('classic.SessionStart', async () => ({}))
  // The moves mv2 records after a switch; `after`: how many status reports went before it.
  const moves: { session: string; previous: string; after: number; at: number }[] = []
  on('turn.start', async ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', async ($, e) => ({ text: e.answer }))
  on('tool.check', async () => ({ decision: check.decision, reason: 'touch in /tmp/x needs approval' }) as never)
  on('tool.call', async () => ({ result: 'done' }) as never)
  on('classic.PermissionRequest', async () => options.below ?? {})
  on('classic.PermissionDenied', async () => ({}))
  on('classic.Elicitation', async () => ({}))
  on('classic.ElicitationResult', async () => ({}))
  on('session.version', async () => ({ value: { version: '2.1.319' } }))
  on('ui.log', async () => ({ value: undefined }))
  on('fs.write', async ($, e) => { files.set(e.path, e.text); touch(); return { value: undefined } })
  on('fs.read', async ($, e) => {
    const text = files.get(e.path)
    if (text === undefined) throw new Error('no such file')
    return { value: text } as never
  })
  on('fs.stat', async () => ({ value: { kind: 'file', size: 4096, mtimeMs: 0, isLink: false } }))
  on('fs.list', async ($, e) => ({ value: [...files.keys()].filter((p) => p.startsWith(`${e.path}/`))
    .map((p) => ({ name: p.slice(e.path.length + 1), kind: 'file', size: 1, mtimeMs: mtimes.get(p) ?? options.now ?? 0, isLink: false })) }) as never)
  on('process.run', async ($, e) => {
    if (e.argv[0] === 'sh' && String(e.argv[2] ?? '').includes('/sessions/')) {
      return { value: { ...ok(0).value, stdout: processFile.id === undefined ? '' : JSON.stringify({ sessionId: processFile.id }) } }
    }
    if (e.argv[0] === python && e.argv.includes('--native-session-id')) {
      const flag = (name: string) => e.argv[e.argv.indexOf(name) + 1] ?? ''
      moves.push({ session: flag('--native-session-id'), previous: flag('--previous-session-id'), after: reports.length, at: clock.now() })
      return ok(0)
    }
    if (e.argv[0] === python && e.argv.includes('--status')) {
      const flag = (name: string) => { const at = e.argv.indexOf(name); return at < 0 ? null : e.argv[at + 1] ?? null }
      if (failReports > 0 && e.argv.includes('--prompt-events-json')) { failReports -= 1; return ok(1) }
      reports.push({ status: flag('--status') ?? '', events: JSON.parse(flag('--prompt-events-json') ?? '[]'), session: flag('--prompt-session-id') })
      return ok(0)
    }
    if (e.argv[0] === 'rm') { for (const path of e.argv.slice(2)) files.delete(path); touch(); return ok(0) }
    if (e.argv[0] === 'mv') {
      const [from, to] = [e.argv[1] ?? '', e.argv[2] ?? '']
      lost.taken += 1
      if (lost.left > 0) { lost.left -= 1; files.delete(from); touch(); return ok(1) }
      const text = files.get(from)
      if (text === undefined) return ok(1)
      files.delete(from)
      files.set(to, text)
      touch()
      return ok(0)
    }
    if (e.argv[0] === '/bin/sh' && e.argv.length === 6) {
      const [answer, prompt] = [e.argv[4] ?? '', e.argv[5] ?? '']
      waits += 1
      waited.add(prompt)
      try {
        for (;;) {
          if (escaped) throw new Error('$.process.run(/bin/sh) aborted')
          if (files.has(answer)) return ok(0)
          if (!files.has(prompt)) return ok(2)
          await new Promise<void>((wake) => waiters.push(wake))
        }
      } finally {
        waits -= 1
      }
    }
    return ok(0)
  })
  const published = () => [...files.keys()].map((p) => ID.exec(p)?.[1]).filter((id): id is string => id !== undefined)
  // Each wait the plugin still holds a prompt in is Esc'd, as the person would, and the test ends once no wait is left
  // and each prompt it held has its end written. A prompt only listed has no wait to end.
  const unsettled = () => waits > 0 || [...waited].some((prompt) => !files.has(prompt.replace(/\.json$/, '.end.json')))
  onTestEnd(on, async () => {
    if (!unsettled()) return
    escaped = true
    touch()
    for (let i = 0; i < 200 && unsettled(); i++) await new Promise<void>((wake) => { waiters.push(wake); void clock.settle().then(wake) })
    if (unsettled()) throw new Error(`a prompt this test held never ended: ${waits} wait(s) left`)
    await clock.settle()
  })
  return {
    clock, files, published, check, lost, reports, processFile, moves,
    record: (id: string) => JSON.parse(files.get(`${FOLDER}/${id}.json`) ?? 'null'),
    ended: (id: string) => JSON.parse(files.get(`${FOLDER}/${id}.end.json`) ?? 'null'),
    // As core writes it: once, into its own prompts folder, naming the launch and conversation.
    answer: (id: string, answer: Record<string, unknown>) => {
      files.set(`/agent/prompts/${id}.answer.json`, JSON.stringify({ schema: 1, id, launch_id: 'launch-1', native_id: 's1', ...answer }))
      touch()
    },
    escape: () => { escaped = true; touch() },
    // Until the mod has published a prompt (or written its end record).
    until: async (test: () => boolean) => { for (let i = 0; i < 200 && !test(); i++) await new Promise<void>((wake) => { waiters.push(wake); void clock.settle().then(wake) }) },
  }
}

const BASH = { command: 'touch /tmp/x/a.txt' }
const SESSION = [{ type: 'addDirectories', directories: ['/tmp/x'], destination: 'session' },
  { type: 'setMode', mode: 'acceptEdits', destination: 'session' }]
const SAVED = [{ type: 'addRules', rules: [{ toolName: 'Bash', ruleContent: 'touch:*' }], behavior: 'allow', destination: 'localSettings' }]
const QUESTIONS = { questions: [
  { question: 'Which colour?', header: 'Colour', options: [{ label: 'Blue', description: '' }, { label: 'Red', description: '' }], multiSelect: false },
  { question: 'Which fruits?', header: 'Fruit', options: [{ label: 'Apple', description: '' }, { label: 'Plum', description: '' }], multiSelect: true },
] }
const PLAN = { plan: '# Plan\n1. Write the file.', planFilePath: '/home/plans/p.md' }

type Raise = { tool: { check: (input: never) => Promise<unknown> }; classic: { PermissionRequest: (input: never) => Promise<unknown> } }

async function ask($: Raise, tool: string, input: unknown, suggestions?: unknown, extra: Record<string, unknown> = {}) {
  await $.tool.check({ tool, input, tool_use_id: 'toolu_1', approver: 'person' } as never)
  return $.classic.PermissionRequest({ tool_name: tool, tool_input: input, permission_suggestions: suggestions, session_id: 's1',
    transcript_path: '/home/.claude/projects/p/s1.jsonl', permission_mode: 'default', ...extra } as never) as Promise<{ decision?: unknown }>
}

const OURS = '11111111-2222-4333-8444-555555555555'
const OLDER = '22222222-2222-4333-8444-555555555555'
// One prompt this launch's earlier load published, one an earlier launch's.
const SWEPT = {
  [`${FOLDER}/${OURS}.json`]: JSON.stringify({ schema: 1, id: OURS, launch: 'launch-1', session: 's1', kind: 'question' }),
  [`${FOLDER}/${OLDER}.json`]: JSON.stringify({ schema: 1, id: OLDER, launch: 'launch-0', session: 's0', kind: 'permission' }),
}

describe('prompts for mv2', () => {
  test("mv2's answer is checked against what was published and returned as Claude's decision", async ($, on) => {
    // The decisions are the only way an answer reaches Claude; one that maps a choice to the wrong decision would
    // allow what the person meant to refuse, or save a rule they didn't choose, and the dialog still looks the same.
    const cases: [string, string, unknown, unknown, Record<string, unknown>, unknown, boolean][] = [
      ['allow once', 'Bash', BASH, SESSION, { choice: 'allow' }, { behavior: 'allow' }, false],
      ['the first session suggestion', 'Bash', BASH, SESSION, { choice: 'allow_session' }, { behavior: 'allow', updatedPermissions: [SESSION[0]] }, false],
      ['the second session suggestion', 'Bash', BASH, SESSION, { choice: 'allow_session_2' }, { behavior: 'allow', updatedPermissions: [SESSION[1]] }, false],
      // The model reads a deny message as the call's result: bare, Haiku took it for an error. It says who answered.
      ['deny with a message, from the person', 'Bash', BASH, SESSION, { choice: 'deny', message: 'Use the build folder.', caller: { kind: 'person' } },
        { behavior: 'deny', message: 'The user declined this tool use through mv2: Use the build folder.' }, false],
      ['deny from a plugin', 'Bash', BASH, SESSION, { choice: 'deny', message: 'Not now.', caller: { kind: 'plugin', name: 'qa-answer' } },
        { behavior: 'deny', message: 'The plugin qa-answer, answering for the user through mv2, declined this tool use: Not now.' }, false],
      ['deny without a message', 'Bash', BASH, SESSION, { choice: 'deny', caller: { kind: 'person' } },
        { behavior: 'deny', message: 'The user declined this tool use through mv2.' }, false],
      ['deny with no readable caller', 'Bash', BASH, SESSION, { choice: 'deny', message: 'x', caller: { kind: 'plugin', name: 'bad name!' } },
        { behavior: 'deny', message: 'This tool use was declined through mv2: x' }, false],
      ["a person's saved rule", 'Bash', BASH, SAVED, { choice: 'allow_always', allow_rule: true }, { behavior: 'allow', updatedPermissions: SAVED }, true],
      ['questions, one with several labels', 'AskUserQuestion', QUESTIONS, undefined, { answers: { 'Which colour?': ['Blue'], 'Which fruits?': ['Apple', 'Plum'] } },
        { behavior: 'allow', updatedInput: { ...QUESTIONS, answers: { 'Which colour?': 'Blue', 'Which fruits?': 'Apple, Plum' } } }, false],
      ['typed text for a question', 'AskUserQuestion', QUESTIONS, undefined, { answers: { 'Which colour?': ['Green, please'], 'Which fruits?': ['Plum'] } },
        { behavior: 'allow', updatedInput: { ...QUESTIONS, answers: { 'Which colour?': 'Green, please', 'Which fruits?': 'Plum' } } }, false],
      ['approve a plan', 'ExitPlanMode', PLAN, undefined, { choice: 'allow' }, { behavior: 'allow', updatedInput: PLAN }, false],
      ['keep planning', 'ExitPlanMode', PLAN, undefined, { choice: 'deny', message: 'Read it back too.', caller: { kind: 'person' } },
        { behavior: 'deny', message: 'The user wants you to keep planning, answering through mv2: Read it back too.' }, false],
    ]
    const w = world(on)
    for (const [, tool, input, suggestions, answer, decision, saved] of cases) {
      const before = new Set(w.published())
      const held = ask($, tool, input, suggestions)
      await w.until(() => w.published().some((id) => !before.has(id)))
      const id = w.published().find((p) => !before.has(p)) ?? ''
      w.answer(id, answer)
      expect(await held).toEqual({ decision })
      // Its launch: a prompt whose launch is no longer the agent's was closed by the stop.
      expect(w.ended(id)).toMatchObject({ how: 'sent', saved_rule: saved, launch: 'launch-1', tool_use_id: 'toolu_1',
        transcript: { path: '/home/.claude/projects/p/s1.jsonl', offset: 4096 } })
      expect(w.files.has(`${FOLDER}/${id}.json`)).toBe(false)
    }
  })

  test('the published prompt names its call and offers only what Claude can take', async ($, on) => {
    // What mv2 shows and validates against: a rule saved to the person's settings must be marked so a plugin can't
    // choose it, and a question's options must be the tool's own.
    const w = world(on)
    const asks = [ask($, 'Bash', BASH, [...SESSION, ...SAVED])]
    await w.until(() => w.published().length === 1)
    const record = w.record(w.published()[0] ?? '')
    expect(record).toMatchObject({ schema: 1, launch: 'launch-1', session: 's1', claude: '2.1.319', tool_use_id: 'toolu_1', kind: 'permission',
      tool: 'Bash', summary: 'touch /tmp/x/a.txt', answerable: true, subagent: null, transcript: { path: '/home/.claude/projects/p/s1.jsonl', offset: 4096 } })
    expect(record.detail).toMatchObject({ input: BASH, reason: 'touch in /tmp/x needs approval' })
    expect(record.choices.map((c: { id: string; saves_rule: boolean }) => [c.id, c.saves_rule])).toEqual(
      [['allow', false], ['allow_session', false], ['allow_session_2', false], ['allow_always', true], ['deny', false]])
    asks.push(ask($, 'AskUserQuestion', QUESTIONS))
    await w.until(() => w.published().length === 2)
    const question = w.published().map(w.record).find((r) => r.kind === 'question')
    expect(question.questions.map((q: { id: string; multiple: boolean; options: unknown }) => [q.id, q.multiple, q.options])).toEqual(
      [['Which colour?', false, [['Blue', ''], ['Red', '']]], ['Which fruits?', true, [['Apple', ''], ['Plum', '']]]])
    expect(question.choices).toEqual([])
    await closed(w, asks)
  })

  test("an answer that doesn't fit is refused, and the prompt is left to the person", async ($, on) => {
    // The mod checks the answer again at the native boundary: core's check may be older than the prompt, and an
    // answer the mod can't map must never become a bare allow (which Claude ignores) or another prompt's decision.
    const cases: [string, unknown, unknown, Record<string, unknown>][] = [
      ['an unoffered choice', BASH, SESSION, { choice: 'allow_always' }],
      ['a saved rule without the person', BASH, SAVED, { choice: 'allow_always' }],
      ['another prompt', BASH, SESSION, { choice: 'allow', id: '00000000-0000-4000-8000-000000000000' }],
      ['another conversation', BASH, SESSION, { choice: 'allow', native_id: 's2' }],
      ['another launch', BASH, SESSION, { choice: 'allow', launch_id: 'launch-0' }],
      ['a question left out', QUESTIONS, undefined, { answers: { 'Which colour?': ['Blue'] } }],
      ['two labels for one', QUESTIONS, undefined, { answers: { 'Which colour?': ['Blue', 'Red'], 'Which fruits?': ['Plum'] } }],
    ]
    const w = world(on)
    for (const [label, input, suggestions, answer] of cases) {
      const before = new Set(w.published())
      const held = ask($, input === QUESTIONS ? 'AskUserQuestion' : 'Bash', input, suggestions)
      await w.until(() => w.published().some((id) => !before.has(id)))
      const id = w.published().find((p) => !before.has(p)) ?? ''
      w.answer(id, answer)
      expect([label, await held]).toEqual([label, {}])
      expect([label, w.ended(id)?.how]).toEqual([label, 'refused'])
    }
  })

  test('a prompt that closes first is never answered', async ($, on) => {
    // The person answers in the terminal, or presses Esc, or the turn ends: an answer written after that must not
    // reach a later prompt, and the end record says why so mv2 can tell the person.
    const w = world(on)
    // The call ran: the person allowed it.
    const held = ask($, 'Bash', BASH, SESSION)
    await w.until(() => w.published().length === 1)
    const first = w.published()[0] ?? ''
    w.check.decision = 'allow'
    await $.tool.call({ tool: 'Bash', input: BASH, tool_use_id: 'toolu_1' } as never)
    expect(await held).toEqual({})
    expect(w.ended(first)).toMatchObject({ how: 'closed', cause: 'resolved' })
    w.answer(first, { choice: 'allow' })
    await w.clock.settle()
    expect(w.ended(first).how).toBe('closed')
    // Esc ends the hook's wait.
    w.check.decision = 'ask'
    const second = ask($, 'Bash', BASH, SESSION)
    await w.until(() => w.published().length === 1 && w.published()[0] !== first)
    const id = w.published()[0] ?? ''
    w.escape()
    expect(await second).toEqual({})
    expect(w.ended(id)).toMatchObject({ how: 'closed', cause: 'interrupted' })
  })

  test('an answer core removed before the mod took it is never acted on', async ($, on) => {
    // Core proves an answer was never taken by winning its own removal of the file: a hook that acted on a copy it
    // read before losing that race would answer a prompt core reports as never answered.
    const w = world(on, { lose: 1 })
    const held = ask($, 'Bash', BASH, SESSION)
    await w.until(() => w.published().length === 1)
    const id = w.published()[0] ?? ''
    w.answer(id, { choice: 'allow' })
    await w.until(() => w.lost.taken === 1)
    await w.clock.settle()
    expect(w.ended(id)).toBeNull()
    w.check.decision = 'allow'
    await $.tool.call({ tool: 'Bash', input: BASH, tool_use_id: 'toolu_1' } as never)
    expect(await held).toEqual({})
    expect(w.ended(id)).toMatchObject({ how: 'closed', cause: 'resolved' })
  })

  test('the end record keeps what was offered, never the call', async ($, on) => {
    // A person-first outcome reports which offered labels the person chose only against this schema; the call's
    // input (a command, a file's contents) must not outlive the open prompt.
    const w = world(on)
    const held = ask($, 'AskUserQuestion', QUESTIONS)
    await w.until(() => w.published().length === 1)
    const id = w.published()[0] ?? ''
    w.check.decision = 'allow'
    await $.tool.call({ tool: 'AskUserQuestion', input: QUESTIONS, tool_use_id: 'toolu_1' } as never)
    await held
    const ended = w.ended(id)
    expect(ended.questions.map((q: { id: string; options: unknown }) => [q.id, q.options])).toEqual(
      [['Which colour?', [['Blue', ''], ['Red', '']]], ['Which fruits?', [['Apple', ''], ['Plum', '']]]])
    expect(JSON.stringify(ended)).not.toContain('"input"')
  })

  test('the turn ending closes what it left open', async ($, on) => {
    const w = world(on)
    await $.turn.start({ text: 'go', turnId: 't1' })
    const held = ask($, 'Bash', BASH, SESSION)
    await w.until(() => w.published().length === 1)
    const id = w.published()[0] ?? ''
    await $.turn.complete({ turnId: 't1', answer: '', isAborted: true } as never)
    expect(await held).toEqual({})
    expect(w.ended(id)).toMatchObject({ how: 'closed', cause: 'interrupted' })
  })

  test('a prompt is paired with its own call by its input, whatever order the prompts come in', async ($, on) => {
    // A prompt names no call. Paired by order, two parallel calls of one tool could take each other's call ID: the
    // wrong prompt would close when the other call runs, and the outcome would credit the wrong answer.
    const w = world(on)
    const raise = (input: unknown) => $.classic.PermissionRequest({ tool_name: 'Read', tool_input: input, session_id: 's1',
      transcript_path: '/home/.claude/projects/p/s1.jsonl', permission_mode: 'default' } as never)
    await $.tool.check({ tool: 'Read', input: { file_path: '/x/1' }, tool_use_id: 'toolu_A', approver: 'person' } as never)
    await $.tool.check({ tool: 'Read', input: { file_path: '/x/2' }, tool_use_id: 'toolu_B', approver: 'person' } as never)
    void raise({ file_path: '/x/2' })  // B's prompt first
    void raise({ file_path: '/x/1' })
    await w.until(() => w.published().length === 2)
    const byFile = Object.fromEntries(w.published().map(w.record).map((r) => [r.detail.input.file_path, r.tool_use_id]))
    expect(byFile).toEqual({ '/x/1': 'toolu_A', '/x/2': 'toolu_B' })
    // Two identical calls asked at once, and a prompt with no asked call: neither is held.
    await $.tool.check({ tool: 'Read', input: { file_path: '/x/3' }, tool_use_id: 'toolu_C', approver: 'person' } as never)
    await $.tool.check({ tool: 'Read', input: { file_path: '/x/3' }, tool_use_id: 'toolu_D', approver: 'person' } as never)
    expect(await raise({ file_path: '/x/3' })).toEqual({})
    expect(await raise({ file_path: '/x/9' })).toEqual({})
    const unpaired = w.published().map(w.record).filter((r) => ['/x/3', '/x/9'].includes(r.detail.input.file_path))
    expect(unpaired.map((r) => [r.tool_use_id, r.answerable])).toEqual([[null, false], [null, false]])
  })

  test("a prompt another hook decides, or one mv2 can't hold, is never held", async ($, on) => {
    // The person's own PermissionRequest hooks answer first; a subagent's ask may have no dialog (Claude denies it
    // once hooks return), and holding one with duplicate question text would map answers to the wrong question.
    const decided = { decision: { behavior: 'deny', message: 'by policy' } }
    const w1 = world(on, { below: decided })
    expect(await ask($, 'Bash', BASH, SESSION)).toEqual(decided)
    expect(w1.published()).toEqual([])
  })

  test("a subagent's ask and duplicate questions are listed but not held", async ($, on) => {
    const w = world(on)
    expect(await ask($, 'Bash', BASH, SESSION, { agent_id: 'sub-1' })).toEqual({})
    const twice = { questions: [{ question: 'Same?', options: [{ label: 'Yes' }] }, { question: 'Same?', options: [{ label: 'No' }] }] }
    expect(await ask($, 'AskUserQuestion', twice)).toEqual({})
    const records = w.published().map(w.record)
    expect(records.map((r) => [r.kind, r.answerable, r.subagent])).toEqual([['permission', false, 'sub-1'], ['question', false, null]])
    expect(records.every((r) => typeof r.reason === 'string' && r.reason.includes('answer it in the terminal'))).toBe(true)
  })

  test("an MCP form is listed, never held, until Claude has its result", async ($, on) => {
    const w = world(on)
    await $.classic.Elicitation({ mcp_server_name: 'docs', message: 'Pick a page', elicitation_id: 'el-1', session_id: 's1' } as never)
    await w.until(() => w.published().length === 1)
    const id = w.published()[0] ?? ''
    expect(w.record(id)).toMatchObject({ kind: 'form', answerable: false, summary: 'docs: Pick a page' })
    await $.classic.ElicitationResult({ mcp_server_name: 'docs', elicitation_id: 'el-1', action: 'accept' } as never)
    await w.until(() => w.ended(id) !== null)
    expect(w.ended(id)).toMatchObject({ how: 'closed', cause: 'person' })
  })

  test("an old conversation's prompt end goes before the switch's move, though queued behind the new one's open", async ($, on) => {
    // Core takes prompt edges only for the conversation it has recorded: the old one's must reach it before the move,
    // the new one's after. Sent only from the queue's head, the old one's end waited behind the new one's open until
    // the move's bound (about 32 s), then went after the move, to a core that no longer had its conversation.
    const w = world(on)
    await $.turn.start({ text: 'go', turnId: 't1' })  // the session is known: s1
    const first = ask($, 'Bash', BASH)
    await w.until(() => w.published().length === 1)
    const old = w.published()[0] ?? ''
    await $.classic.SessionStart({ source: 'clear', session_id: 's2' } as never)  // Claude doesn't name s2 yet
    const second = ask($, 'Bash', BASH, undefined, { session_id: 's2', transcript_path: '/home/.claude/projects/p/s2.jsonl' })
    await w.until(() => w.published().length === 2)
    w.answer(old, { choice: 'allow' })
    await first
    await w.clock.settle()
    w.processFile.id = 's2'
    const named = w.clock.now()
    for (let i = 0; i < 400 && w.moves.length === 0; i++) await w.clock.advance(100)
    expect(w.moves.map((m) => [m.previous, m.session])).toEqual([['s1', 's2']])
    const closedAt = w.reports.findIndex((r) => r.session === 's1' && r.events.some((e) => e.type === 'closed' && e.id === old))
    expect(closedAt).toBeGreaterThanOrEqual(0)
    expect(closedAt).toBeLessThan(w.moves[0]?.after ?? -1)
    expect((w.moves[0]?.at ?? Infinity) - named).toBeLessThan(5000)
    await closed(w, [second])
  })

  test('end records are kept for a while, then pruned', async ($, on) => {
    // An end record is how a late answer learns the prompt was already answered; kept forever they would grow with
    // every prompt, and pruned at once a late answer would read as unknown.
    const day = 24 * 3600 * 1000
    const now = 10 * day
    const id = (n: number) => `${String(n).padStart(8, '0')}-0000-4000-8000-000000000000`
    const files: Record<string, string> = {}
    const mtimes: Record<string, number> = {}
    for (let n = 0; n < 60; n++) {
      files[`${FOLDER}/${id(n)}.end.json`] = '{}'
      mtimes[`${FOLDER}/${id(n)}.end.json`] = n < 10 ? now - 2 * day - n : now - 1000 - n  // ten old, fifty from a burst today
    }
    const w = world(on, { files, mtimes, now })
    await $.session.start({ cwd: '/w' } as never)
    await w.until(() => [...w.files.keys()].filter((p) => p.endsWith('.end.json')).length < 60)
    // Every record from today stays, past the newest 48 too; only the day-old ones go.
    const kept = [...w.files.keys()].filter((p) => p.endsWith('.end.json'))
    expect(kept.length).toBe(50)
    for (let n = 10; n < 60; n++) expect(kept).toContain(`${FOLDER}/${id(n)}.end.json`)
  })

  test('a session start closes what an earlier load of the mod published', async ($, on) => {
    // After a reload the old hook may be gone: its prompt would stay listed forever, and an answer to it unread.
    const old = '11111111-2222-4333-8444-555555555555'
    const w = world(on, { files: { [`${FOLDER}/${old}.json`]: '{"schema":1}' } })
    await $.session.start({ cwd: '/w' } as never)
    await w.until(() => w.ended(old) !== null && !w.files.has(`${FOLDER}/${old}.json`))
    expect(w.ended(old)).toMatchObject({ how: 'closed', cause: 'stopped' })
  })

  test("each prompt's open and end reach core with the status reports, the end before its wait ends", async ($, on) => {
    // A plugin reads 'the agent left waiting' as 'every prompt of that wait has closed', and follows prompts by these
    // events alone: an end sent after the status change, or one never sent, leaves it showing a prompt that is gone.
    const w = world(on)
    const raised: Promise<unknown>[] = []
    const prompt = async (raise: () => Promise<unknown>) => {
      const before = new Set(w.published())
      raised.push(raise())
      await w.until(() => w.published().some((id) => !before.has(id)))
      return w.published().find((p) => !before.has(p)) ?? ''
    }
    const ids: Record<string, string> = {}
    // mv2 answers it, then its call runs.
    ids.answered = await prompt(() => ask($, 'Bash', BASH, SESSION))
    w.answer(ids.answered ?? '', { choice: 'allow' })
    await w.until(() => w.ended(ids.answered ?? '') !== null)
    w.check.decision = 'allow'
    await $.tool.call({ tool: 'Bash', input: BASH, tool_use_id: 'toolu_1' } as never)
    // Its call runs without mv2's answer: the person allowed it in the terminal.
    w.check.decision = 'ask'
    ids.resolved = await prompt(() => ask($, 'Bash', BASH, SESSION))
    w.check.decision = 'allow'
    await $.tool.call({ tool: 'Bash', input: BASH, tool_use_id: 'toolu_1' } as never)
    // The turn ends under it.
    await $.turn.start({ text: 'go', turnId: 't1' })
    w.check.decision = 'ask'
    ids.turn = await prompt(() => ask($, 'Bash', BASH, SESSION))
    await $.turn.complete({ turnId: 't1', answer: '', isAborted: false } as never)
    // An MCP form, answered in the terminal.
    ids.form = await prompt(() => $.classic.Elicitation({ mcp_server_name: 'docs', message: 'Pick', elicitation_id: 'el-1', session_id: 's1' } as never))
    await $.classic.ElicitationResult({ mcp_server_name: 'docs', elicitation_id: 'el-1', action: 'accept' } as never)
    // A subagent's ask, listed only, until its call ends.
    w.check.decision = 'ask'
    ids.subagent = await prompt(() => ask($, 'Bash', BASH, SESSION, { agent_id: 'sub-1' }))
    w.check.decision = 'allow'
    await $.tool.call({ tool: 'Bash', input: BASH, tool_use_id: 'toolu_1' } as never)
    await Promise.all(raised)  // each hook returns once its prompt ends
    await w.clock.settle()
    const expected: Record<string, [string, string, string | null]> = {
      answered: ['permission', 'submitted', null], resolved: ['permission', 'gone', 'closed'], turn: ['permission', 'gone', 'turn_ended'],
      form: ['form', 'person', 'person'], subagent: ['permission', 'gone', 'closed'],
    }
    for (const [name, [kind, state, cause]] of Object.entries(expected)) {
      const id = ids[name] ?? ''
      const at = (type: string) => w.reports.findIndex((r) => r.events.some((e) => e.type === type && e.id === id))
      const [opened, closed] = [at('opened'), at('closed')]
      expect([name, opened >= 0, closed >= opened]).toEqual([name, true, true])
      expect(w.reports[opened]?.events.find((e) => e.id === id)).toEqual({ type: 'opened', id, kind, requires_waiting: true })
      expect(w.reports[closed]?.events.find((e) => e.type === 'closed' && e.id === id)).toEqual({ type: 'closed', id, kind, state, cause })
      expect(w.reports[closed]?.session).toBe('s1')
      // From its open to its end, every report said the agent was still waiting: the end went first.
      if (name !== 'subagent') expect([name, w.reports.slice(opened, closed).every((r) => r.status === 'waiting')]).toEqual([name, true])
      // Each once.
      expect(w.reports.flatMap((r) => r.events).filter((e) => e.id === id).length).toBe(2)
    }
  })

  test("a reload's sweep reports the ends of this launch's prompts only", async ($, on) => {
    // The sweep is the only place a reload's lost prompts end; an earlier launch's are closed by core at that
    // launch's end, so reporting them again would open a prompt in the wrong launch.
    const w = world(on, { files: SWEPT })
    await $.session.start({ cwd: '/w' } as never)
    await w.until(() => w.reports.some((r) => r.events.length > 0))
    await w.clock.settle()
    expect(w.reports.flatMap((r) => r.events.map((e) => [r.session, e]))).toEqual(
      [['s1', { type: 'closed', id: OURS, kind: 'question', state: 'gone', cause: 'stopped' }]])
  })

  test('a report core keeps failing drops the prompt events it carried, never the status', async ($, on) => {
    // Kept for good, a batch core can't take would keep every later status from being reported; core closes what it
    // still holds open from its own reads and the launch's end.
    const w = world(on, { files: SWEPT, failReports: 3 })
    await $.session.start({ cwd: '/w' } as never)
    await w.until(() => w.ended(OURS) !== null)
    for (let n = 0; n < 3; n++) {
      await $.turn.start({ text: 'go', turnId: `t${n}` })
      await $.turn.complete({ turnId: `t${n}`, answer: '', isAborted: false } as never)
      await w.clock.settle()
    }
    await $.turn.start({ text: 'go', turnId: 't9' })
    await w.until(() => w.reports.some((r) => r.status === 'working'))
    expect(w.reports.flatMap((r) => r.events)).toEqual([])
  })

  test("a prompt's end carries the state and cause prompt_result gives from its end record alone", () => {
    // The same table as tests/test_claude_prompts.py's for the adapter's prompt_settled: the two must not drift, or a
    // plugin would see one outcome in the event and another from prompt_result.
    const rows: [Record<string, unknown>, { state: string; cause: string | null }][] = [
      [{ how: 'sent' }, { state: 'submitted', cause: null }],
      [{ how: 'refused' }, { state: 'refused', cause: 'invalid_answer' }],
      [{ how: 'closed', cause: 'person' }, { state: 'person', cause: 'person' }],
      [{ how: 'closed', cause: 'interrupted' }, { state: 'gone', cause: 'interrupted' }],
      [{ how: 'closed', cause: 'turn_ended' }, { state: 'gone', cause: 'turn_ended' }],
      [{ how: 'closed', cause: 'stopped' }, { state: 'gone', cause: 'stopped' }],
      [{ how: 'closed', cause: 'resolved' }, { state: 'gone', cause: 'closed' }],
      [{ how: 'closed', cause: 'denied' }, { state: 'gone', cause: 'closed' }],
    ]
    for (const [record, outcome] of rows) expect([record, closedOutcome(record)]).toEqual([record, outcome])
  })


  test("each choice says what it does, and a suggestion that does more than allow the same again is 'other'", async ($, on) => {
    // A plugin picks 'the allow-for-session choice' by meaning: a mode switch called allow_session would let it turn
    // on accept-edits for the whole session thinking it allowed one request again.
    const rows: [Record<string, unknown>, boolean, { meaning?: string; short?: string }][] = [
      [{ type: 'addRules', rules: [], behavior: 'allow', destination: 'session' }, false, { meaning: 'allow_session' }],
      [{ type: 'addRules', rules: [], behavior: 'allow', destination: 'localSettings' }, true, { meaning: 'allow_always' }],
      [{ type: 'addDirectories', directories: ['/tmp/x'], destination: 'session' }, false, { meaning: 'allow_session' }],
      [{ type: 'addDirectories', directories: ['/tmp/x'], destination: 'userSettings' }, true, { meaning: 'allow_always' }],
      [{ type: 'setMode', mode: 'acceptEdits', destination: 'session' }, false, { meaning: 'other', short: 'Allow, accept edits' }],
      [{ type: 'setMode', mode: 'somethingNew', destination: 'session' }, false, { meaning: 'other', short: 'Allow, set mode' }],
      [{ type: 'setMode', mode: 'acceptEdits', destination: 'localSettings' }, true, { meaning: 'other', short: 'Allow, accept edits' }],
      [{ type: 'addRules', rules: [], behavior: 'deny', destination: 'session' }, false, { meaning: 'other', short: 'Allow, change rules' }],
      [{ type: 'removeRules', rules: [], behavior: 'allow', destination: 'session' }, false, { meaning: 'other', short: 'Allow, change rules' }],
    ]
    for (const [suggestion, saved, expected] of rows) expect([suggestion, suggestionMeaning(suggestion, saved)]).toEqual([suggestion, expected])
    for (const row of rows) expect((row[2].short ?? '').length).toBeLessThanOrEqual(24)
    const w = world(on)
    const asks = [ask($, 'Bash', BASH, SESSION)]
    await w.until(() => w.published().length === 1)
    expect(w.record(w.published()[0] ?? '').choices.map((c: { id: string; meaning?: string; short?: string }) => [c.id, c.meaning, c.short]))
      .toEqual([['allow', 'allow', undefined], ['allow_session', 'allow_session', undefined],
        ['allow_session_2', 'other', 'Allow, accept edits'], ['deny', 'deny', undefined]])
    asks.push(ask($, 'ExitPlanMode', PLAN))
    await w.until(() => w.published().length === 2)
    const plan = w.published().map(w.record).find((r) => r.kind === 'plan')
    expect(plan.choices.map((c: { meaning: string }) => c.meaning)).toEqual(['approve_plan', 'keep_planning'])
    await closed(w, asks)
  })

})
