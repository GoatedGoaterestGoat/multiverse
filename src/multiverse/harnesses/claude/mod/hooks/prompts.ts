// The prompts an agent waits on, for mv2 to read and answer (mv2 answer; a plugin's agent.answer_prompt).
//
// Claude runs classic.PermissionRequest beside its own dialog, and whichever answers first wins. Once the rest of
// the hook chain has left the prompt undecided, the mod publishes it as claude-prompts/<id>.json in the agent's
// folder, with the choices it can return, and holds the hook in $.process.run waits: time in a `$` call isn't the
// hook's own budget, and Esc ends the wait with the dispatch. Core writes the answer once, as prompts/<id>.answer.json
// (its own folder, which it also cleans); the hook, still holding that prompt, first takes the file by renaming it
// (core proves an answer was never taken by winning its own unlink of that name, so a copy read before a failed
// take is never acted on), then checks the answer against what it published and returns its decision. Whatever ends a prompt writes <id>.end.json first and then removes the
// published file: "sent" (mv2's answer went to Claude, whose stored rows say whether it took), "refused" (left to
// the person) or "closed" with the cause. An answer for a prompt no hook holds any more is never read.
import type { EngineInterface } from 'claude-code'

export const FOLDER = 'claude-prompts'
const SCHEMA = 1
// A wait looks for the answer and the prompt's own file every 0.2 s for a minute; the hook then looks again.
const WAITER = 'i=0; while [ $i -lt 300 ]; do [ -f "$1" ] && exit 0; [ -f "$2" ] || exit 2; sleep 0.2; i=$((i+1)); done; exit 1'
const DETAIL_LIMIT = 65536
// End records answer outcomes, a late answer's "already answered" included: the newest few dozen are kept, and
// older ones go once they are a day old (core keeps its own record of every outcome it settled).
const KEEP_ENDS = 48
const KEEP_MS = 24 * 3600 * 1000
const PRUNE_EVERY = 16
const ANSWER_LIMIT = 1048576
// A suggestion with one of these destinations also changes the person's saved settings: a person's answer only.
const SAVED = new Set(['userSettings', 'projectSettings', 'localSettings'])
const SCOPES: Record<string, string> = {
  session: 'for this session', cliArg: 'for this session', localSettings: 'saved to your local project settings',
  projectSettings: 'saved to your project settings', userSettings: 'saved to your user settings',
}

type Choice = { id: string; label: string; saves_rule: boolean; takes_message: boolean }
type Question = { id: string; header: string; question: string; options: [string, string][]; multiple: boolean;
  free_text: boolean }
type Decision = Record<string, unknown>

type Open = {
  id: string
  folder: string
  answer: string                     // core's answer file for it
  launch?: string
  session?: string
  toolUseId?: string
  transcript?: { path: string; offset: number }   // where its call's rows will be stored
  key?: string                       // its entry in the mod's waiting map
  kind: string
  input: Record<string, unknown>     // the call's whole input: an answer to questions or a plan rewrites it
  decisions: Map<string, Decision>   // what each offered choice returns
  saves: Set<string>
  questions: Question[]
  choices: Choice[]                  // as published: the end record keeps them, so a person-first outcome can say
                                     // which offered labels the person picked (never the call's input)
  ended?: Promise<void>
}

const open = new Map<string, Open>()
let endsSincePrune = 0
const instance = newId()
let version: string | undefined

function newId(): string {
  const random = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto?.randomUUID
  if (random !== undefined) return random.call((globalThis as { crypto: unknown }).crypto)
  const part = () => Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0')
  return `${part()}-${part().slice(0, 4)}-4${part().slice(0, 3)}-a${part().slice(0, 3)}-${part()}${part().slice(0, 4)}`
}

function bounded(value: unknown): unknown {
  const text = JSON.stringify(value ?? null)
  return text.length <= DETAIL_LIMIT ? value : { truncated: true, preview: text.slice(0, DETAIL_LIMIT) }
}

function line(text: unknown, limit = 200): string {
  const first = typeof text === 'string' ? text.trim().split('\n')[0] ?? '' : ''
  return first.length > limit ? `${first.slice(0, limit - 1)}…` : first
}

function summary(tool: string, input: Record<string, unknown>): string {
  if (tool === 'Bash') return line(input.command) || 'Run a command'
  for (const key of ['file_path', 'notebook_path', 'path', 'url', 'query', 'pattern']) {
    if (typeof input[key] === 'string') return `${tool} ${line(input[key])}`
  }
  return tool
}

// What a suggestion does, in mv2's words (never claimed to be the dialog's label).
function describe(s: Record<string, unknown>): string {
  const scope = SCOPES[String(s.destination)] ?? `in ${String(s.destination)}`
  const rules = Array.isArray(s.rules)
    ? s.rules.map((r: { toolName?: string; ruleContent?: string }) => r.ruleContent ? `${r.toolName}(${r.ruleContent})` : String(r.toolName)).join(', ')
    : ''
  const dirs = Array.isArray(s.directories) ? s.directories.join(', ') : ''
  if (s.type === 'addRules' && s.behavior === 'allow') return `Allow, and also allow ${rules}, ${scope}`
  if (s.type === 'addDirectories') return `Allow, and allow access to ${dirs}, ${scope}`
  if (s.type === 'setMode') return `Allow, and switch to ${String(s.mode)} mode, ${scope}`
  return `Allow, and ${String(s.type)} ${rules || dirs}, ${scope}`.replace(/ +,/, ',')
}

function permission(suggestions: unknown, plan: boolean) {
  const choices: Choice[] = []
  const decisions = new Map<string, Decision>()
  const saves = new Set<string>()
  const add = (choice: Choice, decision: Decision) => { choices.push(choice); decisions.set(choice.id, decision); if (choice.saves_rule) saves.add(choice.id) }
  if (plan) {
    add({ id: 'allow', label: 'Approve the plan; edits are then asked for', saves_rule: false, takes_message: false }, { behavior: 'allow' })
    add({ id: 'deny', label: 'Keep planning, with your feedback', saves_rule: false, takes_message: true }, { behavior: 'deny' })
    return { choices, decisions, saves }
  }
  add({ id: 'allow', label: 'Allow once', saves_rule: false, takes_message: false }, { behavior: 'allow' })
  const seen = new Map<string, number>()
  for (const s of Array.isArray(suggestions) ? suggestions : []) {
    if (s === null || typeof s !== 'object') continue
    const saved = SAVED.has(String((s as { destination?: unknown }).destination))
    const base = saved ? 'allow_always' : 'allow_session'
    const n = (seen.get(base) ?? 0) + 1
    seen.set(base, n)
    add({ id: n === 1 ? base : `${base}_${n}`, label: describe(s as Record<string, unknown>), saves_rule: saved, takes_message: false },
      { behavior: 'allow', updatedPermissions: [s] })
  }
  // No "deny and stop the turn" (interrupt): Claude stores it exactly as the person's No or Esc, with no decision
  // row, so mv2 could never tell whose answer took.
  add({ id: 'deny', label: 'Deny; the agent carries on', saves_rule: false, takes_message: true }, { behavior: 'deny' })
  return { choices, decisions, saves }
}

function questions(input: Record<string, unknown>): Question[] {
  const list = Array.isArray(input.questions) ? input.questions : []
  return list.filter((q) => q !== null && typeof q === 'object').map((q: Record<string, unknown>) => ({
    id: String(q.question ?? ''),
    header: typeof q.header === 'string' ? q.header : '',
    question: String(q.question ?? ''),
    options: (Array.isArray(q.options) ? q.options : []).map((o: { label?: unknown; description?: unknown }) =>
      [String(o?.label ?? ''), typeof o?.description === 'string' ? o.description : ''] as [string, string]),
    multiple: q.multiSelect === true,
    free_text: true,
  }))
}

async function now($: EngineInterface): Promise<string> {
  return new Date(await $.clock.now()).toISOString()
}

async function remove($: EngineInterface, paths: string[]) {
  try { await $.process.run(['rm', '-f', ...paths], { timeoutMs: 10000 }) } catch { /* the next session start removes it */ }
}

// End a prompt once: its end record first, then its files, so a reader never finds neither.
function end($: EngineInterface, prompt: Open, record: Record<string, unknown>): Promise<void> {
  if (prompt.ended !== undefined) return prompt.ended
  if (open.get(prompt.id) === prompt) open.delete(prompt.id)
  const base = `${prompt.folder}/${prompt.id}`
  prompt.ended = (async () => {
    try {
      // The call and where its rows go: how mv2 reads from Claude's own record whose answer took.
      await $.fs.write(`${base}.end.json`, JSON.stringify({ schema: SCHEMA, id: prompt.id, kind: prompt.kind,
        launch: prompt.launch ?? null, tool_use_id: prompt.toolUseId ?? null, transcript: prompt.transcript ?? null, choices: prompt.choices,
        questions: prompt.questions, ...record, at: await now($) }))
    } catch { /* the prompt's own file still goes: its absence is the end */ }
    await remove($, [`${base}.json`])
    endsSincePrune += 1
    if (endsSincePrune >= PRUNE_EVERY) await prune($, prompt.folder)
  })()
  return prompt.ended
}

async function prune($: EngineInterface, folder: string) {
  endsSincePrune = 0
  let entries
  try { entries = await $.fs.list(folder) } catch { return }
  const ends = entries.filter((entry) => /^[0-9a-f-]{36}\.end\.json$/.test(entry.name)).sort((a, b) => b.mtimeMs - a.mtimeMs)
  const current = await $.clock.now()
  const old = ends.slice(KEEP_ENDS).filter((entry) => current - entry.mtimeMs > KEEP_MS)
  if (old.length > 0) await remove($, old.map((entry) => `${folder}/${entry.name}`))
}

// Calls and turns that ended close the prompts that belonged to them.
export function closeKeys($: EngineInterface, keys: string[], cause: string) {
  for (const prompt of [...open.values()]) if (prompt.key !== undefined && keys.includes(prompt.key)) void end($, prompt, { how: 'closed', cause })
}

export function closeAll($: EngineInterface, cause: string) {
  for (const prompt of [...open.values()]) void end($, prompt, { how: 'closed', cause })
}

// Claude gives a hook's deny message to the model as the call's bare result, with nothing saying who wrote it, and
// Haiku took a bare one for an error. So it says who answered, from core's `caller` ("the user", the model's word
// for the person, or the plugin by name), and that the message is their answer; a deny with none still says so.
// An answer without a readable caller says only that it came through mv2.
const PLUGIN_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/

function denial(kind: string, message: string | undefined, caller: unknown): string {
  const said = message === undefined ? '.' : `: ${message}`
  const c = caller !== null && typeof caller === 'object' ? caller as { kind?: unknown; name?: unknown } : {}
  const plugin = c.kind === 'plugin' && typeof c.name === 'string' && PLUGIN_NAME.test(c.name) ? c.name : undefined
  if (kind === 'plan') {
    if (plugin !== undefined) return `The plugin ${plugin}, answering for the user through mv2, wants you to keep planning${said}`
    if (c.kind === 'person') return `The user wants you to keep planning, answering through mv2${said}`
    return `You were asked through mv2 to keep planning${said}`
  }
  if (plugin !== undefined) return `The plugin ${plugin}, answering for the user through mv2, declined this tool use${said}`
  if (c.kind === 'person') return `The user declined this tool use through mv2${said}`
  return `This tool use was declined through mv2${said}`
}

// The answer, checked against what this hook published; undefined with a reason when it doesn't fit.
function decision(prompt: Open, answer: Record<string, unknown>): { decision?: Decision; reason?: string; saved?: boolean } {
  if (answer.schema !== SCHEMA || answer.id !== prompt.id || answer.launch_id !== (prompt.launch ?? null)
      || answer.native_id !== (prompt.session ?? null)) return { reason: 'The answer names another prompt' }
  const message = typeof answer.message === 'string' && answer.message.trim() ? answer.message : undefined
  if (prompt.kind === 'question') {
    const given = answer.answers
    if (given === null || typeof given !== 'object' || Array.isArray(given)) return { reason: 'The answer gives no answers to the questions' }
    const answers: Record<string, string> = {}
    for (const q of prompt.questions) {
      const labels = (given as Record<string, unknown>)[q.id]
      if (!Array.isArray(labels) || labels.length === 0 || !labels.every((l) => typeof l === 'string' && l.trim())) {
        return { reason: 'Every question needs an answer' }
      }
      if (!q.multiple && labels.length > 1) return { reason: 'A question that takes one answer was given several' }
      answers[q.id] = labels.join(', ')
    }
    if (Object.keys(given).some((k) => !prompt.questions.some((q) => q.id === k))) return { reason: 'The answer names a question this prompt does not ask' }
    return { decision: { behavior: 'allow', updatedInput: { ...prompt.input, answers } }, saved: false }
  }
  const choice = typeof answer.choice === 'string' ? answer.choice : ''
  const base = prompt.decisions.get(choice)
  if (base === undefined) return { reason: 'That choice is not offered for this prompt' }
  if (prompt.saves.has(choice) && answer.allow_rule !== true) return { reason: "Only the person can choose a rule saved to their settings" }
  if (prompt.kind === 'plan' && base.behavior === 'allow') return { decision: { behavior: 'allow', updatedInput: prompt.input }, saved: false }
  if (base.behavior === 'deny') return { decision: { ...base, message: denial(prompt.kind, message, answer.caller) }, saved: false }
  return { decision: base, saved: prompt.saves.has(choice) }
}

// Take core's answer file by renaming it; false when core removed it first (it had proved the prompt's launch or
// conversation ended): then nothing from it is ever acted on.
async function claim($: EngineInterface, file: string, taken: string): Promise<boolean> {
  try { return (await $.process.run(['mv', file, taken], { timeoutMs: 10000 })).exitCode === 0 } catch { return false }
}

async function take($: EngineInterface, prompt: Open, file: string): Promise<Decision | undefined | 'lost'> {
  const taken = `${file.replace(/\.answer\.json$/, '')}.answer.taken.json`
  if (!await claim($, file, taken)) return 'lost'
  let answer: Record<string, unknown> | undefined
  try {
    const text = await $.fs.read(taken)
    if (typeof text === 'string' && text.length <= ANSWER_LIMIT) answer = JSON.parse(text)
  } catch { /* unreadable: refused below */ }
  await remove($, [taken])
  // Closed while the answer was being read: it is never applied.
  if (open.get(prompt.id) !== prompt) return undefined
  const checked = answer === undefined ? { reason: 'The answer could not be read' } : decision(prompt, answer)
  if (checked.decision === undefined) {
    await end($, prompt, { how: 'refused', reason: checked.reason })
    return undefined
  }
  await end($, prompt, { how: 'sent', choice: typeof answer?.choice === 'string' ? answer.choice : null, saved_rule: checked.saved === true })
  return checked.decision
}

async function wait($: EngineInterface, prompt: Open, signal: AbortSignal | undefined): Promise<Decision | undefined> {
  const base = `${prompt.folder}/${prompt.id}`
  while (open.get(prompt.id) === prompt) {
    let code: number
    try {
      code = (await $.process.run(['/bin/sh', '-c', WAITER, 'sh', prompt.answer, `${base}.json`], { timeoutMs: 75000 })).exitCode
    } catch {
      // Esc ends the hook's dispatch and kills its wait: nothing is shown for it.
      await end($, prompt, { how: 'closed', cause: 'interrupted' })
      return undefined
    }
    if (signal?.aborted) {
      await end($, prompt, { how: 'closed', cause: 'interrupted' })
      return undefined
    }
    if (code === 0) {
      const decided = await take($, prompt, prompt.answer)
      if (decided !== 'lost') return decided
      continue  // core removed the answer first: keep holding the prompt for the person
    }
    // The prompt's file is gone and nothing here closed it: a newer mod took over this session's prompts.
    if (code === 2 && open.get(prompt.id) === prompt) {
      await end($, prompt, { how: 'closed', cause: 'stopped' })
      return undefined
    }
  }
  return undefined
}

type Link = { toolUseId?: string; key: string; reason?: string }

// classic.PermissionRequest: publish the prompt and hold it until mv2 answers or it closes.
export async function hold<R extends object>($: EngineInterface, e: { tool_name: string; tool_input: unknown;
  permission_suggestions?: unknown; permission_mode?: string; agent_id?: string; session_id?: string; transcript_path?: string },
  below: R, link: Link, signal: AbortSignal | undefined): Promise<R> {
  const agentFolder = await $.env.get('MV2_AGENT_DIR')
  if (!agentFolder) return below
  const input = e.tool_input !== null && typeof e.tool_input === 'object' ? e.tool_input as Record<string, unknown> : {}
  const kind = e.tool_name === 'AskUserQuestion' ? 'question' : e.tool_name === 'ExitPlanMode' ? 'plan' : 'permission'
  const { choices, decisions, saves } = kind === 'question' ? { choices: [], decisions: new Map(), saves: new Set<string>() }
    : permission(e.permission_suggestions, kind === 'plan')
  const asked = kind === 'question' ? questions(input) : []
  let reason: string | undefined
  if (e.agent_id !== undefined) reason = "A subagent's prompt: answer it in the terminal"
  else if (link.toolUseId === undefined) reason = "mv2 can't tell which call this prompt is for: answer it in the terminal"
  else if (asked.length === 0 && kind === 'question') reason = 'A question prompt with no questions mv2 can read: answer it in the terminal'
  else if (new Set(asked.map((q) => q.id)).size !== asked.length) reason = 'Two of its questions have the same text: answer it in the terminal'
  const id = newId()
  const launch = (await $.env.get('MV2_LAUNCH_ID')) || undefined
  const prompt: Open = { id, folder: `${agentFolder}/${FOLDER}`, answer: `${agentFolder}/prompts/${id}.answer.json`, launch,
    session: e.session_id, toolUseId: link.toolUseId, key: link.key, kind, input, decisions, saves, questions: asked, choices }
  if (e.transcript_path) {
    try { prompt.transcript = { path: e.transcript_path, offset: (await $.fs.stat(e.transcript_path)).size } } catch { /* read from the start */ }
  }
  try { version ??= (await $.session.version()).version } catch { /* unknown: mv2 treats it as unchecked */ }
  open.set(prompt.id, prompt)
  try {
    await $.fs.write(`${prompt.folder}/${prompt.id}.json`, JSON.stringify({
      schema: SCHEMA, id: prompt.id, instance, launch: launch ?? null, session: e.session_id ?? null,
      claude: version ?? null, tool_use_id: link.toolUseId ?? null, kind, tool: e.tool_name, asked_at: await now($),
      summary: kind === 'question' ? line(asked[0]?.question) || 'A question' : kind === 'plan' ? 'Approve the plan' : summary(e.tool_name, input),
      detail: { input: bounded(input), reason: link.reason ?? null, permission_mode: e.permission_mode ?? null },
      choices, questions: asked, answerable: reason === undefined, reason: reason ?? null, subagent: e.agent_id ?? null,
      transcript: prompt.transcript ?? null,
    }))
  } catch {
    open.delete(prompt.id)
    return below
  }
  // Listed but never held: a subagent's ask may have no dialog, and holding it would block the subagent.
  if (reason !== undefined) return below
  const decided = await wait($, prompt, signal)
  return decided === undefined ? below : { ...below, decision: decided } as R
}

// MCP forms: Claude asks plugins before it draws the form, so the form is listed, never held.
export async function form($: EngineInterface, key: string, e: { mcp_server_name: string; message?: string; mode?: string;
  requested_schema?: unknown; session_id?: string }) {
  const agentFolder = await $.env.get('MV2_AGENT_DIR')
  if (!agentFolder) return
  const id = newId()
  const prompt: Open = { id, folder: `${agentFolder}/${FOLDER}`, answer: `${agentFolder}/prompts/${id}.answer.json`, key,
    kind: 'form', input: {}, decisions: new Map(), saves: new Set(), questions: [], choices: [] }
  open.set(prompt.id, prompt)
  try {
    await $.fs.write(`${prompt.folder}/${prompt.id}.json`, JSON.stringify({
      schema: SCHEMA, id: prompt.id, instance, launch: (await $.env.get('MV2_LAUNCH_ID')) || null, session: e.session_id ?? null,
      claude: version ?? null, tool_use_id: null, kind: 'form', tool: null, asked_at: await now($),
      summary: `${e.mcp_server_name}: ${line(e.message)}`.replace(/: $/, ''),
      detail: { server: e.mcp_server_name, message: typeof e.message === 'string' ? e.message.slice(0, 4096) : null, mode: e.mode ?? null,
        schema: bounded(e.requested_schema) },
      choices: [], questions: [], answerable: false,
      reason: "An MCP server's form: answer it in the terminal (Claude asks plugins before it draws the form)",
      subagent: null, transcript: null,
    }))
  } catch {
    open.delete(prompt.id)
  }
}

// A session start (or a reload) owns no prompt yet: what an earlier load of the mod published is closed.
export async function sweep($: EngineInterface) {
  const agentFolder = await $.env.get('MV2_AGENT_DIR')
  if (!agentFolder) return
  const folder = `${agentFolder}/${FOLDER}`
  let names: string[]
  try { names = (await $.fs.list(folder)).map((entry) => entry.name) } catch { return }
  for (const name of names) {
    const id = /^([0-9a-f-]{36})\.json$/.exec(name)?.[1]
    if (id === undefined || [...open.values()].some((p) => p.id === id)) continue
    if (!names.includes(`${id}.end.json`)) {
      let old: Record<string, unknown> = {}
      try { old = JSON.parse(await $.fs.read(`${folder}/${name}`)) } catch { /* its call stays unknown */ }
      try {
        await $.fs.write(`${folder}/${id}.end.json`, JSON.stringify({ schema: SCHEMA, id, kind: old.kind ?? null,
          launch: old.launch ?? null, tool_use_id: old.tool_use_id ?? null, transcript: old.transcript ?? null, how: 'closed', cause: 'stopped', at: await now($) }))
      } catch { continue }
    }
    await remove($, [`${folder}/${id}.json`])
  }
  await prune($, folder)
}
