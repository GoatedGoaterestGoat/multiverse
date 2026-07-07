"""Parse a Claude Code JSONL session log into a Forest of Turns.

A turn is delimited by a `user`-with-string-content row (the actual user
prompt). Everything between two user prompts (assistant content blocks
and tool results) belongs to the prior prompt's turn.

Metadata rows (custom-title, file-history-snapshot, attachment, system,
queue-operation, last-prompt, agent-name, permission-mode) are dropped.

Each surviving JSONL row becomes one Leaf. Auto-merging tool_use/tool_result
pairs is a separate preprocess step (see preprocess.py); the parser leaves
them as distinct leaves with linkage info preserved on `Leaf.content`.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from .tokenize import count_image_tokens, count_tokens
from .types import Forest, Leaf, Turn

# Top-level `type` values that carry no compactable content.
# Note: ``attachment`` is a broad container — most kinds (deferred_tools,
# skill_listing, task_reminder, IDE state, etc.) are transient system
# notices and correctly skipped. But ``attachment.type == "queued_command"``
# is a real user-authored prompt (typed while the agent was mid-turn) —
# it lands in the JSONL as an attachment row rather than a regular user
# row because it took the queue path instead of the fresh-prompt path.
# _classify_and_extract handles that case specially BEFORE this blanket
# skip fires, so those aren't silently dropped.
SKIP_TYPES = frozenset({
    "custom-title",
    "agent-name",
    "permission-mode",
    "file-history-snapshot",
    "attachment",
    "last-prompt",
    "queue-operation",
    "system",
})


def _iter_jsonl(path: str) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _live_chain_rows(path: str) -> list[dict[str, Any]]:
    """Read all rows, walk parentUuid back from the live tip, and return
    only rows on the live chain (in chronological / file order).

    Why this matters: claude code records "restore conversation" as a
    new branch — the abandoned rows stay in the JSONL, and the new
    branch's first row's ``parentUuid`` points at an earlier row,
    leaving the abandoned subtree orphaned. A linear read of the JSONL
    treats those orphans as part of the conversation, which corrupts
    tree-build (turns include reverted content) and compaction (the
    summarized output preserves content the user explicitly threw
    away).

    Live tip selection: among the leaves of the parentUuid DAG (uuids
    no other row points to), pick the one with the latest ``timestamp``;
    break ties by file order (last appearance wins).

    Metadata rows (no ``uuid`` — custom-title, agent-name, file-history-
    snapshot, etc.) are preserved at their original file positions.
    They're ``SKIP_TYPES`` for parsing, so they're harmless either way,
    but keeping them doesn't break anything.
    """
    all_rows = list(_iter_jsonl(path))

    by_uuid: dict[str, dict] = {}
    parented: set[str] = set()
    for r in all_rows:
        u = r.get("uuid")
        if u:
            by_uuid[u] = r
            p = r.get("parentUuid")
            if p:
                parented.add(p)

    # If the file is malformed or has no uuid'd rows, return everything.
    if not by_uuid:
        return all_rows

    # Leaves of the DAG: rows that no one else points to as parent.
    leaves = [u for u in by_uuid if u not in parented]
    if not leaves:
        # Cycle or orphaned ancestry — fall back to linear read.
        return all_rows

    # Tip = leaf with latest timestamp; tie-break by latest file
    # appearance.
    file_index = {u: i for i, r in enumerate(all_rows)
                  for u in [r.get("uuid")] if u}
    tip = max(
        leaves,
        key=lambda u: (
            by_uuid[u].get("timestamp", "") or "",
            file_index.get(u, -1),
        ),
    )

    # Walk back via parentUuid.
    chain: set[str] = set()
    cur: str | None = tip
    while cur and cur in by_uuid and cur not in chain:
        chain.add(cur)
        cur = by_uuid[cur].get("parentUuid")

    # Return rows in original file order, keeping (a) chain rows and
    # (b) metadata rows with no uuid. Drop any uuid'd row not on the
    # chain (those are orphaned / reverted branches).
    out: list[dict[str, Any]] = []
    for r in all_rows:
        u = r.get("uuid")
        if u is None or u in chain:
            out.append(r)
    return out


def _build_usage_index(path: str) -> tuple[dict[str, int], dict[str, int]]:
    """Pre-pass over the JSONL: build per-assistant-message totals for
    `output_tokens` and visible-content tokens. Used to estimate the cost
    of redacted thinking blocks via `output_tokens - visible`.

    Visible content = sum of text-block tokens + tool_use (name + input)
    tokens. Thinking blocks have empty plaintext, so they contribute zero
    here; the residual after subtraction is the thinking estimate.
    """
    out_tok: dict[str, int] = {}
    visible_tok: dict[str, int] = {}
    for row in _iter_jsonl(path):
        if row.get("type") != "assistant":
            continue
        msg = row.get("message", {})
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        if not mid:
            continue
        u = msg.get("usage")
        if isinstance(u, dict) and "output_tokens" in u:
            out_tok[mid] = u["output_tokens"]
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                visible_tok[mid] = visible_tok.get(mid, 0) + count_tokens(block.get("text", ""))
            elif bt == "tool_use":
                nm = block.get("name", "")
                inp = json.dumps(block.get("input", {}))
                visible_tok[mid] = visible_tok.get(mid, 0) + count_tokens(nm + inp)
    return out_tok, visible_tok


def _render_content_attachment(att: dict[str, Any]) -> tuple[str, bool] | None:
    """For any non-empty attachment, return ``(rendered_text, compactable)``.
    Returns None only when the attachment has no meaningful content on
    this row (e.g. an empty task_reminder or a hook_additional_context
    whose ``content`` array is empty).

    ``compactable=True`` means the leaf goes through the compaction
    pipeline normally — considered for merging, and preserved (or
    summarized) in the compacted JSONL as a user-role text block. Only
    the WHITELIST kinds get this: a ``queued_command`` (a prompt the
    user actually typed), and an inter-agent-message
    ``hook_additional_context`` (a message from another agent —
    semantically user-role to the recipient, and the sole surviving
    copy once the inbox file is unlinked).

    ``compactable=False`` is the default for everything else. Those
    leaves are counted for display / total accounting but filtered out
    before tree building. This is a whitelist by design: if claude
    later adds a new attachment kind, it'll be counted (via the generic
    fallback below) and stay out of the compaction pipeline until we
    explicitly decide it belongs on the whitelist.

    Rendering: known kinds mirror ``utils/messages.ts`` in claude's
    source (case dispatch on ``attachment.type``), so ``count_tokens``
    lands close to what claude actually sends. Unknown kinds fall back
    to a JSON serialization of the attachment's non-type fields — an
    approximation, but non-zero, and correct-signed.
    """
    att_type = att.get("type")

    # ------------------------------------------------------------------
    # WHITELIST: kinds that ride the user-role text path through
    # compaction because they ARE user-role speech.
    # ------------------------------------------------------------------

    if att_type == "queued_command":
        prompt = att.get("prompt", "") or ""
        return (prompt, True) if prompt else None

    if att_type == "hook_additional_context":
        content = att.get("content")
        if isinstance(content, list):
            joined = "\n".join(
                c if isinstance(c, str) else str(c) for c in content
            )
        elif isinstance(content, str):
            joined = content
        else:
            return None
        if not joined:
            return None
        hook_name = att.get("hookName", "?")
        rendered = f"{hook_name} hook additional context: {joined}"
        # IAM messages ARE user-role (from a peer agent, sole copy).
        # Anything else in hook_additional_context is misc hook context
        # — counted but not compacted.
        is_iam = "[Multiverse Inter-Agent]" in joined
        return (rendered, is_iam)

    # ------------------------------------------------------------------
    # NON-COMPACTABLE, known kinds. Each mirrors claude's own render
    # template from utils/messages.ts for accuracy.
    # ------------------------------------------------------------------

    if att_type == "edited_text_file":
        filename = att.get("filename", "") or ""
        snippet = att.get("snippet", "") or ""
        return (
            f"Note: {filename} was modified, either by the user or by "
            "a linter. This change was intentional, so make sure to "
            "take it into account as you proceed (ie. don't revert it "
            "unless the user asks you to). Don't tell the user this, "
            "since they are already aware. Here are the relevant "
            f"changes (shown with line numbers):\n{snippet}",
            False,
        )

    if att_type == "task_reminder":
        content = att.get("content") or []
        if not isinstance(content, list) or not content:
            return None
        items = "\n".join(
            f"#{t.get('id','?')}. [{t.get('status','?')}] {t.get('subject','')}"
            for t in content if isinstance(t, dict)
        )
        return (
            "The task tools haven't been used recently. If you're "
            "working on tasks that would benefit from tracking "
            "progress, consider using TaskCreate to add new tasks and "
            "TaskUpdate to update task status (set to in_progress when "
            "starting, completed when done). Also consider cleaning up "
            "the task list if it has become stale. Only use these if "
            "relevant to the current work. This is just a gentle "
            "reminder - ignore if not applicable. Make sure that you "
            "NEVER mention this reminder to the user\n\nHere are the "
            f"existing tasks:\n\n{items}",
            False,
        )

    if att_type == "skill_listing":
        content = att.get("content")
        if not isinstance(content, str) or not content:
            return None
        return (
            "The following skills are available for use with the Skill "
            f"tool:\n\n{content}",
            False,
        )

    if att_type == "deferred_tools_delta":
        added = att.get("addedLines") or []
        removed = att.get("removedNames") or []
        parts: list[str] = []
        if added:
            parts.append(
                "The following deferred tools are now available via "
                "ToolSearch:\n" + "\n".join(str(x) for x in added)
            )
        if removed:
            parts.append(
                "The following deferred tools are no longer available "
                "(their MCP server disconnected). Do not search for "
                "them — ToolSearch will return no match:\n"
                + "\n".join(str(x) for x in removed)
            )
        return ("\n\n".join(parts), False) if parts else None

    if att_type == "agent_listing_delta":
        added = att.get("addedLines") or []
        removed = att.get("removedTypes") or []
        parts = []
        if added:
            header = (
                "Available agent types for the Agent tool:"
                if att.get("isInitial")
                else "New agent types are now available for the Agent tool:"
            )
            parts.append(f"{header}\n" + "\n".join(str(x) for x in added))
        if removed:
            parts.append(
                "The following agent types are no longer available:\n"
                + "\n".join(f"- {t}" for t in removed)
            )
        return ("\n\n".join(parts), False) if parts else None

    if att_type == "mcp_instructions_delta":
        added = att.get("addedBlocks") or []
        removed = att.get("removedNames") or []
        parts = []
        if added:
            parts.append(
                "# MCP Server Instructions\n\nThe following MCP "
                "servers have provided instructions for how to use "
                "their tools and resources:\n\n"
                + "\n\n".join(str(x) for x in added)
            )
        if removed:
            parts.append(
                "The following MCP servers have disconnected. Their "
                "instructions above no longer apply:\n"
                + "\n".join(str(x) for x in removed)
            )
        return ("\n\n".join(parts), False) if parts else None

    # ------------------------------------------------------------------
    # FALLBACK: unknown attachment.type — new kinds claude-code added
    # since we last synced with its source. Render the non-``type`` fields
    # as JSON so we count SOME tokens rather than silently dropping the
    # payload, and mark non-compactable so it never surprises anyone by
    # showing up as user speech in the compacted output. When a new kind
    # deserves a proper rendering (or a whitelist entry for compaction),
    # add it explicitly above.
    # ------------------------------------------------------------------
    payload = {k: v for k, v in att.items() if k != "type"}
    if not payload:
        return None
    try:
        rendered = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if not rendered or rendered == "{}":
        return None
    return (rendered, False)


def _classify_and_extract(row: dict[str, Any]) -> Leaf | None:
    """Turn one JSONL row into a Leaf, or None if the row is metadata.

    Each assistant API message is split across multiple JSONL rows in
    Claude Code (one per content block: thinking / text / tool_use), so
    each meaningful row has exactly one content block. We rely on this.
    """
    t = row.get("type")

    # Attachment rows that carry real model-visible content. Rendered
    # here using the same template strings claude uses in
    # utils/messages.ts (case dispatch on attachment.type), so
    # count_tokens reflects roughly what the model actually sees rather
    # than the compact JSON we happen to store on disk. Attachment kinds
    # not returned by _render_content_attachment fall through to the
    # SKIP_TYPES filter below and are dropped (transient markers like
    # plan_mode_exit, file-history-snapshot, IDE-state pings, etc.).
    if t == "attachment":
        att = row.get("attachment")
        if isinstance(att, dict):
            rendered = _render_content_attachment(att)
            if rendered is not None:
                text, compactable = rendered
                uuid = row.get("uuid", "")
                return Leaf(
                    kind="user_text",
                    content={"text": text},
                    tokens=count_tokens(text),
                    preview=_preview(text, "user"),
                    source_uuids=[uuid],
                    compactable=compactable,
                )

    if t in SKIP_TYPES or t is None:
        return None

    msg = row.get("message")
    if not isinstance(msg, dict):
        return None

    content = msg.get("content")
    uuid = row.get("uuid", "")

    # user/<string>: user prompt
    if t == "user" and isinstance(content, str):
        return Leaf(
            kind="user_text",
            content={"text": content},
            tokens=count_tokens(content),
            preview=_preview(content, "user"),
            source_uuids=[uuid],
        )

    # user/<list>: tool_result, or rare user text-in-list
    if t == "user" and isinstance(content, list):
        # Top-level image blocks (a user message that attaches screenshots
        # alongside its text). Collected verbatim so the merge agent can see
        # them and the output renderer can re-emit them. count_image_tokens
        # captures their (substantial) cost.
        top_images = [
            b for b in content
            if isinstance(b, dict) and b.get("type") == "image"
        ]
        img_tokens = sum(count_image_tokens(b) for b in top_images)
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                result_content = block.get("content")
                # `content` may be a string or a list of {type:text,text:...}
                # and/or {type:image,source:...}. Pull text for the summary
                # and carry image sub-blocks verbatim on the leaf.
                result_images: list[dict] = []
                if isinstance(result_content, list):
                    text = "\n".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    result_images = [
                        b for b in result_content
                        if isinstance(b, dict) and b.get("type") == "image"
                    ]
                else:
                    text = str(result_content) if result_content is not None else ""
                images = top_images + result_images
                return Leaf(
                    kind="tool_result",
                    content={
                        "tool_use_id": block.get("tool_use_id"),
                        "text": text,
                        "is_error": block.get("is_error", False),
                    },
                    tokens=count_tokens(text)
                    + img_tokens
                    + sum(count_image_tokens(b) for b in result_images),
                    preview=_preview(text, "tool_result"),
                    source_uuids=[uuid],
                    images=images,
                )
            if btype == "text":
                text = block.get("text", "")
                return Leaf(
                    kind="user_text",
                    content={"text": text},
                    tokens=count_tokens(text) + img_tokens,
                    preview=_preview(text, "user"),
                    source_uuids=[uuid],
                    images=top_images,
                )
        # No text/tool_result block, but the message may be image-only
        # (e.g. a user message whose entire content is a screenshot). Emit a
        # user_text leaf so the images are captured rather than dropped.
        if top_images:
            return Leaf(
                kind="user_text",
                content={"text": ""},
                tokens=img_tokens,
                preview=f"<{len(top_images)} image(s)>",
                source_uuids=[uuid],
                images=top_images,
            )
        return None

    # assistant/<list>: one content block per row in this format
    if t == "assistant" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                text = block.get("thinking", "")
                # tokens here is a baseline (count_tokens of any visible
                # plaintext). The post-pass in parse_session() overrides
                # this with the verbatim estimate from message-level usage
                # for redacted thinking — that's the cost the live agent
                # actually pays when this leaf is preserved as a native
                # thinking block in the compacted output.
                return Leaf(
                    kind="thinking",
                    content={"text": text, "signature": block.get("signature")},
                    tokens=count_tokens(text),
                    preview=_preview(text, "thinking") if text else "<thinking: encrypted>",
                    source_uuids=[uuid],
                )
            if btype == "text":
                text = block.get("text", "")
                return Leaf(
                    kind="asst_text",
                    content={"text": text},
                    tokens=count_tokens(text),
                    preview=_preview(text, "asst"),
                    source_uuids=[uuid],
                )
            if btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                tool_use_id = block.get("id")
                rendered = f"{name}({_short_args(inp)})"
                return Leaf(
                    kind="tool_use",
                    content={
                        "tool_use_id": tool_use_id,
                        "name": name,
                        "input": inp,
                    },
                    tokens=count_tokens(rendered) + count_tokens(json.dumps(inp)),
                    preview=rendered,
                    source_uuids=[uuid],
                )
        return None

    return None


def _short_args(inp: Any, max_len: int = 80) -> str:
    """One-line rendering of a tool's input for previews."""
    if isinstance(inp, dict):
        # Prefer common keys when available
        for key in ("command", "file_path", "path", "url", "query", "pattern"):
            if key in inp and isinstance(inp[key], str):
                v = inp[key]
                return v if len(v) <= max_len else v[: max_len - 3] + "..."
        # Fallback to JSON
        s = json.dumps(inp)
    else:
        s = str(inp)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _preview(text: str, kind: str, max_len: int = 120) -> str:
    """Compact one-line preview suitable for agent prompts."""
    if not text:
        return f"<empty {kind}>"
    flat = " ".join(text.split())
    return flat if len(flat) <= max_len else flat[: max_len - 3] + "..."


def _is_user_prompt_row(row: dict[str, Any], last_pid: str | None) -> bool:
    """True if this row starts a new turn.

    The harness assigns a fresh `promptId` to every real user prompt and
    inherits it for everything (tool_results, hook-injected user records,
    assistant blocks) produced in response. So a `promptId` change is the
    canonical turn boundary; content-shape heuristics misfire on synthetic
    user records like "Stop hook feedback:" that share a promptId with the
    prompt they're paired to.
    """
    if row.get("type") != "user":
        return False
    pid = row.get("promptId")
    if pid is None or pid == last_pid:
        return False
    msg = row.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return True
    return False


def filter_compactable(forest: Forest) -> Forest:
    """Return ``forest`` with non-compactable leaves stripped out of every
    turn — for consumers on the compaction pipeline that must not surface
    display-only attachment content as user speech in the compacted
    output.

    ``parse_session`` returns a forest that includes every leaf the model
    could see, so ``live_stats``/total-token consumers get accurate
    dashboard numbers. But some of those leaves — non-IAM
    ``hook_additional_context``, ``task_reminder``, ``skill_listing``,
    ``deferred_tools_delta``, ``agent_listing_delta``,
    ``mcp_instructions_delta``, ``edited_text_file``, and any future
    attachment kinds we don't explicitly whitelist — are structural
    context, not user speech. Feeding them into ``render_frontier_as_messages``
    would render them as user-role text blocks in the compacted JSONL,
    corrupting the transcript with fake user messages.

    So the compaction pipeline calls this first, and everything
    downstream (tree building, budget allocation, rendering) operates on
    the filtered view. Turn structure (user_prompt, turn_id, ordering)
    is preserved; only ``turn.leaves`` shrinks. Mutates in place and
    returns the same forest for chainability.
    """
    for turn in forest:
        turn.leaves = [l for l in turn.leaves if l.compactable]
    return forest


def parse_session(path: str) -> Forest:
    """Parse a Claude Code JSONL session log into a Forest of Turns.

    Only rows on the **live conversation chain** are processed.
    Abandoned branches from "restore conversation" stay in the file
    but are skipped here — see ``_live_chain_rows`` for the tip-
    selection rule and rationale.
    """
    out_tok, visible_tok = _build_usage_index(path)
    chain_rows = _live_chain_rows(path)

    turns: Forest = []
    current: Turn | None = None
    pending_orphans: list[Leaf] = []
    last_pid: str | None = None

    for row in chain_rows:
        leaf = _classify_and_extract(row)

        # For thinking leaves, override `tokens` with an estimate of the
        # original API-billed thinking length: output_tokens of the source
        # message minus the visible content. This is the cost the live
        # agent will pay if this leaf is preserved as a native thinking
        # block in the compacted output (signature decrypts server-side).
        if leaf is not None and leaf.kind == "thinking":
            mid = (row.get("message") or {}).get("id")
            if mid and mid in out_tok:
                est = max(0, out_tok[mid] - visible_tok.get(mid, 0))
                if est > leaf.tokens:
                    leaf.tokens = est

        if _is_user_prompt_row(row, last_pid):
            if current is not None:
                turns.append(current)
            turn_id = row.get("promptId") or row.get("uuid") or f"turn-{len(turns)}"
            last_pid = row.get("promptId")
            # The user prompt is hoisted out of `leaves` — it acts as a
            # verbatim separator and never enters the compactable region.
            current = Turn(turn_id=turn_id, user_prompt=leaf, leaves=[])
            # Drain any pre-first-prompt orphans into this turn's leaves
            if pending_orphans:
                current.leaves.extend(pending_orphans)
                pending_orphans = []
            continue

        # Non-prompt row
        if leaf is None:
            continue
        if current is None:
            # Content before any user prompt; stash and attach to first turn
            pending_orphans.append(leaf)
        else:
            current.leaves.append(leaf)

    if current is not None:
        turns.append(current)
    elif pending_orphans:
        # Edge case: file with content but no user prompt at all
        turns.append(Turn(turn_id="turn-0", user_prompt=None, leaves=pending_orphans))

    return turns
