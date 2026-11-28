// The mod's own records in the agent's folder (prompt end records, mv2 slash's records): one retention rule and one
// way to remove them, for every folder that keeps them. Core keeps its own record of each outcome, so these are kept
// for a late reader only: the newest few dozen stay, and older ones go once they are a day old.
import type { EngineInterface } from 'claude-code'

export const KEEP = 48
export const KEEP_MS = 24 * 3600 * 1000
export const PRUNE_EVERY = 16  // records written between prunes

// A removal that fails is left for the next prune (or the next session start's sweep).
export async function remove($: EngineInterface, paths: string[]) {
  if (paths.length === 0) return
  try { await $.process.run(['rm', '-f', ...paths], { timeoutMs: 10000 }) } catch { /* the next prune removes them */ }
}

// Keep the newest KEEP of `folder`'s records whose names match `pattern`; of the rest, remove those over a day old.
export async function prune($: EngineInterface, folder: string, pattern: RegExp) {
  try {
    const records = (await $.fs.list(folder)).filter((entry) => pattern.test(entry.name)).sort((a, b) => b.mtimeMs - a.mtimeMs)
    const current = await $.clock.now()
    await remove($, records.slice(KEEP).filter((entry) => current - entry.mtimeMs > KEEP_MS).map((entry) => `${folder}/${entry.name}`))
  } catch { /* the next prune removes them */ }
}
