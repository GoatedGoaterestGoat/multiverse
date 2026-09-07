"""Claude dashboard context capacity, from the recorded native statusline.

``context_window_cached`` answers "how big is this session's context
window?" with the exact ``context_window.context_window_size`` Claude Code
reported to its statusline (captured by ``claude_statusline``), or None.
The exact reader never infers a capacity from a model name: a bare id in the
transcript cannot tell a 200k session from a ``[1m]`` one, and a wrong
capacity would color a real session wrong. Unknown stays unknown.
For dashboard coloring, ``context_window_assumed`` instead falls back to
the explicitly configured assumption (1M by default) until capture arrives.

Records are durable and keyed by workspace slug + session id, so a dormant
row keeps the capacity it was measured at. The cache mirrors
``codex_usage``: an unchanged file costs one stat per tick, a changed one is
re-read, and a deleted or unreadable one reads as unknown rather than stale.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .claude_statusline import is_session_id, parse_record, record_path

_CACHE_LIMIT = 256
# What a Claude row is assumed to run under until its statusline has been
# captured: sessions that predate the launcher shim, dormant rows, or
# sessions started outside multiverse. bin/multiverse launches Claude as
# opus[1m] by default, so 1M is the honest expectation; override with
# MULTIVERSE_CLAUDE_CONTEXT_WINDOW when the default model is not a 1M one.
ASSUMED_CONTEXT_WINDOW = 1_000_000


@dataclass
class _Entry:
    signature: tuple[int, ...] | None = None
    context_window: int | None = None


_CACHE: OrderedDict[Path, _Entry] = OrderedDict()


def _signature(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def assumed_context_window() -> int:
    """The capacity assumed before a session's statusline is captured."""
    raw = os.environ.get("MULTIVERSE_CLAUDE_CONTEXT_WINDOW", "").strip()
    if not raw.isascii() or not raw.isdigit():
        return ASSUMED_CONTEXT_WINDOW
    try:
        value = int(raw)
    except ValueError:
        return ASSUMED_CONTEXT_WINDOW
    return value if value > 0 else ASSUMED_CONTEXT_WINDOW


def context_window_assumed(claude_uuid: str | None, cwd: str) -> int:
    """Exact recorded capacity when known, else ``assumed_context_window()``.

    The dashboard's Claude rows use this: a measured value wins the moment
    the record appears (the statusline renders at startup, before the first
    prompt), and until then the row is colored against the assumed window
    rather than left neutral.
    """
    measured = context_window_cached(claude_uuid, cwd)
    return measured if measured is not None else assumed_context_window()


def context_window_cached(claude_uuid: str | None, cwd: str) -> int | None:
    """Exact recorded context capacity for a Claude session, or None.

    ``cwd`` is the workspace the dashboard row belongs to; the record is
    filed under the same project slug as the session's live transcript.
    A record whose session id does not match is ignored, so a mis-filed or
    hand-edited entry can never color someone else's row.
    """
    # A malformed id or cwd must never raise into a dashboard redraw, nor
    # name a file outside the record dir: the slug is filename-safe by
    # construction, and the id is checked the way the recorder checks it.
    if not is_session_id(claude_uuid) or not isinstance(cwd, str):
        return None
    try:
        path = record_path(claude_uuid, cwd)
    except (OSError, ValueError):
        return None
    entry = _CACHE.get(path)
    if entry is None:
        entry = _Entry()
        _CACHE[path] = entry
    _CACHE.move_to_end(path)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)
    try:
        if _signature(path.stat()) == entry.signature:
            return entry.context_window
        with path.open("rb") as fh:
            st = os.fstat(fh.fileno())
            record = parse_record(fh.read(), claude_uuid)
        entry.signature = _signature(st)
        entry.context_window = record["context_window_size"] if record else None
        return entry.context_window
    except OSError:
        # Do not show a stale capacity after deletion or permission loss.
        entry.signature = None
        entry.context_window = None
        return None
