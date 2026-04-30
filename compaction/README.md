# compaction

Frontier-coherent tree compaction for Claude Code session logs.

See [`docs/compaction-algorithm.md`](../docs/compaction-algorithm.md) for the design.

## Pipeline

```
JSONL session log
    -> parse_session       (parser.py)        -> Forest of Turns
    -> preprocess_forest   (preprocess.py)    -> auto-merged tool pairs
    -> construct_forest    (constructor.py)   -> Tree per Turn  [calls Anthropic API]
    -> compact             (compactor.py)     -> chosen frontier per Turn
    -> render_messages     (render.py)        -> compacted text
```

Each stage is independently runnable and inspectable.

## CLI

```bash
# Dry run (no API; uses a deterministic stub merger)
python -m compaction.cli SESSION.jsonl --budget 32000 --dry-run --stats

# With a real constructor (requires ANTHROPIC_API_KEY)
python -m compaction.cli SESSION.jsonl --budget 32000 --scheduler recency_decay \
    --out compacted.txt
```

Schedulers: `uniform`, `recency_decay` (default), `proportional`, `last_k_raw`.

## Layout

```
compaction/
    pyproject.toml
    src/compaction/
        types.py         # Leaf, Node, Tree, Turn, Forest
        tokenize.py      # token counting (tiktoken)
        parser.py        # JSONL -> Forest
        preprocess.py    # auto-merge tool_use+tool_result pairs
        render.py        # frontier <-> text
        constructor.py   # agentic merge loop (Anthropic API)
        compactor.py     # expand_to_budget + schedulers
        cli.py           # end-to-end entrypoint
```

## Status

- [x] Parser
- [x] Preprocess (auto-merge)
- [x] Compactor + four schedulers
- [x] Render
- [x] Constructor (Anthropic API)
- [x] CLI with --dry-run
- [ ] Pre-summarize oversized leaves (Haiku)
- [ ] Tests
- [ ] Eval harness against Factory.ai compaction-fidelity benchmark
