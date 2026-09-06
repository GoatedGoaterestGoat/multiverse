"""Codex dashboard counters, from the native statusline's token-usage source.

Rollout ``event_msg/token_count`` records contain snapshots, not deltas:
``last_token_usage.total_tokens`` is the current context size (including
Codex's post-compaction estimates). The statusline's ``used-tokens`` item
uses ``total_token_usage``'s non-cached input + output, via ``blended_total``.
Reasoning tokens are already included in output and must not be added again.

See Codex rust-v0.153.4, codex-rs/tui/src/token_usage.rs and
codex-rs/tui/src/chatwidget/status_surfaces.rs. No transcript tokenization,
Multiverse compaction, or SecondBrain state is involved here.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from .codex_transcript import find_rollout_path
from .paths import codex_home


@dataclass(frozen=True)
class CodexTokenUsage:
    live_tokens: int | None = None
    total_tokens: int | None = None


_UNKNOWN = CodexTokenUsage()
_CHUNK_SIZE = 64 * 1024
_PATH_TTL = 30.0
_MISSING_PATH_TTL = 5.0
_CACHE_LIMIT = 256


@dataclass
class _Entry:
    path: Path | None
    discovered_at: float
    signature: tuple[int, ...] | None = None
    usage: CodexTokenUsage = _UNKNOWN


_CACHE: OrderedDict[tuple[Path, str], _Entry] = OrderedDict()


def _signature(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _complete_lines_reverse(fh: BinaryIO, size: int) -> Iterator[bytes]:
    """Read a fixed snapshot backwards, ignoring an unfinished final record.

    Chunk fragments are joined only at a newline, keeping long tool/image
    records linear-time rather than repeatedly copying a growing buffer.
    """
    position = size
    fragments: list[bytes] = []
    trailing = True
    while position:
        count = min(position, _CHUNK_SIZE)
        position -= count
        fh.seek(position)
        parts = fh.read(count).split(b"\n")
        if len(parts) == 1:
            fragments.append(parts[0])
            continue
        if not trailing:
            yield parts[-1] + b"".join(reversed(fragments))
        trailing = False
        for line in reversed(parts[1:-1]):
            yield line
        fragments = [parts[0]]
    if not trailing:
        yield b"".join(reversed(fragments))


def _integer(value: object) -> int | None:
    # bool is an int in Python, but is not a token measurement.
    return value if type(value) is int else None


def _usage_from_record(raw: bytes) -> CodexTokenUsage | None:
    if b'"token_count"' not in raw:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    # Rate-limit-only token_count events have info=null and preserve usage.
    if not isinstance(info, dict):
        return None
    last = info.get("last_token_usage")
    total = info.get("total_token_usage")
    live = _integer(last.get("total_tokens")) if isinstance(last, dict) else None
    if live is not None and live < 0:
        live = None
    used = None
    if isinstance(total, dict):
        incoming = _integer(total.get("input_tokens"))
        cached = _integer(total.get("cached_input_tokens"))
        outgoing = _integer(total.get("output_tokens"))
        if incoming is not None and cached is not None and outgoing is not None:
            used = max(incoming - max(cached, 0), 0) + max(outgoing, 0)
            # Codex omits used-tokens until it has nonzero measured usage.
            used = used or None
    if live is None and used is None:
        return None
    return CodexTokenUsage(live, used)


def token_usage_cached(thread_id: str | None, home: Path | None = None) -> CodexTokenUsage:
    """Latest native counters for a wrapped or dormant thread, or unknown.

    Unchanged files require only stat; changed files are read backwards to
    the latest usage snapshot. Discovery is cached separately (including
    misses) so the dashboard doesn't glob the session tree on every tick.
    Inode/size/nanosecond timestamps invalidate replacement and truncation.
    All state is bounded, process-local, and keyed by Codex home + thread ID.
    """
    if not thread_id:
        return _UNKNOWN
    home = Path(home) if home is not None else codex_home()
    key = (home, thread_id)
    now = time.monotonic()
    entry = _CACHE.get(key)
    ttl = _PATH_TTL if entry and entry.path is not None else _MISSING_PATH_TTL
    if entry is None or now - entry.discovered_at >= ttl:
        path = find_rollout_path(home, thread_id)
        if entry is None or entry.path != path:
            entry = _Entry(path, now)
            _CACHE[key] = entry
        else:
            entry.discovered_at = now
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)
    if entry.path is None:
        return _UNKNOWN
    try:
        if _signature(entry.path.stat()) == entry.signature:
            return entry.usage
        with entry.path.open("rb") as fh:
            st = os.fstat(fh.fileno())
            usage = next(
                (usage for raw in _complete_lines_reverse(fh, st.st_size)
                 if (usage := _usage_from_record(raw)) is not None),
                _UNKNOWN,
            )
        entry.signature = _signature(st)
        entry.usage = usage
        return usage
    except OSError:
        # Do not show stale measurements after deletion or permission loss.
        entry.signature = None
        return _UNKNOWN
