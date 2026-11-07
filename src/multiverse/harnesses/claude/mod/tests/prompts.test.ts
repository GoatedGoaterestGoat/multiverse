// Offline tests of the prompts the mod publishes for mv2 (hooks/prompts.ts): `claude plugin test`. The test's
// hooks stand in for Claude's core and the host beneath the mod: a file system the mod's files land in, and the
// wait process, which ends when its answer file appears or its prompt file goes, or throws when the test presses Esc.
import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

const FOLDER = '/agent/claude-prompts'
const ID = /^\/agent\/claude-prompts\/([0-9a-f-]{36})\.json$/

// `lose`: how many of core's answer files core removes first (it proved the launch ended) before the mod's take.
type Options = { below?: Record<string, unknown>; files?: Record<string, string>; lose?: number; now?: number;
  mtimes?: Record<string, number> }

function world(on: On, options: Options = {}) {
  mock.env(on, { MV2_PYTHON: '/python', MV2_AGENT_DIR: '/agent', MV2_LAUNCH_ID: 'launch-1' })
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
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
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
      for (;;) {
        if (escaped) throw new Error('$.process.run(/bin/sh) aborted')
        if (files.has(answer)) return ok(0)
        if (!files.has(prompt)) return ok(2)
        await new Promise<void>((wake) => waiters.push(wake))
      }
    }
    return ok(0)
  })
  const published = () => [...files.keys()].map((p) => ID.exec(p)?.[1]).filter((id): id is string => id !== undefined)
  return {
    clock, files, published, check, lost,
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
    void ask($, 'Bash', BASH, [...SESSION, ...SAVED])
    await w.until(() => w.published().length === 1)
    const record = w.record(w.published()[0] ?? '')
    expect(record).toMatchObject({ schema: 1, launch: 'launch-1', session: 's1', claude: '2.1.319', tool_use_id: 'toolu_1', kind: 'permission',
      tool: 'Bash', summary: 'touch /tmp/x/a.txt', answerable: true, subagent: null, transcript: { path: '/home/.claude/projects/p/s1.jsonl', offset: 4096 } })
    expect(record.detail).toMatchObject({ input: BASH, reason: 'touch in /tmp/x needs approval' })
    expect(record.choices.map((c: { id: string; saves_rule: boolean }) => [c.id, c.saves_rule])).toEqual(
      [['allow', false], ['allow_session', false], ['allow_session_2', false], ['allow_always', true], ['deny', false]])
    void ask($, 'AskUserQuestion', QUESTIONS)
    await w.until(() => w.published().length === 2)
    const question = w.published().map(w.record).find((r) => r.kind === 'question')
    expect(question.questions.map((q: { id: string; multiple: boolean; options: unknown }) => [q.id, q.multiple, q.options])).toEqual(
      [['Which colour?', false, [['Blue', ''], ['Red', '']]], ['Which fruits?', true, [['Apple', ''], ['Plum', '']]]])
    expect(question.choices).toEqual([])
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
})
