"""Stdlib-only, idempotent live → full mirror synchronization.

Callers hold the existing per-mirror sync lock. The UUID marker remains the
compaction/recovery anchor. A separate cursor remembers *all* consumed bytes,
including UUID-less metadata after that anchor; it is never trusted across a
changed marker, changed mirror, or rewritten live prefix.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path

from .atomic_files import atomic_write_bytes, atomic_write_json, atomic_write_text


def signature(path: Path) -> tuple[int, ...]:
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _read_marker(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _record(line: bytes) -> dict:
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def _fingerprint(line: bytes) -> bytes:
    # Existing mirrors reserialize JSON, so whitespace/key order can differ.
    row = _record(line)
    return json.dumps(row, sort_keys=True).encode() if row else line.strip()


def _tail_overlap(existing: deque[bytes], prefix: list[bytes]) -> int:
    """Longest prefix matching the existing tail, in linear time (KMP)."""
    if not prefix:
        return 0
    fallback = [0] * len(prefix)
    matched = 0
    for i in range(1, len(prefix)):
        while matched and prefix[i] != prefix[matched]:
            matched = fallback[matched - 1]
        if prefix[i] == prefix[matched]:
            matched += 1
        fallback[i] = matched
    matched = 0
    for line in existing:
        value = _fingerprint(line)
        while matched and (matched == len(prefix) or value != prefix[matched]):
            matched = fallback[matched - 1]
        if value == prefix[matched]:
            matched += 1
    return matched


def sync_mirror(live: Path, full: Path, marker_path: Path) -> int:
    """Append complete, previously unconsumed live records. Never rewrite full."""
    if not live.is_file():
        return 0
    full.parent.mkdir(parents=True, exist_ok=True)
    cursor_path = full.with_suffix(".cursor.json")
    marker = _read_marker(marker_path)
    live_sig = signature(live)
    full_sig = signature(full) if full.exists() else None
    try:
        cursor = json.loads(cursor_path.read_text())
        if not isinstance(cursor, dict):
            cursor = {}
    except (OSError, ValueError):
        cursor = {}
    valid = (
        cursor.get("version") == 1 and cursor.get("marker") == marker
        and full_sig is not None and cursor.get("full") == list(full_sig)
    )
    if valid and cursor.get("live") == list(live_sig):
        return 0

    data = live.read_bytes()
    # An in-flight writer may have only written half a JSON record. Leave it
    # unconsumed until the newline arrives, including during bootstrap.
    data = data[:data.rfind(b"\n") + 1]
    lines = data.splitlines(keepends=True)

    def save_cursor(last_marker: str | None) -> None:
        # Use the PRE-read live signature: a racing append must force another
        # pass, not be mistaken for data included in this snapshot.
        atomic_write_json(cursor_path, {
            "version": 1, "marker": last_marker, "live": live_sig,
            "full": signature(full), "offset": len(data),
            "prefix": hashlib.sha256(data).hexdigest(),
        })

    if full_sig is None:
        atomic_write_bytes(full, data)
        last = next((u for line in reversed(lines)
                     if isinstance(u := _record(line).get("uuid"), str)), None)
        if last:
            atomic_write_text(marker_path, last)
        save_cursor(last or marker)
        return sum(bool(line.strip()) for line in lines)

    offset = cursor.get("offset")
    append_only = (
        valid and type(offset) is int and 0 <= offset <= len(data)
        and hashlib.sha256(data[:offset]).hexdigest() == cursor.get("prefix")
    )
    if append_only:
        pending = data[offset:].splitlines(keepends=True)
    elif marker is None:
        pending = lines
    else:
        anchor = next((i for i, line in enumerate(lines)
                       if _record(line).get("uuid") == marker), None)
        if anchor is None:
            # Compaction owns updating the marker. Do not mirror a synthetic
            # compacted prefix while waiting for that update.
            return 0
        pending = lines[anchor + 1:]

    pending = [line for line in pending if line.strip()]
    if not pending:
        save_cursor(marker)
        return 0

    uuids: set[str] = set()
    last_uuid = None
    # Only retain as much legacy metadata as could match our pending prefix.
    tail: deque[bytes] = deque(maxlen=len(pending))
    with full.open("rb") as fh:
        for line in fh:
            # Bloated legacy mirrors are mostly UUID-less bookkeeping. Do
            # not JSON-decode it just to build the parent-UUID index.
            u = _record(line).get("uuid") if b'"uuid"' in line else None
            if isinstance(u, str):
                uuids.add(u)
                last_uuid = u
                tail.clear()
            elif not append_only and line.strip():
                tail.append(line)

    if not append_only and last_uuid == marker:
        # Upgrade legacy mirrors without recopying an already mirrored tail.
        # Compare occurrences in order, not a set: identical UUID-less user
        # attachments can legitimately occur more than once.
        prefix = []
        for line in pending:
            if isinstance(_record(line).get("uuid"), str):
                break
            prefix.append(_fingerprint(line))
        pending = pending[_tail_overlap(tail, prefix):]

    appended = 0
    last_marker = marker
    if pending:
        with full.open("ab") as dst:
            for line in pending:
                if not line.strip():
                    continue
                row = _record(line)
                parent = row.get("parentUuid")
                if isinstance(parent, str) and parent and parent not in uuids:
                    row["parentUuid"] = last_uuid
                u = row.get("uuid")
                if isinstance(u, str):
                    uuids.add(u)
                    last_uuid = last_marker = u
                dst.write(json.dumps(row).encode() + b"\n" if row else line)
                appended += 1
    if last_marker and last_marker != marker:
        atomic_write_text(marker_path, last_marker)
    save_cursor(last_marker)
    return appended
