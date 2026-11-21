import { ended, expect, onTestEnd, test } from './kit'

// The kit's own timeout starts the next test while a body still runs, and a world's cleanup is what stops its plugin
// copy reaching that next test: a cleanup that never runs, one skipped after another's failure, or a late one run
// as the next test's lets it leak.
test("a test's cleanups all run, even when its body never ends or one of them fails, and stay its own", async ($) => {
  const first = {}, second = {}  // each wrapped body's registrar, as the kit gives each test its own
  const ran: string[] = []
  const stuck = ended(async (_, on) => {
    onTestEnd(on, async () => { ran.push('first') })
    onTestEnd(on, async () => { throw new Error('cleanup broke') })
    onTestEnd(on, async () => { ran.push('last') })
    await new Promise<void>(() => {})
  }, 150)
  let failure = ''
  try { await stuck($, first as never) } catch (error) { failure = String(error) }
  expect(ran).toEqual(['last', 'first'])
  expect(failure).toContain('the test timed out after 100 ms')
  expect(failure).toContain('its cleanup failed too: cleanup broke')
  // The timed-out body runs on into the next test and registers a cleanup: it runs at once, never as the next test's.
  const order: string[] = []
  const next = ended(async (_, on) => {
    onTestEnd(on, async () => { order.push('own') })
    onTestEnd(first, async () => { order.push('stale') })
    await Promise.resolve()
    order.push('body')
  }, 150)
  await next($, second as never)
  expect(order).toEqual(['stale', 'body', 'own'])
  failure = ''
  const clean = ended(async (_, on) => { onTestEnd(on, async () => { throw new Error('only the cleanup broke') }) }, 150)
  try { await clean($, {} as never) } catch (error) { failure = String(error) }
  expect(failure).toContain("the test's cleanup failed: only the cleanup broke")
})
