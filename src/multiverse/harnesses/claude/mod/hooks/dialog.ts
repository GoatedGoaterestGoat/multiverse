// A command's own dialog (/model's "Switch model?" confirmation, its picker, /config) reaches no hook: while a command
// runs, the mod asks the prompt box to take an empty insert at the cursor. Claude refuses it with the cause `dialog`
// while a dialog holds the keys; otherwise it changes nothing (the draft, the cursor and an open menu stay as they are).

export const FIRST_MS = 250
export const EVERY_MS = 1000

// Whether a fill's answer says a dialog holds the keys. Only Claude's own `dialog` cause does: `no_composer` means a
// headless session, and a refusal without a cause is a hook's (another plugin keeping text out), not a dialog.
export function dialogIn(filled: unknown): boolean {
  return typeof filled === "object" && filled !== null && (filled as { isFilled?: unknown }).isFilled === false
    && (filled as { refusal?: unknown }).refusal === "dialog"
}

// Checks while a command runs, first soon after it starts and then every second, and calls `change` each time the
// answer differs from the last. The returned stop ends it: no check runs or reports after it, even one under way.
export function watch(check: () => Promise<boolean>, after: (ms: number, run: () => void) => void,
                      change: (open: boolean) => void): () => void {
  let running = true
  let open = false
  const tick = async () => {
    if (!running) return
    let now = false
    try { now = await check() } catch { now = false }
    if (!running) return
    if (now !== open) {
      open = now
      change(open)
    }
    after(EVERY_MS, () => void tick())
  }
  after(FIRST_MS, () => void tick())
  return () => { running = false }
}
