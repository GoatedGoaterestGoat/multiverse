"""Two-JSONL architecture: live (compactable) ↔ full (always-uncompacted).

Each claude session has:
  ~/.claude/projects/<slug>/<uuid>.jsonl  — LIVE: what claude reads/writes.
  ~/.multiverse/full_jsonls/<uuid>.jsonl   — FULL: append-only mirror, never
                                             compacted. Source of truth for
                                             tree building and total-token
                                             counts displayed at compaction
                                             time.
  ~/.multiverse/full_jsonls/<uuid>.marker  — uuid of the last live row that
                                             was mirrored to full. Used by
                                             the mirror to skip rows it has
                                             already copied. Tracking by
                                             uuid (not byte offset) so the
                                             marker survives compaction
                                             rewriting live.

Mirror sync (incremental):
  walk live.jsonl → skip rows up to and including <marker_uuid> → append
  the rest to full.jsonl → set marker = last appended row's uuid.

Bootstrap (legacy support):
  if full.jsonl is missing, copy live.jsonl verbatim and set
  marker = last uuid in the copy.

The fast-path mirror that runs as a hook on every UserPromptSubmit / Stop /
SessionEnd is implemented stdlib-only in scripts/mirror-jsonl.py to keep
hook-fire latency low. This module is the richer version used by the
compaction widget — it leans on `compaction.parser` for token/turn counts.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .paths import claude_projects_dir, multiverse_dir

# Make the vendored compaction package importable. The compaction widget
# wants per-turn token counts and total turn counts; both live in the
# parser. We do the path injection at module import time so callers don't
# have to repeat it.
_VENDOR = str(Path(__file__).resolve().parent.parent / "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _full_dir() -> Path:
    return multiverse_dir() / "full_jsonls"


def full_jsonl_path(claude_uuid: str) -> Path:
    return _full_dir() / f"{claude_uuid}.jsonl"


def marker_path(claude_uuid: str) -> Path:
    return _full_dir() / f"{claude_uuid}.marker"


def cwd_slug(cwd: str) -> str:
    """Claude Code's project-slug rule: replace any non-alphanumeric with `-`."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def live_jsonl_path(claude_uuid: str, cwd: str) -> Path:
    return claude_projects_dir() / cwd_slug(cwd) / f"{claude_uuid}.jsonl"


# ---------------------------------------------------------------------------
# Marker I/O
# ---------------------------------------------------------------------------

def read_marker(claude_uuid: str) -> str | None:
    try:
        v = marker_path(claude_uuid).read_text().strip()
        return v or None
    except OSError:
        return None


def write_marker(claude_uuid: str, uuid: str) -> None:
    p = marker_path(claude_uuid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(uuid)


def last_uuid_in(path: Path) -> str | None:
    """Return the uuid of the last row in a JSONL file (or None if empty)."""
    last: str | None = None
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = d.get("uuid")
                if isinstance(u, str):
                    last = u
    except OSError:
        return None
    return last


# Backwards-compat alias for early callers that used the underscored name.
_last_uuid_in = last_uuid_in


# ---------------------------------------------------------------------------
# Sync + bootstrap
# ---------------------------------------------------------------------------

def sync_full(claude_uuid: str, cwd: str) -> int:
    """Sync live.jsonl into full.jsonl; bootstrap if missing.

    Returns the number of rows appended (0 means up-to-date or no live).
    """
    full_dir = _full_dir()
    full_dir.mkdir(parents=True, exist_ok=True)

    live = live_jsonl_path(claude_uuid, cwd)
    full = full_jsonl_path(claude_uuid)

    if not live.is_file():
        return 0

    if not full.exists():
        # Bootstrap: full is identical to live.
        full.write_bytes(live.read_bytes())
        last = _last_uuid_in(full)
        if last:
            write_marker(claude_uuid, last)
        return _row_count(full)

    # Load full's existing uuids so we can detect (and heal) parentUuid
    # pointers that would otherwise leave full's chain broken — e.g.
    # across a compaction boundary, where the previous live tip is a
    # synthetic uuid that never made it into full.
    full_uuids: set[str] = set()
    full_last_uuid: str | None = None
    try:
        with full.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = d.get("uuid")
                if isinstance(u, str):
                    full_uuids.add(u)
                    full_last_uuid = u
    except OSError:
        pass

    marker = read_marker(claude_uuid)
    last_appended = marker
    found = marker is None
    appended = 0
    with live.open() as src, full.open("a") as dst:
        for line in src:
            stripped = line.strip()
            if not stripped:
                continue
            if not found:
                try:
                    d = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if d.get("uuid") == marker:
                    found = True
                continue
            try:
                d = json.loads(stripped)
            except json.JSONDecodeError:
                dst.write(line if line.endswith("\n") else line + "\n")
                appended += 1
                continue
            parent = d.get("parentUuid")
            if isinstance(parent, str) and parent and parent not in full_uuids:
                d["parentUuid"] = full_last_uuid
            u = d.get("uuid")
            if isinstance(u, str):
                full_uuids.add(u)
                full_last_uuid = u
                last_appended = u
            dst.write(json.dumps(d) + "\n")
            appended += 1
    if last_appended and last_appended != marker:
        write_marker(claude_uuid, last_appended)
    return appended


def _row_count(path: Path) -> int:
    n = 0
    try:
        with path.open() as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        pass
    return n


# ---------------------------------------------------------------------------
# Token / turn counts (from full.jsonl, syncing first)
# ---------------------------------------------------------------------------

def _parse_full(claude_uuid: str, cwd: str):
    sync_full(claude_uuid, cwd)
    full = full_jsonl_path(claude_uuid)
    if not full.exists():
        return []
    from compaction.parser import parse_session  # noqa: E402
    return parse_session(str(full))


def total_token_count(claude_uuid: str, cwd: str) -> int:
    """Sum every leaf token (including hoisted user prompts) across all
    turns in the FULL jsonl. This is the uncompacted cost the live agent
    would pay if no compaction were applied."""
    forest = _parse_full(claude_uuid, cwd)
    total = 0
    for turn in forest:
        if turn.user_prompt is not None:
            total += turn.user_prompt.tokens
        for leaf in turn.leaves:
            total += leaf.tokens
    return total


def total_turn_count(claude_uuid: str, cwd: str) -> int:
    return len(_parse_full(claude_uuid, cwd))


def live_stats(claude_uuid: str, cwd: str) -> tuple[int, int]:
    """Return ``(total_tokens, total_turns)`` for the LIVE jsonl — what
    the live agent currently sees on resume. Reflects any compaction
    state. Zero/zero if the live file is missing."""
    live = live_jsonl_path(claude_uuid, cwd)
    if not live.is_file():
        return 0, 0
    from compaction.parser import parse_session  # noqa: E402
    forest = parse_session(str(live))
    tokens = 0
    for turn in forest:
        if turn.user_prompt is not None:
            tokens += turn.user_prompt.tokens
        for leaf in turn.leaves:
            tokens += leaf.tokens
    return tokens, len(forest)


# mtime-keyed cache of live_stats results, per live jsonl path. The
# dashboard's 1-second refresh calls live_stats_cached once per row; without
# caching that's a full chain-walk parse of every live jsonl every tick.
_LIVE_STATS_CACHE: dict[str, tuple[float, tuple[int, int]]] = {}


def live_stats_cached(claude_uuid: str, cwd: str) -> tuple[int, int]:
    """mtime-cached wrapper around ``live_stats``. Same return value; skips
    the parse when the live jsonl hasn't changed since the last call."""
    live = live_jsonl_path(claude_uuid, cwd)
    try:
        mtime = live.stat().st_mtime
    except OSError:
        return 0, 0
    key = str(live)
    cached = _LIVE_STATS_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    result = live_stats(claude_uuid, cwd)
    _LIVE_STATS_CACHE[key] = (mtime, result)
    return result


def structural_tokens(claude_uuid: str, cwd: str) -> int:
    """Return the derived "structural" token count for a live session —
    everything the model sees each turn that ISN'T in the JSONL
    transcript: system prompt template, tool schemas, memory files,
    skills, agents, MCP server descriptions.

    Derivation, not measurement. Claude records the exact API-billed
    input token count (`input_tokens + cache_creation + cache_read`) on
    every real assistant row's ``message.usage``. The parser gives the
    transcript-only sum for the same file state. The difference is
    everything the model saw that the parser can't:

        structural = usage_total - parser_at_snapshot

    THE SNAPSHOT CHOICE MATTERS. Claude's cached microcompaction evicts
    old tool results from the actual API context via cache-editing —
    with no marker in the JSONL (the full content stays on disk). Once
    that kicks in on a long session, the parser (which counts all the
    on-disk content) over-counts relative to the real context, so
    ``usage_total - parser`` at post-eviction requests goes negative.
    But BEFORE any eviction — while usage grows monotonically request
    over request — the parser matches the real context and the
    derivation is clean.

    So we walk the samples and take the PEAK of the monotonically-non-
    decreasing prefix — the last request before any significant usage
    drop. Sessions that never hit eviction (short, or without enough
    tool results) get the last sample, which reflects the CURRENT
    structural (including any deferred tools / MCP that loaded over
    the first few turns, and — critically — any structural growth
    since the last compaction). Sessions that did hit eviction get the
    peak-just-before-drop, which is the last measurement the parser
    can still trust.

    Returns 0 when no usable usage exists yet (freshly-compacted before
    its first post-compaction API call, or a brand-new session).
    """
    live = live_jsonl_path(claude_uuid, cwd)
    if not live.is_file():
        return 0
    # Collect real (non-msg_priv) usage snapshots in file order.
    samples: list[tuple[dict, str, int]] = []  # (usage, uuid, total)
    try:
        with live.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "assistant":
                    continue
                msg = row.get("message", {})
                mid = msg.get("id", "")
                if isinstance(mid, str) and mid.startswith("msg_priv"):
                    continue
                usage = msg.get("usage")
                if not (isinstance(usage, dict)
                        and isinstance(usage.get("input_tokens"), int)):
                    continue
                uuid = row.get("uuid")
                if uuid is None:
                    continue
                total = (
                    usage["input_tokens"]
                    + (usage.get("cache_creation_input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                )
                samples.append((usage, uuid, total))
    except OSError:
        return 0

    if not samples:
        return 0

    # Walk forward; peak = last index that hasn't yet been undercut by a
    # significant drop. A "significant" drop is claude's microcompaction
    # dropping tool_results (typically tens of thousands of tokens); noise
    # from streaming/thinking round-trips within a turn is well under 5k.
    _EVICTION_DROP = 5000
    peak_i = 0
    for i in range(1, len(samples)):
        if samples[i][2] < samples[i - 1][2] - _EVICTION_DROP:
            break
        peak_i = i
    snapshot_usage, snapshot_uuid, usage_total = samples[peak_i]
    # Parser tokens for the same file state UP TO the snapshot's uuid —
    # a leaf whose source_uuids contains snapshot_uuid marks the point;
    # sum tokens for everything at-or-before it.
    from compaction.parser import parse_session  # noqa: E402
    forest = parse_session(str(live))
    parser_at_snapshot = 0
    found = False
    for turn in forest:
        if found:
            break
        if turn.user_prompt is not None:
            parser_at_snapshot += turn.user_prompt.tokens
            if snapshot_uuid in turn.user_prompt.source_uuids:
                found = True
                continue
        for leaf in turn.leaves:
            parser_at_snapshot += leaf.tokens
            if snapshot_uuid in leaf.source_uuids:
                found = True
                break
    if not found:
        # Snapshot uuid isn't on the current chain (a revert-branch
        # assistant row, or metadata that doesn't map to a leaf). Fall
        # back to the full parser total — under-estimates structural if
        # there was real content past the snapshot, never over-estimates.
        parser_at_snapshot = 0
        for turn in forest:
            if turn.user_prompt is not None:
                parser_at_snapshot += turn.user_prompt.tokens
            for leaf in turn.leaves:
                parser_at_snapshot += leaf.tokens
    return max(0, usage_total - parser_at_snapshot)


# mtime-keyed cache of structural_tokens results.
_STRUCTURAL_CACHE: dict[str, tuple[float, int]] = {}


def structural_tokens_cached(claude_uuid: str, cwd: str) -> int:
    """mtime-cached wrapper around ``structural_tokens``. Structural is
    stable across turns for a session, but the file mtime changes on
    every turn, so we recompute — cheap for the dashboard's 1s tick and
    keeps the value fresh across compaction/resume boundaries."""
    live = live_jsonl_path(claude_uuid, cwd)
    try:
        mtime = live.stat().st_mtime
    except OSError:
        return 0
    key = str(live)
    cached = _STRUCTURAL_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    result = structural_tokens(claude_uuid, cwd)
    _STRUCTURAL_CACHE[key] = (mtime, result)
    return result


def last_real_usage_total(claude_uuid: str, cwd: str) -> int:
    """Total input tokens claude billed on the most recent real (non-
    ``msg_priv``) API request — literally what ``/context`` shows.

    ``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``
    from the last assistant row that recorded a usage object. Returns 0
    when no real API call has landed yet (freshly-compacted before its
    first turn, or a brand-new session).

    Preferable to ``live_stats + structural_tokens`` as the dashboard's
    displayed total, because it's a direct measurement of the current
    context size — no derivation, no drift, no over-count when claude's
    microcompaction evicts old tool results (which leaves the parser
    counting on-disk content that's no longer in context).
    """
    live = live_jsonl_path(claude_uuid, cwd)
    if not live.is_file():
        return 0
    last_total = 0
    try:
        with live.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "assistant":
                    continue
                msg = row.get("message", {})
                mid = msg.get("id", "")
                if isinstance(mid, str) and mid.startswith("msg_priv"):
                    continue
                usage = msg.get("usage")
                if not (isinstance(usage, dict)
                        and isinstance(usage.get("input_tokens"), int)):
                    continue
                last_total = (
                    usage["input_tokens"]
                    + (usage.get("cache_creation_input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                )
    except OSError:
        return 0
    return last_total


_LAST_USAGE_CACHE: dict[str, tuple[float, int]] = {}


def last_real_usage_total_cached(claude_uuid: str, cwd: str) -> int:
    """mtime-cached wrapper around ``last_real_usage_total``."""
    live = live_jsonl_path(claude_uuid, cwd)
    try:
        mtime = live.stat().st_mtime
    except OSError:
        return 0
    key = str(live)
    cached = _LAST_USAGE_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    result = last_real_usage_total(claude_uuid, cwd)
    _LAST_USAGE_CACHE[key] = (mtime, result)
    return result


def minimum_compacted_tokens(claude_uuid: str, cwd: str) -> tuple[int, int, int]:
    """Return ``(min_tokens, n_cached, n_total)`` — the smallest possible
    live-JSONL cost if every turn compacted to its most-collapsed frontier
    (Node root alone) plus its verbatim user-prompt text.

    For turns with a cached tree, the minimum per turn is
    ``user_prompt.tokens + root_tree.tokens``. Turns without a cached
    tree contribute their full leaf sum (over-estimate — an actual
    build would reduce them further). The caller inspects
    ``n_cached / n_total`` to know how tight the estimate is.

    Skips aborted-stub trees the same way ``compact_runner`` does —
    they aren't real summaries so their root tokens equal the leaf
    sum anyway; treating them as no-tree keeps the semantics honest.
    """
    forest = _parse_full(claude_uuid, cwd)
    if not forest:
        return 0, 0, 0

    cache_root = multiverse_dir() / "trees" / claude_uuid
    total_min = 0
    n_cached = 0
    for turn in forest:
        prompt_tokens = turn.user_prompt.tokens if turn.user_prompt else 0
        turn_min: int | None = None
        tree_path = cache_root / f"{turn.turn_id}.json"
        if tree_path.is_file():
            try:
                data = json.loads(tree_path.read_text())
                tree = data.get("tree")
                if isinstance(tree, dict) and not _is_aborted_stub(tree):
                    tok = tree.get("tokens")
                    if isinstance(tok, int):
                        turn_min = tok
                        n_cached += 1
            except (OSError, json.JSONDecodeError):
                pass
        if turn_min is None:
            # Fallback: sum of leaf tokens (what the turn would cost
            # uncompacted). Not the "true" minimum but the best
            # estimate we can give without a tree.
            turn_min = sum(l.tokens for l in turn.leaves)
        total_min += prompt_tokens + turn_min
    return total_min, n_cached, len(forest)


def _is_aborted_stub(tree: dict) -> bool:
    """A node whose sole 'summary' is EXACTLY the constructor's aborted-stub
    placeholder (``<construction aborted; uncollapsed frontier>``) — its
    children ARE its leaves (nothing was really merged), so the root's
    tokens equal the leaf sum and the tree is functionally equivalent to
    no tree at all.

    Exact match, not substring: a real summary can legitimately mention
    "aborted; uncollapsed" as content (e.g. a turn about debugging this
    very feature) without being an unbuilt stub itself."""
    if tree.get("type") != "node":
        return False
    summary = tree.get("summary", "")
    if not isinstance(summary, str):
        return False
    return summary == "<construction aborted; uncollapsed frontier>"


def list_orphan_fulls(cwd_filter: str | None = None) -> list[dict]:
    """Enumerate full_jsonls with no matching live JSONL.

    An "orphan" is a full.jsonl whose corresponding live JSONL
    (``$CLAUDE_HOME/projects/<slug(cwd)>/<uuid>.jsonl``) does not exist
    on disk — typically because claude code's ``cleanupPeriodDays``
    auto-reap deleted the live but our mirror persists independently.

    Returns a list of dicts, one per orphan:
      {uuid, title, cwd, size_bytes, mtime, turns, last_user_msg,
       trees_cached}

    ``cwd_filter`` (if given) restricts to orphans whose recorded cwd
    matches exactly. The RestoreScreen uses this to scope to the current
    monitor's workspace.
    """
    full_dir = _full_dir()
    if not full_dir.is_dir():
        return []

    # Build set of currently-live uuids, from every project slug under
    # $CLAUDE_HOME/projects — cheap enough to enumerate on demand and
    # avoids stale caching.
    live_uuids: set[str] = set()
    proj_root = claude_projects_dir()
    if proj_root.is_dir():
        try:
            for slug_dir in proj_root.iterdir():
                if not slug_dir.is_dir():
                    continue
                for f in slug_dir.iterdir():
                    if f.suffix == ".jsonl":
                        live_uuids.add(f.stem)
        except OSError:
            pass

    trees_dir = multiverse_dir() / "trees"

    orphans: list[dict] = []
    for f in full_dir.iterdir():
        if f.suffix != ".jsonl":
            continue
        uuid = f.stem
        if uuid in live_uuids:
            continue
        try:
            meta = _extract_full_meta(f)
        except OSError:
            continue
        if cwd_filter is not None and meta.get("cwd") != cwd_filter:
            continue
        n_trees = 0
        session_trees = trees_dir / uuid
        if session_trees.is_dir():
            try:
                n_trees = sum(
                    1 for p in session_trees.iterdir() if p.suffix == ".json"
                )
            except OSError:
                pass
        meta["trees_cached"] = n_trees
        orphans.append(meta)
    # Newest first.
    orphans.sort(key=lambda m: m.get("mtime", 0.0), reverse=True)
    return orphans


def _extract_full_meta(path: Path) -> dict:
    """Return metadata for one full JSONL — title, cwd, size, mtime,
    chain-turn count, last user message. One forward pass.
    """
    from compaction.parser import parse_session  # noqa: E402

    title: str | None = None
    cwd: str | None = None
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "custom-title":
                ct = d.get("customTitle")
                if isinstance(ct, str) and ct:
                    title = ct
            if not cwd:
                v = d.get("cwd")
                if isinstance(v, str) and v:
                    cwd = v

    # Chain-walk once for turns + last user message (skip synthetic
    # user rows the parser drops on the floor).
    forest = parse_session(str(path))
    turns = len(forest)
    last_user_msg = ""
    for turn in reversed(forest):
        if turn.user_prompt is not None:
            txt = turn.user_prompt.content.get("text", "") or ""
            if txt:
                last_user_msg = txt.strip()
                break

    stat = path.stat()
    return {
        "uuid": path.stem,
        "title": title or "(no title)",
        "cwd": cwd or "",
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "turns": turns,
        "last_user_msg": last_user_msg,
    }


def per_turn_tokens(claude_uuid: str, cwd: str) -> list[tuple[str, int]]:
    """Return [(turn_id, total_tokens_in_turn), ...] from full.jsonl,
    where total_tokens_in_turn includes the user_prompt leaf."""
    out: list[tuple[str, int]] = []
    for turn in _parse_full(claude_uuid, cwd):
        t = turn.user_prompt.tokens if turn.user_prompt else 0
        t += sum(l.tokens for l in turn.leaves)
        out.append((turn.turn_id, t))
    return out
