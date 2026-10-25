// The Multiverse status line, as pure logic: no `$`, so any file may import it
// (a `$` cannot be passed across files) and the tests can call it directly.
//
// Claude allows one status line per plugin; this module's line is the normal
// line below (a plugin that shows progress does so in its own).
//
// The line's items come from `statusline.json` at the universe root (shipped in
// every template, managed by `mv2 refresh`):
//   {"schema": 1, "items": {"agent": false, "phase": true, "summary": false,
//                           "inbox": true}}
// The order of the keys is the order on screen. A key that is missing keeps
// its default, an unknown key is ignored, and an unreadable file means the
// defaults.
//
// Plugins add items of their own. A launch declares them in runtime.json
// (`plugins.status_items`: name, plugin and whether each line shows it by
// default), and the plugin writes the text to
// `state/agents/<id>/status-items/<name>.txt`. A declared item is a key like the
// core ones: `items` names it to place it or turn it on or off; one it does not
// name shows by its `claude` default, after everything else.

export const ITEMS = ["agent", "phase", "summary", "inbox"] as const
export type Item = (typeof ITEMS)[number]

export const DEFAULTS: Readonly<Record<Item, boolean>> =
  { agent: false, phase: true, summary: false, inbox: true }

export type Config = { order: readonly string[]; on: Readonly<Record<string, boolean>> }

// One status-line item a plugin declared for this launch.
export type PluginItem = { name: string; plugin: string; claude: boolean; tmux: boolean }

// The manifest's rule for item names, and the names core keeps for itself (on either line).
const ITEM_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const ITEM_NAME_LENGTH = 40
const CORE_NAMES = new Set(["agent", "harness", "status", "phase", "summary", "inbox", "context"])

// What the line shows. `inbox` counts unread inbox-only messages.
export type Facts = {
  agent?: string
  universe?: string
  phase?: string
  summary?: string
  inbox: number
  // Each plugin item's text, by item name, as its file holds it.
  plugins?: Readonly<Record<string, string>>
}

const SEPARATOR = " · "
const SUMMARY_LENGTH = 60
export const INBOX_LIMIT = 50

// A subverse is a universe at `.../subverses/<name>`; a root universe (the project's own `.multiverse`) is not.
export function isSubverse(universePath: string): boolean {
  const parts = universePath.split("/").filter((part) => part !== "")
  return parts.length >= 2 && parts[parts.length - 2] === "subverses"
}

export function defaultConfig(declared: readonly PluginItem[] = []): Config {
  const on: Record<string, boolean> = { ...DEFAULTS }
  for (const item of declared) on[item.name] = item.claude
  return { order: [...ITEMS, ...declared.map((item) => item.name)], on }
}

// The plugin items a launch declared: runtime.json's `plugins.status_items`. A
// malformed record declares none; a malformed entry, a name that breaks the rule
// or is a core one, and a repeated name are left out.
export function parseItems(runtime: unknown): PluginItem[] {
  const plugins = typeof runtime === "object" && runtime !== null ? (runtime as Record<string, unknown>).plugins : undefined
  const listed = typeof plugins === "object" && plugins !== null ? (plugins as Record<string, unknown>).status_items : undefined
  if (!Array.isArray(listed)) return []
  const items: PluginItem[] = []
  for (const entry of listed) {
    if (typeof entry !== "object" || entry === null) continue
    const { name, plugin, claude, tmux } = entry as Record<string, unknown>
    if (typeof name !== "string" || name.length > ITEM_NAME_LENGTH || !ITEM_NAME.test(name) || CORE_NAMES.has(name)) continue
    if (typeof plugin !== "string" || typeof claude !== "boolean" || typeof tmux !== "boolean") continue
    if (items.some((item) => item.name === name)) continue
    items.push({ name, plugin, claude, tmux })
  }
  return items
}

// `text` is the file's content, or undefined when there is no file: the
// defaults, with nothing to report. Anything unusable also gives the defaults,
// with the reason to log.
export function parseConfig(text: string | undefined, declared: readonly PluginItem[] = []): { config: Config; problem?: string } {
  const fallback = () => defaultConfig(declared)
  if (text === undefined) return { config: fallback() }
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return { config: fallback(), problem: "statusline.json is not valid JSON" }
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    return { config: fallback(), problem: "statusline.json must be an object" }
  }
  const file = data as Record<string, unknown>
  if (file.schema !== undefined && file.schema !== 1) {
    return { config: fallback(), problem: `statusline.json has unsupported schema ${JSON.stringify(file.schema)}` }
  }
  const items = file.items
  if (items === undefined) return { config: fallback() }
  if (typeof items !== "object" || items === null || Array.isArray(items)) {
    return { config: fallback(), problem: "statusline.json items must be an object" }
  }
  const defaults = fallback()
  const on: Record<string, boolean> = { ...defaults.on }
  const everything = defaults.order
  const known = new Set(everything)
  const order: string[] = []
  const wrong: string[] = []
  for (const [key, value] of Object.entries(items)) {
    if (!known.has(key) || order.includes(key)) continue
    if (typeof value !== "boolean") {
      wrong.push(key)
      continue
    }
    on[key] = value
    order.push(key)
  }
  // What the file leaves out keeps its default: the core items, then the plugins' in declared order.
  for (const item of everything) if (!order.includes(item)) order.push(item)
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
      // The name, plus the universe only for an agent in a subverse (the caller leaves `universe` out otherwise).
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
    } else {
      // A plugin's item: the first line of its file, cleaned and shortened like a summary.
      const text = clean((facts.plugins?.[item] ?? "").split(/\r?\n/, 1)[0] ?? "", SUMMARY_LENGTH)
      if (text) parts.push(text)
    }
  }
  return parts.length ? parts.join(SEPARATOR) : undefined
}

// The line in the slot. Nothing here touches the screen: the writer sets it,
// then shows `currentLine()`.
let base: string | undefined

export function setBase(line: string | undefined) {
  base = line
}

export function currentLine(): string | undefined {
  return base
}
