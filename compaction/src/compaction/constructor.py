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
import re
import subprocess
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .priv_session import (
    build_synthetic_jsonl_from_frontier,
    build_synthetic_jsonl_from_source,
    cwd_slug,
    session_path,
)
from .render import render_frontier_as_message_blocks, render_frontier_for_constructor
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

Point of view (IMPORTANT):
  The trace you are summarizing is YOUR OWN past work as the agent. \
  In the entries you see, "I"/"me"/"my" refers to you (the agent), and \
  "you" refers to the user. Write summaries in the FIRST PERSON from \
  the agent's perspective: say "I read the file", "I tried X then Y", \
  NOT "the assistant read the file" or "Claude tried...". When the \
  trace contains a user message, refer to that speaker as "the user" \
  or "you" (paraphrasing what the user said TO you).

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
    its compression level: factual, present-tense, FIRST-PERSON, no \
    preamble. Preserve file paths, function/identifier names, exit codes, \
    errors, and user/agent decisions verbatim where possible.

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


# ---------------------------------------------------------------------------
# Privileged constructor: model receives the actual conversation
# ---------------------------------------------------------------------------
#
# The text-mode constructor above sees only previews of leaves — for thinking
# leaves it sees `<thinking: encrypted>` and nothing else. The privileged
# variant sends the full conversation as native `messages` so the API
# validates thinking signatures and decrypts server-side, giving the model
# server-side access to the original chain-of-thought when it makes merge
# decisions.
#
# Requirements:
#  - Same Anthropic credential as the one that produced the thinking
#    signatures (otherwise the API 400s on signature validation).
#  - Public-lineup model (Opus / Sonnet / Haiku 4.x); Anthropic-internal
#    "protected" classes have stricter binding.

PRIV_USER_INSTRUCTION_TEMPLATE = """\
The conversation above is one user-assistant turn from an agent trace. \
You will summarize it bottom-up by repeatedly merging spans. On this call, \
pick exactly ONE contiguous span [start, end) of the current frontier to \
collapse into a summary node.

Each entry in the frontier below maps 1:1 to a content block in the \
assistant message you just read. For thinking entries you have server-side \
access to the original chain-of-thought via signature decryption — use it \
to write summaries that incorporate the actual reasoning, not just the \
visible outcomes.

Current frontier ({n_entries} entries):

{frontier_index}

Pick a span and call merge(start, end, summary). Indices are over the \
frontier above. Constraints: 0 <= start < end <= {n_entries}; \
end - start >= 2.
"""


def _walk_leaves_under(entry: Node | Leaf):
    if isinstance(entry, Leaf):
        yield entry
    else:
        for c in entry.children:
            yield from _walk_leaves_under(c)


def _render_priv_frontier_index(frontier: Frontier) -> str:
    """Indexed listing of the frontier for the merge instruction. Each entry
    here corresponds 1:1 to a content block in the assistant message above.
    """
    lines = []
    for i, entry in enumerate(frontier):
        if isinstance(entry, Leaf):
            kind = entry.kind
            if kind == "thinking":
                hint = "(server-decrypted chain-of-thought available to you)"
            else:
                p = entry.preview.replace("\n", " ").replace('"', "'")
                if len(p) > 100:
                    p = p[:97] + "..."
                hint = f'"{p}"'
            lines.append(f"  [{i}] {kind} | {hint}")
        else:  # Node summary
            n = sum(1 for _ in _walk_leaves_under(entry))
            p = entry.preview.replace("\n", " ").replace('"', "'")
            if len(p) > 100:
                p = p[:97] + "..."
            lines.append(f"  [{i}] summary node (covers {n} leaves) | \"{p}\"")
    return "\n".join(lines)


def _build_priv_messages(turn: Turn, frontier: Frontier) -> list[dict]:
    """Build the messages array for one privileged-mode merge call.

    Structure:
      [
        {"role":"user",      "content": <verbatim user_prompt that bounded the turn>},
        {"role":"assistant", "content": <render_frontier_as_message_blocks(frontier)>},
        {"role":"user",      "content": <merge instruction with frontier index>},
      ]

    Thinking blocks in the assistant content carry their original signatures.
    Tool calls are rendered as text descriptions (not native tool_use blocks)
    so we don't need matching tool_results in a following user message —
    that user message slot is occupied by the merge instruction.
    """
    user_text = ""
    if turn.user_prompt is not None:
        user_text = turn.user_prompt.content.get("text", "") or ""
    if not user_text:
        user_text = "<original user prompt was not present in this turn>"

    assistant_blocks = render_frontier_as_message_blocks(frontier)
    instruction = PRIV_USER_INSTRUCTION_TEMPLATE.format(
        n_entries=len(frontier),
        frontier_index=_render_priv_frontier_index(frontier),
    )

    return [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_blocks},
        {"role": "user", "content": instruction},
    ]


def _request_merge_priv(
    client,
    turn: Turn,
    frontier: Frontier,
    cfg: ConstructorConfig,
) -> tuple[int, int, str]:
    """Issue one privileged merge request. Returns (start, end, summary).

    Same retry / response-parsing logic as `_request_merge`; only the input
    shape differs.
    """
    messages = _build_priv_messages(turn, frontier)

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
                messages=messages,
            )
        except Exception as e:
            last_err = e
            continue

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "merge":
                inp: Any = block.input
                try:
                    return int(inp["start"]), int(inp["end"]), str(inp["summary"])
                except (KeyError, TypeError, ValueError) as e:
                    last_err = e
                    continue

        last_err = _MergeRequestError("no merge tool call in response")
    raise _MergeRequestError(str(last_err) if last_err else "unknown")


def construct_turn_priv(
    turn: Turn,
    client,
    cfg: ConstructorConfig,
) -> ConstructionTrace:
    """Privileged-constructor build for one turn.

    The model sees the original conversation as native `messages`, so it can
    read thinking content via signature decryption when picking merges and
    writing summaries. Otherwise structurally identical to `construct_turn`:
    same merge tool, same loop, same termination, same trace shape.
    """
    trace = ConstructionTrace(
        turn_id=turn.turn_id, initial_leaf_count=len(turn.leaves)
    )
    if not turn.leaves:
        trace.aborted_reason = "empty turn"
        return trace
    if len(turn.leaves) == 1:
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
            start, end, summary = _request_merge_priv(client, turn, frontier, cfg)
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
# Privileged constructor via Claude CLI (subscription auth)
# ---------------------------------------------------------------------------
#
# Same logical loop as `construct_turn_priv` above, but instead of calling
# the Anthropic SDK with a constructed `messages` array, we:
#   1. Build a synthetic Claude Code session JSONL representing the current
#      frontier (verbatim from source for step 0; rebuilt from frontier for
#      subsequent steps).
#   2. Place it in ~/.claude/projects/<slug>/<fresh-uuid>.jsonl.
#   3. Run `claude --resume <fresh-uuid> -p "<merge-instruction>" ...`.
#   4. Parse the response, apply the merge, repeat.
#
# Subscription auth via the keychain, so thinking signatures validate
# against the original credential — no ANTHROPIC_API_KEY needed.

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_first_json_object(text: str) -> dict | None:
    """Tolerantly pull the first JSON object out of model output. Handles
    optional ```json fences and leading/trailing prose."""
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start: i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None


def _priv_cli_request_merge(
    session_id: str,
    instruction: str,
    cfg: ConstructorConfig,
    timeout_seconds: int = 300,
) -> tuple[int, int, str]:
    """Invoke `claude --resume <id> -p <instruction>` for one merge call."""
    cmd = [
        "claude",
        "-p", instruction,
        "--resume", session_id,
        "--no-session-persistence",
        "--disable-slash-commands",
        "--setting-sources", "user",
        "--tools", "",
        "--max-turns", "1",
        "--output-format", "json",
        "--model", cfg.model,
        "--system-prompt", SYSTEM_PROMPT,
    ]

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout_seconds
    )
    if proc.returncode != 0:
        raise _MergeRequestError(
            f"claude exit {proc.returncode}: {proc.stderr[:300]}"
        )

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise _MergeRequestError(f"could not parse CLI envelope: {e}\n"
                                 f"stdout[:500]: {proc.stdout[:500]}")

    text = envelope.get("result", "") if isinstance(envelope, dict) else ""
    if not isinstance(text, str) or not text.strip():
        raise _MergeRequestError(f"empty `result` in CLI envelope: {envelope}")

    obj = _extract_first_json_object(text)
    if obj is None:
        raise _MergeRequestError(
            f"no JSON object in model output:\n{text[:500]}"
        )
    try:
        return int(obj["start"]), int(obj["end"]), str(obj["summary"])
    except (KeyError, TypeError, ValueError) as e:
        raise _MergeRequestError(f"merge JSON missing fields: {e}\nobject: {obj}")


def construct_turn_priv_cli(
    turn: Turn,
    source_jsonl: str,
    cfg: ConstructorConfig,
    cwd: str | None = None,
    progress: Callable[[int, str, int], None] | None = None,
    keep_first_step_session: bool = False,
) -> ConstructionTrace:
    """Privileged-mode tree construction via Claude CLI (subscription auth).

    Each merge step:
      1. Builds a synthetic Claude Code session JSONL representing the
         current frontier (verbatim from source for step 0; rebuilt from
         the current frontier for subsequent steps).
      2. Places it under ~/.claude/projects/<cwd-slug>/<fresh-uuid>.jsonl.
      3. Runs `claude --resume <fresh-uuid> -p "<merge-instruction>" ...`.
      4. Parses the response, applies the merge, repeats.
    """
    trace = ConstructionTrace(
        turn_id=turn.turn_id, initial_leaf_count=len(turn.leaves)
    )
    if not turn.leaves:
        trace.aborted_reason = "empty turn"
        return trace
    if len(turn.leaves) == 1:
        turn.root = turn.leaves[0]
        trace.finished = True
        return trace

    cwd = cwd or os.getcwd()
    user_prompt_text = ""
    if turn.user_prompt is not None:
        user_prompt_text = turn.user_prompt.content.get("text", "") or ""
    if not user_prompt_text:
        user_prompt_text = "<original user prompt was empty>"

    frontier: Frontier = list(turn.leaves)
    next_id = 0
    safety_cap = int(cfg.safety_cap_multiplier * len(turn.leaves))

    for step in range(safety_cap):
        if len(frontier) == 1:
            break

        session_id = str(uuidlib.uuid4())
        if step == 0:
            jsonl_text = build_synthetic_jsonl_from_source(
                source_jsonl, turn, session_id, cwd
            )
        else:
            jsonl_text = build_synthetic_jsonl_from_frontier(
                user_prompt_text, frontier, session_id, cwd
            )

        sp = session_path(session_id, cwd)
        sp.write_text(jsonl_text)

        instruction = PRIV_USER_INSTRUCTION_TEMPLATE.format(
            n_entries=len(frontier),
            frontier_index=_render_priv_frontier_index(frontier),
        )
        instruction += (
            "\n\nOutput format (STRICT): a JSON object on a single line:\n"
            '{"start": <int>, "end": <int>, "summary": "<text>"}'
        )

        try:
            try:
                start, end, summary = _priv_cli_request_merge(
                    session_id, instruction, cfg
                )
            except _MergeRequestError as e:
                trace.aborted_reason = f"step {step}: {e}"
                break
        finally:
            if not (step == 0 and keep_first_step_session):
                try:
                    sp.unlink()
                except OSError:
                    pass

        if not _valid_span(start, end, len(frontier)):
            trace.aborted_reason = (
                f"step {step}: invalid span ({start}, {end}) for frontier "
                f"of size {len(frontier)}"
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
        if progress is not None:
            progress(step, summary, len(frontier))

    if len(frontier) == 1:
        turn.root = frontier[0]
        trace.finished = True
    else:
        turn.root = Node(
            id=next_id,
            children=list(frontier),
            summary="<construction aborted; uncollapsed frontier>",
            tokens=sum(getattr(n, "tokens", 0) for n in frontier),
            preview="<aborted>",
        )
        trace.aborted_reason = trace.aborted_reason or "hit safety cap"

    return trace
