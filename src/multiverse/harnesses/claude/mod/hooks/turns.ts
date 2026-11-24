// The main conversation's turns, for core's event journal (index.ts reports them with its status reports): a turn
// starts at the row that opens it, a prompt joins it as a queued command at a tool result, and it ends at
// turn.complete, once its last row is in the transcript file. Each is named as the transcript reader groups rows
// (harnesses/claude/transcripts.py labels): a user row with an origin opens a prompt turn and a local command's row
// a command turn, so `turn` is the opening row's id and `ref` a row's id. Metadata only: never a row's text.
// A turn Claude continues on its own (turn.start with no new prompt row) has no start, and its end is a later end of
// the same turn, which supersedes the first.
//
// Rows are ordered by when their append was entered (`seq`), which is the order Claude stores them in; the order the
// appends finish in can differ, and is never used to rank them.

// An edge whose content isn't known yet holds its place (no `event` yet): an end still being proved, or a join whose
// row is still being stored. The edges after it wait behind it, so core never sees them out of order. index.ts sends
// only up to the first such place. A prompt turn's end (`holds`) keeps the agent 'working' until it is sent, even
// once proved, so idle never goes before it. A join's place is given up when its turn's end goes, or at a /clear
// for the old conversation, so it can never hold the queue past its turn's bound.
export type TurnTransition = { session: string; event?: Record<string, unknown>; holds?: boolean }
export const turnTransitions: TurnTransition[] = []

type Source = 'person' | 'harness'
type Opening = { ref: string; seq: number; source: Source; attempt?: string; firstTask: boolean }
type Row = { id: string; seq: number }

// After a local command's output: the next main row decides whether a report for the model follows (a user note in
// the same conversation, which the transcript reader groups under the output row: /context's, /rename's).
// The report row itself is held pending on the decision until noted (`note`): its append can finish before the
// output's, before the command's end even exists.
type Decision = { session: string | undefined; decided: boolean; report: boolean; pending: number; note?: string; owner?: Owner }

// A prompt turn or a local command: its rows noted so far (in storage order), its conversation, the rows admitted
// for it but not noted yet (an append still being stored), its joins' places, and for a command whether a report
// follows its output.
// `steps`: for a prompt turn Claude continued, the last row admitted before each later turn.start; an end proves only
// the rows of its own step, so a continuation's rows never become an earlier completion's. `inflight`: the admission
// order of its rows still being stored.
export type Owner = { id: string; kind: 'prompt' | 'command'; rows: Row[]; session: string | undefined; pending: number;
  places: Map<TurnTransition, number>; decision?: Decision; output?: string; wake?: () => void; steps?: number[];
  inflight?: Set<number> }

let seq = 0
let generation = 0                 // /clears so far: a row from before the latest is stale, known session id or not
let openedTo = 0                   // the newest opening row used so far: an older one noted late opens nothing
let commandTo = 0                  // the same for a local command's row
let opening: Opening | undefined  // stored, its turn.start not yet seen
let current: Owner | undefined     // the prompt turn running, or the last that ran
let command: { id: string; seq: number; session: string | undefined } | undefined  // a local command awaiting its output
let awaiting: Decision | undefined // a command's output whose next row hasn't arrived
let task = false                   // the next person's prompt row is this launch's task
const waiting = new Set<Owner>()   // owners whose end is being proved: a /clear wakes them

const OUTPUT = ['<local-command-stdout>', '<local-command-stderr>', '<local-command-caveat>']
const isOutput = (text: string) => OUTPUT.some((marker) => text.trimStart().startsWith(marker))

// On the first load, MV2_FIRST_TASK marks a CLI task or wake prompt. A reload never marks the opener.
export function loaded(firstTask: boolean) {
  opening = undefined
  current = undefined
  command = undefined
  awaiting = undefined
  task = firstTask
}

function push(session: string | undefined, event: Record<string, unknown>) {
  if (session !== undefined) turnTransitions.push({ session, event })
}

function poke(owner: Owner | undefined) {
  if (owner === undefined) return
  const wake = owner.wake
  owner.wake = undefined
  wake?.()
}

function drop(place: TurnTransition) {
  const at = turnTransitions.indexOf(place)
  if (at >= 0) turnTransitions.splice(at, 1)
}

function source(origin: { kind: string }): Source {
  // Typed at the terminal (Claude stores a typed local command as its own); anything else came from elsewhere, and
  // core makes it mv2 when the attempt it carries is one of mv2's deliveries.
  return origin.kind === 'composer' || origin.kind === 'engine' ? 'person' : 'harness'
}

// `session`: the conversation the turn ran in, bound when it started: its end goes under that one, even if a /clear
// moved the session on while the end waited (core then drops it and closes the old turn at the switch).
export type Ending = { turn: string; kind: 'prompt' | 'command'; state: string; session: string | undefined; owner: Owner;
  step?: number }

// What a row arriving (session.append entered, main conversation only) is: its place in storage order; its owner,
// held pending until the row is noted, so it lands there even if another turn has started (or a /clear moved on) by
// then; a join's place among the edges, taken now so it can never go after its own turn's end; and, for a local
// command's output, the decision its next row makes.
// `session`: the conversation the row arrived in, which a late callback never trades for a newer one's; `generation`:
// how many /clears there had been, so a row that arrived before its session id was known is judged too.
export type Ticket = { seq: number; session?: string; generation?: number; owner?: Owner; place?: TurnTransition;
  output?: Decision; reportOf?: Decision }

export function admit(row: { type: string; name?: string; door: string; text: string }, running: boolean,
  session: string | undefined): Ticket {
  const ticket: Ticket = { seq: ++seq, session, generation }
  const decision = awaiting
  awaiting = undefined
  if (decision !== undefined) {
    decision.decided = true
    decision.report = row.door === 'note' && row.type === 'user' && decision.session !== undefined && decision.session === session
    if (decision.report) {
      decision.pending += 1
      ticket.reportOf = decision
    }
    poke(decision.owner)
  }
  if (row.type === 'system' && row.name === 'local_command' && isOutput(row.text)) {
    ticket.output = { session, decided: false, report: false, pending: 0 }
    awaiting = ticket.output
  }
  const owner = running ? current : undefined
  if (owner === undefined) return ticket
  owner.pending += 1
  ;(owner.inflight ??= new Set()).add(ticket.seq)
  ticket.owner = owner
  if (row.type === 'attachment' && row.name === 'queued_command' && owner.session !== undefined) {
    ticket.place = { session: owner.session }
    turnTransitions.push(ticket.place)
    owner.places.set(ticket.place, ticket.seq)
  }
  return ticket
}

// The row is noted, or never will be: its owner's end looks again at once, and a join's place it never filled goes.
// Says whether the queue changed, so index.ts reports what may now be ready.
export function release(ticket: Ticket): boolean {
  let changed = false
  if (ticket.place !== undefined && ticket.place.event === undefined) {
    drop(ticket.place)
    ticket.owner?.places.delete(ticket.place)
    changed = true
  }
  if (ticket.owner !== undefined) {
    ticket.owner.pending -= 1
    ticket.owner.inflight?.delete(ticket.seq)
    poke(ticket.owner)
  }
  if (ticket.reportOf !== undefined) {
    ticket.reportOf.pending -= 1
    poke(ticket.reportOf.owner)
  }
  return changed
}

// The owner's end is going: a join of it still being stored gives up its place rather than hold the queue.
// Only the joins of the end's own step: a continuation's join, admitted after its turn.start, keeps its place.
export function abandon(end: Ending) {
  const upTo = limit(end)
  for (const [place, at] of end.owner.places) {
    if (at > upTo) continue
    if (place.event === undefined) drop(place)
    end.owner.places.delete(place)
  }
}

// A /clear: a command's pending decision has no report (a note now is the new conversation's), and an end still
// being proved stops (the new file can't hold its rows). The old conversation's edges still go, in their own order:
// order is kept per conversation (batch), so they never hold up the new one's.
export function cleared() {
  generation += 1
  current = undefined  // the old conversation's turn: only the hooks and rows that captured it still reach it
  opening = undefined  // a prompt row stored but not yet started is the old conversation's
  command = undefined  // as is a local command still waiting for its output
  if (awaiting !== undefined) {
    awaiting.decided = true
    awaiting.report = false
    poke(awaiting.owner)
    awaiting = undefined
  }
  for (const owner of waiting) poke(owner)
}

// The next edges ready to send, all one conversation's, in order: from the first known edge with no unknown edge of
// its conversation before it, its conversation's known edges up to the next unknown one. Core keeps order within a
// conversation only, so another conversation's waiting edge never holds these back.
export function batch(limit: number): TurnTransition[] {
  const blocked = new Set<string>()
  let session: string | undefined
  const out: TurnTransition[] = []
  for (const edge of turnTransitions) {
    if (out.length >= limit) break
    if (session === undefined) {
      if (blocked.has(edge.session)) continue
      if (edge.event === undefined) { blocked.add(edge.session); continue }
      session = edge.session
      out.push(edge)
    } else if (edge.session === session) {
      if (edge.event === undefined) break
      out.push(edge)
    }
  }
  return out
}

export function take(edges: TurnTransition[]) {
  for (const edge of edges) drop(edge)
}

export function proving(owner: Owner, on: boolean) {
  if (on) waiting.add(owner)
  else waiting.delete(owner)
}

function insert(rows: Row[], row: Row) {
  let at = rows.length
  while (at > 0 && (rows[at - 1]?.seq ?? 0) > row.seq) at -= 1
  rows.splice(at, 0, row)
}

// A row the main conversation stored. `attempt`: the mv2 delivery attempt the mod matched it to. `slash`: the row is
// a command mv2 ran for 'mv2 slash' (`attempt` is the run): the person's, as typed. Returns a local command's end, to
// be queued once its rows are in the transcript file (index.ts proves it, as for a turn's). A row noted after a /clear
// moved on from the conversation it arrived in is stale: it still lands in its own turn (its rows, its join), but it
// opens or ends nothing, so the new conversation's turns never take it; nor does an older row noted late replace a
// newer one's opening or command.
export function stored(session: string | undefined, row: { door: string; origin: { kind: string }; type: string;
  name?: string; uuid: string; text: string }, attempt: string | undefined, ticket: Ticket, slash = false): Ending | undefined {
  const from = (origin: { kind: string }): Source => slash ? 'person' : source(origin)
  if (ticket.reportOf !== undefined) {
    ticket.reportOf.note = row.uuid  // a command's report for the model: its end names it once it is whole in the file
    return undefined
  }
  const owner = ticket.owner
  if (owner !== undefined) insert(owner.rows, { id: row.uuid, seq: ticket.seq })
  const scope = ticket.session ?? session
  const stale = ticket.generation !== undefined && ticket.generation !== generation
  if (row.type === 'user' && (row.door === 'prompt' || row.door === 'command') && row.origin.kind !== 'engine'
      && row.origin.kind !== 'model' && row.origin.kind !== 'tool') {
    if (stale || ticket.seq <= openedTo || (opening !== undefined && opening.seq > ticket.seq)) return undefined
    const first = task && row.origin.kind === 'composer'
    task = false
    opening = { ref: row.uuid, seq: ticket.seq, source: from(row.origin), attempt, firstTask: first }
    return undefined
  }
  if (row.type === 'system' && row.name === 'local_command') {
    if (stale) return undefined
    if (isOutput(row.text)) {
      if (command === undefined || command.seq > ticket.seq) return undefined
      const ended: Owner = { id: command.id, kind: 'command', rows: [{ id: command.id, seq: command.seq }, { id: row.uuid, seq: ticket.seq }],
        session: command.session, pending: 0, places: new Map(), decision: ticket.output, output: row.uuid }
      if (ticket.output !== undefined) ticket.output.owner = ended
      const end: Ending = { turn: command.id, kind: 'command', state: 'finished', session: command.session, owner: ended }
      command = undefined
      return end
    }
    if (ticket.seq <= commandTo) return undefined
    commandTo = ticket.seq
    command = { id: row.uuid, seq: ticket.seq, session: scope }
    push(scope, { type: 'started', turn: row.uuid, kind: 'command', ref: row.uuid, source: from(row.origin),
      ...(attempt ? { attempt } : {}), first_task: false })
    return undefined
  }
  if (owner !== undefined && row.type === 'attachment' && row.name === 'queued_command') {
    const event = { type: 'joined', turn: owner.id, ref: row.uuid, source: source(row.origin), ...(attempt ? { attempt } : {}) }
    if (ticket.place !== undefined) {
      ticket.place.event = event  // under its own turn's conversation, in its own place
      owner.places.delete(ticket.place)
    } else push(owner.session, event)
  }
  return undefined
}

// turn.start on the main conversation: the stored opening row starts it; without one Claude continues the last turn.
export function started(session: string | undefined) {
  if (opening === undefined) return
  const { ref, seq: at, source: from, attempt, firstTask } = opening
  opening = undefined
  openedTo = Math.max(openedTo, at)
  current = { id: ref, kind: 'prompt', rows: [{ id: ref, seq: at }], session, pending: 0, places: new Map() }
  push(session, { type: 'started', turn: ref, kind: 'prompt', ref, source: from, ...(attempt ? { attempt } : {}), first_task: firstTask })
}

const STATES: Record<string, string> = { answer: 'finished', refusal: 'finished', aborted: 'interrupted', error: 'error' }

// turn.complete on the main conversation: which turn ended, and how. The end itself is queued by ended(), once the
// turn's rows are in the file (or the wait's bound passed).
// `turn`: the turn that was running when the hook was entered (a newer one may have started since). No default: a
// hook that captured none (a reload mid-turn) ends nothing, never whatever turn is current by the time it resumes.
export function ending(reason: string | undefined, aborted: boolean | undefined, turn: Owner | undefined): Ending | undefined {
  if (turn === undefined) return undefined  // a reload mid-turn: this load never saw what opened it
  const state = STATES[reason ?? ''] ?? (aborted ? 'interrupted' : 'unknown')
  return { turn: turn.id, kind: 'prompt', state, session: turn.session, owner: turn, step: turn.steps?.length ?? 0 }
}

// turn.start entered on the main conversation: rows admitted from now on are a later step's (a continuation keeps the
// turn; a new turn takes over current once its start is known).
export function stepped() {
  if (current !== undefined) (current.steps ??= []).push(seq)
}

// The last row admission an end proves: its step's boundary, once a later start has drawn one.
export function limit(end: Ending): number {
  return end.step === undefined ? Infinity : end.owner.steps?.[end.step] ?? Infinity
}

// Whether any of the end's rows, up to its limit, is still being stored.
export function storing(end: Ending): boolean {
  const upTo = limit(end)
  for (const at of end.owner.inflight ?? []) if (at <= upTo) return true
  return false
}

// The prompt turn running now, or the last that ran.
export function active(): Owner | undefined {
  return current
}

// The end's place, taken as it is decided (turn.complete, a local command's output row), before anything after it.
export function reserve(end: Ending): TurnTransition | undefined {
  if (end.session === undefined) return undefined
  const place: TurnTransition = { session: end.session, holds: end.kind === 'prompt' }
  turnTransitions.push(place)
  return place
}

export function ended(place: TurnTransition | undefined, end: Ending, ref: string | null) {
  if (place !== undefined) place.event = { type: 'ended', turn: end.turn, kind: end.kind, ref, state: end.state }
}

// A whole record read from the file's tail, in file order.
export type DiskRecord = { uuid: string; parent?: string; type?: string; meta?: boolean }

// The end's row from what the file holds: the owner's row latest in the file, and whether the end can go now. A
// prompt turn's can go once its rows, from the first the file holds on in storage order, are all whole there (a row
// before that one is older than the tail read; one still missing, or half written, after it isn't). A command's
// can go once its output is whole and its next row has decided: with a report, once the report is noted and whole
// in the file (by its id, or as the reader finds it, a meta user row whose parent is the output row).
export function latest(owner: Owner, disk: DiskRecord[], upTo = Infinity): { ref: string | null; complete: boolean } {
  const at = new Map(disk.map((record, i) => [record.uuid, i]))
  let ref: string | null = null
  let best = -1
  const consider = (id: string) => {
    const i = at.get(id)
    if (i !== undefined && i > best) { best = i; ref = id }
  }
  const rows = owner.rows.filter((row) => row.seq <= upTo)
  for (const row of rows) consider(row.id)
  if (owner.kind === 'command') {
    const decision = owner.decision
    const noted = decision?.note !== undefined && at.has(decision.note) ? decision.note : undefined
    const linked = owner.output === undefined ? undefined
      : disk.find((record) => record.parent === owner.output && record.type === 'user' && record.meta === true)?.uuid
    const report = noted ?? linked
    if (report !== undefined) consider(report)
    const decided = decision === undefined || decision.decided
    const reported = decision === undefined || !decision.report || (decision.pending === 0 && report !== undefined)
    return { ref, complete: owner.output !== undefined && at.has(owner.output) && decided && reported }
  }
  const first = rows.findIndex((row) => at.has(row.id))
  if (first < 0) return { ref, complete: false }
  return { ref, complete: rows.slice(first).every((row) => at.has(row.id)) }
}
