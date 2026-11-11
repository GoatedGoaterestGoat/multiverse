// Only for reads of mv2's replace-written records, never native calls, writes or exclusive claims.
import type { EngineInterface } from 'claude-code'

function missing(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const value = error as { code?: unknown; message?: unknown }
  // The host wraps fs failures as HooksError and retains the operation and errno in its message.
  return value.code === 'ENOENT' || typeof value.message === 'string' &&
    /(?:^|\$\.fs\.(?:read|stat): )ENOENT\b/.test(value.message)
}

export async function retryMissing<T>($: EngineInterface, read: () => Promise<T>): Promise<T> {
  const deadline = await $.clock.now() + 50
  for (let attempt = 0; ; attempt++) {
    try {
      return await read()
    } catch (error) {
      if (!missing(error)) throw error
      const remaining = deadline - await $.clock.now()
      if (attempt === 4 || remaining <= 0) throw error
      await $.clock.sleep(Math.min(5, remaining))
    }
  }
}
