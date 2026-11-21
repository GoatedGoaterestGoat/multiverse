// The kit's test(), with a budget a busy machine can meet. The kit gives each test 5000 ms, and these tests, which
// take milliseconds alone, took up to 6.5 s each with four copies of the suite running at once: a test then failed
// on time, and its body, still running, made a rejection nothing handled once its engine was gone. Their clocks are
// the kit's, so the budget changes no check, only how long a hung test takes to fail.
import { test as kitTest, type TestBody, type TestOptions, type TestRest } from 'claude-code/testing'

export { describe, expect, mock } from 'claude-code/testing'

export const TEST_MS = 15_000
const CLEANUP_MS = 3_000  // of a test's budget, kept for its cleanups (a third of a short budget)

// What a test's world must finish before the test ends, pass, fail or time out. A test's plugin copy outlives its test
// until its own work settles, and meanwhile it reads the next test's hooks: a prompt it still holds keeps reporting
// into the next test's world. Cleanups belong to the test whose registrar (`on`) registered them: a body that timed
// out and runs on can't add one to the next test's, and one it adds after its test ended runs at once.
type Finish = () => Promise<void>
const owned = new Map<unknown, Finish[] | 'ended'>()
export const onTestEnd = (on: unknown, finish: Finish): void => {
  const ends = owned.get(on)
  if (Array.isArray(ends)) ends.push(finish)
  else void Promise.resolve().then(finish).catch(() => {})  // its test is over: clean up now, there is no one to tell
}

// Real time, as the kit's own budget is: the tests' clocks are the kit's mock. The kit's types don't declare timers.
const timers = globalThis as unknown as { setTimeout(run: () => void, ms: number): unknown; clearTimeout(timer: unknown): void }

const within = <T>(work: Promise<T>, ms: number, what: string): Promise<T> => new Promise<T>((resolve, reject) => {
  const timer = timers.setTimeout(() => reject(new Error(`${what} timed out after ${ms} ms`)), ms)
  work.then((value) => { timers.clearTimeout(timer); resolve(value) },
    (error: unknown) => { timers.clearTimeout(timer); reject(error) })
})

export const ended = (body: TestBody, budget: number): TestBody => async ($, on) => {
  const ends: Finish[] = []
  owned.set(on, ends)
  const reserve = Math.min(CLEANUP_MS, Math.floor(budget / 3))
  const started = Date.now()
  let failure: unknown = undefined
  let failed = false
  try {
    // Its own deadline inside the kit's, so the cleanups still run before the kit moves on to the next test.
    await within(Promise.resolve().then(() => body($, on)), budget - reserve, 'the test')
  } catch (error) {
    failed = true
    failure = error
  }
  // Every cleanup runs, the latest first, whatever an earlier one or the body did.
  const problems: string[] = []
  owned.set(on, 'ended')
  for (const finish of ends.reverse()) {
    try {
      await within(Promise.resolve().then(finish), Math.max(1, budget - (Date.now() - started) - 50), 'a cleanup')
    } catch (error) {
      problems.push(error instanceof Error ? error.message : String(error))
    }
  }
  if (failed) {
    if (problems.length && failure instanceof Error) failure.message += `\n(its cleanup failed too: ${problems.join('; ')})`
    throw failure
  }
  if (problems.length) throw new Error(`the test's cleanup failed: ${problems.join('; ')}`)
}

export const test = (name: string, ...rest: TestRest): void => {
  if (rest.length === 1) kitTest(name, { timeoutMs: TEST_MS }, ended(rest[0], TEST_MS))
  else {
    const options = { timeoutMs: TEST_MS, ...rest[0] } as TestOptions
    kitTest(name, options, ended(rest[1], options.timeoutMs ?? TEST_MS))
  }
}
