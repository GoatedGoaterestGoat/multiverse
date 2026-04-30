"""Core data types for the compaction tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

LeafKind = Literal[
    "user_text",       # user prompt or other user-initiated text
    "thinking",        # assistant thinking block
    "asst_text",       # assistant text response
    "tool_use",        # assistant tool call (unpaired)
    "tool_result",     # tool result (unpaired)
    "tool_exchange",   # auto-merged tool_use + tool_result
]


@dataclass
class Leaf:
    """One atomic unit of conversation content.

    Built from one JSONL row in the simple case, or from an auto-merge of
    several rows (e.g. tool_use + tool_result -> tool_exchange).

    `tokens` is the cost the LIVE agent will pay if this leaf is preserved
    raw in the compacted output. The compacted output is fed back via
    structured rendering (`render_frontier_as_message_blocks`), where:

    * Thinking leaves emit the original `{type:"thinking", signature:...}`
      block verbatim. The Anthropic API decrypts the signature server-side
      under the user's credential and feeds the original chain-of-thought
      into the model's context — billed at the original thinking length
      (~hundreds to thousands of tokens). For these leaves, `tokens` is
      the estimated original thinking length, computed by the parser from
      `usage.output_tokens - count_tokens(visible content)` of the source
      message (rather than the 5-token placeholder string the visualizer
      shows for human display).
    * All other leaves emit text blocks; `tokens` is their text-rendering
      cost.

    The `<thinking: encrypted>` / `[thinking: encrypted]` strings are purely
    cosmetic — they appear in the visualizer and in the text-mode debug
    dump, but the live agent never sees them; it sees the verbatim block.
    """
    kind: LeafKind
    content: dict[str, Any]    # raw payload; format depends on kind
    tokens: int                # cost the live agent will pay if this leaf
                               # survives in the structured-rendered output
    preview: str               # short rendering used in agent prompts
    source_uuids: list[str] = field(default_factory=list)


@dataclass
class Node:
    """An internal node in a turn's compaction tree.

    Created by a single merge() call during construction. Children are
    a contiguous run of frontier entries (other Nodes or Leaves) at the
    moment of the merge.

    `delta` is the token cost of expanding this node = sum(child tokens)
    - self.tokens. Auto-computed from `children` + `tokens` in __post_init__
    so callers never pass it. `expand_to_budget` uses it for O(1) per-step
    incremental budget checks.
    """
    id: int                                # sequential merge id; root has the highest id
    children: list[Union["Node", Leaf]]
    summary: str                           # text covering this span
    tokens: int                            # token count of `summary`
    preview: str                           # short rendering used in agent prompts
    delta: int = field(init=False, default=0, repr=False)

    def __post_init__(self):
        self.delta = sum(c.tokens for c in self.children) - self.tokens


# A frontier is an ordered list of Nodes/Leaves that tile a turn exactly.
Frontier = list[Union[Node, Leaf]]


@dataclass
class Turn:
    """One user-prompt-to-next-user-prompt window of conversation.

    The originating user prompt is held separately on `user_prompt` and is
    NOT part of the compactable region — it acts as a verbatim separator
    between turns and is never summarized into the tree. `leaves` holds
    only the assistant response chain (thinking, text, tool exchanges,
    plus any harness-injected mid-turn user records sharing this turn's
    promptId), and `root` is built over those leaves alone.
    """
    turn_id: str                            # promptId of the originating user prompt, or synthesized
    user_prompt: Union[Leaf, None]          # verbatim separator; never compacted; may be None for orphan turns
    leaves: list[Leaf]                      # compactable region: assistant response + mid-turn user injections
    # Set after construction:
    root: Union[Node, Leaf, None] = None


# A forest is an ordered list of Turns (oldest first).
Forest = list[Turn]
