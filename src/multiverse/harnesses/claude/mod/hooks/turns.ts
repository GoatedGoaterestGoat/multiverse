// The main conversation's turns, for core's event journal (index.ts reports them with its status reports): a turn
// starts at the row that opens it, a prompt joins it as a queued command at a tool result, and it ends at
// turn.complete, once its last row is in the transcript file. Each is named as the transcript reader groups rows
// (harnesses/claude/transcripts.py labels): a user row with an origin opens a prompt turn and a local command's row
// a command turn, so `turn` is the opening row's id and `ref` a row's id. Metadata only: never a row's text.
// A turn Claude continues on its own (turn.start with no new prompt row) has no start, and its end is a later end of
// the same turn, which supersedes the first.
// Claude can take several stored prompt rows in at one turn.start (follow-ups queued behind a turn): the reader makes
// each a turn of its own, so each gets its own start, and each but the last its end, before the next one's start.
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
type Opening = { ref: string; seq: number; session: string | undefined; source: Source; attempt?: string; firstTask: boolean }
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
  inflight?: Set<number>; last?: string }

let seq = 0
let generation = 0                 // /clears so far: a row from before the latest is stale, known session id or not
let openedTo = 0                   // the newest opening row used so far: an older one noted late opens nothing
let commandTo = 0                  // the same for a local command's row
let openings: Opening[] = []       // stored, their turn.start not yet seen, in storage order
const arriving = new Set<number>() // opener rows admitted and not yet noted: a turn.start waits for those before it
let arrival: (() => void) | undefined  // wakes a turn.start waiting for them
let loose: Row[] = []              // rows noted between turns, before the next turn.start: grouped with the opener before them
const unowned = new Map<number, Ticket>()  // such rows still being stored
let current: Owner | undefined     // the prompt turn running, or the last that ran
let command: { id: string; seq: number; session: string | undefined } | undefined  // a local command awaiting its output
let awaiting: Decision | undefined // a command's output whose next row hasn't arrived
let task = false                   // the next person's prompt row is this launch's task
let span = false                   // after a compaction, until its first real row: the reader's compaction span
let naming: Caveat | undefined     // a caveat whose next row hasn't arrived yet: that row says whether it names the command
let compacting: Compact | undefined  // a /compact's command turn, until its compaction returns
const waiting = new Set<Owner>()   // owners whose end is being proved: a /clear wakes them

const OUTPUT = ['<local-command-stdout>', '<local-command-stderr>', '<local-command-caveat>']
const isOutput = (text: string) => OUTPUT.some((marker) => text.trimStart().startsWith(marker))
const MARKS = [...OUTPUT, '<command-name>', '/', '[Request interrupted by user']
const CAVEAT = '<local-command-caveat>'

// A local command Claude stores as user rows (Claude Code 2.1.332's /reload-plugins, typed or run by mv2): a meta
// caveat, the '<command-name>' row, then the system output row. The reader makes the caveat the command turn's first
// row, so the turn starts there; its start takes its place at once and is filled once the row after it says how:
// a '<command-name>' row names the command, and the run when mv2 ran it, once noted; any other row leaves the
// caveat's own source. Its place is taken as the caveat arrives, or, before the session is known, as it is noted.
type Caveat = { place?: TurnTransition; id?: string; source?: Source; attempt?: string; named?: boolean; nameDone?: boolean }
const caveated = (row: Opener) => row.type === 'user' && row.meta === true && row.text.trimStart().startsWith(CAVEAT)
const names = (row: Opener) => row.type === 'user' && row.text.trimStart().startsWith('<command-name>')

// A /compact's own row (Claude Code 2.1.332): typed, a note from Claude's engine; run by mv2, a prompt the plugin
// submitted. The reader makes it the first row of its command turn (the file holds it rewritten as a
// '<command-name>' row with the same id), and no row after it reaches the mod, so the turn starts at it and ends
// when its compaction returns (compactEnded). Claude writes that row to the file only after the compaction, so its
// start keeps its place until then, for core to find the row. A compaction that fails or is cancelled ends it as
// it went; one that never comes (Claude's "No messages to compact" raises no session.compact) lets its start go at
// the next command, turn.start or /clear, and leaves its end to core. Its start can then be unresolved: Claude
// files such a /compact under a caveat the mod never sees, or not at all.
type Compact = { place?: TurnTransition; owner?: Owner; start?: Record<string, unknown>; gone?: boolean }
const go = (compact: Compact | undefined) => {
  if (compact === undefined) return
  compact.gone = true  // noted later, its start goes at once
  if (compact.place !== undefined && compact.place.event === undefined && compact.start !== undefined) compact.place.event = compact.start
}
const compacts = (row: Opener) => row.type === 'user' && /^\/compact(\s|$)/.test(row.text.trimStart())
  && (row.door === 'note' || row.door === 'prompt' || row.door === 'command')
  && row.origin.kind !== 'model' && row.origin.kind !== 'tool'

// The command a caveat opened: a typed one (no origin stamped, or the person's or engine's) is the person's.
const typed = (origin: { kind: string }): Source =>
  origin.kind === 'unclassified' || origin.kind === 'composer' || origin.kind === 'engine' ? 'person' : 'harness'

function fill(caveat: Caveat) {
  if (caveat.place === undefined || caveat.place.event !== undefined || caveat.id === undefined || caveat.named === undefined) return
  if (caveat.named && !caveat.nameDone) return
  caveat.place.event = { type: 'started', turn: caveat.id, kind: 'command', ref: caveat.id, source: caveat.source ?? 'person',
    ...(caveat.attempt ? { attempt: caveat.attempt } : {}), first_task: false }
}

type Opener = { type: string; door: string; origin: { kind: string }; meta?: boolean; text: string }

// The reader's prompt rule (harnesses/claude/transcripts.py labels), as far as a row's append shows it (a local
// command's caveat and name rows, which the reader makes a command turn, are taken before it): a user row
// Claude stamped with an origin other than its engine's, the model's or a tool's opens a prompt turn; one with no
// stamp ('unclassified', no origin in the file) only if it is neither meta nor marked as a command, its output, a
// /compact or an interruption (a typed /reload-plugins stores such rows), and isn't in a compaction's span. The
// reader also wants an unstamped row's promptId to be new; Claude doesn't show an append its promptId, and the
// rows that share one with an earlier row come through other doors (tool results, attachments) or are a command's
// or a compaction's own.
const stamped = (row: Opener) => row.origin.kind !== 'unclassified' && row.origin.kind !== 'engine'
  && row.origin.kind !== 'model' && row.origin.kind !== 'tool'
function opens(row: Opener, inSpan = false): boolean {
  if (row.type !== 'user' || (row.door !== 'prompt' && row.door !== 'command')) return false
  if (stamped(row)) return true
  if (row.origin.kind !== 'unclassified' || inSpan) return false
  const head = row.text.trimStart()
  return row.meta !== true && !MARKS.some((mark) => head.startsWith(mark))
}

// The reader's end of a compaction's span: a stamped user row, a reply or a tool result.
const real = (row: Opener) => (row.type === 'user' && stamped(row)) || row.door === 'response' || row.door === 'tool-result'

// A row noted between turns that the reader groups with the prompt row before it: neither an opener nor a command's
// own row (a /compact, a caveat, a '<command-name>' row, on any door), output or report, which the reader puts in a
// command turn of its own.
const groups = (row: Opener & { name?: string }, inSpan: boolean) => !opens(row, inSpan) && row.door !== 'command'
  && !(row.type === 'system' && row.name === 'local_command') && !compacts(row) && !caveated(row) && !names(row)

// A compaction began (session.compact): until its first real row, an unstamped row opens nothing.
export function compacted() {
  span = true
}

// On the first load, MV2_FIRST_TASK marks a CLI task or wake prompt. A reload never marks the opener.
export function loaded(firstTask: boolean) {
  openings = []
  span = false
  naming = undefined
  compacting = undefined
  arriving.clear()
  loose = []
  unowned.clear()
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
// `unread`: its conversation's file is no longer read (a /clear moved on): the end names its last noted row instead.
export type Ending = { turn: string; kind: 'prompt' | 'command'; state: string; session: string | undefined; owner: Owner;
  step?: number; unread?: boolean; compact?: Compact }

// What a row arriving (session.append entered, main conversation only) is: its place in storage order; its owner,
// held pending until the row is noted, so it lands there even if another turn has started (or a /clear moved on) by
// then; a join's place among the edges, taken now so it can never go after its own turn's end; and, for a local
// command's output, the decision its next row makes.
// `session`: the conversation the row arrived in, which a late callback never trades for a newer one's; `generation`:
// how many /clears there had been, so a row that arrived before its session id was known is judged too.
// `opens`: an opener row, which a turn.start waits for until it is noted; `loose`: a row between turns that groups
// with the opener before it; `caveat`: a local command's caveat; `nameOf`: the '<command-name>' row after one.
export type Ticket = { seq: number; session?: string; generation?: number; owner?: Owner; place?: TurnTransition;
  output?: Decision; reportOf?: Decision; opens?: boolean; loose?: boolean; caveat?: Caveat; nameOf?: Caveat;
  compact?: Compact }

export function admit(row: Opener & { name?: string }, running: boolean, session: string | undefined): Ticket {
  const ticket: Ticket = { seq: ++seq, session, generation }
  const inSpan = span && !real(row)  // a real row ends the span before it is counted, as the reader reads it
  // The row after a caveat says whether it names the command (decided in storage order).
  const before = naming
  naming = undefined
  if (before !== undefined) {
    before.named = names(row)
    if (before.named) ticket.nameOf = before
    else fill(before)
  }
  if (caveated(row) && !inSpan) {  // a caveat a compaction stored is its output, as the reader counts it
    const caveat: Caveat = {}
    if (session !== undefined) {
      caveat.place = { session }
      turnTransitions.push(caveat.place)
    }
    ticket.caveat = naming = caveat
  }
  // A new command means a /compact before it isn't compacting any more: its start goes first.
  const starts = compacts(row) || (caveated(row) && !inSpan) || (row.type === 'system' && row.name === 'local_command' && !isOutput(row.text))
  if (starts && compacting !== undefined) {
    go(compacting)
    compacting = undefined
  }
  if (compacts(row)) {
    const compact: Compact = {}
    if (session !== undefined) {
      compact.place = { session }
      turnTransitions.push(compact.place)
    }
    ticket.compact = compacting = compact
  }
  if (ticket.nameOf === undefined && opens(row, inSpan)) {
    ticket.opens = true
    arriving.add(ticket.seq)
  }
  if (real(row)) span = false  // decided in storage order, as the reader reads them
  const decision = awaiting
  awaiting = undefined
  if (decision !== undefined) {
    decision.decided = true
    // A report is a meta note, as the reader counts it: a /compact typed right after is a note too, but not meta.
    decision.report = row.door === 'note' && row.type === 'user' && row.meta === true && decision.session !== undefined
      && decision.session === session
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
  if (owner === undefined) {
    if (!running && ticket.reportOf === undefined && ticket.output === undefined && groups(row, inSpan)) {
      ticket.loose = true
      unowned.set(ticket.seq, ticket)
    }
    return ticket
  }
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
  // A /compact or a caveat never noted opens nothing; a name row never noted leaves the caveat's own source.
  if (ticket.compact !== undefined && ticket.compact.owner === undefined && ticket.compact.start === undefined) {
    if (ticket.compact.place !== undefined) drop(ticket.compact.place)
    if (compacting === ticket.compact) compacting = undefined
    changed = true
  }
  if (ticket.caveat !== undefined && ticket.caveat.id === undefined && ticket.caveat.place?.event === undefined) {
    if (ticket.caveat.place !== undefined) drop(ticket.caveat.place)
    if (naming === ticket.caveat) naming = undefined
    changed = true
  }
  if (ticket.nameOf !== undefined && !ticket.nameOf.nameDone) {
    ticket.nameOf.nameDone = true
    fill(ticket.nameOf)
    changed = true
  }
  if (ticket.opens === true && arriving.delete(ticket.seq)) arrival?.()
  unowned.delete(ticket.seq)
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
// order is kept per conversation (batch), so they never hold up the new one's. Prompt rows it stored and never took
// in are still its turns: each starts under it, each but the last ends finished (returned, with their places, for
// index.ts, as started() returns them), and the last is left open, for core to close at the switch.
export function cleared(): { end: Ending; place: TurnTransition | undefined }[] {
  const ends = inputs(openings, (opening) => opening.session, seq, false)
  generation += 1
  span = false
  current = undefined  // the old conversation's turn: only the hooks and rows that captured it still reach it
  openings = []  // prompt rows stored but not yet started are the old conversation's
  go(compacting)
  compacting = undefined
  if (naming !== undefined) {  // a caveat whose next row will be the new conversation's: its own source
    naming.named = false
    fill(naming)
    naming = undefined
  }
  loose = []
  unowned.clear()
  arriving.clear()  // and a new turn.start waits for none of its rows
  arrival?.()
  command = undefined  // as is a local command still waiting for its output
  if (awaiting !== undefined) {
    awaiting.decided = true
    awaiting.report = false
    poke(awaiting.owner)
    awaiting = undefined
  }
  for (const owner of waiting) poke(owner)
  return ends
}

// The next edges ready to send, all one conversation's, in order: from the first known edge with no unknown edge of
// its conversation before it, its conversation's known edges up to the next unknown one. Core keeps order within a
// conversation only, so another conversation's waiting edge never holds these back.
// `held`: conversations whose edges wait (core hasn't recorded the move to them yet, and would drop them).
export function batch(limit: number, held: ReadonlySet<string> = new Set()): TurnTransition[] {
  const blocked = new Set<string>(held)
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
export function stored(session: string | undefined, row: Opener & { name?: string; uuid: string }, attempt: string | undefined,
  ticket: Ticket, slash = false): Ending | undefined {
  const from = (origin: { kind: string }): Source => slash ? 'person' : source(origin)
  if (ticket.reportOf !== undefined) {
    ticket.reportOf.note = row.uuid  // a command's report for the model: its end names it once it is whole in the file
    return undefined
  }
  const owner = ticket.owner
  if (owner !== undefined) insert(owner.rows, { id: row.uuid, seq: ticket.seq })
  const scope = ticket.session ?? session
  const stale = ticket.generation !== undefined && ticket.generation !== generation
  if (ticket.compact !== undefined) {
    const compact = ticket.compact
    if (stale || scope === undefined) {
      if (compact.place !== undefined) drop(compact.place)
      if (compacting === compact) compacting = undefined
      return undefined
    }
    if (compact.place === undefined) {
      compact.place = { session: scope }
      turnTransitions.push(compact.place)
    }
    compact.start = { type: 'started', turn: row.uuid, kind: 'command', ref: row.uuid,
      source: slash ? 'person' : typed(row.origin), ...(attempt ? { attempt } : {}), first_task: false }
    // Its own row is the last of its rows the mod sees: its end names it, once that row is in the file.
    compact.owner = { id: row.uuid, kind: 'command', rows: [{ id: row.uuid, seq: ticket.seq }], session: compact.place.session,
      pending: 0, places: new Map(), output: row.uuid }
    if (compact.gone) compact.place.event = compact.start  // let go while its row was being stored
    return undefined
  }
  if (ticket.caveat !== undefined) {
    const caveat = ticket.caveat
    if (stale || ticket.seq <= commandTo || scope === undefined) {
      if (caveat.place !== undefined) drop(caveat.place)
      return undefined
    }
    commandTo = ticket.seq
    if (caveat.place === undefined) {
      caveat.place = { session: scope }
      turnTransitions.push(caveat.place)
    }
    caveat.id = row.uuid
    caveat.source = slash ? 'person' : typed(row.origin)
    if (attempt) caveat.attempt = attempt
    command = { id: row.uuid, seq: ticket.seq, session: caveat.place.session }
    fill(caveat)
    return undefined
  }
  if (ticket.nameOf !== undefined) {
    const caveat = ticket.nameOf
    if (slash) caveat.source = 'person'  // mv2 ran it: this row is the one that names the run
    if (attempt) caveat.attempt = attempt
    caveat.nameDone = true
    fill(caveat)
    return undefined
  }
  if (ticket.opens === true) {
    if (stale || ticket.seq <= openedTo) return undefined
    const first = task && row.origin.kind === 'composer'
    task = false
    const opening = { ref: row.uuid, seq: ticket.seq, session: scope, source: from(row.origin), attempt, firstTask: first }
    let at = openings.length
    while (at > 0 && (openings[at - 1]?.seq ?? 0) > opening.seq) at -= 1
    openings.splice(at, 0, opening)  // in storage order, whatever order the rows are noted in
    return undefined
  }
  if (ticket.loose === true && owner === undefined && !stale) {
    insert(loose, { id: row.uuid, seq: ticket.seq })
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

// A manual compaction returned, or failed (`state`): the /compact it ran for starts and ends, at its own row.
export function compactEnded(state = 'finished'): Ending | undefined {
  const compact = compacting
  compacting = undefined
  // A compaction that happened leaves the /compact's row its turn's first, in the span; one that failed (nothing to
  // compact, an error, Esc) is stored under a caveat the mod never sees, so its start waits for the file (storedUnder).
  if (state === 'finished' || compact?.owner === undefined) go(compact)  // its row is in the file now, if it ever will be
  if (compact?.owner === undefined) return undefined
  return { turn: compact.owner.id, kind: 'command', state, session: compact.owner.session, owner: compact.owner,
    ...(state === 'finished' ? {} : { compact }) }
}

// A failed /compact, from the file: Claude stored its row under its caveat (the row's parent, a meta user row), which
// opens its command turn, so its start and end name the caveat, and its end the output under the row. The start goes
// once the row is there, or when the end goes without (`disk` undefined).
export function storedUnder(end: Ending, disk: DiskRecord[] | undefined) {
  const compact = end.compact
  if (compact === undefined || compact.gone || compact.start === undefined) return
  const mine = compact.start.ref
  if (disk !== undefined) {
    const own = disk.find((record) => record.uuid === mine)
    if (own === undefined) return  // not in the file yet
    const caveat = disk.find((record) => record.uuid === own.parent && record.type === 'user' && record.meta === true)
    if (caveat !== undefined) {
      compact.start = { ...compact.start, turn: caveat.uuid, ref: caveat.uuid }
      end.turn = end.owner.id = caveat.uuid
      const output = disk.find((record) => record.parent === mine)
      if (output !== undefined) end.owner.output = end.owner.last = output.uuid
    }
  }
  go(compact)
}

// Whether every opener row admitted up to `boundary` has been noted (or never will be).
export function arrived(boundary: number): boolean {
  for (const at of arriving) if (at <= boundary) return false
  return true
}

// Wakes a turn.start waiting for opener rows still arriving (undefined: none waits).
export function onArrival(wake: (() => void) | undefined) {
  arrival = wake
}

// turn.start on the main conversation: the opener rows stored before it (`boundary`, the last row admitted before
// it), in storage order, are the inputs Claude took in. Each is a turn of its own: each gets its start, and each but
// the last its end, finished (a later prompt turn is stored), at once after its start and before the next one's,
// naming its last row (its own, or one stored with it before the next opener). The last runs: it is current. Without
// one Claude continues the last turn. An opener noted only after its turn.start is never one: a later start would
// put it out of order. Returns the earlier turns' ends, each with its place taken, for index.ts to prove.
export function started(session: string | undefined, boundary: number): { end: Ending; place: TurnTransition | undefined }[] {
  go(compacting)  // a /compact whose compaction never came: its start goes, and the next start records its end
  compacting = undefined
  const taken = openings.filter((opening) => opening.seq <= boundary)
  openings = openings.filter((opening) => opening.seq > boundary)
  openedTo = Math.max(openedTo, boundary)
  return inputs(taken, () => session, boundary, true)
}

// The taken openers' turns, in storage order: each starts, and each but the last gets its end, finished, its place
// taken at once; the last runs (`runs`), or is left open for core to close (a /clear). `conversation`: each one's.
function inputs(taken: Opening[], conversation: (opening: Opening) => string | undefined, boundary: number,
  runs: boolean): { end: Ending; place: TurnTransition | undefined }[] {
  const ends: { end: Ending; place: TurnTransition | undefined }[] = []
  taken.forEach((opening, index) => {
    const { ref, seq: at, source: from, attempt, firstTask } = opening
    const session = conversation(opening)
    const next = taken[index + 1]?.seq ?? boundary + 1
    const owner: Owner = { id: ref, kind: 'prompt', rows: [{ id: ref, seq: at }], session, pending: 0, places: new Map() }
    for (const row of loose) if (row.seq > at && row.seq < next) insert(owner.rows, row)
    for (const [held, ticket] of unowned) {
      if (held <= at || held >= next) continue
      ticket.owner = owner  // still being stored: it lands in this turn's rows once noted, and its end waits for it
      owner.pending += 1
      ;(owner.inflight ??= new Set()).add(held)
      unowned.delete(held)
    }
    push(session, { type: 'started', turn: ref, kind: 'prompt', ref, source: from, ...(attempt ? { attempt } : {}), first_task: firstTask })
    if (index === taken.length - 1) {
      if (runs) current = owner
    } else {
      const end: Ending = { turn: ref, kind: 'prompt', state: 'finished', session, owner, ...(runs ? {} : { unread: true }) }
      ends.push({ end, place: reserve(end) })
    }
  })
  loose = loose.filter((row) => row.seq > boundary)
  for (const held of [...unowned.keys()]) if (held <= boundary) unowned.delete(held)
  return ends
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
// Returns the last row admitted so far: the boundary between this start's inputs and what comes after.
export function stepped(): number {
  if (current !== undefined) (current.steps ??= []).push(seq)
  return seq
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

// Whether an edge of this conversation is still to reach core: queued, or its place still being filled. An edge
// leaves the queue only once core has taken the report that carried it.
// A switch is leaving `conversation` (its SessionEnd): no row comes after its last, so a command's end that waits for
// the next row to say whether a report follows its output goes now, with none. Returns the local command of that
// conversation still open (started, its output row not noted yet), whose end may yet come, if any.
export function leaving(conversation: string): string | undefined {
  const decision = awaiting
  if (decision !== undefined && decision.session === conversation) {
    awaiting = undefined
    decision.decided = true
    decision.report = false
    poke(decision.owner)
  }
  return command !== undefined && command.session === conversation ? command.id : undefined
}

// The local command open now (its output row not noted yet), if any.
export function openCommand(): string | undefined {
  return command?.id
}

export function edgesFor(session: string): boolean {
  return turnTransitions.some((edge) => edge.session === session)
}

// The owner's last noted row, in storage order.
export function lastRow(owner: Owner): string | null {
  return owner.rows[owner.rows.length - 1]?.id ?? null
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
  if (owner.last !== undefined) consider(owner.last)
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
