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
    """
    kind: LeafKind
    content: dict[str, Any]    # raw payload; format depends on kind
    tokens: int                # token count for use in budget math
    preview: str               # short rendering used in agent prompts
    source_uuids: list[str] = field(default_factory=list)


@dataclass
class Node:
    """An internal node in a turn's compaction tree.

    Created by a single merge() call during construction. Children are
    a contiguous run of frontier entries (other Nodes or Leaves) at the
    moment of the merge.
    """
    id: int                                # sequential merge id; root has the highest id
    children: list[Union["Node", Leaf]]
    summary: str                           # text covering this span
    tokens: int                            # token count of `summary`
    preview: str                           # short rendering used in agent prompts


# A frontier is an ordered list of Nodes/Leaves that tile a turn exactly.
Frontier = list[Union[Node, Leaf]]


@dataclass
class Turn:
    """One user-prompt-to-next-user-prompt window of conversation."""
    turn_id: str                  # promptId of the originating user prompt, or synthesized
    leaves: list[Leaf]
    # Set after construction:
    root: Union[Node, Leaf, None] = None


# A forest is an ordered list of Turns (oldest first).
Forest = list[Turn]
