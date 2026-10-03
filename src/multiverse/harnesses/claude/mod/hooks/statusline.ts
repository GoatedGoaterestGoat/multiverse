// The Multiverse status line, as pure logic: no `$`, so any file may import it
// (a `$` cannot be passed across files) and the tests can call it directly.
//
// Claude allows one status line per plugin. Two writers share it: the normal
// line below, and the progress line of a running /compact. Each writer sets its
// own text here and shows `currentLine()`, so progress wins while it lasts and
// the normal line returns when it is cleared.
//
// The line's items come from `statusline.json` at the universe root (shipped in
// every template, managed by `mv2 refresh`):
//   {"schema": 1, "items": {"agent": false, "phase": true, "summary": false,
//                           "inbox": true, "compaction": true}}
// The order of the keys is the order on screen. A key that is missing keeps
// its default, an unknown key is ignored, and an unreadable file means the
// defaults.

export const ITEMS = ["agent", "phase", "summary", "inbox", "compaction"] as const
export type Item = (typeof ITEMS)[number]

export const DEFAULTS: Readonly<Record<Item, boolean>> =
  { agent: false, phase: true, summary: false, inbox: true, compaction: true }

export type Config = { order: readonly Item[]; on: Readonly<Record<Item, boolean>> }

// What the line shows. `inbox` counts unread inbox-only messages.
export type Facts = {
  agent?: string
  universe?: string
  phase?: string
  summary?: string
  inbox: number
  compaction?: string
}

const SEPARATOR = " · "
const SUMMARY_LENGTH = 60
export const INBOX_LIMIT = 50

export function defaultConfig(): Config {
  return { order: [...ITEMS], on: { ...DEFAULTS } }
}

// `text` is the file's content, or undefined when there is no file: the
// defaults, with nothing to report. Anything unusable also gives the defaults,
// with the reason to log.
export function parseConfig(text: string | undefined): { config: Config; problem?: string } {
  if (text === undefined) return { config: defaultConfig() }
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return { config: defaultConfig(), problem: "statusline.json is not valid JSON" }
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    return { config: defaultConfig(), problem: "statusline.json must be an object" }
  }
  const file = data as Record<string, unknown>
  if (file.schema !== undefined && file.schema !== 1) {
    return { config: defaultConfig(), problem: `statusline.json has unsupported schema ${JSON.stringify(file.schema)}` }
  }
  const items = file.items
  if (items === undefined) return { config: defaultConfig() }
  if (typeof items !== "object" || items === null || Array.isArray(items)) {
    return { config: defaultConfig(), problem: "statusline.json items must be an object" }
  }
  const on = { ...DEFAULTS }
  const order: Item[] = []
  const wrong: string[] = []
  for (const [key, value] of Object.entries(items)) {
    const item = ITEMS.find((name) => name === key)
    if (item === undefined) continue
    if (typeof value !== "boolean") {
      wrong.push(key)
      continue
    }
    on[item] = value
    order.push(item)
  }
  for (const item of ITEMS) if (!order.includes(item)) order.push(item)
  return { config: { order, on }, problem: wrong.length ? `statusline.json items ${wrong.join(", ")} must be true or false` : undefined }
}

// Text from another agent (a reported summary) must not carry control
// characters, such as escape sequences, onto the screen.
export function clean(text: string, length = Infinity): string {
  const plain = text.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, " ").replace(/\s+/g, " ").trim()
  return plain.length > length ? plain.slice(0, length - 1).trimEnd() + "…" : plain
}

export function render(config: Config, facts: Facts): string | undefined {
  const parts: string[] = []
  let joined = false
  for (const item of config.order) {
    if (!config.on[item]) continue
    if (item === "agent") {
      const name = clean(facts.agent ?? "")
      const universe = clean(facts.universe ?? "")
      if (name) parts.push(universe ? `${name} (${universe})` : name)
    } else if (item === "phase" || item === "summary") {
      // Phase and summary read as one segment, where the first of them stands.
      if (joined) continue
      joined = true
      const phase = config.on.phase ? clean(facts.phase ?? "") : ""
      const summary = config.on.summary ? clean(facts.summary ?? "", SUMMARY_LENGTH) : ""
      const text = phase && summary ? `${phase}: ${summary}` : phase || summary
      if (text) parts.push(text)
    } else if (item === "inbox") {
      if (facts.inbox > 0) parts.push(`inbox ${facts.inbox >= INBOX_LIMIT ? `${INBOX_LIMIT}+` : facts.inbox}`)
    } else if (item === "compaction") {
      const text = clean(facts.compaction ?? "")
      if (text) parts.push(text)
    }
  }
  return parts.length ? parts.join(SEPARATOR) : undefined
}

// The two lines sharing the slot. Nothing here touches the screen: a writer
// sets its own, then shows `currentLine()`.
let base: string | undefined
let progress: string | undefined

export function setBase(line: string | undefined) {
  base = line
}

export function setProgress(line: string | undefined) {
  progress = line
}

export function currentLine(): string | undefined {
  return progress ?? base
}
