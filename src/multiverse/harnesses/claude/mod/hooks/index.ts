import type { EngineInterface, Register } from 'claude-code'
import { agePhrase, withAge } from './age'
import { DESCRIPTION, HINT, NAME, registerMultiverse } from './multiverse'
import { INBOX_LIMIT, currentLine, defaultConfig, isSubverse, parseConfig, parseItems, render, setBase } from './statusline'
import type { Config, Facts, PluginItem } from './statusline'
import { registerUsage } from './usage'

// Multiverse sends each message over this session's peer inbox at `next`
// priority, its text between `<<MV2 steer id=UUID>>` (or `follow-up`) and
// `<</MV2>>`. The model reads the text alone, without the tags or Claude's peer
// wrapper:
// - A steer arriving mid-turn is queued as its clean text, which is what Claude's
//   queue line, the stored row and the screen show; it merges at the next tool
//   result as a queued command, and session.append sends it to the model without
//   Claude's peer wrapper. Claude stores queued text as given, so a tag left in it
//   would show for good; the mod tells its stored row by that text instead.
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
// `id=` names one delivery attempt (core gives a retry an ID of its own), so a receipt is about that attempt.
// `sent=` is when the sender sent it (core's record time); older tags have none.
const TAG = /<<MV2(?: (steer|follow-up|ask))?(?: id=([0-9a-fA-F-]{36}))?(?: sent=([0-9][0-9T:.+\-Z]*))?>>([\s\S]*)<<\/MV2>>/

type Message = { text: string; mode: "steer" | "follow-up" | "ask"; id?: string; sent?: string }

// The tagged message in a text; undefined when the text carries none.
function tagged(text: string): Message | undefined {
  const match = TAG.exec(text)
  if (match === null) return undefined
  const mode = match[1] === "follow-up" || match[1] === "ask" ? match[1] : "steer"
  return { text: match[4] ?? "", mode, id: match[2], sent: match[3] }
}

// An older mod's tag around a whole message, as Claude stored it: nothing before the tag but Claude's own peer header
// (inside its system reminder, or no wrapper at all for a prompt row), nothing after it but Claude's trailer. A tag
// anywhere else is quoted in the message's own text: an mv2 message starts with its "Message from" line, so a tag it
// quotes always has text before it.
const HEAD = /^\s*(?:<system-reminder>\s*)?(?:Another Claude session sent a message[^\n]*:\s*)?$/
const TAIL = /^\s*(?:This came from another Claude session[\s\S]*|<\/system-reminder>\s*)?$/

function enveloped(text: string): Message | undefined {
  const match = TAG.exec(text)
  if (match === null) return undefined
  if (!HEAD.test(text.slice(0, match.index)) || !TAIL.test(text.slice(match.index + match[0].length))) return undefined
  return tagged(match[0])
}

// Main-loop turn state. A subagent's or teammate's turn.start and turn.complete
// carry its agentId (turn.start since Claude Code 2.1.299's types say so); only
// the main loop's turns count, or a background subagent would leave "working".
let turnRunning = false

// Whether this module has seen a main-loop turn boundary since it loaded. A reload can come mid-turn, and Claude
// doesn't say whether a turn is running, so until a turn.start or turn.complete the state is unknown, not idle:
// a steer then takes the queued way (right either way), and no message is timed out before an observed idle moment.
let turnKnown = false
const idle = () => turnKnown && !turnRunning

// After an interruption Claude refuses a plugin's prompt ("the session was stopped and nobody has sent input
// since") until the person sends input, but not a peer prompt. Until the person types, a message stays in
// Claude's queue and runs as the peer prompt, its wrapper stripped where it is stored. A reload forgets this;
// Claude's next refusal sets it again.
let interrupted = false

// The native session this process is on. A /clear moves the same process to a new one, which mv2 is told so
// it rebinds the agent: messages, usage and resume then follow the new session.
let sessionId: string | undefined

// What waits on the person, by key (`question:<id>`, `permission:<id>`,
// `elicitation:<id>`), with the tool it waits for. Any entry makes the session
// `waiting`.
const waiting = new Map<string, string>()

// Tool calls the permission check answered `ask`, by tool_use_id, until a
// prompt opens for one of them (the mode may settle the ask instead).
const asked = new Map<string, string>()

// Tools whose call is itself a question to the person.
const QUESTIONS = new Set(["AskUserQuestion", "ExitPlanMode"])

// The status line under the prompt (statusline.ts holds the logic and the slot).
// A timer looks at its files' size and time,
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
        items = parseItems(JSON.parse((await readFile($, `${directory}/runtime.json`)) ?? ""))
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
      // v2 reads a tag's send time; mv2 sends one only to a mod that registered it.
      if (!registered) argv.push("--bridge", "claude-mod-v2")
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

// Tell mv2 this process moved to another session (a /clear), once Claude's own process file names it. Core also
// reads that file, so reporting only after it has changed means no later read of it can still show the old session
// (its writes only move forward). Claude starts that write before the SessionStart hooks but does not wait for it.
// Once: core refuses what it cannot prove (an outdated launch, a session that is not this agent's); a refusal, or a
// file that never shows the new session, is logged and never retried.
const PROCESS_FILE_WAIT_MS = 5000
const PROCESS_FILE_POLL_MS = 100

async function processFileSession($: EngineInterface) {
  try {
    const result = await $.process.run(["sh", "-c", 'cat "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/sessions/$PPID.json"'],
      { timeoutMs: 2000 })
    const record = JSON.parse(result.stdout) as { sessionId?: unknown }
    return typeof record.sessionId === "string" ? record.sessionId : undefined
  } catch {
    return undefined
  }
}

async function moved($: EngineInterface, previous: string, session: string) {
  const python = await $.env.get("MV2_PYTHON")
  if (!python) return
  let published = false
  for (let waited = 0; waited <= PROCESS_FILE_WAIT_MS; waited += PROCESS_FILE_POLL_MS) {
    if (await processFileSession($) === session) { published = true; break }
    await $.clock.sleep(PROCESS_FILE_POLL_MS)
  }
  if (!published) {
    $.ui.log("multiverse: the move to a new session was not recorded: Claude's process file never named it")
    return
  }
  try {
    const result = await $.process.run([python, "-m", "multiverse.tools.runtime_update", "--native-session-id", session,
      "--previous-session-id", previous, "--session-reason", "clear"], { timeoutMs: 10000 })
    if (result.exitCode !== 0) $.ui.log(`multiverse: the move to a new session was not recorded: ${result.stderr.trim()}`)
  } catch (error) {
    $.ui.log(`multiverse: the move to a new session was not recorded: ${String(error)}`)
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

// A message Claude has but hasn't stored yet: its ID (one delivery attempt), for its not-delivered receipt. A
// refused one stays waiting, so a row that turns up after all still marks it delivered. `refused` is "unsure" after
// a timeout and "definite" after Claude's own refusal.
// Every message goes to Claude as its clean text, which is how its stored row is told: `route` says how it went,
// queued (a steer left in Claude's queue, stored as a peer row) or submitted (as the person's own words, stored as
// the plugin's prompt row), and only rows of that kind are matched against it.
type Route = "queued" | "submitted"
type Waiting = { id?: string; text?: string; sent?: string; mode?: Message["mode"]; route?: Route;
  refused?: "unsure" | "definite"; written?: Promise<void> }

// How long an idle session may leave a taken message unstored before it counts as not delivered. Idle means an
// observed one (idle()): a session just loaded, or still starting, has shown no turn boundary yet.
const STORE_GRACE_MS = 5000

// Messages Claude has but hasn't stored yet, oldest first.
const waitingRows: Waiting[] = []

function hold(message: Message, route: Route): Waiting {
  const entry: Waiting = { id: message.id, text: message.text, sent: message.sent, mode: message.mode, route }
  waitingRows.push(entry)
  return entry
}

// Whose a stored row (or the peer prompt Claude runs) is: a message the mod sent this way as clean text, or one an
// older mod sent inside its tag. When both fit, containment decides: a tag inside the waiting text is that message's
// own words (a message about this protocol), so the waiting message wins; a tag around the waiting text is the outer
// envelope of an older message, which names its message exactly, so the tag wins. `enveloped` says the tag won, so
// its envelope is the row's to strip.
function rowMessage(text: string, route: Route): { message: Message; enveloped: boolean } | undefined {
  const waiting = waitingIn(text, route)
  const older = enveloped(text)  // only a tag around the whole message; a quoted one never names a message
  if (waiting !== undefined && (older === undefined || waiting.text.includes(TAG.exec(text)?.[0] ?? "")))
    return { message: waiting, enveloped: false }
  return older === undefined ? undefined : { message: older, enveloped: true }
}

// The message, sent by `route`, that a stored row (or the peer prompt Claude runs) holds: of those still waiting whose
// whole text it contains, the longest (so a short text inside a longer one doesn't take its row), the oldest among
// equals, which is Claude's own order. Claude refusing one for good means it's never stored.
function waitingIn(text: string, route: Route): Message | undefined {
  let found: Waiting | undefined
  for (const entry of waitingRows) {
    if (entry.route !== route || entry.text === undefined || entry.refused === "definite" || !text.includes(entry.text)) continue
    if (found === undefined || entry.text.length > (found.text ?? "").length) found = entry
  }
  return found === undefined || found.text === undefined ? undefined
    : { text: found.text, mode: found.mode ?? "steer", id: found.id, sent: found.sent }
}

// The opening an older mod wrote around a prompt it submitted, and only that, at the very start of a text. Claude
// shows a submitted prompt's text in its queue line until it runs, so the mod now submits it clean.
const OPENING = /^<<MV2 id=[0-9a-fA-F-]{36}(?: sent=[0-9][0-9T:.+\-Z]*)?>>/

function forget(entry: Waiting) {
  const index = waitingRows.indexOf(entry)
  if (index >= 0) waitingRows.splice(index, 1)
}

function pending(): Waiting[] {
  return waitingRows.filter((entry) => !entry.refused)
}

// The waiting message a stored row's tag names; a row carries none, or names nothing waiting, after a reload.
function waitingFor(id: string | undefined): Waiting | undefined {
  return id === undefined ? undefined : waitingRows.find((entry) => entry.id === id)
}

// Not delivered, as far as the mod can tell: Claude refused it or never stored it. A receipt names one attempt.
// It has a file of its own beside the delivery receipt, and a delivery wins wherever both exist, so a refusal
// whose write finishes after the row was stored, even across a reload, never hides the delivery.
// Only Claude's own refusal is definite, and so retryable. A row still missing after a timeout may yet come:
// Claude holds a taken message while a local command such as /compact, or a dialog, holds its turn, and stores
// it afterwards, so a retry then would run it twice. Core reports that one as not confirmed. Claude refusing the
// held submit afterwards is the proof, so its refusal replaces an unsure one; an unsure one never replaces it.
async function refuse($: EngineInterface, entry: Waiting, reason: string, definite = true) {
  if (entry.id === undefined || entry.refused === "definite" || (!definite && entry.refused)) return
  entry.refused = definite ? "definite" : "unsure"
  const id = entry.id
  const write = async () => {
    const directory = await $.env.get("MV2_AGENT_DIR")
    if (!directory) return
    const at = await $.clock.now()
    await $.fs.write(`${directory}/claude-receipts/${id}.not-delivered.json`,
      JSON.stringify({ id, delivered: false, definite, reason, at }) + "\n")
  }
  // One write at a time per attempt, in the order decided here: writes finish in any order, and an unsure one
  // still running when Claude refuses would otherwise land last, leaving a refused message unconfirmed for good.
  const previous = entry.written ?? Promise.resolve()
  entry.written = previous.then(write, write)
  await entry.written
}

// A taken message must be stored within a few seconds of the session being idle.
function expireLater($: EngineInterface, entries: Waiting[]) {
  if (entries.length === 0) return
  $.clock.after(STORE_GRACE_MS, () => {
    if (!idle()) return  // the end of the turn running decides
    for (const entry of entries) {
      if (pending().includes(entry)) void refuse($, entry, `Claude didn't store it within ${STORE_GRACE_MS / 1000} seconds while idle`, false)
    }
  })
}

// Messages already taken when a main-loop turn started; one still unstored at its end was not delivered.
let takenBeforeTurn: Waiting[] = []

// The text the model reads: with its age when it reaches the model more than ten minutes after it was sent.
async function aged($: EngineInterface, text: string, sent: string | undefined): Promise<string> {
  return sent === undefined ? text : withAge(text, agePhrase(sent, await $.clock.now()))
}

// Submit as the person's own words: a turn of its own once the session is idle.
function submit($: EngineInterface, message: Message) {
  const entry = hold(message, "submitted")
  if (idle()) expireLater($, [entry])
  // A hook must not wait for its own submit, so leave the hook first.
  $.clock.after(0, () => {
    $.prompt.submit({ text: message.text, asUser: true }).then((result) => {
      if (result === undefined || result === null || typeof result !== "object" || !("drop" in result)) return
      // Refused, so it never enters the conversation: forget it, and send later messages the peer way.
      forget(entry)
      void refuse($, entry, String(result.drop))
      interrupted = true
      $.ui.log(`multiverse: message not submitted: ${String(result.drop)}`)
    }, (error) => $.ui.log(`multiverse: message not submitted: ${String(error)}`))
  })
}

async function receipt($: EngineInterface, entry: { id?: string }, route: string) {
  const directory = await $.env.get("MV2_AGENT_DIR")
  if (entry.id === undefined || !directory) return
  const at = await $.clock.now()
  await $.fs.write(`${directory}/claude-receipts/${entry.id}.json`, JSON.stringify({ id: entry.id, route, at }) + "\n")
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

  registerMultiverse(on)

  on("session.start", async ($, e, next) => {
    const result = await next(e)
    void report($)
    void $.session.id().then((id) => { sessionId = id }, () => {})
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

  on("classic.SessionStart", { source: "clear" }, async ($, e, next) => {
    const result = await next(e)
    const previous = sessionId
    sessionId = e.session_id
    if (previous && e.session_id && previous !== e.session_id) void moved($, previous, e.session_id)
    return result
  })

  on("turn.start", async ($, e, next) => {
    if (e.agentId === undefined) {
      turnRunning = true
      turnKnown = true
      takenBeforeTurn = pending()
      void report($)
      void refreshStatus($)
    }
    return next(e)
  })

  on("turn.complete", async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      turnRunning = false
      turnKnown = true
      if (e.isAborted) interrupted = true
      const still = pending()
      for (const entry of takenBeforeTurn) {
        if (still.includes(entry)) void refuse($, entry, "Claude ended a turn without storing it", false)
      }
      takenBeforeTurn = []
      expireLater($, pending())
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
    if (!interrupted && (message.mode === "follow-up" || idle())) {
      submit($, message)
      return { consumed: "Multiverse message submitted by the multiverse plugin" }
    }
    // Queued at `next` as its clean text: it merges at the next tool result, or after an interruption runs as the
    // peer prompt of its own.
    const entry = hold(message, "queued")
    if (idle()) expireLater($, [entry])
    return next({ ...e, text: message.text })
  })

  on("session.append", async ($, e, next) => {
    const stored = text(e.message.content)
    // Claude's recorded rendering: its peer wrapper around our text, mid-turn or (after an interruption) as a turn
    // of its own.
    // A row is ours by holding the text of a message the mod sent that way, or by an older mod's tag around it
    // (rowMessage says which, when both fit). A queued steer is a peer row and a submitted prompt the multiverse
    // plugin's, so neither kind takes the other's row.
    const submitted = e.origin.kind === "plugin" && "asUser" in e.origin && e.origin.asUser &&
      "name" in e.origin && e.origin.name === "multiverse"
    const found = e.origin.kind === "peer" ? rowMessage(stored, "queued") : submitted ? rowMessage(stored, "submitted") : undefined
    const message = found?.message
    if (e.origin.kind === "peer" && message !== undefined) {
      const known = waitingFor(message.id)
      if (known !== undefined) forget(known)
      const shown = await aged($, message.text, message.sent)
      const first = e.message.content.findIndex((block) => block.type === "text")
      const reminder = stored.trimStart().startsWith("<system-reminder>")
      const content = e.message.content.flatMap((block, index) => block.type !== "text" ? [block]
        : index === first ? [{ ...block, text: reminder ? `<system-reminder>\n${shown}\n</system-reminder>` : shown }]
        : [])
      const result = await next({ ...e, message: { ...e.message, content } })
      await receipt($, message, e.message.name === "queued_command" ? "mid-turn" : "peer-prompt")
      return result
    }
    if (submitted && found !== undefined && message !== undefined) {
      const known = waitingFor(message.id)
      if (known !== undefined) forget(known)
      const { sent } = message
      // An older mod's envelope comes off, once: its opening at the start of the first text block and its closing at
      // the end of the last, so a tag the message itself mentions stays; a clean prompt has none to strip. The age
      // goes on the sender line, in the first text block; every other block stays exactly as it was.
      const texts = e.message.content.flatMap((part, index) => part.type === "text" ? [index] : [])
      const first = texts[0] ?? -1
      const last = texts[texts.length - 1] ?? -1
      const strip = (part: string, index: number) => {
        if (!found.enveloped) return part
        const opened = index === first ? part.replace(OPENING, "") : part
        return index === last && opened.endsWith("<</MV2>>") ? opened.slice(0, -"<</MV2>>".length) : opened
      }
      const block = e.message.content[first]
      const before = block !== undefined && block.type === "text" ? strip(String(block.text), first) : undefined
      const shown = before === undefined ? undefined : await aged($, before, sent)
      const content = e.message.content.map((part, index) => part.type !== "text" ? part
        : index === first && shown !== undefined ? { ...part, text: shown } : { ...part, text: strip(String(part.text), index) })
      const result = await next({ ...e, message: { ...e.message, content } })
      await receipt($, message, "prompt")
      return result
    }
    return next(e)
  })

  // Older history stored the tags inside Claude's wrapper.
  on("prompt.attachment", { type: "queued_command" }, async ($, e, next) => {
    const message = enveloped(e.text)  // a clean message quoting a tag reaches the model whole
    return message === undefined ? next(e) : { text: message.text }
  })

  // The person's own input at the terminal lifts Claude's refusal of a plugin's prompt. (Other input may not;
  // staying on the peer route costs nothing, and a refusal would set it again.)
  on("prompt.submit", { origin: { kind: "composer" } }, async ($, e, next) => {
    interrupted = false
    return next(e)
  })

  // A steer queued mid-turn that no tool result took would start its own turn.
  on("prompt.submit", { origin: { kind: "peer" } }, async ($, e, next) => {
    if (e.turnId !== undefined) return next(e)
    const message = rowMessage(e.text, "queued")?.message  // as in session.append
    if (message === undefined) return next(e)
    // After an interruption Claude would refuse the submitted copy; the peer prompt itself runs.
    if (interrupted) return next(e)
    const known = waitingFor(message.id)
    if (known !== undefined) forget(known)
    submit($, { ...message, mode: "steer" })
    return { drop: "Multiverse message submitted by the multiverse plugin" }
  })
}
