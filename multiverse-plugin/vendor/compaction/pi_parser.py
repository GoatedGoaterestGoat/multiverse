"""pi session JSONL -> Forest.

The Claude Code parser (``parser.py``) has to reconstruct turn structure from
``promptId`` and rebuild the live chain by scanning for the tip of a branch
graph that the harness only implicitly maintains. pi hands us both for free:
every entry carries ``id``/``parentId``, so the active branch is a literal
walk from the leaf to the root, and a turn boundary is just a user-role
message entry.

Content-block differences from Claude Code worth knowing:

* pi splits nothing — one assistant API message is ONE entry whose ``content``
  is the full block array. Claude Code emits one row per block. So this parser
  iterates blocks within an entry rather than treating each row as one block.
* Thinking is stored as **plaintext** in ``thinking``, with
  ``thinkingSignature`` alongside. No signature decryption, no usage-based
  back-estimation of the original length: ``count_tokens`` on the text is the
  real cost.
* Images are ``{type:"image", data, mimeType}`` rather than Anthropic's
  ``{type:"image", source:{...}}``. They are carried verbatim on the leaf and
  converted only for token counting.
* ``bashExecution`` (a user-run ``!cmd``) and ``custom``/``custom_message``
  (extension-injected) have no Claude Code analogue.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Optional

from .tokenize import count_image_tokens, count_tokens
from .types import Forest, Leaf, Turn

#: Entry types that never contribute context.
_SKIP_ENTRY_TYPES = frozenset({
    "session", "model_change", "thinking_level_change", "label",
    "session_info", "custom",
})


def _iter_entries(path: str) -> Iterator[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _to_anthropic_image(block: dict) -> dict:
    """pi image block -> the shape ``count_image_tokens`` understands."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": block.get("mimeType", "image/png"),
            "data": block.get("data", ""),
        },
    }


def _image_tokens(blocks: list[dict]) -> int:
    return sum(count_image_tokens(_to_anthropic_image(b)) for b in blocks)


def active_branch(path: str) -> list[dict[str, Any]]:
    """Entries on the current branch, root-first.

    pi keeps abandoned branches in the same file (``/tree`` navigation writes
    new children rather than a new file), so a plain file-order read would mix
    timelines. The leaf is the last entry that is not an ancestor of a later
    entry — equivalently, the last entry in file order, since pi appends the
    active leaf's children as it goes.
    """
    entries = [e for e in _iter_entries(path) if e.get("type") != "session"]
    if not entries:
        return []
    by_id = {e["id"]: e for e in entries if isinstance(e.get("id"), str)}
    leaf = entries[-1]
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur: Optional[dict[str, Any]] = leaf
    while cur is not None:
        cid = cur.get("id")
        if not isinstance(cid, str) or cid in seen:
            break
        seen.add(cid)
        chain.append(cur)
        pid = cur.get("parentId")
        cur = by_id.get(pid) if isinstance(pid, str) else None
    chain.reverse()
    return chain


def _preview(text: str, kind: str, max_len: int = 120) -> str:
    flat = " ".join((text or "").split())
    if not flat:
        return f"<{kind}: empty>"
    return flat if len(flat) <= max_len else flat[: max_len - 3] + "..."


def _short_args(inp: Any, max_len: int = 80) -> str:
    if isinstance(inp, dict):
        for key in ("command", "path", "file_path", "url", "query", "pattern"):
            v = inp.get(key)
            if isinstance(v, str):
                return v if len(v) <= max_len else v[: max_len - 3] + "..."
        s = json.dumps(inp, separators=(",", ":"))
    else:
        s = str(inp)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _entry_leaves(entry: dict[str, Any]) -> list[Leaf]:
    """All leaves contributed by one session entry, in order.

    Unlike the Claude parser this returns a *list*: a pi assistant entry holds
    every content block of one API message, so it can yield several leaves.
    """
    etype = entry.get("type")
    eid = entry.get("id", "")

    if etype == "compaction":
        # Skipped on purpose. A compaction entry summarizes entries that are
        # still on this same branch — pi never deletes them — and we are
        # re-deriving those entries ourselves from the raw history. Keeping
        # the old summary too would mean compacting a summary of content we
        # are already compacting, which is how flat compaction compounds
        # error across repeated passes. Reading past it is exactly what makes
        # multiverse compaction non-destructive: every pass starts from the
        # original turns, however many times the session has been compacted.
        return []

    if etype == "branch_summary":
        # Kept, unlike compaction: a branch summary describes an ABANDONED
        # branch, whose entries are not on this path. It is the only surviving
        # record of that work.
        summary = str(entry.get("summary", ""))
        return [Leaf(
            kind="asst_text",
            content={"text": summary, "pi_branch_summary": True},
            tokens=count_tokens(summary),
            preview=_preview(summary, "branch summary"),
            source_uuids=[eid],
        )]

    if etype == "custom_message":
        content = entry.get("content")
        text = content if isinstance(content, str) else _plain_text(content)
        return [Leaf(
            kind="user_text",
            content={"text": text, "customType": entry.get("customType")},
            tokens=count_tokens(text),
            preview=_preview(text, "custom"),
            source_uuids=[eid],
        )]

    if etype != "message":
        return []

    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    role = msg.get("role")

    if role == "user":
        content = msg.get("content")
        images = _blocks_of(content, "image")
        text = content if isinstance(content, str) else _plain_text(content)
        return [Leaf(
            kind="user_text",
            content={"text": text},
            tokens=count_tokens(text) + _image_tokens(images),
            preview=_preview(text, "user") if text else f"<{len(images)} image(s)>",
            source_uuids=[eid],
            images=images,
        )]

    if role == "assistant":
        out: list[Leaf] = []
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                text = str(block.get("thinking", ""))
                out.append(Leaf(
                    kind="thinking",
                    content={
                        "text": text,
                        "signature": block.get("thinkingSignature"),
                    },
                    # pi stores reasoning as plaintext, so unlike the Claude
                    # path there is nothing to back-estimate from usage.
                    tokens=count_tokens(text),
                    preview=_preview(text, "thinking"),
                    source_uuids=[eid],
                ))
            elif btype == "text":
                text = str(block.get("text", ""))
                out.append(Leaf(
                    kind="asst_text",
                    content={"text": text},
                    tokens=count_tokens(text),
                    preview=_preview(text, "asst"),
                    source_uuids=[eid],
                ))
            elif btype == "toolCall":
                name = str(block.get("name", "?"))
                args = block.get("arguments", {})
                rendered = f"{name}({_short_args(args)})"
                out.append(Leaf(
                    kind="tool_use",
                    content={
                        "tool_use_id": block.get("id"),
                        "name": name,
                        "input": args,
                    },
                    tokens=count_tokens(rendered) + count_tokens(json.dumps(args, default=str)),
                    preview=rendered,
                    source_uuids=[eid],
                ))
        return out

    if role == "toolResult":
        content = msg.get("content")
        text = content if isinstance(content, str) else _plain_text(content)
        images = _blocks_of(content, "image")
        return [Leaf(
            kind="tool_result",
            content={
                "tool_use_id": msg.get("toolCallId"),
                "name": msg.get("toolName"),
                "text": text,
                "is_error": bool(msg.get("isError")),
            },
            tokens=count_tokens(text) + _image_tokens(images),
            preview=_preview(text, "tool_result"),
            source_uuids=[eid],
            images=images,
        )]

    if role == "bashExecution":
        if msg.get("excludeFromContext"):
            return []   # `!!` commands are deliberately kept out of context
        command = str(msg.get("command", ""))
        output = str(msg.get("output", ""))
        return [Leaf(
            kind="tool_exchange",
            content={
                "name": "bash",
                "input": {"command": command},
                "result_text": output,
                "is_error": bool(msg.get("exitCode")),
            },
            tokens=count_tokens(command) + count_tokens(output),
            preview=_preview(f"$ {command} -> {output}", "bash"),
            source_uuids=[eid],
        )]

    if role in ("compactionSummary", "branchSummary"):
        summary = str(msg.get("summary", ""))
        return [Leaf(
            kind="asst_text",
            content={"text": summary},
            tokens=count_tokens(summary),
            preview=_preview(summary, "summary"),
            source_uuids=[eid],
        )]

    return []


def _blocks_of(content: Any, btype: str) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == btype]


def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = [
        str(b.get("text", ""))
        for b in _blocks_of(content, "text")
    ]
    return "\n".join(p for p in parts if p)


def _is_turn_boundary(entry: dict[str, Any]) -> bool:
    """A real user prompt starts a new turn.

    Extension-injected ``custom_message`` entries do NOT: they are pushed into
    a turn already in flight (that is exactly how multiverse delivers an
    inter-agent message on pi), so treating them as boundaries would split one
    logical turn in half.
    """
    if entry.get("type") != "message":
        return False
    msg = entry.get("message")
    return isinstance(msg, dict) and msg.get("role") == "user"


def parse_pi_session(path: str) -> Forest:
    """Parse a pi session JSONL into a Forest of Turns.

    Each ``Turn`` gets the pi entry id of its originating user message as its
    ``turn_id``. That id doubles as pi's ``firstKeptEntryId`` handle, so the
    compaction writer can express "keep this turn onward verbatim" without any
    extra bookkeeping.
    """
    turns: Forest = []
    current: Optional[Turn] = None
    orphans: list[Leaf] = []

    for entry in active_branch(path):
        if entry.get("type") in _SKIP_ENTRY_TYPES:
            continue
        leaves = _entry_leaves(entry)
        if _is_turn_boundary(entry):
            if current is not None:
                turns.append(current)
            prompt = leaves[0] if leaves else None
            current = Turn(
                turn_id=str(entry.get("id") or f"turn-{len(turns)}"),
                user_prompt=prompt,
                leaves=[],
            )
            if orphans:
                current.leaves.extend(orphans)
                orphans = []
            continue
        if not leaves:
            continue
        if current is None:
            orphans.extend(leaves)
        else:
            current.leaves.extend(leaves)

    if current is not None:
        turns.append(current)
    elif orphans:
        turns.append(Turn(turn_id="turn-0", user_prompt=None, leaves=orphans))
    return turns
