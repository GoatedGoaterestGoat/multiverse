# Compaction: what everyone else does, what ours costs, and what to do next

A companion to [`compaction-algorithm.md`](compaction-algorithm.md), which
describes the design. This one is about the *economics* — prompted by a
concrete complaint: goat compaction is good, but it burns tokens making one
independent LLM call per merge.

That complaint is correct, and it is smaller than it feels. The measured
number is 3.6x, not 30x, because variable-arity merges mean a turn needs far
fewer merges than it has leaves. The bigger finding is that most of the 3.6x
is **cache breakage, not model calls** — and it is fixable without touching
the algorithm.

---

## 1. What production harnesses do

All four ship flat compaction — replace history with prose — and differ in
what they do *before* reaching for the model.

| Harness | Trigger | Before summarizing | Summarize | Recoverable? |
|---|---|---|---|---|
| **Claude Code** | 3 tiers: microcompaction, auto at limit, `/compact` | Microcompaction offloads bulky tool results early | 1 LLM call, structured multi-section summary; `/compact <focus>` steers it | No — summary replaces history |
| **Codex CLI** | `context_window - min(max_output, 20k) - 13k` (~167k on a 200k model) | Checks whether structured **session memory** (task state, edit history, decisions) already suffices | Only if it doesn't: `POST /v1/responses/compact` returns an AES-encrypted blob the server re-expands | Server-side only; opaque to you |
| **OpenCode** | Model calls a `Compress` tool at task boundaries | **Prunes** stale tool outputs first, but only if that frees >20k; last 40k tokens and skill outputs are protected | Only after pruning fails; overlapping compressions nest, preserving layers | Partially — nesting keeps layers |
| **pi** | `contextTokens > contextWindow - reserveTokens` (16k default), or `/compact` | Nothing; serializes and truncates tool results to 2k chars | 1 LLM call, structured summary; `keepRecentTokens` (20k) stays verbatim | Yes — the compaction entry is appended, original entries stay in the file |

Two patterns are worth stealing outright:

- **Codex and OpenCode both try to avoid the LLM call entirely.** Codex asks
  whether structured state already covers it; OpenCode prunes and only
  summarizes if pruning was not enough. Goat currently always builds trees.
- **pi's compaction is non-destructive by construction.** The compaction entry
  is one node in an append-only tree; the entries it replaces are still there.
  Claude Code needs multiverse's whole two-JSONL mirror to get the same
  property. (This is why the pi harness has no mirror — see
  `vendor/compaction/pi_render.py`.)

## 2. What the literature does

Extending the prior-art table in `compaction-algorithm.md` with what has
landed since:

| Work | Idea | LLM calls at compaction | Relevance to us |
|---|---|---|---|
| **ARC** — Addressable Recall Compaction (arXiv:2607.25066) | Append-only observation store; oversized tool results become content-addressed **citation stubs** with head/tail previews; agent dereferences with `_recall §id` | **Zero.** Deterministic; no model invocation | Directly implements the `recall(message_id)` tool in Multiverse's own README. Lossless, and the cheapest tier available |
| **Structured Context Eviction** (arXiv:2606.11213) | Heuristic eviction of low-value spans instead of uniform compression | Zero | The "cut N before you spend anything" tier |
| **Parallel Context Compaction** (arXiv:2605.23296) | Compress independent segments concurrently and merge | Same total, far lower wall clock | Our per-turn forest is already segment-parallel; we just run it serially |
| **ACON** (ICLR 2026) | Learned compression guideline, flat | 1 | Still the strongest published flat baseline to beat |
| **CAM** (NeurIPS 2025), **RAPTOR**, **Wu et al.** | Bottom-up trees for *retrieval* | n/a | Closest structural analogues; none maintain a per-merge frontier |

The gap in all of them is the same one `compaction-algorithm.md` claims:
nobody treats "a coherent compression at every cardinality" as a construction
invariant. That claim survives. What does not survive is the implicit
assumption that per-merge agentic construction has to be expensive.

## 3. What goat construction actually costs

Measured over the 915 cached trees on this machine that carry a merge
history, replaying each recorded merge sequence to reconstruct the frontier at
every step, and pricing in Anthropic billed input units (uncached 1.0, cache
write 1.25, cache read 0.1):

```
scripts/analyze-construction-cost.py
```

```
ALL          n=915  tok/turn=  6885  calls=  4.4 | billed-in/turn: current=  25017
N in [0,10)  n=485  tok/turn=  1986  calls=  2.4 | billed-in/turn: current=   5761
N in [10,25) n=277  tok/turn=  7164  calls=  5.5 | billed-in/turn: current=  23944
N in [25,60) n=126  tok/turn= 15891  calls=  8.1 | billed-in/turn: current=  55122
N >= 60      n= 27  tok/turn= 49979  calls= 12.3 | billed-in/turn: current= 241429

aggregate: 6.30M transcript tokens -> 22.89M billed input units (3.63x)
```

Three things fall out of this that are worth having in mind before optimizing:

**The call count is not the problem.** A 16-leaf turn averages 4.4 merges, not
15. The constructor picks variable-arity spans (mean arity ~3, and spans of 8+
are common), so merges land at roughly `0.54 * (N-1)`. "One call per merge"
sounds like O(N) calls and is closer to O(N/3).

**Small turns are already cheap and not worth touching.** Half the turns have
fewer than 10 leaves and cost ~5.8k input units to build — noise next to the
turn's own inference. Optimization effort belongs entirely in the tail.

**The tail is where it hurts.** The largest 3% of turns (>=60 leaves) cost
241k input units each to build — roughly 5x the turn's own token count, per
turn, and the auto-build worker runs on every turn of every enabled session.

## 4. Where the cost goes

Not to the model. To cache invalidation.

`construct_turn_priv_cli` writes a **fresh synthetic session under a new UUID
on every merge step**, rebuilt from the *current* frontier. So step *k*'s
assistant block differs from step *k-1*'s starting at the previous merge site.
Everything from there on is a fresh cache write at 1.25x, every step.

The asymmetry that makes this wasteful: the frontier *index* — one preview
line per entry — is small, a few hundred tokens. The turn *content* is the
expensive part, and it is what changes position in the prompt. We are paying
to re-write kilobytes of unchanged transcript in order to move a handful of
index lines.

## 5. Four cheaper designs

### A. Append-mode construction — implemented

Keep one session per turn and *append* to it. Step 0 writes the transcript
once; every later step adds a short instruction plus the previous merge's JSON
reply. The prefix is byte-identical from step to step, so it is a cache read
at 0.1x instead of a write at 1.25x.

| | ALL | N >= 60 |
|---|---|---|
| current | 25,017 | 241,429 |
| append | 17,503 (**1.4x cheaper**) | 148,866 (**1.6x cheaper**) |

No change to the algorithm, the tree, or the frontier invariant — only to
where the bytes sit in the prompt. It also *improves* the input: the model can
see which spans it already collapsed and what it wrote for them, instead of
re-deriving that from summary previews.

Shipped for pi in `vendor/compaction/pi_constructor.py`. The Claude Code
constructor still rebuilds per step; porting it is the obvious next commit and
needs no design work.

### B. Level-batched merges — designed, not built

Ask for a whole *level* per call: partition the current frontier into
contiguous groups and write every group's summary in one reply. Round trips
drop from O(merges) to O(log_arity N).

| | ALL | N >= 60 |
|---|---|---|
| current | 25,017 · 4.4 calls | 241,429 · 12.3 calls |
| level | 12,789 (**2.0x cheaper**) · 2.2 calls | 89,789 (**2.7x cheaper**) · 2.8 calls (**4.4x fewer**) |

The obvious objection is that this destroys frontier coherence: you only get
frontiers at level boundaries, not at every cardinality.

It does not, and the reason is worth stating because it is not obvious. **Node
ids are a free variable.** In the current design ids come from construction
order because construction is sequential. But the *tree* does not depend on
the order groups within a level are collapsed — only the *ids* do, and
`node.id` is `expand_to_budget`'s only ordering signal. So build the tree level-wise, then
assign ids in a separate pass chosen to maximize frontier balance: at each
step, pick the not-yet-collapsed group whose collapse leaves the frontier most
uniform in token density.

That is strictly more control over the invariant than we have now, where
balance depends on a natural-language instruction not to be lopsided.
Open question #1 in `compaction-algorithm.md` — whether that instruction is
enough — stops being a prompt-engineering problem and becomes a sorting
problem.

Latency matters as much as tokens here: each merge is a CLI process spawn plus
a model round trip, so a 60-leaf turn is a dozen serial spawns. 4.4x fewer
round trips is the difference between a background worker that keeps up with
the session and one that does not.

### C. Prune before you build — not built

Both Codex and OpenCode ask "can we avoid the model entirely" first. Goat asks
"how do I summarize this" first. Cheap wins available before any call:

- **Revisit the auto-merge preprocessing default.** `preprocess.py` can pair
  tool_call+tool_result into one `tool_exchange` leaf, and is **off by
  default** — deliberately: the comment in `PreprocessConfig` says the
  constructor should see the true call/result structure and decide for
  itself. That is a real quality argument, and it has never been weighed
  against its price. Pairing alone cuts N by up to half on tool-heavy turns,
  and cost scales with N. Worth an A/B on the frontier-coherence eval rather
  than an assumption in either direction.
- **Evict before summarizing.** A tool result that was superseded (a file read
  three edits ago, a failed command immediately retried) does not need a
  summary; it needs a one-line stub. OpenCode's rule — prune only when it
  frees more than a threshold, never touch the recent window — is a good
  default.
- **Skip construction for turns that will never be compressed.** A 2k-token
  turn under any realistic budget expands to raw anyway. Building a tree for
  it is pure waste, and half of all turns are that small. A size floor below
  which we do not build is a two-line change with a large aggregate effect.

### D. Addressable recall as the bottom tier — not built

ARC's result is the strongest single finding in the recent literature for our
purposes: replace oversized tool results with content-addressed citation
stubs, keep the full bytes in an append-only store, give the agent a `recall`
tool. **Zero LLM calls, zero information loss**, and it reports beating
LLM-summary baselines on long-context reasoning.

This is not a competitor to the summary tree; it is the tier below it. Tool
results are where the tokens are and where summarization loses the most
(exact paths, error strings, line numbers). Stub them addressably; spend the
tree on reasoning and decisions, which is what it is good at.

It is also already in Multiverse's own roadmap — the top-level README
specifies a `recall(message_id)` tool. ARC is that design, with an evaluation
attached.

## 6. Recommended order

1. **Append-mode for the Claude Code constructor.** Done for pi; 1.4-1.6x, no
   design risk, no quality change.
2. **A build-size floor and auto-merge preprocessing on by default.** Removes
   work rather than making it cheaper. Largest aggregate effect per line
   changed.
3. **Addressable recall for oversized tool results.** Zero marginal cost,
   lossless, and it makes every later tier cheaper by shrinking what they see.
4. **Level-batched construction with post-hoc id assignment.** The real
   redesign: 2-2.7x cheaper, 4.4x fewer round trips, and it turns frontier
   balance into something we control rather than something we ask for.
5. **Parallel construction across turns.** The forest is embarrassingly
   parallel and we run it serially. Pure wall-clock, no token change.

Steps 1-3 are cost reductions with no effect on output. Step 4 changes the
tree and needs the frontier-coherence eval from `compaction-algorithm.md`
before it lands.

## 7. Reproducing the numbers

```bash
scripts/analyze-construction-cost.py                  # local tree cache
scripts/analyze-construction-cost.py --trees <dir>    # another cache
```

The script replays each cached tree's recorded merge history to reconstruct
the frontier at every step, then prices the three prompt layouts. It skips
trees with no recorded history (built before history capture, or single-leaf
turns) and says how many it skipped. Constants for prompt boilerplate and
cache multipliers are at the top of the file.

## 8. References

- Addressable Recall Compaction, arXiv:2607.25066
- Beyond Compaction: Structured Context Eviction for Long-Horizon Agents, arXiv:2606.11213
- Parallel Context Compaction for Long-Horizon LLM Agent Serving, arXiv:2605.23296
- Self-Compacting Language Model Agents, arXiv:2606.23525
- ACON: Optimizing Context Compression for Long-Horizon LLM Agents, ICLR 2026
- CAM: A Constructivist View of Agentic Memory, arXiv:2510.05520, NeurIPS 2025
- RAPTOR, arXiv:2401.18059; Wu et al., arXiv:2109.10862
- Daniel Vaughan, *Context Compaction Deep Dive: Codex CLI, Claude Code, OpenCode*, 2026-04
- pi, *Compaction & Branch Summarization* (`docs/compaction.md` in `@earendil-works/pi-coding-agent`)
- Anthropic, *Prompt caching* — cache write 1.25x (5m) / 2x (1h), cache read 0.1x
