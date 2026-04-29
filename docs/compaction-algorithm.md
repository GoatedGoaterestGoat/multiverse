# Frontier-Coherent Tree Compaction

A design for context compaction that replaces the standard "summarize the whole conversation into a paragraph" approach with a structured forest of summary trees, one per turn, where every step of construction yields a coherent compression of the input at its size.

## Motivation

Production agents (Claude Code, Codex, OpenCode) all do flat compaction: when context fills up, an LLM summarizes the entire conversation into a single blob that replaces the prior history. This has three problems:

1. **Pay full cost on every compaction.** A single summarize pass over a 200K-token history happens whenever the threshold trips.
2. **Lose structure permanently.** After compaction, you cannot recover the original turns or selectively re-expand a region.
3. **No notion of "compaction at compression ratio X".** You get whatever the model produces, not a budget-controlled output.

The proposal: build a structured representation of each turn once, in the background. At compaction time, pick a precomputed slice that fits the budget. Treat compaction as a structural operation, not an LLM-summarize operation.

## Core ideas

### 1. Forest of trees, one per turn

Each user-assistant turn (one user prompt plus the full assistant response including all tool calls, tool results, thinking blocks) becomes its own summary tree. Leaves are individual JSON entries from the trace; internal nodes are summaries of contiguous spans of children.

A whole session is then a forest, ordered by turn. Each turn's tree is built once and frozen when the turn ends.

### 2. Frontier-coherent bottom-up construction

A *frontier* is a list of nodes whose spans tile the turn exactly (no gaps, no overlaps, in order). Construction proceeds bottom-up: at each step, one contiguous span of the current frontier is collapsed into a single summary node. After N-1 such merges (for an N-leaf turn), the frontier is a single root node.

The construction property we want: **every intermediate frontier (after k merges, for k = 0, 1, ..., N-1) is itself a coherent compression of the entire turn at its size.** Not literally uniform — content-driven non-uniformity is good (a redundant tool-retry loop *should* compress harder than a decision moment) — but the variation across the frontier should reflect content density, not construction-order artifacts.

This is the headline construction invariant. As of 2026-04, no published method explicitly enforces or evaluates it. The closest analogs are Wu et al.'s recursive book summarization (level-by-level, but discards intermediate trees) and contiguity-constrained Ward's method (gives a frontier at every k, but greedy cost-based merging is known to skew).

### 3. Compaction = expansion from the root by node ID

Each `merge()` call assigns a sequential ID to the node it creates. The first merge produces node `0` (covers a small local span); the last merge produces the root (largest ID, covers the whole turn). Higher ID = later merge = more abstract / covers more leaves.

Compaction is computed by expanding from the root frontier:

1. Start with `frontier = [root]`.
2. To use more of the budget, expand: pick the highest-ID node currently in the frontier and replace it with its direct children.
3. Repeat until the next expansion would exceed the budget.

This walks the tree back through merges in reverse construction order. It is mathematically equivalent to picking the kth construction frontier, but does not require storing all N-1 frontiers — the tree alone is sufficient, and expansion is computed on demand.

No LLM call, no embeddings, no similarity scoring at compaction time. The only signal used during expansion is `node.id`. Compaction is a deterministic structural traversal of the tree.

### 4. Single-tool agentic merging

Construction is done by an LLM with one tool: `merge(start, end, summary)`. Each merge step spawns a fresh agent invocation with the current frontier rendered into the prompt. The agent picks one span, writes its summary, and exits. Loop until one node remains.

No scoring functions, no priority queues, no hand-tuned weights. The agent uses world knowledge to decide what is most-mergeable at each step, with explicit instructions to keep the resulting frontier balanced. Prompt caching makes the per-step prompt cost mostly free after the first invocation.

## Comparison with prior art

| Method | Granularity | Direction | Adjacency | Compaction primitive | Frontier coherence |
|---|---|---|---|---|---|
| Claude Code 5-layer | Whole session | Flat | n/a | LLM re-summarize, every time | n/a |
| Codex compaction | Whole session | Flat (encrypted blob) | n/a | LLM re-summarize | n/a |
| ChatIndex | Whole session | Online top-down | Ancestor-only | Retrieval (no compaction) | None — branches descend to varying depth |
| RAPTOR | Corpus | Bottom-up | **Broken** (semantic clusters) | Retrieval | Per-level, not per-merge |
| Wu et al. books | Corpus | Bottom-up | Yes | n/a (book summary only) | Per-level only; intermediate trees discarded |
| CAM (NeurIPS 2025) | Corpus | Bottom-up incremental | Semantic + positional | Retrieval | Per-layer, not per-cardinality |
| ACON (ICLR 2026) | Whole session | Flat (learned guideline) | n/a | LLM compress | n/a |
| Ward's contiguous | Sequence | Bottom-up | Yes | Cut at height | Frontier at every k, but skews |
| **This design** | **Per turn (forest)** | **Bottom-up agentic** | **Yes** | **Expand-from-root by node ID** | **Maintained as construction invariant** |

The combination — per-turn forest, structural-adjacency, agentic single-tool construction, frontier-coherence as a stated invariant, expand-from-root-by-merge-id as the compaction primitive — is unclaimed in the literature as of 2026-04.

## Algorithm

### Data model

```
Leaf {
  kind: "tool_call" | "tool_result" | "tool_exchange" | "thinking" | "text" | "user_msg"
  content: str            # raw or auto-merged content
  preview: str            # short rendering used in agent prompts
  tokens: int             # exact token count of `content`
}

Node {
  id: int                 # sequential merge id; root has the highest id in the tree
  span: (start, end)      # half-open over the original leaf array
  children: [Node | Leaf] # variable arity, ordered, contiguous
  summary: str            # text covering the span
  preview: str            # short rendering used in agent prompts
  tokens: int             # exact token count of `summary`
}

Frontier = [Node | Leaf]  # ordered, contiguous, tiles the turn exactly

Tree {
  turn_id: str
  leaves: [Leaf]
  root: Node              # highest-id node; covers the whole turn
}

Forest = [Tree]           # one per session, ordered by turn
```

### Phase 0: Preprocessing

Before agentic construction, apply two cheap transformations to reduce the leaf count and bound preview size.

**Auto-merge rules (config-driven).** Pair or collapse trivially-related entries:

```yaml
auto_merge_rules:
  - kinds: [tool_call, tool_result]
    action: pair
    label: tool_exchange
    preview_template: "{tool_name}({arg_summary}) -> {result_summary}"

  - kinds: [thinking, thinking]
    action: collapse_consecutive
    label: thinking_block

  - kinds: [text, text]
    action: collapse_consecutive
    label: text_block
    enabled: false
```

For `tool_exchange`: combine the call and the result into one leaf. The preview is a one-line outcome (`Read('/path/auth.py') -> 1200 lines, exports verify_token, refresh_token`) generated by template if structured, by Haiku if free-form.

**Pre-summarization for oversized leaves.** Any leaf whose `content` exceeds `preview_max_tokens` (default 200) gets a Haiku-generated preview. The full content is retained on the leaf for lossless fallback; only the preview is shown to the constructor agent.

```yaml
preprocessing:
  preview_max_tokens: 200
  pre_summarize_threshold: 500
  pre_summarize_model: haiku
```

### Phase 1: Construction loop

```python
def construct_tree(leaves, config):
    frontier = list(leaves)
    next_id = 0

    while len(frontier) > 1:
        prompt = render_constructor_prompt(frontier, config)
        merge_call = call_constructor(prompt)  # returns (start, end, summary)
        new_node = make_node(
            id=next_id,
            children=frontier[merge_call.start:merge_call.end],
            summary=merge_call.summary,
        )
        next_id += 1
        frontier = (
            frontier[:merge_call.start]
            + [new_node]
            + frontier[merge_call.end:]
        )

    return Tree(leaves=leaves, root=frontier[0])
```

Node IDs are assigned sequentially as merges happen. The root has the largest ID. By construction, a parent's ID is greater than any of its children's IDs.

**Constructor prompt sketch:**

```
You are constructing a summary tree of an agent trace, bottom-up.
Each call to merge() collapses one contiguous span of the current
frontier into a single summary node.

Tool:
  merge(start: int, end: int, summary: str)
    - start, end: half-open range over the frontier indices below
    - summary: the text that will replace the merged span

Current frontier ({len} entries):
[0] {kind} | tokens={t} | preview="{preview}"
[1] ...
...

Decision rule for the next merge:
  Pick the contiguous span such that the resulting frontier remains
  the most coherent possible summary of the entire trace at its new
  compression level.

Concretely:
  - Prefer spans that form a natural unit (a tool call and its
    result; a retry sequence; one attempt at one sub-task).
  - Prefer spans where the content is redundant or low-density.
  - Avoid making the frontier lopsided: do not keep collapsing one
    region while the rest of the frontier is still raw.
  - The summary should read as a natural continuation of the trace
    at its compression level. Preserve identifiers, file paths,
    error messages, and decisions verbatim where possible.

Call merge() exactly once.
```

**Caching properties.** The system prompt and tool schema are constant across merge steps. The frontier representation changes only where the previous merge happened — everything before the merged region is byte-identical from step to step. Anthropic prompt caching captures most of the prefix on every step after the first.

### Phase 2: Read path

Two cleanly-separated layers:

1. **Expansion** — a fixed per-tree primitive. Always priority-by-node-ID. No knobs.
2. **Compaction** — an across-tree budget allocator (the *scheduler*). Pluggable. Decides how the global token budget splits across the forest's turns. Composable.

Compaction calls expansion once per turn with that turn's allocated budget.

#### Expansion (per tree, fixed)

```python
def expand_to_budget(root, budget):
    """Expand a single tree's frontier from [root] until the next
    expansion would exceed budget. Priority is always node.id."""
    frontier = [root]
    while True:
        candidates = [n for n in frontier if isinstance(n, Node)
                                          and n.children]
        if not candidates:
            return frontier
        target = max(candidates, key=lambda n: n.id)

        expanded = []
        for n in frontier:
            expanded.extend(n.children if n is target else [n])

        if total_tokens(expanded) > budget:
            return frontier
        frontier = expanded
```

This is the unchangeable retrieval primitive. Highest-ID node first means undoing the most recent merge first; the result is structurally equivalent to the kth construction frontier without storing all frontiers.

#### Compaction (across trees, pluggable scheduler)

A *scheduler* is a function from `(forest, total_budget)` to a per-turn budget mapping:

```python
Scheduler = Callable[[Forest, int], dict[turn_id, int]]

def compact(forest, total_budget, scheduler):
    per_turn_budget = scheduler(forest, total_budget)
    chosen = []
    for t in forest:
        frontier = expand_to_budget(t.root, per_turn_budget[t.turn_id])
        chosen.extend(frontier)
    return render_as_message_stream(chosen)
```

Concrete schedulers shipped with the implementation:

```python
def uniform(forest, budget):
    """Equal split across all turns."""
    n = len(forest)
    per = budget // n
    return {t.turn_id: per for t in forest}


def recency_decay(forest, budget, half_life=5):
    """Exponential decay: most-recent turn gets the most budget."""
    weights = {}
    for i, t in enumerate(reversed(forest)):
        weights[t.turn_id] = 0.5 ** (i / half_life)
    total_w = sum(weights.values())
    return {tid: int(budget * w / total_w) for tid, w in weights.items()}


def proportional(forest, budget):
    """Budget proportional to original turn size."""
    sizes = {t.turn_id: t.original_token_count for t in forest}
    total = sum(sizes.values())
    return {tid: int(budget * s / total) for tid, s in sizes.items()}


def last_k_raw(forest, budget, k=3):
    """Last k turns get unlimited budget (rendered raw); the rest
    share the remainder uniformly. Falls back to uniform if k recent
    turns alone exceed the budget."""
    last = forest[-k:]
    rest = forest[:-k]
    raw_cost = sum(t.full_token_count for t in last)
    if raw_cost >= budget or not rest:
        return uniform(forest, budget)
    per_rest = (budget - raw_cost) // len(rest)
    return {
        **{t.turn_id: t.full_token_count for t in last},
        **{t.turn_id: per_rest for t in rest},
    }


def cap(scheduler, max_per_turn):
    """Cap any scheduler's per-turn allocations and redistribute the
    overflow to remaining turns."""
    def wrapped(forest, budget):
        alloc = scheduler(forest, budget)
        overflow = sum(max(0, v - max_per_turn) for v in alloc.values())
        capped = {tid: min(v, max_per_turn) for tid, v in alloc.items()}
        # redistribute overflow uniformly among non-capped turns
        targets = [tid for tid, v in alloc.items() if v <= max_per_turn]
        if targets and overflow:
            bonus = overflow // len(targets)
            for tid in targets:
                capped[tid] += bonus
        return capped
    return wrapped
```

Schedulers are pure data → data; they do not call the model and do not see the tree structure beyond turn-level metadata. New schedulers (e.g., flag-the-failed-tool-result turns, weight by user emphasis, learned schedulers) plug in by implementing the same signature.

The default in production is `recency_decay(half_life=5)`. For benchmarks against flat-summary baselines, `last_k_raw(k=3)` matches Claude Code's de-facto behavior of keeping recent turns verbatim and is a useful apples-to-apples baseline.

### Phase 3: Integration with Claude Code

Construction runs in the background, triggered by the `Stop` hook (assistant finishes responding). By the time the user submits the next prompt, the previous turn's tree is usually complete. If construction has not finished by the next compaction trigger, the in-flight turn is treated as a single unsummarized leaf in the compacted output.

`/compact` becomes a frontier-checkpoint pick. `/compact <focus>` adds the LLM-rank step with the focus as the query.

## Configuration

```yaml
compaction:
  preprocessing:
    preview_max_tokens: 200
    pre_summarize_threshold: 500
    pre_summarize_model: haiku-4.5

    auto_merge_rules:
      - kinds: [tool_call, tool_result]
        action: pair
        label: tool_exchange
      - kinds: [thinking, thinking]
        action: collapse_consecutive
        label: thinking_block

  construction:
    constructor_model: sonnet-4.6
    max_merges_per_turn: 3 * leaf_count   # safety cap
    background_trigger: stop_hook

  compaction:
    default_budget_tokens: 32000
    scheduler: recency_decay              # name of a registered Scheduler
    scheduler_args:
      half_life: 5
    # alternative examples:
    # scheduler: uniform
    # scheduler: last_k_raw
    # scheduler_args: {k: 3}
    # scheduler: cap(recency_decay, max_per_turn=8000)
```

## Cost estimate

For a 50-entry tool-heavy turn:

- Auto-merge tool_call+result pairs: 50 -> ~25 leaves.
- Pre-summarize ~5 oversized leaves: 5 Haiku calls.
- Construction: 24 merges, 24 fresh agent invocations with prompt caching.
- Total per turn: ~$0.05-0.15 with Sonnet 4.6 + Haiku 4.5 at current pricing.

Compaction itself is free — no LLM call, just a tree traversal.

For comparison, Claude Code's flat compaction runs whenever the threshold trips and re-summarizes the entire context each time at full cost. The amortized cost crossover point is approximately the second compaction in a session.

## Evaluation plan

### Primary benchmarks

1. **Factory.ai compaction-fidelity benchmark** (Feb 2026, 36k-message conversations). Headline metric. Sweeps the full frontier sequence at compression ratios {50%, 75%, 90%}.
2. **ACON's AppWorld + OfficeBench** (ICLR 2026). Agentic task success at fixed peak-token budget. ACON reports 26-54% peak-token reduction; the bar to beat at the same task accuracy.

### Baselines

1. Claude Code 5-layer flat (production bar).
2. ACON (strongest published compression).
3. CAM (NeurIPS 2025) flattened at a chosen layer.
4. ChatIndex with depth-cutoff compaction.
5. RAPTOR-with-adjacency (force contiguous clusters).
6. Wu et al. fixed-fanout balanced.
7. Last-K-turns + summary-of-rest (the embarrassingly-simple baseline).

### Metrics

- **Compaction fidelity** (Factory.ai score) at compression ratios {50, 75, 90}%.
- **Task success rate** on AppWorld and OfficeBench at fixed peak-token budgets.
- **Identifier-preservation rate** (regex-extracted file paths, function names, error codes preserved post-compaction).
- **Construction cost** ($/turn at p50, p95).
- **Compaction latency** (ms; should be sub-100ms in the no-query path).
- **Frontier-coherence eval** (custom): held-out fact probes — for each fact in a held-out set, at which compression ratio does the frontier stop preserving it? Smooth degradation curves are the goal.

### Ablations

- Force binary arity (variable-arity vs binary).
- Disable auto-merge preprocessing.
- Disable pre-summarization of oversized leaves.
- Replace constructor with single-pass full-turn summarizer (tests whether the per-merge agent loop matters).
- Uniform per-turn budget vs LLM-rank (tests the rank step).
- Single global tree vs per-turn forest.
- Haiku as constructor (cost-quality tradeoff).

### Implementation order

1. **Preprocessing module.** Auto-merge rule engine + pre-summarize for oversized leaves. Hand-eval on 5 real session logs.
2. **Constructor agent + loop.** Single `merge()` tool, frontier renderer, fresh-agent-per-step loop. Inspect every intermediate frontier on the same 5 logs by hand. Look for chaining/skewing. If skewed, harden the prompt's anti-lopsidedness instruction.
3. **Compaction primitive.** Expand-from-root by highest node ID until budget. No LLM, no embeddings.
4. **Factory.ai eval.** Decision point: must beat Claude Code 5-layer flat at 75% compression, or pause and diagnose.
5. **ACON's AppWorld + OfficeBench.** Full baseline matrix.
6. **Query-aware expansion + LLM-rank-turns.** Add the agentic compaction layer.
7. **Ablations.**
8. **Claude Code integration.** Stop-hook background construction; replace `/compact` with frontier-pick. Production hardening.

### Decision points / when to abandon

- After step 2: if hand-eval shows consistent chaining or summary drift, the prompt is wrong. Iterate or abandon agentic-merge in favor of ReadAgent-style upfront span proposing.
- After step 4: if no win over flat compaction on Factory.ai at 75%, the design does not work. Abandon or pivot.
- After step 5: if construction cost is >$1/turn at p95 and quality lift is <10% over flat, not shippable.

## Open questions

1. **Anti-lopsidedness in the constructor prompt.** Whether the natural-language instruction to "distribute merges across the trace" is enough, or whether we need a structural counter (e.g., feed the model a "current frontier balance" stat). Resolved empirically in step 2.
2. **Cross-turn anchors at compaction.** When turn N references something introduced in turn M, should turn M's frontier be expanded further around the relevant subtree? Probably handled by the query-aware priority function (relevance × size), but worth measuring against a stricter rule.
3. **Tool-result chunking.** A 50KB file read should not be one leaf; how to split. Probably split by deterministic boundaries (line ranges of N tokens) before pre-summarization runs.
4. **Multi-leaf preview rendering at compaction time.** After picking a frontier, the rendering back into the agent's prompt needs a format choice — flat reconstruction with `[summary of N entries]` markers, or structured XML with explicit compression levels. Default: flat for v1, structured later.

## References

- Wu et al., *Recursively Summarizing Books with Human Feedback*, arXiv:2109.10862, 2021.
- Sarthi et al., *RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval*, arXiv:2401.18059, ICLR 2024.
- Lee et al., *A Human-Inspired Reading Agent for Long-Document Comprehension*, arXiv:2402.09727, 2024.
- Edge et al., *From Local to Global: A GraphRAG Approach to Query-Focused Summarization*, arXiv:2404.16130, 2024.
- Packer et al., *MemGPT: Towards LLMs as Operating Systems*, arXiv:2310.08560, 2023.
- Rezazadeh et al., *MemTree*, arXiv:2410.14052, 2024.
- *CAM: A Constructivist View of Agentic Memory*, arXiv:2510.05520, NeurIPS 2025.
- *ACON: Optimizing Context Compression for Long-Horizon LLM Agents*, ICLR 2026.
- *MemAgent*, ICLR 2026.
- *SimpleMem*, ICLR 2026.
- VectifyAI, *ChatIndex: Tree Indexing for Long Conversations*, github.com/VectifyAI/ChatIndex, 2025.
- Factory.ai, *Evaluating Compression*, 2026.
- Anthropic, *Effective Context Engineering for AI Agents*, 2025.
