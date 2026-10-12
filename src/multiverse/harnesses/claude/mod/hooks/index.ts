import type { EngineInterface, Register } from 'claude-code'
import { registerCompaction } from './compaction'
import { DESCRIPTION, HINT, NAME, registerMultiverse } from './multiverse'
import { INBOX_LIMIT, currentLine, defaultConfig, isSubverse, parseConfig, parseItems, render, setBase, withCompaction } from './statusline'
import type { Config, Facts, PluginItem } from './statusline'
import { registerUsage } from './usage'

// Multiverse sends each message over this session's peer inbox at `next`
// priority, its text between `<<MV2 steer id=UUID>>` (or `follow-up`) and
// `<</MV2>>`. The model reads the text alone, without the tags or Claude's peer
// wrapper:
// - A steer arriving mid-turn loses its tags in session.receive, so the queued
//   message shows clean, merges at the next tool result as a queued command,
//   and session.append stores it without Claude's peer wrapper.
// - A steer arriving while idle, and every follow-up, is taken in
//   session.receive and submitted as the person's own words, which runs as a
//   turn of its own once the current turn has ended.
// - A steer queued mid-turn with no tool result after it would start its own
//   turn as a wrapped peer prompt, so prompt.submit drops it and submits it.
// When a message's row enters the conversation, the mod writes a receipt in the
// agent's directory for `mv2 deliver --check`.
// A question tagged `ask` never enters the conversation: the mod answers it
// with $.model.fork over the session's own transcript and writes the answer in
// the agent's directory for `mv2 ask`.
//
// It also reports this session's status to Multiverse: working while a
// main-loop turn runs, waiting while a permission prompt, a question or an MCP
// elicitation waits on the person, idle otherwise.

// `<<MV2>>` alone is the first version's tag, still found in older history.
const TAG = /<<MV2(?: (steer|follow-up|ask))?(?: id=([0-9a-fA-F-]{36}))?>>([\s\S]*)<<\/MV2>>/

type Message = { text: string; mode: "steer" | "follow-up" | "ask"; id?: string }

// The tagged message in a text; undefined when the text carries none.
function tagged(text: string): Message | undefined {
  const match = TAG.exec(text)
  if (match === null) return undefined
  const mode = match[1] === "follow-up" || match[1] === "ask" ? match[1] : "steer"
  return { text: match[3] ?? "", mode, id: match[2] }
}

// Main-loop turn state: turn.start fires for the main loop only, and a
// subagent's turn.complete carries its agentId.
let turnRunning = false

// What waits on the person, by key (`question:<id>`, `permission:<id>`,
// `elicitation:<id>`), with the tool it waits for. Any entry makes the session
// `waiting`.
const waiting = new Map<string, string>()

// Tool calls the permission check answered `ask`, by tool_use_id, until a
// prompt opens for one of them (the mode may settle the ask instead).
const asked = new Map<string, string>()

// Tools whose call is itself a question to the person.
const QUESTIONS = new Set(["AskUserQuestion", "ExitPlanMode"])

// The status line under the prompt (statusline.ts holds the logic and the slot
// it shares with /compact's progress). A timer looks at its files' size and time,
// with no subprocess, and reads only what changed; `mv2 inbox --peek` runs only
// when the inbox changed. Plugin items add runtime.json (which declares them) and
// the file of each item that is on.
const REFRESH_MS = 2000
const watched = new Map<string, string>()
let statusConfig: Config = defaultConfig()
const statusFacts: Facts = { inbox: 0 }
let configText: string | undefined
let declared: PluginItem[] = []
const itemTexts: Record<string, string> = {}
let statusTimer: { cancel: () => void } | undefined
let statusRunning = false
let shown: string | undefined

// True when the file differs from the last time it was looked at (or is new).
async function changed($: EngineInterface, path: string): Promise<boolean> {
  let key = "missing"
  try {
    const stat = await $.fs.stat(path)
    key = `${stat.mtimeMs}:${stat.size}`
  } catch { /* no such file */ }
  if (watched.get(path) === key) return false
  watched.set(path, key)
  return true
}

async function readFile($: EngineInterface, path: string): Promise<string | undefined> {
  try {
    return await $.fs.read(path)
  } catch {
    return undefined
  }
}

function field(data: unknown, key: string): string | undefined {
  const value = typeof data === "object" && data !== null ? (data as Record<string, unknown>)[key] : undefined
  return typeof value === "string" ? value : undefined
}

async function refreshStatus($: EngineInterface) {
  if (statusRunning) return
  statusRunning = true
  try {
    const [directory, universe, cli] = await Promise.all(
      [$.env.get("MV2_AGENT_DIR"), $.env.get("MV2_UNIVERSE"), $.env.get("MV2_CLI")])
    if (!directory || !universe) return
    const configFile = `${universe}/statusline.json`
    const properties = `${directory}/properties.json`
    const [configChanged, propertiesChanged, inboxChanged, cursorChanged, runtimeChanged] = await Promise.all([
      changed($, configFile), changed($, properties), changed($, `${directory}/inbox.jsonl`), changed($, `${directory}/inbox.cursor`),
      changed($, `${directory}/runtime.json`)])
    let itemsChanged = false
    if (runtimeChanged) {
      // Status reports rewrite runtime.json often; the items it declares rarely change.
      let items: PluginItem[] | undefined
      try {
        items = withCompaction(parseItems(JSON.parse((await readFile($, `${directory}/runtime.json`)) ?? "")),
          (await $.env.get("MV2_COMPACTION")) === "1")
      } catch { /* mid-write: the next change reads it */ }
      if (items !== undefined && JSON.stringify(items) !== JSON.stringify(declared)) {
        declared = items
        itemsChanged = true
      }
    }
    if (configChanged) configText = await readFile($, configFile)
    if (configChanged || itemsChanged) {
      const { config, problem } = parseConfig(configText, declared)
      statusConfig = config
      if (problem && configChanged) $.ui.log(`multiverse: ${problem}; using the defaults`)
    }
    // Each plugin item that is on: its file, read when it changed. One that is off is not looked at.
    for (const item of declared) {
      const path = `${directory}/status-items/${item.name}.txt`
      if (!statusConfig.on[item.name]) {
        delete itemTexts[item.name]
        watched.delete(path)
      } else if (await changed($, path)) {
        itemTexts[item.name] = (await readFile($, path)) ?? ""
      }
    }
    for (const name of Object.keys(itemTexts)) if (!declared.some((item) => item.name === name)) delete itemTexts[name]
    statusFacts.plugins = itemTexts
    if (propertiesChanged) {
      try {
        const data: unknown = JSON.parse((await readFile($, properties)) ?? "")
        statusFacts.agent = field(data, "name")
        statusFacts.phase = field(data, "phase")
        statusFacts.summary = field(data, "summary")
      } catch { /* mid-write: the next change reads it */ }
    }
    if (statusFacts.universe === undefined) {
      if (!isSubverse(universe)) {
        statusFacts.universe = ""  // a root universe is left out of the agent item
      } else {
        try {
          statusFacts.universe = field(JSON.parse((await readFile($, `${universe}/universe.json`)) ?? ""), "name") ?? ""
        } catch { /* the name shows once universe.json reads */ }
      }
    }
    if ((inboxChanged || cursorChanged) && cli) {
      try {
        const result = await $.process.run([cli, "inbox", "--peek", "--json"], { timeoutMs: 10000 })
        const data: unknown = result.exitCode === 0 ? JSON.parse(result.stdout) : undefined
        const messages = typeof data === "object" && data !== null ? (data as { messages?: unknown }).messages : undefined
        if (Array.isArray(messages)) statusFacts.inbox = Math.min(messages.length, INBOX_LIMIT)
      } catch { /* keep the last count */ }
    }
    setBase(render(statusConfig, statusFacts))
    const line = currentLine()
    if (line !== shown) {
      shown = line
      $.ui.status(line)
    }
  } finally {
    statusRunning = false
  }
}

let prompts = 0
let reported: string | undefined
let registered = false
let reporting = false

function status(): string {
  return waiting.size > 0 ? "waiting" : turnRunning ? "working" : "idle"
}

// Report the current status once it changes, one report at a time, so a
// burst of changes ends with the latest state. The first report also tells
// Multiverse this launch's mod is loaded.
async function report($: EngineInterface) {
  if (reporting) return
  reporting = true
  try {
    // Not an mv2 command: agents report their phase, never their status.
    const python = await $.env.get("MV2_PYTHON")
    if (!python) return
    while (!registered || reported !== status()) {
      const value = status()
      const argv = [python, "-m", "multiverse.tools.runtime_update", "--status", value]
      if (!registered) argv.push("--bridge", "claude-mod-v1")
      const result = await $.process.run(argv, { timeoutMs: 10000 })
      if (result.exitCode !== 0) {
        $.ui.log(`multiverse: status report failed: ${result.stderr.trim()}`)
        return
      }
      reported = value
      registered = true
    }
  } finally {
    reporting = false
  }
}

// A call has finished or run: nothing of it waits on the person any more. A
// prompt that matched no call ends with the next finished call of its tool.
function settle($: EngineInterface, id: string | undefined, tool?: string) {
  if (id === undefined) return
  asked.delete(id)
  const removed = [waiting.delete(`question:${id}`), waiting.delete(`permission:${id}`)]
  const unmatched = [...waiting].find(([key, name]) => key.startsWith("permission:unmatched-") && name === tool)
  if (unmatched !== undefined) removed.push(waiting.delete(unmatched[0]))
  if (removed.some(Boolean)) void report($)
}

// Steers rewritten in session.receive and not yet stored, by text: their IDs.
const queued = new Map<string, string | undefined>()

// Texts submitted as the person's words and not yet stored, by text: their IDs.
const submitted = new Map<string, string | undefined>()

// Submit as the person's own words: a turn of its own once the session is idle.
function submit($: EngineInterface, message: Message) {
  submitted.set(message.text, message.id)
  // A hook must not wait for its own submit, so leave the hook first.
  $.clock.after(0, () => {
    $.prompt.submit({ text: message.text, asUser: true }).catch(
      (error) => $.ui.log(`multiverse: message not submitted: ${String(error)}`))
  })
}

async function receipt($: EngineInterface, id: string | undefined, route: string) {
  const directory = await $.env.get("MV2_AGENT_DIR")
  if (id === undefined || !directory) return
  const at = await $.clock.now()
  await $.fs.write(`${directory}/claude-receipts/${id}.json`, JSON.stringify({ id, route, at }) + "\n")
}

// Answer a side question over the session's own transcript; nothing is added
// to the conversation.
async function answer($: EngineInterface, message: Message) {
  const directory = await $.env.get("MV2_AGENT_DIR")
  if (message.id === undefined || !directory) return
  const result = await $.model.fork({ prompt: message.text })
  const at = await $.clock.now()
  const record = result.isAnswered
    ? { id: message.id, answered: true, text: result.text, usage: result.usage, at }
    : { id: message.id, answered: false, reason: result.reason, usage: "usage" in result ? result.usage : undefined, at }
  await $.fs.write(`${directory}/claude-asks/${message.id}.json`, JSON.stringify(record) + "\n")
}

function text(content: readonly { type: string; text?: unknown }[]): string {
  return content.map((block) => block.type === "text" ? String(block.text) : "").join("\n")
}

export const register: Register = (on) => {
  registerUsage(on)
  registerCompaction(on)

  registerMultiverse(on)

  on("session.start", async ($, e, next) => {
    const result = await next(e)
    void report($)
    $.command.register({ name: NAME, description: DESCRIPTION, argumentHint: HINT, immediate: true })
      .catch((error: unknown) => $.ui.log(`multiverse: could not register /${NAME}: ${String(error)}`))
    // A new session (or a reload) knows nothing of what is on screen or on disk.
    statusTimer?.cancel()
    watched.clear()
    shown = undefined
    statusConfig = defaultConfig()
    Object.assign(statusFacts, { agent: undefined, universe: undefined, phase: undefined, summary: undefined, inbox: 0 })
    configText = undefined
    declared = []
    for (const name of Object.keys(itemTexts)) delete itemTexts[name]
    statusTimer = $.clock.every(REFRESH_MS, () => { void refreshStatus($) })
    void refreshStatus($)
    return result
  })

  on("turn.start", async ($, e, next) => {
    turnRunning = true
    void report($)
    void refreshStatus($)
    return next(e)
  })

  on("turn.complete", async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      turnRunning = false
      // An ended turn has closed its prompts and questions.
      waiting.clear()
      asked.clear()
      void report($)
      void refreshStatus($)
    }
    return result
  })

  // A question tool waits on the person for as long as its call runs; any
  // call that finishes has left its permission prompt.
  on("tool.call", async ($, e, next) => {
    const id = e.tool_use_id
    if (id !== undefined && QUESTIONS.has(e.tool)) {
      waiting.set(`question:${id}`, e.tool)
      void report($)
    }
    try {
      return await next(e)
    } finally {
      settle($, id, e.tool)
    }
  })

  // `ask` may open a permission prompt for this call, or the mode settles it.
  on("tool.check", async ($, e, next) => {
    const verdict = await next(e)
    if (verdict.decision === "ask" && e.tool_use_id !== undefined) asked.set(e.tool_use_id, e.tool)
    return verdict
  })

  // The prompt is open: it belongs to the oldest asked call of that tool.
  on("classic.PermissionRequest", async ($, e, next) => {
    const id = [...asked].find(([, tool]) => tool === e.tool_name)?.[0]
    if (id !== undefined) asked.delete(id)
    prompts += 1
    waiting.set(`permission:${id ?? `unmatched-${prompts}`}`, e.tool_name)
    void report($)
    return next(e)
  })

  on("classic.PermissionDenied", async ($, e, next) => {
    settle($, e.tool_use_id)
    return next(e)
  })

  // An approved Bash call draws its progress row once it runs.
  on("ui.render", { component: "ToolProgress" }, async ($, e, next) => {
    if (waiting.has(`permission:${e.props.tool_use_id}`)) settle($, e.props.tool_use_id)
    return next(e)
  })

  on("classic.Elicitation", async ($, e, next) => {
    waiting.set(`elicitation:${e.elicitation_id ?? e.mcp_server_name}`, e.mcp_server_name)
    void report($)
    return next(e)
  })

  on("classic.ElicitationResult", async ($, e, next) => {
    if (waiting.delete(`elicitation:${e.elicitation_id ?? e.mcp_server_name}`)) void report($)
    return next(e)
  })

  on("session.receive", { origin: { kind: "peer" } }, async ($, e, next) => {
    const message = tagged(e.text)
    if (message === undefined) return next(e)
    if (message.mode === "ask") {
      // The fork may take a while; leave the hook first.
      $.clock.after(0, () => {
        answer($, message).catch((error) => $.ui.log(`multiverse: question not answered: ${String(error)}`))
      })
      return { consumed: "Multiverse question answered by the multiverse plugin" }
    }
    if (message.mode === "follow-up" || !turnRunning) {
      submit($, message)
      return { consumed: "Multiverse message submitted by the multiverse plugin" }
    }
    // Queued at `next` without its tags: it merges at the next tool result.
    queued.set(message.text, message.id)
    return next({ ...e, text: message.text })
  })

  on("session.append", async ($, e, next) => {
    const stored = text(e.message.content)
    if (e.origin.kind === "peer" && e.message.name === "queued_command") {
      // Claude's recorded rendering: its peer wrapper around our text.
      const known = [...queued].find(([queuedText]) => stored.includes(queuedText))
      const message = known ? { text: known[0], id: known[1] } : tagged(stored)
      if (message === undefined) return next(e)
      queued.delete(message.text)
      const first = e.message.content.findIndex((block) => block.type === "text")
      const reminder = stored.trimStart().startsWith("<system-reminder>")
      const content = e.message.content.flatMap((block, index) => block.type !== "text" ? [block]
        : index === first ? [{ ...block, text: reminder ? `<system-reminder>\n${message.text}\n</system-reminder>` : message.text }]
        : [])
      const result = await next({ ...e, message: { ...e.message, content } })
      await receipt($, message.id, "mid-turn")
      return result
    }
    if (e.origin.kind === "plugin" && "asUser" in e.origin && e.origin.asUser && submitted.has(stored)) {
      const id = submitted.get(stored)
      submitted.delete(stored)
      const result = await next(e)
      await receipt($, id, "prompt")
      return result
    }
    return next(e)
  })

  // Older history stored the tags inside Claude's wrapper.
  on("prompt.attachment", { type: "queued_command" }, async ($, e, next) => {
    const message = tagged(e.text)
    return message === undefined ? next(e) : { text: message.text }
  })

  // A steer queued mid-turn that no tool result took would start its own turn.
  on("prompt.submit", { origin: { kind: "peer" } }, async ($, e, next) => {
    if (e.turnId !== undefined) return next(e)
    const message: Message | undefined = queued.has(e.text)
      ? { text: e.text, mode: "steer", id: queued.get(e.text) } : tagged(e.text)
    if (message === undefined) return next(e)
    queued.delete(message.text)
    submit($, message)
    return { drop: "Multiverse message submitted by the multiverse plugin" }
  })
}
