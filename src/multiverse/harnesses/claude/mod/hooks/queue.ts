// The mv2 messages Claude holds for this agent right now (claude-queue.json in the agent folder): taken from the
// inbox and handed to Claude, not yet stored in its conversation, nor refused. mv2 reads it to tell an agent that
// another agent's follow-ups are still waiting for its turn to end (docs/design/queued-prompts.md).
//
// Only mv2's own messages are written, never what the person types. `coverage` is "complete" only when the mod has
// seen everything Claude holds of mv2's: from a launch's first load, or once the session has been idle (Claude
// runs what it queued before it goes idle). A reload starts again from nothing and doesn't know what the code
// before it handed over, so until then it is "unknown" with the reason.
import type { EngineInterface } from 'claude-code'
import { retryMissing } from './records'

export const QUEUE_FILE = 'claude-queue.json'
const SCHEMA = 1

export type QueueEntry = { id: string; mode: string; since: number }
// `why` says which uncertainty keeps it from being complete; the reason the file gives follows from it.
export type QueueView = { complete: boolean; why?: 'reloaded' | 'unsure'; entries: QueueEntry[] }

export const RELOADED = "The multiverse plugin reloaded while Claude may hold messages taken before it; " +
  "known again once the agent has been idle"
// A message not stored in time isn't proof Claude dropped it: a local command such as /compact, or a dialog, holds
// a taken message and stores it afterwards. It isn't listed (it may never come), and coverage says so.
export const UNSURE = "Claude may still hold an mv2 message it didn't store in time, as during /compact or a dialog; " +
  "known again once the agent has been idle after a later turn"

let writing: Promise<void> = Promise.resolve()

// Write the view as it stands when the write runs, one write at a time, so the last one written is the latest state.
// The file is written beside its name and moved over it, so mv2 never reads half of one.
export function publishQueue($: EngineInterface, view: (now: number) => QueueView, session: () => string | undefined) {
  const write = async () => {
    const [directory, launch] = await Promise.all([$.env.get('MV2_AGENT_DIR'), $.env.get('MV2_LAUNCH_ID')])
    if (!directory || !launch) return
    const now = await $.clock.now()
    const { complete, why, entries } = view(now)
    const record = {
      schema: SCHEMA, launch_id: launch, session_id: session() ?? null, observed_at: new Date(now).toISOString(),
      coverage: complete ? 'complete' : 'unknown', reason: complete ? null : why === 'unsure' ? UNSURE : RELOADED,
      entries: entries.map((entry) => ({ message_id: entry.id, mode: entry.mode, since: new Date(entry.since).toISOString() })),
    }
    const path = `${directory}/${QUEUE_FILE}`
    await $.fs.write(`${path}.tmp`, JSON.stringify(record) + '\n')
    await $.process.run(['mv', `${path}.tmp`, path], { timeoutMs: 10000 })
  }
  writing = writing.then(write, write).catch(() => $.ui.log('multiverse: queue report not written'))
  return writing
}

// Whether this load comes after another one in the same launch: the queue file already names this launch, or the
// launch's bridge is already registered. A new launch's first load finds neither, and Claude's queue is empty then.
export async function reloaded($: EngineInterface): Promise<boolean> {
  const [directory, launch] = await Promise.all([$.env.get('MV2_AGENT_DIR'), $.env.get('MV2_LAUNCH_ID')])
  if (!directory || !launch) return true
  const names = async (file: string, field: string) => {
    try {
      const data: unknown = JSON.parse(await retryMissing($, () => $.fs.read(`${directory}/${file}`)))
      const record = typeof data === 'object' && data !== null ? data as Record<string, unknown> : {}
      return record.launch_id === launch && (field === '' || (record[field] !== undefined && record[field] !== null))
    } catch {
      return false  // absent after the bounded retry, or unreadable: says nothing about this launch
    }
  }
  return (await names(QUEUE_FILE, '')) || (await names('runtime.json', 'native_bridge'))
}
