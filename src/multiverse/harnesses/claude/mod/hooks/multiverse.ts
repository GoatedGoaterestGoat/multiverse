import type { EngineInterface, On } from 'claude-code'

// The native /multiverse command, registered at session start:
//   /multiverse | /multiverse list   the universe catalog, as command output
//   /multiverse <name> [args]        that command, workflow or skill, submitted as the
//                                    person's own prompt (what `mv2 command` prints)
//   /multiverse send <agent> <text>  a message to another agent, sent as this agent
//   /multiverse inbox                this agent's unread inbox-only messages, which
//                                    this marks read (`mv2 inbox`)
//
// Everything but `list` acts with the person's authority at this terminal (it
// sends as this agent, submits a prompt as the person's, or changes what the
// agent has read), so it runs only for a prompt the person typed (the composer,
// or Remote Control); any other origin may only list.

export const NAME = "multiverse"
export const DESCRIPTION = "Multiverse: list, run a command or skill, message an agent, read the inbox"
export const HINT = "[list | <name> [args] | send <agent> <text> | inbox]"

const PEOPLE = new Set(["composer", "bridge"])
const MESSAGE_LENGTH = 500
const TIMEOUT_MS = 20000

export type Parsed =
  | { kind: "list" }
  | { kind: "inbox" }
  | { kind: "send"; agent: string; text: string }
  | { kind: "prompt"; name: string; args: string }
  | { kind: "error"; text: string }

// `list`, `send` and `inbox` are reserved: a catalog entry with one of those
// names is shadowed by the subcommand.
export function parse(args: string): Parsed {
  const text = args.trim()
  if (text === "" || text === "list") return { kind: "list" }
  const split = text.search(/\s/)
  const word = split < 0 ? text : text.slice(0, split)
  const rest = split < 0 ? "" : text.slice(split).trim()
  if (word === "list") return { kind: "list" }
  if (word === "inbox") return { kind: "inbox" }
  if (word === "send") {
    const at = rest.search(/\s/)
    const agent = at < 0 ? rest : rest.slice(0, at)
    const message = at < 0 ? "" : rest.slice(at).trim()
    if (!agent || !message) return { kind: "error", text: "Usage: /multiverse send <agent> <text>" }
    return { kind: "send", agent, text: message }
  }
  return { kind: "prompt", name: word, args: rest }
}

type Message = { at?: string; from?: { agent_id?: string; name?: string }; text?: string }

// One unread message as a line: who, when and what, shortened.
export function describe(message: Message): string {
  const from = message.from ?? {}
  const sender = from.agent_id ? `agent ${from.agent_id.slice(0, 8)}` : from.name ?? "unknown"
  const text = (message.text ?? "").replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, " ").trim()
  const shown = text.length > MESSAGE_LENGTH ? text.slice(0, MESSAGE_LENGTH - 1).trimEnd() + "…" : text
  return `• ${sender}${message.at ? ` (${message.at.slice(0, 19).replace("T", " ")} UTC)` : ""}: ${shown}`
}

export function inbox(json: string): string {
  let messages: Message[]
  try {
    const data: unknown = JSON.parse(json)
    messages = Array.isArray((data as { messages?: unknown }).messages) ? (data as { messages: Message[] }).messages : []
  } catch {
    return "Could not read the inbox: unexpected output from mv2."
  }
  if (messages.length === 0) return "No unread inbox-only messages."
  const noun = messages.length === 1 ? "message" : "messages"
  return [`${messages.length} unread inbox-only ${noun} (now marked read):`, ...messages.map(describe)].join("\n")
}

// Run an mv2 command. Output and failure both come back as text.
async function mv2($: EngineInterface, cli: string, argv: string[]) {
  try {
    const result = await $.process.run([cli, ...argv], { timeoutMs: TIMEOUT_MS })
    return { ok: result.exitCode === 0, out: result.stdout, err: result.stderr.trim() || result.stdout.trim() }
  } catch (error) {
    return { ok: false, out: "", err: `mv2 did not finish: ${error instanceof Error ? error.message : String(error)}` }
  }
}

export function registerMultiverse(on: On) {
  on("command.run", { command: NAME }, async ($, e) => {
    const cli = await $.env.get("MV2_CLI")
    if (!cli) return { text: "Multiverse is not available in this session (MV2_CLI is not set)." }
    const parsed = parse(e.args)
    if (parsed.kind === "error") return { text: parsed.text }

    if (parsed.kind === "list") {
      const result = await mv2($, cli, ["catalog"])
      return { text: result.ok ? result.out.trim() || "Nothing in this universe's catalog." : `mv2 catalog failed: ${result.err}` }
    }

    if (!PEOPLE.has(e.origin.kind)) {
      return { text: `/multiverse ${parsed.kind === "prompt" ? parsed.name : parsed.kind} acts with the person's authority, so only a prompt the person typed can run it.` }
    }

    if (parsed.kind === "inbox") {
      // A normal read: it marks the messages read, and the person sees what the agent has now been shown.
      const result = await mv2($, cli, ["inbox"])
      return { text: result.ok ? inbox(result.out) : `mv2 inbox failed: ${result.err}` }
    }

    if (parsed.kind === "send") {
      // With this session's own MV2_AGENT_ID, so the recipient reads "Message from <this agent>".
      const result = await mv2($, cli, ["send", parsed.agent, `--text=${parsed.text}`])
      if (!result.ok) return { text: `Not sent: ${result.err}` }
      let state = "sent"
      try { state = String((JSON.parse(result.out) as { state?: unknown }).state ?? state) } catch { /* plain text output */ }
      return { text: `Sent to ${parsed.agent} (${state}).` }
    }

    const result = await mv2($, cli, ["command", parsed.name, ...(parsed.args ? ["--", parsed.args] : [])])
    if (!result.ok) return { text: `${result.err}\nRun /multiverse list to see what is available.` }
    // Submitting from inside this hook would wait on the turn it belongs to: leave it first.
    $.clock.after(0, () => {
      $.prompt.submit({ text: result.out, asUser: true })
        .catch((error: unknown) => $.ui.log(`multiverse: could not submit ${parsed.name}: ${String(error)}`))
    })
    return { text: `Running ${parsed.name}.` }
  })
}
