"""Partial-transcript generation + per-agent delivery markers for the SecondBrain seam.

The ``agent.response`` event (DESIGN §4b) carries a ``full`` field: a transcript of
**what happened since the last time we delivered a transcript for that agent**, never
the whole backlog. This module produces that transcript and manages the marker that
makes it incremental.

The moving parts:

* **Delivery marker** — per agent, the point up to which we last delivered a transcript.
  Keyed on a **stable identity** ``(resolved cwd, agent_name)`` — NOT the claude_uuid,
  which changes on resume/compaction/fold. Stored one file per agent under
  ``~/.multiverse/secondbrain/transcript_markers/`` and written atomically (tmp+rename).
  Claude markers record ``{claude_uuid, turn_id, row_uuid, updated_at}``. Codex markers
  record ``{runtime, thread_id, record_offset, record_hash, turn_id, item_id,
  updated_at}`` and reconstruct canonical history by replaying rollback records.

* **Transcript source** — Claude uses the append-only FULL mirror at
  ``~/.multiverse/full_jsonls/<claude_uuid>.jsonl`` (survives compaction). Codex uses
  its native rollout JSONL directly through :mod:`.codex_transcript`; no second mirror
  is written. The agent's current runtime session is resolved fresh each call.

Semantics:

* ``initialize_marker`` sets the marker to the **last turn that exists right now** — so
  the first partial transcript covers "from now on", not the entire history. Called when
  a workspace first connects to SecondBrain. Idempotent: a second call is a no-op.
* A partial transcript is the content after the runtime-specific marker up to the latest
  history point. Claude advances by turn; Codex can advance within a partial turn.
* **Peek + commit split** (``peek_partial_transcript`` / ``commit_marker``) lets the
  connector advance the marker only AFTER the event is durably queued: ``peek`` returns
  the text plus an opaque ``cursor`` capturing exactly the point it measured up to, and
  ``commit`` stores that cursor. Because the cursor is fixed at peek time, a turn that
  arrives between peek and commit is never skipped — at-least-once, no gaps.

Reverts (the user restored the conversation to an earlier point and continued on a new
branch): the marked turn is now on an abandoned branch, absent from the live-chain forest
that ``parse_session`` returns. We do NOT re-dump — we walk ``parentUuid`` up from the
marker's ``row_uuid`` through the (still-present, append-only) abandoned rows until we
reach a row that IS on the live chain; the turn that owns that row is the effective marker
(the divergence point), and we deliver only the turns after it. Its persistence re-anchors
the marker back onto the live chain on the next commit.

Codex rollback records leave abandoned turns in the append-only rollout. The Codex adapter
replays the log at both the marker and current points, finds their longest canonical
turn/item prefix, and delivers the current suffix from that divergence.

Cross-uuid boundaries (a fold mints a new claude_uuid whose fresh full jsonl contains
neither the old ``turn_id`` nor the old ``row_uuid``): the walk finds no on-chain ancestor,
so we deliver the whole current forest once. That is bounded — a folded session is already
compacted — and re-anchors the marker to the new uuid's latest turn.

Dependency-light on purpose (stdlib + :mod:`.paths` + :mod:`.full_jsonl` + the vendored
``compaction`` parser; :mod:`.sessions` is imported lazily inside the resolver to avoid an
import cycle and its heavier deps). This module is pure-ish: JSONL reads + marker files, no
network, no side effects beyond the marker it owns.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import full_jsonl
from .atomic_files import atomic_write_json
from .paths import multiverse_dir

# Make the vendored compaction package importable (same pattern as full_jsonl /
# sb_connector): vendor/ on sys.path so ``from compaction... import`` resolves.
_VENDOR = str(Path(__file__).resolve().parent.parent / "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


#: Max length of a tool call's rendered arguments before truncation. Tool *results*
#: are dropped entirely (per spec), but a call's args can still be large (e.g. a Write
#: payload), so we cap them to keep the transcript compact.
ARG_TRUNC = 100


# ---------------------------------------------------------------------------
# Marker storage — one JSON file per (cwd, agent) under the connector state dir
# ---------------------------------------------------------------------------

def _markers_dir() -> Path:
    return multiverse_dir() / "secondbrain" / "transcript_markers"


def _resolve_cwd(cwd: str) -> str:
    return str(Path(cwd).expanduser().resolve())


def _marker_key(cwd: str, agent_name: str) -> str:
    """Stable, collision-safe filename stem for a marker.

    Human-readable agent slug + a short hash of the exact (resolved cwd, agent name)
    pair, so two agents that sanitize to the same slug (or the same name in two
    workspaces) never share a marker file.
    """
    resolved = _resolve_cwd(cwd)
    digest = hashlib.sha256(f"{resolved}\x00{agent_name}".encode()).hexdigest()[:16]
    safe_agent = re.sub(r"[^a-zA-Z0-9_-]", "-", agent_name).strip("-") or "agent"
    return f"{safe_agent}-{digest}"


def _marker_path(cwd: str, agent_name: str) -> Path:
    return _markers_dir() / f"{_marker_key(cwd, agent_name)}.json"


def read_marker(cwd: str, agent_name: str) -> dict | None:
    """The stored delivery marker for an agent, or ``None`` if never initialized.

    Claude and Codex use runtime-specific cursor fields while sharing the stable
    ``(cwd, agent_name)`` marker identity.
    """
    try:
        return json.loads(_marker_path(cwd, agent_name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_marker(
    cwd: str,
    agent_name: str,
    claude_uuid: str | None,
    turn_id: str | None,
    row_uuid: str | None,
) -> dict:
    marker = {
        "cwd": _resolve_cwd(cwd),
        "agent_name": agent_name,
        "claude_uuid": claude_uuid,
        "turn_id": turn_id,
        "row_uuid": row_uuid,
        "updated_at": _now_iso(),
    }
    path = _marker_path(cwd, agent_name)
    atomic_write_json(path, marker)
    return marker


def _write_codex_marker(cwd: str, agent_name: str, cursor: dict) -> dict:
    marker = {
        "cwd": _resolve_cwd(cwd),
        "agent_name": agent_name,
        "runtime": "codex",
        "thread_id": cursor.get("thread_id"),
        "record_offset": cursor.get("record_offset"),
        "record_hash": cursor.get("record_hash"),
        "turn_id": cursor.get("turn_id"),
        "item_id": cursor.get("item_id"),
        "updated_at": _now_iso(),
    }
    path = _marker_path(cwd, agent_name)
    atomic_write_json(path, marker)
    return marker


def _now_iso() -> str:
    # UTC, no microseconds — matches the connector's ``_now_iso`` style.
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Source resolution: agent name -> current runtime session -> parsed history
# ---------------------------------------------------------------------------


def _resolve_agent_row(cwd: str, agent_name: str):  # noqa: ANN202
    from .sessions import find_agent_by_name  # lazy: heavy deps, avoid cycle

    return find_agent_by_name(cwd, agent_name)


def _load_forest(claude_uuid: str, cwd: str):
    """Sync the FULL mirror then parse it into a ``Forest`` (oldest first).

    Returns ``[]`` if the full jsonl doesn't exist even after a sync attempt.
    """
    from compaction.parser import filter_compactable, parse_session  # noqa: E402

    full_jsonl.sync_full(claude_uuid, cwd)
    full = full_jsonl.full_jsonl_path(claude_uuid)
    if not full.exists():
        return []
    forest = parse_session(str(full))
    # Drop non-compactable structural noise (task_reminder / skill_listing /
    # deferred-tools / agent-listing / mcp-instructions deltas, edited-file
    # snapshots, non-IAM hook context) — they aren't conversation, and would
    # render as fake user speech. Real prompts, assistant output, tool calls,
    # inter-agent messages, and queued commands all survive.
    return filter_compactable(forest)


def _full_row_maps(claude_uuid: str) -> tuple[dict[str, str], dict[str, str]]:
    """``(parent_of, ts_of)`` from one light forward pass over the full jsonl.

    ``parent_of[uuid]`` → that row's ``parentUuid`` (for the revert walk over the
    append-only tree, which still holds abandoned branches). ``ts_of[uuid]`` → the
    row's ISO timestamp (for turn-header times). Best-effort: a missing/garbled file
    yields empty maps.
    """
    parent_of: dict[str, str] = {}
    ts_of: dict[str, str] = {}
    path = full_jsonl.full_jsonl_path(claude_uuid)
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = d.get("uuid")
                if not isinstance(u, str):
                    continue
                p = d.get("parentUuid")
                if isinstance(p, str) and p:
                    parent_of[u] = p
                ts = d.get("timestamp")
                if isinstance(ts, str):
                    ts_of[u] = ts
    except OSError:
        pass
    return parent_of, ts_of


def _turn_tip_uuid(turn) -> str | None:
    """A concrete JSONL row uuid at the tip of ``turn`` — the anchor stored in the
    marker so we can walk ``parentUuid`` if the turn later falls off the live chain.

    Prefer the last leaf's last source uuid (the turn's most-advanced row); fall back
    to the user-prompt row for a leaf-less turn.
    """
    for leaf in reversed(turn.leaves):
        if leaf.source_uuids:
            return leaf.source_uuids[-1]
    if turn.user_prompt is not None and turn.user_prompt.source_uuids:
        return turn.user_prompt.source_uuids[-1]
    return None


# ---------------------------------------------------------------------------
# Turn selection: everything after the marker, revert-aware
# ---------------------------------------------------------------------------

def _effective_after_index(forest, marker: dict | None, parent_of: dict[str, str]) -> int | None:
    """Index into ``forest`` of the last turn we've already delivered, on the CURRENT
    live chain. The caller delivers ``forest[index + 1:]``.

    Returns ``None`` to mean "deliver the whole forest" — the marker anchors nothing on
    the current chain (fresh agent, or a fold/uuid boundary where neither the turn_id nor
    the row_uuid exists in this file).

    Resolution order:
      1. **Fast path** — a turn whose ``turn_id`` equals the marker's ``turn_id`` is still
         on the live chain (no revert affecting the marker). Return its index.
      2. **Revert walk** — the marked turn fell off the chain (user restored to an earlier
         point). Walk ``parentUuid`` up from the marker's ``row_uuid`` through the abandoned
         branch until a row that IS on the live chain is reached; return that row's turn
         index (the divergence point).
      3. Neither resolves → ``None`` (whole forest).
    """
    marker_turn_id = marker.get("turn_id") if marker else None
    marker_row_uuid = marker.get("row_uuid") if marker else None

    # 1. Fast path: marked turn still on the live chain.
    if marker_turn_id is not None:
        for i, turn in enumerate(forest):
            if turn.turn_id == marker_turn_id:
                # Repair legacy stale markers: if the marker's row_uuid doesn't match
                # this turn's CURRENT tip, the marker was committed when the turn was
                # still partial (a pre-8dab34a bug — old peek anchored the cursor onto
                # a partial tail turn's user_prompt row before the assistant leaves
                # existed). Now that leaves are here, treat the turn as only partially
                # delivered: return i-1 so the caller re-emits it in full. The next
                # commit_marker will write a tip-anchored row_uuid, healing the marker.
                # Idempotent — a well-anchored marker (row_uuid == current tip) still
                # returns i.
                if marker_row_uuid:
                    current_tip = _turn_tip_uuid(turn)
                    if current_tip and marker_row_uuid != current_tip:
                        return i - 1  # forest[i:] re-includes this turn from the start
                return i

    # 2. Revert walk: map every on-chain row uuid → its turn index, then climb.
    row_uuid = marker.get("row_uuid") if marker else None
    if not row_uuid:
        return None
    uuid_to_idx: dict[str, int] = {}
    for i, turn in enumerate(forest):
        if turn.user_prompt is not None:
            for u in turn.user_prompt.source_uuids:
                uuid_to_idx[u] = i
        for leaf in turn.leaves:
            for u in leaf.source_uuids:
                uuid_to_idx[u] = i
    cur: str | None = row_uuid
    seen: set[str] = set()
    while cur and cur not in seen:
        idx = uuid_to_idx.get(cur)
        if idx is not None:
            return idx
        seen.add(cur)
        cur = parent_of.get(cur)

    # 3. No on-chain ancestor (crossed a fold/uuid boundary).
    return None


# ---------------------------------------------------------------------------
# Rendering — compact, bracketed lines; thinking omitted; tool results dropped
# ---------------------------------------------------------------------------

def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        iso = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ts


def _short_args(inp) -> str:
    """One-line rendering of a tool's input, truncated to ``ARG_TRUNC``."""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "url", "query", "pattern", "prompt"):
            v = inp.get(key)
            if isinstance(v, str) and v:
                s = v
                break
        else:
            s = json.dumps(inp, ensure_ascii=False)
    else:
        s = str(inp)
    s = " ".join(s.split())  # collapse whitespace so it stays one line
    return s if len(s) <= ARG_TRUNC else s[: ARG_TRUNC - 1] + "…"


def _render_leaf(leaf, error_tool_ids: set[str]) -> str | None:
    """Render one assistant-region leaf to a bracketed block, or ``None`` to skip.

    Thinking is omitted; tool *results* are dropped (an error surfaces as a marker on
    the matching tool call, or as a standalone line for an orphan error result).
    """
    kind = leaf.kind
    if kind == "thinking":
        return None
    if kind == "asst_text":
        text = (leaf.content.get("text") or "").strip()
        return f"[assistant] {text}" if text else None
    if kind == "user_text":
        # Mid-turn user record (Stop-hook feedback, queued command, inter-agent message).
        text = (leaf.content.get("text") or "").strip()
        if text:
            return f"[user] {text}"
        imgs = getattr(leaf, "images", None) or []
        return f"[user] <{len(imgs)} image(s)>" if imgs else None
    if kind == "tool_use":
        name = leaf.content.get("name", "?")
        args = _short_args(leaf.content.get("input", {}))
        err = " (error)" if leaf.content.get("tool_use_id") in error_tool_ids else ""
        return f"[tool:{name}] {args}{err}".rstrip()
    if kind == "tool_exchange":
        # Auto-merged use+result (only appears if preprocessing ran); result omitted.
        name = leaf.content.get("name", "?")
        args = _short_args(leaf.content.get("input", {}))
        err = " (error)" if leaf.content.get("is_error") else ""
        return f"[tool:{name}] {args}{err}".rstrip()
    if kind == "tool_result":
        # Results are dropped. Keep only an ORPHAN error (its tool_use lives in an
        # earlier, already-delivered turn, so no call line carries the marker here).
        tid = leaf.content.get("tool_use_id")
        if leaf.content.get("is_error") and tid not in error_tool_ids:
            return "[tool-result] (error)"
        return None
    return None


def _render_turn(turn, ts_map: dict[str, str]) -> str:
    lines: list[str] = []
    # Header with the user-prompt row's wall-clock time when we can find it.
    ts = ""
    if turn.user_prompt is not None and turn.user_prompt.source_uuids:
        ts = _fmt_ts(ts_map.get(turn.user_prompt.source_uuids[0]))
    lines.append(f"[turn {ts}]" if ts else "[turn]")

    if turn.user_prompt is not None:
        ptext = (turn.user_prompt.content.get("text") or "").strip()
        if ptext:
            lines.append(f"[user] {ptext}")

    # Which tool calls errored, so the call line (not a dropped result) carries it.
    error_tool_ids: set[str] = set()
    for leaf in turn.leaves:
        if leaf.kind == "tool_result" and leaf.content.get("is_error"):
            tid = leaf.content.get("tool_use_id")
            if tid:
                error_tool_ids.add(tid)

    for leaf in turn.leaves:
        line = _render_leaf(leaf, error_tool_ids)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _render_forest(turns, ts_map: dict[str, str]) -> str:
    return "\n\n".join(_render_turn(t, ts_map) for t in turns)


# ---------------------------------------------------------------------------
# Public API — the connector calls these
# ---------------------------------------------------------------------------

def initialize_marker(cwd: str, agent_name: str) -> bool:
    """Set the delivery marker to the agent's **latest history point**, once.

    Idempotent: if a marker already exists this is a no-op and returns ``False``.
    Returns ``True`` when a new marker was written. Call this when a workspace first
    connects to SecondBrain — so the first partial transcript covers "from now on"
    rather than dumping the whole backlog.

    An agent that can't be resolved (unknown name, no live/dormant session, no native
    session ID) also returns ``False`` — the connector can retry once the agent exists.
    """
    if read_marker(cwd, agent_name) is not None:
        return False
    row = _resolve_agent_row(cwd, agent_name)
    if row is None:
        return False
    if row.runtime == "codex":
        from . import codex_transcript
        from .sessions import CODEX_HOME

        thread_id = row.session_id
        if not thread_id and row.session is not None:
            thread_id = row.session.runtime_session_id
        if not thread_id:
            return False
        cursor = codex_transcript.initialize_cursor(CODEX_HOME, thread_id)
        if cursor is None:
            return False
        _write_codex_marker(cwd, agent_name, cursor)
        return True
    claude_uuid = row.claude_uuid
    if not claude_uuid:
        return False
    forest = _load_forest(claude_uuid, cwd)
    if forest:
        last = forest[-1]
        _write_marker(cwd, agent_name, claude_uuid, last.turn_id, _turn_tip_uuid(last))
    else:
        _write_marker(cwd, agent_name, claude_uuid, None, None)
    return True


def peek_partial_transcript(cwd: str, agent_name: str) -> tuple[str, dict] | None:
    """``(transcript_text, cursor)`` for turns after the marker up to the latest.

    Does NOT advance the marker — pass ``cursor`` to :func:`commit_marker` once the
    event is durably queued. ``cursor`` is opaque (a JSON-able dict) and captures the
    exact point measured, so a turn arriving between peek and commit is never skipped.

    Returns ``None`` if the agent can't be resolved. Returns ``("", cursor)`` when there
    are no new turns (``cursor`` then equals the current marker position). If no marker
    exists yet, this behaves as if the marker were "before all history" and delivers the
    whole forest — but the connector is expected to :func:`initialize_marker` on first
    connect so this only happens for a genuinely new agent.
    """
    row = _resolve_agent_row(cwd, agent_name)
    if row is None:
        return None
    marker = read_marker(cwd, agent_name)
    if row.runtime == "codex":
        from . import codex_transcript
        from .sessions import CODEX_HOME

        thread_id = row.session_id
        if not thread_id and row.session is not None:
            thread_id = row.session.runtime_session_id
        if not thread_id:
            return None
        return codex_transcript.peek_partial_transcript(CODEX_HOME, thread_id, marker)

    claude_uuid = row.claude_uuid
    if not claude_uuid:
        return None

    forest = _load_forest(claude_uuid, cwd)
    parent_of, ts_of = _full_row_maps(claude_uuid)

    idx = _effective_after_index(forest, marker, parent_of)
    start = 0 if idx is None else idx + 1
    new_turns = forest[start:]

    if new_turns:
        text = _render_forest(new_turns, ts_of)
        # Which turn to anchor the cursor onto (commit_marker persists this so the next
        # peek's _effective_after_index knows what's already been delivered):
        # normally the LAST new turn. EXCEPT when that last new turn is "partial" —
        # it has a user_prompt row but no assistant leaves yet (a user.prompt event
        # firing between UserPromptSubmit and Stop, before the agent has responded).
        # If we advanced the marker into a partial turn, _effective_after_index would
        # match it by turn_id on the NEXT peek and treat the whole turn as delivered,
        # losing the agent's response when Stop later fires. Anchor on the previous
        # turn (fully-delivered) instead — SB gets deliberate redundancy in the next
        # event (per the "cheap-now-LLM-later" full-is-view-on-demand design), and
        # nothing is dropped.
        last_turn = new_turns[-1]
        if not last_turn.leaves and len(new_turns) > 1:
            anchor_turn = new_turns[-2]  # skip the partial tail
        elif not last_turn.leaves:
            # only one new turn and it's partial → don't advance at all; re-anchor onto
            # the pre-marker turn so agent.response gets the whole (soon-to-be-complete)
            # turn on its next peek. If idx is None (fresh marker, no prior turn), just
            # leave the cursor where the marker was — commit becomes a no-op.
            anchor_turn = forest[idx] if (idx is not None and forest) else None
        else:
            anchor_turn = last_turn
    else:
        text = ""
        # Caught up. Re-anchor onto the current chain: if the revert walk landed us on
        # a divergence turn (idx set), point the cursor there so the persisted marker is
        # back on the live chain; otherwise leave it (empty forest → nothing to anchor).
        anchor_turn = forest[idx] if (idx is not None and forest) else None

    if anchor_turn is not None:
        cursor = {
            "claude_uuid": claude_uuid,
            "turn_id": anchor_turn.turn_id,
            "row_uuid": _turn_tip_uuid(anchor_turn),
        }
    else:
        # Nothing to advance to — preserve the existing marker position verbatim so a
        # commit is a harmless no-op rather than a reset.
        cursor = {
            "claude_uuid": claude_uuid,
            "turn_id": marker.get("turn_id") if marker else None,
            "row_uuid": marker.get("row_uuid") if marker else None,
        }
    return text, cursor


def commit_marker(cwd: str, agent_name: str, cursor: dict) -> None:
    """Advance the delivery marker to ``cursor`` (from :func:`peek_partial_transcript`).

    Call this AFTER the ``agent.response`` event is durably queued, so a crash between
    peek and commit re-delivers (overlapping, deduped by SB) rather than dropping turns.
    """
    if not isinstance(cursor, dict):
        return
    if cursor.get("runtime") == "codex":
        _write_codex_marker(cwd, agent_name, cursor)
        return
    _write_marker(
        cwd,
        agent_name,
        cursor.get("claude_uuid"),
        cursor.get("turn_id"),
        cursor.get("row_uuid"),
    )


def partial_transcript(cwd: str, agent_name: str) -> str:
    """Convenience: peek + commit atomically. Returns the transcript and advances the
    marker in one step. Prefer the peek/commit split when you need at-least-once
    durability across the event being queued. Returns ``""`` when the agent can't be
    resolved or there are no new turns."""
    peeked = peek_partial_transcript(cwd, agent_name)
    if peeked is None:
        return ""
    text, cursor = peeked
    commit_marker(cwd, agent_name, cursor)
    return text
