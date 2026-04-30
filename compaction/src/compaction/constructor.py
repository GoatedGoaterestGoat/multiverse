"""Agentic tree construction.

For each turn, the constructor builds a summary tree by repeatedly calling
a small LLM agent with a single `merge` tool. Each call:

  1. Renders the current frontier as an indexed listing.
  2. Sends a fresh, stateless request to Sonnet with the `merge` tool.
  3. Reads back the merge call (start, end, summary).
  4. Applies the merge: collapses frontier[start:end] into a new Node with
     the next sequential id and the model-written summary.

The loop terminates when the frontier has one entry (the root).

Stateless: each iteration is a fresh API call. Anthropic prompt caching
captures the constant prefix (system prompt, tool def) after the first
iteration; the frontier representation changes only where the previous
merge happened.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from .render import render_frontier_for_constructor
from .tokenize import count_tokens
from .types import Forest, Frontier, Leaf, Node, Turn

# The default Sonnet ID used for construction. Override via config.
DEFAULT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are constructing a summary tree of an agent trace, bottom-up.

You are called repeatedly; each call sees the current frontier and you \
make exactly ONE merge by calling the `merge` tool once.

A merge collapses one contiguous span of frontier entries [start, end) \
(half-open over the indices shown) into a single summary node. The \
resulting frontier becomes the input to the next call.

Decision rule for the next merge:
  Pick the contiguous span such that the resulting frontier remains the \
  most coherent possible summary of the entire trace at its new \
  compression level.

Concretely:
  - Prefer spans that form a natural unit: a tool exchange and its \
    immediate follow-up; a retry sequence; one attempt at one sub-task.
  - Prefer spans where content is redundant or low-density.
  - Avoid making the frontier lopsided: do not keep collapsing one region \
    while the rest of the frontier is still raw. Distribute merges across \
    the trace so every part is summarized at roughly the same level of \
    detail (with denser content allowed to compress harder).
  - Your summary should read as a natural continuation of the trace at \
    its compression level: factual, present-tense, no preamble. Preserve \
    file paths, function/identifier names, exit codes, errors, and \
    user/agent decisions verbatim where possible.

Call `merge` exactly once. Do not output any text outside the tool call.
"""

MERGE_TOOL = {
    "name": "merge",
    "description": (
        "Collapse a contiguous span of the current frontier into a single "
        "summary node. Indices are half-open: [start, end)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start": {
                "type": "integer",
                "description": "Inclusive start index in the frontier.",
            },
            "end": {
                "type": "integer",
                "description": (
                    "Exclusive end index. Must satisfy end > start, "
                    "and end - start >= 2 (a merge must collapse at "
                    "least two adjacent entries)."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Summary text covering the merged span. Will replace "
                    "the merged entries in the next frontier."
                ),
            },
        },
        "required": ["start", "end", "summary"],
    },
}


# ---------------------------------------------------------------------------
# Configuration & client
# ---------------------------------------------------------------------------

@dataclass
class ConstructorConfig:
    model: str = DEFAULT_MODEL
    max_tokens_per_call: int = 1024
    safety_cap_multiplier: float = 3.0
    max_retries_per_step: int = 2
    api_key: str | None = None  # falls back to ANTHROPIC_API_KEY env
    capture_trace: bool = False  # if True, fill ConstructionTrace on each turn


@dataclass
class ConstructionTrace:
    """Per-turn diagnostic record. Useful for the by-hand inspection step."""
    turn_id: str
    initial_leaf_count: int
    merges: list[dict] = field(default_factory=list)
    aborted_reason: str | None = None
    finished: bool = False


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def construct_forest(
    forest: Forest,
    config: ConstructorConfig | None = None,
    progress: Callable[[int, int, Turn], None] | None = None,
) -> list[ConstructionTrace]:
    """Build a tree on every Turn in the forest. Mutates each Turn.root.

    Returns one ConstructionTrace per turn (only populated when
    config.capture_trace is True; otherwise minimal records).
    """
    cfg = config or ConstructorConfig()
    client = _make_client(cfg.api_key)
    traces: list[ConstructionTrace] = []
    for i, turn in enumerate(forest):
        if progress is not None:
            progress(i, len(forest), turn)
        trace = construct_turn(turn, client, cfg)
        traces.append(trace)
    return traces


def construct_turn(turn: Turn, client, cfg: ConstructorConfig) -> ConstructionTrace:
    """Build a tree on a single turn. Mutates `turn.root`."""
    trace = ConstructionTrace(
        turn_id=turn.turn_id, initial_leaf_count=len(turn.leaves)
    )
    if not turn.leaves:
        trace.aborted_reason = "empty turn"
        return trace
    if len(turn.leaves) == 1:
        # Single-leaf turn: the leaf IS the root.
        turn.root = turn.leaves[0]
        trace.finished = True
        return trace

    frontier: Frontier = list(turn.leaves)
    next_id = 0
    safety_cap = int(cfg.safety_cap_multiplier * len(turn.leaves))

    for step in range(safety_cap):
        if len(frontier) == 1:
            break
        try:
            start, end, summary = _request_merge(client, frontier, cfg)
        except _MergeRequestError as e:
            trace.aborted_reason = f"step {step}: {e}"
            break

        if not _valid_span(start, end, len(frontier)):
            trace.aborted_reason = (
                f"step {step}: invalid span ({start}, {end}) for "
                f"frontier of size {len(frontier)}"
            )
            break

        merged_children = list(frontier[start:end])
        new_node = Node(
            id=next_id,
            children=merged_children,
            summary=summary,
            tokens=count_tokens(summary),
            preview=_summary_preview(summary),
        )
        next_id += 1

        frontier = list(frontier[:start]) + [new_node] + list(frontier[end:])

        if cfg.capture_trace:
            trace.merges.append({
                "step": step,
                "start": start,
                "end": end,
                "summary": summary,
                "new_node_id": new_node.id,
                "new_frontier_size": len(frontier),
            })

    if len(frontier) == 1:
        turn.root = frontier[0]
        trace.finished = True
    else:
        # Hit the safety cap; root is a synthetic node wrapping the leftover
        # frontier so downstream code can still expand from a single root.
        turn.root = Node(
            id=next_id,
            children=list(frontier),
            summary="<construction aborted; uncollapsed frontier>",
            tokens=sum(n.tokens for n in frontier),
            preview="<aborted>",
        )
        trace.aborted_reason = trace.aborted_reason or "hit safety cap"

    return trace


# ---------------------------------------------------------------------------
# API plumbing
# ---------------------------------------------------------------------------

def _make_client(api_key: str | None):
    """Lazily import anthropic so the package can be parsed without it."""
    import anthropic
    return anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))


class _MergeRequestError(RuntimeError):
    pass


def _request_merge(client, frontier: Frontier, cfg: ConstructorConfig) -> tuple[int, int, str]:
    """Issue one merge request to the model. Returns (start, end, summary).

    Retries up to cfg.max_retries_per_step on transient API failures; raises
    _MergeRequestError if the model never returns a usable tool call.
    """
    user_msg = render_frontier_for_constructor(frontier)

    last_err: Exception | None = None
    for attempt in range(cfg.max_retries_per_step + 1):
        try:
            resp = client.messages.create(
                model=cfg.model,
                max_tokens=cfg.max_tokens_per_call,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[MERGE_TOOL],
                tool_choice={"type": "tool", "name": "merge"},
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as e:  # network / rate limit / model error
            last_err = e
            continue

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "merge":
                inp: Any = block.input
                try:
                    start = int(inp["start"])
                    end = int(inp["end"])
                    summary = str(inp["summary"])
                except (KeyError, TypeError, ValueError) as e:
                    last_err = e
                    continue
                return start, end, summary

        last_err = _MergeRequestError("no merge tool call in response")
    raise _MergeRequestError(str(last_err) if last_err else "unknown")


def _valid_span(start: int, end: int, n: int) -> bool:
    return 0 <= start < end <= n and (end - start) >= 2


def _summary_preview(summary: str, max_len: int = 120) -> str:
    flat = " ".join(summary.split())
    return flat if len(flat) <= max_len else flat[: max_len - 3] + "..."
