import type { EngineInterface, Register } from 'claude-code'

// Typeahead for the multiverse mod's /multiverse command, as the person types in the prompt box:
//   /multiverse <word>          the subcommands and the catalog's commands, workflows and skills
//   /multiverse send <agent>    this universe's agents (a comma list completes its last name)
//   /multiverse <name> <args>   nothing: the arguments are free
//
// It is its own plugin because `prompt.autocomplete` is new in Claude Code 2.1.292 and an older
// Claude refuses a module that hooks it. The Claude adapter loads this plugin only for 2.1.292 or
// newer, so the multiverse mod itself keeps working everywhere.
//
// The catalog (`mv2 catalog --json`) and the agent list (`mv2 list --json`) are read in the background and kept a few seconds. Typing
// never waits on them: a hook answers from what is kept, and reading starts after it returns, first
// while `/mu...` is still being typed. A failed read leaves no rows.

// Kept equal to the multiverse mod's command and subcommands (a Python test compares them).
export const COMMAND = "multiverse"
const RESERVED = new Set(["list", "send", "inbox"])
// What `mv2 command` runs, by catalog kind, as a row names it.
const RUNNABLE: Record<string, string> = { commands: "command", workflows: "workflow", skills: "skill" }
const FRESH_MS = 5000
const LOAD_TIMEOUT_MS = 5000

export type Suggestion = { text: string; label?: string; description?: string }
export type CatalogEntry = { kind: string; name: string; description: string }
export type Known = { catalog: readonly Suggestion[]; agents: readonly Suggestion[] }

export const SUBCOMMANDS: readonly Suggestion[] = [
  { text: "list", description: "this universe's commands, workflows and skills" },
  { text: "send ", label: "send", description: "message an agent, as this agent" },
  { text: "inbox", description: "this agent's unread inbox-only messages (marks them read)" },
]

type Cataloged = { kind?: unknown; name?: unknown; description?: unknown }

// What `mv2 catalog --json` lists that `mv2 command` runs: commands, workflows and skills.
export function catalogEntries(listed: unknown): CatalogEntry[] {
  if (!Array.isArray(listed)) return []
  return (listed as Cataloged[]).flatMap((entry) => {
    const { kind, name, description } = entry ?? {}
    if (typeof kind !== "string" || !(kind in RUNNABLE) || typeof name !== "string" || !/^[^\s:]+$/.test(name)) return []
    return [{ kind, name, description: typeof description === "string" ? description.trim() : "" }]
  })
}

// A row writes the bare name when `mv2 command` takes it as it is: unique among what it runs, and
// not a subcommand's name, which would shadow it. Otherwise it writes `kind/name`.
export function catalogRows(entries: readonly CatalogEntry[]): Suggestion[] {
  const seen = new Map<string, number>()
  for (const entry of entries) seen.set(entry.name, (seen.get(entry.name) ?? 0) + 1)
  return entries.map((entry) => ({
    text: seen.get(entry.name) === 1 && !RESERVED.has(entry.name) ? entry.name : `${entry.kind}/${entry.name}`,
    description: RUNNABLE[entry.kind] + (entry.description ? ` · ${entry.description}` : ""),
  }))
}

type Listed = { id?: unknown; name?: unknown; universe?: unknown; live?: unknown; status?: unknown; phase?: unknown; damaged?: unknown }

// The agents `mv2 send` can reach from here: `mv2 list --json` also shows other universes' agents,
// which it cannot. This agent is left out, and so is a name `send` could not take as one word, and a row
// built from a damaged record (an orphan's name is its ID): send can't reach it.
export function agentRows(listed: unknown, self?: string, universe?: string): Suggestion[] {
  if (!Array.isArray(listed)) return []
  return (listed as Listed[]).flatMap((agent) => {
    const name = agent?.name
    if (typeof name !== "string" || !/^[^\s,]+$/.test(name) || agent.id === self || agent.damaged != null) return []
    if (universe && agent.universe !== universe) return []
    const status = typeof agent.status === "string" && agent.status !== "-" && agent.status !== "unknown" ? agent.status : undefined
    const parts = [agent.live, status, agent.phase].filter((part): part is string => typeof part === "string" && part !== "")
    return [{ text: name, description: parts.join(" · ") }]
  })
}

function starts(row: Suggestion, token: string): boolean {
  const shown = (row.label ?? row.text).toLowerCase()
  const typed = token.toLowerCase()
  return shown.startsWith(typed) || shown.slice(shown.indexOf("/") + 1).startsWith(typed)
}

// The rows for the token at the cursor, given the text before it.
export function suggest(before: string, token: string, known: Known): Suggestion[] {
  if (/^\/multiverse[ \t]+$/.test(before)) return [...SUBCOMMANDS, ...known.catalog].filter((row) => starts(row, token))
  if (/^\/multiverse[ \t]+send[ \t]+$/.test(before)) {
    const cut = token.lastIndexOf(",") + 1
    const named = new Set(token.slice(0, cut).split(","))
    return known.agents
      .filter((row) => !named.has(row.text) && starts(row, token.slice(cut)))
      .map((row) => ({ text: `${token.slice(0, cut)}${row.text} `, label: row.text, description: row.description }))
  }
  return []
}

type Kept = { rows: Suggestion[]; at: number; loading: boolean }
const kept: Record<"catalog" | "agents", Kept> = {
  catalog: { rows: [], at: -Infinity, loading: false },
  agents: { rows: [], at: -Infinity, loading: false },
}

// Read one kind of typeahead data with mv2. A failed read leaves no rows until the next one.
async function load($: EngineInterface, which: "catalog" | "agents", cli: string) {
  const entry = kept[which]
  try {
    const result = await $.process.run([cli, ...(which === "catalog" ? ["catalog", "--json"] : ["list", "--json"])],
                                       { timeoutMs: LOAD_TIMEOUT_MS })
    if (result.exitCode !== 0) entry.rows = []
    else if (which === "catalog") entry.rows = catalogRows(catalogEntries(JSON.parse(result.stdout)))
    else entry.rows = agentRows(JSON.parse(result.stdout), await $.env.get("MV2_AGENT_ID"), await $.env.get("MV2_UNIVERSE"))
  } catch {
    entry.rows = []
  } finally {
    entry.at = await $.clock.now()
    entry.loading = false
  }
}

export const register: Register = (on) => {
  // A load or reload starts with nothing kept.
  for (const entry of Object.values(kept)) Object.assign(entry, { rows: [], at: -Infinity, loading: false })

  on("prompt.autocomplete", async ($, e, next) => {
    const theirs = await next(e)
    if (!e.text.startsWith("/m")) return theirs
    const before = e.text.slice(0, e.start)
    // While the command's own name is typed (from `/mu`, past the other `/m` commands), read ahead
    // for the words after it.
    const reading = before === "" ? ("/" + COMMAND).startsWith(e.token) && e.token.length >= 3 : before.startsWith("/" + COMMAND)
    if (!reading) return theirs
    const cli = await $.env.get("MV2_CLI")
    if (!cli) return theirs
    const now = await $.clock.now()
    for (const which of ["catalog", "agents"] as const) {
      const entry = kept[which]
      if (!entry.loading && now - entry.at >= FRESH_MS) {
        entry.loading = true
        // After this hook returns: typing never waits on mv2.
        $.clock.after(0, () => load($, which, cli))
      }
    }
    const mine = suggest(before, e.token, { catalog: kept.catalog.rows, agents: kept.agents.rows })
    return mine.length ? { suggestions: [...theirs.suggestions, ...mine] } : theirs
  })
}
