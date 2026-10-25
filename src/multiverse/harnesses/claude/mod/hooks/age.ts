// How late a Multiverse message reaches the model, at the end of the sender line
// core frames it with: "Message from NAME (sent 25 min ago):", after any
// "(in universe U)". Shown only past ten minutes, rounded down, as core's
// framed_text says it in `mv2 inbox`, wakes and retries.

export const LATE_MS = 10 * 60 * 1000

// "sent 25 min ago", "sent 2 hours ago", "sent 3 days ago", or undefined when
// the message is not late or its send time can't be read. A time without its
// zone is refused, as core refuses it: JavaScript would read it as local time.
export function agePhrase(sent: string | undefined, nowMs: number): string | undefined {
  if (sent === undefined || !/(?:Z|[+-]\d\d:?\d\d)$/.test(sent)) return undefined
  const sentMs = Date.parse(sent)
  const age = nowMs - sentMs
  if (!Number.isFinite(age) || age <= LATE_MS) return undefined
  const minutes = Math.floor(age / 60000)
  if (minutes < 60) return `sent ${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `sent ${hours} hour${hours === 1 ? "" : "s"} ago`
  const days = Math.floor(hours / 24)
  return `sent ${days} day${days === 1 ? "" : "s"} ago`
}

// An age already in a sender line: core ages the frame when it prepares a wake or a retry.
const AGED = / \(sent \d+ (?:min|hours?|days?) ago\)(?=:$)/

// The framed text with `phrase` as its sender line's age (replacing any age it
// had, or removing it when `phrase` is undefined); any other text unchanged.
export function withAge(text: string, phrase: string | undefined): string {
  const end = text.indexOf("\n")
  const first = end < 0 ? text : text.slice(0, end)
  if (!/^Message from .+:$/.test(first)) return text
  const bare = first.replace(AGED, "")
  const line = phrase === undefined ? bare : `${bare.slice(0, -1)} (${phrase}):`
  return line + (end < 0 ? "" : text.slice(end))
}
